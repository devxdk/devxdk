"""The signature age rule in scripts/ci/scrape-sign-push.sh.

The functions are EXTRACTED FROM THE REAL SCRIPT and sourced, never copied here
— a duplicated shell function would pass this test forever while the script
drifted.

Skipping is possible only on Windows without Git Bash. CI runs on
ubuntu-latest, where bash always exists, so this can never silently vanish
there.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "ci" / "scrape-sign-push.sh"
BASH = shutil.which("bash")


def extract(name):
    """The text of one shell function, from `name() {` to the closing brace."""
    lines = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def extract_assign(name):
    lines = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    return next(l for l in lines if l.startswith(f"{name}="))


@unittest.skipUnless(BASH, "bash is required (present on every CI runner)")
class TestSignatureAgeRule(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        (self.dir / "redis.json").write_text('{"kind":"service","releases":[]}\n', encoding="utf-8")

    def _fake_mansign(self, verify_ok=True):
        path = self.dir / "mansign.sh"
        path.write_text(
            "#!/usr/bin/env bash\n"
            f"exit {0 if verify_ok else 1}\n", encoding="utf-8", newline="\n")
        os.chmod(path, 0o755)
        return path

    def _write_sig(self, body):
        (self.dir / "redis.json.minisig").write_text(body, encoding="utf-8", newline="\n")

    def _run(self, verify_ok=True):
        harness = textwrap.dedent(f"""\
            set -euo pipefail
            {extract_assign('RESIGN_MAX_AGE')}
            {extract_assign('RESIGN_FUTURE_SKEW')}
            {extract('sig_timestamp')}
            {extract('needs_resign')}
            MANSIGN="{self._fake_mansign(verify_ok).as_posix()}"
            derived="RWQfakekey"
            cd "{self.dir.as_posix()}"
            if needs_resign redis.json; then echo RESIGN; else echo SKIP; fi
            """)
        script = self.dir / "harness.sh"
        script.write_text(harness, encoding="utf-8", newline="\n")
        proc = subprocess.run([BASH, str(script)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _trusted(self, stamp):
        return (f"untrusted comment: signature\nSIGBASE64\n"
                f"trusted comment: timestamp:{stamp}\tfile:redis.json\nGLOBALSIGBASE64\n")

    def test_fresh_is_a_noop(self):
        self._write_sig(self._trusted(int(time.time()) - 3600))
        self.assertEqual(self._run(), "SKIP")

    def test_expired_is_resigned(self):
        self._write_sig(self._trusted(int(time.time()) - 8 * 24 * 3600))
        self.assertEqual(self._run(), "RESIGN")

    def test_just_inside_the_window_is_a_noop(self):
        # The boundary matters: 7 days minus an hour must NOT churn.
        self._write_sig(self._trusted(int(time.time()) - 7 * 24 * 3600 + 3600))
        self.assertEqual(self._run(), "SKIP")

    def test_materially_future_is_resigned(self):
        # Verification never looks at the timestamp, so a far-future signature
        # verifies happily and is never "older than 7 days" — it would be stuck
        # un-refreshed forever while the client refuses it as future-dated.
        self._write_sig(self._trusted(int(time.time()) + 5 * 24 * 3600))
        self.assertEqual(self._run(), "RESIGN")

    def test_slightly_future_within_skew_is_a_noop(self):
        self._write_sig(self._trusted(int(time.time()) + 3600))
        self.assertEqual(self._run(), "SKIP")

    def test_missing_timestamp_is_resigned(self):
        self._write_sig("untrusted comment: x\nSIG\ntrusted comment: file:redis.json\nGLOBAL\n")
        self.assertEqual(self._run(), "RESIGN")

    def test_duplicate_timestamp_is_resigned(self):
        self._write_sig("untrusted comment: x\nSIG\n"
                        "trusted comment: timestamp:100\ttimestamp:200\tfile:redis.json\nGLOBAL\n")
        self.assertEqual(self._run(), "RESIGN")

    def test_malformed_timestamp_is_resigned(self):
        self._write_sig("untrusted comment: x\nSIG\n"
                        "trusted comment: timestamp:notanumber\tfile:redis.json\nGLOBAL\n")
        self.assertEqual(self._run(), "RESIGN")

    def test_missing_signature_file_is_resigned(self):
        self.assertEqual(self._run(), "RESIGN")

    def test_invalid_signature_is_resigned(self):
        self._write_sig(self._trusted(int(time.time())))
        self.assertEqual(self._run(verify_ok=False), "RESIGN")


class TestScriptInvariants(unittest.TestCase):
    """Properties of the script text itself that a behavioural test cannot see."""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")

    def test_the_key_equality_assert_is_intact(self):
        # FORCE_RESIGN skips it, which is exactly why the age rule must NOT be
        # implemented with FORCE_RESIGN: that would disable this guard weekly.
        self.assertIn('if [ "${FORCE_RESIGN:-false}" != "true" ] && '
                      '[ "$derived" != "$committed" ]', self.text)

    def test_the_age_rule_does_not_use_force_resign(self):
        body = extract("needs_resign")
        self.assertNotIn("FORCE_RESIGN", body)

    def test_a_refresh_rewrites_only_the_signature(self):
        # sign_changed must not touch the JSON, and therefore must not bump the
        # revision — that is what lands a refreshed manifest on the client's
        # equal-revision + equal-hash fast path instead of looking like new
        # content.
        body = extract("sign_changed")
        self.assertIn('"$MANSIGN" -key "$keyfile" "$f"', body)
        for forbidden in ("schema.write", "python3 scripts/scrape.py", ">", "tee"):
            self.assertNotIn(forbidden, body.replace(">/dev/null", ""))

    def test_is_manifest_goes_through_strictjson(self):
        body = extract("is_manifest")
        self.assertIn("strictjson", body)
        self.assertIn("PYTHONPATH=scripts", body)

    def test_the_history_gate_runs_between_commit_and_push(self):
        commit = self.text.index('git commit -m "chore: refresh')
        gate = self.text.index("check_revision_history.py")
        push = self.text.index('if git push "$remote" HEAD:main')
        self.assertLess(commit, gate)
        self.assertLess(gate, push)
        # INSIDE the retry loop: a rejected push resets to the new tip and
        # replays the transaction, so a hoisted check would validate a base that
        # no longer exists.
        loop = self.text.index("for attempt in 1 2 3 4 5; do")
        self.assertLess(loop, gate)


if __name__ == "__main__":
    unittest.main()
