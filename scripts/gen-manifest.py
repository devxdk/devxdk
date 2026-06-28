#!/usr/bin/env python3
"""Regenerate node.json and go.json from official upstream metadata.

DevXDK's component manifest pins exact download URLs plus their SHA256 hashes.
Editing those hashes by hand goes stale the moment upstream ships a new release,
so this script rebuilds the two upstream-sourced manifests directly from the
sources that already publish authoritative hashes:

  - Node.js: https://nodejs.org/dist/index.json (release list) and
    https://nodejs.org/dist/v<VER>/SHASUMS256.txt (per-file sha256). We track the
    newest LTS in the v24.x line.
  - Go: https://go.dev/dl/?mode=json which publishes sha256 + size per file for
    every stable release.

Only the Python standard library is used (urllib.request, json). Upstreams
publish the hashes for us, so no local hashing is required; file sizes are read
from upstream JSON (Go) or via a best-effort HTTP HEAD (Node), falling back to 0.

Run it from the repo root:

    python3 scripts/gen-manifest.py

It overwrites node.json and go.json in the repo root and prints a short summary
to stderr. CI (scrape-and-sign.yml) runs this daily, then re-signs every JSON.
"""

import json
import sys
import urllib.request
from urllib.error import URLError

NODE_INDEX_URL = "https://nodejs.org/dist/index.json"
NODE_LINE = "v24."
GO_DL_URL = "https://go.dev/dl/?mode=json"

# Maps a manifest platform key to the upstream archive filename suffix.
NODE_PLATFORMS = {
    "windows/amd64": "win-x64.zip",
    "linux/amd64": "linux-x64.tar.gz",
    "darwin/amd64": "darwin-x64.tar.gz",
    "darwin/arm64": "darwin-arm64.tar.gz",
}

GO_PLATFORMS = {
    "windows/amd64": "windows-amd64.zip",
    "linux/amd64": "linux-amd64.tar.gz",
    "darwin/amd64": "darwin-amd64.tar.gz",
    "darwin/arm64": "darwin-arm64.tar.gz",
}


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp)


def fetch_text(url):
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read().decode("utf-8")


def head_size(url):
    """Best-effort Content-Length via HTTP HEAD; returns 0 if unavailable."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as resp:
            length = resp.headers.get("Content-Length")
            return int(length) if length else 0
    except (URLError, ValueError, OSError):
        return 0


def remote_size(url):
    """Total size via HEAD, falling back to a ranged GET (Content-Range), so a
    server that omits Content-Length on HEAD doesn't silently yield 0 (which would
    disable the download size check). Warns and returns 0 only when both fail."""
    size = head_size(url)
    if size:
        return size
    try:
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            cr = resp.headers.get("Content-Range")  # e.g. "bytes 0-0/123456"
            if cr and "/" in cr:
                total = cr.rsplit("/", 1)[-1]
                if total.isdigit():
                    return int(total)
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > 1:
                return int(length)
    except (URLError, ValueError, OSError):
        pass
    sys.stderr.write(f"WARNING: could not determine size for {url}; size check disabled\n")
    return 0


def build_node():
    index = fetch_json(NODE_INDEX_URL)

    # index.json is newest-first. Pick the newest v24.x whose "lts" field is a
    # non-false string (i.e. an LTS codename like "Krypton"), not boolean false.
    chosen = None
    for entry in index:
        version = entry.get("version", "")
        if not version.startswith(NODE_LINE):
            continue
        if isinstance(entry.get("lts"), str) and entry["lts"]:
            chosen = entry
            break
    if chosen is None:
        raise RuntimeError(f"no LTS release found in the {NODE_LINE}x line")

    ver = chosen["version"].lstrip("v")
    released_at = chosen.get("date", "")

    shasums = fetch_text(f"https://nodejs.org/dist/v{ver}/SHASUMS256.txt")
    # Each line is "<sha256>  <filename>".
    sha_by_file = {}
    for line in shasums.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            sha_by_file[parts[-1]] = parts[0]

    platforms = {}
    for key, suffix in NODE_PLATFORMS.items():
        filename = f"node-v{ver}-{suffix}"
        sha = sha_by_file.get(filename)
        if sha is None:
            raise RuntimeError(f"sha256 for {filename} not in SHASUMS256.txt")
        url = f"https://nodejs.org/dist/v{ver}/{filename}"
        platforms[key] = {
            "url": url,
            "sha256": sha,
            "size_bytes": remote_size(url),
        }

    return {
        "name": "node",
        "display_name": "Node.js",
        "kind": "runtime",
        "releases": [
            {
                "version": ver,
                "channel": "lts",
                "released_at": released_at,
                "platforms": platforms,
            }
        ],
    }


def build_go():
    releases = fetch_json(GO_DL_URL)

    # The feed is newest-first; keep only stable releases and pick the highest.
    def version_key(entry):
        # entry["version"] looks like "go1.26.3".
        raw = entry.get("version", "go0").removeprefix("go")
        parts = []
        for chunk in raw.split("."):
            num = ""
            for ch in chunk:
                if ch.isdigit():
                    num += ch
                else:
                    break
            parts.append(int(num) if num else 0)
        return tuple(parts)

    stable = [r for r in releases if r.get("stable") is True]
    if not stable:
        raise RuntimeError("no stable Go release found")
    chosen = max(stable, key=version_key)

    ver = chosen["version"].removeprefix("go")
    files_by_name = {f.get("filename"): f for f in chosen.get("files", [])}

    platforms = {}
    for key, suffix in GO_PLATFORMS.items():
        filename = f"go{ver}.{suffix}"
        info = files_by_name.get(filename)
        if info is None:
            raise RuntimeError(f"{filename} not in go.dev release files")
        platforms[key] = {
            "url": f"https://go.dev/dl/{filename}",
            "sha256": info.get("sha256", ""),
            "size_bytes": int(info.get("size", 0)),
        }

    return {
        "name": "go",
        "display_name": "Go",
        "kind": "runtime",
        "releases": [
            {
                "version": ver,
                "channel": "stable",
                # The go.dev feed has no release date; emit the field empty so the
                # Go/Node output shape matches and a refresh doesn't strip it (L).
                "released_at": "",
                "platforms": platforms,
            }
        ],
    }


def write_manifest(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def main():
    node = build_node()
    write_manifest("node.json", node)
    print(
        f"node.json -> {node['releases'][0]['version']} "
        f"({node['releases'][0]['released_at']})",
        file=sys.stderr,
    )

    go = build_go()
    write_manifest("go.json", go)
    print(f"go.json   -> {go['releases'][0]['version']}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
