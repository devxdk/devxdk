#!/usr/bin/env python3
"""Write a pending record for a built or adopted asset.

The build-runtimes finalize job calls this once per verified leg after its asset
is published to a Release, dropping pending/<component>-<version>[-rN]-<goos>-<goarch>.json.
apply_pending (run by scrape-and-sign) then folds it into the manifest + ledger.

The record carries only the ordering identity + the artifact tuple; channel and
released_at are deliberately omitted (apply_pending derives them). Validates
against the config before writing. Standard library only.
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from devxdk_manifest import config, pending  # noqa: E402
from devxdk_manifest.config import ConfigError  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PENDING_DIR = REPO_ROOT / "pending"


def pending_filename(component, version, revision, platform):
    """<component>-<version>[-rN]-<goos>-<goarch>.json (rN only for revision >= 2)."""
    suffix = "" if revision <= 1 else f"-r{revision}"
    plat = platform.replace("/", "-")
    return f"{component}-{version}{suffix}-{plat}.json"


def build_record(args):
    return {
        "component": args.component,
        "version": args.version,
        "platform": args.platform,
        "line": args.line,
        "ordering_kind": args.ordering_kind,
        "provider": args.provider,
        "epoch": args.epoch,
        "revision": args.revision,
        "source_version": args.source_version,
        "url": args.url,
        "sha256": args.sha256,
        "size_bytes": args.size_bytes,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write a pending record for a managed asset.")
    ap.add_argument("--component", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--platform", required=True)
    ap.add_argument("--line", required=True)
    ap.add_argument("--ordering-kind", required=True, choices=["built", "adopted"])
    ap.add_argument("--provider", required=True)
    ap.add_argument("--epoch", type=int, required=True)
    ap.add_argument("--revision", type=int, required=True)
    ap.add_argument("--source-version", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--sha256", required=True)
    ap.add_argument("--size-bytes", type=int, required=True)
    args = ap.parse_args(argv)

    data = build_record(args)
    # Parse-validate it (catches a wrong kind, missing field, etc.) and confirm
    # the target (component, line, platform) is a configured managed key.
    rec = pending.PendingRecord.from_dict(data)
    cfg = config.load()
    try:
        plat = cfg.find_platform(rec.component, rec.line, rec.platform)
    except ConfigError as e:
        sys.stderr.write(f"add_built_release: {e}\n")
        return 1
    if not plat.managed:
        sys.stderr.write(f"add_built_release: {rec.platform} is a scrape platform, not managed\n")
        return 1

    PENDING_DIR.mkdir(exist_ok=True)
    out = PENDING_DIR / pending_filename(rec.component, rec.version, rec.revision, rec.platform)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    sys.stderr.write(f"wrote {out.relative_to(REPO_ROOT)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
