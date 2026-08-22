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
from devxdk_manifest import handoff, releasepub, resolvers  # noqa: E402
from devxdk_manifest.tests.test_releasepub import FakeAPI  # noqa: E402  (reuse the fake API)

# Pins the static-provenance tests validate against, so they do not move when
# config/tracked-versions.toml is bumped.
FAKE_PINS = {
    "openssl": {"version": "3.5.7"},
    "pcre2": {"version": "10.47"},
    "zlib": {"version": "1.3.2"},
}
FAKE_PINS_VERSIONS = {k: v["version"] for k, v in FAKE_PINS.items()}


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


def _nginx_leg_dir(root, leg, entries):
    """Materialize a verified devxdk-nginx-unix leg carrying one meta per entry.

    entries is a list of (version, static_libs) where static_libs is the dict to
    put under provenance.static_libs, or None to omit the block entirely.
    """
    d = pathlib.Path(root) / leg
    d.mkdir(parents=True)
    for version, static_libs in entries:
        archive = f"nginx-{version}-linux-amd64.tar.gz"
        (d / archive).write_bytes(f"nginx-{version}-bytes".encode())
        sha = hashlib.sha256((d / archive).read_bytes()).hexdigest()
        provenance = {"recipe": "nginx-unix", "os": "linux"}
        if static_libs is not None:
            provenance["static_libs"] = static_libs
        meta = {
            "component": "nginx", "version": version, "platform": "linux/amd64",
            "line": version.rsplit(".", 1)[0], "ordering_kind": "built",
            "provider": "devxdk-nginx-unix", "epoch": 1, "revision": 1,
            "source_version": version, "archive": archive, "sha256": sha,
            "size_bytes": (d / archive).stat().st_size,
            "provenance": provenance,
        }
        (d / f"{archive}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return d, handoff.write(d)


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
        self._orig_pins = publish_legs._static_pins
        publish_legs.download_artifact = self._fake_download
        publish_legs._committed_releases = lambda _c: []
        publish_legs._static_pins = lambda: FAKE_PINS
        self.addCleanup(self._restore)

    def _restore(self):
        publish_legs.download_artifact = self._orig_dl
        publish_legs._committed_releases = self._orig_rel
        publish_legs._static_pins = self._orig_pins

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


class TestStaticPinProvenance(unittest.TestCase):
    """The publish-time static-pin validator (fail-closed, two passes)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.staged = {}
        self.reconciled = []
        self._orig_dl = publish_legs.download_artifact
        self._orig_rel = publish_legs._committed_releases
        self._orig_pins = publish_legs._static_pins
        self._orig_reconcile = releasepub.reconcile_release
        publish_legs.download_artifact = self._fake_download
        publish_legs._committed_releases = lambda _c: []
        publish_legs._static_pins = lambda: FAKE_PINS
        releasepub.reconcile_release = self._count_reconcile
        self.addCleanup(self._restore)

    def _restore(self):
        publish_legs.download_artifact = self._orig_dl
        publish_legs._committed_releases = self._orig_rel
        publish_legs._static_pins = self._orig_pins
        releasepub.reconcile_release = self._orig_reconcile

    def _count_reconcile(self, api, tag, **kw):
        self.reconciled.append(tag)
        return self._orig_reconcile(api, tag, **kw)

    def _fake_download(self, artifact_id, dest):
        import shutil
        shutil.copytree(self.staged[artifact_id], dest, dirs_exist_ok=True)

    def _needs(self, entries, artifact_id="n1"):
        d, msha = _nginx_leg_dir(self.root / "src", f"{artifact_id}-nginx", entries)
        self.staged[artifact_id] = d
        return json.dumps({"leg-nginx-linux-amd64": {
            "result": "success",
            "outputs": {"artifact_id": artifact_id, "manifest_sha256": msha}}})

    def test_matching_meta_reconciles(self):
        needs = self._needs([("1.30.4", dict(FAKE_PINS_VERSIONS))])
        metas, errors = publish_legs.publish(needs, self.root / "work", api=FakeAPI())
        self.assertEqual(errors, [])
        self.assertEqual([m["version"] for m in metas], ["1.30.4"])
        self.assertEqual(self.reconciled, ["nginx-1.30.4"])

    def test_mismatch_collects_error_and_reconciles_nothing(self):
        stale = dict(FAKE_PINS_VERSIONS, zlib="1.3.1")
        needs = self._needs([("1.30.4", stale)])
        metas, errors = publish_legs.publish(needs, self.root / "work", api=FakeAPI())
        self.assertEqual(metas, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("zlib", errors[0])
        self.assertIn("1.3.1", errors[0])
        self.assertIn("1.3.2", errors[0])
        self.assertEqual(self.reconciled, [])

    def test_second_meta_mismatch_reconciles_zero(self):
        # The ordering property: a leg can carry several metas, and pass 1 must
        # reject the whole leg BEFORE pass 2 uploads meta #1's assets. A check
        # bolted into the old single pass would already have published 1.30.4.
        needs = self._needs([
            ("1.30.4", dict(FAKE_PINS_VERSIONS)),
            ("1.31.0", dict(FAKE_PINS_VERSIONS, pcre2="10.46")),
        ])
        metas, errors = publish_legs.publish(needs, self.root / "work", api=FakeAPI())
        self.assertEqual(metas, [])
        self.assertEqual(self.reconciled, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("pcre2", errors[0])

    def test_missing_static_libs_is_rejected(self):
        # Fail CLOSED: a recipe edit that drops the block must be an error, not a
        # silent pass. Assert the REJECTION, never that it goes through untouched.
        needs = self._needs([("1.30.4", None)])
        metas, errors = publish_legs.publish(needs, self.root / "work", api=FakeAPI())
        self.assertEqual(metas, [])
        self.assertEqual(self.reconciled, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("must declare provenance.static_libs", errors[0])

    def test_extra_key_is_rejected(self):
        needs = self._needs([("1.30.4", dict(FAKE_PINS_VERSIONS, brotli="1.1.0"))])
        metas, errors = publish_legs.publish(needs, self.root / "work", api=FakeAPI())
        self.assertEqual(metas, [])
        self.assertEqual(self.reconciled, [])
        self.assertIn("want exactly", errors[0])


class TestStaticPinMapHygiene(unittest.TestCase):
    def test_map_keys_are_enabled_providers(self):
        # A row naming a provider that no longer exists must fail here rather
        # than sit dead in the map.
        self.assertTrue(
            set(publish_legs.STATIC_PIN_PROVIDERS) <= resolvers.ENABLED_PROVIDERS,
            set(publish_legs.STATIC_PIN_PROVIDERS) - resolvers.ENABLED_PROVIDERS)

    def test_unmapped_provider_with_no_static_libs_passes(self):
        meta = {"provider": "devxdk-redis-msys2", "provenance": {"recipe": "x"}}
        self.assertEqual(publish_legs.validate_static_pins(meta, FAKE_PINS), [])

    def test_unmapped_provider_declaring_static_libs_is_validated(self):
        # The presence rule: a future recipe is covered from its first run, even
        # before its row is added to the map.
        meta = {"provider": "devxdk-redis-unix",
                "provenance": {"static_libs": {"openssl": "3.5.6"}}}
        errs = publish_legs.validate_static_pins(meta, FAKE_PINS)
        self.assertEqual(len(errs), 1)
        self.assertIn("openssl", errs[0])

    def test_static_lib_with_no_pin_row_is_an_error(self):
        meta = {"provider": "devxdk-redis-unix",
                "provenance": {"static_libs": {"brotli": "1.1.0"}}}
        errs = publish_legs.validate_static_pins(meta, FAKE_PINS)
        self.assertEqual(len(errs), 1)
        self.assertIn("no [pins.brotli] version", errs[0])


if __name__ == "__main__":
    unittest.main()
