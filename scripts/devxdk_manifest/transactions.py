"""Small append-only Git transaction used by the runtime receipt journal."""
import base64
import os
import pathlib
import subprocess
import sys

from .publication import PublicationError


def git_env(token=None):
    env = dict(os.environ)
    if token:
        authorization = base64.b64encode(('x-access-token:' + token).encode()).decode()
        env.update(GIT_CONFIG_COUNT='2',
                   GIT_CONFIG_KEY_0='http.https://github.com/.extraheader', GIT_CONFIG_VALUE_0='',
                   GIT_CONFIG_KEY_1='http.https://github.com/.extraheader',
                   GIT_CONFIG_VALUE_1='AUTHORIZATION: basic ' + authorization)
    return env


def append(root, files, message, token=None, attempts=5):
    root = pathlib.Path(root)
    for path in files:
        if not path.startswith(('build-receipts/', 'build-operations/')) or '..' in pathlib.PurePosixPath(path).parts:
            raise PublicationError('journal transaction may only append receipt/operation files')
    env = git_env(token)

    def git(*args, check=True):
        return subprocess.run(['git', *args], cwd=root, env=env, capture_output=True, text=True, check=check)

    for attempt in range(attempts):
        git('fetch', 'origin', 'main')
        git('reset', '--hard', 'FETCH_HEAD')
        for path, data in files.items():
            dest = root / path
            if dest.exists() and dest.read_bytes() != data:
                raise PublicationError(f'append-only journal conflict: {path}')
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        git('add', '--', *sorted(files))
        if git('diff', '--cached', '--quiet', check=False).returncode == 0:
            return git('rev-parse', 'HEAD').stdout.strip()
        git('commit', '-m', message)
        subprocess.run([sys.executable, 'scripts/ci/check_revision_history.py', '--base', 'FETCH_HEAD'], cwd=root, check=True)
        subprocess.run([sys.executable, 'scripts/ci/check_build_receipts.py', '--base', 'FETCH_HEAD'], cwd=root, check=True)
        if git('push', 'origin', 'HEAD:main', check=False).returncode == 0:
            return git('rev-parse', 'HEAD').stdout.strip()
    raise PublicationError(f'journal push failed after {attempts} attempts')
