"""Tests for line retirement and reactivation."""

import pathlib
import sys
import tempfile
import textwrap
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

import apply_lifecycle as al  # noqa: E402
from devxdk_manifest import config, lifecycle, merge, schema  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _asset(url, sha="a" * 64, size=100):
    return {"url": url, "sha256": sha, "size_bytes": size}


def _node_state():
    st = merge.ScrapeState()
    for p, suffix in (("windows/amd64", "win-x64.zip"), ("linux/amd64", "linux-x64.tar.gz")):
        url = f"https://nodejs.org/dist/v24.17.0/node-v24.17.0-{suffix}"
        st.put("node", "24", p, merge.ScrapeRecord(provider="nodejs", epoch=1, floor_version="24.17.0",
                tuples=[merge.Tuple("24.17.0", url, "a" * 64, 100, "lts", "2026-06-17")]))
    return st


def _node_manifest():
    return {"name": "node", "display_name": "Node.js", "kind": "runtime", "releases": [
        {"version": "24.17.0", "channel": "lts", "released_at": "2026-06-17", "platforms": {
            "windows/amd64": _asset("https://nodejs.org/dist/v24.17.0/node-v24.17.0-win-x64.zip"),
            "linux/amd64": _asset("https://nodejs.org/dist/v24.17.0/node-v24.17.0-linux-x64.tar.gz")}}]}


def _redis_ledger(epoch=1):
    st = merge.LedgerState()
    st.put("redis", "8.8.0", "windows/amd64", merge.LedgerRecord(
        kind="built", line="8", provider="devxdk-redis-msys2", epoch=epoch, key="1",
        source_version="8.8.0", url="https://github.com/devxdk/devxdk/releases/download/redis-8.8.0/x.zip",
        sha256="a" * 64, size_bytes=100, channel="stable", released_at="2026-01-01"))
    return st


def _redis_cfg(epoch=1):
    body = textwrap.dedent(f"""\
        schema = 1
        [components.redis]
        kind = "service"
        [components.redis.lines."8"]
        channel = "stable"
        retain_per_line = 3
        platforms = {{ "windows/amd64" = {{ type = "build", provider = "devxdk-redis-msys2", epoch = {epoch} }} }}
    """)
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
    f.write(body)
    f.close()
    return config.load(f.name)


class TestScraped(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()

    def test_retire_then_recompose_removes_releases(self):
        st = _node_state()
        led = merge.LedgerState()
        self.assertTrue(lifecycle.retire_line(st, led, "node", "24", _node_manifest()))
        for _c, _l, _p, rec in st.iter_records():
            self.assertEqual(rec.status, "tombstone")
            self.assertIn("24.17.0", rec.release_snapshots)
        manifest = merge.recompose("node", "Node.js", "runtime", self.cfg, st, led)
        self.assertEqual(manifest["releases"], [])

    def test_reactivate_restores(self):
        st = _node_state()
        led = merge.LedgerState()
        lifecycle.retire_line(st, led, "node", "24", _node_manifest())
        self.assertTrue(lifecycle.reactivate_line(self.cfg, st, led, "node", "24"))
        manifest = merge.recompose("node", "Node.js", "runtime", self.cfg, st, led)
        self.assertEqual(manifest["releases"][0]["version"], "24.17.0")
        self.assertEqual(set(manifest["releases"][0]["platforms"]), {"windows/amd64", "linux/amd64"})


class TestManaged(unittest.TestCase):
    def test_retire_managed(self):
        led = _redis_ledger()
        m = {"releases": [{"version": "8.8.0"}]}
        self.assertTrue(lifecycle.retire_line(merge.ScrapeState(), led, "redis", "8", m))
        self.assertEqual(led.get("redis", "8.8.0", "windows/amd64").status, "tombstone")

    def test_reactivate_needs_epoch_bump(self):
        led = _redis_ledger(epoch=1)
        lifecycle.retire_line(merge.ScrapeState(), led, "redis", "8", {"releases": [{"version": "8.8.0"}]})
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.reactivate_line(_redis_cfg(epoch=1), merge.ScrapeState(), led, "redis", "8")

    def test_reactivate_with_epoch_bump_restamps(self):
        led = _redis_ledger(epoch=1)
        lifecycle.retire_line(merge.ScrapeState(), led, "redis", "8", {"releases": [{"version": "8.8.0"}]})
        self.assertTrue(lifecycle.reactivate_line(_redis_cfg(epoch=2), merge.ScrapeState(), led, "redis", "8"))
        entry = led.get("redis", "8.8.0", "windows/amd64")
        self.assertEqual(entry.status, "active")
        self.assertEqual(entry.epoch, 2)  # re-stamped (provider + kind unchanged)


class TestApplyNoop(unittest.TestCase):
    def test_real_config_no_transitions(self):
        # The committed config retires nothing, so apply_lifecycle is a clean no-op
        # and does not perturb the committed state/manifests.
        import shutil
        tmp = pathlib.Path(tempfile.mkdtemp())
        try:
            (tmp / "state").mkdir()
            shutil.copy(REPO_ROOT / "state" / "scrape-versions.json", tmp / "state" / "scrape-versions.json")
            shutil.copy(REPO_ROOT / "state" / "asset-revisions.json", tmp / "state" / "asset-revisions.json")
            self.assertEqual(al.apply(tmp), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
