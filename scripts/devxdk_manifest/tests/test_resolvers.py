"""Tests for the per-provider resolvers and the leg-map planner."""

import json
import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

from devxdk_manifest import config, merge, plan, resolvers, schema  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# Append-ordered like the real files: newer lines first, then maintenance
# releases of OLDER lines, plus prerelease entries that must be skipped.
HASHES = """\
hash redis-8.8.0.tar.gz sha256 88422181efb0c9c0abba332e3e391d409e1e13714b838931669235e5796f704b http://download.redis.io/releases/redis-8.8.0.tar.gz
hash redis-9.0.0-rc1.tar.gz sha256 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa http://download.redis.io/releases/redis-9.0.0-rc1.tar.gz
hash redis-7.4.9.tar.gz sha256 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb http://download.redis.io/releases/redis-7.4.9.tar.gz
hash redis-8.2.7.tar.gz sha256 cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc http://download.redis.io/releases/redis-8.2.7.tar.gz
"""

PHP_RELEASES = {
    "8.4": {"version": "8.4.23",
            "nts-vs17-x64": {"zip": {"path": "php-8.4.23-nts-Win32-vs17-x64.zip", "sha256": "AA" * 32}}},
    "8.5": {"version": "8.5.8",
            "nts-vs17-x64": {"zip": {"path": "php-8.5.8-nts-Win32-vs17-x64.zip", "sha256": "bb" * 32}}},
}


class FakeFetcher:
    def __init__(self, texts=None, jsons=None, paginated=None):
        self.texts, self.jsons = texts or {}, jsons or {}
        self.paginated = paginated or {}

    def get_text(self, url, headers=None):
        return self.texts[url]

    def get_json(self, url, headers=None):
        return self.jsons[url]

    def get_json_paginated(self, url, headers=None):
        return self.paginated[url]


def _hashes_fetcher(ref="deadbeef"):
    url = f"https://raw.githubusercontent.com/redis/redis-hashes/{ref}/README"
    return FakeFetcher(texts={url: HASHES})


class TestHashesNewest(unittest.TestCase):
    def test_version_sorts_and_skips_prereleases(self):
        got = resolvers.hashes_newest(_hashes_fetcher(), "redis/redis-hashes", "deadbeef", "redis", "8")
        self.assertEqual(got["source_version"], "8.8.0")  # not 8.2.7 (appended later), not 9.0.0-rc1
        self.assertTrue(got["source_url"].startswith("https://"))  # http rewritten

    def test_line_filtering(self):
        got = resolvers.hashes_newest(_hashes_fetcher(), "redis/redis-hashes", "deadbeef", "redis", "7")
        self.assertEqual(got["source_version"], "7.4.9")

    def test_no_stable_release_errors(self):
        with self.assertRaises(resolvers.ResolveError):
            resolvers.hashes_newest(_hashes_fetcher(), "redis/redis-hashes", "deadbeef", "redis", "9")


class TestPhpWindows(unittest.TestCase):
    URL = "https://downloads.php.net/~windows/releases/releases.json"

    def test_resolves_branch(self):
        got = resolvers.php_windows_newest(FakeFetcher(jsons={self.URL: PHP_RELEASES}), "8.4")
        self.assertEqual(got["source_version"], "8.4.23")
        self.assertEqual(got["variant"], "nts-vs17-x64")
        self.assertEqual(got["source_sha256"], "aa" * 32)  # lowercased

    def test_missing_branch(self):
        with self.assertRaises(resolvers.ResolveError):
            resolvers.php_windows_newest(FakeFetcher(jsons={self.URL: PHP_RELEASES}), "8.3")

    def test_two_variants_ambiguous(self):
        data = {"8.4": dict(PHP_RELEASES["8.4"], **{"nts-vs18-x64": PHP_RELEASES["8.4"]["nts-vs17-x64"]})}
        with self.assertRaises(resolvers.ResolveError):
            resolvers.php_windows_newest(FakeFetcher(jsons={self.URL: data}), "8.4")


