"""Tests for build-leg planning: the static leg set, config<->job parity, and
the canonical revision/naming helpers."""

import pathlib
import unittest

from devxdk_manifest import config, plan

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
BUILD_RUNTIMES = REPO_ROOT / ".github" / "workflows" / "build-runtimes.yml"


class TestStaticLegs(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()

    def test_leg_id(self):
        self.assertEqual(plan.leg_id("redis", "windows/amd64"), "redis-windows-amd64")

    def test_managed_only(self):
        ids = plan.static_leg_ids(self.cfg)
        # php/redis/valkey/python/postgres are managed; go/node/composer/mariadb
        # are scraped and must have NO leg.
        self.assertIn("redis-windows-amd64", ids)
        self.assertIn("php-linux-amd64", ids)
        self.assertFalse(any(i.startswith(("go-", "node-", "composer-", "mariadb-")) for i in ids))
        # nginx: only the built Unix platforms, never the scraped Windows one.
        self.assertIn("nginx-linux-amd64", ids)
        self.assertNotIn("nginx-windows-amd64", ids)


class TestParity(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()

    def test_committed_workflow_in_parity(self):
        text = BUILD_RUNTIMES.read_text(encoding="utf-8")
        self.assertEqual(plan.check_static_job_parity(self.cfg, text), [])

    def test_missing_job_flagged(self):
        text = BUILD_RUNTIMES.read_text(encoding="utf-8").replace("  leg-redis-windows-amd64:", "  leg-removed:")
        errors = plan.check_static_job_parity(self.cfg, text)
        self.assertTrue(any("missing caller job leg-redis-windows-amd64" in e for e in errors))
        self.assertTrue(any("stale caller job leg-removed" in e for e in errors))


class TestHelpers(unittest.TestCase):
    def test_next_revision(self):
        self.assertEqual(plan.next_revision([]), 1)
        self.assertEqual(plan.next_revision([1]), 2)
        self.assertEqual(plan.next_revision([1, 2, 3]), 4)
        self.assertEqual(plan.next_revision([1, 3]), 2)

    def test_archive_name(self):
        self.assertEqual(plan.archive_name("redis", "8.8.0", 1, "windows/amd64", "zip"),
                         "redis-8.8.0-windows-amd64.zip")
        self.assertEqual(plan.archive_name("redis", "8.8.0", 3, "linux/amd64", "tar.gz"),
                         "redis-8.8.0-r3-linux-amd64.tar.gz")

    def test_release_tag(self):
        self.assertEqual(plan.release_tag("php", "8.5.6", 1), "php-8.5.6")
        self.assertEqual(plan.release_tag("php", "8.5.6", 2), "php-8.5.6-r2")


if __name__ == "__main__":
    unittest.main()
