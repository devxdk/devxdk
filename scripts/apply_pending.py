#!/usr/bin/env python3
"""Fold pending built/adopted assets into the manifests and ordering ledger.

Reads every record in pending/, applies it under the monotonic guard
(devxdk_manifest.pending), rebuilds each affected component manifest from the
scrape state + ledger, and deletes every consumed pending file. Fails closed on
a corrupt record: nothing is written and no file is deleted, so a bad record can
never half-apply. Run from the repo root before signing (scrape-and-sign.yml).

Standard library only.
"""

import argparse
import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from devxdk_manifest import config, merge, pending, schema  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE_FILE = REPO_ROOT / "state" / "scrape-versions.json"
LEDGER_FILE = REPO_ROOT / "state" / "asset-revisions.json"
PENDING_DIR = REPO_ROOT / "pending"


def load_pending(pending_dir):
    out = []
    for path in sorted(pending_dir.glob("*.json")):
        rec = pending.PendingRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        out.append((rec, path))
    return out


def apply(repo_root=REPO_ROOT, today=None):
    repo_root = pathlib.Path(repo_root)
    today = today or datetime.date.today().isoformat()
    cfg = config.load()
    scrape_state = merge.ScrapeState.load(repo_root / "state" / "scrape-versions.json")
    ledger = merge.LedgerState.load(repo_root / "state" / "asset-revisions.json")

    records = load_pending(repo_root / "pending")
    if not records:
        return {"applied": [], "discarded": [], "affected": []}

    applied, discarded, affected = pending.apply_pending_records(
        cfg, ledger, [r for r, _p in records], today
    )

    for comp in sorted(affected):
        mpath = repo_root / f"{comp}.json"
        existing = schema.load(mpath)
        manifest = merge.recompose(comp, existing["display_name"], existing["kind"], cfg, scrape_state, ledger)
        schema.write(mpath, manifest)

    ledger.save(repo_root / "state" / "asset-revisions.json")
    # Every processed record — applied or discarded — is consumed in the same run.
    for _rec, path in records:
        path.unlink()

    return {
        "applied": [(r.component, r.version, r.platform) for r in applied],
        "discarded": [(r.component, r.version, r.platform, reason) for r, reason in discarded],
        "affected": sorted(affected),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fold pending managed assets into the manifests.")
    ap.add_argument("--today", help="ISO date for a version's first-publication released_at (default: today)")
    args = ap.parse_args(argv)
    try:
        result = apply(REPO_ROOT, args.today)
    except (pending.PendingError, merge.GuardError) as e:
        sys.stderr.write(f"apply_pending: FAILED (nothing written): {e}\n")
        return 1
    for c, v, p in result["applied"]:
        sys.stderr.write(f"applied {c} {v} {p}\n")
    for c, v, p, reason in result["discarded"]:
        sys.stderr.write(f"discarded {c} {v} {p} ({reason})\n")
    sys.stderr.write(f"apply_pending: OK ({len(result['applied'])} applied, {len(result['discarded'])} discarded)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
