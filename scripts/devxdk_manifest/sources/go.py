"""Newest stable release per configured Go family from go.dev."""
from __future__ import annotations

import functools
import re

from .. import config, schema, versions

DL_URL = "https://go.dev/dl/?mode=json"
PLATFORMS = {
    "windows/amd64": "windows-amd64.zip",
    "linux/amd64": "linux-amd64.tar.gz",
    "darwin/amd64": "darwin-amd64.tar.gz",
    "darwin/arm64": "darwin-arm64.tar.gz",
}


def build(fetcher, lines=None) -> dict:
    lines = config.load().component("go").lines if lines is None else lines
    upstream = fetcher.get_json(DL_URL)
    if not isinstance(upstream, list):
        raise RuntimeError("Go release index must be a list")
    releases = []
    for lid, line in lines.items():
        if line.retired or line.historical_only:
            continue
        candidates = [r for r in upstream if r.get("stable") is True
                      and versions.in_family(r.get("version", ""), lid)
                      and not versions.parse(r["version"]).is_prerelease()]
        if not candidates:
            raise RuntimeError(f"no stable Go release for family {lid}")
        chosen = max(candidates, key=functools.cmp_to_key(
            lambda a, b: versions.compare_str(a["version"], b["version"])))
        ver = chosen["version"].removeprefix("go")
        files = {f.get("filename"): f for f in chosen.get("files", [])}
        platforms = {}
        for key, suffix in PLATFORMS.items():
            if key not in line.platforms:
                continue
            filename = f"go{ver}.{suffix}"
            info = files.get(filename)
            if info is None:
                raise RuntimeError(f"{filename} not in go.dev release files")
            sha, size = info.get("sha256"), info.get("size")
            if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
                    or not isinstance(size, int) or isinstance(size, bool) or size <= 0):
                raise RuntimeError(f"{filename}: invalid SHA256 or size")
            platforms[key] = schema.asset(f"https://go.dev/dl/{filename}", sha, size)
        if set(platforms) != set(line.platforms):
            raise RuntimeError(f"Go {lid}: unsupported configured platform")
        releases.append(schema.release(ver, line.channel, "", platforms))
    return schema.component("go", "Go", "runtime", releases)
