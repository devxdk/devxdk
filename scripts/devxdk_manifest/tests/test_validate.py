"""Tests for validate_manifests: the committed manifests pass, and every
malformed branch fails closed."""

import pathlib
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

import validate_manifests as vm  # noqa: E402
from devxdk_manifest import allowlist, config  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
HOSTS = allowlist.VENDORED_HOSTS
NODE_URL = "https://nodejs.org/dist/v24.17.0/node-v24.17.0-win-x64.zip"


def _asset(url=NODE_URL, sha="a" * 64, size=100):
    return {"url": url, "sha256": sha, "size_bytes": size}


def _release(ver="24.17.0", channel="lts", platforms=None):
    return {"version": ver, "channel": channel, "released_at": "",
            "platforms": platforms if platforms is not None else {"windows/amd64": _asset()}}


def _manifest(name="node", kind="runtime", releases=None):
    return {"name": name, "display_name": "X", "kind": kind, "releases": releases or []}


class TestRealManifests(unittest.TestCase):
    def test_committed_manifests_and_state_are_clean(self):
        self.assertEqual(vm.validate(REPO_ROOT), [])

    def test_clean_with_app_source_allowlist(self):
        src = REPO_ROOT.parent / "internal" / "download" / "allowlist.go"
        if not src.exists():
            self.skipTest("app-src allowlist.go not available")
        self.assertEqual(vm.validate(REPO_ROOT, str(src)), [])


class TestComponentRules(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()

    def _errs(self, data, name="node"):
        return vm._validate_component(self.cfg, data, name, HOSTS)

    def test_clean_node(self):
        self.assertEqual(self._errs(_manifest(releases=[_release()])), [])

    def test_valid_prerelease(self):
        r = _release(ver="24.18.0-rc1", channel="prerelease",
                     platforms={"windows/amd64": _asset(url=NODE_URL.replace("24.17.0", "24.18.0"))})
        self.assertEqual(self._errs(_manifest(releases=[r])), [])

    def test_bad_sha(self):
        for bad in ("A" * 64, "abc", "a" * 63):
            errs = self._errs(_manifest(releases=[_release(platforms={"windows/amd64": _asset(sha=bad)})]))
            self.assertTrue(any("sha256" in e for e in errs), bad)

    def test_non_https(self):
        errs = self._errs(_manifest(releases=[_release(platforms={"windows/amd64": _asset(url="http://nodejs.org/x.zip")})]))
        self.assertTrue(any("https" in e for e in errs))

    def test_bad_host(self):
        errs = self._errs(_manifest(releases=[_release(platforms={"windows/amd64": _asset(url="https://evil.com/x.zip")})]))
        self.assertTrue(any("not allowlisted" in e for e in errs))

    def test_bad_extension(self):
        errs = self._errs(_manifest(releases=[_release(platforms={"windows/amd64": _asset(url="https://nodejs.org/x.tar.xz")})]))
        self.assertTrue(any("extension" in e for e in errs))

    def test_zero_size(self):
        errs = self._errs(_manifest(releases=[_release(platforms={"windows/amd64": _asset(size=0)})]))
        self.assertTrue(any("size_bytes" in e for e in errs))

    def test_duplicate_version(self):
        errs = self._errs(_manifest(releases=[_release(), _release()]))
        self.assertTrue(any("duplicate" in e for e in errs))

    def test_alias_versions(self):
        # Two raw-distinct versions that compare equal. Use a component whose line
        # admits them; php line 8.5 with 8.5.0 spellings differing only by case.
        m = {"name": "php", "display_name": "PHP", "kind": "runtime", "releases": [
            {"version": "8.5.0-RC1", "channel": "prerelease", "released_at": "",
             "platforms": {"windows/amd64": _asset(url="https://github.com/devxdk/devxdk/releases/download/x/a.zip")}},
            {"version": "8.5.0-rc1", "channel": "prerelease", "released_at": "",
             "platforms": {"windows/amd64": _asset(url="https://github.com/devxdk/devxdk/releases/download/x/b.zip")}},
        ]}
        errs = vm._validate_component(self.cfg, m, "php", HOSTS)
        self.assertTrue(any("alias" in e for e in errs))

    def test_prerelease_channel_mismatch(self):
        errs = self._errs(_manifest(releases=[_release(ver="24.18.0-rc1", channel="lts",
                                                       platforms={"windows/amd64": _asset(url=NODE_URL.replace("24.17.0", "24.18.0"))})]))
        self.assertTrue(any("prerelease" in e for e in errs))

    def test_stable_with_prerelease_channel(self):
        errs = self._errs(_manifest(releases=[_release(channel="prerelease")]))
        self.assertTrue(any("prerelease" in e for e in errs))

    def test_postgres_major_minor(self):
        m = {"name": "postgres", "display_name": "PostgreSQL", "kind": "service", "releases": [
            {"version": "18.4.1", "channel": "stable", "released_at": "",
             "platforms": {"linux/amd64": _asset(url="https://github.com/devxdk/devxdk/releases/download/x/pg.tar.gz")}},
        ]}
        errs = vm._validate_component(self.cfg, m, "postgres", HOSTS)
        self.assertTrue(any("MAJOR.MINOR" in e for e in errs))

    def test_untracked_component_with_releases(self):
        errs = vm._validate_component(self.cfg, _manifest(name="tablex", releases=[_release()]), "tablex", HOSTS)
        self.assertTrue(any("untracked" in e for e in errs))

    def test_invalid_platform_key(self):
        errs = self._errs(_manifest(releases=[_release(platforms={"linux/riscv64": _asset()})]))
        self.assertTrue(any("invalid platform key" in e for e in errs))

    def test_platform_not_in_line(self):
        # darwin/amd64 is not a configured node platform in line 24? It IS. Use a
        # platform valid globally but not configured for this component/line:
        # mariadb has no darwin platform.
        m = {"name": "mariadb", "display_name": "MariaDB", "kind": "service", "releases": [
            {"version": "11.8.8", "channel": "lts", "released_at": "",
             "platforms": {"darwin/arm64": _asset(url="https://archive.mariadb.org/x.tar.gz")}},
        ]}
        errs = vm._validate_component(self.cfg, m, "mariadb", HOSTS)
        self.assertTrue(any("not configured for line" in e for e in errs))

    def test_composer_phar_ext(self):
        good = {"name": "composer", "display_name": "Composer", "kind": "runtime", "releases": [
            {"version": "2.10.2", "channel": "stable", "released_at": "",
             "platforms": {"any": _asset(url="https://getcomposer.org/download/2.10.2/composer.phar")}},
        ]}
        self.assertEqual(vm._validate_component(self.cfg, good, "composer", HOSTS), [])
        bad = {"name": "composer", "display_name": "Composer", "kind": "runtime", "releases": [
            {"version": "2.10.2", "channel": "stable", "released_at": "",
             "platforms": {"any": _asset(url="https://getcomposer.org/download/2.10.2/composer.zip")}},
        ]}
        self.assertTrue(any("extension" in e for e in vm._validate_component(self.cfg, bad, "composer", HOSTS)))


if __name__ == "__main__":
    unittest.main()
