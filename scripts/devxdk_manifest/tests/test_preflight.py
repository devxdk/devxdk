"""RESOLVE-THEN-WRITE in all four multi-manifest writers.

schema.write now raises on a malformed prior, and these scripts save their state
and ledger only AFTER their loop — so a raise on the SECOND component would
leave the FIRST manifest already rewritten with neither ledger saved, breaking
the "FAILED (nothing written)" contract each of them advertises in its own error
handler.

Each test drives the script's REAL loop and mocks only its neighbours, so a
write-inside-the-loop implementation fails and a resolve-then-write one passes.
"""

import contextlib
import io
import pathlib
import sys
import unittest
from unittest import mock

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

import apply_lifecycle  # noqa: E402
import apply_pending  # noqa: E402
import apply_revocations  # noqa: E402
import scrape  # noqa: E402
from devxdk_manifest import schema, strictjson  # noqa: E402

TWO = ["nginx", "redis"]  # sorted() order matters: nginx resolves, redis raises


def _fake_manifest(name):
    return {"name": name, "display_name": name, "kind": "service", "releases": []}


def _resolve_second_raises():
    """A schema.resolve that succeeds once and then fails, like a tree whose
    SECOND component carries a malformed prior."""
    calls = {"n": 0}

    def side_effect(path, data):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise schema.SchemaError(
                f"{pathlib.Path(path).name}: prior revision must be an integer, got None")
        return schema.with_revision(data, 1)

    return side_effect, calls


class PreflightCase(unittest.TestCase):
    def assert_nothing_written(self, module, run):
        """Run `run` with the module's neighbours mocked; assert zero writes.

        THE assertRaises LIVES INSIDE, not at the call site. With it outside,
        the raise escapes before the assertions below ever execute and the test
        passes against a write-inside-the-loop implementation — verified by
        disarming apply_lifecycle and watching it stay green.
        """
        side_effect, calls = _resolve_second_raises()
        patches = [
            mock.patch.object(module.schema, "load",
                              side_effect=lambda p: _fake_manifest(pathlib.Path(p).stem)),
            mock.patch.object(module.merge, "recompose",
                              side_effect=lambda name, *a, **k: _fake_manifest(name)),
            mock.patch.object(module.schema, "resolve", side_effect=side_effect),
            mock.patch.object(module.schema, "write_resolved"),
        ]
        with contextlib.ExitStack() as stack:
            started = [stack.enter_context(p) for p in patches]
            writer = started[-1]
            with self.assertRaises(schema.SchemaError):
                run()
        self.assertEqual(writer.call_count, 0,
                         "a malformed prior on the SECOND component must leave the FIRST unwritten")
        self.assertGreaterEqual(calls["n"], 2,
                                "every target must be resolved before any is written")

    def assert_clean_refusal(self, module, exc):
        err = io.StringIO()
        with mock.patch.object(module, "apply", side_effect=exc):
            with contextlib.redirect_stderr(err):
                rc = module.main([])
        self.assertEqual(rc, 1)
        self.assertIn("FAILED (nothing written)", err.getvalue())


