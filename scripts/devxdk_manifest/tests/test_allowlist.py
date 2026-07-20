"""Allowlist host-matching and the vendored-vs-app-source parity check."""

import pathlib
import unittest

from devxdk_manifest import allowlist

# In this dev layout the manifest repo is nested inside the app checkout; in CI
# the signer job checks the app repo out to app-src/. Try both, skip if neither.
_CANDIDATES = [
    pathlib.Path(__file__).resolve().parents[4] / "internal" / "download" / "allowlist.go",
    pathlib.Path(__file__).resolve().parents[3] / "app-src" / "internal" / "download" / "allowlist.go",
]


class TestHostAllowed(unittest.TestCase):
    def test_exact_and_subdomain(self):
        self.assertTrue(allowlist.host_allowed("nodejs.org"))
        self.assertTrue(allowlist.host_allowed("dist.nodejs.org"))
        self.assertTrue(allowlist.host_allowed("manifest.devxdk.com"))
        self.assertTrue(allowlist.host_allowed("NODEJS.ORG"))       # case-insensitive
        self.assertTrue(allowlist.host_allowed("go.dev."))          # trailing dot

    def test_rejects_lookalikes(self):
        self.assertFalse(allowlist.host_allowed("nodejs.org.evil.com"))
        self.assertFalse(allowlist.host_allowed("evil-nodejs.org"))
        self.assertFalse(allowlist.host_allowed("example.com"))
        self.assertFalse(allowlist.host_allowed(""))


class TestParity(unittest.TestCase):
    def test_vendored_matches_app_source(self):
        src = next((c for c in _CANDIDATES if c.exists()), None)
        if src is None:
            self.skipTest("app-src allowlist.go not available (secretless PR CI)")
        parsed = allowlist.parse_allowlist_go(src)
        self.assertEqual(
            parsed, allowlist.VENDORED_HOSTS,
            "vendored allowlist drifted from internal/download/allowlist.go — re-vendor it",
        )


if __name__ == "__main__":
    unittest.main()
