#!/usr/bin/env bash
# The full scrape-and-sign transaction, extracted from the workflow so it is
# reviewable and reusable. Resets to the live main tip and replays the entire
# sequence on a push race, so stale output can never overwrite a concurrently
# committed pending record, revocation, or provider-epoch bump.
#
# Env (from the manifest-release environment): MINISIGN_SECRET_KEY,
# MANIFEST_PUSH_TOKEN, MANSIGN (path to devxdk-mansign), ALLOWLIST_GO (path to
# the pinned app-src allowlist.go), FORCE_RESIGN, optional DEVXDK_ROTATION_WINDOW.
set -euo pipefail

keyfile="$(mktemp)"
trap 'rm -f "$keyfile"' EXIT
printf '%s' "$MINISIGN_SECRET_KEY" > "$keyfile"

# The active public key derived from the signing secret, and the committed one.
derived="$("$MANSIGN" -key "$keyfile" -pubout | tr -d '[:space:]')"
committed="$(grep -v '^untrusted' keys/manifest-signing.pub | tr -d '[:space:]')"

# Normal runs hard-assert the signing key equals the committed key. A rotation
# (force_resign, or the intentional stage-1 divergence under DEVXDK_ROTATION_WINDOW)
# is the only path allowed to start with derived != committed.
if [ "${FORCE_RESIGN:-false}" != "true" ] && [ "$derived" != "$committed" ] && [ "${DEVXDK_ROTATION_WINDOW:-0}" != "1" ]; then
  echo "signing key does not match keys/manifest-signing.pub (rotation must set force_resign)" >&2
  exit 1
fi

is_manifest() {
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if isinstance(d,dict) and 'kind' in d and 'releases' in d else 1)" "$1"
}

# devxdk-mansign embeds a timestamp in every signature, so re-signing an unchanged
# manifest would churn its .minisig on every run. Sign a manifest ONLY when its
# signature is missing or does not verify against the derived key (i.e. its JSON
# changed) — or unconditionally under force_resign (a key rotation).
sign_changed() {
  local f
  for f in *.json; do
    is_manifest "$f" || continue
    if [ "${FORCE_RESIGN:-false}" = "true" ] || ! "$MANSIGN" -verify -pub "$derived" "$f" "$f" >/dev/null 2>&1; then
      "$MANSIGN" -key "$keyfile" "$f"
    fi
  done
}

git config user.name "devxdk-bot"
git config user.email "bot@devxdk.com"
remote="https://x-access-token:${MANIFEST_PUSH_TOKEN}@github.com/devxdk/devxdk.git"

for attempt in 1 2 3 4 5; do
  git fetch origin main
  git reset --hard FETCH_HEAD

  python3 scripts/apply_lifecycle.py
  python3 scripts/apply_revocations.py
  python3 scripts/apply_pending.py
  python3 scripts/scrape.py
  python3 scripts/validate_manifests.py --allowlist-go "$ALLOWLIST_GO"
  sign_changed

  git add -A
  if git diff --cached --quiet; then
    echo "No manifest changes to commit."
    exit 0
  fi
  git commit -m "chore: refresh, rebuild, and re-sign manifests"
  if git push "$remote" HEAD:main; then
    echo "Pushed on attempt ${attempt}."
    exit 0
  fi
  echo "Push rejected; resetting to tip and replaying the full transaction (attempt ${attempt})." >&2
done

echo "Exhausted push retries." >&2
exit 1