class TestApplyLifecycle(PreflightCase):
    def test_nothing_written_and_no_state_saved(self):
        state, ledger = mock.Mock(), mock.Mock()
        patches = [
            mock.patch.object(apply_lifecycle.merge.ScrapeState, "load", return_value=state),
            mock.patch.object(apply_lifecycle.merge.LedgerState, "load", return_value=ledger),
            mock.patch.object(apply_lifecycle.config, "load", return_value=mock.Mock()),
            mock.patch.object(apply_lifecycle.lifecycle, "apply_lifecycle", return_value=set(TWO)),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            self.assert_nothing_written(apply_lifecycle, lambda: apply_lifecycle.apply(pathlib.Path(".")))
        state.save.assert_not_called()
        ledger.save.assert_not_called()

    def test_main_reports_the_clean_refusal(self):
        for exc in (schema.SchemaError("bad prior"),
                    strictjson.StrictJSONError("duplicate member")):
            with self.subTest(type(exc).__name__):
                self.assert_clean_refusal(apply_lifecycle, exc)


class TestApplyRevocations(PreflightCase):
    def test_nothing_written_and_no_state_saved(self):
        state, ledger = mock.Mock(), mock.Mock()
        records = [mock.Mock(scope="s", component=c, version="1", platform="p") for c in TWO]
        # Two revocation records, one per component, so the rebuild loop has two
        # targets and the SECOND one's prior is the malformed one.
        patches = [
            mock.patch.object(apply_revocations.merge.ScrapeState, "load", return_value=state),
            mock.patch.object(apply_revocations.merge.LedgerState, "load", return_value=ledger),
            mock.patch.object(apply_revocations.config, "load", return_value=mock.Mock()),
            mock.patch.object(apply_revocations.pathlib.Path, "glob",
                              return_value=[pathlib.Path(f"revocations/{c}.json") for c in TWO]),
            mock.patch.object(apply_revocations.strictjson, "load", return_value={}),
            mock.patch.object(apply_revocations.revoke.RevocationRecord, "from_dict",
                              side_effect=records),
            mock.patch.object(apply_revocations.revoke, "apply",
                              side_effect=[("delete", c) for c in TWO]),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            self.assert_nothing_written(apply_revocations, lambda: apply_revocations.apply(pathlib.Path(".")))
        state.save.assert_not_called()
        ledger.save.assert_not_called()

    def test_main_reports_the_clean_refusal(self):
        self.assert_clean_refusal(apply_revocations,
                                  strictjson.StrictJSONError("duplicate member"))


class TestApplyPending(PreflightCase):
    def test_nothing_written_and_no_ledger_saved(self):
        ledger = mock.Mock()
        patches = [
            mock.patch.object(apply_pending.merge.ScrapeState, "load", return_value=mock.Mock()),
            mock.patch.object(apply_pending.merge.LedgerState, "load", return_value=ledger),
            mock.patch.object(apply_pending.config, "load", return_value=mock.Mock()),
            mock.patch.object(apply_pending, "load_pending",
                              return_value=[(mock.Mock(), mock.Mock()) for _ in TWO]),
            mock.patch.object(apply_pending.pending, "apply_pending_records",
                              return_value=([], [], set(TWO))),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            self.assert_nothing_written(
                apply_pending,
                lambda: apply_pending.apply(pathlib.Path("."), today="2026-01-01"))
        ledger.save.assert_not_called()

    def test_main_reports_the_clean_refusal(self):
        self.assert_clean_refusal(apply_pending, schema.SchemaError("bad prior"))


class TestScrape(unittest.TestCase):
    """scrape.py needed this most: schema.write sat OUTSIDE its except, so a
    raise there escaped main() with earlier manifests already rewritten, state
    never saved, and NO "FAILED" message at all."""

    def _run(self, resolve_side_effect):
        cfg = mock.Mock()
        cfg.scrape_keys.return_value = [(c, "l", "p", "plat") for c in TWO]
        state = mock.Mock()
        err = io.StringIO()
        patches = [
            mock.patch.object(scrape.config, "load", return_value=cfg),
            mock.patch.object(scrape.fetch, "Fetcher", return_value=mock.Mock()),
            mock.patch.object(scrape.merge.ScrapeState, "load", return_value=state),
            mock.patch.object(scrape.merge.LedgerState, "load", return_value=mock.Mock()),
            mock.patch.dict(scrape.SOURCES, {c: (lambda _f: None) for c in TWO}, clear=True),
            mock.patch.object(scrape.merge, "scrape_reconcile",
                              side_effect=lambda st, c, cand, led: (
                                  st, {"releases": [{"version": "1", "released_at": ""}]}, [])),
            mock.patch.object(scrape.schema, "resolve", side_effect=resolve_side_effect),
            mock.patch.object(scrape.schema, "write_resolved"),
            contextlib.redirect_stderr(err),
        ]
        with contextlib.ExitStack() as stack:
            started = [stack.enter_context(p) for p in patches]
            writer = started[-2]
            rc = scrape.main([])
        return rc, writer, state, err.getvalue()

    def test_nothing_written_and_the_failure_is_reported(self):
        side_effect, calls = _resolve_second_raises()
        rc, writer, state, err = self._run(side_effect)
        self.assertEqual(rc, 1)
        self.assertEqual(writer.call_count, 0)
        self.assertGreaterEqual(calls["n"], 2)
        state.save.assert_not_called()
        self.assertIn("FAILED (nothing written)", err)

    def test_the_happy_path_still_writes_everything(self):
        # The negative control: without the fault, both manifests are written
        # and the state is saved.
        rc, writer, state, _err = self._run(lambda p, d: schema.with_revision(d, 1))
        self.assertEqual(rc, 0)
        self.assertEqual(writer.call_count, len(TWO))
        state.save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
