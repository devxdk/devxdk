"""finalize_builds: the two-pass write, and the reshaped push loop.

Two properties, and each was a real hole:

  * write_pending used to interleave the parse and the write, so a malformed
    SECOND .meta.json left the FIRST pending record already on disk.
  * commit_and_push fetched and reset only AFTER a rejection, so on the FIRST
    attempt FETCH_HEAD was absent or left over from something unrelated and was
    not a comparison base at all — which is why the pre-push gate needed the
    loop reshaped before it could mean anything.
"""

import contextlib
import io
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

import finalize_builds  # noqa: E402
from devxdk_manifest import handoff, strictjson  # noqa: E402

GOOD_META = {
    "component": "redis", "version": "8.8.0", "platform": "windows/amd64",
    "line": "8", "ordering_kind": "built", "provider": "devxdk-redis-msys2",
    "epoch": 1, "revision": 1, "source_version": "8.8.0",
    "archive": "redis-8.8.0-windows-amd64.zip",
    "sha256": "a" * 64, "size_bytes": 100,
}


class MetasDir(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def meta(self, name, doc_or_bytes):
        path = self.dir / f"{name}.meta.json"
        if isinstance(doc_or_bytes, (bytes, bytearray)):
            path.write_bytes(doc_or_bytes)
        else:
            path.write_text(json.dumps(doc_or_bytes, indent=2) + "\n", encoding="utf-8")
        return path


class TestWritePendingIsAtomic(MetasDir):
    def test_a_malformed_second_meta_writes_nothing_at_all(self):
        # Sorted order puts "a-..." first, so the FIRST file is the good one and
        # the SECOND is the ambiguous one — the shape that used to leave one
        # record behind.
        self.meta("a-redis", GOOD_META)
        self.meta("b-nginx", b'{"component":"nginx","Component":"redis","version":"1"}\n')
        with mock.patch.object(finalize_builds.add_built_release, "main", return_value=0) as adder:
            with self.assertRaises(strictjson.StrictJSONError):
                finalize_builds.write_pending(self.dir)
        adder.assert_not_called()

    def test_a_meta_missing_a_field_is_caught_in_the_preflight(self):
        self.meta("a-redis", GOOD_META)
        incomplete = dict(GOOD_META)
        del incomplete["revision"]
        self.meta("b-nginx", incomplete)
        with mock.patch.object(finalize_builds.add_built_release, "main", return_value=0) as adder:
            with self.assertRaises(SystemExit) as cm:
                finalize_builds.write_pending(self.dir)
        self.assertIn("revision", str(cm.exception))
        adder.assert_not_called()

    def test_the_happy_path_still_writes_every_record(self):
        self.meta("a-redis", GOOD_META)
        self.meta("b-redis", dict(GOOD_META, platform="linux/amd64"))
        with mock.patch.object(finalize_builds.add_built_release, "main", return_value=0) as adder:
            written = finalize_builds.write_pending(self.dir)
        self.assertEqual(adder.call_count, 2)
        self.assertEqual(written, ["redis", "redis"])


class TestHandoffDoesNotImplyWellFormed(MetasDir):
    def test_a_verified_bundle_can_still_carry_an_ambiguous_meta(self):
        # handoff.verify authenticates the BYTES; it does not parse the
        # individual .meta.json files. Authenticity never implies
        # well-formedness — which is the whole reason the strict parse belongs
        # on this path too.
        (self.dir / "redis.zip").write_bytes(b"PK\x03\x04")
        path = self.meta("redis", b'{"component":"redis","Component":"nginx"}\n')
        sha = handoff.write(self.dir)
        self.assertIsNotNone(handoff.verify(self.dir, sha))  # passes
        with self.assertRaises(strictjson.StrictJSONError):
            strictjson.load(path)


class TestCommitAndPushLoop(unittest.TestCase):
    def _run(self, push_results, gate_ok=True):
        """Drive commit_and_push with a scripted sequence of push exit codes."""
        calls = []

        def fake_git(*args, check=True):
            calls.append(args)
            if args[0] == "diff":
                return mock.Mock(returncode=1)  # there IS something staged
            if args[0] == "push":
                rc = push_results.pop(0)
                return mock.Mock(returncode=rc, stderr="rejected")
            return mock.Mock(returncode=0, stderr="")

        with mock.patch.object(finalize_builds, "_git", side_effect=fake_git), \
                mock.patch.object(finalize_builds, "write_pending", return_value=["redis"]), \
                mock.patch.object(finalize_builds, "_check_revision_history", return_value=gate_ok), \
                contextlib.redirect_stderr(io.StringIO()):
            ok = finalize_builds.commit_and_push("metas", attempts=3)
        return ok, [a[0] for a in calls]

    def test_first_attempt_fetches_and_resets_before_anything_else(self):
        # The reshape: FETCH_HEAD must be a real comparison base on attempt 1,
        # which it was not when the fetch happened only after a rejection.
        ok, verbs = self._run([0])
        self.assertTrue(ok)
        self.assertEqual(verbs[:2], ["fetch", "reset"])
        self.assertEqual(verbs, ["fetch", "reset", "add", "diff", "commit", "push"])

    def test_the_retry_path_fetches_and_resets_again_at_the_top(self):
        ok, verbs = self._run([1, 0])
        self.assertTrue(ok)
        self.assertEqual(verbs, ["fetch", "reset", "add", "diff", "commit", "push",
                                 "fetch", "reset", "add", "diff", "commit", "push"])
        # And no redundant post-rejection pair: exactly one fetch per attempt.
        self.assertEqual(verbs.count("fetch"), 2)
        self.assertEqual(verbs.count("reset"), 2)

    def test_a_failing_gate_stops_before_the_push(self):
        ok, verbs = self._run([0], gate_ok=False)
        self.assertFalse(ok)
        self.assertNotIn("push", verbs)

    def test_the_gate_runs_on_every_attempt(self):
        with mock.patch.object(finalize_builds, "_git") as git, \
                mock.patch.object(finalize_builds, "write_pending", return_value=["redis"]), \
                mock.patch.object(finalize_builds, "_check_revision_history",
                                  return_value=True) as gate, \
                contextlib.redirect_stderr(io.StringIO()):
            git.side_effect = lambda *a, **k: mock.Mock(
                returncode=1 if a[0] in ("diff", "push") else 0, stderr="")
            finalize_builds.commit_and_push("metas", attempts=3)
        self.assertEqual(gate.call_count, 3)


if __name__ == "__main__":
    unittest.main()
