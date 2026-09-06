# Packaging tools

`build-packaging-tools.yml` builds the Linux AppImage packager and the runtime
embedded in its output. It has read-only repository access and no signing keys.

`inputs.json` freezes the Ubuntu image, Ubuntu package snapshot, upstream Git
commits (including submodules and linuxdeploy's exclusion list), and source
archive hashes. `keys/packaging-inputs.sha256` and the versions inventory must
change together when these inputs change. The build uses the Ubuntu 22.04
baseline and records the actual binary and source package versions.

The output contains:

- `linuxdeploy-x86_64.AppImage`, including its appimage plugin and appimagetool;
- `runtime-x86_64`, selected through `LDAI_RUNTIME_FILE` when creating an AppImage;
- `packaging-sources.tar.gz`, containing upstream sources, the applied FUSE and
  exclusion-list changes, distribution source archives and patches, build
  commands, link maps, and copyright files;
- `PROVENANCE.json`, installed/source package lists, `NOTICE.txt`, and checksums.

The bundle contains code under several licenses. linuxdeploy's top-level license
does not describe every bundled component. Keep the source archive and notices
with the binaries when publishing them. Build timestamps mean this workflow does
not claim byte-for-byte reproducibility.

To refresh the application pins:

1. Review the source changes, update the input lock and its checksum, and pass CI.
2. Dispatch `build-packaging-tools.yml`. Require a successful run, including the
   completed bundle's execution and plugin checks.
3. Download that run's artifact by immutable ID, verify its archive digest and
   member manifest, and review the binaries, sources, and package identities.
4. Create a new versioned public tool release. Upload all outputs to the draft
   before publishing it, and record the build commit and run in the release
   notes. Do not replace assets under an existing tool tag.
5. Verify the public downloads and update the app repository's two AppImage
   checksum files and versions inventory. The release pipeline verifies both
   inputs before decoding signing material.

The upstream rolling-asset currency reports remain review notifications; release
jobs consume the versioned DevXDK assets and their committed hashes.
