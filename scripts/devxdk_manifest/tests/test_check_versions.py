"""Tests for the version-currency gate (scripts/ci/check_versions.py).

This file lives HERE, and nowhere else, because manifest CI discovers tests with
``python -m unittest discover -s devxdk_manifest/tests -p 'test_*.py'`` from
``working-directory: scripts``. A copy placed anywhere else would silently never
run, and "runs that repo's own full CI" would be satisfied while executing
nothing.

It is also what pins the scanner's supported floor: this job runs under Python
3.12, so a 3.13/3.14-only idiom in check_versions.py fails here rather than
shipping quietly into the app repo's byte-identical vendored copy.
"""

import datetime
import json
import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS / "ci"))

import check_versions as cv  # noqa: E402

REPO_ROOT = SCRIPTS.parent


def write(root, rel, text):
    p = pathlib.Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


MIN_SCAN = '''schema = 1
[scan]
include_globs = [".github/workflows/*.yml", "**/*.sh", "**/go.mod"]
exclude_dirs = [".git"]
'''


class ScannerCase(unittest.TestCase):
    """Base: build a throwaway repo, run one consistency pass over it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def check(self, inventory_body, today="2026-08-22"):
        inv_path = write(self.root, ".github/versions-inventory.toml", inventory_body)
        inv = cv.load_inventory(inv_path)
        cv.validate_inventory(inv, inv_path)
        return cv.run_consistency(self.root, inv, inv_path,
                                  datetime.date.fromisoformat(today))


# ---------------------------------------------------------------------------
# Forward direction
# ---------------------------------------------------------------------------

class TestForward(ScannerCase):
    def test_moved_pin_is_caught(self):
        write(self.root, ".github/workflows/ci.yml",
              'jobs:\n  a:\n    steps:\n      - uses: actions/checkout@' + "a" * 40 + '\n')
        errs = self.check(MIN_SCAN + '''
[[rows]]
id = "checkout"
why = "test"
value = "%s"
[[rows.checks]]
kind = "scan"
select_kind = "uses"
select_identity = "actions/checkout"
count = 1
''' % ("b" * 40))
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("PARTIAL bump", errs[0])

    def test_sites_disagreeing_is_caught(self):
        # The property Dependabot's grouped PRs break: one site moves, the rest
        # do not. What the row catches is the PARTIAL bump, not the missing one.
        write(self.root, ".github/workflows/a.yml", 'go-version: "1.27.0"\n')
        write(self.root, ".github/workflows/b.yml", 'go-version: "1.26.6"\n')
        errs = self.check(MIN_SCAN + '''
[[rows]]
id = "go"
why = "test"
value = "1.27.0"
[[rows.checks]]
kind = "scan"
select_kind = "go-version"
count = 2
''')
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("1.26.6", errs[0])

    def test_new_site_of_an_inventoried_pin_fails_the_count(self):
        write(self.root, ".github/workflows/a.yml", 'go-version: "1.27.0"\n')
        write(self.root, ".github/workflows/b.yml", 'go-version: "1.27.0"\n')
        errs = self.check(MIN_SCAN + '''
[[rows]]
id = "go"
why = "test"
value = "1.27.0"
[[rows.checks]]
kind = "scan"
select_kind = "go-version"
count = 1
''')
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("expected 1 site", errs[0])

    def test_removed_pin_is_caught(self):
        write(self.root, ".github/workflows/a.yml", "jobs: {}\n")
        errs = self.check(MIN_SCAN + '''
[[rows]]
id = "go"
why = "test"
value = "1.27.0"
[[rows.checks]]
kind = "scan"
select_kind = "go-version"
count = 1
''')
        self.assertEqual(len(errs), 1, errs)

    def test_file_check_with_a_regex(self):
        write(self.root, "keys/x.sha256",
              "# url: https://example.invalid/minisign/releases/download/0.12/x.tar.gz\n")
        errs = self.check(MIN_SCAN + '''
[[rows]]
id = "minisign"
why = "test"
value = "0.13"
[[rows.checks]]
kind = "file"
file = "keys/x.sha256"
regex = "download/([0-9.]+)/"
count = 1
''')
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("0.12", errs[0])


# ---------------------------------------------------------------------------
# Reverse direction - the half that catches a NEWLY ADDED pin
# ---------------------------------------------------------------------------

class TestReverse(ScannerCase):
    def test_uninventoried_pin_fails(self):
        write(self.root, ".github/workflows/a.yml",
              "jobs:\n  a:\n    steps:\n      - uses: some/action@" + "c" * 40 + "\n")
        errs = self.check(MIN_SCAN + '''
[[rows]]
id = "unrelated"
why = "test"
value = "1.27.0"
[[rows.checks]]
kind = "scan"
select_kind = "go-version"
count = 0
''')
        self.assertTrue(any("UNINVENTORIED" in e for e in errs), errs)

    def test_uses_is_scanned_by_key_not_by_the_pinned_spelling(self):
        # THE INVERSION TEST. A regex like `uses: ...@<40-hex>` only recognizes
        # actions that are ALREADY correct, so the mutable tag the repo forbids
        # would match nothing and sail through. Scanning by KEY is what makes the
        # one case the gate exists to catch visible.
        write(self.root, ".github/workflows/a.yml",
              "jobs:\n  a:\n    steps:\n      - uses: vendor/action@v1\n")
        errs = self.check(MIN_SCAN + '''
[[rows]]
id = "unrelated"
why = "test"
value = "x"
[[rows.checks]]
kind = "scan"
select_kind = "go-version"
count = 0
''')
        self.assertTrue(any("40-hex" in e for e in errs), errs)

    def test_local_composite_action_is_allowlisted(self):
        write(self.root, ".github/workflows/a.yml",
              "jobs:\n  a:\n    uses: ./.github/workflows/leg.yml\n")
        errs = self.check(MIN_SCAN + '''
[[allowlist]]
select_kind = "uses-local"

[[rows]]
id = "unrelated"
why = "test"
value = "x"
[[rows.checks]]
kind = "scan"
select_kind = "go-version"
count = 0
''')
        self.assertEqual(errs, [])

    def test_floating_runner_label_allowlisted_dated_one_is_not(self):
        write(self.root, ".github/workflows/a.yml",
              "jobs:\n  a:\n    runs-on: ubuntu-latest\n  b:\n    runs-on: ubuntu-22.04\n")
        errs = self.check(MIN_SCAN + '''
[[allowlist]]
select_kind = "runs-on"
select_values = ["ubuntu-latest"]

[[rows]]
id = "unrelated"
why = "test"
value = "x"
[[rows.checks]]
kind = "scan"
select_kind = "go-version"
count = 0
''')
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("ubuntu-22.04", errs[0])

    def test_indirect_runs_on_is_not_truncated(self):
        # A character-class capture stops at the first space and would turn
        # `${{ inputs.runner }}` into the literal `${{`, classifying an ordinary
        # indirection as an unrecognised pin.
        write(self.root, ".github/workflows/a.yml", "jobs:\n  a:\n    runs-on: ${{ inputs.runner }}\n")
        errors = []
        hits = cv.scan_tree(self.root, {"include_globs": [".github/workflows/*.yml"]}, errors)
        indirect = [h for h in hits if h.kind == "runs-on-indirect"]
        self.assertEqual(len(indirect), 1, hits)
        self.assertEqual(indirect[0].value, "${{ inputs.runner }}")


class TestYamlFlowMappings(ScannerCase):
    """A pin written in a YAML FLOW mapping is still a pin.

    This is not hypothetical: the app repo's release.yml writes
    `with: { node-version: "24" }` and `with: { go-version: "${{ env.GO_VERSION }}" }`
    on one line, three times each. A line-anchored `^\\s*node-version:` saw NONE
    of them, so the gate was blind to three real node-version sites in a repo
    whose inventory claims seven - found only by pointing the scanner at the
    second repo.
    """

    def _scan(self, body):
        write(self.root, ".github/workflows/a.yml", body)
        errors = []
        hits = cv.scan_tree(self.root, {"include_globs": [".github/workflows/*.yml"]}, errors)
        return hits, errors

    def test_flow_mapping_node_version_is_seen(self):
        hits, errors = self._scan('      - with: { node-version: "24" }\n')
        self.assertEqual(errors, [])
        self.assertEqual([(h.kind, h.value) for h in hits], [("node-version", "24")])

    def test_flow_mapping_go_version_indirection_survives_whole(self):
        hits, _ = self._scan('      - with: { go-version: "${{ env.GO_VERSION }}" }\n')
        self.assertEqual([(h.kind, h.value) for h in hits],
                         [("go-version", "${{ env.GO_VERSION }}")])

    def test_two_pins_in_one_flow_mapping(self):
        hits, _ = self._scan('      - with: { go-version: "1.27.0", node-version: "24" }\n')
        self.assertEqual(sorted((h.kind, h.value) for h in hits),
                         [("go-version", "1.27.0"), ("node-version", "24")])

    def test_unquoted_flow_scalar_stops_at_the_brace(self):
        hits, _ = self._scan("      - with: { node-version: 24 }\n")
        self.assertEqual([(h.kind, h.value) for h in hits], [("node-version", "24")])

    def test_block_form_still_works(self):
        hits, _ = self._scan('        with:\n          node-version: "22.22.2"\n')
        self.assertEqual([(h.kind, h.value) for h in hits], [("node-version", "22.22.2")])

    def test_a_longer_key_ending_in_the_pin_key_is_not_matched(self):
        # `cache-dependency-path:` ends in nothing relevant, but `...-version:`
        # keys are a real hazard: only a key that OPENS the line or follows
        # `{`/`,` counts.
        hits, _ = self._scan("          cache-dependency-path: frontend/package-lock.json\n"
                             "          my-go-version: 9.9.9\n")
        self.assertEqual(hits, [])

    def test_uses_in_flow_form_is_still_checked(self):
        _, errors = self._scan("      - { uses: vendor/action@v1 }\n")
        self.assertTrue(any("40-hex" in e for e in errors), errors)


# ---------------------------------------------------------------------------
# Go tool refs - the four forms a literal-version regex sees none of
# ---------------------------------------------------------------------------

class TestGoToolRefs(ScannerCase):
    def _scan(self, body):
        write(self.root, "run.sh", body)
        errors = []
        hits = cv.scan_tree(self.root, {"include_globs": ["**/*.sh"]}, errors)
        return hits, errors

    def test_floating_latest_is_a_hard_failure(self):
        _, errors = self._scan("go install github.com/go-task/task/v3/cmd/task@latest\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("floating ref", errors[0])

    def test_short_hash_is_floating(self):
        _, errors = self._scan("go install example.com/x/y@abc1234\n")
        self.assertEqual(len(errors), 1, errors)

    def test_branch_name_is_floating(self):
        _, errors = self._scan("go install example.com/x/y@main\n")
        self.assertEqual(len(errors), 1, errors)

    def test_full_40_hex_is_accepted(self):
        # Immutable, so not floating. Rejecting it while ci.yml REQUIRES 40-hex
        # for every `uses:` would have the gate forbid the exact pinning
        # discipline the repo mandates one line away.
        hits, errors = self._scan("go install example.com/x/y@" + "f" * 40 + "\n")
        self.assertEqual(errors, [])
        self.assertEqual([h.kind for h in hits], ["go-tool"])

    def test_go_run_is_scanned_too(self):
        hits, errors = self._scan("go run github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.13.1 run\n")
        self.assertEqual(errors, [])
        self.assertEqual(hits[0].identity,
                         "github.com/golangci/golangci-lint/v2/cmd/golangci-lint")
        self.assertEqual(hits[0].value, "v2.13.1")

    def test_shell_variable_ref_is_indirected_not_floating(self):
        hits, errors = self._scan('go install example.com/x/y@${GOMOD_VERSION}\n')
        self.assertEqual(errors, [])
        self.assertEqual([h.kind for h in hits], ["go-tool-indirect"])

    def test_github_expression_ref_is_indirected(self):
        write(self.root, ".github/workflows/a.yml",
              "    - run: go install example.com/x/y@${{ env.V }}\n")
        errors = []
        hits = cv.scan_tree(self.root, {"include_globs": [".github/workflows/*.yml"]}, errors)
        self.assertEqual(errors, [])
        self.assertEqual([h.kind for h in hits], ["go-tool-indirect"])


# ---------------------------------------------------------------------------
# The structured [pins.*] walk
# ---------------------------------------------------------------------------

class TestPinsWalk(ScannerCase):
    def test_reaches_a_nested_table(self):
        # A regex over TOML would miss [pins.php_redis.dll."8.5"] entirely, which
        # is the one thing this walk exists for.
        write(self.root, "config/tracked-versions.toml", '''schema = 1
[pins.php_redis]
version = "6.3.0"
[pins.php_redis.dll."8.5"]
file = "php_redis-6.3.0-8.5-nts-vs17-x64.zip"
sha256 = "%s"
''' % ("d" * 64))
        hits = cv.scan_pins(self.root, "config/tracked-versions.toml")
        idents = sorted(h.identity for h in hits)
        self.assertEqual(idents, [
            "pins.php_redis.dll.8.5.file",
            "pins.php_redis.dll.8.5.sha256",
            "pins.php_redis.version",
        ])

    def test_identity_prefix_covers_the_whole_table(self):
        hit = cv.Hit("toml-pin", "pins.php_redis.dll.8.5.file", "x", "c.toml")
        self.assertTrue(cv.selector_matches(
            {"select_kind": "toml-pin", "select_identity": "pins.php_redis"}, hit))
        self.assertFalse(cv.selector_matches(
            {"select_kind": "toml-pin", "select_identity": "pins.php"}, hit))

    def test_untracked_field_is_not_a_pin(self):
        write(self.root, "config/tracked-versions.toml",
              'schema = 1\n[pins.x]\nversion = "1"\nnote = "not a pin"\n')
        hits = cv.scan_pins(self.root, "config/tracked-versions.toml")
        self.assertEqual([h.identity for h in hits], ["pins.x.version"])


# ---------------------------------------------------------------------------
# The exception model
# ---------------------------------------------------------------------------

class TestExceptionModel(ScannerCase):
    BASE = MIN_SCAN + '''
[[rows]]
id = "r"
why = "test"
value = "1"
%s
[[rows.checks]]
kind = "scan"
select_kind = "go-version"
count = 0
'''

    def _validate(self, extra):
        inv_path = write(self.root, ".github/versions-inventory.toml", self.BASE % extra)
        inv = cv.load_inventory(inv_path)
        cv.validate_inventory(inv, inv_path)

    def test_both_condition_and_date_is_rejected(self):
        with self.assertRaises(cv.CheckError) as ctx:
            self._validate('exception = "hold"\ncondition = "c"\nexpires_on = "2026-11-30"')
        self.assertIn("never both", str(ctx.exception))

    def test_neither_is_rejected(self):
        with self.assertRaises(cv.CheckError) as ctx:
            self._validate('exception = "hold"')
        self.assertIn("must carry", str(ctx.exception))

    def test_a_hold_may_not_carry_a_date(self):
        with self.assertRaises(cv.CheckError):
            self._validate('exception = "hold"\nexpires_on = "2026-11-30"')

    def test_a_time_bounded_row_may_not_carry_a_condition(self):
        with self.assertRaises(cv.CheckError):
            self._validate('exception = "time-bounded"\ncondition = "c"')

    def test_condition_without_declaring_an_exception_is_rejected(self):
        with self.assertRaises(cv.CheckError):
            self._validate('condition = "c"')

    def test_expiry_fails_the_offline_job_past_the_date(self):
        write(self.root, ".github/workflows/a.yml", "jobs: {}\n")
        body = self.BASE % 'exception = "time-bounded"\nexpires_on = "2026-11-30"'
        errs = self.check(body, today="2026-12-01")
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("EXPIRED", errs[0])

    def test_expiry_is_silent_before_the_date(self):
        write(self.root, ".github/workflows/a.yml", "jobs: {}\n")
        body = self.BASE % 'exception = "time-bounded"\nexpires_on = "2026-11-30"'
        self.assertEqual(self.check(body, today="2026-11-30"), [])


# ---------------------------------------------------------------------------
# Version selection - @v/list, retractions, ordering
# ---------------------------------------------------------------------------

class TestVersionSelection(unittest.TestCase):
    def test_release_sorts_above_its_prerelease(self):
        self.assertGreater(cv.version_key("v1.2.3"), cv.version_key("v1.2.3-rc1"))

    def test_retraction_ranges_are_parsed(self):
        mod = """module example.com/x

