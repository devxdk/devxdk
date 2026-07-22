"""Component-Release asset reconciliation for build-runtimes' publish job.

A build leg uploads its verified bundle as a workflow artifact; publish downloads
it by immutable id, authenticates it (handoff.py), and reconciles the component
GitHub Release `<component>-<version>[-rN]` so the object-code archive and its
corresponding-source assets land immutably. This module is the reconciliation
ENGINE, driven through an injectable ReleaseAPI so the whole contract is
fake-tested without touching GitHub; publish_legs.py backs the API with `gh`.

Rules (from the plan):
  * a new Release is created as a DRAFT and undrafted only after every member is
    present and digest-verified — an interrupted run never exposes a public
    object-code archive without its source (AGPL §6);
  * within a release, corresponding-source/license assets upload BEFORE the
    object-code archive (`object_code` flag; members arrive pre-ordered);
  * an existing asset whose digest matches the leg's verified bytes is ADOPTED
    (published bytes win); a REFERENCED asset (already named by a committed
    manifest tuple) is immutable — a missing or mismatched referenced member is
    a hard error (publish the next revision), never a delete;
  * an UNREFERENCED `starter`-state remnant (GitHub's failed-upload leftover)
    is deleted and re-uploaded; an unreferenced non-starter mismatch is a hard
    error (never silently replace public bytes);
  * every upload is digest-verified (asset API `digest`, else download+rehash).

Standard library only.
"""

from __future__ import annotations

import hashlib


class ReleaseError(RuntimeError):
    """A Release reconciliation invariant failed (fail closed)."""


def asset_sha256(api, asset) -> str:
    """The asset's sha256: parse the API `digest` (algorithm-prefixed) when it is
    sha256, else download the bytes and hash them (the plan's digest fallback)."""
    digest = asset.get("digest") or ""
    algo, _, hexval = digest.partition(":")
    if algo == "sha256" and len(hexval) == 64:
        return hexval.lower()
    data = api.download_asset(asset)
    return hashlib.sha256(data).hexdigest()


def _is_starter(asset) -> bool:
    """GitHub's documented failed-upload remnant: state 'starter' (uploaded
    false), safe to delete. A zero-byte asset with no usable digest is the same
    class when the API does not surface state."""
    if asset.get("state") == "starter":
        return True
    return asset.get("size", 0) == 0 and not asset.get("digest")


def reconcile_release(api, tag, *, prerelease, members, referenced_names):
    """Reconcile ONE component Release to hold exactly `members`, immutably.

    `members` is the pre-ordered list of (name, local_path, sha256, object_code)
    — source/license first, object-code last. `referenced_names` is the set of
    asset names already bound by a committed manifest tuple (immutable). Returns
    the action log; raises ReleaseError on any immutability violation."""
    # Object-code-last is a hard invariant, not a convention — assert the caller
    # ordered correctly so a future caller bug cannot expose code before source.
    seen_object = False
    for name, _p, _s, object_code in members:
        if seen_object and not object_code:
            raise ReleaseError(f"{tag}: source member {name} ordered after object code")
        seen_object = seen_object or object_code

    rel = api.get_release(tag)
    created = rel is None
    if created:
        rel = api.create_release(tag, prerelease=prerelease)
    draft = rel.get("draft", False)
    existing = {a["name"]: a for a in rel.get("assets", [])}
    actions = []

    for name, path, want_sha, _object_code in members:
        cur = existing.get(name)
        if cur is not None:
            # A starter remnant has no usable bytes to hash — a referenced name
            # can never be in starter state (it was verified before the manifest
            # bound it), so an unreferenced starter is always a deletable
            # failed-upload leftover; delete and re-upload without hashing.
            if _is_starter(cur) and name not in referenced_names:
                api.delete_asset(cur["id"])
                actions.append(("delete-remnant", name))
                cur = None
        if cur is not None:
            got = asset_sha256(api, cur)
            if got == want_sha:
                actions.append(("adopt", name))
                continue
            # digest mismatch on an existing (non-starter) asset
            if name in referenced_names:
                raise ReleaseError(
                    f"{tag}: referenced asset {name} digest {got} != verified {want_sha} "
                    f"(published bytes are immutable — publish the next revision)")
            if not draft:
                raise ReleaseError(
                    f"{tag}: unreferenced published asset {name} digest mismatch "
                    f"(never replace public bytes — publish the next revision)")
            # Draft phase: a mismatched unreferenced asset from a prior failed
            # attempt is freely replaceable.
            api.delete_asset(cur["id"])
            actions.append(("delete-remnant", name))
        up = api.upload_asset(rel["id"], name, path)
        got = asset_sha256(api, up)
        if got != want_sha:
            raise ReleaseError(f"{tag}: uploaded {name} digest {got} != verified {want_sha}")
        actions.append(("upload", name))

    # Cleanup: an UNREFERENCED starter remnant NOT in our member set is deletable
    # (a prior failed upload); anything else is left untouched.
    member_names = {m[0] for m in members}
    for name, asset in sorted(existing.items()):
        if name in member_names or name in referenced_names:
            continue
        if _is_starter(asset):
            api.delete_asset(asset["id"])
            actions.append(("cleanup-starter", name))

    if draft:
        api.publish_release(rel["id"])
        actions.append(("undraft", tag))
    return actions


def build_members(meta, stage_dir):
    """Ordered (name, path, sha256, object_code) release members for one leg
    version. A meta may declare `release_assets` explicitly (source/license
    entries with object_code=false first, the archive last); otherwise the sole
    member is the object-code archive. The engine re-asserts the ordering, so a
    malformed meta cannot expose object code before its source."""
    import pathlib

    stage_dir = pathlib.Path(stage_dir)
    if meta.get("ordering_kind") == "adopted":
        return []  # adopt references the upstream asset by URL — nothing to rehost
    declared = meta.get("release_assets")
    if declared:
        members = []
        for a in declared:
            name = a["name"]
            members.append((name, stage_dir / name, a["sha256"].lower(), bool(a["object_code"])))
        # Source/license (object_code false) first, archive last — stable.
        members.sort(key=lambda m: m[3])
        return members
    return [(meta["archive"], stage_dir / meta["archive"], meta["sha256"].lower(), True)]


def referenced_asset_names(manifest_releases, tag):
    """Asset basenames referenced by committed manifest tuples for this release
    tag — i.e. any platform URL of the form .../releases/download/<tag>/<name>.
    Those assets are immutable; a leg may never delete or replace them."""
    needle = f"/releases/download/{tag}/"
    names = set()
    for rel in manifest_releases:
        for asset in rel.get("platforms", {}).values():
            url = asset.get("url", "")
            idx = url.find(needle)
            if idx != -1:
                names.add(url[idx + len(needle):])
    return names
