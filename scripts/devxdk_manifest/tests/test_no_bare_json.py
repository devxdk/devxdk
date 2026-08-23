"""Every JSON parse in the manifest repo's non-test Python goes through
devxdk_manifest.strictjson — enforced by a machine, not by a list someone has to
keep complete.

AN AST WALK, NOT A REGEX, and the difference is the whole point: `json` exposes
exactly three public decode entry points (load, loads, JSONDecoder), but
enumerating the API is not enumerating the SYNTAX that reaches it. `import json
as j` then `j.loads`, `from json.decoder import JSONDecoder`, and
`json.decoder.JSONDecoder` all evade any literal pattern set.

STATE THE LIMIT rather than claiming completeness: this covers static
reachability in .py files. Dynamic access (getattr(json, "lo" + "ads"),
importlib) is out of scope in a repo whose own CI executes the code — and so are
the 19 shell-embedded parses, which are enumerated in the plan as a KNOWN set
and are safe because they parse LEG_ITEMS this repo's own planner just produced.
The one exception there, is_manifest() in scrape-sign-push.sh, decides which
files get SIGNED and is routed through strictjson explicitly.
"""

import ast
import pathlib
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]

# Decode entry points. raw_decode and decode are reachable only through a
# JSONDecoder instance, so blocking the constructor covers them.
FORBIDDEN = {"load", "loads", "JSONDecoder"}
# Named so the guard does not fail on correct code the day it lands.
ALLOWED = {"dump", "dumps", "JSONEncoder", "JSONDecodeError"}

# THE ONE NAMED EXCLUSION, and it is a rule rather than a favour. §1 makes this
# file CANONICAL here and vendors it byte-identically into the app repo, where
# it is executed by that repo's own versions-* workflows and where no
# devxdk_manifest package exists — so forcing a strictjson import into it would
# make the vendored copy ImportError on every app-repo run. It is sound on the
# same test applied everywhere else: what it decodes is registry and GitHub API
# responses, never a signed document, and it has no cross-language reader, so
# not one of the six divergence classes reaches it.
EXCLUDED = ("ci/check_versions.py",)

# The parser itself is where the real json calls live.
SELF = "devxdk_manifest/strictjson.py"


def python_files():
    """Every non-test .py under scripts/, recursively.

    THE WALK ROOT IS scripts/, NOT the package directory. Ten of the sixteen
    decode sites sit at scripts/ top level — apply_pending, apply_revocations,
    ci_verify, finalize_builds, plan_builds and publish_legs (five of them) —
    so a package-rooted guard would leave the pending, revocation, finalize and
    publish paths entirely unscanned while reporting itself exhaustive.
    """
    out = []
    for path in sorted(SCRIPTS.rglob("*.py")):
        rel = path.relative_to(SCRIPTS).as_posix()
        if "/tests/" in f"/{rel}" or rel.startswith("tests/"):
            continue
        if "__pycache__" in rel:
            continue
        if rel == SELF or rel in EXCLUDED:
            continue
        out.append((rel, path))
    return out


def bare_json_uses(source, label="<src>"):
    """Every statically reachable bare-json decode in `source`."""
    tree = ast.parse(source, filename=label)
    aliases = {}  # local name -> dotted module path

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "json" or alias.name.startswith("json."):
                    # `import json.decoder` binds the ROOT name, not the dotted one.
                    local = alias.asname or alias.name.split(".")[0]
                    aliases[local] = alias.name if alias.asname else alias.name.split(".")[0]

    findings = []

    def dotted(node):
        """The alias-resolved dotted name of an attribute chain, or None."""
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        root = aliases.get(node.id)
        if root is None:
            return None
        parts.append(root)
        return ".".join(reversed(parts))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in ("json", "json.decoder"):
                for alias in node.names:
                    if alias.name in FORBIDDEN:
                        findings.append((node.lineno, f"from {node.module} import {alias.name}"))
        elif isinstance(node, ast.Attribute):
            name = dotted(node)
            if name and name.split(".")[0] == "json" and node.attr in FORBIDDEN:
                findings.append((node.lineno, name))
    return sorted(set(findings))


class TestGuardItself(unittest.TestCase):
    """A regex implementation passes the first case and fails the next four."""

    def test_catches_the_plain_form(self):
        self.assertTrue(bare_json_uses("import json\njson.loads(x)\n"))

    def test_catches_an_alias(self):
        self.assertTrue(bare_json_uses("import json as j\nj.loads(x)\n"))

    def test_catches_from_json_import(self):
        self.assertTrue(bare_json_uses("from json import loads\nloads(x)\n"))

    def test_catches_from_json_decoder_import(self):
        self.assertTrue(bare_json_uses("from json.decoder import JSONDecoder\nJSONDecoder()\n"))

    def test_catches_the_dotted_submodule_form(self):
        self.assertTrue(bare_json_uses("import json.decoder\njson.decoder.JSONDecoder()\n"))

    def test_catches_json_load(self):
        self.assertTrue(bare_json_uses("import json\njson.load(fh)\n"))

    def test_allows_the_encoders_and_the_error(self):
        src = ("import json\n"
               "json.dumps(x)\n"
               "json.dump(x, fh)\n"
               "json.JSONEncoder()\n"
               "try:\n    pass\nexcept json.JSONDecodeError:\n    pass\n")
        self.assertEqual(bare_json_uses(src), [])

    def test_ignores_an_unrelated_loads(self):
        self.assertEqual(bare_json_uses("import yaml\nyaml.loads(x)\n"), [])
        self.assertEqual(bare_json_uses("from devxdk_manifest import strictjson\nstrictjson.loads(x)\n"), [])


class TestWalkRoot(unittest.TestCase):
    """A guard with a correct rule and a narrow root is the failure mode this
    test most needs to catch, and it is invisible from the rule alone."""

    def setUp(self):
        self.rels = [rel for rel, _p in python_files()]

    def test_not_empty(self):
        self.assertGreater(len(self.rels), 10, self.rels)

    def test_includes_a_top_level_script(self):
        # publish_legs.py is the sharpest: it carries the JSONDecoder site that
        # neither `json.load(` nor `json.loads(` would have matched.
        self.assertIn("publish_legs.py", self.rels)
        self.assertIn("apply_pending.py", self.rels)

    def test_includes_a_package_module(self):
        self.assertIn("devxdk_manifest/merge.py", self.rels)

    def test_excludes_tests_and_the_parser(self):
        self.assertNotIn(SELF, self.rels)
        self.assertFalse([r for r in self.rels if "/tests/" in f"/{r}"])


class TestExclusion(unittest.TestCase):
    def test_exactly_one_and_it_still_exists(self):
        self.assertEqual(len(EXCLUDED), 1, "the exclusion list must not grow by habit")
        for rel in EXCLUDED:
            self.assertTrue((SCRIPTS / rel).is_file(),
                            f"{rel} is excluded by name but is not there — moving or deleting "
                            "it must fail here rather than silently widening the hole")


class TestRepository(unittest.TestCase):
    def test_no_bare_json_decode_anywhere(self):
        offenders = []
        for rel, path in python_files():
            for lineno, what in bare_json_uses(path.read_text(encoding="utf-8"), rel):
                offenders.append(f"{rel}:{lineno}: {what}")
        self.assertEqual(offenders, [], "route these through devxdk_manifest.strictjson")


if __name__ == "__main__":
    unittest.main()
