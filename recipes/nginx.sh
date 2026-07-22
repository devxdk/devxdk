#!/usr/bin/env bash
# nginx Unix static source build (devxdk-nginx-unix, Phase 3). Linux + macOS.
#
# nginx.org publishes a detached .asc per source tarball and NO .sha256, so the
# source is GPG-verified against the pinned release-manager keys
# (scripts/devxdk_manifest/keys/nginx/*.key + [pins.nginx_keys] fingerprints)
# imported into an ISOLATED keyring. The pinned pcre2/zlib/openssl sources
# ([pins]) are then compiled STATICALLY INTO nginx (--with-<lib>=<src>), so the
# bundle needs no system libraries at the user's machine. The result is the
# nginx-<ver>/ wrapper the bundle contract expects (ArchiveStrip=1): sbin/nginx +
# conf/ (mime.types + fastcgi_params, both needed by the vhost template) + html/.
# Smoke: `nginx -V` module asserts + a real `nginx -t`. tar.gz + .meta.json.
set -euo pipefail

leg="${1:?usage: nginx.sh <leg>}"
case "$leg" in
  nginx-linux-*|nginx-darwin-*) ;;
  *) echo "::error::unexpected nginx leg '$leg' (nginx.sh builds only the unix legs)" >&2; exit 1 ;;
esac

os="$(uname -s)"   # Linux | Darwin
case "$os" in
  Linux|Darwin) ;;
  *) echo "::error::nginx.sh runs on Linux/macOS, not $os" >&2; exit 1 ;;
esac

repo_root="$(pwd)"
outdir="$repo_root/build/$leg"
mkdir -p "$outdir"
keydir="scripts/devxdk_manifest/keys/nginx"

[ "$os" != Linux ] || command -v gcc >/dev/null 2>&1 \
  || { sudo apt-get update -y >/dev/null && sudo apt-get install -y build-essential >/dev/null; }

# --- pins (dep versions/hashes + the nginx key fingerprints) ---------------
eval "$(python3 - <<'PY'
import tomllib
p = tomllib.load(open("config/tracked-versions.toml", "rb"))["pins"]
print(f'openssl_ver={p["openssl"]["version"]}; openssl_sha={p["openssl"]["sha256"]}')
print(f'pcre2_ver={p["pcre2"]["version"]}; pcre2_sha={p["pcre2"]["sha256"]}')
print(f'zlib_ver={p["zlib"]["version"]}; zlib_sha={p["zlib"]["sha256"]}')
print('nginx_fprs="' + " ".join(p["nginx_keys"]["fingerprints"]) + '"')
PY
)"

