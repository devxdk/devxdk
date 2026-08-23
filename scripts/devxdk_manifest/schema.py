"""The component-manifest JSON model and its byte-stable serialization.

The serialization here is load-bearing: replacing gen-manifest.py must not change
one byte of the client-visible manifests, and the daily scrape must produce a
zero diff when nothing upstream changed. So the field order below mirrors the
committed manifests exactly (Python dicts preserve insertion order; json.dump
does NOT sort keys), and dump_str is fixed at ``indent=2`` + a trailing newline,
written with LF endings — identical to gen-manifest.py's write_manifest.
"""

from __future__ import annotations

import json
import pathlib

from .strictjson import loads as strict_loads

# The largest value Go's int64 holds. Python's ints are arbitrary-precision and
# Go's are not, so the bound is required, not stylistic: without it a manifest
# carrying 2**63 validates here, gets SIGNED, and then fails json.Unmarshal on
# every client.
INT64_MAX = 9223372036854775807

# Canonical per-release and per-asset key order (mirrors the committed manifests
# and internal/manifest's struct tags).
RELEASE_FIELDS = ("version", "channel", "released_at", "platforms")
ASSET_FIELDS = ("url", "sha256", "size_bytes")

# Where "revision" sits in a component manifest. A FIXED slot, not an append:
# dump_str is insertion-ordered and the committed byte layout is asserted by
# tests, so the bytes would otherwise depend on dict construction order.
REVISION_AFTER = "kind"


class SchemaError(ValueError):
    """A document that is well-formed JSON but not a manifest we may write."""


