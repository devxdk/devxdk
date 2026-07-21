#!/usr/bin/env bash
# Shared MSYS2 build for the Windows redis/valkey bundles (devxdk-*-msys2).
#
# Called by recipes/redis.sh and recipes/valkey.sh with the component name;
# everything else (pinned hash repo, binary names, license files) derives from
# it. Runs on a Windows host (git-bash on the runner) and drives the MSYS
# subsystem at C:\msys64 for the actual compile — the exact flow the spike
# verified locally: redis 8.8.0 builds with ZERO source patches, needing only
#   REDIS_CFLAGS=-D_GNU_SOURCE      (debug.c includes dlfcn.h before fmacros.h,
#                                    and Cygwin's Dl_info sits behind __GNU_VISIBLE)
#   CFLAGS=-Wno-char-subscripts     (hiredis -Werror trips newlib's ctype macros)
# and building only the product binaries (the tests/modules target does not
# link under MSYS and is never shipped).
#
# Per LEG_ITEMS item (mode=build): verify the source tarball against the PINNED
# hash-repo ref from config/tracked-versions.toml, build, bundle the four exes +
# every ldd-reported msys DLL at archive root, collect each shipped DLL's pacman
# owner license (copyleft gate: msys-2.0.dll is msys2-runtime), layout-check,
# smoke in the app's exact invocation shape (WorkDir + relative etc/<name>.conf,
# launched OUTSIDE any MSYS environment), zip flat, write .meta.json. Leaves
# EXACTLY <archive>.zip + <archive>.meta.json in build/<leg>/ — the member-digest
# manifest covers every file, so work trees must not linger.
set -euo pipefail

component="${1:?usage: rediscache-msys2.sh <redis|valkey> <leg>}"
leg="${2:?usage: rediscache-msys2.sh <redis|valkey> <leg>}"

case "$component" in
  redis)
    hashes_repo="redis/redis-hashes"
    pins_key="redis_hashes"
    license_files="LICENSE.txt COPYING REDISCONTRIBUTIONS.txt"
    smoke_port=6399
    ;;
  valkey)
    hashes_repo="valkey-io/valkey-hashes"
    pins_key="valkey_hashes"
    license_files="COPYING LICENSE.txt"
    smoke_port=6398
    ;;
  *) echo "::error::unknown rediscache component '$component'" >&2; exit 1 ;;
esac

MSYS2_BASH="${DEVXDK_MSYS2_BASH:-/c/msys64/usr/bin/bash}"
[ -x "$MSYS2_BASH" ] || { echo "::error::MSYS2 bash not found at $MSYS2_BASH" >&2; exit 1; }
msys() { MSYSTEM=MSYS "$MSYS2_BASH" -lc "$*"; }

repo_root="$(pwd)"
outdir="$repo_root/build/$leg"
mkdir -p "$outdir"

# The pinned hash-repo ref is the verification root for the source download.
ref=$(python3 - "$pins_key" <<'EOF'
import sys, tomllib
with open("config/tracked-versions.toml", "rb") as fh:
    cfg = tomllib.load(fh)
print(cfg["pins"][sys.argv[1]]["ref"])
EOF
)
echo "pinned $hashes_repo ref: $ref"

# Toolchain (idempotent; a stale runner package DB gets one -Syu self-heal).
if ! msys 'pacman -S --noconfirm --needed base-devel gcc openssl-devel pkgconf' >/dev/null 2>&1; then
  echo "pacman install failed; refreshing the package database"
  msys 'pacman -Syu --noconfirm' >/dev/null || true
  msys 'pacman -Syu --noconfirm' >/dev/null
  msys 'pacman -S --noconfirm --needed base-devel gcc openssl-devel pkgconf' >/dev/null
fi

hashes_file="$outdir/.hashes-$component"
curl -fsSL --retry 6 --retry-max-time 300 --max-time 60 -o "$hashes_file" \
  "https://raw.githubusercontent.com/$hashes_repo/$ref/README"

items_json="${LEG_ITEMS:?LEG_ITEMS must carry the per-line plan items}"
count=$(python3 -c "import json,sys;print(len(json.loads(sys.argv[1])))" "$items_json")

