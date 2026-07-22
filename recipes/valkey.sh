#!/usr/bin/env bash
# Valkey build recipe — routes the leg to its per-OS implementation.
# windows/amd64 -> the shared MSYS2 build (devxdk-valkey-msys2, Phase 1 mirror).
# linux/darwin  -> valkey-unix (Phase 3; also the ONLY Linux Valkey source —
#                  valkey-hashes covers source tarballs only, so official
#                  binary tarballs have no authenticated verification chain).
set -euo pipefail

leg="${1:?usage: valkey.sh <leg>}"
case "$leg" in
  valkey-windows-amd64) exec bash recipes/lib/rediscache-msys2.sh valkey "$leg" ;;
  valkey-linux-*|valkey-darwin-*) exec bash recipes/lib/rediscache-unix.sh valkey "$leg" ;;
  *) echo "::error::unexpected valkey leg '$leg'" >&2; exit 1 ;;
esac
