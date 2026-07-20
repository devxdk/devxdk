#!/usr/bin/env python3
"""Consume committed revocation records before the scrape/apply pass.

Reads every record in revocations/, applies it to the scrape state or ordering
ledger under the expected-tuple guard, rebuilds each affected manifest, saves
both state files, and deletes the consumed records — failing closed so a bad
record writes nothing. Runs first in scrape-and-sign's full-transaction retry.

Standard library only.
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from devxdk_manifest import config, merge, revoke, schema  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def apply(repo_root=REPO_ROOT):
    repo_root = pathlib.Path(repo_root)
    cfg = config.load()
    scrape_state = merge.ScrapeState.load(repo_root / "state" / "scrape-versions.json")
    ledger = merge.LedgerState.load(repo_root / "state" / "asset-revisions.json")

    records = []
    for path in sorted((repo_root / "revocations").glob("*.json")):
        records.append((revoke.RevocationRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))), path))
    if not records:
        return {"applied": [], "affected": []}

    applied, affected = [], set()
    for rec, _path in records:
        action, comp = revoke.apply(scrape_state, ledger, rec)
        applied.append((rec.scope, rec.component, rec.version, rec.platform, action))
        affected.add(comp)

    for comp in sorted(affected):
        mpath = repo_root / f"{comp}.json"
        existing = schema.load(mpath)
        manifest = merge.recompose(comp, existing["display_name"], existing["kind"], cfg, scrape_state, ledger)
        schema.write(mpath, manifest)

    scrape_state.save(repo_root / "state" / "scrape-versions.json")
    ledger.save(repo_root / "state" / "asset-revisions.json")
    for _rec, path in records:
        path.unlink()
    return {"applied": applied, "affected": sorted(affected)}


def main(argv=None):
    argparse.ArgumentParser(description="Consume committed revocation records.").parse_args(argv)
    try:
        result = apply(REPO_ROOT)
    except (revoke.RevocationError, merge.GuardError) as e:
        sys.stderr.write(f"apply_revocations: FAILED (nothing written): {e}\n")
        return 1
    for scope, c, v, p, action in result["applied"]:
        sys.stderr.write(f"{scope} {action}: {c} {v} {p}\n")
    sys.stderr.write(f"apply_revocations: OK ({len(result['applied'])} applied)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
