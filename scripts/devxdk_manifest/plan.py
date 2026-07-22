"""Build-leg planning: the static (component, platform) leg set and revisions.

build-runtimes uses STATIC per-(component, platform) leg jobs, not a dynamic
matrix (only static jobs can export the per-leg immutable-ID handoff a compromised
sibling leg cannot forge). The caller-job list in build-runtimes.yml is therefore
a second copy of the (component, platform) truth; check_static_job_parity binds
it to tracked-versions.toml in both directions so a config addition without its
job, or a stale job after a config deletion, fails CI.

Standard library only.
"""

from __future__ import annotations

import re


def leg_id(component: str, platform: str) -> str:
    """The stable leg id, e.g. redis + windows/amd64 -> redis-windows-amd64."""
    return f"{component}-{platform.replace('/', '-')}"


def static_legs(cfg):
    """Every managed (build/adopt) (component, platform) pair — one leg each,
    regardless of how many lines the pair spans."""
    return sorted({(c, p) for c, _l, p, _plat in cfg.managed_keys()})


def static_leg_ids(cfg):
    return sorted({leg_id(c, p) for c, p in static_legs(cfg)})


_JOB_RE = re.compile(r"^  (leg-[a-z0-9-]+):", re.MULTILINE)


def check_static_job_parity(cfg, workflow_text: str):
    """Bind build-runtimes.yml's `leg-*` caller jobs to the config in both
    directions. Returns a list of errors ([] = in parity)."""
    job_ids = set(_JOB_RE.findall(workflow_text))
    expected = {f"leg-{lid}" for lid in static_leg_ids(cfg)}
    errors = []
    for missing in sorted(expected - job_ids):
        errors.append(f"build-runtimes.yml missing caller job {missing}")
    for extra in sorted(job_ids - expected):
        errors.append(f"build-runtimes.yml has a stale caller job {extra} (no configured managed pair)")
    return errors


def next_revision(existing_revisions) -> int:
    """The next unused DevXDK revision (1 = no suffix; 2 = -r2; ...)."""
    used = set(existing_revisions)
    r = 1
    while r in used:
        r += 1
    return r


# Per-platform runner labels (mirrors the static caller jobs in build-runtimes.yml).
RUNNERS = {
    "windows/amd64": "windows-2022",
    "linux/amd64": "ubuntu-22.04",
    "darwin/arm64": "macos-15",
    "darwin/amd64": "macos-15-intel",
}


def recipe_for(provider: str) -> str:
    """Recipe id from the provider id (devxdk-redis-msys2 -> redis-msys2)."""
    return provider.removeprefix("devxdk-")


class PlanError(RuntimeError):
    """The plan found an inconsistent published state (fail closed)."""


def published_revisions(release_assets, component, version, platform, cap=50):
    """Which DevXDK revisions of (component, version, platform) already have
    their archive published as a Release asset. Revisions are allocated
    sequentially, so probe upward and stop at the first absent release; the
    cap only bounds a pathological store."""
    found = set()
    ext = "zip" if platform.startswith("windows/") else "tar.gz"
    for r in range(1, cap + 1):
        assets = release_assets(release_tag(component, version, r))
        if assets is None:
            break
        if archive_name(component, version, r, platform, ext) in assets:
            found.add(r)
        # A release for the tag can exist while THIS platform's asset is
        # still missing (multi-platform version) — keep probing revisions.
    return found


def decide(*, manifest_has, ledger_rec, pending_exists, revisions, force):
    """Mode + revision for one (component, line, platform, version).

    Returns (mode, revision) or None to skip. The immutability guard lives
    upstream of this table: a manifest tuple without its ledger entry is a
    PlanError, not a plannable state."""
    if manifest_has and ledger_rec is None:
        raise PlanError("manifest carries the tuple but the ledger has no active entry")
    if pending_exists:
        return None  # finalize already queued it; the next scrape applies it
    if manifest_has:
        if force:
            return ("build", next_revision(revisions))  # deliberate repack -> next -rN
        return None  # published and applied — nothing to do
    if revisions:
        if force:
            return ("build", next_revision(revisions))
        return ("finalize-only", max(revisions))  # asset public, manifest lagging
    return ("build", next_revision(revisions))


