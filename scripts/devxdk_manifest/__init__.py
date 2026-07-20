"""DevXDK manifest build pipeline — standard-library Python only.

This package builds, validates, and signs the signed component manifests served
from manifest.devxdk.com. Every module here is stdlib-only (the ONE exception is
the pinned reference-minisign binary CI shells out to for Ed25519 signature
verification, which stdlib cannot do — see the manifest-repo CI workflow).

Modules:
  versions  — the natural-order version comparator (pinned to the Go client).
  config    — tracked-versions.toml loader (the single source of truth).
"""
