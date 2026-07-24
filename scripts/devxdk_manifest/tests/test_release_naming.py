"""Tests pinning the single release-tag rule (L37): finalize's download URL
and the pending filename compose from plan.release_tag (publish_legs calls it
directly), so the -rN rule cannot drift into a signed-manifest 404."""

import pathlib
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

import add_built_release  # noqa: E402
import finalize_builds  # noqa: E402
from devxdk_manifest import plan  # noqa: E402


class TestReleaseNaming(unittest.TestCase):
    def test_download_url_composes_from_release_tag(self):
        meta = {"component": "redis", "version": "8.8.0", "revision": 1,
                "archive": "redis-8.8.0-windows-amd64.zip"}
        self.assertEqual(
            finalize_builds._download_url(meta),
            "https://github.com/devxdk/devxdk/releases/download/redis-8.8.0/redis-8.8.0-windows-amd64.zip")
        meta["revision"] = 3
        meta["archive"] = "redis-8.8.0-r3-windows-amd64.zip"
        self.assertEqual(
            finalize_builds._download_url(meta),
            "https://github.com/devxdk/devxdk/releases/download/redis-8.8.0-r3/redis-8.8.0-r3-windows-amd64.zip")

    def test_download_url_adopted_passthrough(self):
        meta = {"ordering_kind": "adopted", "url": "https://upstream/x.tar.gz",
                "component": "postgres", "version": "18.4", "revision": 1}
        self.assertEqual(finalize_builds._download_url(meta), "https://upstream/x.tar.gz")

    def test_pending_filename_composes_from_release_tag(self):
        self.assertEqual(add_built_release.pending_filename("redis", "8.8.0", 1, "windows/amd64"),
                         "redis-8.8.0-windows-amd64.json")
        self.assertEqual(add_built_release.pending_filename("redis", "8.8.0", 2, "linux/amd64"),
                         "redis-8.8.0-r2-linux-amd64.json")
        # Composition identity with the canonical rule.
        self.assertEqual(
            add_built_release.pending_filename("php", "8.5.6", 4, "darwin/arm64"),
            f"{plan.release_tag('php', '8.5.6', 4)}-darwin-arm64.json")


if __name__ == "__main__":
    unittest.main()
