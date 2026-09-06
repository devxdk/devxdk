"""PHP trust material and distinct source-confirmation outcomes."""
import pathlib
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "ci"))
import check_php_rm_keys as roster
import confirm_php_source as confirmation
from devxdk_manifest import config, fetch

SHA = "a" * 64


def release(version="8.5.10", sha=SHA):
    return {version: {"source": [{"filename": f"php-{version}.tar.gz", "sha256": sha}]}}


class Feed:
    def __init__(self, *responses):
        self.responses = iter(responses)

    def get_json(self, url):
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class SourceConfirmation(unittest.TestCase):
    def test_exact_source_returns_digest(self):
        self.assertEqual(confirmation.confirm("8.5", "8.5.10", SHA, Feed(release())), (0, SHA))

    def test_stale_response_can_converge(self):
        sleeps = []
        result = confirmation.confirm("8.5", "8.5.10", SHA,
                                      Feed(release("8.5.9"), release()), sleeps.append)
        self.assertEqual(result, (0, SHA))
        self.assertEqual(sleeps, [5])

    def test_failure_classes_stay_distinct(self):
        cases = [(Feed(release("8.5.11")), 2),
                 (Feed(fetch.FetchError("DNS unavailable")), 3),
                 (Feed(ValueError("invalid JSON")), 4),
                 (Feed(release(sha="bad")), 4),
                 (Feed(release(sha="b" * 64)), 4),
                 (Feed(release("8.5.9"), release("8.5.9"), release("8.5.9")), 5)]
        for feed, expected in cases:
            with self.subTest(expected=expected):
                code, message = confirmation.confirm("8.5", "8.5.10", SHA, feed, lambda _: None)
                self.assertEqual(code, expected, message)


class Roster(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()
        key = self.cfg.pins["php_keys"]["fingerprints"][0]
        self.html = ''.join(f'<h3>PHP {line}</h3><pre>pub rsa4096\n {key}\nuid Manager</pre>'
                            for line in ("8.4", "8.5"))

    def test_all_sections_required(self):
        self.assertEqual(roster.check(self.html, self.cfg), [])
        self.assertTrue(roster.check(self.html.replace('PHP 8.4', 'PHP 9.4'), self.cfg))
        self.assertTrue(roster.check('<html>maintenance</html>', self.cfg))

    def test_unknown_manager_and_malformed_section_fail(self):
        key = self.cfg.pins["php_keys"]["fingerprints"][0]
        self.assertTrue(roster.check(self.html.replace(key, 'F' * 40), self.cfg))
        self.assertTrue(roster.check(self.html.replace(key, 'truncated'), self.cfg))

    def test_duplicate_section_is_not_silently_merged(self):
        with self.assertRaises(ValueError):
            roster.check(self.html + self.html, self.cfg)


@unittest.skipIf(sys.platform == "win32", "uses native Unix GnuPG and bash")
class Keyrings(unittest.TestCase):
    def test_imported_ring_survives_helper_and_rejects_extra_primary(self):
        for component in ("php", "nginx"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as home:
                env = dict(os.environ, GNUPGHOME=home)
                fingerprints = config.load().pins[component + "_keys"]["fingerprints"]
                args = ['bash', str(ROOT / 'scripts/ci/verify_keyring.sh'),
                        str(ROOT / 'scripts/devxdk_manifest/keys' / component), ' '.join(fingerprints)]
                good = subprocess.run(args, env=env, capture_output=True, text=True)
                self.assertEqual(good.returncode, 0, good.stderr)
                listed = subprocess.check_output(['gpg', '--batch', '--with-colons', '--list-keys'], env=env, text=True)
                self.assertIn(fingerprints[0], listed)
                bad = subprocess.run(args[:-1] + [' '.join(fingerprints[1:])], env=env, capture_output=True)
                self.assertNotEqual(bad.returncode, 0)
                subprocess.run(['gpgconf', '--kill', 'all'], env=env, check=True)
