#!/usr/bin/env bash
# Unix static PHP build via static-php-cli (devxdk-php-spc, Phase 3). Linux+macOS.
#
# Each PHP minor is its own line (8.4, 8.5); a leg builds every line for its
# platform sequentially. Per line: verify the pinned spc builder binary
# (config [pins.static_php_cli]); GPG-verify the php source tarball against the
# pinned php.net release-manager keys (keys/php/*.key + [pins.php_keys]) AND its
# sha256 from php.net's releases JSON; feed exactly those verified bytes to spc
# over a loopback URL (-U php-src:...) so spc compiles the audited source; build
# the baseline extension set STATICALLY (`spc build ... --build-cli --build-fpm`);
# assemble the flat bundle the app contract expects (ArchiveStrip=0 -> archive
# root == version dir): bin/php + sbin/php-fpm + php.ini (templates/php.ini.unix)
# + licenses/ (spc dump-license). Smoke: php -v/-m(baseline+opcache)/--ini +
# php-fpm -v/-t/loaded-config. tar.gz + .meta.json (php source as a first-class
# release asset). static-php-cli has no CGI SAPI, so there is no php-cgi here —
# the app's php-fpm service def owns the Unix FastCGI path.
set -euo pipefail

leg="${1:?usage: php-spc.sh <leg>}"
case "$leg" in
  php-linux-*|php-darwin-*) ;;
  *) echo "::error::php-spc.sh builds only the unix php legs, not '$leg'" >&2; exit 1 ;;
esac

os="$(uname -s)"   # Linux | Darwin
arch="$(uname -m)" # x86_64 | arm64
repo_root="$(pwd)"
outdir="$repo_root/build/$leg"
mkdir -p "$outdir"
keydir="scripts/devxdk_manifest/keys/php"

# The baseline 16 extensions (docs/runtimes-and-services.md) + opcache (built in)
# + the plan's Unix extras; every name is validated against spc's ext.json.
EXTS="mbstring,curl,fileinfo,openssl,pdo_mysql,mysqli,pdo_sqlite,sqlite3,redis,sodium,intl,zip,exif,sockets,gd,soap,opcache,phar,iconv,ctype,session,tokenizer,xml,dom,simplexml,xmlreader,xmlwriter,filter,posix,pcntl"
# The baseline `php -m` must report every one of these (opcache reports as
# "Zend OPcache"); the rest are extras that need not be individually asserted.
BASELINE="mbstring curl fileinfo openssl pdo_mysql mysqli pdo_sqlite sqlite3 redis sodium intl zip exif sockets gd soap"

# --- pins: spc version + per-runner sha, and the php key fingerprints -------
eval "$(python3 - "$os" "$arch" <<'PY'
import sys, tomllib
os_, arch = sys.argv[1], sys.argv[2]
p = tomllib.load(open("config/tracked-versions.toml", "rb"))["pins"]
spc = p["static_php_cli"]
if os_ == "Linux" and arch == "x86_64":
    asset, sha = "spc-linux-x86_64.tar.gz", spc["sha256_linux_x86_64"]
elif os_ == "Darwin" and arch == "arm64":
    asset, sha = "spc-macos-aarch64.tar.gz", spc["sha256_macos_aarch64"]
elif os_ == "Darwin" and arch == "x86_64":
    asset, sha = "spc-macos-x86_64.tar.gz", spc["sha256_macos_x86_64"]
else:
    sys.stderr.write(f"no static-php-cli pin for {os_}/{arch}\n"); sys.exit(1)
print(f'spc_ver={spc["version"]}; spc_asset={asset}; spc_sha={sha}')
print('php_fprs="' + " ".join(p["php_keys"]["fingerprints"]) + '"')
PY
)"

# --- fetch + verify the pinned spc builder binary --------------------------
spc_tgz="$outdir/$spc_asset"
curl -fsSL --retry 6 --retry-max-time 300 --max-time 300 -o "$spc_tgz" \
  "https://github.com/crazywhalecc/static-php-cli/releases/download/$spc_ver/$spc_asset"
echo "$spc_sha  $spc_tgz" | shasum -a 256 -c - >/dev/null || { echo "::error::static-php-cli $spc_ver sha256 mismatch" >&2; exit 1; }
tar xzf "$spc_tgz" -C "$outdir"
SPC="$outdir/spc"
[ -x "$SPC" ] || { chmod +x "$SPC" 2>/dev/null || true; }
[ -x "$SPC" ] || { echo "::error::spc binary not executable after extract" >&2; exit 1; }
"$SPC" --version >/dev/null || { echo "::error::spc binary does not run" >&2; exit 1; }

