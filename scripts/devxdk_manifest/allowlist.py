"""The download-host allowlist the manifest validator enforces on every URL.

The authoritative list lives in the app repo's internal/download/allowlist.go.
The signing workflow checks that repo out at a pinned commit and parses the hosts
from it; the VENDORED copy below is the offline fallback for the secretless PR CI
(which cannot fetch the private app repo). A parity test asserts the two agree,
so a W1.1-style drift (a host added in the app but not here) fails CI.
"""

from __future__ import annotations

import re

# Vendored from internal/download/allowlist.go — order-identical, parity-checked.
VENDORED_HOSTS = [
    "php.net", "windows.php.net",
    "nodejs.org",
    "python.org", "www.python.org",
    "go.dev",
    "mariadb.org",
    "postgresql.org", "www.postgresql.org",
    "nginx.org",
    "valkey.io",
    "redis.io",
    "getcomposer.org",
    "dl.google.com",
    "get.enterprisedb.com",
    "dlm.mariadb.com",
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "devxdk.github.io",
    "devxdk.com",
]

_BLOCK_RE = re.compile(r"allowedHosts\s*=\s*\[\]string\{(.*?)\n\}", re.DOTALL)
_STRING_RE = re.compile(r'"([^"]+)"')


def parse_allowlist_go(path) -> list:
    """Extract the ordered host list from internal/download/allowlist.go."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = _BLOCK_RE.search(text)
    if not m:
        raise ValueError(f"{path}: could not find the allowedHosts slice")
    return _STRING_RE.findall(m.group(1))


def host_allowed(host: str, hosts=None) -> bool:
    """Exact match or a subdomain of an allowlisted apex — mirrors the Go
    hostAllowed (rejects look-alikes like php.net.evil.com)."""
    hosts = VENDORED_HOSTS if hosts is None else hosts
    host = host.rstrip(".").lower()
    for a in hosts:
        if host == a or host.endswith("." + a):
            return True
    return False
