"""Tests for the publish/finalize orchestration (needs-parsing, publish, pending)."""

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))  # sibling test modules

import finalize_builds  # noqa: E402
import publish_legs  # noqa: E402
from devxdk_manifest import handoff  # noqa: E402
from devxdk_manifest.tests.test_releasepub import FakeAPI  # noqa: E402  (reuse the fake API)


def _leg_dir(root, leg, component, version):
    """Materialize a verified leg artifact dir (archive + meta + manifest.json)."""
    d = pathlib.Path(root) / leg
    d.mkdir(parents=True)
    archive = f"{component}-{version}-windows-amd64.zip"
    (d / archive).write_bytes(f"{component}-{version}-bytes".encode())
    sha = hashlib.sha256((d / archive).read_bytes()).hexdigest()
    meta = {
        "component": component, "version": version, "platform": "windows/amd64",
        "line": version.rsplit(".", 1)[0] if component == "php" else version.split(".")[0],
        "ordering_kind": "built", "provider": f"devxdk-{component}-msys2" if component != "php" else "devxdk-php-windows",
        "epoch": 1, "revision": 1, "source_version": version,
        "archive": archive, "sha256": sha, "size_bytes": (d / archive).stat().st_size,
    }
    (d / f"{archive}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    manifest_sha = handoff.write(d)
    return d, manifest_sha, meta


def _adopt_leg_dir(root, leg, component, version, url):
    """Materialize a verified ADOPT leg artifact dir: a meta only, no archive
    (adopt references the upstream URL — nothing is rehosted)."""
    d = pathlib.Path(root) / leg
    d.mkdir(parents=True)
    meta = {
        "component": component, "version": version, "platform": "windows/amd64",
        "line": version.rsplit(".", 1)[0], "ordering_kind": "adopted",
        "provider": "astral", "epoch": 1, "revision": 1, "source_version": version,
        "url": url, "sha256": "c" * 64, "size_bytes": 123,
    }
    (d / f"{component}-{version}-windows-amd64.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d, handoff.write(d), meta


class TestSuccessLegs(unittest.TestCase):
    def test_selects_success_with_outputs(self):
        needs = json.dumps({
            "plan": {"result": "success", "outputs": {}},
            "leg-redis-windows-amd64": {"result": "success",
                "outputs": {"artifact_id": "111", "manifest_sha256": "a" * 64}},
            "leg-php-windows-amd64": {"result": "failure", "outputs": {}},
            "leg-valkey-windows-amd64": {"result": "skipped", "outputs": {}},
            "leg-nginx-linux-amd64": {"result": "success", "outputs": {}},  # no ids -> excluded
        })
        got = publish_legs.success_legs(needs)
        self.assertEqual(set(got), {"redis-windows-amd64"})
        self.assertEqual(got["redis-windows-amd64"]["artifact_id"], "111")


class TestPublish(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.staged = {}   # artifact_id -> leg dir (the "downloaded" artifact)
        self._orig_dl = publish_legs.download_artifact
        self._orig_rel = publish_legs._committed_releases
        publish_legs.download_artifact = self._fake_download
        publish_legs._committed_releases = lambda _c: []
        self.addCleanup(self._restore)

    def _restore(self):
        publish_legs.download_artifact = self._orig_dl
        publish_legs._committed_releases = self._orig_rel

    def _fake_download(self, artifact_id, dest):
        import shutil
        shutil.copytree(self.staged[artifact_id], dest, dirs_exist_ok=True)

    def _stage(self, leg, component, version, artifact_id):
        src = self.root / "src"
        d, msha, meta = _leg_dir(src, f"{artifact_id}-{leg}", component, version)
        self.staged[artifact_id] = d
        return {"result": "success", "outputs": {"artifact_id": artifact_id, "manifest_sha256": msha}}

    def test_reconciles_success_legs_and_returns_metas(self):
        needs = {
            "leg-redis-windows-amd64": self._stage("redis-windows-amd64", "redis", "8.8.0", "a1"),
            "leg-valkey-windows-amd64": self._stage("valkey-windows-amd64", "valkey", "9.1.0", "a2"),
        }
        api = FakeAPI()
        metas, errors = publish_legs.publish(json.dumps(needs), self.root / "work", api=api)
        self.assertEqual(errors, [])
        self.assertEqual({m["component"] for m in metas}, {"redis", "valkey"})
        # Both releases created as drafts and undrafted.
        self.assertFalse(api.releases["redis-8.8.0"]["draft"])
        self.assertFalse(api.releases["valkey-9.1.0"]["draft"])

    def test_referenced_immutable_mismatch_is_collected_not_raised(self):
        needs = {"leg-redis-windows-amd64": self._stage("redis-windows-amd64", "redis", "8.8.0", "a1")}
        # A published release already carries a DIFFERENT-bytes referenced asset.
        api = FakeAPI(releases={"redis-8.8.0": {"id": 1, "draft": False, "assets": [
            {"id": 2, "name": "redis-8.8.0-windows-amd64.zip", "size": 3,
             "digest": "sha256:" + "e" * 64, "_bytes": b"OLD"}]}})
        publish_legs._committed_releases = lambda c: [{"platforms": {"windows/amd64": {
            "url": "https://github.com/devxdk/devxdk/releases/download/redis-8.8.0/redis-8.8.0-windows-amd64.zip"}}}] if c == "redis" else []
        metas, errors = publish_legs.publish(json.dumps(needs), self.root / "work", api=api)
        self.assertEqual(metas, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("immutable", errors[0].lower() + " ")  # message mentions immutability

    def test_dry_run_verifies_without_mutation(self):
        needs = {"leg-redis-windows-amd64": self._stage("redis-windows-amd64", "redis", "8.8.0", "a1")}
        api = FakeAPI()
        metas, errors = publish_legs.publish(json.dumps(needs), self.root / "work", api=api, dry=True)
        self.assertEqual(errors, [])
        self.assertEqual(len(metas), 1)
        self.assertEqual(api.releases, {})  # nothing mutated

    def test_adopt_leg_passes_through_without_release(self):
        # An adopt leg re-hosts nothing: its meta is returned for finalize, but no
        # Release is created or asset uploaded.
        url = "https://github.com/astral-sh/python-build-standalone/releases/download/20260718/x.tar.gz"
        d, msha, _ = _adopt_leg_dir(self.root / "src", "a3-python-windows-amd64", "python", "3.14.6", url)
        self.staged["a3"] = d
        needs = {"leg-python-windows-amd64": {"result": "success",
                 "outputs": {"artifact_id": "a3", "manifest_sha256": msha}}}
        api = FakeAPI()
        metas, errors = publish_legs.publish(json.dumps(needs), self.root / "work", api=api)
        self.assertEqual(errors, [])
        self.assertEqual([m["component"] for m in metas], ["python"])
        self.assertEqual(metas[0]["url"], url)
        self.assertEqual(api.releases, {})  # adopt created no Release


class TestWritePending(unittest.TestCase):
    def test_writes_records_with_release_download_urls(self):
        with tempfile.TemporaryDirectory() as t:
            metas = pathlib.Path(t) / "metas"
            metas.mkdir()
            meta = {
                "component": "redis", "version": "8.8.0", "platform": "windows/amd64",
                "line": "8", "ordering_kind": "built", "provider": "devxdk-redis-msys2",
                "epoch": 1, "revision": 1, "source_version": "8.8.0",
                "archive": "redis-8.8.0-windows-amd64.zip", "sha256": "a" * 64, "size_bytes": 100,
            }
            (metas / "000-redis-8.8.0.meta.json").write_text(json.dumps(meta), encoding="utf-8")
            # add_built_release writes into the real repo's pending/; redirect it.
            import add_built_release
            orig = add_built_release.PENDING_DIR
            add_built_release.PENDING_DIR = pathlib.Path(t) / "pending"
            try:
                written = finalize_builds.write_pending(metas)
            finally:
                add_built_release.PENDING_DIR = orig
            self.assertEqual(written, ["redis"])
            rec = json.loads((pathlib.Path(t) / "pending" / "redis-8.8.0-windows-amd64.json").read_text())
            self.assertEqual(rec["url"],
                "https://github.com/devxdk/devxdk/releases/download/redis-8.8.0/redis-8.8.0-windows-amd64.zip")
            self.assertEqual(rec["sha256"], "a" * 64)

    def test_adopt_pending_uses_upstream_url(self):
        upstream = "https://github.com/astral-sh/python-build-standalone/releases/download/20260718/x.tar.gz"
        with tempfile.TemporaryDirectory() as t:
            metas = pathlib.Path(t) / "metas"
            metas.mkdir()
            meta = {
                "component": "python", "version": "3.14.6", "platform": "windows/amd64",
                "line": "3.14", "ordering_kind": "adopted", "provider": "astral",
                "epoch": 1, "revision": 1, "source_version": "3.14.6",
                "url": upstream, "sha256": "c" * 64, "size_bytes": 123,
            }
            (metas / "000-python-3.14.6.meta.json").write_text(json.dumps(meta), encoding="utf-8")
            import add_built_release
            orig = add_built_release.PENDING_DIR
            add_built_release.PENDING_DIR = pathlib.Path(t) / "pending"
            try:
                finalize_builds.write_pending(metas)
            finally:
                add_built_release.PENDING_DIR = orig
            rec = json.loads((pathlib.Path(t) / "pending" / "python-3.14.6-windows-amd64.json").read_text())
            self.assertEqual(rec["url"], upstream)  # upstream, NOT a devxdk Release URL
            self.assertEqual(rec["ordering_kind"], "adopted")


if __name__ == "__main__":
    unittest.main()
