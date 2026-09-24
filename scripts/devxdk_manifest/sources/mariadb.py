"""MariaDB scrape adapter — newest point release per tracked major.minor line.

downloads.mariadb.org's REST API publishes a sha256 per file (checksum.sha256sum),
while the durable download URLs live on archive.mariadb.org (the plan's chosen
host). So the adapter reads the hash from the REST metadata and constructs the
archive URL, sizing it with a HEAD — which also proves the archive file exists
(a zero/absent size is fail-closed, so a manifest never points at a dead URL).

One release per tracked line; recompose orders them newest-first. The tracked
line set is asserted against tracked-versions.toml by a parity test, so a config
line without an adapter entry (or vice versa) fails CI rather than silently going
unscraped.
"""

from __future__ import annotations

from .. import config, schema

REST_BASE = "https://downloads.mariadb.org/rest-api/mariadb"
ARCHIVE_BASE = "https://archive.mariadb.org"

# Manifest platform key -> (REST/archive file basename suffix, archive subdir).
# The tracked upstream REST feeds publish Windows, Linux, and source archives,
# but no macOS binaries. macOS intentionally has no MariaDB preset entry until
# a separately reviewed provider supplies a usable native distribution.
PLATFORMS = {
    "windows/amd64": ("winx64.zip", "winx64-packages"),
    "linux/amd64": ("linux-systemd-x86_64.tar.gz", "bintar-linux-systemd-x86_64"),
}


def _newest_release(releases: dict) -> str:
    """The numerically-highest release id — the feed's dict order is not trusted."""
    return max(releases, key=lambda v: [int(x) for x in v.split(".")])


def _sha256(file_entry: dict) -> str:
    cs = file_entry.get("checksum") or {}
    sha = (cs.get("sha256sum") or "").strip().lower()
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise RuntimeError(f"missing/malformed sha256sum for {file_entry.get('file_name')!r}")
    return sha


def build(fetcher, lines: dict | None = None) -> dict:
    lines = lines if lines is not None else config.load().component("mariadb").lines
    releases = []
    for line, policy in lines.items():
        if policy.retired or policy.historical_only:
            continue
        data = fetcher.get_json(f"{REST_BASE}/{line}/")
        rel_map = data.get("releases") or {}
        if not rel_map:
            raise RuntimeError(f"mariadb REST has no releases for line {line}")
        ver = _newest_release(rel_map)
        files = {f.get("file_name"): f for f in rel_map[ver].get("files", [])}

        platforms = {}
        expected = {p for p, value in policy.platforms.items() if value.type == "scrape"}
        for pkey, (suffix, subdir) in PLATFORMS.items():
            if pkey not in expected:
                continue
            fname = f"mariadb-{ver}-{suffix}"
            entry = files.get(fname)
            if entry is None:
                raise RuntimeError(f"mariadb {ver}: {fname} not in the REST file list")
            sha = _sha256(entry)
            url = f"{ARCHIVE_BASE}/mariadb-{ver}/{subdir}/{fname}"
            size = fetcher.remote_size(url)
            if size <= 0:
                raise RuntimeError(f"mariadb {ver}: {url} is missing or unsized on archive.mariadb.org")
            platforms[pkey] = schema.asset(url, sha, size)

        # No release date in the metadata used here; released_at stays empty.
        if set(platforms) != expected:
            raise RuntimeError(f"mariadb {line}: unsupported configured scrape platform")
        releases.append(schema.release(ver, policy.channel, "", platforms))

    return schema.component("mariadb", "MariaDB", "service", releases)
