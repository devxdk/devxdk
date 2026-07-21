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
    raise ResolveError(f"no resolver for provider {provider!r}")
