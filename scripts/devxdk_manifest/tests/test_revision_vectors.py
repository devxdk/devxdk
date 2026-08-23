"""The Python half of the cross-language revision vectors.

ONE SPECIFICATION, TWO IMPLEMENTATIONS, kept honest by shared golden vectors
rather than shared code: this file drives schema.next_revision, and the app
repo's Go side drives updatejson's equivalent against a byte-identical vendored
copy. The vector-parity CI job proves the two copies are the same bytes.

A vector that only one language consumes proves nothing, so keep the verdicts
here mechanical — read the file, apply the rule, compare.
"""

import base64
import json
import pathlib
import tempfile
import unittest

from devxdk_manifest import schema, strictjson

VECTORS = (pathlib.Path(__file__).resolve().parents[1]
           / "testdata" / "revision-projection-vectors.json")


def _load():
    with open(VECTORS, "rb") as fh:
        return strictjson.loads(fh.read())


class TestRevisionVectors(unittest.TestCase):
    def setUp(self):
        self.doc = _load()
        self.dir = pathlib.Path(tempfile.mkdtemp())
        import shutil
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_the_file_is_well_formed_and_complete(self):
        self.assertEqual(self.doc["schema"], 1)
        names = [v["name"] for v in self.doc["vectors"]]
        self.assertEqual(len(names), len(set(names)), "vector names must be unique")
        # The three rejections and the negative control are what make the rest
        # meaningful; assert they are present by name so a future edit cannot
        # quietly drop them.
        for required in ("identical-bytes", "formatting-only-reorder",
                         "duplicate-top-level-revision", "case-fold-collision",
                         "non-ascii-member-name"):
            self.assertIn(required, names)
        self.assertTrue(all(v.get("why") for v in self.doc["vectors"]),
                        "every vector states why it exists")

    def test_every_vector(self):
        for v in self.doc["vectors"]:
            with self.subTest(v["name"]):
                path = self.dir / "redis.json"
                path.write_bytes(base64.b64decode(v["prior_b64"]))

                if v["expect"] == "reject":
                    with self.assertRaises(strictjson.StrictJSONError):
                        strictjson.load(path)
                    continue

                prior = strictjson.load(path)
                candidate = base64.b64decode(v["candidate_b64"])
                # THE RULE, spelled out rather than delegated, so this file
                # documents the specification the Go side must match.
                if candidate == path.read_bytes():
                    got = prior["revision"]
                    verdict = "preserve"
                else:
                    got = prior["revision"] + 1
                    verdict = "bump"
                self.assertEqual(verdict, v["expect"])
                self.assertEqual(got, v["expect_revision"])

    def test_schema_next_revision_agrees_with_every_vector(self):
        """The same verdicts through the PRODUCTION code path."""
        for v in self.doc["vectors"]:
            if v["expect"] == "reject":
                continue
            with self.subTest(v["name"]):
                path = self.dir / "redis.json"
                path.write_bytes(base64.b64decode(v["prior_b64"]))
                # next_revision takes the candidate DOCUMENT and re-serializes
                # it with the prior's revision, which must reproduce
                # candidate_b64 exactly — otherwise the vector and the
                # implementation disagree about what "the candidate" is.
                candidate_doc = json.loads(base64.b64decode(v["candidate_b64"]).decode("utf-8"))
                prior_revision = strictjson.load(path)["revision"]
                self.assertEqual(
                    schema.dump_str(schema.with_revision(candidate_doc, prior_revision)).encode("utf-8"),
                    base64.b64decode(v["candidate_b64"]),
                    "candidate_b64 must be canonical output carrying the prior's revision")
                self.assertEqual(schema.next_revision(path, candidate_doc), v["expect_revision"])

    def test_rejections_are_refused_by_the_production_writer_too(self):
        for v in self.doc["vectors"]:
            if v["expect"] != "reject":
                continue
            with self.subTest(v["name"]):
                path = self.dir / "redis.json"
                path.write_bytes(base64.b64decode(v["prior_b64"]))
                with self.assertRaises(strictjson.StrictJSONError):
                    schema.write(path, {"name": "redis", "display_name": "Redis",
                                        "kind": "service", "releases": []})


if __name__ == "__main__":
    unittest.main()
