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

# Canonical per-release and per-asset key order (mirrors the committed manifests
# and internal/manifest's struct tags).
RELEASE_FIELDS = ("version", "channel", "released_at", "platforms")
ASSET_FIELDS = ("url", "sha256", "size_bytes")


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
    trailing newline, insertion-ordered keys)."""
    return json.dumps(data, indent=2) + "\n"


def write(path, data: dict) -> None:
    """Write a manifest to disk with LF newlines and no BOM."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dump_str(data))


def load(path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def is_component_manifest(data) -> bool:
    """The shape scrape-and-sign gates signing on: a top-level object carrying
    both "kind" and "releases". A stray root JSON (config, state) is not signed."""
    return isinstance(data, dict) and "kind" in data and "releases" in data
