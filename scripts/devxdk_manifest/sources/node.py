"""Newest stable release of each configured Node family from the official feed."""
from __future__ import annotations

import functools
import re

from .. import config, schema, versions

INDEX_URL = "https://nodejs.org/dist/index.json"
PLATFORMS = {
    "windows/amd64": "win-x64.zip",
    "linux/amd64": "linux-x64.tar.gz",
    "darwin/amd64": "darwin-x64.tar.gz",
    "darwin/arm64": "darwin-arm64.tar.gz",
}


def build(fetcher, lines=None) -> dict:
    lines = config.load().component("node").lines if lines is None else lines
    index = fetcher.get_json(INDEX_URL)
    if not isinstance(index, list):
        raise RuntimeError("Node release index must be a list")
    releases = []
    for lid, line in lines.items():
        if line.retired or line.historical_only:
            continue
        candidates = []
        for entry in index:
            raw = entry.get("version", "")
            if not versions.in_family(raw, lid) or versions.parse(raw).is_prerelease():
                continue
            if line.track == "lts" and not (isinstance(entry.get("lts"), str) and entry["lts"]):
                continue
            candidates.append(entry)
        if not candidates:
            raise RuntimeError(f"no Node {line.track} release for family {lid}")
        chosen = max(candidates, key=functools.cmp_to_key(
            lambda a, b: versions.compare_str(a["version"], b["version"])))
        ver = chosen["version"].removeprefix("v")
        shasums = fetcher.get_text(f"https://nodejs.org/dist/v{ver}/SHASUMS256.txt")
        hashes = {}
        for raw in shasums.splitlines():
            fields = raw.split()
            if len(fields) != 2:
                raise RuntimeError(f"malformed Node checksum line for {ver}")
            sha, filename = fields
            if not re.fullmatch(r"[0-9a-fA-F]{64}", sha) or filename in hashes:
                raise RuntimeError(f"invalid or duplicate Node checksum for {filename}")
            hashes[filename] = sha.lower()
        platforms = {}
        for key, suffix in PLATFORMS.items():
            if key not in line.platforms:
                continue
            filename = f"node-v{ver}-{suffix}"
            sha = hashes.get(filename)
            if sha is None:
                raise RuntimeError(f"sha256 for {filename} not in SHASUMS256.txt")
            url = f"https://nodejs.org/dist/v{ver}/{filename}"
            size = fetcher.remote_size(url)
            if size <= 0:
                raise RuntimeError(f"Node archive is missing or unsized: {url}")
            platforms[key] = schema.asset(url, sha, size)
        if set(platforms) != set(line.platforms):
            raise RuntimeError(f"Node {lid}: unsupported configured platform")
        releases.append(schema.release(ver, line.channel, chosen.get("date", ""), platforms))
    return schema.component("node", "Node.js", "runtime", releases)
