#!/usr/bin/env python3
"""Reject host-specific dynamic dependencies in a portable macOS bundle."""
import pathlib
import subprocess
import sys


def verify(root):
    root = pathlib.Path(root)
    errors = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        kind = subprocess.check_output(['file', '-b', str(path)], text=True)
        if 'Mach-O' not in kind:
            continue
        output = subprocess.check_output(['otool', '-L', str(path)], text=True)
        for line in output.splitlines()[1:]:
            dependency = line.strip().split(' (', 1)[0]
            if not dependency.startswith(('/usr/lib/', '/System/Library/', '@rpath/', '@loader_path/', '@executable_path/')):
                errors.append(f'{path.relative_to(root)}: non-portable dependency {dependency}')
    if errors:
        raise RuntimeError('\n'.join(errors))


if __name__ == '__main__':
    verify(sys.argv[1])
