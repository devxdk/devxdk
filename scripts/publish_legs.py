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

from devxdk_manifest import config, handoff, plan, releasepub, schema, strictjson  # noqa: E402

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
        # GET /releases/tags/{tag} returns 404 for DRAFT releases (a draft has no
        # tag ref until published), so a draft we created on a prior attempt would
        # be invisible and re-created endlessly. List releases instead — the list
        # endpoint includes drafts for an authenticated token — and match by
        # tag_name, so draft resumption works.
        for rel in self._list_releases():
            if rel.get("tag_name") == tag:
                return {"id": rel["id"], "draft": rel["draft"], "assets": self._assets(rel["id"])}
        return None

    def _list_releases(self):
        proc = self._api("--paginate", f"repos/{REPO}/releases?per_page=100")
        out = []
        for chunk in _split_json_arrays(proc.stdout):
            out.extend(chunk)
        return out

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
        # Create via the API POST so the RESPONSE carries the new release's id
        # directly. `gh release create` + a re-fetch races GitHub's list
        # eventual-consistency — the just-created draft is briefly absent from
        # GET /releases, and absent from GET /releases/tags/{tag} until published
        # — so neither re-fetch is reliable immediately after create. Component
        # releases are always drafts and never the repo "latest" (make_latest is
        # the string enum "false", not a bool).
        args = ["-X", "POST", f"repos/{REPO}/releases",
                "-f", f"tag_name={tag}", "-F", "draft=true",
                "-f", f"name={tag}", "-f", "body=Automated DevXDK build.",
                "-f", "make_latest=false"]
        if prerelease:
            args += ["-F", "prerelease=true"]
        data = strictjson.loads(self._api(*args).stdout)
        return {"id": data["id"], "draft": data["draft"], "assets": []}

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
        data = strictjson.loads(self._api(f"repos/{REPO}/releases/{release_id}").stdout)
        return data["tag_name"]


def _split_json_arrays(text):
    """gh --paginate concatenates top-level JSON arrays; yield each.

    A GENERATOR, so a strict refusal surfaces at the consumer's `for` loop
    (_list_releases, _assets), not at the call that creates it.
    """
    return strictjson.split_json_arrays(text)


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
    for job, info in strictjson.loads(needs_json).items():
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


# Provider -> the EXACT set of [pins.*] names that provider's leg metadata must
# declare under provenance.static_libs. Fail CLOSED: a missing static_libs
# block, a missing key, or an extra key is an error BEFORE publication, not a
# pass. A presence-only rule would let a recipe edit that drops or renames the
# block turn this control off silently while CI stayed green.
#
# devxdk-nginx-unix is the case that exists today: recipes/nginx.sh statically
# links openssl, pcre2 and zlib into the three unix nginx bundles we ship, so a
# stale pin there is a shipped-binary CVE, not a CI-hygiene item.
#
# Keys must be a subset of resolvers.ENABLED_PROVIDERS (asserted in
# test_publish_legs) so a row naming a provider that no longer exists fails the
# tests rather than sitting dead.
STATIC_PIN_PROVIDERS = {
    "devxdk-nginx-unix": frozenset({"openssl", "pcre2", "zlib"}),
}


def _static_pins():
    """The [pins.*] table from the tree THIS run checked out.

    Its own function so tests can substitute it, mirroring _committed_releases.
    """
    return config.load().pins


