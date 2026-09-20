"""Pin the Python comparator to the Go client via the shared test vectors.

The vectors live in ``testdata/version-vectors.json``, vendored byte-identically
from the app repo's ``internal/version/testdata/version-vectors.json``. If the
two comparators ever disagree, this test (Python side) and the Go vectors test
(Go side) both fail, so a release can never be sorted or de-duplicated
differently by the pipeline than by the client.
"""

import json
import pathlib
import unittest

from devxdk_manifest import versions

VECTORS = pathlib.Path(__file__).resolve().parents[1] / "testdata" / "version-vectors.json"


def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


class TestVersionVectors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(VECTORS.read_text(encoding="utf-8"))

    def test_compare(self):
        for c in self.data["compare"]:
            a, b, want = c["a"], c["b"], c["cmp"]
            got = _sign(versions.compare_str(a, b))
            self.assertEqual(got, want, f"compare({a!r},{b!r})={got}, want {want}")
            # Antisymmetry: swapping the operands negates the result.
            got_rev = _sign(versions.compare_str(b, a))
            self.assertEqual(got_rev, -want, f"compare({b!r},{a!r})={got_rev}, want {-want}")

    def test_parse(self):
        for c in self.data["parse"]:
            s = c["in"]
            if not c["ok"]:
                with self.assertRaises(versions.ParseError, msg=f"parse({s!r}) should fail"):
                    versions.parse(s)
                continue
            v = versions.parse(s)
            self.assertEqual(v.major, c["major"], f"parse({s!r}).major")
            self.assertEqual(v.minor, c["minor"], f"parse({s!r}).minor")
            self.assertEqual(v.patch, c["patch"], f"parse({s!r}).patch")
            self.assertEqual(v.prerelease, c["prerelease"], f"parse({s!r}).prerelease")


class TestProviderKey(unittest.TestCase):
    def test_numeric_dash(self):
        # Adopted-asset ordering keys compare numerically across dots and dashes.
        self.assertEqual(_sign(versions.compare_provider_key("17.4-2", "17.4-10")), -1)
        self.assertEqual(_sign(versions.compare_provider_key("17.4-10", "17.4-2")), 1)
        self.assertEqual(_sign(versions.compare_provider_key("17.4-2", "17.4-2")), 0)
        self.assertEqual(_sign(versions.compare_provider_key("18.1-1", "17.9-9")), 1)


class TestReleaseFamilies(unittest.TestCase):
    def test_canonical_and_bounded(self):
        for value in ("22", "1.26", "7.4.33", "0"):
            self.assertTrue(versions.valid_family(value), value)
        for value in ("", "v8", "+8", "08.2", "8.", "8.2.1.0", str(1 << 63)):
            self.assertFalse(versions.valid_family(value), value)

    def test_matching_and_overlap(self):
        for release, family, expected in (
            ("22.23.2", "22", True), ("1.26.8", "1.26", True),
            ("1.27.1", "1.26", False), ("8.20.0", "8.2", False),
            ("18.6", "18", True), ("8.6.0-rc1", "8.6", True),
            ("7.4.34", "7.4.33", False), ("nonsense", "1", False),
        ):
            self.assertEqual(versions.in_family(release, family), expected)
        self.assertTrue(versions.families_overlap("8", "8.2"))
        self.assertFalse(versions.families_overlap("8.2", "8.20"))


if __name__ == "__main__":
    unittest.main()
