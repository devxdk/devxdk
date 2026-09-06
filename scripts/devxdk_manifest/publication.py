"""Validated operation identities and publication members, independent of GitHub."""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import urllib.parse

from . import schema, strictjson, versions

IDENTITY = ("component", "line", "version", "platform", "ordering_kind",
            "provider", "epoch", "revision", "source_version")


class PublicationError(ValueError):
    pass


def digest(value, label="sha256"):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise PublicationError(f"{label} must be a lowercase SHA-256")
    return value


def positive(value, label):
    problem = schema.require_positive_int64(value, label)
    if problem:
        raise PublicationError(problem)
    return value


def basename(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value):
        raise PublicationError(f"unsafe publication basename: {value!r}")
    return value


def identity(item):
    if not isinstance(item, dict):
        raise PublicationError("metadata must be an object")
    missing = set(IDENTITY) - item.keys()
    if missing:
        raise PublicationError(f"missing identity fields: {sorted(missing)}")
    for field in ("component", "line", "version", "provider", "source_version"):
        basename(item[field])
    versions.parse(item["version"])
    if item["platform"] not in schema.PLATFORM_ORDER or item["ordering_kind"] not in ("built", "adopted"):
        raise PublicationError("invalid platform or ordering kind")
    positive(item["epoch"], "epoch")
    positive(item["revision"], "revision")
    return tuple(item[field] for field in IDENTITY)


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(encode(value)).hexdigest()


def validate_operation(operation):
    if not isinstance(operation, dict) or operation.get("schema") != 1:
        raise PublicationError("missing or invalid operation schema")
    basename(operation.get("id"))
    if not re.fullmatch(r"[0-9a-f]{40}", operation.get("source_commit", "")):
        raise PublicationError("operation source_commit must be a full Git SHA")
    if not isinstance(operation.get("legs"), dict) or not isinstance(operation.get("targets"), list):
        raise PublicationError("operation must carry legs and coverage targets")
    from . import plan
    seen = set()
    for leg, items in operation["legs"].items():
        if not isinstance(items, list) or not items:
            raise PublicationError(f"{leg}: empty planned leg")
        for item in items:
            key = identity(item)
            if key in seen or leg != plan.leg_id(item["component"], item["platform"]):
                raise PublicationError(f"duplicate or misplaced plan item in {leg}")
            seen.add(key)
            if item.get("mode") not in ("build", "finalize-only"):
                raise PublicationError(f"{leg}: invalid plan mode")
    targets = {}
    for target in operation["targets"]:
        key = identity(target)
        if key in targets:
            raise PublicationError('duplicate operation target')
        platforms = target.get('coverage_platforms')
        if not isinstance(platforms, list) or not platforms or any(p not in schema.PLATFORM_ORDER for p in platforms):
            raise PublicationError('coverage targets must declare their platform set')
        if target['platform'] not in platforms or len(platforms) != len(set(platforms)):
            raise PublicationError('invalid coverage platform set')
        targets[key] = target
    if seen - targets.keys():
        raise PublicationError('planned work is missing from operation targets')
    return operation


def parse_needs(text):
    needs = strictjson.loads(text)
    if not isinstance(needs, dict) or needs.get("plan", {}).get("result") != "success":
        raise PublicationError("a successful plan is required")
    raw = needs["plan"].get("outputs", {}).get("operation")
    if not raw:
        raise PublicationError("plan operation output is missing")
    return needs, validate_operation(strictjson.loads(raw))


def validate_metas(items, metas):
    expected = {identity(item): item for item in items}
    got = {}
    for meta in metas:
        key = identity(meta)
        if key not in expected:
            raise PublicationError(f"unexpected metadata identity: {key}")
        if key in got:
            raise PublicationError(f"duplicate metadata identity: {key}")
        digest(meta.get("sha256"))
        positive(meta.get("size_bytes"), "size_bytes")
        item = expected[key]
        # PHP source bytes are frozen by the plan as well as by the recipe.
        if item.get("source_sha256") and item["component"] == "php":
            provenance = meta.get("provenance") or {}
            source_sha = provenance.get("source_sha256") or provenance.get("official_zip_sha256")
            if source_sha != item["source_sha256"]:
                raise PublicationError(f"{key}: source digest differs from the plan")
        got[key] = meta
    missing = expected.keys() - got.keys()
    if missing:
        raise PublicationError(f"missing planned metadata: {sorted(missing)}")
    return [got[key] for key in sorted(got)]


def members(meta, directory):
    """Bind every declared member to a local authenticated file before upload."""
    from . import plan, handoff
    if meta["ordering_kind"] == "adopted":
        from .allowlist import host_allowed
        url = urllib.parse.urlsplit(meta.get("url", ""))
        if url.scheme != "https" or not url.hostname or url.username or url.password or not host_allowed(url.hostname):
            raise PublicationError("adopted URL is not allowed")
        return []
    ext = "zip" if meta["platform"].startswith("windows/") else "tar.gz"
    archive = plan.archive_name(meta["component"], meta["version"], meta["revision"], meta["platform"], ext)
    if meta.get("archive") != archive:
        raise PublicationError(f"archive must be {archive}")
    declared = meta.get("release_assets")
    if declared is None:
        declared = [{"name": archive, "sha256": meta["sha256"], "object_code": True}]
    if not isinstance(declared, list) or not declared:
        raise PublicationError("empty release_assets")
    out, names = [], set()
    for entry in declared:
        if not isinstance(entry, dict):
            raise PublicationError("invalid release member")
        name = basename(entry.get("name"))
        if name in names or name == "manifest.json":
            raise PublicationError(f"duplicate or reserved release member: {name}")
        names.add(name)
        path = pathlib.Path(directory) / name
        sha = digest(entry.get("sha256"))
        if not isinstance(entry.get("object_code"), bool):
            raise PublicationError("object_code must be a boolean")
        if path.is_symlink() or not path.is_file() or handoff.sha256_file(path) != sha:
            raise PublicationError(f"{name}: missing or mismatched verified publication member")
        size = positive(path.stat().st_size, name)
        out.append({"name": name, "sha256": sha, "size_bytes": size, "object_code": entry["object_code"]})
    archives = [m for m in out if m["name"] == archive]
    if len(archives) != 1 or not archives[0]["object_code"] or archives[0]["sha256"] != meta["sha256"] or archives[0]["size_bytes"] != meta["size_bytes"]:
        raise PublicationError("archive is missing or differs from its metadata")
    return sorted(out, key=lambda m: (m["object_code"], m["name"]))


def download_url(meta):
    from . import plan
    if meta["ordering_kind"] == "adopted":
        return meta["url"]
    tag = plan.release_tag(meta["component"], meta["version"], meta["revision"])
    return f"https://github.com/devxdk/devxdk/releases/download/{tag}/{meta['archive']}"


def pending_record(meta):
    from .pending import PendingRecord
    return PendingRecord(**{field: meta[field] for field in IDENTITY},
                         url=download_url(meta), sha256=meta["sha256"], size_bytes=meta["size_bytes"])
