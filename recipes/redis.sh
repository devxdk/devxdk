#!/usr/bin/env bash
# Redis build recipe — routes the leg to its per-OS implementation.
# windows/amd64 -> the shared MSYS2 build (devxdk-redis-msys2, Phase 1).
# linux/darwin  -> the shared Unix source build (devxdk-redis-unix, Phase 3).
set -euo pipefail

leg="${1:?usage: redis.sh <leg>}"
case "$leg" in
  redis-windows-amd64) exec bash recipes/lib/rediscache-msys2.sh redis "$leg" ;;
  redis-linux-*|redis-darwin-*) exec bash recipes/lib/rediscache-unix.sh redis "$leg" ;;
  *) echo "::error::unexpected redis leg '$leg'" >&2; exit 1 ;;
esac
