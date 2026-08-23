"""The non-manifest trust readers refuse an ambiguous document too.

A valid signature, or a matching sha256, never implies a well-formed body — that
is the same distinction §4 draws for manifests, applied to the records that
authorize a trust-root transition, rewrite signed manifests, or carry ordering
state.
"""

import json
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

import ci_verify  # noqa: E402
from devxdk_manifest import handoff, merge, strictjson  # noqa: E402

REPO_ROOT = SCRIPTS.parent


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)


class TestOrderingStateLoaders(TempDir):
    def test_the_committed_state_files_still_load(self):
        # The strict primitive must not reject what is already published.
        merge.ScrapeState.load(REPO_ROOT / "state" / "scrape-versions.json")
        merge.LedgerState.load(REPO_ROOT / "state" / "asset-revisions.json")

    def test_a_case_colliding_scrape_state_is_a_GuardError(self):
        path = self.dir / "scrape-versions.json"
        path.write_bytes(b'{"schema":1,"records":{},"Records":{"x":1}}\n')
        with self.assertRaises(merge.GuardError) as cm:
            merge.ScrapeState.load(path)
        self.assertIn("strict JSON", str(cm.exception))

    def test_a_duplicate_bearing_ledger_is_a_GuardError(self):
        path = self.dir / "asset-revisions.json"
        path.write_bytes(b'{"schema":1,"entries":{},"entries":{"x":1}}\n')
        with self.assertRaises(merge.GuardError):
            merge.LedgerState.load(path)

    def test_a_bad_schema_field_is_still_checked_after_the_strict_parse(self):
        path = self.dir / "asset-revisions.json"
        path.write_bytes(b'{"schema":99,"entries":{}}\n')
        with self.assertRaises(merge.GuardError) as cm:
            merge.LedgerState.load(path)
        self.assertIn("schema", str(cm.exception))


class TestRotationRecord(TempDir):
    """ci_verify's rotation check authorizes a TRUST-ROOT TRANSITION, and its
    contract is (ok, reason) — so it must RETURN, never raise."""

    def _verify(self, body):
        record = self.dir / "rotation.json"
        record.write_bytes(body)
        (self.dir / "rotation.json.minisig").write_bytes(b"sig\n")
        verify = ci_verify.default_record_verify("minisign")
        # The signature VERIFIES; the body is still ambiguous. That is the whole
        # point: authenticity never implies well-formedness.
        with mock.patch.object(ci_verify.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="", stderr="")):
            return verify(record, "OLDPUB", "NEWPUB", "manifest")

    def test_a_case_colliding_record_returns_false_with_a_reason(self):
        ok, reason = self._verify(
            b'{"trust_root":"manifest","old_pub":"OLDPUB",'
            b'"new_pub":"NEWPUB","New_pub":"ATTACKER"}\n')
        self.assertFalse(ok)
        self.assertIn("strict JSON", reason)

    def test_a_duplicate_member_returns_false_with_a_reason(self):
        ok, reason = self._verify(
            b'{"trust_root":"manifest","new_pub":"NEWPUB",'
            b'"new_pub":"ATTACKER","old_pub":"OLDPUB"}\n')
        self.assertFalse(ok)
        self.assertIn("strict JSON", reason)

    def test_a_well_formed_record_still_passes(self):
        ok, reason = self._verify(
            b'{"trust_root":"manifest","old_pub":"OLDPUB","new_pub":"NEWPUB"}\n')
        self.assertTrue(ok, reason)

    def test_a_field_mismatch_still_returns_its_own_reason(self):
        ok, reason = self._verify(
            b'{"trust_root":"manifest","old_pub":"WRONG","new_pub":"NEWPUB"}\n')
        self.assertFalse(ok)
        self.assertIn("old_pub", reason)


class TestHandoffManifest(TempDir):
    def test_an_ambiguous_handoff_manifest_raises_HandoffError(self):
        # Through its EXISTING try/except, so the failure keeps its typed shape.
        (self.dir / "manifest.json").write_bytes(
            b'{"schema":1,"members":[],"Members":[{"path":"x"}]}\n')
        with self.assertRaises(handoff.HandoffError) as cm:
            handoff.verify(self.dir)
        self.assertIn("not valid JSON", str(cm.exception))

    def test_a_plain_syntax_error_still_raises_HandoffError(self):
        # json.JSONDecodeError is deliberately still caught there — the strict
        # parser ADDS a type, it does not replace one.
        (self.dir / "manifest.json").write_bytes(b"{ not json\n")
        with self.assertRaises(handoff.HandoffError):
            handoff.verify(self.dir)


class TestPendingAndRevocationRecords(TempDir):
    def test_a_duplicate_bearing_record_is_refused_before_it_is_used(self):
        # Both records REWRITE SIGNED MANIFESTS on the strength of what they
        # parse, which is why they go strict rather than being treated as
        # internal plumbing.
        path = self.dir / "rec.json"
        path.write_bytes(b'{"component":"redis","component":"nginx"}\n')
        with self.assertRaises(strictjson.StrictJSONError):
            strictjson.load(path)

    def test_a_well_formed_record_loads(self):
        path = self.dir / "rec.json"
        path.write_text(json.dumps({"component": "redis"}) + "\n", encoding="utf-8")
        self.assertEqual(strictjson.load(path), {"component": "redis"})


if __name__ == "__main__":
    unittest.main()
