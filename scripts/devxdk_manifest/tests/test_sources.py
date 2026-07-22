"""Byte-identity, selection, and determinism tests for the node/go scrapers.

The byte-identity tests reconstruct the frozen upstream responses from the
COMMITTED node.json / go.json and assert the adapters reproduce those exact
bytes — proving scrape.py replaces gen-manifest.py without changing one byte of
a client-visible manifest, and that a no-change scrape is a zero diff. No test
touches a live feed.
"""

import json
import pathlib
import unittest

from devxdk_manifest import schema
from devxdk_manifest.sources import composer, go, node

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


class FakeFetcher:
    """Serves canned responses; same surface as fetch.Fetcher."""

    def __init__(self, json_map=None, text_map=None, size_map=None):
        self.json_map = json_map or {}
        self.text_map = text_map or {}
        self.size_map = size_map or {}

    def get_json(self, url, headers=None):
        return self.json_map[url]

    def get_text(self, url, headers=None):
        return self.text_map[url]

    def remote_size(self, url):
        return self.size_map[url]


def _committed(name):
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    return text, json.loads(text)


class TestNodeByteIdentity(unittest.TestCase):
    def test_reproduces_committed(self):
        raw, data = _committed("node.json")
        self.assertEqual(len(data["releases"]), 1)
        rel = data["releases"][0]
        ver = rel["version"]
        platforms = rel["platforms"]

        shasum_lines, sizes = [], {}
        for asset in platforms.values():
            fname = asset["url"].rsplit("/", 1)[-1]
            shasum_lines.append(f"{asset['sha256']}  {fname}")
            sizes[asset["url"]] = asset["size_bytes"]

        fetcher = FakeFetcher(
            json_map={node.INDEX_URL: [{"version": f"v{ver}", "date": rel["released_at"], "lts": "Krypton"}]},
            text_map={f"https://nodejs.org/dist/v{ver}/SHASUMS256.txt": "\n".join(shasum_lines) + "\n"},
            size_map=sizes,
        )
        out = schema.dump_str(node.build(fetcher))
        self.assertEqual(out, raw, "node.build output must be byte-identical to committed node.json")

    def test_selects_newest_lts_in_line(self):
        # A newer non-LTS 24.x must be skipped for the newest LTS 24.x.
        shasums = "\n".join(
            f"deadbeef{'0'*56}  node-v24.18.0-{s}" for s in ("win-x64.zip", "linux-x64.tar.gz", "darwin-x64.tar.gz", "darwin-arm64.tar.gz")
        ) + "\n"
        fetcher = FakeFetcher(
            json_map={node.INDEX_URL: [
                {"version": "v25.0.0", "date": "2026-09-01", "lts": False},
                {"version": "v24.19.0", "date": "2026-08-01", "lts": False},   # newer 24.x, not LTS
                {"version": "v24.18.0", "date": "2026-07-01", "lts": "Krypton"},  # newest LTS 24.x
                {"version": "v24.17.0", "date": "2026-06-01", "lts": "Krypton"},
            ]},
            text_map={"https://nodejs.org/dist/v24.18.0/SHASUMS256.txt": shasums},
            size_map={f"https://nodejs.org/dist/v24.18.0/node-v24.18.0-{s}": 100
                      for s in ("win-x64.zip", "linux-x64.tar.gz", "darwin-x64.tar.gz", "darwin-arm64.tar.gz")},
        )
        out = node.build(fetcher)
        self.assertEqual(out["releases"][0]["version"], "24.18.0")

    def test_no_lts_raises(self):
        fetcher = FakeFetcher(json_map={node.INDEX_URL: [{"version": "v24.0.0", "lts": False}]})
        with self.assertRaises(RuntimeError):
            node.build(fetcher)


