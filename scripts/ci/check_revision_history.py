#!/usr/bin/env python3
"""Enforce revision monotonicity across git history.

THE GENERATOR RULE IS NOT THE INVARIANT. schema.write only fires when the
pipeline regenerates a file, and the cases that break monotonicity are exactly
the ones that never run it: a `git revert` of a manifest commit restores an
older file *with its older revision* — content changes, revision goes DOWN, and
every client sees a genuine rollback. A hand-edit, or a merge resolved in favour
of the older side, does the same. Only history can tell "the generator behaved"
apart from "the invariant held".

Three rules:

  1. HEAD INVARIANT, always on — every file in the set carries an integer
     revision >= 1. This is the one that does the real work: a bootstrap-only
     check waves a PARTIALLY backfilled tree through forever, because a manifest
     nothing rescrapes is never written and so never meets schema.write's
     fail-closed rule. The first symptom would be clients refusing that one
     component at runtime.

  2. BOOTSTRAP TRANSITION — if the comparison tree carries no revisions
     anywhere, this commit is the backfill: every file must go absent -> 1 and
     NOTHING ELSE may change. The one place a semantic comparison is sound,
     because no client holds any mark yet, so the reordering hazard that forbids
     it everywhere else has nothing to bite.

  3. MONOTONIC otherwise — BYTE-FIRST, with no projection at all. Identical raw
     bytes pass; bytes that differ at all require head_revision > base_revision;
     a new file must be revision == 1.

Rule 3 deliberately does NOT reconstruct a "content excluding revision"
projection. Token-substituting the base's revision into the head bytes is
ambiguous twice over — a duplicate top-level member has no unique slot, and a
digit-width change (9 -> 10) moves every following byte — and re-serializing is
worse, because app/update.json is produced by Go's updatejson.Encode and no
Python serializer reproduces it byte-for-byte. Byte-first is the only rule that
covers both file kinds with one implementation, and it can never bless a byte
change without a bump, which is the property the client depends on.

What byte-first gives up is "no gratuitous bump" — a commit changing only the
revision passes here. That is harmless to clients (a higher revision is accepted
and recorded) and is asserted directly by schema.write's idempotence tests.

Standard library only. Run from the repo root.
"""

import argparse
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from devxdk_manifest import schema, strictjson  # noqa: E402

APP_UPDATE = "app/update.json"

RETIREMENT_HINT = (
    "there is no deletion policy because there should be no deletion: retire a "
    "component by publishing an EMPTY releases array at a HIGHER revision, which "
    "keeps the mark monotone and keeps the file fetchable. Deleting and later "
    "re-adding it returns the component at revision 1, below every mark clients "
    "already hold, giving it a permanent rollback refusal with no recovery"
)


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)


def _blob(ref, path, cwd=None):
    """The raw bytes of `path` at `ref`, or None when it does not exist there."""
    proc = _git("show", f"{ref}:{path}", cwd=cwd)
    if proc.returncode != 0:
        return None
    return proc.stdout


