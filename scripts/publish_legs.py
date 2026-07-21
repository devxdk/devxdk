#!/usr/bin/env python3
"""Publish job body: reconcile each verified leg's Release, emit finalizable metas.

Reads the workflow `needs` context (toJSON), selects legs whose job result is
`success`, downloads each BY its immutable artifact id, authenticates it with
handoff.verify against the leg's manifest_sha256 job output, then reconciles the
component Release `<component>-<version>[-rN]` through releasepub. Emits a
finalizable-metas directory listing ONLY the legs that fully verified.

The reconciliation LOGIC lives in releasepub (fake-API unit-tested); this CLI is
the thin gh-backed shell-out. Fails RED on any collected per-leg failure while
still emitting the partial success list, so a late-platform re-run converges and
finalize still applies the legs that worked. Standard library only.
"""

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from devxdk_manifest import handoff, releasepub, schema  # noqa: E402

REPO = "devxdk/devxdk"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class GhReleaseAPI:
    """releasepub.ReleaseAPI backed by `gh` (auth via GH_TOKEN in the env)."""

    def _gh(self, *args, check=True):
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise releasepub.ReleaseError(f"gh {' '.join(args)}: {proc.stderr.strip()}")
        return proc

    def _api(self, *args, check=True):
        return self._gh("api", "-H", "Accept: application/vnd.github+json", *args, check=check)

    def get_release(self, tag):
        proc = self._api(f"repos/{REPO}/releases/tags/{tag}", check=False)
        if proc.returncode != 0:
            if "Not Found" in proc.stderr or "404" in proc.stderr:
                return None
            raise releasepub.ReleaseError(f"gh get release {tag}: {proc.stderr.strip()}")
        rel = json.loads(proc.stdout)
        return {"id": rel["id"], "draft": rel["draft"], "assets": self._assets(rel["id"])}

    def _assets(self, release_id):
        # Paginate to exhaustion — a multi-platform, multi-source release can
        # exceed the 30/page default.
        proc = self._api("--paginate", f"repos/{REPO}/releases/{release_id}/assets?per_page=100")
        out = []
        # --paginate concatenates JSON arrays; normalize by re-parsing per line-batch.
        for chunk in _split_json_arrays(proc.stdout):
            out.extend(chunk)
        return [{"id": a["id"], "name": a["name"], "size": a["size"],
                 "digest": a.get("digest"), "state": a.get("state"),
                 "url": a["url"]} for a in out]

    def create_release(self, tag, *, prerelease):
        # Component releases never become the repo's "latest" (the app release
        # owns that pointer); always draft, prerelease per the version class.
        args = ["release", "create", tag, "--repo", REPO, "--draft",
                "--title", tag, "--notes", "Automated DevXDK build.", "--latest=false"]
        if prerelease:
            args.append("--prerelease")
        self._gh(*args)
        rel = self._api(f"repos/{REPO}/releases/tags/{tag}")
        data = json.loads(rel.stdout)
        return {"id": data["id"], "draft": True, "assets": []}

    def upload_asset(self, release_id, name, path):
        self._gh("release", "upload", self._tag_for(release_id), path, "--repo", REPO, "--clobber")
        for a in self._assets(release_id):
            if a["name"] == name:
                return a
        raise releasepub.ReleaseError(f"uploaded asset {name} not found after upload")

    def delete_asset(self, asset_id):
        self._api("-X", "DELETE", f"repos/{REPO}/releases/assets/{asset_id}")

    def publish_release(self, release_id):
        self._api("-X", "PATCH", f"repos/{REPO}/releases/{release_id}",
                  "-F", "draft=false")

    def download_asset(self, asset):
        req = urllib.request.Request(asset["url"], headers={
            "Accept": "application/octet-stream",
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}"})
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 (github api)
            return resp.read()

    def _tag_for(self, release_id):
        data = json.loads(self._api(f"repos/{REPO}/releases/{release_id}").stdout)
        return data["tag_name"]


def _split_json_arrays(text):
    """gh --paginate concatenates top-level JSON arrays; yield each."""
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \r\n\t":
            i += 1
        if i >= n:
            break
        obj, end = dec.raw_decode(text, i)
        yield obj
        i = end


def download_artifact(artifact_id, dest):
    """Download a workflow artifact by immutable id and extract it into dest."""
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest.with_suffix(".zip")
    proc = subprocess.run(
        ["gh", "api", f"repos/{REPO}/actions/artifacts/{artifact_id}/zip"],
        capture_output=True)
    if proc.returncode != 0:
        raise releasepub.ReleaseError(f"download artifact {artifact_id}: {proc.stderr.decode().strip()}")
    zip_path.write_bytes(proc.stdout)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if member.startswith("/") or ".." in pathlib.PurePosixPath(member).parts:
                raise releasepub.ReleaseError(f"unsafe artifact member {member}")
        zf.extractall(dest)
    zip_path.unlink()


def success_legs(needs_json):
    """{leg: {artifact_id, manifest_sha256}} for every leg-* need that succeeded
    and carries both outputs."""
    out = {}
    for job, info in json.loads(needs_json).items():
        if not job.startswith("leg-") or info.get("result") != "success":
            continue
        outputs = info.get("outputs") or {}
        aid, msha = outputs.get("artifact_id"), outputs.get("manifest_sha256")
        if aid and msha:
            out[job[len("leg-"):]] = {"artifact_id": aid, "manifest_sha256": msha}
    return out


def _committed_releases(component):
    mpath = REPO_ROOT / f"{component}.json"
    return schema.load(mpath).get("releases", []) if mpath.exists() else []


def publish(needs_json, workdir, api=None, dry=False):
    """Reconcile every success leg's Release; return (finalizable_metas, errors)."""
    api = api or GhReleaseAPI()
    workdir = pathlib.Path(workdir)
    legs = success_legs(needs_json)
    metas, errors = [], []

    for leg, ref in sorted(legs.items()):
        legdir = workdir / leg
        try:
            download_artifact(ref["artifact_id"], legdir)
            handoff.verify(legdir, ref["manifest_sha256"])
        except (releasepub.ReleaseError, handoff.HandoffError) as e:
            errors.append(f"{leg}: artifact verify failed: {e}")
            continue
        for meta_path in sorted(legdir.glob("*.meta.json")):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            tag = f"{meta['component']}-{meta['version']}" + (
                "" if meta["revision"] <= 1 else f"-r{meta['revision']}")
            try:
                members = releasepub.build_members(meta, legdir)
                referenced = releasepub.referenced_asset_names(
                    _committed_releases(meta["component"]), tag)
                prerelease = "-" in meta["version"]
                if not dry:
                    releasepub.reconcile_release(api, tag, prerelease=prerelease,
                                                 members=members, referenced_names=referenced)
                metas.append(meta)
            except releasepub.ReleaseError as e:
                errors.append(f"{tag}: {e}")
    return metas, errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reconcile leg Releases; emit finalizable metas.")
    ap.add_argument("--needs", required=True, help="toJSON(needs) from the workflow")
    ap.add_argument("--workdir", required=True, help="scratch dir for downloaded artifacts")
    ap.add_argument("--out", required=True, help="finalizable-metas output dir")
    ap.add_argument("--dry", action="store_true", help="verify + plan, no Release mutation")
    ap.add_argument("--github-output", help="path to $GITHUB_OUTPUT for the artifact-id gate")
    args = ap.parse_args(argv)

    metas, errors = publish(args.needs, args.workdir, dry=args.dry)

    out = pathlib.Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    if metas:
        out.mkdir(parents=True)
        for i, meta in enumerate(metas):
            (out / f"{i:03d}-{meta['component']}-{meta['version']}.meta.json").write_text(
                json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # The metas are written BEFORE the red exit so finalize still receives the
    # partial success list on a collected per-leg failure (the plan's
    # emit-handoff-first-then-exit-nonzero contract). In workflow mode the flags
    # drive a later gate step; the CLI itself returns 0 so the upload step runs.
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"has_metas={'true' if metas else 'false'}\n")
            fh.write(f"has_errors={'true' if errors else 'false'}\n")

    for e in errors:
        sys.stderr.write(f"publish_legs: ERROR {e}\n")
    sys.stderr.write(f"publish_legs: {len(metas)} finalizable, {len(errors)} error(s)\n")
    if args.github_output:
        return 0
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