class TestDecide(unittest.TestCase):
    def _ledger_rec(self):
        return merge.LedgerRecord(
            kind="built", line="8", provider="devxdk-redis-msys2", epoch=1, key="1",
            source_version="8.8.0", url="https://x/y.zip", sha256="a" * 64,
            size_bytes=1, channel="stable", released_at="2026-01-01")

    def test_fresh_builds_r1(self):
        self.assertEqual(plan.decide(manifest_has=False, ledger_rec=None,
                                     pending_exists=False, revisions=set(), force=False),
                         ("build", 1))

    def test_published_asset_without_manifest_finalizes(self):
        self.assertEqual(plan.decide(manifest_has=False, ledger_rec=None,
                                     pending_exists=False, revisions={1, 2}, force=False),
                         ("finalize-only", 2))

    def test_up_to_date_skips(self):
        self.assertIsNone(plan.decide(manifest_has=True, ledger_rec=self._ledger_rec(),
                                      pending_exists=False, revisions={1}, force=False))

    def test_force_takes_next_revision(self):
        self.assertEqual(plan.decide(manifest_has=True, ledger_rec=self._ledger_rec(),
                                     pending_exists=False, revisions={1}, force=True),
                         ("build", 2))

    def test_pending_skips(self):
        self.assertIsNone(plan.decide(manifest_has=False, ledger_rec=None,
                                      pending_exists=True, revisions={1}, force=False))

    def test_manifest_without_ledger_is_corruption(self):
        with self.assertRaises(plan.PlanError):
            plan.decide(manifest_has=True, ledger_rec=None,
                        pending_exists=False, revisions=set(), force=False)


class TestPublishedRevisions(unittest.TestCase):
    def test_probes_until_absent(self):
        store = {
            "redis-8.8.0": ["redis-8.8.0-windows-amd64.zip"],
            "redis-8.8.0-r2": ["redis-8.8.0-r2-windows-amd64.zip"],
        }
        got = plan.published_revisions(store.get, "redis", "8.8.0", "windows/amd64")
        self.assertEqual(got, {1, 2})

    def test_release_without_platform_asset_not_counted(self):
        store = {"redis-8.8.0": ["redis-8.8.0-linux-amd64.tar.gz"]}
        got = plan.published_revisions(store.get, "redis", "8.8.0", "windows/amd64")
        self.assertEqual(got, set())


