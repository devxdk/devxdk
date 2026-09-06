"""Correlate publication with exact Actions runs and commits, with bounded waits."""
import io
import pathlib
import subprocess
import time
import zipfile

from . import publication as pub, strictjson

REPO = 'devxdk/devxdk'


def gh_json(endpoint, *args):
    result = subprocess.run(['gh','api','-H','X-GitHub-Api-Version: 2026-03-10',endpoint,*args],
                            capture_output=True,text=True,check=True)
    return strictjson.loads(result.stdout)


def dispatch_sign():
    result = gh_json(f'repos/{REPO}/actions/workflows/scrape-and-sign.yml/dispatches',
                     '-X','POST','-f','ref=main')
    return pub.positive(result.get('workflow_run_id'), 'dispatched workflow_run_id')


def wait_run(run_id, deadline, sleep=time.sleep):
    while time.monotonic() < deadline:
        run = gh_json(f'repos/{REPO}/actions/runs/{int(run_id)}')
        if run['status'] == 'completed':
            if run['conclusion'] != 'success':
                raise pub.PublicationError(f"workflow {run_id} ended {run['conclusion']}")
            return run
        sleep(20)
    raise pub.PublicationError(f'timed out waiting for workflow {run_id}')


def signing_commit(run):
    name = f"sign-result-{run['run_attempt']}"
    artifacts = []
    page = 1
    while True:
        batch = gh_json(f"repos/{REPO}/actions/runs/{run['id']}/artifacts?per_page=100&page={page}")['artifacts']
        artifacts.extend(a for a in batch if a['name'] == name and not a['expired'])
        if len(batch) < 100:
            break
        page += 1
    if len(artifacts) != 1:
        raise pub.PublicationError(f'{name}: expected exactly one signing result artifact')
    data = subprocess.check_output(['gh','api',f"repos/{REPO}/actions/artifacts/{artifacts[0]['id']}/zip"])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo('sign-result.json')
        if info.file_size > 65536:
            raise pub.PublicationError('oversized signing result')
        result = strictjson.loads(archive.read(info))
    if result.get('run_id') != run['id'] or result.get('run_attempt') != run['run_attempt']:
        raise pub.PublicationError('signing result belongs to another run')
    import re
    if not re.fullmatch(r'[0-9a-f]{40}', result.get('commit','')):
        raise pub.PublicationError('invalid signing commit')
    return result['commit']


def wait_checks(commit, deadline, sleep=time.sleep):
    for workflow in ('ci.yml', 'versions-consistency.yml'):
        while time.monotonic() < deadline:
            result = gh_json(f'repos/{REPO}/actions/workflows/{workflow}/runs?head_sha={commit}&event=push&per_page=100')
            runs = result['workflow_runs']
            if runs:
                run = max(runs, key=lambda r: r['id'])
                wait_run(run['id'], deadline, sleep)
                break
            sleep(20)
        else:
            raise pub.PublicationError(f'no completed {workflow} for commit {commit}')


def checkout_commit(root, commit):
    root = pathlib.Path(root)
    subprocess.run(['git','fetch','origin',commit],cwd=root,check=True)
    subprocess.run(['git','checkout','--detach',commit],cwd=root,check=True)
