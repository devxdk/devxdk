"""Adversarial operation, receipt, recovery, and observed-publication tests."""
import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts/ci'))
import check_build_receipts
import publish_legs
import run_leg
from devxdk_manifest import config, coverage, handoff, merge, plan, publication as pub, receipts, schema, workflow_status
from devxdk_manifest.tests import test_publish_legs as fixtures
planned_needs = fixtures.planned_needs
from devxdk_manifest.tests.test_releasepub import FakeAPI


class Fixture(unittest.TestCase):
    setUp = fixtures.TestPublish.setUp
    _stage = fixtures.TestPublish._stage
    _restore = fixtures.TestPublish._restore
    _fake_download = fixtures.TestPublish._fake_download
    def stage_operation(self):
        leg = 'redis-windows-amd64'
        info = self._stage(leg, 'redis', '8.8.0', '1001')
        needs = json.loads(planned_needs({'leg-' + leg: info}, self.staged))
        return leg, needs

    def publish(self, needs, **kw):
        return publish_legs.publish(json.dumps(needs), self.root / 'work', api=FakeAPI(), **kw)

class PublicationContract(Fixture):
    def test_planned_failed_cancelled_skipped_missing_are_errors(self):
        leg, original = self.stage_operation()
        for status in ('failure', 'cancelled', 'skipped', None):
            with self.subTest(status=status):
                needs = copy.deepcopy(original)
                if status is None:
                    del needs['leg-' + leg]
                else:
                    needs['leg-' + leg]['result'] = status
                metas, errors = self.publish(needs)
                self.assertFalse(metas)
                self.assertEqual(len(errors), 1)

    def test_unplanned_skipped_is_ignored(self):
        _leg, needs = self.stage_operation()
        needs['leg-valkey-linux-amd64'] = {'result': 'skipped'}
        metas, errors = self.publish(needs)
        self.assertEqual(len(metas), 1)
        self.assertFalse(errors)

    def test_success_without_outputs_is_an_error(self):
        leg, needs = self.stage_operation()
        needs['leg-' + leg]['outputs'] = {}
        self.assertTrue(self.publish(needs)[1])

    def test_authenticated_artifact_without_metadata_is_an_error(self):
        leg, needs = self.stage_operation()
        for path in self.staged['1001'].glob('*.meta.json'):
            path.unlink()
        needs['leg-' + leg]['outputs']['manifest_sha256'] = handoff.write(self.staged['1001'])
        self.assertIn('missing planned metadata', self.publish(needs)[1][0])

    def test_missing_second_version_is_an_error(self):
        leg, needs = self.stage_operation()
        operation = json.loads(needs['plan']['outputs']['operation'])
        missing = dict(operation['legs'][leg][0], version='8.9.0', source_version='8.9.0')
        operation['legs'][leg].append(missing)
        operation['targets'].append(dict(missing, coverage_platforms=['windows/amd64']))
        needs['plan']['outputs']['operation'] = json.dumps(operation)
        self.assertIn('8.9.0', self.publish(needs)[1][0])

    def test_duplicate_and_substituted_identity_fail_before_journal(self):
        leg, needs = self.stage_operation()
        src = self.staged['1001']
        path = next(src.glob('*.meta.json'))
        (src / 'duplicate.meta.json').write_bytes(path.read_bytes())
        needs['leg-' + leg]['outputs']['manifest_sha256'] = handoff.write(src)
        persist = mock.Mock()
        self.assertTrue(self.publish(needs, persist=persist)[1])
        persist.assert_not_called()

    def test_journal_precedes_any_release_mutation(self):
        _leg, needs = self.stage_operation()
        api = FakeAPI()
        recorded = []
        def persist(operation, values):
            self.assertEqual(api.log, [])
            recorded.extend(values)
        metas, errors = publish_legs.publish(json.dumps(needs), self.root / 'work', api=api, persist=persist)
        self.assertFalse(errors)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]['meta'], metas[0])

    def test_dry_run_never_journals_or_publishes(self):
        _leg, needs = self.stage_operation()
        persist = mock.Mock()
        metas, errors = self.publish(needs, dry=True, persist=persist)
        self.assertTrue(metas)
        self.assertFalse(errors)
        persist.assert_not_called()

    def test_bad_leg_does_not_prevent_good_leg_finalization(self):
        leg, needs = self.stage_operation()
        info = self._stage('valkey-windows-amd64', 'valkey', '9.1.0', '1002')
        needs = json.loads(planned_needs({'leg-' + leg: needs['leg-' + leg],
                                         'leg-valkey-windows-amd64': info}, self.staged))
        needs['leg-valkey-windows-amd64']['result'] = 'failure'
        metas, errors = self.publish(needs)
        self.assertEqual([m['component'] for m in metas], ['redis'])
        self.assertEqual(len(errors), 1)


