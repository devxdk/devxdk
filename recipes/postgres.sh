#!/usr/bin/env bash
# Adopt recipe for postgres on every platform (theseus-rs/postgresql-binaries).
#
# Adopt = re-host BY REFERENCE: the manifest points at the upstream theseus
# tarball, verified by its published .sha256 sidecar. theseus ships native builds
# for all four targets (including aarch64 macOS) and wraps each in a single
# postgresql-<ver>-<triple>/ dir, so the app's ArchiveStrip=1 lands bin/postgres
# with no repack. Per LEG_ITEMS line, this re-resolves the asset for its platform
# (asserting the planned full version is still newest), downloads it,
# self-hash-verifies against the sidecar, extracts, and smokes with a real initdb
# plus pg_ctl start/stop, then writes an adopt .meta.json (upstream url, no
# archive; version=MAJOR.MINOR, source_version=full).
set -euo pipefail

leg="${1:?usage: postgres.sh <leg>}"
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

  case "$platform" in
    windows/amd64) triple="x86_64-pc-windows-msvc"; exe=".exe"; is_win=1 ;;
    linux/amd64)   triple="x86_64-unknown-linux-gnu"; exe=""; is_win=0 ;;
    darwin/amd64)  triple="x86_64-apple-darwin"; exe=""; is_win=0 ;;
    darwin/arm64)  triple="aarch64-apple-darwin"; exe=""; is_win=0 ;;
    *) echo "::error::postgres: unsupported platform $platform" >&2; exit 1 ;;
  esac

  # --- resolve the upstream asset for THIS platform, asserting identity ------
  resolved=$(python3 - "$source_version" "$line" "$platform" <<'PY'
import sys
sys.path.insert(0, "scripts")
from devxdk_manifest import fetch, resolvers
want, line, platform = sys.argv[1], sys.argv[2], sys.argv[3]
got = resolvers.theseus_newest(fetch.Fetcher(), line)
if got["source_version"] != want:
    sys.stderr.write(f"theseus newest {got['source_version']} != planned {want}\n")
    sys.exit(2)
a = got["platforms"][platform]
print(a["url"], a["sha256"], a["size"])
PY
) || { echo "::error::$leg: could not resolve theseus asset for $platform $source_version" >&2; exit 1; }
  read -r url sha size <<< "$resolved"
  echo "adopt postgres $source_version (manifest $version) $platform: $url"

  # --- download + self-hash verify against the published sidecar ------------
  work="$outdir/work-$version-${platform//\//-}"; rm -rf "$work"; mkdir -p "$work"
  asset="$work/pg.tar.gz"
  curl -fsSL --retry 6 --retry-max-time 300 --max-time 900 -o "$asset" "$url"
  got_sha=$(python3 -c "import hashlib,sys;h=hashlib.sha256();f=open(sys.argv[1],'rb')
while True:
 b=f.read(1<<20)
 if not b: break
 h.update(b)
print(h.hexdigest())" "$asset")
  [ "$got_sha" = "$sha" ] || { echo "::error::sha256 mismatch: got $got_sha want $sha" >&2; exit 1; }
  got_size=$(python3 -c "import os,sys;print(os.path.getsize(sys.argv[1]))" "$asset")

  # --- extract + layout check (theseus wraps in postgresql-<ver>-<triple>/) --
  tar xzf "$asset" -C "$work"
  pgroot="$work/postgresql-$source_version-$triple"
  for b in postgres initdb pg_ctl; do
    [ -f "$pgroot/bin/$b$exe" ] || { echo "::error::layout: bin/$b$exe missing under the wrapper dir" >&2; exit 1; }
  done
  if find "$pgroot" \( -name '.devxdk-complete' -o -name '.devxdk-initialized' \) | grep -q .; then
    echo "::error::layout: bundle carries DevXDK marker files" >&2; exit 1
  fi

  # --- smoke: real initdb + pg_ctl start/stop on the target OS --------------
  data="$work/pgdata"
  "$pgroot/bin/initdb$exe" -D "$data" -U postgres -A trust --encoding=UTF8 >/dev/null 2>&1 \
    || { echo "::error::smoke: initdb failed" >&2; exit 1; }
  if [ "$is_win" = 1 ]; then
    # Windows postgres has no Unix socket; start on a TCP loopback port.
    "$pgroot/bin/pg_ctl$exe" -D "$data" -o "-p 54329 -c listen_addresses=127.0.0.1" -w start >/dev/null 2>&1 \
      || { echo "::error::smoke: pg_ctl start failed" >&2; exit 1; }
  else
    export LD_LIBRARY_PATH="$pgroot/lib:${LD_LIBRARY_PATH:-}"
    export DYLD_LIBRARY_PATH="$pgroot/lib:${DYLD_LIBRARY_PATH:-}"
    sock="$work/sock"; mkdir -p "$sock"
    "$pgroot/bin/pg_ctl" -D "$data" -o "-p 54329 -k $sock -c listen_addresses=''" -w start >/dev/null 2>&1 \
      || { echo "::error::smoke: pg_ctl start failed" >&2; exit 1; }
  fi
  "$pgroot/bin/pg_ctl$exe" -D "$data" -w stop >/dev/null 2>&1 || true
  echo "smoke: postgres $source_version $platform initdb + pg_ctl start/stop OK"

  # --- meta (adopt: version=MAJOR.MINOR, source_version=full, url=upstream) --
  URL="$url" SHA="$sha" SIZE="$got_size" VERSION="$version" PLATFORM="$platform" \
  LINE="$line" PROVIDER="$provider" EPOCH="$epoch" REVISION="$revision" \
  SOURCE_VERSION="$source_version" \
  python3 - "$outdir/postgres-$version-${platform//\//-}.meta.json" <<'PY'
import json, os, sys
meta = {
    "component": "postgres",
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
        "recipe": "postgres-theseus",
        "source_url": os.environ["URL"],
        "run_url": (os.environ.get("GITHUB_SERVER_URL", "") + "/" + os.environ.get("GITHUB_REPOSITORY", "") +
                    "/actions/runs/" + os.environ["GITHUB_RUN_ID"]) if os.environ.get("GITHUB_RUN_ID") else "local",
    },
}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    fh.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
PY

  rm -rf "$work"
  echo "adopted postgres $version ($source_version) $platform (sha256 $sha, $got_size bytes)"
done

echo "$leg: done"