for i in $(seq 0 $((count - 1))); do
  item() { python3 -c "import json,sys;print(json.loads(sys.argv[1])[$i].get(sys.argv[2],''))" "$items_json" "$1"; }
  mode=$(item mode); version=$(item version); revision=$(item revision)
  line=$(item line); platform=$(item platform); provider=$(item provider)
  epoch=$(item epoch); source_version=$(item source_version)

  [ "$mode" = "build" ] || { echo "::error::$leg item $version has mode '$mode' — only build is implemented in the recipe (finalize-only re-verifies the published asset in publish)" >&2; exit 1; }
  [ "$platform" = "windows/amd64" ] || { echo "::error::$leg is the msys2 recipe; platform $platform is not its target" >&2; exit 1; }

  # --- verify the source against the pinned hashes file -------------------
  entry=$(grep -E "^hash ${component}-${source_version}\.tar\.gz sha256 " "$hashes_file" | head -1)
  [ -n "$entry" ] || { echo "::error::${component}-${source_version}.tar.gz not in $hashes_repo@$ref" >&2; exit 1; }
  src_sha=$(echo "$entry" | awk '{print $4}')
  src_url=$(echo "$entry" | awk '{print $5}' | sed 's|^http://|https://|')

  work="$outdir/work-$version"
  rm -rf "$work" && mkdir -p "$work"
  curl -fsSL --retry 6 --retry-max-time 300 --max-time 300 -o "$work/src.tar.gz" "$src_url"
  echo "$src_sha  $work/src.tar.gz" | sha256sum -c -
  tar xzf "$work/src.tar.gz" -C "$work"
  srcdir=$(find "$work" -maxdepth 1 -mindepth 1 -type d | head -1)
  [ -n "$srcdir" ] || { echo "::error::source tarball produced no directory" >&2; exit 1; }
  srcdir_msys=$(cygpath -u "$(cygpath -w "$srcdir")")

  # --- build (product binaries only; tests/modules never links or ships) --
  # ONE src-level invocation: naming the product binaries builds the needed
  # deps automatically, and the env CFLAGS reaches hiredis's sub-make (its
  # -Werror trips newlib's ctype char-subscript warning otherwise).
  msys "cd '$srcdir_msys/src' && CFLAGS=-Wno-char-subscripts make MALLOC=libc BUILD_TLS=yes REDIS_CFLAGS=-D_GNU_SOURCE ${component}-server ${component}-cli ${component}-check-aof ${component}-check-rdb -j\$(nproc)" >"$work/build.log" 2>&1 \
    || { tail -40 "$work/build.log" >&2; exit 1; }

  # --- bundle: exes + every ldd-reported msys DLL at archive root ---------
  stage="$outdir/stage-$version"
  rm -rf "$stage" && mkdir -p "$stage/licenses"
  for exe in server cli check-aof check-rdb; do
    cp "$srcdir/src/$component-$exe.exe" "$stage/"
  done
  # ldd resolves by the CHILD shell's PATH, which can find another msys-2.0.dll
  # (git-bash ships one) — so take only the NAMES and copy every DLL from the
  # canonical /usr/bin of the MSYS2 install the build actually linked against.
  dlls=$(msys "ldd '$srcdir_msys/src/${component}-server.exe'" | awk '$1 ~ /^msys-.*\.dll$/{print $1}' | sort -u)
  [ -n "$dlls" ] || { echo "::error::ldd reported no msys DLLs — not an MSYS build?" >&2; exit 1; }
  pacman_prov=""
  for dll in $dlls; do
    [ -f "/c/msys64/usr/bin/$dll" ] || { echo "::error::$dll not in /c/msys64/usr/bin" >&2; exit 1; }
    cp "/c/msys64/usr/bin/$dll" "$stage/"
    dll="/usr/bin/$dll"
    # Copyleft gate: ship each DLL's owning package license files verbatim.
    # Layouts differ per package (msys2-runtime keeps COPYING under
    # /usr/share/doc/Cygwin, gcc-libs under /usr/share/licenses), so ask
    # pacman for the actual file list instead of assuming a directory.
    pkg=$(msys "pacman -Qo '$dll'" | awk '{print $(NF-1)}')
    pkgver=$(msys "pacman -Q '$pkg'" | awk '{print $2}')
    pacman_prov="$pacman_prov $pkg=$pkgver"
    lic_files=$(msys "pacman -Ql '$pkg'" | awk '{print $2}' | grep -iE '/(licenses?|copying|license)([./]|$)|/doc/.*(COPYING|LICENSE)' | grep -v '/$' || true)
    [ -n "$lic_files" ] || { echo "::error::no license files recorded for shipped package $pkg (copyleft gate)" >&2; exit 1; }
    mkdir -p "$stage/licenses/$pkg"
    for lf in $lic_files; do
      [ -f "/c/msys64$lf" ] && cp "/c/msys64$lf" "$stage/licenses/$pkg/"
    done
    [ -n "$(ls -A "$stage/licenses/$pkg")" ] || { echo "::error::license files for $pkg listed but not present on disk" >&2; exit 1; }
  done
  copied_any=0
  for lf in $license_files; do
    [ -f "$srcdir/$lf" ] && cp "$srcdir/$lf" "$stage/licenses/" && copied_any=1
  done
  [ "$copied_any" = 1 ] || { echo "::error::no source license file found (looked for: $license_files)" >&2; exit 1; }

  # --- layout check --------------------------------------------------------
  for f in "$component-server.exe" "$component-cli.exe" msys-2.0.dll; do
    [ -f "$stage/$f" ] || { echo "::error::layout: $f missing at archive root" >&2; exit 1; }
  done
  if find "$stage" -name '.devxdk-complete' -o -name '.devxdk-initialized' | grep -q .; then
    echo "::error::layout: bundle must not contain DevXDK marker files" >&2; exit 1
  fi

  # --- smoke: the app's exact shape, OUTSIDE any MSYS environment ---------
  smoke="$outdir/smoke-$version"
  rm -rf "$smoke" && mkdir -p "$smoke/etc"
  cp "$stage"/*.exe "$stage"/*.dll "$smoke/"
  printf 'port %s\nbind 127.0.0.1\ndir ./\nsave ""\nappendonly no\n' "$smoke_port" > "$smoke/etc/$component.conf"
  smoke_win=$(cygpath -w "$smoke")
  "$smoke/$component-server.exe" --version | grep -qi "v=$source_version" \
    || { echo "::error::smoke: --version does not report $source_version" >&2; exit 1; }
  powershell.exe -NoProfile -Command \
    "Start-Process -FilePath '$smoke_win\\$component-server.exe' -ArgumentList 'etc/$component.conf' -WorkingDirectory '$smoke_win' -WindowStyle Hidden" \
    || { echo "::error::smoke: server failed to start" >&2; exit 1; }
  ok=""
  for _ in $(seq 1 20); do
    if "$smoke/$component-cli.exe" -p "$smoke_port" ping 2>/dev/null | grep -q PONG; then ok=1; break; fi
    sleep 1
  done
  [ -n "$ok" ] || { echo "::error::smoke: no PONG on port $smoke_port" >&2; exit 1; }
  "$smoke/$component-cli.exe" -p "$smoke_port" set devxdk-smoke ok >/dev/null
  [ "$("$smoke/$component-cli.exe" -p "$smoke_port" get devxdk-smoke)" = "ok" ] \
    || { echo "::error::smoke: SET/GET round-trip failed" >&2; exit 1; }
  "$smoke/$component-cli.exe" -p "$smoke_port" shutdown nosave 2>/dev/null || true
  echo "smoke: $component $source_version PONG + SET/GET + shutdown OK"

  # --- archive + meta ------------------------------------------------------
  suffix=""; [ "$revision" -ge 2 ] && suffix="-r$revision"
  archive="$component-$version$suffix-windows-amd64.zip"
  python3 scripts/zip_dir.py "$stage" "$outdir/$archive"
  zip_sha=$(sha256sum "$outdir/$archive" | awk '{print $1}')
  zip_size=$(stat -c %s "$outdir/$archive")

  ARCHIVE="$archive" COMPONENT="$component" VERSION="$version" REVISION="$revision" \
  LINE="$line" PLATFORM="$platform" PROVIDER="$provider" EPOCH="$epoch" \
  SOURCE_VERSION="$source_version" ZIP_SHA="$zip_sha" ZIP_SIZE="$zip_size" \
  SRC_URL="$src_url" SRC_SHA="$src_sha" HASHES_REF="$ref" \
  PACMAN_PROV="$(echo $pacman_prov | tr ' ' '\n' | sort -u | tr '\n' ' ')" \
  DLLS="$(echo $dlls | tr '\n' ' ')" \
  python3 - "$outdir/$archive.meta.json" <<'EOF'
import json, os, sys
meta = {
    "component": os.environ["COMPONENT"],
    "version": os.environ["VERSION"],
    "platform": os.environ["PLATFORM"],
    "line": os.environ["LINE"],
    "ordering_kind": "built",
    "provider": os.environ["PROVIDER"],
    "epoch": int(os.environ["EPOCH"]),
    "revision": int(os.environ["REVISION"]),
    "source_version": os.environ["SOURCE_VERSION"],
    "archive": os.environ["ARCHIVE"],
    "sha256": os.environ["ZIP_SHA"],
    "size_bytes": int(os.environ["ZIP_SIZE"]),
    "provenance": {
        "recipe": f"{os.environ['COMPONENT']}-msys2",
        "source_url": os.environ["SRC_URL"],
        "source_sha256": os.environ["SRC_SHA"],
        "hashes_ref": os.environ["HASHES_REF"],
        "pacman": os.environ["PACMAN_PROV"].split(),
        "shipped_dlls": os.environ["DLLS"].split(),
        "run_url": (os.environ.get("GITHUB_SERVER_URL", "") + "/" + os.environ.get("GITHUB_REPOSITORY", "") +
                    "/actions/runs/" + os.environ["GITHUB_RUN_ID"]) if os.environ.get("GITHUB_RUN_ID") else "local",
    },
}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    fh.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
EOF

  # Only archive + meta may remain (the member-digest manifest covers every file).
  rm -rf "$work" "$stage" "$smoke"
  echo "built $archive (sha256 $zip_sha, $zip_size bytes)"
done

rm -f "$hashes_file"
echo "$leg: done"