class Recovery(Fixture):
    def recorded(self):
        leg, needs = self.stage_operation()
        operation = json.loads(needs['plan']['outputs']['operation'])
        meta = json.loads(next(self.staged['1001'].glob('*.meta.json')).read_text())
        receipt = receipts.make(operation, operation['legs'][leg][0], meta,
                                pub.members(meta, self.staged['1001']), needs['leg-' + leg]['outputs'], config.load().pins)
        return receipt

    def test_completed_upload_recovers_without_original_artifact(self):
        receipt = self.recorded()
        member = receipt['members'][0]
        api = FakeAPI(releases={'redis-8.8.0': {'id': 1, 'draft': False, 'assets': [
            {'name': member['name'], '_bytes': (self.staged['1001'] / member['name']).read_bytes()}]}})
        download = mock.Mock(side_effect=AssertionError('must not depend on expired workflow artifact'))
        result = run_leg.recover(receipt, self.root / 'recovered', api, download)
        self.assertEqual(result, receipt['meta'])
        download.assert_not_called()

    def test_partial_upload_restores_original_bytes(self):
        receipt = self.recorded()
        result = run_leg.recover(receipt, self.root / 'recovered', FakeAPI(), self._fake_download)
        self.assertEqual(result, receipt['meta'])
        self.assertEqual(pub.members(result, self.root / 'recovered'), receipt['members'])

    def test_missing_original_and_corrupt_public_bytes_fail(self):
        receipt = self.recorded()
        with self.assertRaisesRegex(pub.PublicationError, 'new revision'):
            run_leg.recover(receipt, self.root / 'missing', FakeAPI(), mock.Mock(side_effect=OSError('expired')))
        member = receipt['members'][0]
        api = FakeAPI(releases={'redis-8.8.0': {'id': 1, 'draft': False,
            'assets': [{'name': member['name'], '_bytes': b'tampered'}]}})
        with self.assertRaisesRegex(pub.PublicationError, 'refusing replacement'):
            run_leg.recover(receipt, self.root / 'bad', api)

    def test_receipts_are_canonical_and_append_only(self):
        receipt = self.recorded()
        path = self.root / receipts.path_for(receipt['item'])
        path.parent.mkdir(parents=True)
        path.write_bytes(pub.encode(receipt))
        self.assertEqual(receipts.load(self.root, receipt['item']), receipt)
        self.assertFalse(check_build_receipts.check(self.root))
        with mock.patch.object(check_build_receipts.subprocess, 'run', return_value=mock.Mock(stdout='M\t' + path.relative_to(self.root).as_posix())):
            self.assertTrue(check_build_receipts.check(self.root, 'base'))


class Observation(unittest.TestCase):
    def test_failed_signing_run_never_passes(self):
        import time
        with mock.patch.object(workflow_status, 'gh_json', return_value={'status': 'completed', 'conclusion': 'failure'}):
            with self.assertRaises(pub.PublicationError):
                workflow_status.wait_run(123, time.monotonic() + 30)

    def test_wait_is_bounded(self):
        with self.assertRaisesRegex(pub.PublicationError, 'timed out'):
            workflow_status.wait_run(123, 0)

    def test_forced_revision_does_not_reuse_gaps_or_skip_pending(self):
        self.assertEqual(plan.next_revision({1, 3}), 4)
        self.assertEqual(plan.decide(manifest_has=False, ledger_rec=None, pending_exists=True, revisions={1}, force=True), ('build', 2))


