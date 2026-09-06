"""Read live publication state without changing the run's checked-out source."""
import contextlib
import os
import pathlib
import subprocess
import tempfile


@contextlib.contextmanager
def snapshot(repo_root):
    root = pathlib.Path(repo_root)
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        yield root
        return
    subprocess.run(['git', 'fetch', 'origin', 'main'], cwd=root, check=True, capture_output=True)
    names = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', 'FETCH_HEAD'], cwd=root, text=True).splitlines()
    names = [n for n in names if n.endswith('.json') and
             ('/' not in n or n.startswith(('state/', 'pending/', 'build-receipts/', 'build-operations/')))]
    payload = ''.join(f'FETCH_HEAD:{name}\n' for name in names).encode()
    blobs = subprocess.run(['git', 'cat-file', '--batch'], input=payload, cwd=root, check=True, capture_output=True).stdout
    with tempfile.TemporaryDirectory(prefix='devxdk-state-') as directory:
        dest = pathlib.Path(directory)
        cursor = 0
        for name in names:
            end = blobs.index(b'\n', cursor)
            _sha, kind, length = blobs[cursor:end].split()
            if kind != b'blob':
                raise ValueError(f'not a state blob: {name}')
            size = int(length)
            path = dest / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blobs[end + 1:end + 1 + size])
            cursor = end + size + 2
        yield dest
