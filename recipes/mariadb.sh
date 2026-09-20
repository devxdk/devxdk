#!/usr/bin/env bash
# Portable native macOS server, built from the checksum-verified upstream source.
set -euo pipefail
leg="${1:?leg required}"
case "$leg" in mariadb-darwin-amd64|mariadb-darwin-arm64) ;; *) echo 'unsupported MariaDB build target' >&2; exit 1;; esac
root="$PWD"
out="$root/build/$leg"
mkdir -p "$out"
brew install cmake ninja bison
bison="$(brew --prefix bison)/bin/bison"
count=$(python3 -c 'import json,os; print(len(json.loads(os.environ["LEG_ITEMS"])))')
for ((i=0; i<count; i++)); do
  item() { python3 -c 'import json,os,sys; print(json.loads(os.environ["LEG_ITEMS"])[int(sys.argv[1])][sys.argv[2]])' "$i" "$1"; }
  ver=$(item version); revision=$(item revision); platform=$(item platform)
  [ "$(item mode)" = build ] || { echo 'recovery belongs to run_leg.py' >&2; exit 1; }
  resolved=$(python3 - "$ver" "$(item line)" "$(item source_sha256)" <<'PY'
import sys
sys.path.insert(0, 'scripts')
from devxdk_manifest import fetch, resolvers
source = resolvers.mariadb_source_newest(fetch.Fetcher(), sys.argv[2])
if (source['source_version'], source['source_sha256']) != (sys.argv[1], sys.argv[3]):
    raise SystemExit('MariaDB source differs from the frozen plan; re-plan')
print(source['source_url'], source['source_sha256'])
PY
)
  read -r url sha <<< "$resolved"
  work="$out/work-$ver"
  mkdir -p "$work"
  src="$out/mariadb-$ver-src.tar.gz"
  curl -fsSL --retry 6 --max-time 900 -o "$src" "$url"
  echo "$sha  $src" | shasum -a 256 -c -
  tar xzf "$src" -C "$work"
  prefix="$work/bundle/mariadb-$ver"
  cmake -S "$work/mariadb-$ver" -B "$work/cmake" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_OSX_DEPLOYMENT_TARGET=12.0 \
    -DCMAKE_IGNORE_PREFIX_PATH='/opt/homebrew;/usr/local' \
    -DCMAKE_INSTALL_NAME_DIR=@rpath \
    -DCMAKE_INSTALL_RPATH='@loader_path/../lib;@loader_path/..;@executable_path/../lib' \
    -DBISON_EXECUTABLE="$bison" -DINSTALL_LAYOUT=STANDALONE -DINSTALL_SCRIPTDIR=bin \
    -DWITH_SSL=bundled -DWITH_ZLIB=bundled -DWITH_PCRE=bundled \
    -DWITH_UNIT_TESTS=OFF -DWITH_EMBEDDED_SERVER=OFF -DWITH_WSREP=OFF \
    -DPLUGIN_ROCKSDB=NO -DPLUGIN_MROONGA=NO -DPLUGIN_CONNECT=NO -DPLUGIN_S3=NO \
    -DPLUGIN_COLUMNSTORE=NO -DPLUGIN_LZ4=NO -DPLUGIN_LZO=NO -DPLUGIN_SNAPPY=NO \
    -DPLUGIN_LZMA=NO -DPLUGIN_ZSTD=NO -DPLUGIN_BZIP2=NO
  cmake --build "$work/cmake" --parallel 3
  cmake --install "$work/cmake"
  python3 scripts/ci/verify_macos_bundle.py "$prefix"
  python3 scripts/ci/smoke_mariadb.py "$prefix" "$ver"
  suffix=""; [ "$revision" -lt 2 ] || suffix="-r$revision"
  archive="mariadb-$ver$suffix-${platform//\//-}.tar.gz"
  tar czf "$out/$archive" -C "$work/bundle" "mariadb-$ver"
  INDEX="$i" ARCHIVE="$archive" SOURCE_ARCHIVE="$(basename "$src")" SOURCE_URL="$url" SOURCE_SHA="$sha" \
  python3 - "$out" <<'PY'
import hashlib, json, os, pathlib, sys
root = pathlib.Path(sys.argv[1]); item = json.loads(os.environ['LEG_ITEMS'])[int(os.environ['INDEX'])]
def member(name, object_code):
    path = root / name
    with path.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'name': name, 'sha256': digest, 'size_bytes': path.stat().st_size, 'object_code': object_code}
binary = member(os.environ['ARCHIVE'], True)
source = member(os.environ['SOURCE_ARCHIVE'], False)
meta = {key: item[key] for key in ('component','version','platform','line','ordering_kind','provider','epoch','revision','source_version')}
meta.update(archive=binary['name'], sha256=binary['sha256'], size_bytes=binary['size_bytes'],
            release_assets=[binary, source],
            provenance={'recipe':'mariadb-macos', 'source_url':os.environ['SOURCE_URL'],
                        'source_sha256':os.environ['SOURCE_SHA'], 'tls':'bundled-wolfssl',
                        'macos_minimum':'12.0', 'storage_engines':'upstream core; optional external compression/search/cloud engines excluded'})
(root / (binary['name'] + '.meta.json')).write_text(json.dumps(meta, indent=2) + '\n', encoding='utf-8')
PY
done
