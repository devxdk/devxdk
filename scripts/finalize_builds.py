#!/usr/bin/env python3
"""Finalize job body: write pending records for verified legs, dispatch the scrape.

Consumes the publish job's finalizable-metas (downloaded by the workflow), writes
one pending/<component>-<version>[-rN]-<goos>-<goarch>.json per meta via
add_built_release, commits them with a rebase-retry (the daily scrape and other
publishes advance main between attempts), and dispatches scrape-and-sign so
apply_pending folds them into the signed manifests.

The pending files are the ONLY publish→scrape signal — they cover build, adopt,
AND finalize-only legs alike. Standard library only; git/gh are shelled out.
"""

import argparse
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import add_built_release  # noqa: E402

REPO = "devxdk/devxdk"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _download_url(meta):
    tag = f"{meta['component']}-{meta['version']}" + (
        "" if meta["revision"] <= 1 else f"-r{meta['revision']}")
    return f"https://github.com/{REPO}/releases/download/{tag}/{meta['archive']}"


def write_pending(metas_dir, repo_root=REPO_ROOT):
    """Write a pending record per meta; return the list of written paths."""
    metas_dir = pathlib.Path(metas_dir)
    written = []
    for meta_path in sorted(metas_dir.glob("*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        rc = add_built_release.main([
            "--component", meta["component"],
            "--version", meta["version"],
            "--platform", meta["platform"],
            "--line", meta["line"],
            "--ordering-kind", meta["ordering_kind"],
            "--provider", meta["provider"],
            "--epoch", str(meta["epoch"]),
            "--revision", str(meta["revision"]),
            "--source-version", meta["source_version"],
            "--url", _download_url(meta),
            "--sha256", meta["sha256"],
            "--size-bytes", str(meta["size_bytes"]),
        ])
        if rc != 0:
            raise SystemExit(f"finalize: add_built_release rejected {meta_path.name}")
        written.append(meta["component"])
    return written


def _git(*args, check=True):
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=check)


def commit_and_push(attempts=5):
    """Commit pending/ and push with a full rebase-retry: on rejection reset to
    the freshly fetched tip and RE-WRITE the pending records against it, so a
    concurrently committed scrape/publish never clobbers or is clobbered."""
    for attempt in range(1, attempts + 1):
        _git("add", "pending")
        if not _git("diff", "--cached", "--quiet", check=False).returncode:
            sys.stderr.write("finalize: no pending changes to commit\n")
            return True
        _git("commit", "-m", "chore: queue built-runtime pending records")
        push = _git("push", "origin", "HEAD:main", check=False)
        if push.returncode == 0:
            sys.stderr.write(f"finalize: pushed on attempt {attempt}\n")
            return True
        sys.stderr.write(f"finalize: push rejected (attempt {attempt}); rebasing\n")
        _git("fetch", "origin", "main")
        _git("reset", "--hard", "FETCH_HEAD")
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write pending records and dispatch the scrape.")
    ap.add_argument("--metas", required=True, help="downloaded finalizable-metas dir")
    ap.add_argument("--no-dispatch", action="store_true", help="write+commit only (tests/local)")
    args = ap.parse_args(argv)

    written = write_pending(args.metas)
    if not written:
        sys.stderr.write("finalize: no metas to finalize\n")
        return 0
    if not commit_and_push():
        sys.stderr.write("finalize: exhausted push retries\n")
        return 1
    if not args.no_dispatch:
        subprocess.run(["gh", "workflow", "run", "scrape-and-sign.yml", "--repo", REPO], check=True)
        sys.stderr.write("finalize: dispatched scrape-and-sign\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
