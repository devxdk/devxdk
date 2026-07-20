"""Tests for the build-leg member-digest handoff (leg -> publish contract)."""

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

import handoff as handoff_cli  # noqa: E402  (scripts/handoff.py, the CLI)
from devxdk_manifest import handoff  # noqa: E402  (the package module)


def _populate(d: pathlib.Path):
    """A representative leg directory: one archive blob + its .meta.json."""
    (d / "redis-8.8.0-windows-amd64.zip").write_bytes(b"PK\x03\x04 fake archive bytes")
    (d / "redis-8.8.0-windows-amd64.meta.json").write_text(
        json.dumps({"sha256": "a" * 64, "size": 22}), encoding="utf-8"
    )


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _populate(self.d)

    def test_write_then_verify(self):
        sha = handoff.write(self.d)
        self.assertRegex(sha, r"^[0-9a-f]{64}$")
        # The written manifest is present, excludes itself, and lists both files.
        manifest = handoff.verify(self.d, sha)
        paths = {m["path"] for m in manifest["members"]}
        self.assertEqual(paths, {"redis-8.8.0-windows-amd64.zip", "redis-8.8.0-windows-amd64.meta.json"})
        self.assertNotIn(handoff.MANIFEST_NAME, paths)

    def test_expected_hash_mismatch_fails(self):
        handoff.write(self.d)
        with self.assertRaises(handoff.HandoffError):
            handoff.verify(self.d, "b" * 64)

    def test_deterministic(self):
        sha1 = handoff.write(self.d)
        first = (self.d / handoff.MANIFEST_NAME).read_bytes()
        sha2 = handoff.write(self.d)
        second = (self.d / handoff.MANIFEST_NAME).read_bytes()
        self.assertEqual(sha1, sha2)
        self.assertEqual(first, second)

    def test_nested_members(self):
        (self.d / "provenance").mkdir()
        (self.d / "provenance" / "sources.txt").write_text("src", encoding="utf-8")
        sha = handoff.write(self.d)
        manifest = handoff.verify(self.d, sha)
        self.assertIn("provenance/sources.txt", {m["path"] for m in manifest["members"]})

    def test_tampered_member_bytes(self):
        handoff.write(self.d)
        (self.d / "redis-8.8.0-windows-amd64.zip").write_bytes(b"swapped bytes of a different length")
        with self.assertRaises(handoff.HandoffError):
            handoff.verify(self.d)

    def test_missing_declared_member(self):
        handoff.write(self.d)
        (self.d / "redis-8.8.0-windows-amd64.meta.json").unlink()
        with self.assertRaises(handoff.HandoffError):
            handoff.verify(self.d)

    def test_undeclared_extra_member(self):
        handoff.write(self.d)
        (self.d / "sneaked-in.bin").write_bytes(b"not in the manifest")
        with self.assertRaises(handoff.HandoffError):
            handoff.verify(self.d)


class TestCraftedManifest(unittest.TestCase):
    """Directly craft a manifest.json to exercise the field-level guards."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _populate(self.d)
        handoff.write(self.d)
        self.manifest = json.loads((self.d / handoff.MANIFEST_NAME).read_text())

    def _rewrite(self, manifest):
        (self.d / handoff.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

    def test_wrong_declared_size(self):
        self.manifest["members"][0]["size"] += 1
        self._rewrite(self.manifest)
        with self.assertRaises(handoff.HandoffError):
            handoff.verify(self.d)

    def test_wrong_declared_sha(self):
        self.manifest["members"][0]["sha256"] = "0" * 64
        self._rewrite(self.manifest)
        with self.assertRaises(handoff.HandoffError):
            handoff.verify(self.d)

    def test_manifest_lists_itself(self):
        self.manifest["members"].append(
            {"type": "file", "path": handoff.MANIFEST_NAME, "sha256": "a" * 64, "size": 1}
        )
        self._rewrite(self.manifest)
        with self.assertRaises(handoff.HandoffError):
            handoff.verify(self.d)

    def test_path_escape(self):
        for bad in ("../escape.bin", "/abs/escape.bin", "./dot.zip", "a//b.zip"):
            self.manifest["members"][0]["path"] = bad
            self._rewrite(self.manifest)
            with self.assertRaises(handoff.HandoffError):
                handoff.verify(self.d)

    def test_wrong_schema(self):
        self.manifest["schema"] = 999
        self._rewrite(self.manifest)
        with self.assertRaises(handoff.HandoffError):
            handoff.verify(self.d)

    def test_not_json(self):
        (self.d / handoff.MANIFEST_NAME).write_text("{ not json", encoding="utf-8")
        with self.assertRaises(handoff.HandoffError):
            handoff.verify(self.d)


class TestEdgeCases(unittest.TestCase):
    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(handoff.HandoffError):
                handoff.write(pathlib.Path(t))

    def test_missing_manifest(self):
        with tempfile.TemporaryDirectory() as t:
            d = pathlib.Path(t)
            _populate(d)
            with self.assertRaises(handoff.HandoffError):
                handoff.verify(d)

    def test_symlink_member_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            d = pathlib.Path(t)
            _populate(d)
            try:
                os.symlink(d / "redis-8.8.0-windows-amd64.zip", d / "link.zip")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not permitted on this host")
            with self.assertRaises(handoff.HandoffError):
                handoff.write(d)


class TestCLI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        _populate(self.d)

    def test_write_prints_sha_then_verify(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(handoff_cli.main(["write", str(self.d)]), 0)
        sha = out.getvalue().strip()
        self.assertRegex(sha, r"^[0-9a-f]{64}$")
        self.assertEqual(handoff_cli.main(["verify", str(self.d), "--expect", sha]), 0)

    def test_verify_wrong_expect_exits_nonzero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            handoff_cli.main(["write", str(self.d)])
        self.assertEqual(handoff_cli.main(["verify", str(self.d), "--expect", "c" * 64]), 1)

    def test_verify_corrupt_exits_nonzero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            handoff_cli.main(["write", str(self.d)])
        (self.d / "redis-8.8.0-windows-amd64.zip").write_bytes(b"tampered to a new length")
        self.assertEqual(handoff_cli.main(["verify", str(self.d)]), 1)


if __name__ == "__main__":
    unittest.main()
