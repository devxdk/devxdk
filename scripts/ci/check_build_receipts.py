#!/usr/bin/env python3
"""Validate every durable build receipt and prohibit journal rewrites/deletions."""
import argparse
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from devxdk_manifest import publication as pub, receipts, strictjson  # noqa: E402


def check(root, base=None, head='HEAD'):
    root = pathlib.Path(root)
    errors = []
    for directory in ('build-receipts', 'build-operations'):
        for path in (root / directory).rglob('*.json'):
            try:
                data = strictjson.load(path)
                if directory == 'build-receipts':
                    receipts.validate(data)
                    expected = receipts.path_for(data['item'])
                else:
                    pub.validate_operation(data)
                    expected = receipts.operation_path(data['id'])
                if path.relative_to(root).as_posix() != expected:
                    raise pub.PublicationError('identity does not match journal path')
                if path.read_bytes() != pub.encode(data):
                    raise pub.PublicationError('journal file is not canonical UTF-8/LF JSON')
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(f'{path}: {exc}')
    if base:
        proc = subprocess.run(['git','diff','--name-status','--no-renames',base,head,'--','build-receipts','build-operations'],
                              cwd=root,capture_output=True,text=True,check=True)
        for line in proc.stdout.splitlines():
            status, path = line.split('\t', 1)
            if status != 'A':
                errors.append(f'immutable journal entry changed: {path} ({status})')
    return errors


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--base')
    ap.add_argument('--head', default='HEAD')
    args = ap.parse_args()
    root = pathlib.Path(__file__).resolve().parents[2]
    errors = check(root, args.base, args.head)
    for error in errors:
        print(error, file=sys.stderr)
    return int(bool(errors))


if __name__ == '__main__':
    sys.exit(main())
