"""Member-digest handoff for the build-runtimes leg -> publish contract.

A build leg uploads ONE workflow artifact (its component archives + .meta.json
files) named by its static leg id. Artifact NAMES are substitutable: upload-artifact
mints a fresh id on overwrite, and the run-scoped artifact token would let a
compromised sibling leg rewrite a same-named artifact — so publish consumes each
leg by its IMMUTABLE artifact-id and must re-verify the bytes it downloaded. This
module is that check. The leg writes a canonical ``manifest.json`` digesting every
OTHER member, and its own sha256 crosses to publish as a JOB OUTPUT (the
orchestration plane a sibling job cannot forge). publish verifies manifest.json
against that hash, then every member's digest and size, before a single byte
reaches a Release.

The leg members are FLAT files — component archives that publish uploads verbatim
and never extracts, plus their .meta.json — so this is a one-level digest manifest.
The app repo's two-level (outer artifact / inner tar) handoff exists there because
its members include the .app bundle and the tools tarball, which must be safely
extracted; nothing here is extracted, so Level B is not needed. ``manifest.json``
is a reserved name excluded from its own member list. Hashing only; standard
library (the app repo's Go handoff CLI is not reachable from this repo).
"""

from __future__ import annotations

import hashlib
import json
import pathlib

MANIFEST_NAME = "manifest.json"
SCHEMA = 1
_CHUNK = 1 << 20


class HandoffError(Exception):
    """A leg artifact does not match its member-digest manifest."""


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_members(directory: pathlib.Path):
    """Every regular file under `directory` except the reserved manifest, as
    (posix-relative-path, path) pairs sorted by path. A symlink or any
    non-regular entry is a hard error: leg artifacts are plain files, and
    following a symlink would let a member's bytes escape the artifact."""
    members = []
    for path in sorted(directory.rglob("*"), key=lambda p: p.relative_to(directory).as_posix()):
        rel = path.relative_to(directory).as_posix()
        if path.is_symlink():
            raise HandoffError(f"symlink member is not allowed: {rel}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise HandoffError(f"non-regular member is not allowed: {rel}")
        if rel == MANIFEST_NAME:
            continue
        members.append((rel, path))
    return members


def generate(directory) -> dict:
    """Build the member-digest manifest for a leg directory (does not write it)."""
    directory = pathlib.Path(directory)
    if not directory.is_dir():
        raise HandoffError(f"not a directory: {directory}")
    members = [
        {"type": "file", "path": rel, "sha256": sha256_file(path), "size": path.stat().st_size}
        for rel, path in _file_members(directory)
    ]
    if not members:
        raise HandoffError(f"leg directory has no members: {directory}")
    return {"schema": SCHEMA, "members": members}


def dump_str(manifest: dict) -> str:
    """Canonical serialization: sorted keys, indent=2, trailing LF. The members
    list is already path-sorted by generate(), so two writes of the same tree
    produce identical bytes and therefore an identical manifest sha256."""
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def write(directory) -> str:
    """Generate and write ``manifest.json`` into `directory`; return its sha256
    (the value the leg exports as a job output for publish to verify against)."""
    directory = pathlib.Path(directory)
    text = dump_str(generate(directory))
    (directory / MANIFEST_NAME).write_text(text, encoding="utf-8", newline="\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verify(directory, expected_sha256: str | None = None) -> dict:
    """Verify a downloaded leg artifact and return its parsed manifest.

    Checks, in order: manifest.json exists; its own sha256 equals the job-output
    hash (when given); it parses and carries the expected schema; every declared
    member has a normalized, root-confined path, is a present regular file, and
    matches the declared size and sha256; and the declared set EXACTLY equals the
    files actually present (no undeclared extra, caught via _file_members, which
    also rejects any symlink). Raises HandoffError on any mismatch."""
    directory = pathlib.Path(directory)
    mpath = directory / MANIFEST_NAME
    if not mpath.is_file():
        raise HandoffError(f"{MANIFEST_NAME} is missing in {directory}")

    raw = mpath.read_bytes()
    if expected_sha256 is not None:
        got = hashlib.sha256(raw).hexdigest()
        if got != expected_sha256.strip().lower():
            raise HandoffError(f"{MANIFEST_NAME} sha256 {got} != expected {expected_sha256.strip().lower()}")

    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HandoffError(f"{MANIFEST_NAME} is not valid JSON: {e}") from e
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise HandoffError(f"{MANIFEST_NAME} schema is not {SCHEMA}")
    declared = manifest.get("members")
    if not isinstance(declared, list) or not declared:
        raise HandoffError(f"{MANIFEST_NAME} members is not a non-empty list")

    seen = set()
    for entry in declared:
        if not isinstance(entry, dict) or entry.get("type") != "file":
            raise HandoffError(f"invalid member entry: {entry!r}")
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            raise HandoffError(f"member has no path: {entry!r}")
        if rel == MANIFEST_NAME:
            raise HandoffError(f"{MANIFEST_NAME} must not list itself as a member")
        norm = pathlib.PurePosixPath(rel)
        if norm.is_absolute() or ".." in norm.parts or rel != norm.as_posix():
            raise HandoffError(f"member path is not a normalized relative path: {rel}")
        if rel in seen:
            raise HandoffError(f"duplicate member path: {rel}")
        seen.add(rel)

        member_path = directory / norm
        if member_path.is_symlink() or not member_path.is_file():
            raise HandoffError(f"member is missing or not a regular file: {rel}")
        size = member_path.stat().st_size
        if size != entry.get("size"):
            raise HandoffError(f"member {rel} size {size} != declared {entry.get('size')}")
        digest = sha256_file(member_path)
        declared_sha = str(entry.get("sha256", "")).lower()
        if digest != declared_sha:
            raise HandoffError(f"member {rel} sha256 {digest} != declared {declared_sha}")

    actual = {rel for rel, _ in _file_members(directory)}
    undeclared = actual - seen
    if undeclared:
        raise HandoffError(f"undeclared members present in artifact: {sorted(undeclared)}")
    return manifest
