#!/usr/bin/env python3
"""Build-leg planning CLI.

  --leg-map-json       Resolve newest versions per enabled provider, classify
                       each (component, line, platform) against the ledger,
                       manifests, pending queue, and published Release assets,
                       and print {leg_id: [item, ...]} for legs WITH work.
                       Options: --components/--platforms (comma filters),
                       --version (pin instead of newest), --force (next -rN).
                       Release-asset lookups go through the gh CLI
                       (GH_TOKEN); --assume-no-releases replaces them with
                       "absent" for offline/local planning.
  --leg-ids-json       Emit the JSON array of ALL static leg ids.
  --check-parity FILE  Assert build-runtimes.yml's leg-* caller jobs match the
                       configured managed (component, platform) pairs both ways.

Standard library only.
"""

import argparse
import json
import pathlib
import subprocess
import sys
import os
import re

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from devxdk_manifest import config, fetch, plan, resolvers, strictjson, publication, receipts, current_state  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class ReleaseIndex:
    """Enumerate tags once; cached full asset lists also handle revision gaps."""
    def __init__(self):
        proc = subprocess.run(['gh', 'api', '--paginate', 'repos/devxdk/devxdk/releases?per_page=100'],
                              capture_output=True, text=True, check=True)
        self.tags = {r['tag_name'] for page in strictjson.split_json_arrays(proc.stdout) for r in page}
        self.cache = {}

    def revisions(self, component, version):
        pattern = re.compile(re.escape(component + '-' + version) + r'(?:-r([1-9][0-9]*))?$')
        return sorted({int(m[1]) if m[1] else 1 for tag in self.tags if (m := pattern.fullmatch(tag))})

    def __call__(self, tag):
        if tag not in self.tags:
            return None
        if tag not in self.cache:
            self.cache[tag] = gh_release_assets(tag)
        return self.cache[tag]


def gh_release_assets(tag):
    """Asset names of a Release, or None when the release does not exist.
    Any OTHER gh failure is fatal — the plan must not misread an outage as
    'nothing published' and re-plan work that would collide."""
    proc = subprocess.run(
        ["gh", "release", "view", tag, "--repo", "devxdk/devxdk",
         "--json", "assets", "--jq", "[.assets[].name]"],
        capture_output=True, text=True)
    if proc.returncode == 0:
        return strictjson.loads(proc.stdout)
    if "release not found" in proc.stderr.lower():
        return None
    raise plan.PlanError(f"gh release view {tag}: {proc.stderr.strip()}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build-leg planning.")
    ap.add_argument("--leg-map-json", action="store_true", help="print the resolved leg map")
    ap.add_argument("--operation-json", action="store_true", help="emit frozen operation targets and work map")
    ap.add_argument("--components", default="", help="comma-separated component filter")
    ap.add_argument("--platforms", default="", help="comma-separated platform filter")
    ap.add_argument("--version", default="",
                    help="assert/limit the single selected component's CURRENT "
                         "resolved version (not an arbitrary pin; needs --force "
                         "to rebuild a published version)")
    ap.add_argument("--force", action="store_true", help="publish the next unused revision")
    ap.add_argument("--assume-no-releases", action="store_true",
                    help="treat every Release as absent (offline/local planning)")
    ap.add_argument("--leg-ids-json", action="store_true", help="print the JSON array of static leg ids")
    ap.add_argument("--check-parity", metavar="WORKFLOW", help="check config <-> caller-job parity")
    args = ap.parse_args(argv)

    cfg = config.load()
    if args.leg_map_json or args.operation_json:
        with current_state.snapshot(REPO_ROOT) as state_root:
            operation_id = os.environ.get('GITHUB_RUN_ID', 'local')
            saved = state_root / receipts.operation_path(operation_id)
            if args.operation_json and saved.exists():
                print(json.dumps(publication.validate_operation(strictjson.load(saved)), sort_keys=True))
                return 0
            release_assets = (lambda _tag: None) if args.assume_no_releases else ReleaseIndex()
            targets = []
            try:
                legs = plan.build_leg_map(
                    cfg, state_root, fetch.Fetcher(), release_assets,
                    components=[c for c in args.components.split(",") if c] or None,
                    platforms=[p for p in args.platforms.split(",") if p] or None,
                    version_override=args.version or None,
                    force=args.force,
                    targets=targets,
                )
            except (plan.PlanError, resolvers.ResolveError, fetch.FetchError, ValueError) as e:
                sys.stderr.write(f"plan_builds: FAILED: {e}\n")
                return 1
            if args.operation_json:
                sha = subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO_ROOT,text=True).strip()
                operation = {'schema': 1, 'id': operation_id, 'source_commit': sha,
                             'legs': legs, 'targets': targets, 'force': args.force,
                             'filters': {'components': args.components, 'platforms': args.platforms, 'version': args.version}}
                print(json.dumps(publication.validate_operation(operation), sort_keys=True))
            else:
                print(json.dumps(legs, sort_keys=True))
            return 0
    if args.leg_ids_json:
        print(json.dumps(plan.static_leg_ids(cfg)))
        return 0
    if args.check_parity:
        text = pathlib.Path(args.check_parity).read_text(encoding="utf-8")
        errors = plan.check_static_job_parity(cfg, text)
        if errors:
            sys.stderr.write(f"plan_builds: {len(errors)} static-job parity error(s)\n")
            for e in errors:
                sys.stderr.write(f"  - {e}\n")
            return 1
        sys.stderr.write("plan_builds: static-job parity OK\n")
        return 0
    ap.error("choose --leg-map-json, --leg-ids-json, or --check-parity")


if __name__ == "__main__":
    sys.exit(main())