class TestBuildLegMap(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "state").mkdir()
        merge.LedgerState().save(self.root / "state" / "asset-revisions.json")

        hashes_redis = "https://raw.githubusercontent.com/redis/redis-hashes/{}/README".format(
            self.cfg.pins["redis_hashes"]["ref"])
        hashes_valkey = "https://raw.githubusercontent.com/valkey-io/valkey-hashes/{}/README".format(
            self.cfg.pins["valkey_hashes"]["ref"])
        self.fetcher = FakeFetcher(
            texts={
                hashes_redis: HASHES,
                hashes_valkey: HASHES.replace("redis", "valkey").replace("8.8.0", "9.1.0")
                                     .replace("8.2.7", "9.0.4").replace("7.4.9", "8.1.8"),
            },
            jsons={
                "https://downloads.php.net/~windows/releases/releases.json": PHP_RELEASES,
                ASTRAL_LATEST: {"id": 356187877, "tag_name": "20260718"},
                THESEUS_RELEASES: [_theseus_release("18.4.0"), _theseus_release("18.3.0")],
            },
            paginated={ASTRAL_ASSETS: [_astral_asset("3.14.6", t, f"{i:064d}", 100 + i)
                                       for i, t in enumerate(_TRIPLES.values(), 1)]},
        )
        self.fetcher.texts["https://gh/postgresql-18.4.0-x86_64-unknown-linux-gnu.tar.gz.sha256"] = (
            f"{'a' * 64}  postgresql-18.4.0-x86_64-unknown-linux-gnu.tar.gz\n")

    def _map(self, **kw):
        return plan.build_leg_map(self.cfg, self.root, self.fetcher, lambda _t: None, **kw)

    def test_fresh_state_plans_all_enabled_legs(self):
        legs = self._map()
        # redis/valkey/php are windows-only (their unix providers are not yet
        # enabled); python (astral adopt) is enabled on all four platforms.
        self.assertEqual(set(legs), {
            "redis-windows-amd64", "valkey-windows-amd64", "php-windows-amd64",
            "python-windows-amd64", "python-linux-amd64",
            "python-darwin-amd64", "python-darwin-arm64",
            "postgres-linux-amd64"})  # theseus (linux); EDB win/mac not enabled
        self.assertEqual([i["version"] for i in legs["php-windows-amd64"]], ["8.4.23", "8.5.8"])
        # postgres: manifest version is MAJOR.MINOR while source_version is the
        # full theseus version (the adopt ordering key).
        pg = legs["postgres-linux-amd64"][0]
        self.assertEqual((pg["version"], pg["source_version"], pg["ordering_kind"], pg["provider"]),
                         ("18.4", "18.4.0", "adopted", "theseus"))
        item = legs["redis-windows-amd64"][0]
        self.assertEqual(item, {
            "component": "redis", "version": "8.8.0", "revision": 1, "line": "8",
            "platform": "windows/amd64", "runner": "windows-2022", "recipe": "redis-msys2",
            "mode": "build", "ordering_kind": "built", "provider": "devxdk-redis-msys2",
            "epoch": 1, "source_version": "8.8.0"})
        # An adopt leg carries ordering_kind "adopted" and the astral provider.
        py = legs["python-linux-amd64"][0]
        self.assertEqual((py["component"], py["version"], py["ordering_kind"],
                          py["provider"], py["mode"], py["runner"]),
                         ("python", "3.14.6", "adopted", "astral", "build", "ubuntu-22.04"))

    def test_component_filter(self):
        legs = self._map(components=["php"])
        self.assertEqual(set(legs), {"php-windows-amd64"})

    def test_version_override_targets_one_line(self):
        legs = self._map(components=["php"], version_override="8.4.23")
        self.assertEqual([i["line"] for i in legs["php-windows-amd64"]], ["8.4"])

    def test_published_asset_flips_to_finalize_only(self):
        assets = {"redis-8.8.0": ["redis-8.8.0-windows-amd64.zip"]}
        legs = plan.build_leg_map(self.cfg, self.root, self.fetcher, assets.get,
                                  components=["redis"])
        self.assertEqual(legs["redis-windows-amd64"][0]["mode"], "finalize-only")

    def test_revoked_ledger_entry_skips(self):
        led = merge.LedgerState()
        led.put("redis", "8.8.0", "windows/amd64", merge.LedgerRecord(
            kind="built", line="8", provider="devxdk-redis-msys2", epoch=1, key="1",
            source_version="8.8.0", url="https://x/y.zip", sha256="a" * 64,
            size_bytes=1, channel="stable", released_at="2026-01-01", revoked=True))
        led.save(self.root / "state" / "asset-revisions.json")
        legs = self._map(components=["redis"])
        self.assertEqual(legs, {})

    def test_manifest_without_ledger_raises(self):
        schema.write(self.root / "redis.json", schema.component("redis", "Redis", "service", [
            schema.release("8.8.0", "stable", "2026-01-01",
                           {"windows/amd64": schema.asset("https://x/y.zip", "a" * 64, 1)})]))
        with self.assertRaises(plan.PlanError):
            self._map(components=["redis"])


def _astral_asset(ver, triple, sha, size, digest=True):
    a = {
        "name": f"cpython-{ver}+20260718-{triple}-install_only.tar.gz",
        "browser_download_url": f"https://github.com/astral-sh/python-build-standalone/"
                                f"releases/download/20260718/cpython-{ver}+20260718-{triple}-install_only.tar.gz",
        "size": size,
    }
    if digest:
        a["digest"] = f"sha256:{sha}"
    return a


ASTRAL_LATEST = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
ASTRAL_ASSETS = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/356187877/assets"
_TRIPLES = {
    "windows/amd64": "x86_64-pc-windows-msvc",
    "linux/amd64": "x86_64-unknown-linux-gnu",
    "darwin/amd64": "x86_64-apple-darwin",
    "darwin/arm64": "aarch64-apple-darwin",
}