def _manifest_has(repo_root, component, version, platform):
    import pathlib

    from . import schema

    mpath = pathlib.Path(repo_root) / f"{component}.json"
    if not mpath.exists():
        return False
    for rel in schema.load(mpath).get("releases", []):
        if rel.get("version") == version and platform in rel.get("platforms", {}):
            return True
    return False


def _pending_exists(repo_root, component, version, platform):
    import pathlib

    plat = platform.replace("/", "-")
    pending = pathlib.Path(repo_root) / "pending"
    if not pending.is_dir():
        return False
    prefix = f"{component}-{version}"
    return any(p.name.startswith(prefix) and p.name.endswith(f"-{plat}.json")
               for p in pending.glob("*.json"))


def build_leg_map(cfg, repo_root, fetcher, release_assets, *,
                  components=None, platforms=None, version_override=None, force=False):
    """Resolve every enabled managed (component, line, platform) into leg items.

    Returns {leg_id: [item, ...]} containing ONLY legs with work. Providers
    outside resolvers.ENABLED_PROVIDERS are omitted (their recipe does not
    exist yet — an emitted item would be a guaranteed red leg); filters narrow
    a dispatch to chosen components/platforms; version_override pins the
    version instead of resolving newest (single-component dispatches only);
    force publishes the next unused revision even when up to date."""
    from . import merge, resolvers

    ledger = merge.LedgerState.load(f"{repo_root}/state/asset-revisions.json")
    resolved_cache = {}
    legs = {}

    for component, line_id, platform, plat in sorted(cfg.managed_keys()):
        if components and component not in components:
            continue
        if platforms and platform not in platforms:
            continue
        if plat.provider not in resolvers.ENABLED_PROVIDERS:
            continue

        cache_key = (plat.provider, component, line_id)
        if cache_key not in resolved_cache:
            resolved_cache[cache_key] = resolvers.resolve(
                plat.provider, cfg, component, line_id, fetcher)
        src = resolved_cache[cache_key]
        # The manifest version can differ from the ordering key: postgres is
        # MAJOR.MINOR in the manifest (validator-enforced) while its ordering key
        # is the full upstream version (18.4.0), so a later 18.4.x build
        # supersedes an earlier one. Providers that don't set manifest_version
        # (redis/php/python) keep version == source_version unchanged.
        version = version_override or src.get("manifest_version") or src["source_version"]
        source_version = version if version_override else src["source_version"]
        if version_override and not _in_line_of(cfg, component, line_id, version):
            continue  # an override targets exactly one line; others skip

        rec = ledger.get(component, version, platform)
        if rec is not None and (rec.status != "active" or rec.revoked):
            continue  # tombstoned/revoked: retirement or revocation owns this key
        revisions = published_revisions(release_assets, component, version, platform)
        try:
            outcome = decide(
                manifest_has=_manifest_has(repo_root, component, version, platform),
                ledger_rec=rec,
                pending_exists=_pending_exists(repo_root, component, version, platform),
                revisions=revisions,
                force=force,
            )
        except PlanError as e:
            raise PlanError(f"{component} {version} {platform}: {e}") from e
        if outcome is None:
            continue
        mode, revision = outcome
        legs.setdefault(leg_id(component, platform), []).append({
            "component": component,
            "version": version,
            "revision": revision,
            "line": line_id,
            "platform": platform,
            "runner": RUNNERS[platform],
            "recipe": recipe_for(plat.provider),
            "mode": mode,
            "ordering_kind": plat.ordering_kind,
            "provider": plat.provider,
            "epoch": plat.epoch,
            "source_version": source_version,
        })
    return legs


def _in_line_of(cfg, component, line_id, version):
    from . import merge

    return merge.line_for(cfg, component, version) == line_id


def archive_name(component, version, revision, platform, ext):
    """Canonical archive filename: <component>-<version>[-rN]-<goos>-<goarch>.<ext>."""
    suffix = "" if revision <= 1 else f"-r{revision}"
    return f"{component}-{version}{suffix}-{platform.replace('/', '-')}.{ext}"


def release_tag(component, version, revision) -> str:
    """The component Release tag: <component>-<version>[-rN]."""
    suffix = "" if revision <= 1 else f"-r{revision}"
    return f"{component}-{version}{suffix}"
