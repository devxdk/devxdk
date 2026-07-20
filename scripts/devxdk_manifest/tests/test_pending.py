"""Tests for the ordering-ledger transition rules and the apply_pending flow."""

import json
import pathlib
import sys
import tempfile
import textwrap
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

import apply_pending as ap  # noqa: E402
from devxdk_manifest import config, merge, pending  # noqa: E402

REDIS_URL = "https://github.com/devxdk/devxdk/releases/download/redis-8.8.0/redis-8.8.0-windows-amd64.zip"


def _prec(**kw):
    d = dict(component="redis", version="8.8.0", platform="windows/amd64", line="8",
             ordering_kind="built", provider="devxdk-redis-msys2", epoch=1, revision=1,
             source_version="8.8.0", url=REDIS_URL, sha256="a" * 64, size_bytes=100)
    d.update(kw)
    return pending.PendingRecord.from_dict(d)


def _ledger(rec: pending.PendingRecord, **over):
    d = dict(kind=rec.ordering_kind, line=rec.line, provider=rec.provider, epoch=rec.epoch,
             key=rec.key, source_version=rec.source_version, url=rec.url, sha256=rec.sha256,
             size_bytes=rec.size_bytes, channel="stable", released_at="2026-01-01")
    d.update(over)
    st = merge.LedgerState()
    st.put(rec.component, rec.version, rec.platform, merge.LedgerRecord(**d))
    return st


def _temp_cfg(epoch=1, provider="devxdk-redis-msys2", ptype="build"):
    body = textwrap.dedent(f"""\
        schema = 1
        [components.redis]
        kind = "service"
        [components.redis.lines."8"]
        channel = "stable"
        retain_per_line = 3
        platforms = {{ "windows/amd64" = {{ type = "{ptype}", provider = "{provider}", epoch = {epoch} }} }}
    """)
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
    f.write(body)
    f.close()
    return config.load(f.name)


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()

    def test_first_publish(self):
        self.assertEqual(pending.classify(self.cfg, merge.LedgerState(), _prec())[0], pending.APPLY)

    def test_revision_bump_applies(self):
        led = _ledger(_prec(revision=1))
        self.assertEqual(pending.classify(self.cfg, led, _prec(revision=2, sha256="b" * 64))[0], pending.APPLY)

    def test_stale_revision_discarded(self):
        led = _ledger(_prec(revision=2))
        out, reason = pending.classify(self.cfg, led, _prec(revision=1))
        self.assertEqual(out, pending.DISCARD)
        self.assertIn("stale", reason)

    def test_idempotent_discarded(self):
        led = _ledger(_prec(revision=1))
        out, reason = pending.classify(self.cfg, led, _prec(revision=1))
        self.assertEqual(out, pending.DISCARD)
        self.assertIn("idempotent", reason)

    def test_equal_key_conflict_errors(self):
        led = _ledger(_prec(revision=1))
        with self.assertRaises(pending.PendingError):
            pending.classify(self.cfg, led, _prec(revision=1, sha256="b" * 64))

    def test_untracked_line_discarded(self):
        out, reason = pending.classify(self.cfg, merge.LedgerState(), _prec(line="99", version="99.0.0"))
        self.assertEqual(out, pending.DISCARD)
        self.assertIn("not active", reason)

    def test_provider_mismatch_errors(self):
        with self.assertRaises(pending.PendingError):
            pending.classify(self.cfg, merge.LedgerState(), _prec(provider="devxdk-evil"))

    def test_kind_mismatch_errors(self):
        with self.assertRaises(pending.PendingError):
            pending.classify(self.cfg, merge.LedgerState(), _prec(ordering_kind="adopted"))

    def test_revoked_blocks_equal_or_higher(self):
        led = _ledger(_prec(revision=1), revoked=True)
        # lower -> discarded; higher -> hard error naming readmit
        out, _ = pending.classify(self.cfg, led, _prec(revision=1))
        self.assertEqual(out, pending.DISCARD)
        with self.assertRaises(pending.PendingError):
            pending.classify(self.cfg, led, _prec(revision=2, sha256="b" * 64))


