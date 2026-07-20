"""Go scrape adapter — highest stable release from go.dev/dl.

A faithful reproduction of gen-manifest.py's build_go: same stable filter and
numeric version selection, same platform order, same empty released_at (the
go.dev feed carries no date) — so the generated go.json is byte-identical.
"""

from __future__ import annotations

from .. import schema

DL_URL = "https://go.dev/dl/?mode=json"

PLATFORMS = {
    "windows/amd64": "windows-amd64.zip",
    "linux/amd64": "linux-amd64.tar.gz",
    "darwin/amd64": "darwin-amd64.tar.gz",
    "darwin/arm64": "darwin-arm64.tar.gz",
}


def _version_key(entry):
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


def build(fetcher) -> dict:
    releases = fetcher.get_json(DL_URL)

    stable = [r for r in releases if r.get("stable") is True]
    if not stable:
        raise RuntimeError("no stable Go release found")
    chosen = max(stable, key=_version_key)

    ver = chosen["version"].removeprefix("go")
    files_by_name = {f.get("filename"): f for f in chosen.get("files", [])}

    platforms = {}
    for key, suffix in PLATFORMS.items():
        filename = f"go{ver}.{suffix}"
        info = files_by_name.get(filename)
        if info is None:
            raise RuntimeError(f"{filename} not in go.dev release files")
        platforms[key] = schema.asset(
            f"https://go.dev/dl/{filename}",
            info.get("sha256", ""),
            int(info.get("size", 0)),
        )

    # The go.dev feed has no release date; emit released_at empty so the shape
    # matches Node and a refresh doesn't strip the field.
    return schema.component(
        "go", "Go", "runtime",
        [schema.release(ver, "stable", "", platforms)],
    )
