"""Append-only build receipts: provenance survives workflow-artifact expiration."""
from __future__ import annotations

import pathlib

from . import publication as pub, strictjson


def path_for(item):
    pub.identity(item)
    platform = item["platform"].replace("/", "-")
    return f"build-receipts/{item['component']}/{item['version']}/{platform}/e{item['epoch']}-r{item['revision']}.json"


def operation_path(operation_id):
    return f"build-operations/{pub.basename(operation_id)}.json"


def input_pins(pins, item):
    provider = item["provider"]
    names = {
        "devxdk-nginx-unix": ("openssl", "pcre2", "zlib", "nginx_keys"),
        "devxdk-php-spc": ("static_php_cli", "php_keys"),
        "devxdk-php-windows": ("php_redis",),
        "devxdk-redis-unix": ("redis_hashes",),
        "devxdk-redis-msys2": ("redis_hashes",),
        "devxdk-valkey-unix": ("valkey_hashes",),
        "devxdk-valkey-msys2": ("valkey_hashes",),
    }.get(provider, ())
    return {name: pins[name] for name in names}


def make(operation, item, meta, members, artifact, pins):
    pub.validate_metas([item], [meta])
    receipt = {"schema": 1, "operation_id": operation["id"],
               "source_commit": operation["source_commit"], "item": item,
               "meta": meta, "members": members, "artifact": artifact,
               "inputs": input_pins(pins, item)}
    validate(receipt)
    return receipt


def validate(receipt):
    if not isinstance(receipt, dict) or receipt.get("schema") != 1:
        raise pub.PublicationError("invalid receipt schema")
    pub.basename(receipt.get("operation_id"))
    import re
    if not re.fullmatch(r"[0-9a-f]{40}", receipt.get("source_commit", "")):
        raise pub.PublicationError("invalid receipt source commit")
    pub.validate_metas([receipt["item"]], [receipt["meta"]])
    artifact = receipt.get("artifact")
    if not isinstance(artifact, dict) or not str(artifact.get("artifact_id", "")).isdigit():
        raise pub.PublicationError("missing receipt artifact id")
    pub.digest(artifact.get("manifest_sha256"))
    if not isinstance(receipt.get("inputs"), dict) or not isinstance(receipt.get("members"), list):
        raise pub.PublicationError("missing receipt inputs or members")
    names = set()
    for member in receipt["members"]:
        name = pub.basename(member.get("name"))
        if name in names or not isinstance(member.get("object_code"), bool):
            raise pub.PublicationError("invalid receipt member set")
        names.add(name)
        pub.digest(member.get("sha256"))
        pub.positive(member.get("size_bytes"), "member size")
    meta = receipt["meta"]
    if meta["ordering_kind"] == "built":
        archives = [m for m in receipt["members"] if m["name"] == meta.get("archive")]
        if len(archives) != 1 or not archives[0]["object_code"] or any(
                archives[0][k] != meta[k] for k in ("sha256", "size_bytes")):
            raise pub.PublicationError("receipt archive is missing or inconsistent")
    elif receipt["members"]:
        raise pub.PublicationError("adopted receipt must not publish members")
    return receipt


def load(root, item):
    path = pathlib.Path(root) / path_for(item)
    if not path.exists():
        return None
    receipt = validate(strictjson.load(path))
    if pub.identity(receipt["item"]) != pub.identity(item):
        raise pub.PublicationError(f"receipt identity differs from its path: {path}")
    return receipt


def existing(root, component, version, platform):
    directory = pathlib.Path(root) / 'build-receipts' / pub.basename(component) / pub.basename(version) / platform.replace('/', '-')
    result = []
    for path in sorted(directory.glob('*.json')):
        receipt = validate(strictjson.load(path))
        if pathlib.Path(root) / path_for(receipt['item']) != path:
            raise pub.PublicationError(f"receipt identity differs from path: {path}")
        result.append(receipt)
    return result


def additions(root, operation, new_receipts):
    """Prepare immutable files; an identical retry keeps the original provenance."""
    result = {operation_path(operation['id']): pub.encode(operation)}
    for receipt in new_receipts:
        path = path_for(receipt['item'])
        old_path = pathlib.Path(root) / path
        if old_path.exists():
            old = validate(strictjson.load(old_path))
            if (pub.identity(old['item']) != pub.identity(receipt['item'])
                    or old['members'] != receipt['members'] or old['meta'] != receipt['meta']
                    or old['inputs'] != receipt['inputs']):
                raise pub.PublicationError(f"conflicting immutable receipt: {path}")
            result[path] = old_path.read_bytes()
        else:
            result[path] = pub.encode(receipt)
    return result