class TestEpoch(unittest.TestCase):
    def test_future_epoch_errors(self):
        cfg = _temp_cfg(epoch=1)
        with self.assertRaises(pending.PendingError):
            pending.classify(cfg, merge.LedgerState(), _prec(epoch=2))

    def test_pre_migration_discarded(self):
        cfg = _temp_cfg(epoch=2)
        out, reason = pending.classify(cfg, merge.LedgerState(), _prec(epoch=1))
        self.assertEqual(out, pending.DISCARD)
        self.assertIn("pre-migration", reason)

    def test_wholesale_supersession_across_epoch(self):
        cfg = _temp_cfg(epoch=2, provider="devxdk-redis-msys2")
        led = _ledger(_prec(epoch=1, revision=5))  # old-epoch entry, high key
        # A current-epoch r1 supersedes wholesale (no cross-epoch key compare).
        self.assertEqual(pending.classify(cfg, led, _prec(epoch=2, revision=1, sha256="b" * 64))[0], pending.APPLY)


class TestAdoptedOrdering(unittest.TestCase):
    def setUp(self):
        self.cfg = _temp_cfg(provider="theseus", ptype="adopt")

    def _arec(self, sv, **kw):
        return _prec(ordering_kind="adopted", provider="theseus", source_version=sv, **kw)

    def test_numeric_dash_ordering(self):
        led = _ledger(self._arec("17.4-2"))
        self.assertEqual(pending.classify(self.cfg, led, self._arec("17.4-10", sha256="b" * 64))[0], pending.APPLY)
        self.assertEqual(pending.classify(self.cfg, led, self._arec("17.4-1"))[0], pending.DISCARD)


class TestApplyIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        (self.tmp / "state").mkdir()
        (self.tmp / "pending").mkdir()
        merge.ScrapeState().save(self.tmp / "state" / "scrape-versions.json")
        merge.LedgerState().save(self.tmp / "state" / "asset-revisions.json")
        (self.tmp / "redis.json").write_text(
            json.dumps({"name": "redis", "display_name": "Redis", "kind": "service", "releases": []}, indent=2) + "\n",
            encoding="utf-8")

    def _drop(self, **kw):
        rec = _prec(**kw)
        d = {k: getattr(rec, k) for k in pending.PENDING_FIELDS}
        name = f"{rec.component}-{rec.version}{'' if rec.revision <= 1 else '-r%d' % rec.revision}-{rec.platform.replace('/', '-')}.json"
        (self.tmp / "pending" / name).write_text(json.dumps(d, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def test_apply_writes_manifest_and_ledger(self):
        self._drop()
        result = ap.apply(self.tmp, today="2026-07-20")
        self.assertEqual(len(result["applied"]), 1)
        # Manifest now carries the release.
        redis = json.loads((self.tmp / "redis.json").read_text(encoding="utf-8"))
        self.assertEqual(redis["releases"][0]["version"], "8.8.0")
        self.assertEqual(redis["releases"][0]["released_at"], "2026-07-20")
        self.assertIn("windows/amd64", redis["releases"][0]["platforms"])
        # Ledger updated, pending consumed.
        ledger = merge.LedgerState.load(self.tmp / "state" / "asset-revisions.json")
        self.assertIsNotNone(ledger.get("redis", "8.8.0", "windows/amd64"))
        self.assertEqual(list((self.tmp / "pending").glob("*.json")), [])

    def test_second_platform_reuses_released_at(self):
        self._drop()
        ap.apply(self.tmp, today="2026-07-20")
        # A second platform of the same version, applied on a different day.
        self._drop(platform="linux/amd64", url=REDIS_URL.replace("windows-amd64", "linux-amd64"),
                   provider="devxdk-redis-unix")
        ap.apply(self.tmp, today="2026-09-09")
        redis = json.loads((self.tmp / "redis.json").read_text(encoding="utf-8"))
        rel = redis["releases"][0]
        self.assertEqual(set(rel["platforms"]), {"windows/amd64", "linux/amd64"})
        self.assertEqual(rel["released_at"], "2026-07-20")  # first publication date reused

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
