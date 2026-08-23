"""The six divergence classes, each refused AT THE RIGHT LAYER.

Classes 1-5 are syntax and belong to strictjson. Class 6 (numbers) is SCHEMA and
must NOT be here: strictjson also parses upstream release feeds and `gh api`
output, where a float is correct JSON. Several tests below assert that
strictjson ACCEPTS class-6 shapes, because that is what proves the layering is
right rather than merely lenient.
"""

import json
import pathlib
import unittest

from devxdk_manifest import schema, strictjson

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


class TestClass1Duplicates(unittest.TestCase):
    def test_exact_duplicate_member_is_refused(self):
        with self.assertRaises(strictjson.StrictJSONError):
            strictjson.loads(b'{"revision": 6, "revision": 7}')

    def test_duplicate_at_depth_is_refused(self):
        with self.assertRaises(strictjson.StrictJSONError):
            strictjson.loads(b'{"releases": [{"version": "1", "version": "2"}]}')


class TestClass2CaseFold(unittest.TestCase):
    def test_revision_alias_is_refused(self):
        # Python reads 6, Go reads 100. A history gate ordering this at 6 makes
        # the client record 100, after which the genuine revision 7 is LOWER
        # than the mark and that component is refused permanently.
        with self.assertRaises(strictjson.StrictJSONError) as cm:
            strictjson.loads(b'{"revision": 6, "Revision": 100}')
        self.assertIn("case-fold", str(cm.exception))

    def test_the_hazard_is_not_specific_to_revision(self):
        for raw in (b'{"name": "redis", "Name": "valkey"}',
                    b'{"kind": "runtime", "Kind": "service"}'):
            with self.assertRaises(strictjson.StrictJSONError):
                strictjson.loads(raw)

    def test_nested_alias_is_refused(self):
        with self.assertRaises(strictjson.StrictJSONError):
            strictjson.loads(b'{"releases": [{"version": "1", "Version": "2"}]}')

    def test_single_uppercase_spelling_is_accepted_as_a_distinct_name(self):
        # {"REVISION": 42} with no lowercase member unmarshals to Revision:42 in
        # Go and to a nothing-named-revision dict in Python. strictjson cannot
        # see that — one object, one name, no collision — so the SCHEMA layer
        # catches it: require_positive_int64 on data["revision"] reports the
        # missing field. Asserted so the division of labour is explicit.
        doc = strictjson.loads(b'{"REVISION": 42}')
        self.assertEqual(doc, {"REVISION": 42})
        self.assertIsNotNone(schema.require_positive_int64(doc.get("revision"), "revision"))

    def test_fold_is_ascii_only_not_unicode(self):
        # Python's str.casefold() maps 'ß' to 'ss'; Go does not. Using it would
        # make the two implementations disagree about the rule meant to make
        # them agree. Non-ASCII names are refused outright instead (class 3).
        self.assertEqual("ß".casefold(), "ss")
        with self.assertRaises(strictjson.StrictJSONError):
            strictjson.loads('{"ß": 1}'.encode("utf-8"))


class TestClass3NonASCIINames(unittest.TestCase):
    def test_non_ascii_member_name_is_refused(self):
        with self.assertRaises(strictjson.StrictJSONError) as cm:
            strictjson.loads('{"naïve": 1}'.encode("utf-8"))
        self.assertIn("non-ASCII", str(cm.exception))

    def test_non_ascii_VALUES_are_fine(self):
        # Only names are restricted — display_name carries real text.
        self.assertEqual(strictjson.loads('{"display_name": "Café"}'.encode("utf-8")),
                         {"display_name": "Café"})


class TestClass4Constants(unittest.TestCase):
    def test_nan_and_infinity_are_refused(self):
        for raw in (b'{"x": NaN}', b'{"x": Infinity}', b'{"x": -Infinity}'):
            with self.assertRaises(strictjson.StrictJSONError):
                strictjson.loads(raw)

    def test_python_accepts_them_without_the_rule(self):
        # The reason the rule exists: the pipeline would validate and SIGN a
        # manifest that no Go client can parse at all.
        self.assertNotEqual(json.loads('{"x": NaN}')["x"], json.loads('{"x": NaN}')["x"])

    def test_dump_str_refuses_to_emit_them(self):
        # The writer must move with the reader, or they disagree by one run.
        with self.assertRaises(ValueError):
            schema.dump_str({"x": float("nan")})


