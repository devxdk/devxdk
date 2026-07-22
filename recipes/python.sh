#!/usr/bin/env bash
# Adopt recipe for python (astral python-build-standalone).
#
# Adopt = re-host BY REFERENCE: the manifest points at the upstream install_only
# asset, verified by astral's published sha256 digest. Per LEG_ITEMS line, this
# recipe re-resolves the asset for its platform (asserting the planned version is
# still newest), downloads it, self-hash-verifies against the digest, extracts +
# smokes on the target OS (python -V + `python -m pip` — astral ships no pip.exe,
# which is why the app's pip shim runs `-m pip`), and writes a .meta.json with
# ordering_kind=adopted and url=upstream. It produces NO archive — nothing is
# rehosted, so the leg's only member is the meta the handoff manifest covers.
set -euo pipefail

leg="${1:?usage: python.sh <leg>}"
items_json="${LEG_ITEMS:?LEG_ITEMS must carry the per-line plan items}"
repo_root="$(pwd)"
outdir="$repo_root/build/$leg"
mkdir -p "$outdir"

count=$(python3 -c "import json,sys;print(len(json.loads(sys.argv[1])))" "$items_json")

for i in $(seq 0 $((count - 1))); do
  item() { python3 -c "import json,sys;print(json.loads(sys.argv[1])[$i].get(sys.argv[2],''))" "$items_json" "$1"; }
  mode=$(item mode); version=$(item version); platform=$(item platform)
  source_version=$(item source_version); line=$(item line)
  provider=$(item provider); epoch=$(item epoch); revision=$(item revision)

  [ "$mode" = "build" ] || { echo "::error::$leg item $version has mode '$mode'; the adopt recipe only runs the build (fetch+verify+smoke) mode" >&2; exit 1; }

  # --- resolve the upstream asset for THIS platform, asserting identity ------
  # The planner and the leg both resolve "newest"; if astral published a newer
  # version between them, fail rather than adopt an off-plan version (a re-run
  # picks up the new newest deterministically).
  resolved=$(python3 - "$platform" "$source_version" <<'PY'
import sys
sys.path.insert(0, "scripts")
from devxdk_manifest import fetch, resolvers
platform, want = sys.argv[1], sys.argv[2]
line = ".".join(want.split(".")[:2])
got = resolvers.astral_newest(fetch.Fetcher(), line)
if got["source_version"] != want:
    sys.stderr.write(f"astral newest {got['source_version']} != planned {want}\n")
    sys.exit(2)
a = got["platforms"][platform]
print(a["url"], a["sha256"], a["size"])
PY
) || { echo "::error::$leg: could not resolve astral asset for $platform $source_version" >&2; exit 1; }
  read -r url sha size <<< "$resolved"
  echo "adopt python $source_version $platform: $url"

  # --- download + self-hash verify against the published digest -------------
  work="$outdir/work-$version"; rm -rf "$work"; mkdir -p "$work"
  asset="$work/python.tar.gz"
  curl -fsSL --retry 6 --retry-max-time 300 --max-time 600 -o "$asset" "$url"
  got_sha=$(python3 -c "import hashlib,sys;h=hashlib.sha256();f=open(sys.argv[1],'rb')
while True:
 b=f.read(1<<20)
 if not b: break
 h.update(b)
print(h.hexdigest())" "$asset")
  [ "$got_sha" = "$sha" ] || { echo "::error::sha256 mismatch: got $got_sha want $sha" >&2; exit 1; }
  got_size=$(python3 -c "import os,sys;print(os.path.getsize(sys.argv[1]))" "$asset")

  # --- extract + layout check (astral install_only wraps everything in python/) ---
  tar xzf "$asset" -C "$work"
  case "$platform" in
    windows/*) pybin="$work/python/python.exe" ;;
    *)         pybin="$work/python/bin/python3" ;;
  esac
  [ -f "$pybin" ] || { echo "::error::layout: $pybin missing after extract" >&2; exit 1; }
  if find "$work/python" \( -name '.devxdk-complete' -o -name '.devxdk-initialized' \) | grep -q .; then
    echo "::error::layout: bundle carries DevXDK marker files" >&2; exit 1
  fi

  # --- smoke on the target OS -----------------------------------------------
  "$pybin" -V 2>&1 | grep -q "$source_version" \
    || { echo "::error::smoke: python -V does not report $source_version" >&2; exit 1; }
  "$pybin" -m pip --version >/dev/null 2>&1 \
    || { echo "::error::smoke: 'python -m pip' failed" >&2; exit 1; }
  echo "smoke: python $source_version -V + -m pip OK"

  # --- meta (adopt: url=upstream, no archive) -------------------------------
  URL="$url" SHA="$sha" SIZE="$got_size" VERSION="$version" PLATFORM="$platform" \
  LINE="$line" PROVIDER="$provider" EPOCH="$epoch" REVISION="$revision" \
  SOURCE_VERSION="$source_version" \
  python3 - "$outdir/python-$version-${platform//\//-}.meta.json" <<'PY'
import json, os, sys
meta = {
    "component": "python",
    "version": os.environ["VERSION"],
    "platform": os.environ["PLATFORM"],
    "line": os.environ["LINE"],
    "ordering_kind": "adopted",
    "provider": os.environ["PROVIDER"],
    "epoch": int(os.environ["EPOCH"]),
    "revision": int(os.environ["REVISION"]),
    "source_version": os.environ["SOURCE_VERSION"],
    "url": os.environ["URL"],
    "sha256": os.environ["SHA"],
    "size_bytes": int(os.environ["SIZE"]),
    "provenance": {
        "recipe": "python-astral",
        "source_url": os.environ["URL"],
        "run_url": (os.environ.get("GITHUB_SERVER_URL", "") + "/" + os.environ.get("GITHUB_REPOSITORY", "") +
                    "/actions/runs/" + os.environ["GITHUB_RUN_ID"]) if os.environ.get("GITHUB_RUN_ID") else "local",
    },
}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    fh.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
PY

  rm -rf "$work"
  echo "adopted python $version $platform (sha256 $sha, $got_size bytes)"
done

echo "$leg: done"
