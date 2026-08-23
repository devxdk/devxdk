"""The history gate: revision monotonicity across git history.

Every case here is driven through a REAL throwaway git repo, because the thing
under test is history and a fixture that fakes it would prove nothing.
"""

import importlib.util
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

_GATE = pathlib.Path(__file__).resolve().parents[2] / "ci" / "check_revision_history.py"
_spec = importlib.util.spec_from_file_location("check_revision_history", _GATE)
crh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crh)


def _manifest(name="redis", revision=None, releases=None, display_name="Redis"):
    doc = {"name": name, "display_name": display_name, "kind": "service"}
    if revision is not None:
        doc["revision"] = revision
    doc["releases"] = releases if releases is not None else []
    return doc


def _release(ver="8.8.0"):
    return [{"version": ver, "channel": "stable", "released_at": "2026-01-01",
             "platforms": {"windows/amd64": {"url": "https://example.com/x.zip",
                                             "sha256": "a" * 64, "size_bytes": 10}}}]


class GitFixture(unittest.TestCase):
    """A throwaway repo with a base commit and a head commit."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "t")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "core.autocrlf", "false")

    def _git(self, *args):
        proc = subprocess.run(["git", *args], cwd=self.dir, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"git {' '.join(args)}: {proc.stderr}")
        return proc.stdout

    def write(self, relpath, doc_or_bytes):
        path = self.dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(doc_or_bytes, (bytes, bytearray)):
            path.write_bytes(doc_or_bytes)
        else:
            path.write_bytes((json.dumps(doc_or_bytes, indent=2) + "\n").encode("utf-8"))

    def remove(self, relpath):
        (self.dir / relpath).unlink()

    def commit(self, message="c"):
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message, "--allow-empty")
        return self._git("rev-parse", "HEAD").strip()

    def check(self, base, head="HEAD"):
        return crh.check(base, head, cwd=str(self.dir))


class TestHeadInvariant(GitFixture):
    def test_a_half_backfilled_tree_fails(self):
        # The partial case a bootstrap-only check would wave through forever.
        self.write("redis.json", _manifest("redis"))
        self.write("nginx.json", _manifest("nginx", display_name="nginx"))
        base = self.commit()
        self.write("redis.json", _manifest("redis", revision=1))
        # nginx.json deliberately left un-backfilled.
        self.commit()
        errors = self.check(base)
        self.assertTrue(any("nginx.json" in e and "revision" in e for e in errors), errors)

    def test_non_integer_revision_fails(self):
        self.write("redis.json", _manifest("redis", revision=1))
        base = self.commit()
        self.write("redis.json", _manifest("redis", revision="2"))
        self.commit()
        self.assertTrue(any("must be an integer" in e for e in self.check(base)))

    def test_bool_revision_fails(self):
        # isinstance(True, int) is True in Python, so a bare integer check would
        # read `true` as revision 1.
        self.write("redis.json", _manifest("redis", revision=1))
        base = self.commit()
        self.write("redis.json", _manifest("redis", revision=True))
        self.commit()
        self.assertTrue(any("must be an integer" in e for e in self.check(base)))


class TestBootstrap(GitFixture):
    def _seed_unrevisioned(self):
        self.write("redis.json", _manifest("redis", releases=_release()))
        self.write("nginx.json", _manifest("nginx", display_name="nginx"))
        self.write("app/update.json", {"schema": 1, "channels": {}})
        return self.commit()

    def _canonical(self, doc):
        # What schema.dump_str produces — the bootstrap's own serializer.
        return (json.dumps(doc, indent=2, allow_nan=False) + "\n").encode("utf-8")

    def test_all_at_once_passes(self):
        base = self._seed_unrevisioned()
        self.write("redis.json", self._canonical(
            _manifest("redis", revision=1, releases=_release())))
        self.write("nginx.json", self._canonical(
            _manifest("nginx", revision=1, display_name="nginx")))
        self.write("app/update.json", {"schema": 1, "revision": 1, "channels": {}})
        self.commit()
        self.assertEqual(self.check(base), [])

    def test_missing_one_file_fails(self):
        base = self._seed_unrevisioned()
        self.write("redis.json", self._canonical(
            _manifest("redis", revision=1, releases=_release())))
        self.write("app/update.json", {"schema": 1, "revision": 1, "channels": {}})
        self.commit()
        self.assertTrue(any("nginx.json" in e for e in self.check(base)))

    def test_a_smuggled_release_edit_fails(self):
        # The deep-equality half is the ONLY thing that can see this, and
        # without a test it is the rule most likely to become a no-op.
        base = self._seed_unrevisioned()
        self.write("redis.json", self._canonical(
            _manifest("redis", revision=1, releases=_release("9.9.9"))))
        self.write("nginx.json", self._canonical(
            _manifest("nginx", revision=1, display_name="nginx")))
        self.write("app/update.json", {"schema": 1, "revision": 1, "channels": {}})
        self.commit()
        self.assertTrue(any("NOTHING else" in e for e in self.check(base)))

    def test_revision_other_than_one_fails(self):
        base = self._seed_unrevisioned()
        self.write("redis.json", self._canonical(
            _manifest("redis", revision=2, releases=_release())))
        self.write("nginx.json", self._canonical(
            _manifest("nginx", revision=1, display_name="nginx")))
        self.write("app/update.json", {"schema": 1, "revision": 1, "channels": {}})
        self.commit()
        self.assertTrue(any("exactly 1" in e for e in self.check(base)))

    def test_hand_edited_backfill_fails_on_the_serializer_belt(self):
        base = self._seed_unrevisioned()
        # Correct content, non-canonical bytes (4-space indent).
        doc = _manifest("redis", revision=1, releases=_release())
        self.write("redis.json", (json.dumps(doc, indent=4) + "\n").encode("utf-8"))
        self.write("nginx.json", self._canonical(
            _manifest("nginx", revision=1, display_name="nginx")))
        self.write("app/update.json", {"schema": 1, "revision": 1, "channels": {}})
        self.commit()
        self.assertTrue(any("dump_str" in e for e in self.check(base)))

    def test_update_json_is_exempt_from_the_serializer_belt(self):
        # Go's updatejson.Encode produces those bytes; no Python serializer
        # reproduces them, so the deep-equality check stands alone there.
        base = self._seed_unrevisioned()
        self.write("redis.json", self._canonical(
            _manifest("redis", revision=1, releases=_release())))
        self.write("nginx.json", self._canonical(
            _manifest("nginx", revision=1, display_name="nginx")))
        self.write("app/update.json",
                   b'{\n\t"schema": 1,\n\t"revision": 1,\n\t"channels": {}\n}\n')
        self.commit()
        self.assertEqual(self.check(base), [])


class TestMonotonic(GitFixture):
    def _seed(self, revision=5):
        self.write("redis.json", _manifest("redis", revision=revision, releases=_release()))
        return self.commit()

    def test_identical_bytes_pass(self):
        base = self._seed()
        self.commit("empty")
        self.assertEqual(self.check(base), [])

    def test_changed_bytes_with_a_greater_revision_pass(self):
        base = self._seed()
        self.write("redis.json", _manifest("redis", revision=6, releases=_release("9.0.0")))
        self.commit()
        self.assertEqual(self.check(base), [])

    def test_a_synthetic_revert_fails(self):
        # The case the generator rule alone cannot see: an older manifest
        # restored verbatim, older revision and all.
        old = _manifest("redis", revision=5, releases=_release("8.8.0"))
        self.write("redis.json", old)
        self.commit()
        self.write("redis.json", _manifest("redis", revision=6, releases=_release("9.0.0")))
        base = self.commit()
        self.write("redis.json", old)  # the revert
        self.commit()
        errors = self.check(base)
        self.assertTrue(any("not greater than" in e for e in errors), errors)

    def test_equal_revision_with_changed_bytes_fails(self):
        base = self._seed()
        self.write("redis.json", _manifest("redis", revision=5, releases=_release("9.0.0")))
        self.commit()
        self.assertTrue(any("not greater than" in e for e in self.check(base)))

    def test_digit_width_bump_needs_no_token_surgery(self):
        # 9 -> 10 moves every following byte. A projection-based gate has to do
        # token substitution for this; byte-first does not.
        base = self._seed(revision=9)
        self.write("redis.json", _manifest("redis", revision=10, releases=_release()))
        self.commit()
        self.assertEqual(self.check(base), [])

    def test_new_file_must_start_at_one(self):
        base = self._seed()
        self.write("valkey.json", _manifest("valkey", revision=3, display_name="Valkey"))
        self.commit()
        self.assertTrue(any("must start at revision 1" in e for e in self.check(base)))

    def test_update_json_uses_the_same_rule(self):
        self.write("redis.json", _manifest("redis", revision=5, releases=_release()))
        self.write("app/update.json", {"schema": 1, "revision": 4, "channels": {"stable": {}}})
        base = self.commit()
        self.write("app/update.json", {"schema": 1, "revision": 4, "channels": {"stable": {"x": 1}}})
        self.commit()
        self.assertTrue(any("update.json" in e and "not greater than" in e
                            for e in self.check(base)))


class TestFileSet(GitFixture):
    def test_set_is_the_predicate_plus_update_json(self):
        self.write("redis.json", _manifest("redis", revision=1))
        self.write("config-ish.json", {"not": "a manifest"})  # no kind/releases
        self.write("app/update.json", {"schema": 1, "revision": 1})
        self.commit()
        names = crh.file_set("HEAD", "HEAD", cwd=str(self.dir))
        self.assertEqual(names, ["app/update.json", "redis.json"])

    def test_deleted_manifest_fails_and_names_the_retirement_path(self):
        self.write("redis.json", _manifest("redis", revision=5, releases=_release()))
        self.write("nginx.json", _manifest("nginx", revision=1, display_name="nginx"))
        base = self.commit()
        self.remove("redis.json")
        self.commit()
        errors = self.check(base)
        self.assertTrue(any("redis.json" in e for e in errors), errors)
        self.assertTrue(any("EMPTY releases" in e for e in errors), errors)

    def test_delete_then_readd_returns_below_the_mark(self):
        self.write("redis.json", _manifest("redis", revision=5, releases=_release()))
        base = self.commit()
        self.remove("redis.json")
        self.commit("delete")
        self.write("redis.json", _manifest("redis", revision=1, releases=_release()))
        self.commit("re-add")
        # Against the pre-delete base the re-add is a rollback from 5 to 1.
        self.assertTrue(any("not greater than" in e for e in self.check(base)))

    def test_mangled_out_of_the_predicate_fails(self):
        self.write("redis.json", _manifest("redis", revision=5, releases=_release()))
        base = self.commit()
        self.write("redis.json", {"name": "redis", "revision": 6})  # no kind/releases
        self.commit()
        errors = self.check(base)
        self.assertTrue(any("no longer a component manifest" in e for e in errors), errors)


class TestStrictness(GitFixture):
    def test_case_fold_collision_is_refused_not_ordered(self):
        # Python would order this at 6 while Go records 100, after which the
        # genuine revision 7 is permanently refused.
        self.write("redis.json", _manifest("redis", revision=5, releases=_release()))
        base = self.commit()
        self.write("redis.json", b'{"name":"redis","kind":"service","revision":6,'
                                 b'"Revision":100,"releases":[]}\n')
        self.commit()
        errors = self.check(base)
        self.assertTrue(any("case-fold" in e or "no longer a component manifest" in e
                            for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
