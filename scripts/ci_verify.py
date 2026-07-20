#!/usr/bin/env python3
"""Secretless CI checks: manifest signature verification and key immutability.

Two subcommands, both run by the manifest-repo CI (no secrets):

  verify-signatures  Verify every committed root manifest against
                     keys/manifest-signing.pub and app/update.json (when present)
                     against keys/app-release-signing.pub, using the pinned
                     reference minisign binary. Also checks each signature's
                     trusted-comment file: field and rejects an orphan half.

  check-keys         Enforce the trust-key rules against the base/predecessor
                     commit: keys are PR-immutable; a push-to-main key change
                     needs an old-key-signed rotation record naming the new key;
                     a key ADDITION is legal only when the path never existed in
                     history (the one-time seed); a deletion always fails.

The minisign and record-verification calls are injectable so the git-decision
logic is unit-tested without the reference binary. Standard library only.
"""

import argparse
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from devxdk_manifest import schema  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

MANIFEST_KEY = "keys/manifest-signing.pub"
RELEASE_KEY = "keys/app-release-signing.pub"
KEY_FILES = {MANIFEST_KEY: "manifest", RELEASE_KEY: "release"}
ZERO_SHA = "0000000000000000000000000000000000000000"


# -- git helpers -----------------------------------------------------------

def _git(args, cwd):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


def _pub_b64(text: str):
    """The base64 key line of a 2-line minisign .pub (skip the comment line)."""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("untrusted comment:"):
            return line
    return None


# -- signature verification ------------------------------------------------

def default_minisign_verify(minisign_bin):
    def verify(json_path, pubkey_path):
        p = subprocess.run(
            [str(minisign_bin), "-V", "-q", "-p", str(pubkey_path), "-m", str(json_path)],
            capture_output=True, text=True,
        )
        return p.returncode == 0, (p.stderr or p.stdout).strip()
    return verify


def _trusted_comment_file(sig_path: pathlib.Path):
    """Return the file: field of a .minisig trusted comment, or None."""
    lines = sig_path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if line.startswith("trusted comment:") and "file:" in line:
            return line.split("file:", 1)[1].strip()
    return None


def verify_signatures(repo_root, verify) -> list:
    """Verify every signed pair. `verify(json_path, pubkey_path) -> (ok, msg)`."""
    repo_root = pathlib.Path(repo_root)
    errors = []

    pairs = []
    for path in sorted(repo_root.glob("*.json")):
        if schema.is_component_manifest(schema.load(path)):
            pairs.append((path, repo_root / MANIFEST_KEY, f"{path.stem}.json"))
    app_update = repo_root / "app" / "update.json"
    if app_update.exists():
        pairs.append((app_update, repo_root / RELEASE_KEY, "update.json"))

    for json_path, pubkey, expected in pairs:
        sig = json_path.with_suffix(json_path.suffix + ".minisig")
        if not sig.exists():
            errors.append(f"{json_path.name}: missing {sig.name}")
            continue
        ok, msg = verify(json_path, pubkey)
        if not ok:
            errors.append(f"{json_path.name}: signature does not verify ({msg})")
            continue
        tc = _trusted_comment_file(sig)
        if tc != expected:
            errors.append(f"{json_path.name}: trusted-comment file:{tc!r} != {expected!r}")

    # Orphan .minisig with no signed JSON.
    signed = {p.name for p, _k, _e in pairs}
    for sig in sorted(repo_root.glob("*.minisig")):
        base = sig.name[:-len(".minisig")]
        if base not in signed:
            errors.append(f"{sig.name}: orphan signature (no {base})")
    return errors


# -- key immutability ------------------------------------------------------