class TestClass5Encoding(unittest.TestCase):
    """Five vectors: two rejections and THREE acceptances.

    This is the only rule implemented as a scan over raw text, so it is the only
    one that can silently OVER-reject a valid manifest — and the failure mode
    there is a publish that refuses a correct document, which nobody notices
    until it happens.
    """

    def test_reject_raw_invalid_utf8_in_a_string(self):
        with self.assertRaises(strictjson.StrictJSONError) as cm:
            strictjson.loads(b'{"v": "\xff\xfe"}')
        self.assertIn("UTF-8", str(cm.exception))

    def test_reject_a_lone_surrogate_escape(self):
        with self.assertRaises(strictjson.StrictJSONError):
            strictjson.loads(b'{"v": "\\ud800"}')
        with self.assertRaises(strictjson.StrictJSONError):
            strictjson.loads(b'{"v": "\\uDC00"}')

    def test_accept_a_valid_surrogate_pair(self):
        self.assertEqual(strictjson.loads(b'{"v": "\\uD83D\\uDE00"}'), {"v": "\U0001F600"})

    def test_accept_an_escaped_backslash_followed_by_uD800(self):
        # The \\u here is a literal backslash then the text "uD800" — not an
        # escape at all. A naive contains("\\ud800") scan rejects this.
        self.assertEqual(strictjson.loads(b'{"v": "\\\\uD800"}'), {"v": "\\uD800"})

    def test_accept_lowercase_hex(self):
        # Hex digits ARE case-insensitive, unlike the ASCII member-name fold
        # rule. An implementation that gets that backwards fails on real emoji.
        self.assertEqual(strictjson.loads(b'{"v": "\\ud83d\\ude00"}'), {"v": "\U0001F600"})

    def test_bytes_are_not_sniffed_for_utf16(self):
        # json.loads sniffs the encoding for a bytes input and accepts UTF-16
        # and UTF-32, so the .decode("utf-8") in strictjson IS the gate — the
        # parser never was one.
        self.assertEqual(json.loads('{"a":1}'.encode("utf-16")), {"a": 1})
        with self.assertRaises(strictjson.StrictJSONError):
            strictjson.loads('{"a":1}'.encode("utf-16"))

    def test_a_str_carrying_a_lone_surrogate_is_refused(self):
        with self.assertRaises(strictjson.StrictJSONError):
            strictjson.loads('{"v": "\ud800"}')


class TestClass6IsNotHere(unittest.TestCase):
    """The layering proof: strictjson must ACCEPT these."""

    def test_accepts_a_float_in_a_non_schema_position(self):
        # A `gh api` response. Imposing our types on third-party JSON would be
        # wrong, not strict.
        self.assertEqual(strictjson.loads(b'{"score": 1.5}'), {"score": 1.5})

    def test_accepts_a_bignum_in_a_non_schema_position(self):
        self.assertEqual(strictjson.loads(b'{"id": 99999999999999999999}')["id"],
                         99999999999999999999)

    def test_the_schema_layer_refuses_them(self):
        for bad in (1.5, 99999999999999999999, True, 0, -1, None, "1"):
            self.assertIsNotNone(schema.require_positive_int64(bad, "revision"),
                                 f"{bad!r} must be refused")
        self.assertIsNone(schema.require_positive_int64(1, "revision"))
        self.assertIsNone(schema.require_positive_int64(schema.INT64_MAX, "revision"))

    def test_int64_max_plus_one_is_refused(self):
        # Python's ints are arbitrary-precision and Go's int64 is not: without
        # the upper bound this class stays open.
        self.assertIsNotNone(schema.require_positive_int64(schema.INT64_MAX + 1, "revision"))

    def test_true_is_refused_because_isinstance_int_accepts_it(self):
        self.assertTrue(isinstance(True, int))
        self.assertIsNotNone(schema.require_positive_int64(True, "revision"))


class TestSplitJSONArrays(unittest.TestCase):
    def test_splits_concatenated_arrays(self):
        chunks = list(strictjson.split_json_arrays('[{"a":1}]\n[{"b":2}]\n'))
        self.assertEqual(chunks, [[{"a": 1}], [{"b": 2}]])

    def test_refusal_surfaces_at_the_consumer(self):
        # A GENERATOR: creating it raises nothing; the `for` loop does.
        gen = strictjson.split_json_arrays('[{"a":1,"A":2}]')
        with self.assertRaises(strictjson.StrictJSONError):
            list(gen)


class TestLiveDocuments(unittest.TestCase):
    def test_all_published_documents_pass_the_scan_unchanged(self):
        # Nothing live breaks — both serializers emit lowercase keys from struct
        # tags and dict literals, so there are no collisions to migrate.
        paths = [p for p in sorted(REPO_ROOT.glob("*.json"))]
        update = REPO_ROOT / "app" / "update.json"
        if update.exists():
            paths.append(update)
        self.assertGreaterEqual(len(paths), 12, "expected the published manifest set")
        for path in paths:
            with self.subTest(path.name):
                strictjson.load(path)


if __name__ == "__main__":
    unittest.main()
