"""Tests for the scrape-versions monotonic guard, seeding, parity, and recompose."""

import pathlib
import unittest

from devxdk_manifest import config, merge, schema

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
STATE_FILE = REPO_ROOT / "state" / "scrape-versions.json"


def _tup(v, sha="a" * 64, url=None, size=1, channel="stable", released=""):
    return merge.Tuple(v, url or f"https://x/{v}", sha, size, channel, released)


def _rec(provider="p", floor=None, tuples=(), revoked=()):
    return merge.ScrapeRecord(
        provider=provider, epoch=1, floor_version=floor,
        tuples=list(tuples), revoked=list(revoked),
    )


class TestCommittedState(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()
        self.state = merge.ScrapeState.load(STATE_FILE)

    def test_canonical_serialization(self):
        # The committed state file must already be in the canonical byte layout
        # (sorted keys, indent=2, trailing newline) the pipeline writes.
        self.assertEqual(self.state.dump_str(), STATE_FILE.read_text(encoding="utf-8"))

    def test_parity_clean(self):
        errors = merge.check_scrape_parity(self.cfg, self.state, REPO_ROOT)
        self.assertEqual(errors, [], f"scrape parity errors: {errors}")

    def test_seed_reproduces_committed_state(self):
        # At the bootstrap snapshot a fresh seed equals the committed state.
        seeded = merge.seed(self.cfg, REPO_ROOT)
        self.assertEqual(seeded.dump_str(), self.state.dump_str())

    def test_parity_detects_orphan_state_tuple(self):
        # Delete a manifest-backed tuple's manifest side by mutating a copy of
        # the state to hold an extra tuple with no manifest -> parity must flag it.
        st = merge.ScrapeState.load(STATE_FILE)
        rec = st.get("node", "24", "windows/amd64")
        rec.tuples.append(_tup("24.99.0"))
        errors = merge.check_scrape_parity(self.cfg, st, REPO_ROOT)
        self.assertTrue(any("24.99.0" in e for e in errors))


class TestLineFor(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()

    def test_matches(self):
        cases = {
            ("node", "24.18.0"): "24",
            ("go", "1.26.5"): "1",
            ("mariadb", "11.8.10"): "11.8",
            ("nginx", "1.30.5"): "1.30",
            ("php", "8.5.6"): "8.5",
            ("php", "8.4.20"): "8.4",
        }
        for (c, v), want in cases.items():
            self.assertEqual(merge.line_for(self.cfg, c, v), want, f"{c} {v}")

    def test_untracked_returns_none(self):
        self.assertIsNone(merge.line_for(self.cfg, "node", "25.0.0"))
        self.assertIsNone(merge.line_for(self.cfg, "mariadb", "10.5.1"))


class TestGuard(unittest.TestCase):
    def test_admit_newer_and_evict(self):
        r = _rec(floor="24.17.0", tuples=[_tup("24.17.0")])
        new, actions = merge.reconcile_key(r, [_tup("24.18.0")], retain=1)
        self.assertEqual(new.floor_version, "24.18.0")
        self.assertEqual([t.version for t in new.tuples], ["24.18.0"])
        self.assertIn(("admit", "24.18.0"), actions)
        self.assertIn(("evict", "24.17.0"), actions)

    def test_equal_idempotent(self):
        r = _rec(floor="24.17.0", tuples=[_tup("24.17.0", sha="b" * 64)])
        new, actions = merge.reconcile_key(r, [_tup("24.17.0", sha="b" * 64)], retain=1)
        self.assertEqual([t.version for t in new.tuples], ["24.17.0"])
        self.assertEqual(new.floor_version, "24.17.0")
        self.assertFalse(any(a[0] == "admit" for a in actions))

    def test_equal_version_mutation_errors(self):
        r = _rec(floor="24.17.0", tuples=[_tup("24.17.0", sha="b" * 64)])
        with self.assertRaises(merge.GuardError):
            merge.reconcile_key(r, [_tup("24.17.0", sha="c" * 64)], retain=1)

    def test_below_floor_ignored(self):
        r = _rec(floor="24.18.0", tuples=[_tup("24.18.0")])
        new, actions = merge.reconcile_key(r, [_tup("24.16.0")], retain=1)
        self.assertEqual([t.version for t in new.tuples], ["24.18.0"])
        self.assertIn(("ignore-below-floor", "24.16.0"), actions)

    def test_retention_evicts_oldest(self):
        r = _rec(floor="11.8.9", tuples=[_tup("11.8.9"), _tup("11.8.8")])
        new, actions = merge.reconcile_key(r, [_tup("11.8.10")], retain=2)
        self.assertEqual([t.version for t in new.tuples], ["11.8.10", "11.8.9"])
        self.assertIn(("evict", "11.8.8"), actions)
        self.assertEqual(new.floor_version, "11.8.10")

    def test_revoked_version_errors(self):
        r = _rec(floor="8.0.0", tuples=[_tup("8.0.0")], revoked=[_tup("7.9.9")])
        with self.assertRaises(merge.GuardError):
            merge.reconcile_key(r, [_tup("7.9.9")], retain=3)

    def test_feed_drop_keeps_committed_tuple(self):
        # Upstream stops listing 11.8.9; immutability keeps it (no rollback).
        r = _rec(floor="11.8.9", tuples=[_tup("11.8.9"), _tup("11.8.8")])
        new, actions = merge.reconcile_key(r, [_tup("11.8.8")], retain=2)
        self.assertEqual(sorted(t.version for t in new.tuples), ["11.8.8", "11.8.9"])
        self.assertFalse(any(a[0] in ("admit", "evict") for a in actions))
        self.assertEqual(new.floor_version, "11.8.9")


class TestReconcileRecompose(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()

    def test_recompose_reproduces_committed_manifest(self):
        state = merge.ScrapeState.load(STATE_FILE)
        for name in ("node", "go", "mariadb", "nginx", "composer"):
            comp = schema.load(REPO_ROOT / f"{name}.json")
            rebuilt = merge.recompose(name, comp["display_name"], comp["kind"], self.cfg, state)
            self.assertEqual(
                schema.dump_str(rebuilt),
                (REPO_ROOT / f"{name}.json").read_text(encoding="utf-8"),
                f"recompose({name}) must reproduce the committed manifest byte-for-byte",
            )

    def test_scrape_reconcile_idempotent(self):
        # Reconciling the committed node manifest against the state changes nothing.
        state = merge.ScrapeState.load(STATE_FILE)
        node = schema.load(REPO_ROOT / "node.json")
        _st, manifest, actions = merge.scrape_reconcile(state, self.cfg, node)
        self.assertEqual(schema.dump_str(manifest), (REPO_ROOT / "node.json").read_text(encoding="utf-8"))
        self.assertFalse(any(a[3][0] in ("admit", "evict") for a in actions))

    def test_scrape_reconcile_admits_newer(self):
        state = merge.ScrapeState.load(STATE_FILE)
        node = schema.load(REPO_ROOT / "node.json")
        # Bump the candidate to a newer patch on every platform.
        rel = node["releases"][0]
        rel["version"] = "24.18.0"
        for pkey, a in rel["platforms"].items():
            a["url"] = a["url"].replace("24.17.0", "24.18.0")
        _st, manifest, actions = merge.scrape_reconcile(state, self.cfg, node)
        self.assertEqual(manifest["releases"][0]["version"], "24.18.0")
        self.assertTrue(any(a[3] == ("admit", "24.18.0") for a in actions))
        # retain_per_line = 1 -> 24.17.0 evicted, floor advanced.
        self.assertEqual(state.get("node", "24", "windows/amd64").floor_version, "24.18.0")


if __name__ == "__main__":
    unittest.main()
