"""Node.js scrape adapter — newest LTS in the tracked line from nodejs.org.

A faithful reproduction of gen-manifest.py's build_node: same index selection,
same SHASUMS256.txt parse, same platform order, same field order — so the
generated node.json is byte-identical to what gen-manifest.py produced.
"""

from __future__ import annotations

from .. import schema

INDEX_URL = "https://nodejs.org/dist/index.json"
DEFAULT_LINE_PREFIX = "v24."

# Manifest platform key -> upstream archive filename suffix. Insertion order is
# the manifest's platform order, so it must not change.
PLATFORMS = {
    "windows/amd64": "win-x64.zip",
    "linux/amd64": "linux-x64.tar.gz",
    "darwin/amd64": "darwin-x64.tar.gz",
    "darwin/arm64": "darwin-arm64.tar.gz",
}


def build(fetcher, line_prefix: str = DEFAULT_LINE_PREFIX) -> dict:
    index = fetcher.get_json(INDEX_URL)

    # index.json is newest-first. Pick the newest entry in the tracked line whose
    # "lts" field is a non-empty codename string (not boolean false).
    chosen = None
    for entry in index:
        version = entry.get("version", "")
        if not version.startswith(line_prefix):
            continue
        if isinstance(entry.get("lts"), str) and entry["lts"]:
            chosen = entry
            break
    if chosen is None:
        raise RuntimeError(f"no LTS release found in the {line_prefix}x line")

    ver = chosen["version"].lstrip("v")
    released_at = chosen.get("date", "")

    shasums = fetcher.get_text(f"https://nodejs.org/dist/v{ver}/SHASUMS256.txt")
    sha_by_file = {}
    for line in shasums.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            sha_by_file[parts[-1]] = parts[0]

    platforms = {}
    for key, suffix in PLATFORMS.items():
        filename = f"node-v{ver}-{suffix}"
        sha = sha_by_file.get(filename)
        if sha is None:
            raise RuntimeError(f"sha256 for {filename} not in SHASUMS256.txt")
        url = f"https://nodejs.org/dist/v{ver}/{filename}"
        platforms[key] = schema.asset(url, sha, fetcher.remote_size(url))

    return schema.component(
        "node", "Node.js", "runtime",
        [schema.release(ver, "lts", released_at, platforms)],
    )