def _root_json_names(ref, cwd=None):
    proc = _git("ls-tree", "-r", "--name-only", ref, cwd=cwd)
    if proc.returncode != 0:
        raise SystemExit(f"check_revision_history: cannot list {ref}: "
                         f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    names = []
    for line in proc.stdout.decode("utf-8").splitlines():
        name = line.strip()
        if name and "/" not in name and name.endswith(".json"):
            names.append(name)
    return names


def _is_manifest_at(ref, name, cwd=None):
    """Whether `name` at `ref` matches the SIGNER'S OWN predicate.

    Derived from is_component_manifest rather than a hard-coded count, which
    would rot the moment a component is added. A blob that does not parse is not
    a manifest for set-membership purposes; the head invariant reports it.
    """
    raw = _blob(ref, name, cwd=cwd)
    if raw is None:
        return False
    try:
        return schema.is_component_manifest(strictjson.loads(raw))
    except (ValueError, strictjson.StrictJSONError):
        return False


def file_set(base, head, cwd=None):
    """The UNION of the comparison tree's and the head tree's manifest sets.

    A head-derived set cannot see a file that STOPPED existing, and no existing
    gate covers that: merge.py skips a missing manifest outright and only walks
    configured components. The harm is not the deletion but the monotonicity
    hole behind it.
    """
    names = set()
    for ref in (base, head):
        if ref is None:
            continue
        for name in _root_json_names(ref, cwd=cwd):
            if _is_manifest_at(ref, name, cwd=cwd):
                names.add(name)
    for ref in (base, head):
        if ref is not None and _blob(ref, APP_UPDATE, cwd=cwd) is not None:
            names.add(APP_UPDATE)
    return sorted(names)


def _revision_of(raw, label, errors):
    """The revision in `raw`, or None with a reason appended to `errors`."""
    try:
        doc = strictjson.loads(raw)
    except (ValueError, strictjson.StrictJSONError) as e:
        errors.append(f"{label}: not strict JSON: {e}")
        return None
    if not isinstance(doc, dict):
        errors.append(f"{label}: not a JSON object")
        return None
    reason = schema.require_positive_int64(doc.get("revision"), f"{label}: revision")
    if reason is not None:
        errors.append(reason)
        return None
    return doc["revision"]


def check(base, head="HEAD", cwd=None):
    """Return a list of failure strings; empty means the history is monotone."""
    errors = []
    names = file_set(base, head, cwd=cwd)
    if not names:
        return ["check_revision_history: no component manifests found in either tree "
                "-- the file set is derived from is_component_manifest, so an empty set "
                "means the predicate or the refs are wrong, not that there is nothing to check"]

    base_raw = {n: _blob(base, n, cwd=cwd) for n in names}
    head_raw = {n: _blob(head, n, cwd=cwd) for n in names}

    # Presence rules, before any revision comparison.
    for name in names:
        if base_raw[name] is not None and head_raw[name] is None:
            errors.append(f"{name}: present in {base} and absent at {head} -- {RETIREMENT_HINT}")
        elif (base_raw[name] is not None and head_raw[name] is not None
              and name != APP_UPDATE and not _is_manifest_at(head, name, cwd=cwd)):
            errors.append(f"{name}: no longer a component manifest at {head} -- it may not drop "
                          f"silently out of the checked set; {RETIREMENT_HINT}")

    # Rule 1 — the head invariant, always on.
    head_revisions = {}
    for name in names:
        if head_raw[name] is None:
            continue
        head_revisions[name] = _revision_of(head_raw[name], f"{name} at {head}", errors)

    bootstrap = all(
        raw is None or _peek_revision(raw) is None
        for raw in base_raw.values()
    )

    if bootstrap:
        errors.extend(_check_bootstrap(names, base_raw, head_raw, head_revisions, head))
        return errors

    # Rule 3 — byte-first monotonic.
    for name in names:
        if head_raw[name] is None:
            continue  # already reported by the presence rule
        head_rev = head_revisions.get(name)
        if head_rev is None:
            continue  # already reported by the head invariant
        if base_raw[name] is None:
            if head_rev != 1:
                errors.append(f"{name}: new file must start at revision 1, got {head_rev}")
            continue
        if base_raw[name] == head_raw[name]:
            continue  # identical bytes -> the revision is necessarily unchanged
        base_rev = _revision_of(base_raw[name], f"{name} at {base}", errors)
        if base_rev is None:
            continue
        if head_rev <= base_rev:
            errors.append(
                f"{name}: bytes changed but revision {head_rev} is not greater than "
                f"{base_rev} at {base} -- a revert, a hand-edit or a merge resolved in "
                "favour of the older side looks exactly like this, and every client "
                "holding a mark would refuse this component permanently")
    return errors


def _peek_revision(raw):
    """The revision in `raw` if it has a usable one, else None. Deliberately
    permissive: this only decides whether the BASE tree is pre-bootstrap."""
    try:
        doc = strictjson.loads(raw)
    except (ValueError, strictjson.StrictJSONError):
        return None
    if not isinstance(doc, dict):
        return None
    value = doc.get("revision")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _check_bootstrap(names, base_raw, head_raw, head_revisions, head):
    """Rule 2 -- absent -> 1, and nothing else may change.

    At the bootstrap the bytes of every file necessarily differ (a field is
    being added), so byte-first cannot express "nothing else changed" — and it
    is not needed, because no client holds any mark at that commit.
    """
    errors = []
    for name in names:
        if head_raw[name] is None:
            continue  # reported by the presence rule
        if head_revisions.get(name) != 1:
            errors.append(f"{name}: the bootstrap commit must set revision to exactly 1, "
                          f"got {head_revisions.get(name)!r}")
            continue
        if base_raw[name] is None:
            continue  # a genuinely new file at the bootstrap is fine at 1
        try:
            base_doc = strictjson.loads(base_raw[name])
            head_doc = strictjson.loads(head_raw[name])
        except (ValueError, strictjson.StrictJSONError) as e:
            errors.append(f"{name}: not strict JSON: {e}")
            continue
        if name != APP_UPDATE:
            # One cheap belt for the component manifests: the head bytes must be
            # what the canonical serializer produces. True by construction
            # because the bootstrap uses schema.dump_str, and it catches a
            # hand-edited backfill. app/update.json is EXEMPT — its bytes come
            # from Go's updatejson.Encode and no Python serializer reproduces
            # them.
            if schema.dump_str(head_doc).encode("utf-8") != head_raw[name]:
                errors.append(f"{name}: bootstrap bytes are not schema.dump_str output "
                              "(a hand-edited backfill)")
        head_doc.pop("revision", None)
        if head_doc != base_doc:
            errors.append(f"{name}: the bootstrap commit may add revision and NOTHING else -- "
                          "a release, asset or display-name edit smuggled in here would ship "
                          "unreviewed under a commit whose whole purpose is mechanical")
    return errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="Enforce manifest revision monotonicity across history.")
    ap.add_argument("--base", required=True,
                    help="the comparison ref: the merge base on pull_request, the pushed "
                         "range's parent on push, FETCH_HEAD before an automated push")
    ap.add_argument("--head", default="HEAD", help="the ref being checked (default HEAD)")
    ap.add_argument("--repo-root", default=None, help="run git in this directory")
    args = ap.parse_args(argv)

    errors = check(args.base, args.head, cwd=args.repo_root)
    if errors:
        sys.stderr.write(f"check_revision_history: {len(errors)} error(s)\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        return 1
    sys.stderr.write(f"check_revision_history: OK ({args.base} -> {args.head})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
