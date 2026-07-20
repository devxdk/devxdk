#!/usr/bin/env python3
"""Write or verify a build leg's member-digest manifest.

  handoff.py write  <dir>                  -> write <dir>/manifest.json, print its sha256
  handoff.py verify <dir> [--expect <sha>] -> verify manifest.json + every member

build-runtime-leg.yml runs `write` after the recipe and exports the printed sha256
as a job output; build-runtimes' publish job runs `verify --expect <that sha>` over
each leg artifact it downloaded by immutable id, hard-erroring before any byte
reaches a Release. Standard library only.
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from devxdk_manifest import handoff  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build-leg member-digest handoff.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="write manifest.json into a leg directory, print its sha256")
    w.add_argument("directory")
    v = sub.add_parser("verify", help="verify a downloaded leg artifact against its manifest.json")
    v.add_argument("directory")
    v.add_argument("--expect", help="expected sha256 of manifest.json (the leg's job output)")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "write":
            sys.stdout.write(handoff.write(args.directory) + "\n")
        else:
            handoff.verify(args.directory, args.expect)
            sys.stderr.write(f"handoff: {args.directory} verified\n")
    except handoff.HandoffError as e:
        sys.stderr.write(f"handoff: FAILED: {e}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
