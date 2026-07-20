"""Tests for the revocation transitions (scraped + managed) and apply_revocations."""

import json
import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

import apply_revocations as ar  # noqa: E402
from devxdk_manifest import merge, revoke  # noqa: E402


def _tuple_fields(url="https://x/8.8.0", sha="a" * 64, size=100, channel="stable", released="2026-01-01"):
    return {"url": url, "sha256": sha, "size_bytes": size, "channel": channel, "released_at": released}


def _scrape_state(version="8.8.0"):
    st = merge.ScrapeState()
    f = _tuple_fields(url=f"https://x/{version}")
    st.put("node", "24", "windows/amd64", merge.ScrapeRecord(
        provider="nodejs", epoch=1, floor_version=version,
        tuples=[merge.Tuple(version=version, **f)]))
    return st


def _ledger():
    st = merge.LedgerState()
    st.put("redis", "8.8.0", "windows/amd64", merge.LedgerRecord(
        kind="built", line="8", provider="devxdk-redis-msys2", epoch=1, key="1",
        source_version="8.8.0", **_tuple_fields()))
    return st


class TestScraped(unittest.TestCase):
    def _rec(self, op, version="8.8.0", **kw):
        d = dict(scope="scraped", component="node", line="24", platform="windows/amd64",
                 version=version, op=op, expected=_tuple_fields(url=f"https://x/{version}"),
                 reason="test")
        d.update(kw)
        return revoke.RevocationRecord.from_dict(d)

    def test_delete_moves_to_revoked(self):
        st = _scrape_state()
        revoke.apply_scraped(st, self._rec("delete"))
        rc = st.get("node", "24", "windows/amd64")
        self.assertEqual(rc.tuples, [])
        self.assertEqual([t.version for t in rc.revoked], ["8.8.0"])
        # The scrape guard now suppresses re-admission of the revoked version.
        with self.assertRaises(merge.GuardError):
            merge.reconcile_key(rc, [merge.Tuple(version="8.8.0", **_tuple_fields(url="https://x/8.8.0"))], retain=3)

    def test_replace_swaps_bytes(self):
        st = _scrape_state()
        rep = _tuple_fields(url="https://x/8.8.0", sha="b" * 64)
        revoke.apply_scraped(st, self._rec("replace", replacement=rep))
        rc = st.get("node", "24", "windows/amd64")
        self.assertEqual(rc.tuples[0].sha256, "b" * 64)

    def test_readmit_restores(self):
        st = _scrape_state()
        revoke.apply_scraped(st, self._rec("delete"))
        rep = _tuple_fields(url="https://x/8.8.0", sha="c" * 64)
        revoke.apply_scraped(st, self._rec("readmit", replacement=rep))
        rc = st.get("node", "24", "windows/amd64")
        self.assertEqual(rc.revoked, [])
        self.assertEqual(rc.tuples[0].sha256, "c" * 64)

    def test_mismatch_expected_errors(self):
        st = _scrape_state()
        bad = self._rec("delete")
        bad.expected["sha256"] = "f" * 64
        with self.assertRaises(revoke.RevocationError):
            revoke.apply_scraped(st, bad)


class TestManaged(unittest.TestCase):
    def _rec(self, op, **kw):
        d = dict(scope="managed", component="redis", platform="windows/amd64", version="8.8.0",
                 op=op, expected=_tuple_fields(), reason="test",
                 expected_ordering={"kind": "built", "provider": "devxdk-redis-msys2",
                                    "epoch": 1, "key": "1", "source_version": "8.8.0"})
        d.update(kw)
        return revoke.RevocationRecord.from_dict(d)

    def test_delete_marks_revoked(self):
        led = _ledger()
        revoke.apply_managed(led, self._rec("delete"))
        self.assertTrue(led.get("redis", "8.8.0", "windows/amd64").revoked)

    def test_readmit_restores(self):
        led = _ledger()
        revoke.apply_managed(led, self._rec("delete"))
        rep = _tuple_fields(sha="d" * 64)
        revoke.apply_managed(led, self._rec("readmit", replacement=rep))
        entry = led.get("redis", "8.8.0", "windows/amd64")
        self.assertFalse(entry.revoked)
        self.assertEqual(entry.sha256, "d" * 64)

    def test_ordering_mismatch_errors(self):
        led = _ledger()
        bad = self._rec("delete")
        bad.expected_ordering["key"] = "9"
        with self.assertRaises(revoke.RevocationError):
            revoke.apply_managed(led, bad)

    def test_managed_replace_rejected(self):
        with self.assertRaises(revoke.RevocationError):
            revoke.RevocationRecord.from_dict({"scope": "managed", "component": "redis",
                "platform": "windows/amd64", "version": "8.8.0", "op": "replace",
                "expected": _tuple_fields(), "reason": "x",
                "expected_ordering": {"kind": "built"}})


class TestApplyIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        (self.tmp / "state").mkdir()
        (self.tmp / "revocations").mkdir()
        _scrape_state().save(self.tmp / "state" / "scrape-versions.json")
        _ledger().save(self.tmp / "state" / "asset-revisions.json")
        # redis.json holds the managed release the ledger entry backs.
        (self.tmp / "redis.json").write_text(json.dumps({
            "name": "redis", "display_name": "Redis", "kind": "service",
            "releases": [{"version": "8.8.0", "channel": "stable", "released_at": "2026-01-01",
                          "platforms": {"windows/amd64": {"url": _tuple_fields()["url"],
                                                          "sha256": "a" * 64, "size_bytes": 100}}}],
        }, indent=2) + "\n", encoding="utf-8")

    def test_managed_delete_removes_platform_and_keeps_ledger(self):
        rec = {"scope": "managed", "component": "redis", "platform": "windows/amd64",
               "version": "8.8.0", "op": "delete", "expected": _tuple_fields(),
               "expected_ordering": {"kind": "built", "provider": "devxdk-redis-msys2",
                                     "epoch": 1, "key": "1", "source_version": "8.8.0"},
               "reason": "bad build"}
        (self.tmp / "revocations" / "r1.json").write_text(json.dumps(rec) + "\n", encoding="utf-8")
        result = ar.apply(self.tmp)
        self.assertEqual(len(result["applied"]), 1)
        redis = json.loads((self.tmp / "redis.json").read_text(encoding="utf-8"))
        self.assertEqual(redis["releases"], [])  # platform removed -> release gone
        ledger = merge.LedgerState.load(self.tmp / "state" / "asset-revisions.json")
        self.assertTrue(ledger.get("redis", "8.8.0", "windows/amd64").revoked)  # sticky
        self.assertEqual(list((self.tmp / "revocations").glob("*.json")), [])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
