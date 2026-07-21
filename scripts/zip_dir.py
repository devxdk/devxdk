#!/usr/bin/env python3
"""Zip a directory's contents FLAT-ROOTED into an archive (strip 0 layout).

  zip_dir.py <dir> <out.zip>

Entries are the directory's files with paths relative to it, sorted, with a
fixed timestamp so re-zipping identical content is byte-stable. Symlinks are
rejected (bundle members are plain files). Standard library only — recipes use
this instead of whichever zip/7z binary a runner happens to carry.
"""

import pathlib
import sys
import zipfile

FIXED_DATE = (1980, 1, 1, 0, 0, 0)  # zip epoch; builds are provenance-recorded, not timestamped


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: zip_dir.py <dir> <out.zip>\n")
        return 2
    root, out = pathlib.Path(argv[0]), pathlib.Path(argv[1])
    if not root.is_dir():
        sys.stderr.write(f"zip_dir: not a directory: {root}\n")
        return 1
    members = sorted(p for p in root.rglob("*") if not p.is_dir())
    if not members:
        sys.stderr.write(f"zip_dir: nothing to archive in {root}\n")
        return 1
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in members:
            if p.is_symlink() or not p.is_file():
                sys.stderr.write(f"zip_dir: non-regular member rejected: {p}\n")
                return 1
            rel = p.relative_to(root).as_posix()
            info = zipfile.ZipInfo(rel, date_time=FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            # 0644 regular file; the app's extractor applies its own policy.
            info.external_attr = 0o644 << 16
            zf.writestr(info, p.read_bytes())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
