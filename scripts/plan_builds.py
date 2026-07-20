#!/usr/bin/env python3
"""Build-leg planning CLI.

  --leg-ids-json       Emit the JSON array of static leg ids (the plan job feeds
                       it to each leg's membership `if:` gate).
  --check-parity FILE  Assert build-runtimes.yml's leg-* caller jobs match the
                       configured managed (component, platform) pairs both ways.

Standard library only.
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from devxdk_manifest import config, plan  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build-leg planning.")
    ap.add_argument("--leg-ids-json", action="store_true", help="print the JSON array of static leg ids")
    ap.add_argument("--check-parity", metavar="WORKFLOW", help="check config <-> caller-job parity")
    args = ap.parse_args(argv)

    cfg = config.load()
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
    ap.error("choose --leg-ids-json or --check-parity")


if __name__ == "__main__":
    sys.exit(main())
