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


if __name__ == "__main__":
    unittest.main()
