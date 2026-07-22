#!/usr/bin/env bash
# Shared Unix (Linux + macOS) source build for the redis/valkey cache bundles
# (devxdk-redis-unix / devxdk-valkey-unix, Phase 3).
#
# Called by recipes/redis.sh and recipes/valkey.sh with the component name;
# everything else (pinned hash repo, binary names, license files) derives from
# it. Mirrors the msys2 lib but for the native toolchains: verify the source
# tarball against the PINNED hash-repo ref from config/tracked-versions.toml,
# `make BUILD_TLS=yes`, assemble bin/ + (linux) lib/ + licenses/ at archive root
# (redis/valkey ArchiveStrip=0 -> root == ServiceDir), layout-check, smoke in the
# app's EXACT run shape (bin/<name>-server <abs conf>; LD_LIBRARY_PATH=lib on
# linux, static OpenSSL on macOS; PING + SET/GET + shutdown nosave), tar.gz, and
# write .meta.json carrying the copyleft source offer (the upstream tarball is a
# first-class Release asset, uploaded BEFORE the object code by publish).
#
# OpenSSL provenance per OS: Linux ships the runtime libssl/libcrypto in lib/ so
# the TLS build needs no system OpenSSL at the user's machine (Apache-2.0, notice
# shipped, no source offer); macOS STATIC-links OpenSSL (otool -L asserts no
# external libssl/libcrypto) so there is no lib/ there.
set -euo pipefail

component="${1:?usage: rediscache-unix.sh <redis|valkey> <leg>}"
leg="${2:?usage: rediscache-unix.sh <redis|valkey> <leg>}"

case "$component" in
  redis)
    hashes_repo="redis/redis-hashes"
    pins_key="redis_hashes"
    license_files="COPYING LICENSE.txt REDISCONTRIBUTIONS.txt"
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

os="$(uname -s)"   # Linux | Darwin
case "$os" in
  Linux|Darwin) ;;
  *) echo "::error::rediscache-unix runs on Linux/macOS, not $os" >&2; exit 1 ;;
esac

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

# Toolchain: build-essential/libssl-dev on Linux (GitHub images carry them; the
# install is an idempotent no-op there); Homebrew openssl@3 on macOS for the
# static link. Fail loudly if the static OpenSSL prefix is missing on macOS.
if [ "$os" = Linux ]; then
  if ! pkg-config --exists openssl 2>/dev/null; then
    sudo apt-get update -y >/dev/null
    sudo apt-get install -y build-essential libssl-dev pkg-config >/dev/null
  fi