class TestGoByteIdentity(unittest.TestCase):
    def test_reproduces_committed(self):
        raw, data = _committed("go.json")
        self.assertEqual(len(data["releases"]), 1)
        rel = data["releases"][0]
        ver = rel["version"]
        files = [
            {"filename": a["url"].rsplit("/", 1)[-1], "sha256": a["sha256"], "size": a["size_bytes"]}
            for a in rel["platforms"].values()
        ]
        fetcher = FakeFetcher(json_map={go.DL_URL: [{"version": f"go{ver}", "stable": True, "files": files}]})
        out = schema.dump_str(go.build(fetcher))
        self.assertEqual(out, raw, "go.build output must be byte-identical to committed go.json")

    def test_selects_highest_stable_numeric(self):
        def files(v):
            return [
                {"filename": f"go{v}.{s}", "sha256": "a" * 64, "size": 1}
                for s in ("windows-amd64.zip", "linux-amd64.tar.gz", "darwin-amd64.tar.gz", "darwin-arm64.tar.gz")
            ]
        fetcher = FakeFetcher(json_map={go.DL_URL: [
            {"version": "go1.27.0", "stable": False, "files": files("1.27.0")},   # newer, unstable
            {"version": "go1.26.9", "stable": True, "files": files("1.26.9")},
            {"version": "go1.26.10", "stable": True, "files": files("1.26.10")},  # 10 > 9 numerically
        ]})
        out = go.build(fetcher)
        self.assertEqual(out["releases"][0]["version"], "1.26.10")


class TestComposerByteIdentity(unittest.TestCase):
    def test_reproduces_committed(self):
        raw, data = _committed("composer.json")
        self.assertEqual(len(data["releases"]), 1)
        rel = data["releases"][0]
        ver = rel["version"]
        asset = rel["platforms"]["any"]
        url = asset["url"]
        path = "/" + url.split("/", 3)[3]  # strip the https://getcomposer.org origin

        fetcher = FakeFetcher(
            json_map={composer.VERSIONS_URL: {"stable": [{"version": ver, "path": path}]}},
            text_map={url + ".sha256sum": f"{asset['sha256']}  composer.phar\n"},
            size_map={url: asset["size_bytes"]},
        )
        out = schema.dump_str(composer.build(fetcher))
        self.assertEqual(out, raw, "composer.build output must be byte-identical to committed composer.json")

    def test_selects_newest_in_line(self):
        # A 3.x is skipped for the newest 2.x; the newest-first order is respected.
        url = "https://getcomposer.org/download/2.10.2/composer.phar"
        fetcher = FakeFetcher(
            json_map={composer.VERSIONS_URL: {"stable": [
                {"version": "3.0.0", "path": "/download/3.0.0/composer.phar"},
                {"version": "2.10.2", "path": "/download/2.10.2/composer.phar"},
                {"version": "2.10.1", "path": "/download/2.10.1/composer.phar"},
            ]}},
            text_map={url + ".sha256sum": f"{'a' * 64}  composer.phar\n"},
            size_map={url: 1},
        )
        out = composer.build(fetcher)
        self.assertEqual(out["releases"][0]["version"], "2.10.2")

    def test_malformed_checksum_raises(self):
        url = "https://getcomposer.org/download/2.10.2/composer.phar"
        fetcher = FakeFetcher(
            json_map={composer.VERSIONS_URL: {"stable": [
                {"version": "2.10.2", "path": "/download/2.10.2/composer.phar"},
            ]}},
            text_map={url + ".sha256sum": "not-a-valid-digest  composer.phar\n"},
            size_map={url: 1},
        )
        with self.assertRaises(RuntimeError):
            composer.build(fetcher)

    def test_no_stable_raises(self):
        fetcher = FakeFetcher(json_map={composer.VERSIONS_URL: {"preview": []}})
        with self.assertRaises(RuntimeError):
            composer.build(fetcher)


class TestDeterminism(unittest.TestCase):
    def test_node_go_stable_across_runs(self):
        raw_n, data_n = _committed("node.json")
        rel = data_n["releases"][0]
        ver = rel["version"]
        shl, sz = [], {}
        for a in rel["platforms"].values():
            shl.append(f"{a['sha256']}  {a['url'].rsplit('/', 1)[-1]}")
            sz[a["url"]] = a["size_bytes"]
        f = FakeFetcher(
            json_map={node.INDEX_URL: [{"version": f"v{ver}", "date": rel["released_at"], "lts": "K"}]},
            text_map={f"https://nodejs.org/dist/v{ver}/SHASUMS256.txt": "\n".join(shl) + "\n"},
            size_map=sz,
        )
        first = schema.dump_str(node.build(f))
        second = schema.dump_str(node.build(f))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
