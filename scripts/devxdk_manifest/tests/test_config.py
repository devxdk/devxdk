"""Validate config.py against the committed tracked-versions.toml plus a set of
deliberately malformed inputs (every validation branch fails closed)."""

import textwrap
import unittest

from devxdk_manifest import config


class TestRealConfig(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()  # the committed config/tracked-versions.toml

    def test_expected_components(self):
        want = {
            "go", "node", "composer", "mariadb", "nginx",
            "php", "python", "postgres", "redis", "valkey",
        }
        self.assertEqual(set(self.cfg.components), want)

    def test_scrape_and_managed_partition(self):
        scrape = {(c, l, p) for c, l, p, _ in self.cfg.scrape_keys()}
        managed = {(c, l, p) for c, l, p, _ in self.cfg.managed_keys()}
        self.assertTrue(scrape.isdisjoint(managed))
        # go/node are fully scraped; php/redis/valkey are fully build/adopt.
        self.assertIn(("go", "1", "linux/amd64"), scrape)
        self.assertIn(("php", "8.5", "windows/amd64"), managed)
        self.assertIn(("python", "3.14", "linux/amd64"), managed)

    def test_provider_and_epoch_defaults(self):
        plat = self.cfg.find_platform("nginx", "1.30", "windows/amd64")
        self.assertEqual(plat.type, "scrape")
        self.assertEqual(plat.provider, "nginx")
        self.assertEqual(plat.epoch, 1)  # omitted -> 1
        unix = self.cfg.find_platform("nginx", "1.30", "linux/amd64")
        self.assertEqual(unix.type, "build")
        self.assertTrue(unix.managed)
        self.assertEqual(unix.ordering_kind, "built")

    def test_php_two_minor_lines(self):
        php = self.cfg.component("php")
        self.assertEqual(set(php.lines), {"8.4", "8.5"})

    def test_pins_are_concrete(self):
        pins = self.cfg.pins
        self.assertEqual(pins["static_php_cli"]["version"], "2.8.5")
        self.assertEqual(len(pins["openssl"]["sha256"]), 64)
        self.assertEqual(len(pins["redis_hashes"]["ref"]), 40)
        self.assertEqual(pins["php_redis"]["dll"]["8.5"]["file"], "php_redis-6.3.0-8.5-nts-vs17-x64.zip")
        for name in ("openssl", "pcre2", "zlib"):
            self.assertRegex(pins[name]["sha256"], r"^[0-9a-f]{64}$")


class TestMalformed(unittest.TestCase):
    def _load(self, body):
        import pathlib
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8") as f:
            f.write(textwrap.dedent(body))
            name = f.name
        try:
            return config.load(pathlib.Path(name))
        finally:
            pathlib.Path(name).unlink()

    def test_wrong_schema(self):
        with self.assertRaises(config.ConfigError):
            self._load('schema = 99\n[components.go]\nkind="runtime"\n[components.go.lines."1"]\nchannel="stable"\nretain_per_line=1\nplatforms={"linux/amd64"={type="scrape",provider="golang"}}\n')

    def test_bad_kind(self):
        with self.assertRaises(config.ConfigError):
            self._load('schema=1\n[components.go]\nkind="widget"\n[components.go.lines."1"]\nchannel="stable"\nretain_per_line=1\nplatforms={"linux/amd64"={type="scrape",provider="golang"}}\n')

    def test_bad_type(self):
        with self.assertRaises(config.ConfigError):
            self._load('schema=1\n[components.go]\nkind="runtime"\n[components.go.lines."1"]\nchannel="stable"\nretain_per_line=1\nplatforms={"linux/amd64"={type="fetch",provider="golang"}}\n')

    def test_bad_platform_key(self):
        with self.assertRaises(config.ConfigError):
            self._load('schema=1\n[components.go]\nkind="runtime"\n[components.go.lines."1"]\nchannel="stable"\nretain_per_line=1\nplatforms={"linux/riscv64"={type="scrape",provider="golang"}}\n')

    def test_empty_provider(self):
        with self.assertRaises(config.ConfigError):
            self._load('schema=1\n[components.go]\nkind="runtime"\n[components.go.lines."1"]\nchannel="stable"\nretain_per_line=1\nplatforms={"linux/amd64"={type="scrape",provider=""}}\n')

    def test_bad_retain(self):
        with self.assertRaises(config.ConfigError):
            self._load('schema=1\n[components.go]\nkind="runtime"\n[components.go.lines."1"]\nchannel="stable"\nretain_per_line=0\nplatforms={"linux/amd64"={type="scrape",provider="golang"}}\n')

    def test_unknown_platform_key_field(self):
        with self.assertRaises(config.ConfigError):
            self._load('schema=1\n[components.go]\nkind="runtime"\n[components.go.lines."1"]\nchannel="stable"\nretain_per_line=1\nplatforms={"linux/amd64"={type="scrape",provider="golang",arch="x"}}\n')


if __name__ == "__main__":
    unittest.main()