else
  brew list openssl@3 >/dev/null 2>&1 || brew install openssl@3 >/dev/null
  ssl_prefix="$(brew --prefix openssl@3)"
  [ -f "$ssl_prefix/lib/libssl.a" ] && [ -f "$ssl_prefix/lib/libcrypto.a" ] \
    || { echo "::error::macOS: static libssl.a/libcrypto.a missing under $ssl_prefix/lib" >&2; exit 1; }
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
  case "$platform" in
    linux/amd64|darwin/amd64|darwin/arm64) ;;
    *) echo "::error::$leg is the unix recipe; platform $platform is not its target" >&2; exit 1 ;;
  esac

  # --- verify the source against the pinned hashes file -------------------
  entry=$(grep -E "^hash ${component}-${source_version}\.tar\.gz sha256 " "$hashes_file" | head -1)
  [ -n "$entry" ] || { echo "::error::${component}-${source_version}.tar.gz not in $hashes_repo@$ref" >&2; exit 1; }
  src_sha=$(echo "$entry" | awk '{print $4}')
  src_url=$(echo "$entry" | awk '{print $5}' | sed 's|^http://|https://|')

  work="$outdir/work-$version"
  rm -rf "$work" && mkdir -p "$work"
  curl -fsSL --retry 6 --retry-max-time 300 --max-time 300 -o "$work/src.tar.gz" "$src_url"
  echo "$src_sha  $work/src.tar.gz" | shasum -a 256 -c -
  tar xzf "$work/src.tar.gz" -C "$work"
  srcdir=$(find "$work" -maxdepth 1 -mindepth 1 -type d | head -1)
  [ -n "$srcdir" ] || { echo "::error::source tarball produced no directory" >&2; exit 1; }

  # --- build (product binaries; BUILD_TLS on both OSes) -------------------
  if [ "$os" = Linux ]; then
    make -C "$srcdir/src" -j"$(nproc)" \
      MALLOC=libc BUILD_TLS=yes \
      "$component-server" "$component-cli" "$component-check-aof" "$component-check-rdb" \
      >"$work/build.log" 2>&1 || { tail -60 "$work/build.log" >&2; exit 1; }
  else
    # macOS: link OpenSSL STATICALLY. redis appends `-lssl -lcrypto`, and a linker
    # prefers a .dylib over a .a in the same -L dir — so point -L at a dir holding
    # ONLY the static archives, forcing static resolution. otool -L asserts it below.
    # CFLAGS/LDFLAGS ride the ENVIRONMENT (not make args) so they reach the
    # deps/hiredis sub-make too (the proven msys2 pattern).
    staticssl="$work/staticssl"; mkdir -p "$staticssl"
    ln -sf "$ssl_prefix/lib/libssl.a" "$staticssl/libssl.a"
    ln -sf "$ssl_prefix/lib/libcrypto.a" "$staticssl/libcrypto.a"
    CFLAGS="-I$ssl_prefix/include" LDFLAGS="-L$staticssl" \
    make -C "$srcdir/src" -j"$(sysctl -n hw.ncpu)" \
      MALLOC=libc BUILD_TLS=yes \
      "$component-server" "$component-cli" "$component-check-aof" "$component-check-rdb" \
      >"$work/build.log" 2>&1 || { tail -60 "$work/build.log" >&2; exit 1; }
  fi

  # --- assemble bin/ (+ linux lib/) + licenses/ at archive root ----------
  stage="$outdir/stage-$version"
  rm -rf "$stage" && mkdir -p "$stage/bin" "$stage/licenses"
  for b in server cli check-aof check-rdb; do
    cp "$srcdir/src/$component-$b" "$stage/bin/"   # check-* are symlinks->server; cp dereferences
  done
  chmod 0755 "$stage/bin/"*

  if [ "$os" = Linux ]; then
    # Ship the OpenSSL runtime the build linked against so the user's machine
    # needs no system OpenSSL (found via LD_LIBRARY_PATH=<ServiceDir>/lib).
    # Capture ldd output to a var first: a `| while read` loop runs in a subshell
    # where `exit 1` cannot abort the recipe.
    mkdir -p "$stage/lib"
    sofiles=$(ldd "$stage/bin/$component-server" | awk '$1 ~ /^lib(ssl|crypto)\.so/ {print $3}')
    [ -n "$sofiles" ] || { echo "::error::ldd reported no libssl/libcrypto for $component-server (BUILD_TLS did not link OpenSSL?)" >&2; exit 1; }
    for so in $sofiles; do
      [ -f "$so" ] || { echo "::error::ldd path '$so' for a libssl/libcrypto entry is not a file" >&2; exit 1; }
      cp -L "$so" "$stage/lib/$(basename "$so")"
    done
    for want in libssl libcrypto; do
      ls "$stage/lib/$want".so* >/dev/null 2>&1 \
        || { echo "::error::$want not copied into lib/ (BUILD_TLS did not link OpenSSL?)" >&2; exit 1; }
    done
    cp /usr/share/doc/libssl3/copyright "$stage/licenses/openssl-copyright.txt" 2>/dev/null \
      || cp /usr/share/doc/libssl3t64/copyright "$stage/licenses/openssl-copyright.txt" \
      || { echo "::error::OpenSSL license notice (dpkg copyright) not found" >&2; exit 1; }
  else
    # Static link: the binary must carry NO external libssl/libcrypto reference.
    if otool -L "$stage/bin/$component-server" | grep -Eiq 'lib(ssl|crypto)'; then
      echo "::error::macOS: $component-server still links a dynamic libssl/libcrypto (static link failed):" >&2
      otool -L "$stage/bin/$component-server" >&2; exit 1
    fi
    cp "$ssl_prefix/LICENSE.txt" "$stage/licenses/openssl-LICENSE.txt" 2>/dev/null \
      || cp "$ssl_prefix/LICENSE" "$stage/licenses/openssl-LICENSE.txt" \
      || { echo "::error::OpenSSL license notice not found under $ssl_prefix" >&2; exit 1; }
  fi

  copied_any=0
  for lf in $license_files; do
    [ -f "$srcdir/$lf" ] && cp "$srcdir/$lf" "$stage/licenses/" && copied_any=1
  done
  [ "$copied_any" = 1 ] || { echo "::error::no source license file found (looked for: $license_files)" >&2; exit 1; }

  # --- layout check --------------------------------------------------------
  for f in "bin/$component-server" "bin/$component-cli"; do
    [ -f "$stage/$f" ] || { echo "::error::layout: $f missing under archive root" >&2; exit 1; }
  done
  if find "$stage" -name '.devxdk-complete' -o -name '.devxdk-initialized' | grep -q .; then
    echo "::error::layout: bundle must not contain DevXDK marker files" >&2; exit 1
  fi

  # --- smoke: the app's exact shape (absolute conf; LD path on linux) -----
  smoke="$outdir/smoke-$version"
  rm -rf "$smoke" && mkdir -p "$smoke/etc" "$smoke/data"
  conf="$smoke/etc/$component.conf"
  printf 'port %s\nbind 127.0.0.1\ndir %s\nsave ""\nappendonly no\ndaemonize no\n' \
    "$smoke_port" "$smoke/data" > "$conf"
  "$stage/bin/$component-server" --version | grep -q "v=$source_version" \
    || { echo "::error::smoke: --version does not report $source_version" >&2; exit 1; }
  # Launch the server exactly as the app does (absolute conf; lib/ on the loader
  # path on linux). Branch rather than expand an empty env array — macOS ships
  # bash 3.2, where "${arr[@]}" on an empty array under `set -u` is unbound.
  if [ "$os" = Linux ]; then
    LD_LIBRARY_PATH="$stage/lib" "$stage/bin/$component-server" "$conf" >"$smoke/server.log" 2>&1 &
  else
    "$stage/bin/$component-server" "$conf" >"$smoke/server.log" 2>&1 &
  fi
  server_pid=$!
  ok=""
  for _ in $(seq 1 20); do
    if "$stage/bin/$component-cli" -p "$smoke_port" ping 2>/dev/null | grep -q PONG; then ok=1; break; fi
    kill -0 "$server_pid" 2>/dev/null || { echo "::error::smoke: server exited early"; cat "$smoke/server.log" >&2; exit 1; }
    sleep 1
  done
  [ -n "$ok" ] || { echo "::error::smoke: no PONG on port $smoke_port"; cat "$smoke/server.log" >&2; exit 1; }
  "$stage/bin/$component-cli" -p "$smoke_port" set devxdk-smoke ok >/dev/null
  [ "$("$stage/bin/$component-cli" -p "$smoke_port" get devxdk-smoke)" = "ok" ] \
    || { echo "::error::smoke: SET/GET round-trip failed" >&2; exit 1; }
  "$stage/bin/$component-cli" -p "$smoke_port" shutdown nosave 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  echo "smoke: $component $source_version PONG + SET/GET + shutdown OK"

  # --- corresponding source (the copyleft offer) --------------------------
  # Object code is never public without its source: the Release carries the exact
  # verified upstream tarball as a first-class asset (publish uploads it BEFORE
  # the object-code archive). Redis 8.x is source-available (AGPL/SSPL/RSAL); the
  # notice + this tarball satisfy the offer. Valkey is BSD-3 — the tarball is
  # harmless extra provenance (the msys2 lib attaches it for both too).
  upstream_src="$component-$source_version-src.tar.gz"
  cp "$work/src.tar.gz" "$outdir/$upstream_src"
  src_sha_asset=$(shasum -a 256 "$outdir/$upstream_src" | awk '{print $1}')

  # --- archive + meta ------------------------------------------------------
  suffix=""; [ "$revision" -ge 2 ] && suffix="-r$revision"
  archive="$component-$version$suffix-${platform//\//-}.tar.gz"
  ( cd "$stage" && COPYFILE_DISABLE=1 tar czf "$outdir/$archive" -- * )
  zip_sha=$(shasum -a 256 "$outdir/$archive" | awk '{print $1}')
  zip_size=$(wc -c < "$outdir/$archive" | tr -d ' ')

  ARCHIVE="$archive" COMPONENT="$component" VERSION="$version" REVISION="$revision" \
  LINE="$line" PLATFORM="$platform" PROVIDER="$provider" EPOCH="$epoch" \
  SOURCE_VERSION="$source_version" ZIP_SHA="$zip_sha" ZIP_SIZE="$zip_size" \
  SRC_URL="$src_url" SRC_SHA="$src_sha" HASHES_REF="$ref" OS="$os" \
  UPSTREAM_SRC="$upstream_src" UPSTREAM_SRC_SHA="$src_sha_asset" \
  python3 - "$outdir/$archive.meta.json" <<'EOF'
