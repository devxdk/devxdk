"""Per-provider newest-version resolvers for the build planner.

Each managed (build/adopt) provider has one resolver that answers "what is the
newest publishable version of this tracked line, per this provider's OWN
source of truth" — the same source the recipe re-verifies against, so plan and
build can never disagree about identity.

ENABLED_PROVIDERS is the phase gate: a provider appears here only when BOTH
its resolver and its recipe exist, so the plan never emits a leg item the leg
job cannot execute (a present-in-map failure is a red run by design). Phase 2
adds astral/edb/theseus (adopt), Phase 3 the unix builds.

Standard library only; network goes through the injectable Fetcher.
"""

from __future__ import annotations

import functools
import os
import re

from . import versions

# Providers whose resolver AND recipe are both implemented (phase-gated).
ENABLED_PROVIDERS = {
    "devxdk-redis-msys2",
    "devxdk-valkey-msys2",
    "devxdk-php-windows",
}

_HASH_LINE = re.compile(r"^hash (\S+)-(\d[\w.\-]*)\.tar\.gz sha256 ([0-9a-f]{64}) (\S+)$")


class ResolveError(RuntimeError):
    """A provider source could not answer the newest-version question."""


def _in_line(ver: str, line_id: str) -> bool:
    """Whether a version belongs to a tracked line (same dotted-shape rule as
    merge.line_for: no dot = major, one dot = major.minor, two = exact)."""
    try:
        v = versions.parse(ver)
    except versions.ParseError:
        return False
    dots = line_id.count(".")
    if dots == 0:
        return v.major_string() == line_id
    if dots == 1:
        return v.major_minor_string() == line_id
    return ver == line_id


def hashes_newest(fetcher, repo: str, ref: str, component: str, line_id: str) -> dict:
    """Newest STABLE version of a line in a redis-style hashes README at the
    PINNED ref. The file is append-ordered (maintenance releases for older
    lines land after newer lines) and carries prerelease entries (-rc1), so
    this version-sorts with the shared comparator and skips prereleases —
    never `tail`."""
    text = fetcher.get_text(f"https://raw.githubusercontent.com/{repo}/{ref}/README")
    best = None
    for raw in text.splitlines():
        m = _HASH_LINE.match(raw.strip())
        if not m or m.group(1) != component:
            continue
        ver, sha, url = m.group(2), m.group(3), m.group(4)
        if not _in_line(ver, line_id):
            continue
        if versions.parse(ver).is_prerelease():
            continue
        if best is None or versions.compare_str(ver, best["source_version"]) > 0:
            best = {
                "source_version": ver,
                "source_sha256": sha,
                "source_url": url.replace("http://", "https://", 1),
            }
    if best is None:
        raise ResolveError(f"no stable {component} release for line {line_id} in {repo}@{ref}")
    return best


def php_windows_newest(fetcher, line_id: str) -> dict:
    """The current version of a PHP branch per windows.php.net's releases.json
    (which lists ONLY the latest release per branch; older builds move to
    archives/, so 'newest' is the only plannable version here). The NTS x64
    variant key is read, never assumed — a vs17->vs18 flip must surface."""
    data = fetcher.get_json("https://downloads.php.net/~windows/releases/releases.json")
    entry = data.get(line_id)
    if not isinstance(entry, dict) or "version" not in entry:
        raise ResolveError(f"php branch {line_id} not in releases.json")
    variants = [k for k in entry if k.startswith("nts-") and k.endswith("-x64")]
    if len(variants) != 1:
        raise ResolveError(f"php {line_id}: expected exactly one nts x64 variant, found {variants}")
    zipinfo = entry[variants[0]].get("zip", {})
    if not zipinfo.get("path") or not zipinfo.get("sha256"):
        raise ResolveError(f"php {line_id}: variant {variants[0]} carries no zip path/sha256")
    return {
        "source_version": entry["version"],
        "source_sha256": zipinfo["sha256"].lower(),
        "source_url": f"https://downloads.php.net/~windows/releases/{zipinfo['path']}",
        "variant": variants[0],
    }