# --- isolated GPG keyring: committed php keys, assert the pinned fprs -------
export GNUPGHOME="$outdir/gnupg"; rm -rf "$GNUPGHOME"; mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"
for kf in "$keydir"/*.key; do
  gpg --batch --quiet --import "$kf" 2>/dev/null || { echo "::error::failed to import $kf" >&2; exit 1; }
done
present=$(gpg --batch --with-colons --list-keys 2>/dev/null | awk -F: '/^fpr:/{print $10}' | sort -u)
for fpr in $php_fprs; do
  printf '%s\n' "$present" | grep -qx "$fpr" \
    || { echo "::error::pinned php key $fpr not present in the keyring after import" >&2; exit 1; }
done
echo "php keyring: pinned release-manager fingerprints present"

# --- ensure the toolchain spc needs (idempotent on the CI images) ----------
# Pre-install the build tools with a FRESH apt index rather than leaning on spc
# doctor's --auto-fix, whose bare `apt-get install` hits 503s on a stale index
# (re2c/autopoint are the two the ubuntu image lacks). apt-get update is retried
# for a transient mirror blip; then doctor --auto-fix finds everything present.
if [ "$os" = Linux ]; then
  for _ in 1 2 3; do sudo apt-get update -y >>"$outdir/apt.log" 2>&1 && break; sleep 5; done
  sudo apt-get install -y --no-install-recommends \
    re2c gettext autoconf automake libtool pkg-config bison flex build-essential \
    >>"$outdir/apt.log" 2>&1 || { echo "::error::apt-get install of the spc toolchain failed"; tail -30 "$outdir/apt.log" >&2; exit 1; }
fi
"$SPC" doctor --auto-fix >"$outdir/doctor.log" 2>&1 || { echo "::error::spc doctor failed"; tail -40 "$outdir/doctor.log" >&2; exit 1; }

# --- loopback source server: spc compiles ONLY our GPG-verified bytes -------
srcserve="$outdir/srcserve"; rm -rf "$srcserve"; mkdir -p "$srcserve"
port=8471
python3 -m http.server "$port" --bind 127.0.0.1 --directory "$srcserve" >"$outdir/httpd.log" 2>&1 &
httpd_pid=$!
trap 'kill "$httpd_pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do
  curl -fsS "http://127.0.0.1:$port/" >/dev/null 2>&1 && break
  kill -0 "$httpd_pid" 2>/dev/null || { echo "::error::source server died"; cat "$outdir/httpd.log" >&2; exit 1; }
  sleep 0.5
done

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
    *) echo "::error::$leg is the php-spc recipe; platform $platform is not its target" >&2; exit 1 ;;
  esac
  minor="$line"   # config line id IS the major.minor (8.4 / 8.5)

  # --- fetch php source, GPG-verify + sha256 from php.net's releases JSON ---
  src_name="php-$source_version.tar.gz"
  src_url="https://www.php.net/distributions/$src_name"
  want_sha=$(python3 - "$minor" "$source_version" <<'PY'
import json, sys, urllib.request
minor, ver = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"https://www.php.net/releases/?json&version={minor}&max=1", timeout=30) as r:
    d = json.load(r)
newest = next(iter(d))
if newest != ver:
    sys.stderr.write(f"php.net {minor} newest is {newest}, plan wants {ver} (re-plan)\n"); sys.exit(2)
src = next(s for s in d[ver]["source"] if s["filename"] == f"php-{ver}.tar.gz")
print(src["sha256"])
PY
) || { echo "::error::$leg: php.net releases JSON did not confirm $source_version" >&2; exit 1; }

  curl -fsSL --retry 6 --retry-max-time 300 --max-time 300 -o "$srcserve/$src_name" "$src_url"
  curl -fsSL --retry 6 --retry-max-time 300 --max-time 60 -o "$srcserve/$src_name.asc" "$src_url.asc"
  echo "$want_sha  $srcserve/$src_name" | shasum -a 256 -c - >/dev/null \
    || { echo "::error::php $source_version sha256 != php.net releases JSON" >&2; exit 1; }
  gpg --batch --verify "$srcserve/$src_name.asc" "$srcserve/$src_name" 2>"$outdir/gpg-$version.out" \
    || { echo "::error::GPG verification of $src_name failed:" >&2; cat "$outdir/gpg-$version.out" >&2; exit 1; }
  grep -q "Good signature" "$outdir/gpg-$version.out" || { echo "::error::no 'Good signature' for $src_name" >&2; cat "$outdir/gpg-$version.out" >&2; exit 1; }
  echo "php $source_version: sha256 (releases JSON) + GPG (pinned RM key) verified"

  # --- spc download (php-src from our loopback) + static build -------------
  wd="$outdir/wd-$version"; rm -rf "$wd"; mkdir -p "$wd"
  ( cd "$wd" && "$SPC" download --for-extensions="$EXTS" --with-php="$minor" \
      -U "php-src:http://127.0.0.1:$port/$src_name" \
      >"$outdir/spc-download-$version.log" 2>&1 ) \
    || { echo "::error::spc download failed"; tail -40 "$outdir/spc-download-$version.log" >&2; exit 1; }
  ( cd "$wd" && "$SPC" build "$EXTS" --build-cli --build-fpm \
      >"$outdir/spc-build-$version.log" 2>&1 ) \
    || { echo "::error::spc build failed"; tail -60 "$outdir/spc-build-$version.log" >&2; exit 1; }

  php_bin="$wd/buildroot/bin/php"
  fpm_bin="$wd/buildroot/bin/php-fpm"
  [ -x "$php_bin" ] || { echo "::error::spc produced no buildroot/bin/php" >&2; exit 1; }
  [ -x "$fpm_bin" ] || { echo "::error::spc produced no buildroot/bin/php-fpm" >&2; exit 1; }

  # --- assemble the flat bundle: bin/php + sbin/php-fpm + php.ini + licenses -
  stage="$outdir/stage-$version"; rm -rf "$stage"; mkdir -p "$stage/bin" "$stage/sbin" "$stage/licenses"
  cp "$php_bin" "$stage/bin/php"
  cp "$fpm_bin" "$stage/sbin/php-fpm"
  chmod 0755 "$stage/bin/php" "$stage/sbin/php-fpm"
  cp templates/php.ini.unix "$stage/php.ini"
  # Complete license notices for php + every statically-linked library.
  ( cd "$wd" && "$SPC" dump-license --for-extensions="$EXTS" --dump-dir="$stage/licenses" \
      >"$outdir/spc-license-$version.log" 2>&1 ) \
    || { echo "::error::spc dump-license failed"; tail -20 "$outdir/spc-license-$version.log" >&2; exit 1; }
  [ -n "$(ls -A "$stage/licenses")" ] || { echo "::error::dump-license produced no notices" >&2; exit 1; }

  # --- layout check --------------------------------------------------------
  for f in bin/php sbin/php-fpm php.ini; do
    [ -e "$stage/$f" ] || { echo "::error::layout: $f missing under archive root" >&2; exit 1; }
  done
  if find "$stage" -name '.devxdk-complete' -o -name '.devxdk-initialized' | grep -q .; then
    echo "::error::layout: bundle must not contain DevXDK marker files" >&2; exit 1
  fi

  # --- smoke: php -v/-m/--ini + php-fpm -v/-t/loaded-config ----------------
  ver_out=$("$stage/bin/php" -v 2>&1)
  printf '%s\n' "$ver_out" | grep -q "PHP $source_version" \
    || { echo "::error::smoke: php -v does not report $source_version" >&2; printf '%s\n' "$ver_out" >&2; exit 1; }
  printf '%s\n' "$ver_out" | grep -qi "warning" \
    && { echo "::error::smoke: php -v emits warnings:"; printf '%s\n' "$ver_out" >&2; exit 1; } || true
  mods=$("$stage/bin/php" -c "$stage/php.ini" -m 2>/dev/null)
  for ext in $BASELINE; do
    printf '%s\n' "$mods" | grep -qix "$ext" || { echo "::error::smoke: extension '$ext' missing from php -m" >&2; exit 1; }
  done
  printf '%s\n' "$mods" | grep -q "Zend OPcache" || { echo "::error::smoke: Zend OPcache missing from php -m" >&2; exit 1; }
  ini_loaded=$("$stage/bin/php" -c "$stage/php.ini" --ini 2>/dev/null | sed -n 's/^Loaded Configuration File:[[:space:]]*//p')
  [ "$ini_loaded" = "$stage/php.ini" ] || { echo "::error::smoke: php --ini loaded '$ini_loaded', want '$stage/php.ini'" >&2; exit 1; }
  "$stage/sbin/php-fpm" -v 2>&1 | grep -q "PHP $source_version" \
    || { echo "::error::smoke: php-fpm -v does not report $source_version" >&2; exit 1; }
  # A minimal FPM config proves the FPM binary parses a real pool AND loads our
  # php.ini (via -c); -t reports success, -i reports the loaded php.ini path.
  fpmconf="$outdir/fpm-$version.conf"
  cat > "$fpmconf" <<CONF