def check_keys(repo_root, event_name, cmp_sha, verify_record) -> list:
    """Enforce key immutability against cmp_sha. `verify_record(record_path,
    old_pub, new_pub, trust_root) -> (ok, msg)` verifies a rotation record's
    old-key signature and that it names the new key (injectable for tests)."""
    repo_root = pathlib.Path(repo_root)
    errors = []

    if event_name == "push" and (not cmp_sha or cmp_sha == ZERO_SHA):
        return ["push event carries a zero/empty predecessor SHA (main predates Phase 0)"]

    for keyfile, trust_root in KEY_FILES.items():
        rc, _ = _git(["diff", "--quiet", cmp_sha, "HEAD", "--", keyfile], repo_root)
        if rc == 0:
            continue  # unchanged

        existed_rc, _ = _git(["cat-file", "-e", f"{cmp_sha}:{keyfile}"], repo_root)
        existed = existed_rc == 0
        present_now = (repo_root / keyfile).exists()

        if not present_now:
            errors.append(f"{keyfile}: key deletion is never allowed")
            continue

        if not existed:
            # Addition — legal only if the path NEVER existed reachable from cmp.
            _rc, out = _git(["log", "--oneline", cmp_sha, "--", keyfile], repo_root)
            if out:
                errors.append(f"{keyfile}: delete-then-reseed is not allowed (path exists in history)")
            # else: one-time seed — allowed (verify-signatures proves payloads verify)
            continue

        # Modification.
        if event_name != "push":
            errors.append(f"{keyfile}: keys are immutable in a pull request")
            continue

        old_pub = _pub_b64(_git(["show", f"{cmp_sha}:{keyfile}"], repo_root)[1])
        new_pub = _pub_b64((repo_root / keyfile).read_text(encoding="utf-8"))
        record = _find_new_rotation_record(repo_root, cmp_sha, trust_root)
        if record is None:
            errors.append(f"{keyfile}: rotated without an old-key-signed rotation record")
            continue
        ok, msg = verify_record(record, old_pub, new_pub, trust_root)
        if not ok:
            errors.append(f"{keyfile}: rotation record invalid ({msg})")
    return errors


def _find_new_rotation_record(repo_root, cmp_sha, trust_root):
    """The rotation record for trust_root added in cmp..HEAD, or None."""
    _rc, out = _git(["diff", "--name-only", "--diff-filter=A", cmp_sha, "HEAD", "--", "keys/rotations/"], repo_root)
    for name in out.splitlines():
        name = name.strip()
        if name.endswith(f"-{trust_root}.json"):
            return pathlib.Path(repo_root) / name
    return None


def default_record_verify(minisign_bin):
    def verify(record_path, old_pub, new_pub, trust_root):
        sig = record_path.with_suffix(record_path.suffix + ".minisig")
        if not sig.exists():
            return False, f"missing {sig.name}"
        # The record is signed by the OLD secret; verify against the OLD public key.
        old_pub_file = record_path.parent / ".rotation-oldkey.pub"
        old_pub_file.write_text(f"untrusted comment: rotation old key\n{old_pub}\n", encoding="utf-8")
        try:
            p = subprocess.run(
                [str(minisign_bin), "-V", "-q", "-p", str(old_pub_file), "-m", str(record_path)],
                capture_output=True, text=True,
            )
        finally:
            old_pub_file.unlink(missing_ok=True)
        if p.returncode != 0:
            return False, "old-key signature does not verify"
        rec = json.loads(record_path.read_text(encoding="utf-8"))
        if rec.get("trust_root") != trust_root:
            return False, f"trust_root {rec.get('trust_root')!r} != {trust_root!r}"
        if rec.get("old_pub") != old_pub:
            return False, "record old_pub != committed old key"
        if rec.get("new_pub") != new_pub:
            return False, "record new_pub != installed new key"
        return True, "ok"
    return verify


def main(argv=None):
    ap = argparse.ArgumentParser(description="Secretless CI signature + key checks.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    vs = sub.add_parser("verify-signatures")
    vs.add_argument("--minisign", required=True)

    ck = sub.add_parser("check-keys")
    ck.add_argument("--event", required=True, choices=["push", "pull_request"])
    ck.add_argument("--cmp-sha", required=True)
    ck.add_argument("--minisign", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "verify-signatures":
        errors = verify_signatures(REPO_ROOT, default_minisign_verify(args.minisign))
    else:
        errors = check_keys(REPO_ROOT, args.event, args.cmp_sha, default_record_verify(args.minisign))

    if errors:
        sys.stderr.write(f"ci_verify {args.cmd}: {len(errors)} error(s)\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        return 1
    sys.stderr.write(f"ci_verify {args.cmd}: OK\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
