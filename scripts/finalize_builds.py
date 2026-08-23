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
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import add_built_release  # noqa: E402
from devxdk_manifest import plan, strictjson  # noqa: E402

REPO = "devxdk/devxdk"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _download_url(meta):
    if meta.get("ordering_kind") == "adopted":
        return meta["url"]  # adopt references the upstream asset directly (no rehost)
    # plan.release_tag is the ONE tag rule (L37): a drifted inline copy would
    # bake a 404 URL into the signed manifest.
    tag = plan.release_tag(meta["component"], meta["version"], meta["revision"])
    return f"https://github.com/{REPO}/releases/download/{tag}/{meta['archive']}"


# Every field write_pending reads out of a .meta.json. Resolved in the
# preflight so a malformed SECOND file cannot leave the FIRST record written.
_META_FIELDS = ("component", "version", "platform", "line", "ordering_kind",
                "provider", "epoch", "revision", "source_version", "sha256",
                "size_bytes")


def write_pending(metas_dir, repo_root=REPO_ROOT):
    """Write a pending record per meta; return the list of written paths.

    PASS 1 strict-parses and field-checks EVERY meta before PASS 2 writes any
    record. The parse and the write used to interleave, so a malformed second
    file left the first record already written — the same shape the four
    multi-manifest writers are preflighted for.

    The preflight lives inside this function deliberately: commit_and_push
    re-invokes it per attempt (reset --hard discards the just-committed
    records), so resolve-then-write here keeps every attempt independently
    atomic.

    Scope the guarantee honestly: this writes files into pending/; the durable
    boundary is the git add + commit + push in commit_and_push, all after it
    returns. One residual stays — an add_built_release REJECTION mid-batch
    raises SystemExit below, which escapes both functions (the retry loop's
    fetch/reset is reached only on a PUSH rejection), leaving earlier records
    untracked and unstaged, never committed, discarded with the CI workspace.
    Pre-existing; closing it needs a dry-run mode on add_built_release.
    """
    metas_dir = pathlib.Path(metas_dir)
    metas = []
    for meta_path in sorted(metas_dir.glob("*.meta.json")):
        meta = strictjson.load(meta_path)
        if not isinstance(meta, dict):
            raise SystemExit(f"finalize: {meta_path.name} is not a JSON object")
        missing = [f for f in _META_FIELDS if f not in meta]
        if missing:
            raise SystemExit(f"finalize: {meta_path.name} is missing {', '.join(missing)}")
        metas.append((meta_path, meta))

    written = []
    for meta_path, meta in metas:
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


def _check_revision_history():
    """Run the pre-push monotonicity gate against the freshly fetched tip."""
    gate = REPO_ROOT / "scripts" / "ci" / "check_revision_history.py"
    proc = subprocess.run([sys.executable, str(gate), "--base", "FETCH_HEAD", "--head", "HEAD"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
    return proc.returncode == 0


def commit_and_push(metas_dir, attempts=5):
    """Write pending/ records, commit, gate, and push with a full rebase-retry.

    EVERY ATTEMPT NOW OPENS WITH fetch + reset --hard, which is a reshape: the
    loop used to fetch and reset only AFTER a rejection, so on the first attempt
    FETCH_HEAD was absent or left over from something unrelated and was not a
    comparison base at all. That shape is now identical to
    scrape-sign-push.sh's — fetch, reset, generate, commit, gate, push — which
    is what lets ONE rule cover all three automated writers.

    Fetching without resetting would be WORSE than not fetching: HEAD would sit
    on the checkout-time tip while FETCH_HEAD moved to the current one, and the
    moment anyone pushed in between the gate would compare two commits with no
    ancestry — hard-failing an ordinary race the push-rejection retry already
    handles.

    The reset is safe on the first attempt for the reason this loop always
    relied on: write_pending re-derives every record from --metas each attempt,
    and that directory lives outside the repo, so reset --hard discards nothing
    it needs. (Idempotent downstream too: apply_pending discards any record
    whose version already landed.)

    This writer touches no manifest, so the gate passes trivially here — RUN IT
    ANYWAY. The property worth having is "every push by the automation actor was
    gated", and that is only true if no writer is exempt. Bypass attaches to the
    ACTOR, not the workflow: one PAT pushes scrape, finalize and the release, so
    a bypass granted for one is a bypass for all three.
    """
    for attempt in range(1, attempts + 1):
        _git("fetch", "origin", "main")
        _git("reset", "--hard", "FETCH_HEAD")
        write_pending(metas_dir)
        _git("add", "pending")
        if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
            sys.stderr.write("finalize: pending records already applied — nothing to commit\n")
            return True
        _git("commit", "-m", "chore: queue built-runtime pending records")
        if not _check_revision_history():
            sys.stderr.write("finalize: revision history gate failed — not pushing\n")
            return False
        push = _git("push", "origin", "HEAD:main", check=False)
        if push.returncode == 0:
            sys.stderr.write(f"finalize: pushed on attempt {attempt}\n")
            return True
        sys.stderr.write(f"finalize: push rejected (attempt {attempt}); rebasing\n{push.stderr.strip()}\n")
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write pending records and dispatch the scrape.")
    ap.add_argument("--metas", required=True, help="downloaded finalizable-metas dir")
    ap.add_argument("--no-dispatch", action="store_true", help="write+commit only (tests/local)")
    args = ap.parse_args(argv)

    import pathlib
    if not sorted(pathlib.Path(args.metas).glob("*.meta.json")):
        sys.stderr.write("finalize: no metas to finalize\n")
        return 0
    if not commit_and_push(args.metas):
        sys.stderr.write("finalize: exhausted push retries\n")
        return 1
    if not args.no_dispatch:
        subprocess.run(["gh", "workflow", "run", "scrape-and-sign.yml", "--repo", REPO], check=True)
        sys.stderr.write("finalize: dispatched scrape-and-sign\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