[global]
error_log = /dev/stderr
daemonize = no
[www]
listen = 127.0.0.1:9909
pm = static
pm.max_children = 1
CONF
  "$stage/sbin/php-fpm" -c "$stage/php.ini" -y "$fpmconf" -t >"$outdir/fpm-t-$version.log" 2>&1 \
    || { echo "::error::smoke: php-fpm -t failed"; cat "$outdir/fpm-t-$version.log" >&2; exit 1; }
  fpm_ini=$("$stage/sbin/php-fpm" -c "$stage/php.ini" -y "$fpmconf" -i 2>/dev/null | sed -n 's/^Loaded Configuration File => //p' | head -1)
  [ "$fpm_ini" = "$stage/php.ini" ] || { echo "::error::smoke: php-fpm loaded ini '$fpm_ini', want '$stage/php.ini'" >&2; exit 1; }
  echo "smoke: php $source_version -v/-m(baseline $(echo $BASELINE | wc -w)+opcache)/--ini + php-fpm -v/-t/loaded-config OK"

  # --- corresponding source (provenance; PHP License is permissive) --------
  upstream_src="php-$source_version-src.tar.gz"
  cp "$srcserve/$src_name" "$outdir/$upstream_src"
  upstream_src_sha=$(shasum -a 256 "$outdir/$upstream_src" | awk '{print $1}')

  # --- archive (flat) + meta ----------------------------------------------
  suffix=""; [ "$revision" -ge 2 ] && suffix="-r$revision"
  archive="php-$version$suffix-${platform//\//-}.tar.gz"
  ( cd "$stage" && COPYFILE_DISABLE=1 tar czf "$outdir/$archive" -- * )
  arc_sha=$(shasum -a 256 "$outdir/$archive" | awk '{print $1}')
  arc_size=$(wc -c < "$outdir/$archive" | tr -d ' ')

  ARCHIVE="$archive" VERSION="$version" REVISION="$revision" LINE="$line" \
  PLATFORM="$platform" PROVIDER="$provider" EPOCH="$epoch" OS="$os" \
  SOURCE_VERSION="$source_version" ARC_SHA="$arc_sha" ARC_SIZE="$arc_size" \
  SRC_URL="$src_url" SRC_SHA="$want_sha" SPC_VER="$spc_ver" \
  UPSTREAM_SRC="$upstream_src" UPSTREAM_SRC_SHA="$upstream_src_sha" EXTS="$EXTS" \
  python3 - "$outdir/$archive.meta.json" <<'PY'
