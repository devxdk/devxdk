#!/usr/bin/env python3
"""Apply line retirement / reactivation transitions from the config.

Retires every line marked `retired = true` (tombstoning its records + snapshotting
its releases) and reactivates every non-retired line that still has tombstones,
then rebuilds the affected manifests and saves both state files. A no-op when no
line is retired and none is tombstoned, so it runs safely first in the
scrape-and-sign transaction. Fails closed. Standard library only.
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from devxdk_manifest import config, lifecycle, merge, schema, strictjson  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def apply(repo_root=REPO_ROOT):
    repo_root = pathlib.Path(repo_root)
    cfg = config.load()
    scrape_state = merge.ScrapeState.load(repo_root / "state" / "scrape-versions.json")
    ledger = merge.LedgerState.load(repo_root / "state" / "asset-revisions.json")

    affected = lifecycle.apply_lifecycle(cfg, scrape_state, ledger, repo_root)
    if not affected:
        return []

    # PREFLIGHT: resolve every target before writing any of them (see
    # schema.resolve). Nothing is written until all of them validate.
    resolved = []
    for comp in sorted(affected):
        mpath = repo_root / f"{comp}.json"
        existing = schema.load(mpath)
        manifest = merge.recompose(comp, existing["display_name"], existing["kind"], cfg, scrape_state, ledger)
        resolved.append((mpath, schema.resolve(mpath, manifest)))
    for mpath, doc in resolved:
        schema.write_resolved(mpath, doc)
    scrape_state.save(repo_root / "state" / "scrape-versions.json")
    ledger.save(repo_root / "state" / "asset-revisions.json")
    return sorted(affected)


def main(argv=None):
    argparse.ArgumentParser(description="Apply line retirement/reactivation.").parse_args(argv)
    try:
        affected = apply(REPO_ROOT)
    except (lifecycle.LifecycleError, merge.GuardError, config.ConfigError,
            schema.SchemaError, strictjson.StrictJSONError) as e:
        # ConfigError too (L34): reactivate_line -> find_platform raises it,
        # and so can config.load() — both must fail closed with the clean
        # "nothing written" report, never an uncaught traceback. SchemaError and
        # StrictJSONError join for the same reason: a ValueError subclass none
        # of these tuples names is an uncaught traceback, not a refusal.
        sys.stderr.write(f"apply_lifecycle: FAILED (nothing written): {e}\n")
        return 1
    if affected:
        sys.stderr.write(f"apply_lifecycle: rebuilt {', '.join(affected)}\n")
    else:
        sys.stderr.write("apply_lifecycle: no transitions\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
