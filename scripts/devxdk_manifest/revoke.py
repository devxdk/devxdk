"""Revocations — the reviewed override the monotonic guards otherwise forbid.

The scrape guard and the ordering ledger never move a published tuple backward
on their own. A revocation is the committed, review-bound record that authorizes
it: a one-shot file in revocations/ naming the exact expected old tuple, so a
malformed or stale record is a hard error, never a silent mutation.

Two scopes:
  * scraped — delete (suppress a version durably), replace (correct a published
    version's bytes), or readmit (un-suppress a revoked version). Operates on the
    scrape-versions record (tuples + revoked list).
  * managed — delete (pull a built/adopted asset: remove the manifest platform
    and mark the ledger entry revoked) or readmit (restore or bump it). Managed
    replace does not exist — a same-version repack publishes the next revision
    through the normal pending/ledger path.

A revoked scraped version stays in the record's `revoked` list so the scraper
suppresses it regardless of the floor; a revoked ledger entry is sticky and
blocks pending records until a readmit. Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import merge

TUPLE_KEYS = ("url", "sha256", "size_bytes", "channel", "released_at")
ORDERING_KEYS = ("kind", "provider", "epoch", "key", "source_version")


class RevocationError(ValueError):
    """A revocation record is malformed or its expected tuple does not match."""


@dataclass
class RevocationRecord:
    scope: str            # scraped | managed
    component: str
    platform: str
    version: str
    op: str               # delete | replace | readmit
    expected: dict        # the old tuple fields (TUPLE_KEYS)
    reason: str
    line: str | None = None            # scraped only
    replacement: dict | None = None    # replace / readmit
    expected_ordering: dict | None = None  # managed only

    @staticmethod
    def from_dict(d: dict) -> "RevocationRecord":
        scope = d.get("scope")
        if scope not in ("scraped", "managed"):
            raise RevocationError(f"scope = {d.get('scope')!r}")
        op = d.get("op")
        valid = {"scraped": {"delete", "replace", "readmit"}, "managed": {"delete", "readmit"}}[scope]
        if op not in valid:
            raise RevocationError(f"{scope} op = {op!r}, want one of {sorted(valid)}")
        for k in ("component", "platform", "version", "expected", "reason"):
            if k not in d:
                raise RevocationError(f"revocation missing {k!r}")
        if scope == "scraped" and "line" not in d:
            raise RevocationError("scraped revocation missing 'line'")
        if scope == "managed" and "expected_ordering" not in d:
            raise RevocationError("managed revocation missing 'expected_ordering'")
        if op in ("replace", "readmit") and "replacement" not in d:
            raise RevocationError(f"{op} revocation missing 'replacement'")
        return RevocationRecord(
            scope=scope, component=d["component"], platform=d["platform"], version=d["version"],
            op=op, expected=d["expected"], reason=d["reason"], line=d.get("line"),
            replacement=d.get("replacement"), expected_ordering=d.get("expected_ordering"),
        )


def _tuple_from(version, fields) -> merge.Tuple:
    missing = [k for k in TUPLE_KEYS if k not in fields]
    if missing:
        raise RevocationError(f"tuple missing {missing}")
    return merge.Tuple(version=version, **{k: fields[k] for k in TUPLE_KEYS})


def _tuple_fields_match(t: merge.Tuple, fields) -> bool:
    return all(getattr(t, k) == fields.get(k) for k in TUPLE_KEYS)


def apply_scraped(scrape_state: merge.ScrapeState, rec: RevocationRecord) -> str:
    rc = scrape_state.get(rec.component, rec.line, rec.platform)
    if rc is None:
        raise RevocationError(f"no scrape record for {rec.component}/{rec.line}/{rec.platform}")

    if rec.op in ("delete", "replace"):
        idx = next((i for i, t in enumerate(rc.tuples) if t.version == rec.version), None)
        if idx is None or not _tuple_fields_match(rc.tuples[idx], rec.expected):
            raise RevocationError(f"scraped {rec.version}: committed tuple does not match expected")
        if rec.op == "delete":
            rc.revoked.append(rc.tuples.pop(idx))
            return "deleted-and-suppressed"
        rc.tuples[idx] = _tuple_from(rec.version, rec.replacement)  # replace
        return "replaced"

    # readmit — the exact revoked tuple must match, then admit the replacement.
    ridx = next((i for i, t in enumerate(rc.revoked) if t.version == rec.version), None)
    if ridx is None or not _tuple_fields_match(rc.revoked[ridx], rec.expected):
        raise RevocationError(f"scraped readmit {rec.version}: no matching revoked tuple")
    rc.revoked.pop(ridx)
    rc.tuples.append(_tuple_from(rec.version, rec.replacement))
    rc.tuples = merge._cmp_sort_desc(rc.tuples)
    return "readmitted"


def apply_managed(ledger: merge.LedgerState, rec: RevocationRecord) -> str:
    entry = ledger.get(rec.component, rec.version, rec.platform)
    if entry is None:
        raise RevocationError(f"no ledger entry for {rec.component} {rec.version} {rec.platform}")
    if not _tuple_fields_match(_ledger_tuple(entry), rec.expected):
        raise RevocationError(f"managed {rec.version} {rec.platform}: committed tuple does not match expected")
    for k in ORDERING_KEYS:
        if str(getattr(entry, k)) != str(rec.expected_ordering.get(k)):
            raise RevocationError(f"managed {rec.version} {rec.platform}: ordering identity mismatch on {k}")

    if rec.op == "delete":
        entry.revoked = True
        return "revoked"

    # readmit — restore the revoked entry (optionally with a bumped tuple).
    if not entry.revoked:
        raise RevocationError(f"managed readmit {rec.version} {rec.platform}: entry is not revoked")
    for k in TUPLE_KEYS:
        setattr(entry, k, rec.replacement[k])
    entry.revoked = False
    return "readmitted"


def _ledger_tuple(entry: merge.LedgerRecord) -> merge.Tuple:
    return merge.Tuple(
        version="", url=entry.url, sha256=entry.sha256, size_bytes=entry.size_bytes,
        channel=entry.channel, released_at=entry.released_at,
    )


def apply(scrape_state: merge.ScrapeState, ledger: merge.LedgerState, rec: RevocationRecord):
    """Apply one revocation. Returns (action, affected_component_or_None).
    A managed op affects the manifest (rebuild); scraped ops mutate the scrape
    record (the scraper rebuilds the manifest on its next run)."""
    if rec.scope == "scraped":
        return apply_scraped(scrape_state, rec), rec.component
    return apply_managed(ledger, rec), rec.component
