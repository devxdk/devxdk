#!/usr/bin/env python3
"""Recheck the planned PHP source without conflating stale feeds with outages."""
import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from devxdk_manifest import fetch, resolvers, versions  # noqa: E402


def confirm(line, version, digest, fetcher=None, sleep=time.sleep):
    fetcher = fetcher or fetch.Fetcher()
    for attempt in range(3):
        try:
            source = resolvers.php_spc_newest(fetcher, line)
        except fetch.FetchError as exc:
            return 3, f"php.net fetch failed: {exc}"
        except (ValueError, resolvers.ResolveError) as exc:
            return 4, f"invalid upstream metadata: {exc}"
        newest = source["source_version"]
        if newest == version:
            if source["source_sha256"] != digest:
                return 4, f"php.net {version} source digest changed since planning"
            return 0, digest
        if versions.compare_str(newest, version) > 0:
            return 2, f"upstream moved, re-plan: php.net {line} is {newest}, plan wants {version}"
        if attempt < 2:
            sleep(5)
    return 5, f"stale upstream metadata after three reads: php.net {line} reports {newest}, plan wants {version}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("line")
    ap.add_argument("version")
    ap.add_argument("sha256")
    args = ap.parse_args()
    code, message = confirm(args.line, args.version, args.sha256)
    print(message, file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
