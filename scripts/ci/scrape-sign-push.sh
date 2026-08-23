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

# is_manifest decides WHICH FILES GET SIGNED, so a document it admits is a
# document the manifest key signs. It goes through the same strict parser as
# everything else: a duplicate or case-fold-colliding member must never reach
# the signer.
#
# PYTHONPATH=scripts because of THIS SCRIPT'S CWD: scrape-and-sign.yml runs it
# with no `working-directory:`, so cwd is the repo ROOT — which the body below
# confirms by invoking `python3 scripts/apply_lifecycle.py` and globbing
# root-level *.json. The package lives at scripts/devxdk_manifest/, so a bare
# import is not on sys.path. Do not "simplify" the prefix away.
is_manifest() {
  PYTHONPATH=scripts python3 -c "
import sys
from devxdk_manifest import schema, strictjson
try:
    d = strictjson.load(sys.argv[1])
except Exception:
    sys.exit(1)
sys.exit(0 if schema.is_component_manifest(d) else 1)
" "$1"
}

# The trusted-comment timestamp of a .minisig, or empty when it is missing,
# duplicated, malformed or non-positive. Parsed HERE rather than through a new
# devxdk-mansign flag: the signer lives in the APP repo and is built from
# config/signer-source.pin, so a flag would have to land there and be re-pinned
# before this commit could work — inverting the ordering §4 rests on. Parsing it
# unverified is fine: sign_changed verifies the signature separately, and every
# value this cannot make sense of forces a re-sign anyway.
sig_timestamp() {
  python3 -c "
import re, sys
try:
    lines = open(sys.argv[1], encoding='utf-8').read().splitlines()
except OSError:
    sys.exit(0)
stamps = []
for line in lines:
    if line.startswith('trusted comment:'):
        stamps += re.findall(r'timestamp:(\d+)', line)
if len(stamps) != 1:
    sys.exit(0)          # missing or duplicate -> force a re-sign
value = int(stamps[0])
if value <= 0:
    sys.exit(0)
print(value)
" "$1"
}

# Anti-freeze needs fresh timestamps to keep flowing even when content does not
# change, or the client's 90-day rule fires on perfectly healthy components. So
# a manifest is re-signed when its signature is missing, does not verify, or is
# simply OLD — bounded at 7 days against a 30-day client warn and a 90-day
# refuse, capping churn at ~52 signature-only commits per manifest per year.
RESIGN_MAX_AGE=$((7 * 24 * 3600))
# "Materially future" matches the client's futureSkew (internal/manifest's
# 48h constant). An independent knob, but keep the cron trigger <= the client
# refusal or a band stays refused indefinitely: verification never looks at the
# timestamp, so a far-future signature verifies happily, is never "older than 7
# days", and would never be refreshed — while the client refuses it. That
# component would be stuck un-refreshed and permanently unavailable.
RESIGN_FUTURE_SKEW=$((48 * 3600))

# Missing, duplicate, malformed, non-positive or materially future all force a
# re-sign, because a fresh signature is the fix in every one of those cases.
needs_resign() {
  local f="$1" sig="$1.minisig" now stamp
  [ -f "$sig" ] || return 0
  "$MANSIGN" -verify -pub "$derived" "$f" "$f" >/dev/null 2>&1 || return 0
  stamp="$(sig_timestamp "$sig")"
  [ -n "$stamp" ] || return 0
  now="$(date -u +%s)"
  [ "$((now - stamp))" -le "$RESIGN_MAX_AGE" ] || return 0
  [ "$((stamp - now))" -le "$RESIGN_FUTURE_SKEW" ] || return 0
  return 1
}

# NOTE ON FORCE_RESIGN: it looks like the right switch for this and is not — it
# SKIPS the signing-key-equals-committed-key assertion above, because it exists
# for key rotation. Using it weekly would disable that guard weekly. The age
# rule lives on the normal path with that assert fully intact.
#
# A signature-only refresh must NOT touch the JSON, and therefore must not bump
# `revision`: that is what lands the refreshed manifest on the client's
# equal-revision + equal-hash fast path instead of looking like new content.
# This function rewrites only the .minisig; keep it that way.
sign_changed() {
  local f
  for f in *.json; do
    is_manifest "$f" || continue
    if [ "${FORCE_RESIGN:-false}" = "true" ] || needs_resign "$f"; then
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
  # CI alone is DETECTIVE and that is too late here: this pushes straight to
  # main and Pages serves from main, so a push-triggered check reports after the
  # bad revision is already fetchable and a red check un-publishes nothing.
  # INSIDE the loop deliberately — a rejected push resets to the new tip and
  # replays the whole transaction, so a check hoisted out would be validating a
  # base that no longer exists.
  python3 scripts/ci/check_revision_history.py --base FETCH_HEAD --head HEAD
  if git push "$remote" HEAD:main; then
    echo "Pushed on attempt ${attempt}."
    exit 0
  fi
  echo "Push rejected; resetting to tip and replaying the full transaction (attempt ${attempt})." >&2
done

echo "Exhausted push retries." >&2
exit 1
