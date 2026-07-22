"""Composer scrape adapter — newest stable in the tracked 2.x line.

getcomposer.org/versions is DISCOVERY ONLY (each entry carries a version + path,
never a hash or size — verified live), so the sha256 comes from the official
per-version ``composer.phar.sha256sum`` file and the size from a HEAD/ranged
double-fetch. Composer ships as a raw ``.phar`` consumed without extraction, so
its single platform key is ``any`` — matching the client's composer path and the
committed composer.json this reproduces byte-for-byte.
"""

from __future__ import annotations

from .. import schema

BASE = "https://getcomposer.org"
VERSIONS_URL = f"{BASE}/versions"
DEFAULT_LINE_PREFIX = "2."


def _checksum(body: str) -> str:
    """Parse the digest from a ``<sha256>  composer.phar`` .sha256sum body,
    fail-closed on anything that is not a lowercase 64-hex string (the manifest
    validator requires lowercase hex; an absent/garbage file must never yield a
    silently-wrong hash)."""
    tokens = body.split()
    if not tokens:
        raise RuntimeError("empty composer.phar.sha256sum")
    sha = tokens[0].strip().lower()
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise RuntimeError(f"malformed sha256 in composer.phar.sha256sum: {tokens[0]!r}")
    return sha


def build(fetcher, line_prefix: str = DEFAULT_LINE_PREFIX) -> dict:
    versions = fetcher.get_json(VERSIONS_URL)
    stable = versions.get("stable") if isinstance(versions, dict) else None
    if not stable:
        raise RuntimeError("getcomposer.org/versions has no 'stable' list")

    # The stable list is newest-first; take the newest entry in the tracked line.
    chosen = None
    for entry in stable:
        version = entry.get("version", "")
        if version.startswith(line_prefix):
            chosen = entry
            break
    if chosen is None:
        raise RuntimeError(f"no stable composer release in the {line_prefix}x line")

    version = chosen["version"]
    path = chosen.get("path") or f"/download/{version}/composer.phar"
    if not path.startswith("/"):
        raise RuntimeError(f"unexpected composer download path: {path!r}")
    url = BASE + path

    sha = _checksum(fetcher.get_text(url + ".sha256sum"))
    size = fetcher.remote_size(url)

    # No release date in the discovery feed; released_at stays empty (matches the
    # committed manifest — the client tolerates an empty released_at).
    platforms = {"any": schema.asset(url, sha, size)}
    return schema.component(
        "composer", "Composer", "runtime",
        [schema.release(version, "stable", "", platforms)],
    )
