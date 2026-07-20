# DevXDK manifest

The public, signed component manifest for [DevXDK](https://devxdk.com), served via
GitHub Pages at **https://manifest.devxdk.com**.

Each component is one JSON file (e.g. `node.json`, `go.json`) plus a detached
[minisign](https://jedisct1.github.io/minisign/) signature (`node.json.minisig`).
DevXDK clients fetch `<host>/<component>.json` and `<host>/<component>.json.minisig`,
verify the signature against an embedded public key, then use the per-platform
download URLs and SHA256 hashes inside the JSON to fetch and verify each runtime
or service.

## Repo split

This **public** repo (`devxdk/devxdk`) holds only public-facing, signed
artifacts. It is safe to expose because every file a client consumes is
signature-verified before use — a tampered manifest fails verification and is
rejected.

The DevXDK application source lives in the **private** repo
`devxdk/devxdkcode`.

This repo is **public** because DevXDK clients fetch it with no credentials: a
private repo's Release assets are not anonymously downloadable, and its Pages
require a paid plan. Keeping the manifest in its own public repo isolates it from
the private application source.

## Hosting / DNS

1. Enable GitHub Pages: deploy from branch, root (`/`).
2. Set the custom domain to `manifest.devxdk.com` (the `CNAME` file in this repo
   does this).
3. Add a DNS `CNAME` record: `manifest` → `devxdk.github.io`.

## Signing flow

`.github/workflows/scrape-and-sign.yml` runs daily (and on demand):

1. One transaction (reset to the live tip and replay on a push race): consume any
   reviewed `revocations/`, fold any `pending/` built/adopted assets
   (`apply_pending.py`), refresh the scraped manifests (`scrape.py`, driven by
   `config/tracked-versions.toml`), then `validate_manifests.py` — so stale output
   can never overwrite a concurrently committed record.
2. Only **component** manifests are signed — a top-level `"kind"` and `"releases"`;
   a stray root JSON is shape-checked out so it never receives a trusted signature.
   Signing is incremental (a manifest is re-signed only when its content changed)
   with `cmd/devxdk-mansign`, writing a detached `*.minisig`.
3. The manifests, both state files, and signatures land in **one** commit, so the
   committed state is never signature-invalid.

DevXDK signs with a single tool, `cmd/devxdk-mansign`, whose signatures are
byte-identical to the reference minisign binary and accepted by DevXDK's
`internal/minisign` verifier. The base64 **public** key is embedded in DevXDK
(`internal/trust`) and can be overridden for testing via the
`DEVXDK_MANIFEST_PUBKEY` environment variable. The **secret** key (the simple
unencrypted `devxdk-mansign` key — no passphrase) lives only in the main-only
`manifest-release` environment as `MINISIGN_SECRET_KEY`, never repo-level;
building the signer checks out the private app repo at the commit pinned in
`config/signer-source.pin` with `DEVXDK_SIGNER_TOKEN`, and the commit is pushed
with `DEVXDK_MANIFEST_PUSH_TOKEN` (never `GITHUB_TOKEN`). CI verifies every
committed pair with reference minisign (`keys/minisign.sha256`) and enforces key
immutability, so the committed state is never signature-invalid.

## Pipeline layout

- `config/tracked-versions.toml` — the single source of truth: every component's
  lines and per-platform provenance (`scrape` / `adopt` / `build`), plus the
  `[pins]` the build recipes verify against. `config/signer-source.pin` pins the
  app-repo commit the signer is built from.
- `state/scrape-versions.json` — the scrape monotonic guard (a floor + committed
  tuples + a durable revoked list per key); `state/asset-revisions.json` — the
  ordering ledger for built/adopted assets. Both are committed, so a missing one
  is a hard error, and both bind bidirectionally to the manifests.
- `pending/` — build/adopt legs drop records here; `apply_pending.py` folds them.
  `revocations/` — reviewed one-shot overrides; `apply_revocations.py` consumes them.
- `keys/` — the committed public trust keys, the pinned reference-minisign hash,
  and rotation records. `scripts/devxdk_manifest/` — the stdlib-Python pipeline
  package + its CLIs; `recipes/` + `templates/` — the build recipes and authored
  `php.ini`s (recipes land with Phase 1/3).
- `.github/workflows/ci.yml` (secretless) validates every PR; `scrape-and-sign.yml`
  signs; `build-runtimes.yml` + `build-runtime-leg.yml` build the bundles.

## Coming soon

Components whose JSON has `"releases": []` are not yet published — the client and
UI render these as "coming soon". Their DevXDK-built/adopted bundles arrive via
the four-stage `.github/workflows/build-runtimes.yml` (plan → static per-platform
legs → publish → finalize): each is built, smoke-tested, published to a Release,
and queued as a `pending/` record that scrape-and-sign folds into the signed
manifest. The per-component recipes land with Phase 1/3.

## Recovery

The manifest is signature-gated, so most failure modes are fail-safe: a client
that can't fetch or can't verify a manifest simply can't *install new* versions —
already-installed runtimes/services keep working.

- **Bad or corrupt manifest published.** Clients reject any `*.json` whose
  `*.minisig` doesn't verify, so a garbled push can't harm installs. Fix forward:
  `git revert` the bad commit (the workflow re-signs on push) or re-run
  `scrape-and-sign.yml` manually. To verify locally before pushing:
  `minisign -V -P <pubkey> -m node.json` (any reference minisign accepts the
  signatures `cmd/devxdk-mansign` writes).
- **Signing-key rotation / compromise.** DevXDK embeds ONE key per trust root, so
  rotation is a two-stage maintenance-window migration, not a continuous overlap:
  stage 1 ships an app update whose embedded trust root is the NEW key, still
  signed by the OLD key; stage 2 cuts signing over to the new key in quick
  succession (`force_resign`, gated by the old-key-signed record CI requires in
  `keys/rotations/`). A client that took stage 1 verifies new-key signatures; one
  that skipped it must reinstall. Full detail in `docs/security.md#trust-roots`.
- **Pages / DNS outage.** `manifest.devxdk.com` is a movable CNAME → repoint it, or
  have clients/CI override the source with `DEVXDK_MANIFEST_URL` (any HTTPS URL,
  or a `file://` path for an air-gapped/local mirror). The signature is checked
  regardless of transport, so an alternate host is just as trustworthy.
- **Restore from scratch.** This repo is the source of truth; re-enable Pages
  (deploy from branch root), confirm the `CNAME` file, and re-run the signing
  workflow to regenerate every `*.minisig`.
