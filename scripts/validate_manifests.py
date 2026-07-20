#!/usr/bin/env python3
"""Validate every committed manifest and both state files before signing.

This is the gate scrape-and-sign runs before it ever signs, and the manifest-repo
CI runs on every PR. It fails closed on any structural problem so a malformed or
unsafe manifest can never receive a trusted signature.

Checks:
  * root *.json are all component manifests (shape: name/display_name/kind/releases);
  * each release version parses, its channel is consistent with the version's
    prerelease classification (lts only on a stable version), and postgres
    versions are MAJOR.MINOR;
  * platform keys are configured; assets are https + allowlisted-host, a
    lowercase 64-hex sha256, a positive size, and a component-aware archive
    extension (.phar for composer, else .zip/.tar.gz/.tgz/.tar);
  * no duplicate versions, and no two versions that compare EQUAL but differ as
    raw strings (the alias guard — the client looks releases up by raw string);
  * the scrape-versions and asset-revisions state files bind bidirectionally to
    the committed manifests (merge.check_scrape_parity / check_ledger_parity).

The download allowlist is read from the pinned app-src checkout when present
(--allowlist-go), else the vendored copy. Standard library only. Run from the
repo root; exits nonzero with the errors printed if anything fails.
"""

import argparse
import pathlib
import re
import sys
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from devxdk_manifest import allowlist, config, merge, schema  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARCHIVE_EXTS = (".tar.gz", ".tgz", ".tar", ".zip")
VALID_CHANNELS = {"stable", "lts", "prerelease"}


def validate(repo_root=REPO_ROOT, allowlist_go=None) -> list:
    repo_root = pathlib.Path(repo_root)
    cfg = config.load()
    hosts = allowlist.parse_allowlist_go(allowlist_go) if allowlist_go else allowlist.VENDORED_HOSTS

    errors = []

    for path in sorted(repo_root.glob("*.json")):
        data = schema.load(path)
        name = path.stem
        if not schema.is_component_manifest(data):
            errors.append(f"{path.name}: root JSON is not a component manifest (would be skipped by the signer)")
            continue
        errors.extend(_validate_component(cfg, data, name, hosts))

    # State-file binding (both directions). These fail closed so deleting a
    # record or entry can never silently reset a monotonic guard.
    try:
        scrape_state = merge.ScrapeState.load(repo_root / "state" / "scrape-versions.json")
        errors.extend(merge.check_scrape_parity(cfg, scrape_state, repo_root))
    except (OSError, merge.GuardError) as e:
        errors.append(f"scrape-versions.json: {e}")
    try:
        ledger = merge.LedgerState.load(repo_root / "state" / "asset-revisions.json")
        errors.extend(merge.check_ledger_parity(cfg, ledger, repo_root))
    except (OSError, merge.GuardError) as e:
        errors.append(f"asset-revisions.json: {e}")

    return errors


def _validate_component(cfg, data, name, hosts) -> list:
    errors = []
    if data.get("name") != name:
        errors.append(f"{name}.json: name field {data.get('name')!r} != filename")
    if not isinstance(data.get("display_name"), str) or not data["display_name"]:
        errors.append(f"{name}.json: display_name must be a non-empty string")
    kind = data.get("kind")
    if kind not in ("runtime", "service"):
        errors.append(f"{name}.json: kind = {kind!r}")
    releases = data.get("releases")
    if not isinstance(releases, list):
        errors.append(f"{name}.json: releases must be a list")
        return errors

    tracked = name in cfg.components
    if tracked and kind != cfg.component(name).kind:
        errors.append(f"{name}.json: kind {kind!r} != config kind {cfg.component(name).kind!r}")
    if not tracked and releases:
        errors.append(f"{name}.json: untracked component may not publish releases")
        return errors

    parsed = []  # (raw_version, parsed) for the alias/dup guard
    seen_raw = set()
    for rel in releases:
        errors.extend(_validate_release(cfg, name, rel, hosts, tracked))
        ver = rel.get("version")
        if isinstance(ver, str):
            if ver in seen_raw:
                errors.append(f"{name}.json: duplicate version {ver!r}")
            seen_raw.add(ver)
            pv = _try_parse(ver)
            if pv is not None:
                parsed.append((ver, pv))

    # Alias guard: no two distinct raw strings that compare EQUAL (the client
    # sorts by comparator but looks up by exact raw string).
    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            (r1, _p1), (r2, _p2) = parsed[i], parsed[j]
            if r1 != r2 and merge.versions.compare_str(r1, r2) == 0:
                errors.append(f"{name}.json: versions {r1!r} and {r2!r} compare equal but differ (alias)")
    return errors


