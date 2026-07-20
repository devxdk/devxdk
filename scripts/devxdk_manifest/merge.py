"""The scrape-versions monotonic guard and manifest recompose.

Wholesale scrape regeneration alone would let a feed roll a published release
back, or silently mutate a retained release's bytes. This module is the guard
that prevents both, backed by a committed ordering ledger
(``state/scrape-versions.json``).

Per (component, line, platform) it holds a monotonic ``floor_version`` (the
high-water mark ever admitted) plus the full set of currently-committed tuples
and a durable ``revoked`` list. The rules on a scrape run:

  * a candidate whose version already exists must carry an IDENTICAL tuple
    (url/sha256/size/channel/released_at) — a byte or metadata change on a
    published release is a hard error;
  * a strictly-newer candidate is admitted and the floor rises;
  * a candidate below the floor that is not a committed tuple is ignored (an
    evicted release or a feed straggler never re-enters);
  * a committed tuple absent from the feed is KEPT (immutability), evicted only
    by retention when a strictly-newer release is admitted;
  * a revoked version is a hard error until an explicit readmit record.

The line-lifecycle (retired/tombstone/reactivation) and revocation transitions
layer on top in a later slice; the fields they need (``status``, ``revoked``,
``release_snapshots``) already exist here so no state migration is required.

Standard library only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace

from . import schema, versions

STATE_SCHEMA = 1
TUPLE_FIELDS = ("version", "url", "sha256", "size_bytes", "channel", "released_at")


class GuardError(ValueError):
    """A scrape transition violated the monotonic guard (fail closed)."""


@dataclass(frozen=True)
class Tuple:
    """One committed scraped release tuple for a (component, line, platform)."""

    version: str
    url: str
    sha256: str
    size_bytes: int
    channel: str
    released_at: str

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "url": self.url,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "channel": self.channel,
            "released_at": self.released_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "Tuple":
        missing = [k for k in TUPLE_FIELDS if k not in d]
        if missing:
            raise GuardError(f"tuple missing fields {missing}: {d}")
        return Tuple(
            version=d["version"],
            url=d["url"],
            sha256=d["sha256"],
            size_bytes=d["size_bytes"],
            channel=d["channel"],
            released_at=d["released_at"],
        )


@dataclass
class ScrapeRecord:
    provider: str
    epoch: int
    status: str = "active"                     # active | tombstone
    floor_version: str | None = None           # high-water mark; None = never published
    tuples: list = field(default_factory=list)          # list[Tuple]
    revoked: list = field(default_factory=list)         # list[Tuple], durable
    release_snapshots: dict = field(default_factory=dict)  # tombstone only

    def as_dict(self) -> dict:
        out = {
            "provider": self.provider,
            "epoch": self.epoch,
            "status": self.status,
            "floor_version": self.floor_version,
            "tuples": [t.as_dict() for t in self.tuples],
            "revoked": [t.as_dict() for t in self.revoked],
        }
        if self.release_snapshots:
            out["release_snapshots"] = self.release_snapshots
        return out

    @staticmethod
    def from_dict(d: dict) -> "ScrapeRecord":
        return ScrapeRecord(
            provider=d["provider"],
            epoch=d.get("epoch", 1),
            status=d.get("status", "active"),
            floor_version=d.get("floor_version"),
            tuples=[Tuple.from_dict(t) for t in d.get("tuples", [])],
            revoked=[Tuple.from_dict(t) for t in d.get("revoked", [])],
            release_snapshots=d.get("release_snapshots", {}),
        )


@dataclass
class ScrapeState:
    schema: int = STATE_SCHEMA
    # records[component][line][platform] -> ScrapeRecord
    records: dict = field(default_factory=dict)

    def get(self, component: str, line: str, platform: str):
        return self.records.get(component, {}).get(line, {}).get(platform)

    def put(self, component: str, line: str, platform: str, record: ScrapeRecord):
        self.records.setdefault(component, {}).setdefault(line, {})[platform] = record

    def iter_records(self):
        for c, lines in self.records.items():
            for l, plats in lines.items():
                for p, rec in plats.items():
                    yield c, l, p, rec

    # -- serialization -----------------------------------------------------

    def as_dict(self) -> dict:
        out = {}
        for c in sorted(self.records):
            out[c] = {}
            for l in sorted(self.records[c]):
                out[c][l] = {}
                for p in sorted(self.records[c][l]):
                    out[c][l][p] = self.records[c][l][p].as_dict()
        return {"schema": self.schema, "records": out}

    def dump_str(self) -> str:
        # Byte-stable: sorted keys, indent=2, trailing newline, LF.
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"

    def save(self, path) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(self.dump_str())

    @staticmethod
    def load(path) -> "ScrapeState":
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return ScrapeState.from_dict(raw)

    @staticmethod
    def from_dict(raw: dict) -> "ScrapeState":
        if raw.get("schema") != STATE_SCHEMA:
            raise GuardError(f"scrape-versions schema = {raw.get('schema')!r}, want {STATE_SCHEMA}")
        st = ScrapeState(schema=STATE_SCHEMA)
        for c, lines in raw.get("records", {}).items():
            for l, plats in lines.items():
                for p, rec in plats.items():
                    st.put(c, l, p, ScrapeRecord.from_dict(rec))
        return st


LEDGER_SCHEMA = 1
LEDGER_FIELDS = (
    "kind", "line", "provider", "epoch", "key", "source_version",
    "url", "sha256", "size_bytes", "channel", "released_at",
)


@dataclass
class LedgerRecord:
    """One ordering-ledger entry for a managed (built/adopted) asset.

    Binds a manifest platform tuple to its ordering identity so a same-hash URL
    or size mutation, or a silent channel flip, cannot slip past. The transition
    rules (epoch supersession, tombstone, revocation) land in a later slice; this
    model only needs to load, bind, and round-trip."""

    kind: str                 # built | adopted
    line: str
    provider: str
    epoch: int
    key: str                  # ordering key (built: integer revision; adopted: source version)
    source_version: str
    url: str
    sha256: str
    size_bytes: int
    channel: str
    released_at: str
    status: str = "active"    # active | tombstone
    revoked: bool = False
    release_snapshot: dict | None = None  # tombstone only

    def as_dict(self) -> dict:
        out = {k: getattr(self, k) for k in LEDGER_FIELDS}
        out["status"] = self.status
        out["revoked"] = self.revoked
        if self.release_snapshot is not None:
            out["release_snapshot"] = self.release_snapshot
        return out

    @staticmethod
    def from_dict(d: dict) -> "LedgerRecord":
        missing = [k for k in LEDGER_FIELDS if k not in d]
        if missing:
            raise GuardError(f"ledger record missing fields {missing}: {d}")
        return LedgerRecord(
            kind=d["kind"], line=d["line"], provider=d["provider"], epoch=d["epoch"],
            key=d["key"], source_version=d["source_version"], url=d["url"],
            sha256=d["sha256"], size_bytes=d["size_bytes"], channel=d["channel"],
            released_at=d["released_at"], status=d.get("status", "active"),
            revoked=d.get("revoked", False), release_snapshot=d.get("release_snapshot"),
        )


@dataclass
class LedgerState:
    schema: int = LEDGER_SCHEMA
    # entries[component][version][platform] -> LedgerRecord
    entries: dict = field(default_factory=dict)

    def get(self, component: str, version: str, platform: str):
        return self.entries.get(component, {}).get(version, {}).get(platform)

    def put(self, component: str, version: str, platform: str, record: LedgerRecord):
        self.entries.setdefault(component, {}).setdefault(version, {})[platform] = record

    def iter_records(self):
        for c, vers in self.entries.items():
            for v, plats in vers.items():
                for p, rec in plats.items():
                    yield c, v, p, rec

    def as_dict(self) -> dict:
        out = {}
        for c in sorted(self.entries):
            out[c] = {}
            for v in sorted(self.entries[c]):
                out[c][v] = {}
                for p in sorted(self.entries[c][v]):
                    out[c][v][p] = self.entries[c][v][p].as_dict()
        return {"entries": out, "schema": self.schema}

    def dump_str(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"

    def save(self, path) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(self.dump_str())

    @staticmethod
    def load(path) -> "LedgerState":
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if raw.get("schema") != LEDGER_SCHEMA:
            raise GuardError(f"asset-revisions schema = {raw.get('schema')!r}, want {LEDGER_SCHEMA}")
        st = LedgerState(schema=LEDGER_SCHEMA)
        for c, vers in raw.get("entries", {}).items():
            for v, plats in vers.items():
                for p, rec in plats.items():
                    st.put(c, v, p, LedgerRecord.from_dict(rec))
        return st


def check_ledger_parity(cfg, ledger: LedgerState, repo_root) -> list:
    """Bind managed (built/adopted) manifest tuples to the ordering ledger in
    both directions. A missing entry would silently disable the monotonic guard;
    an orphan active entry would block ever re-adding the platform. Tombstoned
    and revoked entries are exempt from live-manifest binding."""
    import pathlib

    from .config import ConfigError

    repo_root = pathlib.Path(repo_root)
    errors = []

    managed = {}  # (component, version, platform) -> (asset, release)
    for cname in sorted({c for c, _l, _p, _ in cfg.managed_keys()}):
        mpath = repo_root / f"{cname}.json"
        if not mpath.exists():
            continue
        data = schema.load(mpath)
        for rel in data.get("releases", []):
            ver = rel["version"]
            lid = line_for(cfg, cname, ver)
            for pkey, asset in rel.get("platforms", {}).items():
                if lid is None:
                    continue
                try:
                    plat = cfg.find_platform(cname, lid, pkey)
                except ConfigError:
                    continue
                if not plat.managed:
                    continue
                managed[(cname, ver, pkey)] = (asset, rel)

    for (c, v, p), (asset, rel) in sorted(managed.items()):
        rec = ledger.get(c, v, p)
        if rec is None or rec.status == "tombstone" or rec.revoked:
            errors.append(f"managed manifest {c} {v} {p} has no active ledger entry")
            continue
        if (rec.url != asset["url"] or rec.sha256 != asset["sha256"]
                or rec.size_bytes != asset["size_bytes"]
                or rec.channel != rel["channel"]
                or rec.released_at != rel.get("released_at", "")):
            errors.append(f"ledger entry {c} {v} {p} tuple differs from manifest")

    for c, v, p, rec in ledger.iter_records():
        if rec.status == "tombstone" or rec.revoked:
            continue
        if (c, v, p) not in managed:
            errors.append(f"active ledger entry {c} {v} {p} points at no live manifest tuple")
    return errors


# -- helpers ---------------------------------------------------------------

def _tuples_equal(a: Tuple, b: Tuple) -> bool:
    return a.as_dict() == b.as_dict()


def _cmp_sort_desc(tuples):
    import functools
    return sorted(
        tuples,
        key=functools.cmp_to_key(lambda a, b: versions.compare_str(a.version, b.version)),
        reverse=True,
    )


def line_for(cfg, component: str, ver: str) -> str | None:
    """Return the configured line id a version belongs to, or None if untracked.

    Line granularity is inferred from the line id's dotted shape: no dot = major,
    one dot = major.minor, two dots = full version (matching the app's data-line
    derivation)."""
    comp = cfg.component(component)
    v = versions.parse(ver)
    for lid in comp.lines:
        dots = lid.count(".")
        if dots == 0 and v.major_string() == lid:
            return lid
        if dots == 1 and v.major_minor_string() == lid:
            return lid
        if dots == 2 and ver == lid:
            return lid
    return None


# -- seeding ---------------------------------------------------------------

def seed(cfg, repo_root) -> ScrapeState:
    """Build the scrape state from the committed manifests.

    Every configured scrape (component, line, platform) gets a record — an empty
    one (floor None, no tuples) when nothing is published yet — so the parity
    check binds config and state in both directions from the first commit."""
    import pathlib

    repo_root = pathlib.Path(repo_root)
    state = ScrapeState()

    # Group manifest tuples by (component, line, platform).
    for cname, lid, pkey, plat in cfg.scrape_keys():
        state.put(cname, lid, pkey, ScrapeRecord(provider=plat.provider, epoch=plat.epoch))

    for cname in {c for c, _l, _p, _plat in cfg.scrape_keys()}:
        mpath = repo_root / f"{cname}.json"
        if not mpath.exists():
            continue
        data = schema.load(mpath)
        for rel in data.get("releases", []):
            ver = rel["version"]
            lid = line_for(cfg, cname, ver)
            if lid is None:
                raise GuardError(f"{cname} {ver}: no tracked line in config")
            for pkey, asset in rel.get("platforms", {}).items():
                rec = state.get(cname, lid, pkey)
                if rec is None:
                    # Manifest carries a platform the config does not track as scrape.
                    raise GuardError(f"{cname}.json release {ver} platform {pkey} is not a configured scrape key")
                rec.tuples.append(Tuple(
                    version=ver, url=asset["url"], sha256=asset["sha256"],
                    size_bytes=asset["size_bytes"], channel=rel["channel"],
                    released_at=rel.get("released_at", ""),
                ))

    # Set the floor to the high-water version and trim to retain_per_line.
    for cname, lid, pkey, rec in state.iter_records():
        if not rec.tuples:
            continue
        rec.tuples = _cmp_sort_desc(rec.tuples)
        retain = cfg.line(cname, lid).retain_per_line
        rec.tuples = rec.tuples[:retain]
        rec.floor_version = rec.tuples[0].version
    return state


# -- the guard -------------------------------------------------------------

def reconcile_key(record: ScrapeRecord, candidates, retain: int):
    """Apply the monotonic guard for one (component, line, platform).

    Returns (new_record, actions). Raises GuardError on an equal-version
    mutation or an attempt to re-admit a revoked version."""
    committed = {t.version: t for t in record.tuples}
    revoked = {t.version for t in record.revoked}
    admits = []
    actions = []

    for t in candidates:
        if t.version in revoked:
            raise GuardError(f"{t.version} is revoked; needs an explicit readmit record")
        if t.version in committed:
            if not _tuples_equal(t, committed[t.version]):
                raise GuardError(
                    f"equal-version mutation for {t.version}: a published tuple's "
                    f"bytes/url/size/channel/released_at may not change"
                )
            continue  # idempotent — already committed
        if record.floor_version is None or versions.compare_str(t.version, record.floor_version) > 0:
            admits.append(t)
        else:
            actions.append(("ignore-below-floor", t.version))

    result = list(record.tuples) + admits
    new_floor = record.floor_version
    for t in admits:
        if new_floor is None or versions.compare_str(t.version, new_floor) > 0:
            new_floor = t.version
        actions.append(("admit", t.version))

    # Retention eviction happens ONLY in a run that admits a strictly-newer
    # release (the plan's rule): removes the oldest beyond retain_per_line.
    if admits:
        result = _cmp_sort_desc(result)
        for e in result[retain:]:
            actions.append(("evict", e.version))
        result = result[:retain]

    return replace(record, tuples=result, floor_version=new_floor), actions


# -- decompose / recompose -------------------------------------------------

def manifest_tuples(cfg, manifest: dict):
    """Decompose a candidate component manifest into {(line, platform): [Tuple]}."""
    name = manifest["name"]
    out = {}
    for rel in manifest.get("releases", []):
        ver = rel["version"]
        lid = line_for(cfg, name, ver)
        if lid is None:
            raise GuardError(f"{name} {ver}: candidate version has no tracked line")
        for pkey, asset in rel.get("platforms", {}).items():
            out.setdefault((lid, pkey), []).append(Tuple(
                version=ver, url=asset["url"], sha256=asset["sha256"],
                size_bytes=asset["size_bytes"], channel=rel["channel"],
                released_at=rel.get("released_at", ""),
            ))
    return out


def _merge_platform(by_version, name, ver, channel, released_at, pkey, asset):
    entry = by_version.setdefault(ver, {"channel": channel, "released_at": released_at, "platforms": {}})
    if entry["channel"] != channel or entry["released_at"] != released_at:
        raise GuardError(f"{name} {ver}: channel/released_at differ across platforms")
    entry["platforms"][pkey] = asset


def recompose(name: str, display_name: str, kind: str, cfg, state: ScrapeState, ledger=None) -> dict:
    """Rebuild a component manifest from BOTH sources of truth: the scrape state
    (scrape platforms) and the ordering ledger (built/adopted platforms). This is
    why the scraper "never touches" managed entries — it rebuilds the whole
    manifest from state, and a managed platform's tuple lives in the ledger, so
    regenerating a scrape line preserves it. Releases group across platforms by
    version, newest-first, with canonical field/platform order."""
    by_version = {}
    for cname, _lid, pkey, rec in state.iter_records():
        if cname != name or rec.status == "tombstone":
            continue  # a tombstoned (retired) line is not in the manifest
        for t in rec.tuples:
            _merge_platform(by_version, name, t.version, t.channel, t.released_at, pkey,
                            schema.asset(t.url, t.sha256, t.size_bytes))
    if ledger is not None:
        for cname, ver, pkey, lr in ledger.iter_records():
            if cname != name or lr.status == "tombstone" or lr.revoked:
                continue  # tombstoned/revoked entries are not in the manifest
            _merge_platform(by_version, name, ver, lr.channel, lr.released_at, pkey,
                            schema.asset(lr.url, lr.sha256, lr.size_bytes))

    releases = []
    import functools
    for ver in sorted(by_version, key=functools.cmp_to_key(versions.compare_str), reverse=True):
        e = by_version[ver]
        platforms = schema.order_platforms(e["platforms"])
        releases.append(schema.release(ver, e["channel"], e["released_at"], platforms))
    return schema.component(name, display_name, kind, releases)


def check_scrape_parity(cfg, state: ScrapeState, repo_root) -> list:
    """Bidirectionally bind the config, the scrape state, and the committed
    manifests. Returns a list of error strings ([] = coherent). A missing
    record, an orphan record, or any tuple mismatch fails closed — so deleting a
    record can never silently reset a key's monotonic guard."""
    import pathlib

    from .config import ConfigError

    repo_root = pathlib.Path(repo_root)
    errors = []
    configured = {(c, l, p) for c, l, p, _ in cfg.scrape_keys()}
    recorded = {(c, l, p) for c, l, p, _ in state.iter_records()}

    for key in sorted(configured - recorded):
        errors.append(f"scrape key {key} has no state record")
    for c, l, p, rec in state.iter_records():
        if (c, l, p) not in configured and rec.status != "tombstone":
            errors.append(f"state record {(c, l, p)} maps to no configured scrape key")

    # Index state tuples and cross-check against the committed manifests.
    state_tuples = {}
    for c, l, p, rec in state.iter_records():
        for t in rec.tuples:
            state_tuples[(c, t.version, p)] = t

    manifest_keys = set()
    for c in sorted({k[0] for k in configured}):
        mpath = repo_root / f"{c}.json"
        if not mpath.exists():
            continue
        data = schema.load(mpath)
        for rel in data.get("releases", []):
            ver = rel["version"]
            lid = line_for(cfg, c, ver)
            for pkey, asset in rel.get("platforms", {}).items():
                if lid is None:
                    continue
                try:
                    plat = cfg.find_platform(c, lid, pkey)
                except ConfigError:
                    continue
                if plat.type != "scrape":
                    continue  # managed platform — bound by the ordering ledger, not here
                manifest_keys.add((c, ver, pkey))
                st = state_tuples.get((c, ver, pkey))
                if st is None:
                    errors.append(f"manifest {c} {ver} {pkey} has no scrape state tuple")
                    continue
                if (st.url != asset["url"] or st.sha256 != asset["sha256"]
                        or st.size_bytes != asset["size_bytes"]
                        or st.channel != rel["channel"]
                        or st.released_at != rel.get("released_at", "")):
                    errors.append(f"manifest {c} {ver} {pkey} tuple differs from state")

    for (c, ver, p) in sorted(state_tuples.keys() - manifest_keys):
        errors.append(f"state tuple {c} {ver} {p} is absent from the committed manifest")
    return errors


def scrape_reconcile(state: ScrapeState, cfg, candidate: dict, ledger=None):
    """Reconcile a freshly-scraped candidate manifest against the state.

    Returns (new_state, recomposed_manifest, actions). Mutates state in place;
    the caller writes both the manifest and the updated state atomically. The
    ledger is threaded through so a mixed component (e.g. nginx: scraped Windows +
    built Unix) keeps its managed platforms when a scrape line regenerates."""
    name = candidate["name"]
    tuples_by_key = manifest_tuples(cfg, candidate)
    all_actions = []
    for (lid, pkey), cands in tuples_by_key.items():
        rec = state.get(name, lid, pkey)
        if rec is None:
            raise GuardError(f"{name} candidate targets unconfigured scrape key {lid}/{pkey}")
        retain = cfg.line(name, lid).retain_per_line
        new_rec, actions = reconcile_key(rec, cands, retain)
        state.put(name, lid, pkey, new_rec)
        all_actions.extend((name, lid, pkey, a) for a in actions)
    manifest = recompose(name, candidate["display_name"], candidate["kind"], cfg, state, ledger)
    return state, manifest, all_actions
