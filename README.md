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

A private GitHub repo can still serve a *public* GitHub Pages site on Free/Pro,
but keeping the manifest in its own public repo means it is publicly fetchable
with no auth — which is required, because DevXDK clients use no credentials when
downloading.

## Hosting / DNS

1. Enable GitHub Pages: deploy from branch, root (`/`).
2. Set the custom domain to `manifest.devxdk.com` (the `CNAME` file in this repo
   does this).
3. Add a DNS `CNAME` record: `manifest` → `devxdk.github.io`.

## Signing flow

`.github/workflows/scrape-and-sign.yml` runs daily (and on demand):

1. `scripts/scrape.py` (the `devxdk_manifest` package) refreshes `node.json` and
   `go.json` from official upstream metadata (nodejs.org, go.dev) so the pinned
   SHA256 hashes never go stale by hand. The component/line/platform set it
   regenerates is driven by `config/tracked-versions.toml`.
2. Only **component** manifests are signed — a file with a top-level `"kind"` and
   `"releases"`. Each `*.json` is shape-checked first, and a stray root JSON is
   skipped so it can never receive a trusted signature. Each component manifest is
   signed with `cmd/devxdk-mansign` (built from the private app repo), writing a
   detached `*.minisig`.
3. Any changed `*.json` / `*.minisig` is committed and pushed back.

DevXDK signs with a single tool, `cmd/devxdk-mansign`, whose signatures are
byte-identical to the reference minisign binary and accepted by DevXDK's
`internal/minisign` verifier. The base64 **public** key is embedded in DevXDK
(`internal/trust`) and can be overridden for testing via the
`DEVXDK_MANIFEST_PUBKEY` environment variable. The **secret** key (the simple
unencrypted `devxdk-mansign` key — no passphrase) lives only in this repo's
Actions secrets as `MINISIGN_SECRET_KEY`; building the signer needs a read-only
token for the private app repo, `DEVXDK_SIGNER_TOKEN`.

## Coming soon

Components whose JSON has `"releases": []` are not yet published — the client
and UI render these as "coming soon". Their bundles arrive via
`.github/workflows/build-runtimes.yml` (currently a scaffold; see
`docs/runtimes-and-services.md#php-extensions` in the app repo).

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
- **Signing-key rotation / compromise.** Generate a new keypair (`devxdk-mansign
  -key new.key -pubout`), update this repo's `MINISIGN_SECRET_KEY` Actions secret,
  re-sign every component `*.json`, and ship the new **public** key in a DevXDK app
  update. DevXDK
  honors the previous key for a 90-day overlap (see `docs/security.md#trust-roots`
  in the app repo) so in-field clients keep verifying during the transition.
- **Pages / DNS outage.** `manifest.devxdk.com` is a movable CNAME → repoint it, or
  have clients/CI override the source with `DEVXDK_MANIFEST_URL` (any HTTPS URL,
  or a `file://` path for an air-gapped/local mirror). The signature is checked
  regardless of transport, so an alternate host is just as trustworthy.
- **Restore from scratch.** This repo is the source of truth; re-enable Pages
  (deploy from branch root), confirm the `CNAME` file, and re-run the signing
  workflow to regenerate every `*.minisig`.
