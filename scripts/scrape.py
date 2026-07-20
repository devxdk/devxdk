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

from devxdk_manifest import config, fetch, schema  # noqa: E402
from devxdk_manifest.sources import go, node  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

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

    # Only components declared scrape in the config AND with an implemented
    # source are regenerated; this keeps the config the single source of truth.
    scrape_components = {c for c, _l, _p, _plat in cfg.scrape_keys()}

    rc = 0
    for name in sorted(scrape_components & SOURCES.keys()):
        try:
            data = fetcher_build(SOURCES[name], fetcher)
        except Exception as e:  # noqa: BLE001 - report and continue other components
            sys.stderr.write(f"ERROR scraping {name}: {e}\n")
            rc = 1
            continue
        rel = data["releases"][0]
        path = REPO_ROOT / f"{name}.json"
        if args.dry_run:
            sys.stderr.write(f"[dry-run] {name}.json -> {rel['version']} ({rel.get('released_at', '')})\n")
        else:
            schema.write(path, data)
            sys.stderr.write(f"{name}.json -> {rel['version']} ({rel.get('released_at', '')})\n")

    skipped = sorted(scrape_components - SOURCES.keys())
    if skipped:
        sys.stderr.write(f"no scrape source implemented yet (left untouched): {', '.join(skipped)}\n")
    return rc


def fetcher_build(builder, fetcher):
    return builder(fetcher)


if __name__ == "__main__":
    sys.exit(main())
