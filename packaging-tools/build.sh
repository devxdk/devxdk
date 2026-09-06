#!/usr/bin/env bash
# Build a source-complete tool bundle without importing signing credentials.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
mkdir -p /sources /work /out
cp /build/inputs.json /sources/inputs.json
cp /build/build.sh /sources/build.sh
cp /build/Dockerfile /sources/Dockerfile
dpkg-query -W -f='${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' > /sources/installed-packages.tsv
python3 - <<'PY'
import hashlib,json,pathlib,subprocess,urllib.request
lock=json.load(open('/build/inputs.json'))
for name,(url,commit) in lock['git'].items():
    dest='/sources/'+name
    subprocess.run(['git','init',dest],check=True)
    subprocess.run(['git','-C',dest,'remote','add','origin',url],check=True)
    subprocess.run(['git','-C',dest,'fetch','--depth=1','origin',commit],check=True)
    subprocess.run(['git','-C',dest,'checkout','--detach','FETCH_HEAD'],check=True)
    subprocess.run(['git','-C',dest,'submodule','update','--init','--recursive','--depth=1'],check=True)
for name,(url,digest) in lock['archives'].items():
    archive=pathlib.Path('/sources/'+name+'.archive')
    subprocess.run(['curl','-fsSL','--retry','3','--max-time','300','-o',str(archive),url],check=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=digest: raise SystemExit('source hash mismatch: '+name)
    dest='/sources/'+name
    pathlib.Path(dest).mkdir()
    subprocess.run(['tar','xf',str(archive),'-C',dest,'--strip-components=1'],check=True)
PY

# Keep modifications and generated source beside the original inputs.
cd /sources/patchelf
./bootstrap.sh
./configure --prefix=/usr/local LDFLAGS='-static -static-libgcc -static-libstdc++ -Wl,-Map,/work/patchelf.map'
make -j"$(nproc)" install

mkdir /work/binutils
cd /work/binutils
/sources/binutils/configure --prefix=/opt/binutils --disable-nls --enable-static-link \
  --disable-shared-plugins --disable-dynamicplugin --disable-tls --disable-pie
make -j"$(nproc)"
make clean
make -j"$(nproc)" LDFLAGS='-all-static -Wl,-Map,/work/binutils.map'
make install

# Build the exact patched FUSE and squashfuse sources used by the runtime.
cd /sources/libfuse
patch -p1 < /sources/runtime/patches/libfuse/mount.c.diff
meson setup build --prefix=/usr --default-library=static -Dexamples=false -Dtests=false
ninja -C build install
cd /sources/squashfuse
./autogen.sh
./configure --prefix=/usr/local LDFLAGS=-static
make -j"$(nproc)" install
mkdir -p /usr/local/include/squashfuse
install -m644 ./*.h -t /usr/local/include/squashfuse/

# Ubuntu does not promise a static mimalloc archive; build its exact source.
mkdir -p /sources/distro
cd /sources/distro
mimalloc_version=$(dpkg-query -W -f='${source:Version}' libmimalloc-dev)
apt-get source "mimalloc=$mimalloc_version"
mimalloc_source=$(find /sources/distro -maxdepth 1 -type d -name 'mimalloc-*' | head -1)
cmake -S "$mimalloc_source" -B /work/mimalloc -DMI_BUILD_SHARED=OFF -DMI_BUILD_TESTS=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON
cmake --build /work/mimalloc -j"$(nproc)"
cmake --install /work/mimalloc --prefix /usr/local

cd /sources/runtime/src/runtime
git -C /sources/runtime rev-parse --short HEAD > version
make -j"$(nproc)" runtime CC='clang -L/usr/local/lib/mimalloc-2.0 -Wl,-Map,/work/runtime.map'
cp runtime /out/runtime-x86_64
/opt/binutils/bin/strip --strip-debug --strip-unneeded /out/runtime-x86_64
printf 'AI\002' | dd of=/out/runtime-x86_64 bs=1 count=3 seek=8 conv=notrunc

cmake -S /sources/linuxdeploy -B /work/linuxdeploy -G Ninja -DSTATIC_BUILD=ON \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_EXE_LINKER_FLAGS=-Wl,-Map,/work/linuxdeploy.map
cmake --build /work/linuxdeploy --target linuxdeploy -j"$(nproc)"
cmake -S /sources/plugin -B /work/plugin -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr
cmake --build /work/plugin -j"$(nproc)"
cmake -S /sources/appimagetool -B /work/appimagetool -DBUILD_STATIC=OFF -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr
cmake --build /work/appimagetool -j"$(nproc)"

appdir=/work/AppDir
prefix="$appdir/plugins/linuxdeploy-plugin-appimage/appimagetool-prefix"
mkdir -p "$appdir/usr/bin" "$prefix"
DESTDIR="$appdir/plugins/linuxdeploy-plugin-appimage" cmake --install /work/plugin
DESTDIR="$prefix" cmake --install /work/appimagetool
cp /sources/appimagetool/resources/AppRun.sh "$prefix/AppRun"
chmod +x "$prefix/AppRun"
for binary in desktop-file-validate mksquashfs zsyncmake; do cp "$(command -v "$binary")" "$prefix/usr/bin/"; done
cat > "$appdir/plugins/linuxdeploy-plugin-appimage/usr/bin/appimagetool" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
this_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$this_dir/../../appimagetool-prefix/AppRun" "$@"
SH
chmod +x "$appdir/plugins/linuxdeploy-plugin-appimage/usr/bin/appimagetool"
ln -s ../../plugins/linuxdeploy-plugin-appimage/usr/bin/linuxdeploy-plugin-appimage "$appdir/usr/bin/linuxdeploy-plugin-appimage"
export LDAI_RUNTIME_FILE=/out/runtime-x86_64
export OUTPUT=/out/linuxdeploy-x86_64.AppImage
/work/linuxdeploy/bin/linuxdeploy --appdir "$appdir" \
  -e /work/linuxdeploy/bin/linuxdeploy -e /usr/local/bin/patchelf -e /opt/binutils/bin/strip \
  -e "$prefix/usr/bin/appimagetool" -e "$prefix/usr/bin/desktop-file-validate" \
  -e "$prefix/usr/bin/mksquashfs" -e "$prefix/usr/bin/zsyncmake" \
  -i /sources/linuxdeploy/resources/linuxdeploy.png -d /sources/linuxdeploy/resources/linuxdeploy.desktop \
  --output appimage

# Exercise the completed bundle and its embedded runtime without requiring FUSE.
APPIMAGE_EXTRACT_AND_RUN=1 /out/linuxdeploy-x86_64.AppImage --version
APPIMAGE_EXTRACT_AND_RUN=1 /out/linuxdeploy-x86_64.AppImage --list-plugins

# Resolve binary/source package identities, then retain exact distro sources.
# Include development packages as well: their static archives/header code may
# be incorporated even when ldd has no corresponding dynamic dependency.
python3 - <<'PY'
import pathlib,subprocess
packages={}
for line in pathlib.Path('/sources/installed-packages.tsv').read_text().splitlines():
    binary,version,source,source_version=line.split('\t')
    if '-dev' in binary or binary.split(':')[0] in {'libc6','libgcc-s1','libstdc++6','desktop-file-utils','squashfs-tools','zsync'}:
        packages[source]=source_version
for path in pathlib.Path('/work/AppDir').rglob('*'):
    if path.is_file() and '.so' in path.name:
        result=subprocess.run(['dpkg','-S','*/'+path.name],capture_output=True,text=True)
        for line in result.stdout.splitlines():
            package=line.split(': /',1)[0]
            result=subprocess.run(['dpkg-query','-W','-f=${source:Package}\t${source:Version}',package],capture_output=True,text=True)
            if result.returncode==0 and '\t' in result.stdout:
                source,version=result.stdout.split('\t');packages[source]=version
pathlib.Path('/sources/distro').mkdir(exist_ok=True)
for name,version in sorted(packages.items()):
    subprocess.run(['apt-get','source','--download-only',name+'='+version],cwd='/sources/distro',check=True)
pathlib.Path('/sources/source-packages.tsv').write_text(''.join(f'{k}\t{v}\n' for k,v in sorted(packages.items())))
PY
mkdir -p /sources/copyright
find /usr/share/doc -name copyright -type f -exec cp --parents '{}' /sources/copyright/ \;
cp /work/*.map /sources/ 2>/dev/null || true
cp /sources/installed-packages.tsv /out/
cp /sources/source-packages.tsv /out/
cp /sources/inputs.json /out/PROVENANCE.json
cat > /out/NOTICE.txt <<'TXT'
DevXDK packaging tools: rebuilt from the reviewed sources in PROVENANCE.json.
The bundle is an aggregate; linuxdeploy's MIT license does not cover all members.
packaging-sources.tar.gz contains upstream source trees, authored build commands,
distribution source archives/patches, installed package identities, and copyrights.
The FUSE mount patch and generated runtime version are retained with the sources.
TXT
tar czf /out/packaging-sources.tar.gz -C /sources .
cd /out
sha256sum linuxdeploy-x86_64.AppImage runtime-x86_64 packaging-sources.tar.gz > SHA256SUMS