def _validate_release(cfg, name, rel, hosts, tracked) -> list:
    errors = []
    ver = rel.get("version")
    if not isinstance(ver, str) or not ver:
        return [f"{name}.json: release missing version"]
    pv = _try_parse(ver)
    if pv is None:
        return [f"{name}.json: version {ver!r} does not parse"]

    if name == "postgres" and not re.fullmatch(r"\d+\.\d+", ver):
        errors.append(f"{name}.json: postgres version {ver!r} must be MAJOR.MINOR")

    channel = rel.get("channel")
    if channel not in VALID_CHANNELS:
        errors.append(f"{name}.json {ver}: channel = {channel!r}")
    elif pv.is_prerelease() and channel != "prerelease":
        errors.append(f"{name}.json {ver}: prerelease version must carry channel 'prerelease'")
    elif not pv.is_prerelease() and channel == "prerelease":
        errors.append(f"{name}.json {ver}: stable version must not carry channel 'prerelease'")

    if not isinstance(rel.get("released_at"), str):
        errors.append(f"{name}.json {ver}: released_at must be a string")

    lid = merge.line_for(cfg, name, ver) if tracked else None
    if tracked and lid is None:
        errors.append(f"{name}.json {ver}: no tracked line in config")

    platforms = rel.get("platforms")
    if not isinstance(platforms, dict) or not platforms:
        errors.append(f"{name}.json {ver}: platforms must be a non-empty object")
        return errors

    for pkey, asset in platforms.items():
        if pkey not in config.VALID_PLATFORM_KEYS:
            errors.append(f"{name}.json {ver}: invalid platform key {pkey!r}")
        elif tracked and lid is not None:
            try:
                cfg.find_platform(name, lid, pkey)
            except config.ConfigError:
                errors.append(f"{name}.json {ver}: platform {pkey} is not configured for line {lid}")
        errors.extend(_validate_asset(name, ver, pkey, asset, hosts))
    return errors


def _validate_asset(name, ver, pkey, asset, hosts) -> list:
    errors = []
    if not isinstance(asset, dict):
        return [f"{name}.json {ver} {pkey}: asset must be an object"]
    url = asset.get("url")
    sha = asset.get("sha256")
    size = asset.get("size_bytes")

    if not isinstance(url, str) or not url:
        errors.append(f"{name}.json {ver} {pkey}: url must be a non-empty string")
    else:
        parts = urllib.parse.urlparse(url)
        if parts.scheme != "https":
            errors.append(f"{name}.json {ver} {pkey}: url must be https")
        if not parts.hostname or not allowlist.host_allowed(parts.hostname, hosts):
            errors.append(f"{name}.json {ver} {pkey}: host {parts.hostname!r} not allowlisted")
        exts = (".phar",) if name == "composer" else ARCHIVE_EXTS
        base = parts.path.rsplit("/", 1)[-1].lower()
        if not base.endswith(exts):
            errors.append(f"{name}.json {ver} {pkey}: url extension not in {exts}")

    if not isinstance(sha, str) or not SHA256_RE.match(sha):
        errors.append(f"{name}.json {ver} {pkey}: sha256 must be lowercase 64-hex")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        errors.append(f"{name}.json {ver} {pkey}: size_bytes must be a positive integer")
    return errors


def _try_parse(ver):
    try:
        return merge.versions.parse(ver)
    except merge.versions.ParseError:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate manifests + state before signing.")
    ap.add_argument("--allowlist-go", help="path to internal/download/allowlist.go (pinned app-src)")
    args = ap.parse_args(argv)

    errors = validate(REPO_ROOT, args.allowlist_go)
    if errors:
        sys.stderr.write(f"validate_manifests: {len(errors)} error(s)\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        return 1
    sys.stderr.write("validate_manifests: OK\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
