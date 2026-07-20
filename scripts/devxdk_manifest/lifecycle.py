"""Line retirement and reactivation — dropping or restoring a whole tracked line.

Retiring a line is a two-step reviewed transition, never a feed outcome. Step 1
(a reviewed config commit) sets `retired = true` on the line, so apply_pending
starts discarding its records while parity still holds (the key is present).
Step 2 (the next pipeline run, retire_lines) atomically tombstones the line's
scrape records and ledger entries — snapshotting each removed release so a later
reactivation restores it exactly — and the manifest rebuild then excludes the
tombstoned entries. Reactivation is the mirror: the config un-retires the line
beside its tombstones, and reactivate_lines flips them back to active. A managed
platform's reactivation REQUIRES an epoch bump above the tombstone's epoch, so a
pre-retirement in-flight record that lands afterward is still discarded as
pre-migration.

The consumption side (recompose skipping tombstones, apply_pending's line gate,
the validators' tombstone exemptions) already exists; this module is the
transition that produces those states. Standard library only.
"""

from __future__ import annotations

from . import merge, schema


class LifecycleError(ValueError):
    """A retirement/reactivation transition is inconsistent (fail closed)."""


def _line_versions(scrape_state, ledger, component, line):
    versions = set()
    for c, l, _p, rec in scrape_state.iter_records():
        if c == component and l == line and rec.status == "active":
            versions.update(t.version for t in rec.tuples)
    for c, v, _p, lr in ledger.iter_records():
        if c == component and lr.line == line and lr.status == "active" and not lr.revoked:
            versions.add(v)
    return versions


def retire_line(scrape_state, ledger, component, line, manifest) -> bool:
    """Tombstone one line's active records/entries, snapshotting each release.
    Returns True if anything was tombstoned (idempotent — a no-op otherwise)."""
    versions = _line_versions(scrape_state, ledger, component, line)
    if not versions:
        return False
    snapshots = {rel["version"]: rel for rel in manifest.get("releases", []) if rel["version"] in versions}

    changed = False
    for c, l, _p, rec in scrape_state.iter_records():
        if c == component and l == line and rec.status == "active":
            rec.status = "tombstone"
            rec.release_snapshots = {t.version: snapshots[t.version] for t in rec.tuples if t.version in snapshots}
            changed = True
    for c, v, _p, lr in ledger.iter_records():
        if c == component and lr.line == line and lr.status == "active" and not lr.revoked:
            lr.status = "tombstone"
            lr.release_snapshot = snapshots.get(v)
            changed = True
    return changed


def reactivate_line(cfg, scrape_state, ledger, component, line) -> bool:
    """Flip one line's tombstoned records/entries back to active. A managed
    platform requires an epoch bump above its tombstone epoch (the reactivation
    migration). Returns True if anything was reactivated."""
    changed = False
    for c, l, _p, rec in scrape_state.iter_records():
        if c == component and l == line and rec.status == "tombstone":
            rec.status = "active"
            rec.release_snapshots = {}
            changed = True
    for c, v, p, lr in ledger.iter_records():
        if c == component and lr.line == line and lr.status == "tombstone":
            plat = cfg.find_platform(component, line, p)
            if plat.epoch <= lr.epoch:
                raise LifecycleError(
                    f"reactivating {component}/{line}/{p} needs an epoch bump above {lr.epoch}"
                )
            # Re-stamp to the bumped epoch only when provider AND kind still match,
            # so the ordering floor survives; otherwise leave the old epoch for the
            # first new-provider/kind record to supersede.
            if plat.provider == lr.provider and plat.ordering_kind == lr.kind:
                lr.epoch = plat.epoch
            lr.status = "active"
            lr.release_snapshot = None
            changed = True
    return changed


def apply_lifecycle(cfg, scrape_state, ledger, repo_root):
    """Drive both transitions from the config's `retired` flags. Retire every
    line marked retired with active records; reactivate every non-retired line
    that still has tombstones. Returns the set of affected components."""
    import pathlib

    repo_root = pathlib.Path(repo_root)
    affected = set()

    # Which (component, line) pairs are retired in the config.
    retired = set()
    active_cfg = set()
    for cname, comp in cfg.components.items():
        for lid, line in comp.lines.items():
            (retired if line.retired else active_cfg).add((cname, lid))

    for (cname, lid) in sorted(retired):
        mpath = repo_root / f"{cname}.json"
        manifest = schema.load(mpath) if mpath.exists() else {"releases": []}
        if retire_line(scrape_state, ledger, cname, lid, manifest):
            affected.add(cname)

    # Reactivate a line that is active in config but still has tombstones.
    tombstoned_lines = set()
    for c, l, _p, rec in scrape_state.iter_records():
        if rec.status == "tombstone":
            tombstoned_lines.add((c, l))
    for c, _v, _p, lr in ledger.iter_records():
        if lr.status == "tombstone":
            tombstoned_lines.add((c, lr.line))
    for (cname, lid) in sorted(tombstoned_lines & active_cfg):
        if reactivate_line(cfg, scrape_state, ledger, cname, lid):
            affected.add(cname)

    return affected
