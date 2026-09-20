#!/usr/bin/env python3
"""Recover recorded bytes centrally; recipes only receive genuine build items."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from devxdk_manifest import config, current_state, fetch, handoff, publication as pub, receipts, strictjson
from publish_legs import GhReleaseAPI, download_artifact
from devxdk_manifest.plan import archive_name, release_tag

ROOT = pathlib.Path(__file__).resolve().parent.parent


def recover(receipt, outdir, api=None, download=download_artifact):
    """Require receipt-bound public bytes; restore missing uploads from original handoff."""
    api = api or GhReleaseAPI()
    meta = receipt['meta']
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    if meta['ordering_kind'] == 'adopted':
        data = fetch.Fetcher(max_bytes=meta['size_bytes']).get_bytes(meta['url'])
        import hashlib
        if len(data) != meta['size_bytes'] or hashlib.sha256(data).hexdigest() != meta['sha256']:
            raise pub.PublicationError('adopted bytes differ from committed receipt')
    else:
        release = api.get_release(release_tag(meta['component'], meta['version'], meta['revision']))
        assets = {a['name']: a for a in (release or {}).get('assets', [])}
        missing = []
        import hashlib
        for member in receipt['members']:
            asset = assets.get(member['name'])
            if asset is None or asset.get('state') == 'starter':
                missing.append(member)
                continue
            data = api.download_asset(asset)
            if len(data) != member['size_bytes'] or hashlib.sha256(data).hexdigest() != member['sha256']:
                raise pub.PublicationError(f"published {member['name']} differs from committed receipt; refusing replacement")
            (outdir / member['name']).write_bytes(data)
        if missing:
            with tempfile.TemporaryDirectory(prefix='devxdk-recover-') as directory:
                original = pathlib.Path(directory) / 'original'
                try:
                    download(receipt['artifact']['artifact_id'], original)
                    handoff.verify(original, receipt['artifact']['manifest_sha256'])
                except Exception as exc:
                    raise pub.PublicationError('original bytes unavailable; dispatch a forced build at a new revision') from exc
                for member in missing:
                    path = original / member['name']
                    if not path.is_file() or path.stat().st_size != member['size_bytes'] or handoff.sha256_file(path) != member['sha256']:
                        raise pub.PublicationError(f"original artifact does not contain recorded {member['name']}")
                    shutil.copyfile(path, outdir / member['name'])
    (outdir / f"{meta['component']}-{meta['version']}.meta.json").write_bytes(pub.encode(meta))
    return meta


def main():
    if len(sys.argv) != 2:
        raise SystemExit('usage: run_leg.py <leg>')
    leg = pub.basename(sys.argv[1])
    items = strictjson.loads(os.environ['LEG_ITEMS'])
    pins = config.load().pins
    outdir = ROOT / 'build' / leg
    handoff_dir = outdir / 'handoff'
    if not handoff_dir.resolve().is_relative_to((ROOT / 'build').resolve()):
        raise pub.PublicationError('handoff directory escapes build root')
    if handoff_dir.exists():
        shutil.rmtree(handoff_dir)
    handoff_dir.mkdir(parents=True)
    outcomes = []
    os.environ.setdefault('GH_TOKEN', os.environ.get('GITHUB_TOKEN', ''))
    with current_state.snapshot(ROOT) as state:
        for item in items:
            pub.identity(item)
            from devxdk_manifest.plan import leg_id
            if leg_id(item['component'], item['platform']) != leg:
                raise pub.PublicationError('item belongs to another leg')
            try:
                receipt = receipts.load(state, item)
                if receipt:
                    if receipt['inputs'] != receipts.input_pins(pins, item):
                        raise pub.PublicationError('recorded build inputs changed; start a new operation')
                    meta = recover(receipt, outdir)
                elif item['mode'] == 'finalize-only':
                    raise pub.PublicationError('finalize-only requires a committed receipt; force a new revision')
                else:
                    env = dict(os.environ, LEG_ITEMS=json.dumps([item]))
                    subprocess.run(['bash', 'recipes/leg.sh', leg], cwd=ROOT, env=env, check=True)
                    if item['ordering_kind'] == 'built':
                        ext = 'zip' if item['platform'].startswith('windows/') else 'tar.gz'
                        filename = archive_name(item['component'], item['version'], item['revision'], item['platform'], ext)
                    else:
                        filename = f"{item['component']}-{item['version']}-{item['platform'].replace('/', '-')}"
                    meta = strictjson.load(outdir / (filename + '.meta.json'))
                pub.validate_metas([item], [meta])
                for member in pub.members(meta, outdir):
                    shutil.copyfile(outdir / member['name'], handoff_dir / member['name'])
                (handoff_dir / f"{item['component']}-{item['version']}.meta.json").write_bytes(pub.encode(meta))
                outcomes.append({'version': item['version'], 'result': 'success'})
            except Exception as exc:
                outcomes.append({'version': item['version'], 'result': 'failure', 'error': str(exc)})
                print(f"::error::{leg} {item['version']}: {exc}", file=sys.stderr)
    (handoff_dir / 'outcomes.json').write_bytes(pub.encode(outcomes))
    return int(any(outcome['result'] != 'success' for outcome in outcomes))


if __name__ == '__main__':
    sys.exit(main())