import json, os, sys
# release_assets is the ordered upload set: the corresponding-source tarball
# (object_code false) first, the object-code archive last. build_members sorts
# by the flag, and the reconciler re-asserts source-before-object-code.
release_assets = [
    {"name": os.environ["UPSTREAM_SRC"], "sha256": os.environ["UPSTREAM_SRC_SHA"], "object_code": False},
    {"name": os.environ["ARCHIVE"], "sha256": os.environ["ZIP_SHA"], "object_code": True},
]
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
    "release_assets": release_assets,
    "provenance": {
        "recipe": f"{os.environ['COMPONENT']}-unix",
        "os": os.environ["OS"],
        "source_url": os.environ["SRC_URL"],
        "source_sha256": os.environ["SRC_SHA"],
        "hashes_ref": os.environ["HASHES_REF"],
        "run_url": (os.environ.get("GITHUB_SERVER_URL", "") + "/" + os.environ.get("GITHUB_REPOSITORY", "") +
                    "/actions/runs/" + os.environ["GITHUB_RUN_ID"]) if os.environ.get("GITHUB_RUN_ID") else "local",
    },
}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    fh.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
EOF

  # Only archive + meta + the source asset may remain (member-digest covered).
  rm -rf "$work" "$stage" "$smoke"
  echo "built $archive (sha256 $zip_sha, $zip_size bytes)"
done

rm -f "$hashes_file"
echo "$leg: done"
