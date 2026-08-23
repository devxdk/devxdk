"""Revision assignment at the write boundary.

THE COMPARISON IS BYTE IDENTITY AGAINST THE PRIOR FILE'S ACTUAL BYTES, and the
tests that matter most here are the ones a SEMANTIC comparison passes. A
semantic implementation passes almost everything in this file and fails
TestByteRule — and its production symptom is a permanent rollback refusal for
that component on every client holding a mark.
"""

import io
import json
import pathlib
import unittest

from devxdk_manifest import schema, strictjson

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _manifest(revision=None, releases=None, display_name="Redis"):
    doc = {"name": "redis", "display_name": display_name, "kind": "service"}
    if revision is not None:
        doc["revision"] = revision
    doc["releases"] = releases if releases is not None else []
    return doc


def _release(ver="8.8.0"):
    return [{"version": ver, "channel": "stable", "released_at": "2026-01-01",
             "platforms": {"windows/amd64": {"url": "https://example.com/x.zip",
                                             "sha256": "a" * 64, "size_bytes": 10}}}]


class WriteCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = self.dir / "redis.json"

    def seed(self, raw):
        if isinstance(raw, (bytes, bytearray)):
            self.path.write_bytes(raw)
        else:
            with io.open(self.path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(schema.dump_str(raw))

    def read(self):
        return self.path.read_bytes()

    def revision(self):
        return strictjson.load(self.path)["revision"]


class TestSlotAndAssignment(WriteCase):
    def test_write_not_component_injects_the_revision(self):
        # component() takes no path and no prior, so it structurally cannot
        # preserve or increment a counter — and must not pretend to.
        built = schema.component("redis", "Redis", "service", [])
        self.assertNotIn("revision", built)
        schema.write(self.path, built)
        self.assertEqual(self.revision(), 1)

    def test_the_slot_is_fixed_not_appended(self):
        schema.write(self.path, schema.component("redis", "Redis", "service", []))
        keys = list(strictjson.load(self.path).keys())
        self.assertEqual(keys, ["name", "display_name", "kind", "revision", "releases"])

    def test_first_write_starts_at_one(self):
        schema.write(self.path, _manifest())
        self.assertEqual(self.revision(), 1)

    def test_a_content_change_bumps_exactly_once(self):
        schema.write(self.path, _manifest())
        schema.write(self.path, _manifest(releases=_release()))
        self.assertEqual(self.revision(), 2)

    def test_writing_identical_content_twice_is_idempotent(self):
        schema.write(self.path, _manifest(releases=_release()))
        first = self.read()
        schema.write(self.path, _manifest(releases=_release()))
        self.assertEqual(self.read(), first, "a zero-diff rewrite must not bump")
        self.assertEqual(self.revision(), 1)

    def test_a_supplied_revision_is_ignored_in_favour_of_the_prior(self):
        schema.write(self.path, _manifest())
        schema.write(self.path, _manifest(revision=999, releases=_release()))
        self.assertEqual(self.revision(), 2)


class TestByteRule(WriteCase):
    """The sharpest failure mode in §4: a SEMANTIC comparison passes every other
    test in this file and fails these."""

    def test_a_formatting_only_change_is_treated_as_changed(self):
        # Same content, different key order — dump_str is insertion-ordered, so
        # the published BYTES move while the semantics do not. Under a semantic
        # comparison the revision would be preserved, the client would see equal
        # revision + different hash, and refuse that component permanently.
        doc = _manifest(revision=1, releases=_release())
        reordered = {"display_name": doc["display_name"], "name": doc["name"],
                     "kind": doc["kind"], "revision": 1, "releases": doc["releases"]}
        self.seed(reordered)
        schema.write(self.path, _manifest(releases=_release()))
        self.assertEqual(self.revision(), 2)

    def test_byte_identical_content_preserves_the_revision(self):
        # The negative control for the test above.
        self.seed(_manifest(revision=7, releases=_release()))
        before = self.read()
        schema.write(self.path, _manifest(releases=_release()))
        self.assertEqual(self.read(), before)
        self.assertEqual(self.revision(), 7)

    def test_a_CRLF_prior_forces_an_increment(self):
        # A text-mode read translates CRLF to LF, so a CRLF prior would compare
        # EQUAL to an LF candidate: the revision preserved while the bytes
        # published changed. read_bytes() is what closes that door.
        canonical = schema.dump_str(_manifest(revision=3, releases=_release()))
        self.path.write_bytes(canonical.replace("\n", "\r\n").encode("utf-8"))
        schema.write(self.path, _manifest(releases=_release()))
        self.assertEqual(self.revision(), 4)

    def test_an_absent_final_newline_forces_an_increment(self):
        canonical = schema.dump_str(_manifest(revision=3, releases=_release()))
        self.path.write_bytes(canonical.rstrip("\n").encode("utf-8"))
        schema.write(self.path, _manifest(releases=_release()))
        self.assertEqual(self.revision(), 4)

    def test_trailing_whitespace_forces_an_increment(self):
        canonical = schema.dump_str(_manifest(revision=3, releases=_release()))
        self.path.write_bytes((canonical + "   \n").encode("utf-8"))
        schema.write(self.path, _manifest(releases=_release()))
        self.assertEqual(self.revision(), 4)

    def test_a_non_canonical_prior_is_normalized_AND_bumped(self):
        # Comparing against a RE-DUMP of the parsed prior would silently
        # normalize this and report "unchanged" while the bytes moved — the same
        # lockout through a third door. Compare against the RAW bytes.
        doc = _manifest(revision=3, releases=_release())
        self.path.write_bytes((json.dumps(doc, indent=4) + "\n").encode("utf-8"))
        schema.write(self.path, _manifest(releases=_release()))
        self.assertEqual(self.revision(), 4)
        self.assertEqual(self.read(), schema.dump_str(
            _manifest(revision=4, releases=_release())).encode("utf-8"))

    def test_a_digit_width_change_is_handled_without_token_surgery(self):
        self.seed(_manifest(revision=9, releases=_release()))
        schema.write(self.path, _manifest(releases=_release("9.0.0")))
        self.assertEqual(self.revision(), 10)


class TestFailClosed(WriteCase):
    def test_a_missing_prior_revision_errors_rather_than_resetting(self):
        # Resetting to 1 would be a SILENT ROLLBACK for every client holding a
        # mark for this component.
        self.seed(_manifest(releases=_release()))
        with self.assertRaises(schema.SchemaError):
            schema.write(self.path, _manifest(releases=_release("9.0.0")))

    def test_a_non_integer_prior_revision_errors(self):
        self.seed(_manifest(revision="3", releases=_release()))
        with self.assertRaises(schema.SchemaError):
            schema.write(self.path, _manifest())

    def test_a_zero_or_negative_prior_revision_errors(self):
        for bad in (0, -1):
            with self.subTest(bad):
                self.seed(_manifest(revision=bad, releases=_release()))
                with self.assertRaises(schema.SchemaError):
                    schema.write(self.path, _manifest())

    def test_a_bool_prior_revision_errors(self):
        self.seed(_manifest(revision=True, releases=_release()))
        with self.assertRaises(schema.SchemaError):
            schema.write(self.path, _manifest())

    def test_an_out_of_int64_range_prior_revision_errors(self):
        self.path.write_bytes(
            ('{"name":"redis","display_name":"Redis","kind":"service","revision":'
             + str(schema.INT64_MAX + 1) + ',"releases":[]}\n').encode("utf-8"))
        with self.assertRaises(schema.SchemaError):
            schema.write(self.path, _manifest())

    def test_a_duplicate_bearing_prior_errors_rather_than_incrementing(self):
        # The fail-closed answer, and it matches the malformed-prior rule: an
        # ambiguous prior must not read as "changed" and quietly take a bump.
        self.path.write_bytes(b'{"name":"redis","kind":"service","revision":1,'
                              b'"Revision":100,"releases":[]}\n')
        with self.assertRaises(strictjson.StrictJSONError):
            schema.write(self.path, _manifest())

    def test_the_file_is_untouched_when_the_prior_is_malformed(self):
        self.seed(_manifest(releases=_release()))
        before = self.read()
        with self.assertRaises(schema.SchemaError):
            schema.write(self.path, _manifest())
        self.assertEqual(self.read(), before)


class TestLiveTree(unittest.TestCase):
    def test_all_backfilled_manifests_carry_a_positive_revision(self):
        found = 0
        for path in sorted(REPO_ROOT.glob("*.json")):
            doc = schema.load(path)
            if not schema.is_component_manifest(doc):
                continue
            found += 1
            with self.subTest(path.name):
                self.assertIsNone(schema.require_positive_int64(
                    doc.get("revision"), f"{path.name}: revision"))
        self.assertGreaterEqual(found, 12, "expected the published component set")

    def test_byte_fidelity_through_git_is_still_disabled_for_signed_artifacts(self):
        # Anti-rollback now depends on this too, not just signature
        # verification: with normalization enabled a checkout could rewrite line
        # endings and the client's raw-body hash would be unreproducible.
        attrs = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertRegex(attrs, r"(?m)^\*\.json\s+-text\s*$")
        self.assertRegex(attrs, r"(?m)^\*\.minisig\s+-text\s*$")


if __name__ == "__main__":
    unittest.main()