go 1.22

retract (
    v1.0.1 // bad
    [v1.1.0, v1.1.5]
)

retract v0.9.0
"""
        got = cv.parse_retractions(mod)
        self.assertIn(("v1.0.1", "v1.0.1"), got)
        self.assertIn(("v1.1.0", "v1.1.5"), got)
        self.assertIn(("v0.9.0", "v0.9.0"), got)

    def test_go_latest_reads_retractions_from_the_highest_version(self):
        # Getting this backwards makes the exclusion a no-op: a retracted
        # version's OWN go.mod normally says nothing about it, because an author
        # retracts by publishing a NEW, higher version carrying the directive.
        calls = []

        def fake_get(url, headers=None):
            calls.append(url)
            if url.endswith("/@v/list"):
                return b"v1.0.0\nv1.1.0\nv1.2.0\nv1.3.0-rc1\n"
            if url.endswith("/@v/v1.3.0-rc1.mod"):
                return b"module example.com/x\n\nretract v1.2.0\nretract v1.1.0\n"
            raise AssertionError("unexpected " + url)

        orig = cv._http_get
        cv._http_get = fake_get
        try:
            self.assertEqual(cv.go_latest("example.com/x"), "v1.0.0")
        finally:
            cv._http_get = orig
        # Exactly one .mod fetch, and it is the HIGHEST LISTED version's -
        # prereleases included. A prerelease is excluded from SELECTION but not
        # from the retraction lookup: the module reference requires the version
        # carrying a retract directive to be higher than every other release or
        # pre-release, so the top of the list is where the directives live.
        self.assertEqual([c for c in calls if c.endswith(".mod")],
                         ["https://proxy.golang.org/example.com/x/@v/v1.3.0-rc1.mod"])

    def test_module_path_case_escaping(self):
        self.assertEqual(cv._escape_module("github.com/CycloneDX/cyclonedx-gomod"),
                         "github.com/!cyclone!d!x/cyclonedx-gomod")


class TestCurrencyFailsClosed(unittest.TestCase):
    def test_malformed_response_fails_rather_than_passing(self):
        orig = cv._http_get
        cv._http_get = lambda url, headers=None: b"<html>not json</html>"
        try:
            with self.assertRaises(cv.CheckError) as ctx:
                cv._json_get("https://example.invalid/x")
        finally:
            cv._http_get = orig
        self.assertIn("malformed", str(ctx.exception))

    def test_unreachable_api_raises_and_never_returns_current(self):
        orig = cv._http_get

        def boom(url, headers=None):
            raise cv.CheckError("GET %s: unreachable" % url)

        cv._http_get = boom
        try:
            with self.assertRaises(cv.CheckError):
                cv.npm_latest("anything")
        finally:
            cv._http_get = orig


# ---------------------------------------------------------------------------
# This repo's own inventory
# ---------------------------------------------------------------------------

class TestThisRepoInventory(unittest.TestCase):
    """The manifest repo's real inventory, checked by the unit-test job too.

    Belt and braces with versions-consistency.yml: this asserts the file is
    structurally valid and consistent with the tree in the job that already
    exists, so a broken inventory is caught even before the new check is a
    required one.
    """

    def setUp(self):
        self.inv_path = REPO_ROOT / ".github" / "versions-inventory.toml"
        self.inv = cv.load_inventory(self.inv_path)

    def test_inventory_is_structurally_valid(self):
        cv.validate_inventory(self.inv, self.inv_path)

    def test_inventory_is_consistent_with_the_tree(self):
        errs = cv.run_consistency(REPO_ROOT, self.inv, self.inv_path,
                                  datetime.date.today())
        self.assertEqual(errs, [], "\n".join(errs))

    def test_exactly_one_time_bounded_exception_and_no_holds(self):
        # The count is checkable, so check it: this repo carries the Python
        # verification-cost exception and nothing else. A hold here would mean an
        # upstream constraint nobody has written down.
        kinds = [r.get("exception") for r in self.inv["rows"] if r.get("exception")]
        self.assertEqual(kinds, ["time-bounded"], kinds)

    def test_the_scanner_is_ascii(self):
        # Its failure messages are echoed to a console; a stray em-dash becomes
        # mojibake on a cp1252 host, which is a class of defect this codebase has
        # shipped before.
        src = (SCRIPTS / "ci" / "check_versions.py").read_bytes()
        try:
            src.decode("ascii")
        except UnicodeDecodeError as e:  # pragma: no cover - diagnostic
            self.fail("check_versions.py is not ASCII at byte %d" % e.start)


if __name__ == "__main__":
    unittest.main()
