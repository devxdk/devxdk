import pathlib
import sys
import tempfile
import unittest
from dataclasses import replace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'ci'))
import check_runtime_sources as currency
from devxdk_manifest import config


class RuntimeCurrency(unittest.TestCase):
    def test_stale_index_proposes_both_pin_files_without_modifying_them(self):
        cfg = config.load()
        for name, family in (('redis', '8.10'), ('valkey', '9.1')):
            cfg.components[name].lines = {family: replace(cfg.line(name, family), historical_only=False)}
        class Client:
            def get_json(self, url, headers=None):
                return {'sha': 'f' * 40}
            def get_text(self, url, headers=None):
                name = 'redis' if '/redis/' in url else 'valkey'
                ver = '8.10.1' if name == 'redis' else '9.1.2'
                if '/'+ 'f' * 40 + '/' in url and name == 'redis':
                    ver = '8.10.2'
                return f'hash {name}-{ver}.tar.gz sha256 {"a" * 64} https://example.test/{name}-{ver}.tar.gz\n'
        errors, changes = currency.check(cfg, Client())
        self.assertEqual(len(errors), 1)
        self.assertIn('8.10.2', errors[0])
        self.assertEqual(changes, {cfg.pins['redis_hashes']['ref']: 'f' * 40})
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name in ('config/tracked-versions.toml', '.github/versions-inventory.toml'):
                path = root / name; path.parent.mkdir(parents=True)
                path.write_text(cfg.pins['redis_hashes']['ref'] + '\n')
            patch = currency.proposal(root, changes)
            self.assertIn('a/config/tracked-versions.toml', patch)
            self.assertIn('a/.github/versions-inventory.toml', patch)
            self.assertEqual((root / 'config/tracked-versions.toml').read_text().strip(), cfg.pins['redis_hashes']['ref'])