import json, os, sys
release_assets = [
    {"name": os.environ["UPSTREAM_SRC"], "sha256": os.environ["UPSTREAM_SRC_SHA"], "object_code": False},
    {"name": os.environ["ARCHIVE"], "sha256": os.environ["ARC_SHA"], "object_code": True},
]
meta = {
    "component": "php",
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
        "recipe": "php-spc",
        "os": os.environ["OS"],
        "static_php_cli": os.environ["SPC_VER"],
        "extensions": os.environ["EXTS"],
        "source_url": os.environ["SRC_URL"],
        "source_sha256": os.environ["SRC_SHA"],
        "run_url": (os.environ.get("GITHUB_SERVER_URL", "") + "/" + os.environ.get("GITHUB_REPOSITORY", "") +
                    "/actions/runs/" + os.environ["GITHUB_RUN_ID"]) if os.environ.get("GITHUB_RUN_ID") else "local",
    },
}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    fh.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
PY

  rm -rf "$wd" "$stage"
  rm -f "$srcserve/$src_name" "$srcserve/$src_name.asc"
  echo "built $archive (sha256 $arc_sha, $arc_size bytes)"
done

kill "$httpd_pid" 2>/dev/null || true
rm -rf "$srcserve" "$GNUPGHOME" "$outdir/spc" "$spc_tgz"
echo "$leg: done"