def validate_static_pins(meta, pins):
    """Return a list of error strings for meta's provenance.static_libs block.

    What this proves, stated narrowly: the bytes just built used the pins
    committed in the tree this run checked out. It attests nothing about assets
    published earlier -- between publications the currency gate can prove the
    PIN is current and cannot prove the ARTIFACT was built from it, because the
    leg .meta.json is neither published nor committed (releasepub publishes only
    what release_assets declares, and the pending/ledger record keeps identity,
    url, sha256 and size only). Closing that would mean persisting authenticated
    build-input provenance as a third published format, which this work's public
    surface deliberately does not admit. That is why a static-source pin bump is
    an obligation of the bump procedure -- bump the pin, then force-rebuild at
    the next -rN -- and not something left for a scanner to catch later.
    """
    provider = meta.get("provider")
    declared = (meta.get("provenance") or {}).get("static_libs")
    expected = STATIC_PIN_PROVIDERS.get(provider)

    if expected is None:
        # Not in the map: still validated if it declares anything, so a future
        # recipe is covered from its first run. The map gains its row when that
        # recipe lands; the two layers are complementary.
        if declared is None:
            return []
        if not isinstance(declared, dict):
            return ["provider {!r}: provenance.static_libs must be a table, "
                    "got {!r}".format(provider, declared)]
    else:
        if not isinstance(declared, dict):
            return ["provider {!r} must declare provenance.static_libs {}, "
                    "got {!r}".format(provider, sorted(expected), declared)]
        if set(declared) != set(expected):
            return ["provider {!r} declares static_libs {}, want exactly {}".format(
                provider, sorted(declared), sorted(expected))]

    errs = []
    for name in sorted(declared):
        pinned = (pins.get(name) or {}).get("version")
        if pinned is None:
            errs.append("provider {!r}: static_libs names {!r}, which has no "
                        "[pins.{}] version".format(provider, name, name))
            continue
        if declared[name] != pinned:
            errs.append("provider {!r}: built against {} {!r} but [pins.{}] is "
                        "{!r}".format(provider, name, declared[name], name, pinned))
    return errs


def publish(needs_json, workdir, api=None, dry=False):
    """Reconcile every success leg's Release; return (finalizable_metas, errors)."""
    api = api or GhReleaseAPI()
    workdir = pathlib.Path(workdir)
    legs = success_legs(needs_json)
    pins = _static_pins()
    metas, errors = [], []

    for leg, ref in sorted(legs.items()):
        legdir = workdir / leg
        try:
            download_artifact(ref["artifact_id"], legdir)
            handoff.verify(legdir, ref["manifest_sha256"])
        except (releasepub.ReleaseError, handoff.HandoffError) as e:
            errors.append(f"{leg}: artifact verify failed: {e}")
            continue

        # PASS 1 -- parse and validate EVERY meta in this leg before reconciling
        # ANY of them. A leg really can carry several metas: each recipe builds
        # every tracked line for its (component, platform) pair and writes one
        # .meta.json per line (recipes/README.md:4-7), so mariadb has 5 today and
        # php 2. nginx has one line by CONFIGURATION, not by construction -- a
        # second nginx line would falsify a single-pass guarantee with no code
        # change at all. Two passes make the promise true by shape: a mismatched
        # leg uploads no asset and appends no meta, hence no pending record and
        # no manifest entry.
        leg_metas, leg_errors = [], []
        for meta_path in sorted(legdir.glob("*.meta.json")):
            try:
                meta = strictjson.load(meta_path)
            except (OSError, ValueError) as e:
                leg_errors.append(f"{leg}: {meta_path.name}: unreadable: {e}")
                continue
            leg_errors.extend(f"{leg}: {meta_path.name}: {m}"
                              for m in validate_static_pins(meta, pins))
            leg_metas.append(meta)
        if leg_errors:
            # Failure stays PER LEG, using this function's existing idiom, because
            # publish()'s documented contract is that one bad leg still lets the
            # others finalize. Preflighting all LEGS before reconciling any would
            # change that contract; preflighting all METAS within a leg does not.
            errors.extend(leg_errors)
            continue

        # PASS 2 -- reconcile.
        for meta in leg_metas:
            if meta.get("ordering_kind") == "adopted":
                # Adopt re-hosts nothing: no Release, no asset upload. The leg
                # already self-hash-verified the upstream bytes; finalize writes a
                # pending record pointing at the upstream URL. Pass it straight
                # through to the finalizable set.
                metas.append(meta)
                continue
            # plan.release_tag is the ONE tag rule (L37) -- a drift between the
            # publish tag and finalize's download URL would 404 the signed
            # manifest.
            tag = plan.release_tag(meta["component"], meta["version"], meta["revision"])
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
