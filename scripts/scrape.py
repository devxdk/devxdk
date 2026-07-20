#!/usr/bin/env python3
"""Regenerate the scraped component manifests from upstream metadata.

Replaces gen-manifest.py. The node and go adapters reproduce that script's
output byte-for-byte (see devxdk_manifest/sources); Phase 2 adds the remaining
scrape sources (composer, mariadb, python, postgres, nginx-windows). Components
whose scrape source is not yet implemented are left untouched, so a run only
ever rewrites what it can authoritatively regenerate.

Standard library only. Run from the repo root:

    python3 scripts/scrape.py            # refresh node.json + go.json
    python3 scripts/scrape.py --dry-run  # build + summarize, write nothing

CI (scrape-and-sign.yml) runs this, then validates and re-signs every JSON.
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from devxdk_manifest import config, fetch, merge, schema  # noqa: E402
from devxdk_manifest.sources import go, node  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
STATE_FILE = REPO_ROOT / "state" / "scrape-versions.json"
LEDGER_FILE = REPO_ROOT / "state" / "asset-revisions.json"

# Component name -> builder. Only scrape sources that reproduce their manifest
# byte-for-byte live here; the rest arrive with Phase 2.
SOURCES = {
    "node": node.build,
    "go": go.build,
}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Regenerate scraped component manifests.")
    ap.add_argument("--dry-run", action="store_true", help="build and summarize; write nothing")
    args = ap.parse_args(argv)

    cfg = config.load()
    fetcher = fetch.Fetcher()
    state = merge.ScrapeState.load(STATE_FILE)
    # Managed (built/adopted) platforms live in the ledger; pass it so recompose
    # preserves them when a scrape line regenerates.
    ledger = merge.LedgerState.load(LEDGER_FILE)

    # Only components declared scrape in the config AND with an implemented
    # source are regenerated; this keeps the config the single source of truth.
    scrape_components = {c for c, _l, _p, _plat in cfg.scrape_keys()}

    rc = 0
    for name in sorted(scrape_components & SOURCES.keys()):
        try:
            candidate = SOURCES[name](fetcher)
            # The monotonic guard admits/evicts against committed state, rejecting
            # a feed rollback or a silent republish of a released version.
            _state, manifest, actions = merge.scrape_reconcile(state, cfg, candidate, ledger)
        except Exception as e:  # noqa: BLE001 - report and continue other components
            sys.stderr.write(f"ERROR scraping {name}: {e}\n")
            rc = 1
            continue
        rel = manifest["releases"][0]
        moves = ", ".join(f"{a[3][0]} {a[3][1]}" for a in actions if a[3][0] in ("admit", "evict")) or "no change"
        prefix = "[dry-run] " if args.dry_run else ""
        sys.stderr.write(f"{prefix}{name}.json -> {rel['version']} ({rel.get('released_at', '')}) [{moves}]\n")
        if not args.dry_run:
            schema.write(REPO_ROOT / f"{name}.json", manifest)

    if not args.dry_run:
        # State + manifests are written together, committed in one scrape-and-sign
        # commit; a no-change run re-writes identical bytes (zero diff).
        state.save(STATE_FILE)

    skipped = sorted(scrape_components - SOURCES.keys())
    if skipped:
        sys.stderr.write(f"no scrape source implemented yet (left untouched): {', '.join(skipped)}\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
