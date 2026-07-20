"""Tests for ci_verify: signature-check orchestration (fake verifier) and the
key-immutability decision logic (real temp git repos)."""

import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

import ci_verify as cv  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

MPUB = "untrusted comment: minisign public key A\nRWRuaScc+Qv/mHYGB4RQRODiMbeKeCluuVzrydsCZMKjkWsuZAaY+gDc\n"
RPUB = "untrusted comment: minisign public key B\nRWShVK5RhICCBU4bzDtjH0x0+P985XOlZwQKd9rwlNAzXoS49rvLHul3\n"


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _sha(cwd):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(cwd), capture_output=True, text=True).stdout.strip()


class TestVerifySignatures(unittest.TestCase):
    def test_all_pairs_pass_with_ok_verifier(self):
        errors = cv.verify_signatures(REPO_ROOT, lambda j, k: (True, ""))
        self.assertEqual(errors, [])

    def test_verifier_failure_reported(self):
        errors = cv.verify_signatures(REPO_ROOT, lambda j, k: (False, "bad"))
        # Every committed component manifest is flagged.
        self.assertTrue(any("does not verify" in e for e in errors))
        self.assertGreaterEqual(len(errors), 5)

    def test_trusted_comment_basename_checked(self):
        # The committed .minisig files carry file:<name>.json; an ok verifier plus
        # the real trusted-comment parse must still pass.
        errors = cv.verify_signatures(REPO_ROOT, lambda j, k: (True, ""))
        self.assertEqual(errors, [])


class TestCheckKeys(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = pathlib.Path(self.tmpdir)
        _git(["init", "-q", "-b", "main"], self.repo)
        _git(["config", "user.email", "t@t"], self.repo)
        _git(["config", "user.name", "t"], self.repo)
        (self.repo / "keys").mkdir()

    def _write_keys(self, mpub=MPUB, rpub=RPUB):
        (self.repo / cv.MANIFEST_KEY).write_text(mpub, encoding="utf-8")
        (self.repo / cv.RELEASE_KEY).write_text(rpub, encoding="utf-8")

    def _commit(self, msg="c"):
        _git(["add", "-A"], self.repo)
        _git(["commit", "-q", "-m", msg], self.repo)
        return _sha(self.repo)

    def _ok_record(self, *a):
        return True, "ok"

    def test_no_change_passes(self):
        self._write_keys()
        base = self._commit()
        (self.repo / "node.json").write_text("{}", encoding="utf-8")  # unrelated change
        self._commit()
        self.assertEqual(cv.check_keys(self.repo, "push", base, self._ok_record), [])

    def test_seed_addition_allowed(self):
        (self.repo / "readme").write_text("x", encoding="utf-8")
        base = self._commit("no keys yet")
        self._write_keys()
        self._commit("seed keys")
        self.assertEqual(cv.check_keys(self.repo, "push", base, self._ok_record), [])

    def test_pr_modification_rejected(self):
        self._write_keys()
        base = self._commit()
        self._write_keys(mpub="untrusted comment: x\nRWRDIFFERENTxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n")
        self._commit()
        errs = cv.check_keys(self.repo, "pull_request", base, self._ok_record)
        self.assertTrue(any("immutable in a pull request" in e for e in errs))

    def test_deletion_rejected(self):
        self._write_keys()
        base = self._commit()
        (self.repo / cv.MANIFEST_KEY).unlink()
        self._commit("delete manifest key")
        errs = cv.check_keys(self.repo, "push", base, self._ok_record)
        self.assertTrue(any("deletion is never allowed" in e for e in errs))

    def test_delete_then_reseed_rejected(self):
        self._write_keys()
        self._commit("add")
        (self.repo / cv.MANIFEST_KEY).unlink()
        del_sha = self._commit("delete")
        self._write_keys()
        self._commit("reseed")
        errs = cv.check_keys(self.repo, "push", del_sha, self._ok_record)
        self.assertTrue(any("delete-then-reseed" in e for e in errs))

    def test_push_modification_needs_record(self):
        self._write_keys()
        base = self._commit()
        self._write_keys(mpub="untrusted comment: x\nRWRNEWKEYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n")
        self._commit("rotate without record")
        errs = cv.check_keys(self.repo, "push", base, self._ok_record)
        self.assertTrue(any("without an old-key-signed rotation record" in e for e in errs))

    def test_push_modification_with_valid_record(self):
        self._write_keys()
        base = self._commit()
        (self.repo / "keys" / "rotations").mkdir()
        self._write_keys(mpub="untrusted comment: x\nRWRNEWKEYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n")
        (self.repo / "keys" / "rotations" / "1-manifest.json").write_text("{}", encoding="utf-8")
        self._commit("rotate with record")
        errs = cv.check_keys(self.repo, "push", base, self._ok_record)
        self.assertEqual(errs, [])

    def test_zero_sha_push_rejected(self):
        self._write_keys()
        self._commit()
        errs = cv.check_keys(self.repo, "push", cv.ZERO_SHA, self._ok_record)
        self.assertTrue(any("zero/empty predecessor" in e for e in errs))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
