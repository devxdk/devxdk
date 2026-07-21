#!/usr/bin/env bash
# PHP build recipe — routes the leg to its per-OS implementation.
# windows/amd64 -> php-windows-repack (Phase 1): repackage the OFFICIAL Windows
#   NTS x64 build with the pinned php_redis PECL DLL and the authored php.ini,
#   flat at archive root (php.exe / php-cgi.exe / ext/ / php.ini) — the layout
#   internal/runtimes/php and the php-fpm service def contract on.
# linux/darwin -> php-spc static builds (Phase 3; fails loudly until they land).
#
# A leg covers BOTH tracked lines (8.4 + 8.5) sequentially; each line's item in
# LEG_ITEMS names the exact version the plan resolved from releases.json, and
# the recipe re-verifies it against the same source (single upstream truth).
set -euo pipefail

leg="${1:?usage: php.sh <leg>}"
case "$leg" in
  php-windows-amd64) ;;
  php-linux-*|php-darwin-*)
    echo "::error::recipe devxdk-php-spc for $leg lands with Phase 3" >&2; exit 1 ;;
  *) echo "::error::unexpected php leg '$leg'" >&2; exit 1 ;;
esac

repo_root="$(pwd)"
outdir="$repo_root/build/$leg"
mkdir -p "$outdir"

RELEASES_URL="https://downloads.php.net/~windows/releases"
PECL_URL="https://downloads.php.net/~windows/pecl/releases/redis"

# The 16 baseline extensions + opcache (docs/runtimes-and-services.md); php -m
# must report every one or the bundle does not ship.
BASELINE="mbstring curl fileinfo openssl pdo_mysql mysqli pdo_sqlite sqlite3 redis sodium intl zip exif sockets gd soap"

releases_json="$outdir/.releases.json"
curl -fsSL --retry 6 --retry-max-time 300 --max-time 60 -o "$releases_json" "$RELEASES_URL/releases.json"

pecl_version=$(python3 -c "
import tomllib
cfg = tomllib.load(open('config/tracked-versions.toml','rb'))
print(cfg['pins']['php_redis']['version'])")

items_json="${LEG_ITEMS:?LEG_ITEMS must carry the per-line plan items}"
count=$(python3 -c "import json,sys;print(len(json.loads(sys.argv[1])))" "$items_json")

for i in $(seq 0 $((count - 1))); do
  item() { python3 -c "import json,sys;print(json.loads(sys.argv[1])[$i].get(sys.argv[2],''))" "$items_json" "$1"; }
  mode=$(item mode); version=$(item version); revision=$(item revision)
  line=$(item line); platform=$(item platform); provider=$(item provider)
  epoch=$(item epoch); source_version=$(item source_version)

  [ "$mode" = "build" ] || { echo "::error::$leg item $version has mode '$mode' — only build is implemented in the recipe" >&2; exit 1; }
  [ "$platform" = "windows/amd64" ] || { echo "::error::php.sh windows path got platform $platform" >&2; exit 1; }

  # --- resolve the official zip from releases.json (READ the vs field) ----
  read -r zip_path zip_sha variant <<<"$(python3 - "$releases_json" "$line" "$source_version" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
branch, want = sys.argv[2], sys.argv[3]
e = d.get(branch)
if not e:
    sys.exit(f"branch {branch} not in releases.json")
if e.get("version") != want:
    sys.exit(f"releases.json {branch} is {e.get('version')}, plan wants {want} (older versions live in archives/; re-plan)")
variants = [k for k in e if k.startswith("nts-") and k.endswith("-x64")]
if len(variants) != 1:
    sys.exit(f"expected exactly one nts x64 variant for {branch}, found {variants}")
z = e[variants[0]]["zip"]
print(z["path"], z["sha256"].lower(), variants[0])
EOF
)"

  # --- pinned php_redis DLL for this line, ABI-tag-matched to the variant --
  read -r dll_file dll_sha <<<"$(python3 - "$line" <<'EOF'