class TestAstralNewest(unittest.TestCase):
    def _fetcher(self, assets):
        return FakeFetcher(
            jsons={ASTRAL_LATEST: {"id": 356187877, "tag_name": "20260718"}},
            paginated={ASTRAL_ASSETS: assets},
        )

    def _full(self, ver, start=1):
        return [_astral_asset(ver, t, f"{i:064d}", 100 + i) for i, t in enumerate(_TRIPLES.values(), start)]

    def test_picks_newest_complete_four_platform(self):
        assets = (
            self._full("3.14.6")
            + self._full("3.14.5", start=10)
            + [_astral_asset("3.14.7", "x86_64-pc-windows-msvc", "a" * 64, 1)]  # 3.14.7 only 1 platform → incomplete
            + [_astral_asset("3.13.9", t, "b" * 64, 1) for t in _TRIPLES.values()]  # wrong line
            + [{"name": "cpython-3.14.6+20260718-x86_64-pc-windows-msvc-debug-full.tar.zst", "size": 1}]  # not install_only
        )
        got = resolvers.astral_newest(self._fetcher(assets), "3.14")
        self.assertEqual(got["source_version"], "3.14.6")  # not 3.14.7 (incomplete), not 3.14.5
        self.assertEqual(got["release_tag"], "20260718")
        self.assertEqual(set(got["platforms"]), set(_TRIPLES))
        self.assertEqual(got["platforms"]["windows/amd64"]["sha256"], f"{1:064d}")
        self.assertTrue(got["platforms"]["linux/amd64"]["url"].startswith("https://github.com/astral-sh/"))

    def test_missing_digest_fails_closed(self):
        assets = self._full("3.14.6")
        assets[0] = _astral_asset("3.14.6", _TRIPLES["windows/amd64"], "0" * 64, 1, digest=False)
        with self.assertRaises(resolvers.ResolveError):
            resolvers.astral_newest(self._fetcher(assets), "3.14")

    def test_incomplete_line_raises(self):
        assets = self._full("3.14.6")[:3]  # only 3 of 4 platforms
        with self.assertRaises(resolvers.ResolveError):
            resolvers.astral_newest(self._fetcher(assets), "3.14")


THESEUS_RELEASES = "https://api.github.com/repos/theseus-rs/postgresql-binaries/releases?per_page=100"


def _theseus_release(full, sha="a" * 64):
    tb = f"postgresql-{full}-x86_64-unknown-linux-gnu.tar.gz"
    return {"tag_name": full, "assets": [
        {"name": tb, "browser_download_url": f"https://gh/{tb}", "size": 12000000},
        {"name": tb + ".sha256", "browser_download_url": f"https://gh/{tb}.sha256"},
    ]}


def _theseus_fetcher():
    return FakeFetcher(
        jsons={THESEUS_RELEASES: [
            _theseus_release("18.4.0"), _theseus_release("18.3.0"), _theseus_release("17.6.0")]},
        texts={"https://gh/postgresql-18.4.0-x86_64-unknown-linux-gnu.tar.gz.sha256":
               f"{'a' * 64}  postgresql-18.4.0-x86_64-unknown-linux-gnu.tar.gz\n"},
    )


class TestTheseusNewest(unittest.TestCase):
    def test_newest_in_line_normalizes_manifest_version(self):
        got = resolvers.theseus_newest(_theseus_fetcher(), "18")
        self.assertEqual(got["source_version"], "18.4.0")   # full = ordering key
        self.assertEqual(got["manifest_version"], "18.4")   # MAJOR.MINOR = manifest
        a = got["platforms"]["linux/amd64"]
        self.assertEqual(a["sha256"], "a" * 64)
        self.assertTrue(a["url"].endswith("x86_64-unknown-linux-gnu.tar.gz"))

    def test_missing_sidecar_raises(self):
        f = FakeFetcher(jsons={THESEUS_RELEASES: [{"tag_name": "18.4.0", "assets": [
            {"name": "postgresql-18.4.0-x86_64-unknown-linux-gnu.tar.gz",
             "browser_download_url": "https://gh/x", "size": 1}]}]})
        with self.assertRaises(resolvers.ResolveError):
            resolvers.theseus_newest(f, "18")


if __name__ == "__main__":
    unittest.main()