# astral python-build-standalone: install_only asset triples per manifest platform.
ASTRAL_REPO = "astral-sh/python-build-standalone"
_ASTRAL_TRIPLES = {
    "windows/amd64": "x86_64-pc-windows-msvc",
    "linux/amd64": "x86_64-unknown-linux-gnu",
    "darwin/amd64": "x86_64-apple-darwin",
    "darwin/arm64": "aarch64-apple-darwin",
}
_ASTRAL_ASSET = re.compile(r"^cpython-(\d+\.\d+\.\d+)\+\w+-(.+)-install_only\.tar\.gz$")


def _gh_headers() -> dict:
    """GitHub API headers; authenticate when a token is in the environment so the
    paginated asset listing doesn't exhaust the 60/hr anonymous limit (the plan
    job carries GITHUB_TOKEN)."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _astral_digest_sha256(asset: dict) -> str:
    """The published sha256 from an asset's ``digest`` field ("sha256:<hex>"),
    fail-closed on any other/absent algorithm (astral ships no .sha256 sidecar,
    so this is the only authenticated hash)."""
    digest = (asset.get("digest") or "").strip().lower()
    prefix = "sha256:"
    if not digest.startswith(prefix):
        raise ResolveError(f"astral asset {asset.get('name')!r} has no sha256 digest")
    sha = digest[len(prefix):]
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ResolveError(f"astral asset {asset.get('name')!r} has a malformed sha256 digest")
    return sha


def astral_newest(fetcher, line_id: str) -> dict:
    """Newest CPython in the tracked line from astral's python-build-standalone.

    Adopt provider: the manifest references the upstream install_only asset
    directly, verified by the API's published ``digest``. Returns source_version,
    the date-based release_tag, and per-platform {url, sha256, size} for the four
    install_only assets. The release lists hundreds of assets, so the listing is
    paginated to exhaustion, and only a version present on ALL four platforms is
    plannable (a partial upload must never yield a platform-incomplete release)."""
    headers = _gh_headers()
    release = fetcher.get_json(
        f"https://api.github.com/repos/{ASTRAL_REPO}/releases/latest", headers)
    tag = release["tag_name"]
    assets = fetcher.get_json_paginated(
        f"https://api.github.com/repos/{ASTRAL_REPO}/releases/{release['id']}/assets", headers)

    triple_to_key = {v: k for k, v in _ASTRAL_TRIPLES.items()}
    by_version: dict = {}
    for asset in assets:
        m = _ASTRAL_ASSET.match(asset.get("name", ""))
        if not m:
            continue
        ver, triple = m.group(1), m.group(2)
        pkey = triple_to_key.get(triple)
        if pkey is None or not _in_line(ver, line_id):
            continue
        by_version.setdefault(ver, {})[pkey] = asset

    complete = [v for v, plats in by_version.items() if set(plats) == set(_ASTRAL_TRIPLES)]
    if not complete:
        raise ResolveError(
            f"no complete 4-platform cpython {line_id}.x in astral release {tag}")
    version = max(complete, key=functools.cmp_to_key(versions.compare_str))

    platforms = {}
    for pkey, asset in by_version[version].items():
        platforms[pkey] = {
            "url": asset["browser_download_url"],
            "sha256": _astral_digest_sha256(asset),
            "size": asset["size"],
        }
    return {"source_version": version, "release_tag": tag, "platforms": platforms}


def resolve(provider: str, cfg, component: str, line_id: str, fetcher) -> dict:
    """Dispatch to the provider's resolver. Callers gate on ENABLED_PROVIDERS
    first; an unknown-but-enabled provider is a hard error (config/gate drift)."""
    if provider in ("devxdk-redis-msys2", "devxdk-redis-unix"):
        return hashes_newest(fetcher, "redis/redis-hashes",
                             cfg.pins["redis_hashes"]["ref"], "redis", line_id)
    if provider in ("devxdk-valkey-msys2", "devxdk-valkey-unix"):
        return hashes_newest(fetcher, "valkey-io/valkey-hashes",
                             cfg.pins["valkey_hashes"]["ref"], "valkey", line_id)
    if provider == "devxdk-php-windows":
        return php_windows_newest(fetcher, line_id)
    if provider == "astral":
        return astral_newest(fetcher, line_id)
    raise ResolveError(f"no resolver for provider {provider!r}")
