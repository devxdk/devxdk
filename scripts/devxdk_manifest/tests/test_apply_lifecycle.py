"""Tests for apply_lifecycle's fail-closed error handling (L34)."""

import contextlib
import io
import pathlib
import sys
import unittest
from unittest import mock

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

import apply_lifecycle  # noqa: E402
from devxdk_manifest import config, lifecycle  # noqa: E402


class TestMainFailsClosed(unittest.TestCase):
    def _run_with(self, exc):
        err = io.StringIO()
        with mock.patch.object(apply_lifecycle, "apply", side_effect=exc):
            with contextlib.redirect_stderr(err):
                rc = apply_lifecycle.main([])
        return rc, err.getvalue()

    def test_config_error_reports_cleanly(self):
        # L34: reactivate_line -> find_platform raises ConfigError (and so can
        # config.load()) — both must produce the clean fail-closed report,
        # never an uncaught traceback.
        rc, err = self._run_with(config.ConfigError("platform vanished"))
        self.assertEqual(rc, 1)
        self.assertIn("FAILED (nothing written)", err)

    def test_lifecycle_error_reports_cleanly(self):
        rc, err = self._run_with(lifecycle.LifecycleError("boom"))
        self.assertEqual(rc, 1)
        self.assertIn("FAILED (nothing written)", err)


if __name__ == "__main__":
    unittest.main()