import sys, tomllib
cfg = tomllib.load(open("config/tracked-versions.toml", "rb"))
d = cfg["pins"]["php_redis"]["dll"][sys.argv[1]]
print(d["file"], d["sha256"].lower())
EOF
)"
  case "$dll_file" in
    *"-${variant}.zip") ;;
    *) echo "::error::pinned DLL $dll_file does not match PHP variant $variant (compiler/ABI drift — re-pin)" >&2; exit 1 ;;
  esac

  work="$outdir/work-$version"
  stage="$outdir/stage-$version"
  rm -rf "$work" "$stage" && mkdir -p "$work" "$stage"

  curl -fsSL --retry 6 --retry-max-time 300 --max-time 300 -o "$work/php.zip" "$RELEASES_URL/$zip_path"
  echo "$zip_sha  $work/php.zip" | sha256sum -c -
  python3 -c "import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$work/php.zip" "$stage"

  curl -fsSL --retry 6 --retry-max-time 300 --max-time 120 -o "$work/redis-dll.zip" "$PECL_URL/$pecl_version/$dll_file"
  echo "$dll_sha  $work/redis-dll.zip" | sha256sum -c -
  python3 -c "import sys,zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$work/redis-dll.zip" "$work/redis-dll"
  find "$work/redis-dll" -name 'php_redis.dll' -exec cp {} "$stage/ext/" \;
  [ -f "$stage/ext/php_redis.dll" ] || { echo "::error::php_redis.dll not found in the PECL zip" >&2; exit 1; }

  cp templates/php.ini.windows "$stage/php.ini"
  # PHP 8.5 compiles opcache IN (no php_opcache.dll ships) — the template's
  # zend_extension line would then warn on EVERY invocation, so strip it when
  # the DLL is absent; the smoke still requires "Zend OPcache" in -m either way.
  if [ ! -f "$stage/ext/php_opcache.dll" ]; then
    sed -i '/^zend_extension=opcache/d' "$stage/php.ini"
  fi

  # --- layout check --------------------------------------------------------
  for f in php.exe php-cgi.exe php.ini ext/php_redis.dll; do
    [ -e "$stage/$f" ] || { echo "::error::layout: $f missing" >&2; exit 1; }
  done
  if find "$stage" -name '.devxdk-complete' -o -name '.devxdk-initialized' | grep -q .; then
    echo "::error::layout: bundle must not contain DevXDK marker files" >&2; exit 1
  fi

  # --- smoke (native Windows build; php.ini found beside php.exe) ---------
  ver_out=$("$stage/php.exe" -v 2>&1)
  [[ "$ver_out" == *"PHP $source_version"* ]] \
    || { echo "::error::smoke: php -v does not report $source_version" >&2; exit 1; }
  # Warning-clean: a failed extension load warns on EVERY invocation — a
  # bundle that warns is a bundle that does not ship.
  [[ "$ver_out" != *Warning:* ]] \
    || { echo "::error::smoke: php -v emits warnings:"; printf '%s\n' "$ver_out" >&2; exit 1; }
  mods=$("$stage/php.exe" -m 2>/dev/null)
  for ext in $BASELINE; do
    echo "$mods" | grep -qix "$ext" || { echo "::error::smoke: extension '$ext' missing from php -m" >&2; exit 1; }
  done
  echo "$mods" | grep -q "Zend OPcache" || { echo "::error::smoke: Zend OPcache missing" >&2; exit 1; }
  # Pure bash, no grep: git-bash's grep -iF ABORTS on backslash-heavy Windows
  # path patterns, and pipefail + -q would SIGPIPE-fail matching pipelines.
  ini_out=$("$stage/php.exe" --ini)
  loaded=$(printf '%s\n' "$ini_out" | sed -n 's/^Loaded Configuration File:[[:space:]]*//p')
  loaded="${loaded%\"}"; loaded="${loaded#\"}"   # PHP 8.5 quotes the path
  win_stage=$(cygpath -w "$stage")
  if [[ "${loaded,,}" != "${win_stage,,}"\\php.ini ]]; then
    echo "::error::smoke: php --ini loaded '$loaded', want '$win_stage\\php.ini'" >&2; exit 1
  fi
  "$stage/php-cgi.exe" -v | head -1 | grep -qF "PHP $source_version" \
    || { echo "::error::smoke: php-cgi -v failed" >&2; exit 1; }
  echo "smoke: php $source_version -v/-m(baseline $(echo $BASELINE | wc -w)+opcache)/--ini/php-cgi OK"

  # --- archive + meta ------------------------------------------------------
  suffix=""; [ "$revision" -ge 2 ] && suffix="-r$revision"
  archive="php-$version$suffix-windows-amd64.zip"
  python3 scripts/zip_dir.py "$stage" "$outdir/$archive"
  out_sha=$(sha256sum "$outdir/$archive" | awk '{print $1}')
  out_size=$(stat -c %s "$outdir/$archive")

  ARCHIVE="$archive" COMPONENT="php" VERSION="$version" REVISION="$revision" \
  LINE="$line" PLATFORM="$platform" PROVIDER="$provider" EPOCH="$epoch" \
  SOURCE_VERSION="$source_version" ZIP_SHA="$out_sha" ZIP_SIZE="$out_size" \
  SRC_URL="$RELEASES_URL/$zip_path" SRC_SHA="$zip_sha" \
  DLL_URL="$PECL_URL/$pecl_version/$dll_file" DLL_SHA="$dll_sha" \
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
        "recipe": "php-windows-repack",
        "official_zip_url": os.environ["SRC_URL"],
        "official_zip_sha256": os.environ["SRC_SHA"],
        "php_redis_url": os.environ["DLL_URL"],
        "php_redis_sha256": os.environ["DLL_SHA"],
        "run_url": (os.environ.get("GITHUB_SERVER_URL", "") + "/" + os.environ.get("GITHUB_REPOSITORY", "") +
                    "/actions/runs/" + os.environ["GITHUB_RUN_ID"]) if os.environ.get("GITHUB_RUN_ID") else "local",
    },
}
with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as fh:
    fh.write(json.dumps(meta, indent=2, sort_keys=True) + "\n")
EOF

  rm -rf "$work" "$stage"
  echo "built $archive (sha256 $out_sha, $out_size bytes)"
done

rm -f "$releases_json"
echo "$leg: done"
