"""Official Windows Nginx ZIPs authenticated with the committed upstream keys."""
from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import tempfile

from .. import config, resolvers, schema


def _verify_signature(archive: pathlib.Path, signature: pathlib.Path, cfg):
    root = cfg.path.parent.parent
    with tempfile.TemporaryDirectory(prefix="nginx-keyring-") as directory:
        os.chmod(directory, 0o700)
        env = dict(os.environ, GNUPGHOME=pathlib.Path(directory).as_posix())
        subprocess.run([
            "bash", str(root / "scripts/ci/verify_keyring.sh"),
            str(root / "scripts/devxdk_manifest/keys/nginx"),
            " ".join(cfg.pins["nginx_keys"]["fingerprints"]),
        ], env=env, check=True, capture_output=True, text=True)
        verified = subprocess.run([
            "gpg", "--batch", "--no-auto-key-retrieve", "--status-fd=1", "--verify",
            str(signature), str(archive),
        ], env=env, check=True, capture_output=True, text=True)
        if "[GNUPG:] VALIDSIG " not in verified.stdout:
            raise RuntimeError("Nginx ZIP has no valid upstream signature")


def build(fetcher, lines=None) -> dict:
    cfg = config.load()
    lines = cfg.component("nginx").lines if lines is None else lines
    accepted_path = cfg.path.parent.parent / "nginx.json"
    accepted = schema.load(accepted_path) if accepted_path.exists() else {"releases": []}
    releases = []
    for lid, line in lines.items():
        if line.retired or line.historical_only:
            continue
        expected = {key for key, plat in line.platforms.items() if plat.type == "scrape"}
        if expected != {"windows/amd64"}:
            raise RuntimeError(f"nginx {lid}: Windows is the only supported scrape target")
        source = resolvers.nginx_newest(fetcher, lid)
        version = source["source_version"]
        url = f"https://nginx.org/download/nginx-{version}.zip"
        body = fetcher.get_bytes(url)
        signature = fetcher.get_bytes(url + ".asc")
        if not body or not signature:
            raise RuntimeError(f"nginx {version}: missing ZIP or signature")
        with tempfile.TemporaryDirectory(prefix="nginx-source-") as directory:
            archive = pathlib.Path(directory) / "nginx.zip"
            sig = pathlib.Path(directory) / "nginx.zip.asc"
            archive.write_bytes(body)
            sig.write_bytes(signature)
            _verify_signature(archive, sig, cfg)
        platforms = {"windows/amd64": schema.asset(url, hashlib.sha256(body).hexdigest(), len(body))}
        # A Unix build may have published this mixed release first. Its date is
        # already immutable, and nginx.org has no separate authoritative date.
        date = next((r.get("released_at", "") for r in accepted["releases"] if r["version"] == version), "")
        releases.append(schema.release(version, line.channel, date, platforms))
    return schema.component("nginx", "Nginx", "service", releases)