def require_positive_int64(value, label):
    """Return None when `value` is a JSON integer Go unmarshals into an int64
    and the domain accepts (>= 1), else a human-readable reason.

    Returns rather than raises because validate_manifests collects reasons into
    a list while schema.write must fail closed — one definition either way. The
    bool exclusion is required, not stylistic: isinstance(True, int) is True in
    Python, so `true` would otherwise read as revision 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{label} must be an integer, got {value!r}"
    if not 1 <= value <= INT64_MAX:
        return f"{label} must be in [1, {INT64_MAX}], got {value}"
    return None

# Canonical platform ordering inside a release's "platforms" map. gen-manifest.py
# emitted platforms in this order (NODE_PLATFORMS / GO_PLATFORMS), so recomposing
# a manifest from state must reproduce it or every scrape would reorder platforms
# and churn a diff.
PLATFORM_ORDER = (
    "windows/amd64",
    "linux/amd64",
    "darwin/amd64",
    "darwin/arm64",
    "darwin/universal",
    "any",
)


def order_platforms(platforms: dict) -> dict:
    """Return platforms reordered by the canonical PLATFORM_ORDER (unknown keys
    appended in sorted order, so an unexpected key is deterministic, not lost)."""
    ordered = {}
    for k in PLATFORM_ORDER:
        if k in platforms:
            ordered[k] = platforms[k]
    for k in sorted(platforms):
        if k not in ordered:
            ordered[k] = platforms[k]
    return ordered


def asset(url: str, sha256: str, size_bytes: int) -> dict:
    """Build one platform asset in canonical field order."""
    return {"url": url, "sha256": sha256, "size_bytes": size_bytes}


def release(version: str, channel: str, released_at: str, platforms: dict) -> dict:
    """Build one release in canonical field order. `platforms` maps a platform
    key to an asset() dict; insertion order of the platforms dict is preserved."""
    return {
        "version": version,
        "channel": channel,
        "released_at": released_at,
        "platforms": platforms,
    }


def component(name: str, display_name: str, kind: str, releases: list) -> dict:
    """Build a component manifest in canonical field order."""
    return {
        "name": name,
        "display_name": display_name,
        "kind": kind,
        "releases": releases,
    }


def dump_str(data: dict) -> str:
    """Serialize a manifest to the exact committed byte layout (LF, indent=2,
    trailing newline, insertion-ordered keys).

    allow_nan=False so the writer moves with the reader: the default emits
    NaN/Infinity, which strictjson refuses, and the failure would surface one
    run away from its cause.
    """
    return json.dumps(data, indent=2, allow_nan=False) + "\n"


def with_revision(data: dict, revision: int) -> dict:
    """Rebuild `data` with `revision` in its fixed slot, just after "kind"."""
    out = {}
    for key, value in data.items():
        if key == "revision":
            continue
        out[key] = value
        if key == REVISION_AFTER:
            out["revision"] = revision
    if "revision" not in out:
        out["revision"] = revision
    return out


def next_revision(path, data: dict) -> int:
    """The revision `data` must carry when written to `path`.

    THE COMPARISON IS BYTE IDENTITY AGAINST THE PRIOR FILE'S ACTUAL BYTES, and
    that is the single most defect-prone decision in this design. The client's
    mark holds a SHA-256 over the raw fetched body, so the invariant it depends
    on is: the published bytes change <=> the revision increments. A semantic
    comparison breaks that in one direction — dump_str's output is a function of
    dict CONSTRUCTION ORDER (reorder PLATFORM_ORDER or RELEASE_FIELDS and you
    get identical semantics with different bytes), so it would preserve the
    revision while the bytes moved, and every client holding a mark would see
    equal revision + different hash and refuse that component permanently.

    read_bytes(), never a text read: schema.load's universal-newlines mode
    translates CRLF to LF, so a CRLF prior would compare EQUAL to an LF
    candidate and produce that same permanent lockout through a second door.

    Compared against the prior's RAW bytes rather than a re-dump of the parsed
    prior, for a third door: a hand-edited or differently-formatted prior would
    be silently normalized by a re-dump, so the comparison would say "unchanged"
    while the published bytes changed. A non-canonical prior must read as
    CHANGED and take an increment.
    """
    path = pathlib.Path(path)
    try:
        prior_bytes = path.read_bytes()
    except FileNotFoundError:
        return 1
    prior = strict_loads(prior_bytes)
    if not isinstance(prior, dict):
        raise SchemaError(f"{path.name}: prior is not a JSON object")
    reason = require_positive_int64(prior.get("revision"), f"{path.name}: prior revision")
    if reason is not None:
        # NEVER reset to 1 on a malformed prior: that is a silent rollback for
        # every client already holding a mark for this component.
        raise SchemaError(reason)
    prior_revision = prior["revision"]
    candidate = dump_str(with_revision(data, prior_revision)).encode("utf-8")
    if candidate == prior_bytes:
        return prior_revision
    return prior_revision + 1


def resolve(path, data: dict) -> dict:
    """The exact document write() would produce, without writing anything.

    Multi-manifest callers PREFLIGHT with this. next_revision now raises on a
    malformed prior, and those callers save their state and ledger only AFTER
    their loop — so a raise on the SECOND component would leave the first
    manifest already rewritten with neither ledger saved, breaking the
    "FAILED (nothing written)" contract they advertise in their own handlers.
    """
    return with_revision(data, next_revision(path, data))


def write_resolved(path, resolved: dict) -> None:
    """Write a document already resolved by resolve(), byte-exactly."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dump_str(resolved))


def write(path, data: dict) -> None:
    """Resolve and write in one step — the single-manifest path.

    Revision assignment lives at this boundary and not in component(): that
    constructor takes no path and no prior manifest, so it structurally cannot
    preserve or increment a counter.
    """
    write_resolved(path, resolve(path, data))


def loads(raw) -> dict:
    """Strict-parse manifest bytes (or text). See devxdk_manifest.strictjson."""
    return strict_loads(raw)


def load(path) -> dict:
    """Strict-parse a manifest file. read_bytes so there is exactly one read and
    no universal-newlines translation."""
    return strict_loads(pathlib.Path(path).read_bytes())


def is_component_manifest(data) -> bool:
    """The shape scrape-and-sign gates signing on: a top-level object carrying
    both "kind" and "releases". A stray root JSON (config, state) is not signed."""
    return isinstance(data, dict) and "kind" in data and "releases" in data
