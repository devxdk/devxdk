#!/usr/bin/env bash
# Dispatch a build leg to its per-component recipe.
#
# The recipes themselves land later: Phase 1 (php-windows-repack, redis-msys2)
# and Phase 3 (php-spc, redis/valkey-unix, valkey-msys2, nginx-unix). Until a
# recipe exists, a leg run fails loudly rather than silently publishing nothing —
# build-runtimes treats a present-in-plan leg failure as a real failure.
set -euo pipefail

# A native Windows parent can pass PWD as D:/..., which GNU tar interprets
# as a remote host. Normalize once before any recipe derives archive paths.
if command -v cygpath >/dev/null 2>&1; then
  cd -P -- "$(cygpath -u "$PWD")"
fi

leg="${1:?usage: leg.sh <component>-<goos>-<goarch>}"
component="${leg%%-*}"
recipe="recipes/${component}.sh"

if [ -f "$recipe" ]; then
  exec bash "$recipe" "$leg"
fi

echo "recipe for '${component}' (leg ${leg}) is not implemented yet (Phase 1/3)" >&2
exit 1
