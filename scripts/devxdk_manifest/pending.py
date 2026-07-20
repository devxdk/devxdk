"""Fold pending built/adopted assets into the manifests via the ordering ledger.

A build/adopt leg publishes its asset to a Release, then drops a pending record
in pending/. apply_pending folds each into the ledger + the manifest under a
monotonic guard so a stale finalize (an old-provider job that commits after a
migration, or a lower revision after a newer one landed) can never move a
published tuple backward or resurrect a retired line.

Evaluation order per record (fail closed on corruption, discard on staleness):
  1. line-lifecycle gate — the (component, line, platform) must be ACTIVE in the
     current config (not retired, not config-absent), else DISCARD as stale;
  2. epoch — a record above the config epoch is a hard error (a record from the
     future); below it is DISCARDed as pre-migration (never binds provider/kind);
     at it, provider and ordering_kind must bind to the config;
  3. ledger transition — a lower-epoch existing entry is superseded wholesale; a
     same-epoch entry requires matching provider/kind then a three-way key
     ordering (lower discards, equal must match the tuple, higher applies); an
     above-epoch entry is a hard error; a revoked entry blocks (lower discards,
     equal/higher is a hard error naming readmit).

channel and released_at are NOT carried by the record (deterministic producers
omit them): apply_pending derives channel from the version's classification plus
the line's config, and assigns released_at once at a version's first publication.

Standard library only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import merge, versions
from .config import ConfigError

PENDING_FIELDS = (
    "component", "version", "platform", "line", "ordering_kind",
    "provider", "epoch", "revision", "source_version", "url", "sha256", "size_bytes",
)


class PendingError(ValueError):
    """A pending record is malformed or corrupt (fail closed)."""


@dataclass
class PendingRecord:
    component: str
    version: str
    platform: str
    line: str
    ordering_kind: str   # built | adopted
    provider: str
    epoch: int
    revision: int
    source_version: str
    url: str
    sha256: str
    size_bytes: int

    @property
    def key(self) -> str:
        """Ordering key: the integer revision for built, the source version for
        adopted (compared with the numeric dot/dash provider comparator)."""
        return str(self.revision) if self.ordering_kind == "built" else self.source_version

    @staticmethod
    def from_dict(d: dict) -> "PendingRecord":
        missing = [k for k in PENDING_FIELDS if k not in d]
        if missing:
            raise PendingError(f"pending record missing fields {missing}")
        if d["ordering_kind"] not in ("built", "adopted"):
            raise PendingError(f"ordering_kind = {d['ordering_kind']!r}")
        return PendingRecord(**{k: d[k] for k in PENDING_FIELDS})


def _key_compare(kind: str, a: str, b: str) -> int:
    if kind == "built":
        return (int(a) > int(b)) - (int(a) < int(b))
    return versions.compare_provider_key(a, b)


def derive_channel(cfg, component: str, line_id: str, version: str) -> str:
    """A prerelease version is 'prerelease'; otherwise the line's configured
    channel (stable, or lts for an LTS line)."""
    if versions.try_parse(version) is not None and versions.parse(version).is_prerelease():
        return "prerelease"
    return cfg.line(component, line_id).channel


# One record's outcome.
APPLY, DISCARD = "apply", "discard"


def classify(cfg, ledger: merge.LedgerState, rec: PendingRecord):
    """Decide a record's outcome against config + ledger. Returns
    (APPLY, None) | (DISCARD, reason). Raises PendingError on corruption."""
    # 1. Line-lifecycle gate.
    comp = cfg.components.get(rec.component)
    line = comp.lines.get(rec.line) if comp else None
    if comp is None or line is None or line.retired:
        return DISCARD, "line is not active (retired or config-absent)"
    try:
        plat = cfg.find_platform(rec.component, rec.line, rec.platform)
    except ConfigError:
        return DISCARD, "platform not configured for the line"
    if not plat.managed:
        raise PendingError(f"{rec.component} {rec.platform} is a scrape platform, not managed")

    # 2. Epoch classification vs the current config epoch.
    if rec.epoch > plat.epoch:
        raise PendingError(f"record epoch {rec.epoch} is above config epoch {plat.epoch} (record from the future)")
    if rec.epoch < plat.epoch:
        return DISCARD, f"pre-migration record (epoch {rec.epoch} < config {plat.epoch})"
    # Equal epoch — bind provider/kind to the config.
    if rec.provider != plat.provider:
        raise PendingError(f"provider {rec.provider!r} != config {plat.provider!r} at epoch {plat.epoch}")
    if rec.ordering_kind != plat.ordering_kind:
        raise PendingError(f"ordering_kind {rec.ordering_kind!r} != config {plat.ordering_kind!r}")

    # 3. Ledger transition.
    existing = ledger.get(rec.component, rec.version, rec.platform)
    if existing is None:
        return APPLY, None
    if existing.revoked:
        if _key_compare(rec.ordering_kind, rec.key, existing.key) <= 0:
            return DISCARD, "revoked entry; incoming key is not newer"
        raise PendingError(f"{rec.component} {rec.version} {rec.platform} is revoked; needs a readmit record")
    if existing.epoch < rec.epoch:
        return APPLY, None  # wholesale supersession across the epoch bump
    if existing.epoch > rec.epoch:
        raise PendingError(f"ledger entry epoch {existing.epoch} is above the record epoch {rec.epoch}")
    # Same epoch — provider/kind must match, then three-way key ordering.
    if existing.provider != rec.provider or existing.kind != rec.ordering_kind:
        raise PendingError(f"{rec.component} {rec.version} {rec.platform}: ledger provider/kind mismatch at same epoch")
    c = _key_compare(rec.ordering_kind, rec.key, existing.key)
    if c < 0:
        return DISCARD, f"stale key {rec.key} < committed {existing.key}"
    if c == 0:
        if (existing.url != rec.url or existing.sha256 != rec.sha256
                or existing.size_bytes != rec.size_bytes
                or existing.source_version != rec.source_version):
            raise PendingError(f"{rec.component} {rec.version} {rec.platform}: equal-key tuple conflict")
        return DISCARD, "idempotent (already applied)"
    return APPLY, None


def apply_pending_records(cfg, ledger: merge.LedgerState, records, today: str):
    """Fold records into the ledger. Returns (applied, discarded, affected)
    where affected is the set of components whose manifest must be rebuilt.
    Raises PendingError on the first corrupt record."""
    applied, discarded, affected = [], [], set()
    for rec in records:
        outcome, reason = classify(cfg, ledger, rec)
        if outcome == DISCARD:
            discarded.append((rec, reason))
            continue
        channel = derive_channel(cfg, rec.component, rec.line, rec.version)
        released_at = _released_at(ledger, rec, today)
        ledger.put(rec.component, rec.version, rec.platform, merge.LedgerRecord(
            kind=rec.ordering_kind, line=rec.line, provider=rec.provider, epoch=rec.epoch,
            key=rec.key, source_version=rec.source_version, url=rec.url, sha256=rec.sha256,
            size_bytes=rec.size_bytes, channel=channel, released_at=released_at,
        ))
        applied.append(rec)
        affected.add(rec.component)
    return applied, discarded, affected


def _released_at(ledger: merge.LedgerState, rec: PendingRecord, today: str) -> str:
    """released_at is assigned once at a version's first publication: reuse any
    existing platform's value for the same (component, version), else `today`."""
    for _c, ver, _p, lr in ledger.iter_records():
        if _c == rec.component and ver == rec.version and lr.released_at:
            return lr.released_at
    return today
