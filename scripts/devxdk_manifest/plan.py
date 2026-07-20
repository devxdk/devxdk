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


def archive_name(component, version, revision, platform, ext):
    """Canonical archive filename: <component>-<version>[-rN]-<goos>-<goarch>.<ext>."""
    suffix = "" if revision <= 1 else f"-r{revision}"
    return f"{component}-{version}{suffix}-{platform.replace('/', '-')}.{ext}"


def release_tag(component, version, revision) -> str:
    """The component Release tag: <component>-<version>[-rN]."""
    suffix = "" if revision <= 1 else f"-r{revision}"
    return f"{component}-{version}{suffix}"
