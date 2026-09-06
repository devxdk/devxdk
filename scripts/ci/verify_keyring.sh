#!/usr/bin/env bash
# The caller owns the isolated keyring and keeps it for subsequent verification.
set -euo pipefail
keydir="${1:?usage: verify_keyring.sh <keydir> '<fingerprints>'}"
fingerprints="${2:?pinned fingerprints are required}"
: "${GNUPGHOME:?the caller must create and export an isolated GNUPGHOME}"
[ -d "$GNUPGHOME" ] && [ ! -L "$GNUPGHOME" ] || { echo '::error::GNUPGHOME must be an isolated directory' >&2; exit 1; }
for fingerprint in $fingerprints; do
  [[ "$fingerprint" =~ ^[0-9A-F]{40}$ ]] || { echo '::error::invalid primary fingerprint' >&2; exit 1; }
done
shopt -s nullglob
keys=("$keydir"/*.key)
[ "${#keys[@]}" -gt 0 ] || { echo "::error::no public keys in $keydir" >&2; exit 1; }
for key in "${keys[@]}"; do
  grep -q '^-----BEGIN PGP PUBLIC KEY BLOCK-----' "$key" || { echo "::error::not an armored public key: $key" >&2; exit 1; }
  gpg --batch --quiet --import "$key" || { echo "::error::failed to import $key" >&2; exit 1; }
done
primary=$(gpg --batch --with-colons --list-keys \
  | awk -F: '$1=="pub"{want=1;next} $1=="fpr"{if(want)print $10;want=0;next} {want=0}' | sort -u)
pinned=$(printf '%s\n' "$fingerprints" | tr ' ' '\n' | awk 'NF' | sort -u)
[ -n "$pinned" ] && [ "$primary" = "$pinned" ] || {
  printf '::error::%s keyring does not contain exactly the pinned primaries\nactual:\n%s\nexpected:\n%s\n' "$keydir" "$primary" "$pinned" >&2
  exit 1
}
printf '%s: exact primary fingerprint set verified\n' "$keydir"