# --- isolated keyring: import the committed keys, assert the pinned fprs -----
export GNUPGHOME="$outdir/gnupg"; rm -rf "$GNUPGHOME"; mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"
for kf in "$keydir"/*.key; do
  gpg --batch --quiet --import "$kf" 2>/dev/null || { echo "::error::failed to import $kf" >&2; exit 1; }
done
present=$(gpg --batch --with-colons --list-keys 2>/dev/null | awk -F: '/^fpr:/{print $10}' | sort -u)
for fpr in $nginx_fprs; do
  printf '%s\n' "$present" | grep -qx "$fpr" \
    || { echo "::error::pinned nginx key $fpr not present in the keyring after import" >&2; exit 1; }
done
echo "nginx keyring: $(printf '%s\n' "$present" | grep -c .) keys, all pinned fingerprints present"

# --- static dep sources (pinned; verified before use) ----------------------
deproot="$outdir/deps"; rm -rf "$deproot"; mkdir -p "$deproot"
fetch_dep() { # name version sha url
  local f="$deproot/$1-$2.tar.gz"
  curl -fsSL --retry 6 --retry-max-time 300 --max-time 300 -o "$f" "$4"
  echo "$3  $f" | shasum -a 256 -c - >/dev/null || { echo "::error::$1 $2 sha256 mismatch" >&2; exit 1; }
  tar xzf "$f" -C "$deproot"
}
fetch_dep openssl "$openssl_ver" "$openssl_sha" \
  "https://github.com/openssl/openssl/releases/download/openssl-$openssl_ver/openssl-$openssl_ver.tar.gz"
fetch_dep pcre2 "$pcre2_ver" "$pcre2_sha" \
  "https://github.com/PCRE2Project/pcre2/releases/download/pcre2-$pcre2_ver/pcre2-$pcre2_ver.tar.gz"
fetch_dep zlib "$zlib_ver" "$zlib_sha" \
  "https://github.com/madler/zlib/releases/download/v$zlib_ver/zlib-$zlib_ver.tar.gz"
openssl_src="$deproot/openssl-$openssl_ver"
pcre2_src="$deproot/pcre2-$pcre2_ver"
zlib_src="$deproot/zlib-$zlib_ver"

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

  # --- download + GPG-verify the nginx source -----------------------------
  work="$outdir/work-$version"; rm -rf "$work"; mkdir -p "$work"
  src_url="https://nginx.org/download/nginx-$source_version.tar.gz"
  curl -fsSL --retry 6 --retry-max-time 300 --max-time 300 -o "$work/nginx.tar.gz" "$src_url"
  curl -fsSL --retry 6 --retry-max-time 300 --max-time 60 -o "$work/nginx.tar.gz.asc" "$src_url.asc"
  gpg --batch --verify "$work/nginx.tar.gz.asc" "$work/nginx.tar.gz" 2>"$work/gpg.out" \
    || { echo "::error::GPG verification of nginx-$source_version failed:" >&2; cat "$work/gpg.out" >&2; exit 1; }
  grep -q "Good signature" "$work/gpg.out" || { echo "::error::no 'Good signature' for nginx-$source_version" >&2; cat "$work/gpg.out" >&2; exit 1; }
  src_sha=$(shasum -a 256 "$work/nginx.tar.gz" | awk '{print $1}')   # provenance (self-computed post-verify)
  echo "nginx-$source_version: GPG verified; sha256 $src_sha"
  tar xzf "$work/nginx.tar.gz" -C "$work"
  nginx_src="$work/nginx-$source_version"

  # --- configure + build (pinned pcre2/zlib/openssl static IN) ------------
  stage="$outdir/stage-$version"; rm -rf "$stage"; mkdir -p "$stage"
  prefix="$stage/nginx-$version"
  ( cd "$nginx_src" && ./configure \
      --prefix="$prefix" \
      --with-http_ssl_module --with-http_v2_module --with-http_realip_module \
      --with-http_stub_status_module --with-http_gzip_static_module --with-http_sub_module \
      --with-pcre="$pcre2_src" --with-pcre-jit \
      --with-zlib="$zlib_src" \
      --with-openssl="$openssl_src" --with-openssl-opt="no-shared no-tests" \
      >"$work/configure.log" 2>&1 ) || { tail -40 "$work/configure.log" >&2; exit 1; }
  jobs=$([ "$os" = Linux ] && nproc || sysctl -n hw.ncpu)
  ( cd "$nginx_src" && make -j"$jobs" >"$work/build.log" 2>&1 ) || { tail -60 "$work/build.log" >&2; exit 1; }
  ( cd "$nginx_src" && make install >"$work/install.log" 2>&1 ) || { tail -40 "$work/install.log" >&2; exit 1; }

  # --- layout check (nginx-<ver>/ wrapper, ArchiveStrip=1) ----------------
  for f in "sbin/nginx" "conf/mime.types" "conf/fastcgi_params"; do
    [ -e "$prefix/$f" ] || { echo "::error::layout: nginx-$version/$f missing" >&2; exit 1; }
  done
  [ -d "$prefix/html" ] || { echo "::error::layout: nginx-$version/html missing" >&2; exit 1; }
  if find "$prefix" -name '.devxdk-complete' -o -name '.devxdk-initialized' | grep -q .; then
    echo "::error::layout: bundle must not contain DevXDK marker files" >&2; exit 1
  fi

  # --- smoke: -V module asserts + a real -t -------------------------------
  vout=$("$prefix/sbin/nginx" -V 2>&1)
  printf '%s\n' "$vout" | grep -q "nginx/$source_version" \
    || { echo "::error::smoke: nginx -V does not report $source_version" >&2; printf '%s\n' "$vout" >&2; exit 1; }
  for mod in http_ssl_module http_v2_module http_realip_module http_stub_status_module http_gzip_static_module http_sub_module; do
    printf '%s\n' "$vout" | grep -q -- "--with-$mod" \
      || { echo "::error::smoke: nginx built without --with-$mod" >&2; exit 1; }
  done
  # Statically linked: the binary must not depend on a system pcre2/ssl/crypto/z.
  if [ "$os" = Linux ]; then
    if ldd "$prefix/sbin/nginx" | grep -Eiq 'libpcre|libssl|libcrypto|libz\.so'; then
      echo "::error::smoke: nginx dynamically links a bundled dep (static link failed):" >&2
      ldd "$prefix/sbin/nginx" >&2; exit 1
    fi
  else
    if otool -L "$prefix/sbin/nginx" | grep -Eiq 'libpcre|libssl|libcrypto|libz\.'; then
      echo "::error::smoke: nginx dynamically links a bundled dep (static link failed):" >&2
      otool -L "$prefix/sbin/nginx" >&2; exit 1
    fi
  fi
  "$prefix/sbin/nginx" -p "$prefix" -c conf/nginx.conf -t >"$work/nginx-t.log" 2>&1 \
    || { echo "::error::smoke: nginx -t failed" >&2; cat "$work/nginx-t.log" >&2; exit 1; }
  echo "smoke: nginx $source_version -V(6 modules)/static-link/-t OK"

  # The app owns logs at runtime (its config template points error_log/pid at an
  # absolute app-managed dir), and `nginx -t` just wrote a test error.log here —
  # drop logs/ so the bundle ships none (make install created it; the smoke used it).
  rm -rf "$prefix/logs"

  # --- corresponding source (provenance; nginx is BSD-2, no offer required) -
  upstream_src="nginx-$source_version-src.tar.gz"
  cp "$work/nginx.tar.gz" "$outdir/$upstream_src"
  upstream_src_sha=$(shasum -a 256 "$outdir/$upstream_src" | awk '{print $1}')

  # --- archive (the nginx-<ver>/ wrapper) + meta --------------------------
  suffix=""; [ "$revision" -ge 2 ] && suffix="-r$revision"
  archive="nginx-$version$suffix-${platform//\//-}.tar.gz"
  ( cd "$stage" && COPYFILE_DISABLE=1 tar czf "$outdir/$archive" -- "nginx-$version" )
  arc_sha=$(shasum -a 256 "$outdir/$archive" | awk '{print $1}')
  arc_size=$(wc -c < "$outdir/$archive" | tr -d ' ')

  ARCHIVE="$archive" VERSION="$version" REVISION="$revision" LINE="$line" \
  PLATFORM="$platform" PROVIDER="$provider" EPOCH="$epoch" OS="$os" \
  SOURCE_VERSION="$source_version" ARC_SHA="$arc_sha" ARC_SIZE="$arc_size" \
  SRC_URL="$src_url" SRC_SHA="$src_sha" UPSTREAM_SRC="$upstream_src" UPSTREAM_SRC_SHA="$upstream_src_sha" \
  OPENSSL_VER="$openssl_ver" PCRE2_VER="$pcre2_ver" ZLIB_VER="$zlib_ver" \
  python3 - "$outdir/$archive.meta.json" <<'PY'
import json, os, sys
release_assets = [
    {"name": os.environ["UPSTREAM_SRC"], "sha256": os.environ["UPSTREAM_SRC_SHA"], "object_code": False},
    {"name": os.environ["ARCHIVE"], "sha256": os.environ["ARC_SHA"], "object_code": True},
]
meta = {
    "component": "nginx",
    "version": os.environ["VERSION"],
    "platform": os.environ["PLATFORM"],
    "line": os.environ["LINE"],
    "ordering_kind": "built",
    "provider": os.environ["PROVIDER"],
    "epoch": int(os.environ["EPOCH"]),
    "revision": int(os.environ["REVISION"]),
    "source_version": os.environ["SOURCE_VERSION"],
    "archive": os.environ["ARCHIVE"],
    "sha256": os.environ["ARC_SHA"],
    "size_bytes": int(os.environ["ARC_SIZE"]),
    "release_assets": release_assets,
    "provenance": {
        "recipe": "nginx-unix",
        "os": os.environ["OS"],
        "source_url": os.environ["SRC_URL"],
        "source_sha256": os.environ["SRC_SHA"],
        "static_libs": {
            "openssl": os.environ["OPENSSL_VER"],
            "pcre2": os.environ["PCRE2_VER"],
            "zlib": os.environ["ZLIB_VER"],
        },
        "run_url": (os.environ.get("GITHUB_SERVER_URL", "") + "/" + os.environ.get("GITHUB_REPOSITORY", "") +
                    "/actions/runs/" + os.environ["GITHUB_RUN_ID"]) if os.environ.get("GITHUB_RUN_ID") else "local",
    },
}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    fh.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
PY

  rm -rf "$work" "$stage"
  echo "built $archive (sha256 $arc_sha, $arc_size bytes)"
done

rm -rf "$deproot" "$GNUPGHOME"
echo "$leg: done"
