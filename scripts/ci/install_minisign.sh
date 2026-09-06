#!/usr/bin/env bash
# Secretless verifier bootstrap, using the existing reviewed archive pin.
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
pin="$repo_root/keys/minisign.sha256"
url=$(sed -n 's/^# url:[[:space:]]*//p' "$pin")
inner=$(sed -n 's/^# inner:[[:space:]]*//p' "$pin")
directory="${RUNNER_TEMP:?}/reference-minisign"
mkdir -p "$directory"
cd "$directory"
file=$(awk '!/^#/ && NF {print $2}' "$pin")
curl -fsSL --retry 3 --max-time 120 -o "$file" "$url"
grep -v '^#' "$pin" | sha256sum -c -
tar xzf "$file"
printf 'MINISIGN=%s/%s\n' "$directory" "$inner" >> "${GITHUB_ENV:?}"