class Coverage(unittest.TestCase):
    def setUp(self):
        import shutil
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        (self.root / 'config').mkdir()
        (self.root / 'state').mkdir()
        (self.root / 'pending').mkdir()
        shutil.copyfile(ROOT / 'config/tracked-versions.toml', self.root / 'config/tracked-versions.toml')
        self.cfg = config.load(self.root / 'config/tracked-versions.toml')
        self.ledger = merge.LedgerState()
        self.scraped = merge.ScrapeState()
        self.scraped.save(self.root / 'state/scrape-versions.json')
        self.targets = []
        for platform in ('windows/amd64', 'darwin/arm64'):
            self.accept(self.meta(platform))
        # A retained partial security release is not a target of this backfill.
        self.accept(self.meta('windows/amd64', version='8.5.9'))
        for platform in ('linux/amd64', 'darwin/amd64'):
            meta = self.meta(platform)
            item = {**{k: meta[k] for k in pub.IDENTITY}, 'mode': 'build'}
            self.targets.append(dict(item, coverage_platforms=list(self.cfg.line('php', '8.5').platforms)))
            member = {'name': meta['archive'], 'sha256': meta['sha256'], 'size_bytes': 1, 'object_code': True}
            receipt = receipts.make({'id': '123', 'source_commit': 'a' * 40}, item, meta, [member],
                                    {'artifact_id': '123', 'manifest_sha256': 'b' * 64}, self.cfg.pins)
            path = self.root / receipts.path_for(item)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(pub.encode(receipt))
            record = pub.pending_record(meta)
            (self.root / 'pending' / (platform.replace('/', '-') + '.json')).write_bytes(pub.encode(vars(record)))
        self.ledger.save(self.root / 'state/asset-revisions.json')
        schema.write(self.root / 'php.json', merge.recompose('php', 'PHP', 'runtime', self.cfg, self.scraped, self.ledger))

    def meta(self, platform, version='8.5.10'):
        import hashlib
        plat = self.cfg.find_platform('php', '8.5', platform)
        ext = 'zip' if platform.startswith('windows') else 'tar.gz'
        return {'component': 'php', 'line': '8.5', 'version': version, 'platform': platform,
                'ordering_kind': 'built', 'provider': plat.provider, 'epoch': 1, 'revision': 1,
                'source_version': version, 'sha256': hashlib.sha256(platform.encode()).hexdigest(),
                'size_bytes': 1, 'archive': plan.archive_name('php', version, 1, platform, ext)}

    def accept(self, meta):
        self.ledger.put('php', meta['version'], meta['platform'], merge.LedgerRecord(
            kind='built', line='8.5', provider=meta['provider'], epoch=1, key=str(meta['revision']),
            source_version=meta['source_version'], url=pub.download_url(meta), sha256=meta['sha256'],
            size_bytes=1, channel='stable', released_at='2026-08-27'))

    def test_backfill_combines_accepted_and_pending_platforms(self):
        operation = {'targets': self.targets}
        self.assertTrue(coverage.check(operation, self.root)[0])
        errors, documents = coverage.check(operation, self.root, project_pending=True)
        self.assertEqual(errors, [])
        self.assertEqual(len(next(r for r in documents['php']['releases'] if r['version'] == '8.5.10')['platforms']), 4)
        self.assertEqual(len(next(r for r in documents['php']['releases'] if r['version'] == '8.5.9')['platforms']), 1)

    def test_force_requires_the_planned_revision(self):
        targets = copy.deepcopy(self.targets)
        targets[0]['revision'] = 2
        errors, _ = coverage.check({'targets': targets}, self.root, project_pending=True)
        self.assertTrue(any('revision' in error for error in errors))

    def test_discarded_stale_record_does_not_satisfy_target(self):
        meta = self.meta('linux/amd64')
        meta['revision'] = 2
        self.accept(meta)
        self.ledger.save(self.root / 'state/asset-revisions.json')
        self.assertTrue(coverage.check({'targets': self.targets}, self.root, project_pending=True)[0])
