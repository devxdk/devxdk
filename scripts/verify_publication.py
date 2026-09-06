#!/usr/bin/env python3
"""Observe the exact signing run, its commit checks, and authenticated public bytes."""
import argparse
import os
import pathlib
import subprocess
import sys
import tempfile
import time

from devxdk_manifest import coverage, fetch, publication as pub, strictjson, workflow_status
from ci_verify import default_minisign_verify, _trusted_comment_file

ROOT = pathlib.Path(__file__).resolve().parent.parent


def public_documents(expected, key, minisign, deadline, base='https://manifest.devxdk.com'):
    """Accept a later signed document only if all expected operation tuples remain."""
    verify = default_minisign_verify(minisign)
    last = 'not fetched'
    with tempfile.TemporaryDirectory(prefix='devxdk-public-') as directory:
        directory = pathlib.Path(directory)
        while time.monotonic() < deadline:
            try:
                for name, wanted in expected.items():
                    path = directory / pathlib.PurePosixPath(name).name
                    path.write_bytes(fetch.Fetcher().get_bytes(f'{base}/{name}'))
                    signature = path.with_name(path.name + '.minisig')
                    signature.write_bytes(fetch.Fetcher().get_bytes(f'{base}/{name}.minisig'))
                    ok, error = verify(path, key)
                    if not ok or _trusted_comment_file(signature) != path.name:
                        raise pub.PublicationError(f'{name}: signature/basename verification failed: {error}')
                    document = strictjson.load(path)
                    pub.positive(document.get('revision'), 'public manifest revision')
                    for version, platforms in wanted.items():
                        if name == 'app/update.json':
                            channels = [document, *document.get('channels', {}).values()]
                            release = next((r for r in channels if r.get('version') == version), {})
                        else:
                            release = next((r for r in document.get('releases', []) if r['version'] == version), {})
                        for platform, asset in platforms.items():
                            actual = release.get('platforms', {}).get(platform)
                            if actual is None or any(actual.get(k) != v for k, v in asset.items()):
                                raise pub.PublicationError(f'{name} {version} {platform}: public tuple is not the published tuple')
                return
            except (ValueError, OSError, fetch.FetchError) as exc:
                last = str(exc)
                print(f'Waiting for public publication: {last}', file=sys.stderr)
                time.sleep(20)
    raise pub.PublicationError(f'public verification timed out: {last}')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--operation', help='frozen operation JSON file')
    ap.add_argument('--sign-run-id', type=int)
    ap.add_argument('--commit', help='app manifest commit (no separate signing run)')
    ap.add_argument('--app-version')
    ap.add_argument('--minisign', required=True)
    ap.add_argument('--timeout', type=int, default=5400)
    args = ap.parse_args()
    deadline = time.monotonic() + args.timeout
    operation = pub.validate_operation(strictjson.load(args.operation)) if args.operation else None
    if args.sign_run_id:
        run = workflow_status.wait_run(args.sign_run_id, deadline)
        commit = workflow_status.signing_commit(run)
    else:
        commit = args.commit
    if not commit:
        # A build with no new work still verifies the accepted state. Failed
        # planned legs cannot reach green just because finalize was skipped.
        if operation and operation['legs']:
            raise pub.PublicationError('planned work produced no signing run')
        commit = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    workflow_status.wait_checks(commit, deadline)
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        workflow_status.checkout_commit(ROOT, commit)
    if operation:
        errors, documents = coverage.check(operation, ROOT)
        if errors:
            raise pub.PublicationError('; '.join(errors))
        expected = {}
        for target in operation['targets']:
            c, version = target['component'], target['version']
            release = next(r for r in documents[c]['releases'] if r['version'] == version)
            expected.setdefault(c + '.json', {})[version] = {
                p: release['platforms'][p] for p in target['coverage_platforms']}
        key = ROOT / 'keys/manifest-signing.pub'
    else:
        document = strictjson.load(ROOT / 'app/update.json')
        release = next((r for r in [document, *document.get('channels', {}).values()]
                        if r.get('version') == args.app_version), None)
        if release is None:
            raise pub.PublicationError('app version is absent from the published commit')
        expected = {'app/update.json': {args.app_version: release['platforms']}}
        key = ROOT / 'keys/app-release-signing.pub'
    public_documents(expected, key, args.minisign, deadline)
    print(f'Publication verified at commit {commit}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
