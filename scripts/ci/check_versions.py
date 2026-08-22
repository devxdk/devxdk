#!/usr/bin/env python3
"""DevXDK version-currency gate - one scanner, two repos, two modes.

CANONICAL COPY: ``devxdk/devxdk`` (this repo). The private app repo vendors a
BYTE-IDENTICAL copy at the same path and the app repo's ``vector-parity`` job
``cmp``s the two, exactly as it already does for ``version-vectors.json``. Edit
this file here; never edit the vendored copy directly.

Standard library only, and **no syntax newer than Python 3.12**. That floor is
not a theoretical lower bound, it is the one CI executes: the two ``versions-*``
workflows run this file under current stable, and its tests run inside the
manifest repo's ``python`` job, which is pinned to 3.12. Claiming 3.11 would be
a support claim nothing tests; claiming 3.14 would break that job.

Two modes, deliberately in two separate workflow files:

``--mode consistency``   OFFLINE. Runs on pull_request + push, so it is the
                         required check. Two directions:
                           * forward - every inventory row is still present
                             where the row says, in the declared number of
                             places, and every site agrees;
                           * reverse - every pin-bearing construct found in the
                             tree is either covered by a row or explicitly
                             allowlisted. Without this the gate cannot see a
                             NEWLY ADDED pin, which is the drift the inventory
                             exists to stop.
                         It also enforces the exception model (below) and fails
                         past an ``expires_on`` date.

``--mode currency``      NETWORKED. Runs on schedule + workflow_dispatch only,
                         so it reports no check on a PR. Compares each row to
                         the authoritative API its own row names. A network or
                         parse failure FAILS the job - a currency gate that goes
                         green because it could not reach the registry is worse
                         than no gate.

THE EXCEPTION MODEL. An excepted row carries exactly one of:
  * ``condition``  - a HOLD, blocked by something outside our control (an
                      upstream peer range). No date: it clears when upstream
                      moves.
  * ``expires_on`` - a TIME-BOUNDED exception, where the blocker is our own
                      verification cost. A condition would never become true on
                      its own, so it carries a date and the gate FAILS past it.
Never both, never neither. Without that, a documented exception silently becomes
a permanent one, which is how the drift this gate exists to stop started.

THE ALLOWLIST MODEL. Floating runner labels (``ubuntu-latest`` and friends) and
local ``./...`` composite actions are ALLOWLISTED as deliberately floating - they
are the absence of a pin, not a pin whose value we track. Only DATED labels get
rows.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request

SCHEMA_VERSION = 1

# Network policy for --mode currency. Named here rather than per call site so a
# row can never quietly get a laxer one.
HTTP_TIMEOUT = 30          # seconds per attempt
HTTP_RETRIES = 3           # total attempts
HTTP_BACKOFF = 2.0         # seconds, doubled per retry
USER_AGENT = "devxdk-version-currency (+https://github.com/devxdk/devxdk)"


class CheckError(RuntimeError):
    """A fault in the inventory itself (as opposed to a drifted pin)."""


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def load_inventory(path: pathlib.Path) -> dict:
    # tomllib.loads takes str, not bytes - passing read_bytes() raises TypeError.
    # This is config.py:130 verbatim, deliberately: the repo already parses TOML
    # exactly this way and there is no reason for a second spelling.
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CheckError(f"inventory not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise CheckError(f"{path}: invalid TOML: {e}") from None
    if raw.get("schema") != SCHEMA_VERSION:
        raise CheckError(f"{path}: schema = {raw.get('schema')!r}, want {SCHEMA_VERSION}")
    if not isinstance(raw.get("rows"), list) or not raw["rows"]:
        raise CheckError(f"{path}: no [[rows]]")
    return raw


VALID_EXCEPTION = {"hold", "time-bounded"}
VALID_CURRENCY_API = {"go-proxy", "npm", "github-release", "go-dl",
                      "python-versions", "runner-image", "rolling-asset", "none"}
VALID_CURRENCY_POLICY = {"latest", "series", "series-track", "report-only", "never"}


def validate_inventory(inv: dict, path: pathlib.Path) -> None:
    """Structural validation of the inventory itself. Fails closed."""
    seen = set()
    for row in inv["rows"]:
        rid = row.get("id")
        if not isinstance(rid, str) or not rid:
            raise CheckError(f"{path}: a row has no id")
        if rid in seen:
            raise CheckError(f"{path}: duplicate row id {rid!r}")
        seen.add(rid)
        if not row.get("why"):
            raise CheckError(f"{path}: row {rid!r}: no `why` (every row states why it is inventoried)")
        checks = row.get("checks")
        if not isinstance(checks, list) or not checks:
            raise CheckError(f"{path}: row {rid!r}: no checks")
        for c in checks:
            kind = c.get("kind")
            if kind not in {"scan", "file"}:
                raise CheckError(f"{path}: row {rid!r}: check kind {kind!r}")
            if kind == "file" and not (c.get("file") and c.get("regex")):
                raise CheckError(f"{path}: row {rid!r}: file check needs file + regex")
            if kind == "scan" and not c.get("select_kind"):
                raise CheckError(f"{path}: row {rid!r}: scan check needs select_kind")
            if "assert_value" in c and not isinstance(c["assert_value"], bool):
                raise CheckError(f"{path}: row {rid!r}: assert_value must be a bool")

        # -- the exception model: exactly one of condition / expires_on -------
        exc = row.get("exception")
        if exc is not None:
            if exc not in VALID_EXCEPTION:
                raise CheckError(f"{path}: row {rid!r}: exception = {exc!r}, "
                                 f"want one of {sorted(VALID_EXCEPTION)}")
            has_cond = bool(row.get("condition"))
            has_date = bool(row.get("expires_on"))
            if has_cond and has_date:
                raise CheckError(f"{path}: row {rid!r}: an excepted row carries a condition "
                                 f"OR an expires_on, never both")
            if not has_cond and not has_date:
                raise CheckError(f"{path}: row {rid!r}: an excepted row must carry a testable "
                                 f"condition (hold) or an expires_on (time-bounded)")
            if exc == "hold" and not has_cond:
                raise CheckError(f"{path}: row {rid!r}: a hold carries a condition, not a date")
            if exc == "time-bounded" and not has_date:
                raise CheckError(f"{path}: row {rid!r}: a time-bounded exception carries an "
                                 f"expires_on, not a condition")
            if has_date:
                _parse_date(row["expires_on"], rid, path)
        else:
            if row.get("condition") or row.get("expires_on"):
                raise CheckError(f"{path}: row {rid!r}: condition/expires_on on a row with no "
                                 f"`exception` - say which kind of exception it is")

        cur = row.get("currency")
        if cur is not None:
            api = cur.get("api")
            pol = cur.get("policy")
            if api not in VALID_CURRENCY_API:
                raise CheckError(f"{path}: row {rid!r}: currency.api = {api!r}")
            if pol not in VALID_CURRENCY_POLICY:
                raise CheckError(f"{path}: row {rid!r}: currency.policy = {pol!r}")
            if api not in {"none", "go-dl"} and not cur.get("source"):
                raise CheckError(f"{path}: row {rid!r}: currency needs a source")
            if pol in {"series", "series-track"} and not cur.get("series"):
                raise CheckError(f"{path}: row {rid!r}: policy {pol!r} needs a series")
            if api == "runner-image" and not cur.get("tag_prefix"):
                raise CheckError(f"{path}: row {rid!r}: runner-image needs a tag_prefix")
            if api == "rolling-asset" and not (cur.get("tag") and cur.get("asset")):
                raise CheckError(f"{path}: row {rid!r}: rolling-asset needs tag + asset")
            if api in {"runner-image", "rolling-asset"} and pol != "report-only":
                raise CheckError(f"{path}: row {rid!r}: {api} is informational - its policy "
                                 f"must be report-only, never a failing comparison")


def _parse_date(value, rid, path) -> datetime.date:
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise CheckError(f"{path}: row {rid!r}: expires_on {value!r} is not an ISO date") from None


# ---------------------------------------------------------------------------
# The reverse scan
# ---------------------------------------------------------------------------

class Hit:
    __slots__ = ("kind", "identity", "value", "file", "detail")

    def __init__(self, kind, identity, value, file, detail=""):
        self.kind = kind
        self.identity = identity
        self.value = value
        self.file = file
        self.detail = detail

    def __repr__(self):  # pragma: no cover - diagnostics only
        return f"Hit({self.kind}, {self.identity!r}, {self.value!r}, {self.file})"

    def label(self):
        ident = f" {self.identity}" if self.identity else ""
        return f"{self.kind}{ident} = {self.value!r} ({self.file})"


# `uses:` is scanned BY KEY. Matching `uses: ...@<40-hex>` instead would only
# recognize actions that are ALREADY correct - `vendor/action@v1`, the mutable
# tag the repo forbids, would match nothing, need no row, and sail through. The
# one case the scan exists to catch is the one such a regex cannot see.
RE_GO_TOOL = re.compile(
    r"\bgo\s+(?:install|run)\s+"
    r"(?P<mod>[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}/[^\s@'\"`]+)"
    # The ref alternation puts the two INDIRECTED forms first, because a
    # ${{ ... }} expression contains spaces: a bare character class stops at the
    # first one, captures "${{", and then classifies an ordinary indirection as
    # a forbidden floating ref.
    r"@(?P<ref>\$\{\{[^}]*\}\}|\$\{[^}]*\}|[^\s'\"`)\\;&|]+)")
RE_GO_DIRECTIVE = re.compile(r"^go\s+(\S+)\s*$")
RE_TOOLCHAIN = re.compile(r"^toolchain\s+(\S+)\s*$")
RE_DOCKER_FROM = re.compile(r"^\s*FROM\s+(\S+?):(\S+)")
RE_SHA256_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(\S+)\s*$")

# YAML keys that carry a pin, and the Hit kind each produces. Scanned by KEY.
# Matching `uses: ...@<40-hex>` instead would only recognize actions that are
# ALREADY correct - `vendor/action@v1`, the mutable tag the repo forbids, would
# match nothing, need no inventory row, and sail through. The one case the scan
# exists to catch is the one such a regex cannot see.
YAML_PIN_KEYS = (
    ("uses", "uses"),
    ("go-version", "go-version"),
    ("node-version", "node-version"),
    ("python-version", "python-version"),
    ("version", "version-input"),
    ("runner", "runner"),
    ("runs-on", "runs-on"),
)

# A key is a real key when it opens the line or follows `{` or `,` - which is
# what makes FLOW mappings visible. release.yml writes
# `with: { node-version: "24" }` on one line, and a line-anchored `^\s*key:`
# sees none of those: three node-version sites and three go-version sites were
# invisible until this was widened, in a repo whose inventory claims seven.
# The lookbehind also stops `cache-dependency-path:` matching the `version` key.
_KEY_RE_CACHE = {}


def _key_regex(key):
    rx = _KEY_RE_CACHE.get(key)
    if rx is None:
        rx = re.compile(r"(?:^|[{,])\s*-?\s*" + re.escape(key) + r":")
        _KEY_RE_CACHE[key] = rx
    return rx


def yaml_scalars(line, key):
    """Every scalar this line assigns to `key`, block or flow form."""
    out = []
    for m in _key_regex(key).finditer(line):
        val = _scalar_at(line, m.end())
        if val is not None:
            out.append(val)
    return out


def _scalar_at(line, i):
    """The scalar starting at index i, quotes and flow/comment terminators
    removed. Written out rather than folded into each regex because the two
    shapes disagree: a character-class capture stops at the first space, which
    silently truncates `runs-on: ${{ inputs.runner }}` to `${{` and would then
    classify an ordinary indirection as an unrecognised literal."""
    n = len(line)
    while i < n and line[i] in " \t":
        i += 1
    if i >= n:
        return None
    if line[i] in "\"'":
        q = line[i]
        end = line.find(q, i + 1)
        return line[i + 1:end] if end > 0 else None
    # Unquoted: a ${{ ... }} expression may contain spaces and must survive
    # whole, so consume it as a unit before applying the ordinary terminators.
    out = []
    depth = 0
    while i < n:
        ch = line[i]
        if line.startswith("${{", i):
            depth += 1
            out.append("${{")
            i += 3
            continue
        if depth and line.startswith("}}", i):
            depth -= 1
            out.append("}}")
            i += 2
            continue
        if not depth:
            if ch in ",}":
                break
            if ch == "#" and out and out[-1].endswith(" "):
                break
        out.append(ch)
        i += 1
    val = "".join(out).strip()
    return val or None


RE_SEMVER_REF = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")
RE_SHA40 = re.compile(r"^[0-9a-f]{40}$")
RE_INDIRECT = re.compile(r"\$\{\{?[^}]*\}?\}")

YAML_SUFFIXES = {".yml", ".yaml"}


def scan_tree(root: pathlib.Path, scan_cfg: dict, errors: list) -> list:
    """Walk the tree and return every pin-bearing construct as a Hit.

    Hard failures (a floating Go ref, a non-SHA `uses:`) are appended to
    ``errors`` here rather than becoming Hits: they are forbidden outright, not
    merely uninventoried, so no inventory row could ever excuse them.
    """
    include = scan_cfg.get("include_globs") or []
    exclude_dirs = set(scan_cfg.get("exclude_dirs") or [])
    digest_globs = scan_cfg.get("digest_globs") or []

    hits: list[Hit] = []
    files: list[pathlib.Path] = []
    for pattern in include:
        for p in sorted(root.glob(pattern)):
            if not p.is_file():
                continue
            rel = p.relative_to(root)
            if any(part in exclude_dirs for part in rel.parts[:-1]):
                continue
            if p not in files:
                files.append(p)

    for path in files:
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        is_yaml = path.suffix in YAML_SUFFIXES
        is_gomod = path.name == "go.mod"

        for line in text.splitlines():
            stripped = line.strip()

            if is_yaml:
                for key, kind in YAML_PIN_KEYS:
                    for val in yaml_scalars(line, key):
                        if key == "uses":
                            if val.startswith("."):
                                hits.append(Hit("uses-local", val, "", rel))
                            elif "@" in val:
                                ident, _, sha = val.rpartition("@")
                                if not RE_SHA40.match(sha):
                                    errors.append(
                                        f"{rel}: uses: {val} - every non-local action must be "
                                        f"pinned to a full 40-hex commit (the policy stated at "
                                        f"ci.yml's header, which until now nothing enforced)")
                                else:
                                    hits.append(Hit("uses", ident, sha, rel))
                            else:
                                errors.append(f"{rel}: uses: {val} - no ref at all")
                        elif key == "runs-on":
                            k = "runs-on-indirect" if RE_INDIRECT.search(val) else "runs-on"
                            hits.append(Hit(k, "", val, rel))
                        else:
                            hits.append(Hit(kind, "", val, rel))

            if is_gomod:
                m = RE_GO_DIRECTIVE.match(stripped)
                if m:
                    hits.append(Hit("go-directive", rel, m.group(1), rel))
                m = RE_TOOLCHAIN.match(stripped)
                if m:
                    hits.append(Hit("toolchain", rel, m.group(1), rel))

            m = RE_DOCKER_FROM.match(line)
            if m and path.name.startswith("Dockerfile"):
                hits.append(Hit("docker-from", m.group(1), m.group(2), rel))

            # Go tool refs anywhere: `go install` / `go run` in workflows,
            # Taskfiles, shell scripts and docs alike. Four forms this tree uses
            # today that a literal-version regex sees NONE of: `go run`, a shell
            # variable, a ${{ }} expression, and a floating `@latest`.
            for m in RE_GO_TOOL.finditer(line):
                mod, ref = m.group("mod"), m.group("ref")
                if RE_INDIRECT.search(ref):
                    # Indirected: the row must name the DEFINITION site. That is
                    # self-enforcing - a row pointed at this consumer line would
                    # fail its own forward check, because the literal is not here.
                    hits.append(Hit("go-tool-indirect", mod, ref, rel))
                elif RE_SEMVER_REF.match(ref) or RE_SHA40.match(ref):
                    # A full 40-hex commit is IMMUTABLE, so it is not floating.
                    # Rejecting it while ci.yml requires 40-hex for every `uses:`
                    # would have the gate forbid the exact pinning discipline the
                    # repo mandates one line away.
                    hits.append(Hit("go-tool", mod, ref, rel))
                else:
                    errors.append(
                        f"{rel}: go install/run {mod}@{ref} - a floating ref "
                        f"(latest, a branch, a non-version tag, or a SHORTENED hash) is "
                        f"forbidden: pin a released version or a full 40-hex commit")

    # Digest files are pins too: version-in-URL plus a committed hash, moving as
    # one unit (the NSIS / reference-minisign shape).
    for pattern in digest_globs:
        for p in sorted(root.glob(pattern)):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            for line in p.read_text(encoding="utf-8").splitlines():
                m = RE_SHA256_LINE.match(line.strip())
                if m:
                    hits.append(Hit("digest-file", rel, m.group(1), rel, detail=m.group(2)))
    return hits


# -- the structured [pins.*] walk -------------------------------------------

PIN_FIELDS = ("version", "ref", "fingerprints", "file")
PIN_FIELD_PREFIXES = ("sha256",)


def scan_pins(root: pathlib.Path, pins_file: str) -> list:
    """Walk every [pins.*] table, nested ones included, and emit one Hit per
    tracked field.

    A regex over TOML would miss a nested table - `[pins.php_redis.dll."8.5"]`
    is exactly that shape - which is the one thing this walk exists for.
    """
    path = root / pins_file
    if not path.exists():
        return []
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    pins = raw.get("pins")
    if not isinstance(pins, dict):
        return []
    hits: list[Hit] = []

    def walk(table, prefix):
        for key, val in table.items():
            dotted = f"{prefix}.{key}"
            if isinstance(val, dict):
                walk(val, dotted)
                continue
            tracked = key in PIN_FIELDS or any(key.startswith(p) for p in PIN_FIELD_PREFIXES)
            if not tracked:
                continue
            if isinstance(val, list):
                shown = ",".join(str(v) for v in val)
            else:
                shown = str(val)
            hits.append(Hit("toml-pin", dotted, shown, pins_file))

    walk(pins, "pins")
    return hits


# ---------------------------------------------------------------------------
# Selector matching
# ---------------------------------------------------------------------------

def selector_matches(sel: dict, hit: Hit) -> bool:
    if sel.get("select_kind") != hit.kind:
        return False
    ident = sel.get("select_identity")
    if ident is not None:
        # toml-pin identities are dotted paths: a selector for `pins.php_redis`
        # covers every field of that table and of its nested tables.
        if not (hit.identity == ident or hit.identity.startswith(ident + ".")):
            return False
    values = sel.get("select_values")
    if values is not None and hit.value not in values:
        return False
    return True


def allowlisted(allow: list, hit: Hit) -> bool:
    return any(selector_matches(entry, hit) for entry in allow)


# ---------------------------------------------------------------------------
# consistency
# ---------------------------------------------------------------------------

def run_consistency(root: pathlib.Path, inv: dict, inv_path: pathlib.Path, today) -> list:
    errors: list[str] = []
    scan_cfg = inv.get("scan") or {}
    allow = inv.get("allowlist") or []

    hits = scan_tree(root, scan_cfg, errors)
    pins_file = scan_cfg.get("pins_file")
    if pins_file:
        hits.extend(scan_pins(root, pins_file))

    claimed: list[bool] = [False] * len(hits)

    # -- FORWARD ------------------------------------------------------------
    for row in inv["rows"]:
        rid = row["id"]
        row_value = row.get("value")
        for c in row.get("checks", []):
            expected = c.get("value", row_value)
            if c["kind"] == "scan":
                matched = [i for i, h in enumerate(hits) if selector_matches(c, h)]
                for i in matched:
                    claimed[i] = True
                count = c.get("count")
                if count is not None and len(matched) != count:
                    errors.append(
                        f"row {rid}: expected {count} site(s) of "
                        f"{c['select_kind']} {c.get('select_identity', '')}".rstrip() +
                        f", found {len(matched)}" +
                        (": " + ", ".join(sorted({hits[i].file for i in matched}))
                         if matched else ""))
                elif count is None and not matched:
                    errors.append(f"row {rid}: no site of {c['select_kind']} "
                                  f"{c.get('select_identity', '')} found at all".rstrip())
                if expected is not None and c.get("assert_value", True):
                    for i in matched:
                        if hits[i].value != expected:
                            errors.append(
                                f"row {rid}: {hits[i].file} carries "
                                f"{hits[i].value!r}, inventory says {expected!r} "
                                f" - a PARTIAL bump is exactly what this row catches")
            else:
                fpath = root / c["file"]
                if not fpath.exists():
                    errors.append(f"row {rid}: {c['file']} does not exist")
                    continue
                try:
                    rx = re.compile(c["regex"], re.M)
                except re.error as e:
                    raise CheckError(f"row {rid}: bad regex {c['regex']!r}: {e}") from None
                found = [m.group(1) if rx.groups else m.group(0)
                         for m in rx.finditer(fpath.read_text(encoding="utf-8"))]
                count = c.get("count")
                if count is not None and len(found) != count:
                    errors.append(f"row {rid}: {c['file']}: expected {count} match(es) of "
                                  f"{c['regex']!r}, found {len(found)}")
                elif count is None and not found:
                    errors.append(f"row {rid}: {c['file']}: no match for {c['regex']!r}")
                if expected is not None and c.get("assert_value", True):
                    for got in found:
                        if got != expected:
                            errors.append(f"row {rid}: {c['file']} carries {got!r}, "
                                          f"inventory says {expected!r}")

        # -- the exception model, enforced ---------------------------------
        if row.get("exception") == "time-bounded":
            due = _parse_date(row["expires_on"], rid, inv_path)
            if today > due:
                errors.append(
                    f"row {rid}: time-bounded exception EXPIRED on {due.isoformat()} "
                    f"({row.get('expires_target') or row.get('why')}). This is the gate "
                    f"working: a date was chosen precisely because the clearing condition "
                    f"is our own effort and would otherwise stay false forever. Do the "
                    f"work or take a new, argued date - do not simply move this one.")

    # -- REVERSE ------------------------------------------------------------
    for i, hit in enumerate(hits):
        if claimed[i]:
            continue
        if allowlisted(allow, hit):
            continue
        errors.append(
            f"UNINVENTORIED PIN {hit.label()} - add a row to "
            f"{inv_path.name}, or allowlist it as deliberately floating "
            f"(a floating runner label or a local ./ composite action). A pin "
            f"with no row is a pin this gate cannot check.")
    return errors


# ---------------------------------------------------------------------------
# currency
# ---------------------------------------------------------------------------

def _http_get(url: str, headers: dict | None = None) -> bytes:
    """GET with the module-level timeout / retry / backoff policy.

    Raises on exhaustion. Every caller lets that propagate: a network or parse
    failure FAILS the scheduled job.
    """
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    delay = HTTP_BACKOFF
    last: Exception | None = None
    for attempt in range(HTTP_RETRIES):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310
                return resp.read()
        except urllib.error.HTTPError as e:
            # 403/429 from api.github.com is the documented rate-limit shape.
            # Honour Retry-After when offered, otherwise back off and retry; a
            # 404 is a real answer, not a transient, so stop immediately.
            if e.code == 404:
                raise
            last = e
            wait = delay
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if retry_after and retry_after.isdigit():
                wait = min(int(retry_after), 120)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            wait = delay
        if attempt < HTTP_RETRIES - 1:
            time.sleep(wait)
            delay *= 2
    raise CheckError(f"GET {url}: {last}")


def _json_get(url: str, headers: dict | None = None):
    raw = _http_get(url, headers)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        # A malformed response FAILS. Never treat unparseable as "current".
        raise CheckError(f"GET {url}: malformed response: {e}") from None


def _escape_module(path: str) -> str:
    return "".join("!" + c.lower() if c.isupper() else c for c in path)


RE_SEMVER = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?(?:\+[0-9A-Za-z.\-]+)?$")


def parse_version(v: str):
    m = RE_SEMVER.match(v.strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    return (int(major), int(minor), int(patch), pre)


def version_key(v: str):
    parsed = parse_version(v)
    if parsed is None:
        return None
    major, minor, patch, pre = parsed
    # A release sorts ABOVE a prerelease of the same triple.
    return (major, minor, patch, 0 if pre else 1, pre or "")


RE_RETRACT_BLOCK = re.compile(r"retract\s*\(([^)]*)\)", re.S)
RE_RETRACT_LINE = re.compile(r"^\s*retract\s+(?!\()(.+)$", re.M)


def parse_retractions(modtext: str) -> list:
    """Every retract directive, singles and [lo, hi] ranges alike."""
    bodies: list[str] = []
    for m in RE_RETRACT_BLOCK.finditer(modtext):
        bodies.extend(m.group(1).splitlines())
    for m in RE_RETRACT_LINE.finditer(modtext):
        bodies.append(m.group(1))
    ranges = []
    for body in bodies:
        body = body.split("//")[0].strip()
        if not body:
            continue
        if body.startswith("["):
            parts = [p.strip() for p in body.strip("[]").split(",")]
            if len(parts) == 2:
                ranges.append((parts[0], parts[1]))
        else:
            ranges.append((body.split()[0], body.split()[0]))
    return ranges


def go_latest(module: str) -> str:
    """Max stable version over @v/list, with retractions honoured.

    @v/list, NEVER @latest. Verified, not assumed: for cyclonedx-gomod the
    @latest endpoint reports a version two releases behind what @v/list lists,
    so a gate keyed on @latest would both miss a real bump and then, once the
    bump landed, report the pin as AHEAD of latest and go red on a correct tree.
    The module reference says @latest is an optional fallback consulted only
    "if no suitable versions are found" - it is not the source of truth.

    Retractions are read from the HIGHEST version's go.mod, never from each
    version's own: a module author retracts by publishing a NEW, higher version
    carrying the directive, so a per-version check would never fire. That
    highest version may retract ITSELF, so it is a candidate for exclusion too.
    """
    esc = _escape_module(module)
    raw = _http_get(f"https://proxy.golang.org/{esc}/@v/list").decode("utf-8", "replace")
    listed = [v.strip() for v in raw.splitlines() if v.strip()]
    parseable = [v for v in listed if version_key(v) is not None]
    if not parseable:
        raise CheckError(f"{module}: @v/list returned no parseable versions")
    highest = max(parseable, key=version_key)
    modtext = _http_get(
        f"https://proxy.golang.org/{esc}/@v/{highest}.mod").decode("utf-8", "replace")
    ranges = parse_retractions(modtext)

    def retracted(v: str) -> bool:
        for lo, hi in ranges:
            klo, khi, kv = version_key(lo), version_key(hi), version_key(v)
            if klo is None or khi is None or kv is None:
                continue
            if klo <= kv <= khi:
                return True
        return False

    stable = [v for v in parseable if parse_version(v)[3] is None and not retracted(v)]
    if not stable:
        raise CheckError(f"{module}: every listed version is a prerelease or retracted")
    return max(stable, key=version_key)


def npm_latest(pkg: str) -> str:
    doc = _json_get(f"https://registry.npmjs.org/{pkg.replace('/', '%2f')}/latest")
    v = doc.get("version")
    if not isinstance(v, str):
        raise CheckError(f"npm {pkg}: latest has no version field")
    return v


def _gh_headers(token: str | None) -> dict:
    h = {"Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def gh_latest_release(repo: str, token: str | None) -> str:
    # Unauthenticated is 60 req/hr per IP; the workflows pass GITHUB_TOKEN, and
    # the retry/backoff above covers the unauthenticated fallback.
    doc = _json_get(f"https://api.github.com/repos/{repo}/releases/latest",
                    _gh_headers(token))
    tag = doc.get("tag_name")
    if not isinstance(tag, str):
        raise CheckError(f"github {repo}: latest release has no tag_name")
    return tag


def gh_tag_commit(repo: str, tag: str, token: str | None) -> str:
    doc = _json_get(f"https://api.github.com/repos/{repo}/git/ref/tags/{tag}",
                    _gh_headers(token))
    obj = doc.get("object") or {}
    if obj.get("type") == "tag":
        doc = _json_get(f"https://api.github.com/repos/{repo}/git/tags/{obj['sha']}",
                        _gh_headers(token))
        obj = doc.get("object") or {}
    sha = obj.get("sha")
    if not isinstance(sha, str):
        raise CheckError(f"github {repo}: cannot resolve tag {tag} to a commit")
    return sha


def setup_python_latest() -> str:
    """Newest stable interpreter `actions/setup-python` can resolve.

    setup-python's OWN versions manifest is the authority for a `python-version:`
    input - it is literally what the action resolves against - so a row for that
    pin names it rather than a general "latest Python" feed that could offer a
    build no runner has.
    """
    doc = _json_get("https://raw.githubusercontent.com/actions/python-versions/"
                    "main/versions-manifest.json")
    best = None
    for rel in doc:
        v = rel.get("version")
        if not rel.get("stable") or not isinstance(v, str):
            continue
        k = version_key(v)
        if k and (best is None or k > best[0]):
            best = (k, v)
    if best is None:
        raise CheckError("actions/python-versions: no stable version in the manifest")
    return best[1]


def runner_image_latest(tag_prefix: str, token: str | None) -> str:
    """Newest actions/runner-images release whose tag carries this prefix.

    INFORMATIONAL ONLY. A dated runner label is a deliberate compatibility pin
    (the app repo's ubuntu-22.04 is its glibc 2.35 baseline; the manifest repo's
    four labels fix the build environment for reproducible bundles), so the gate
    reports what shipped and never demands the move.
    """
    doc = _json_get(f"https://api.github.com/repos/actions/runner-images/"
                    f"releases?per_page=100", _gh_headers(token))
    tags = [r.get("tag_name") for r in doc
            if isinstance(r.get("tag_name"), str) and r["tag_name"].startswith(tag_prefix)]
    if not tags:
        return "(no release with that prefix in the latest 100)"
    return max(tags)


def rolling_asset_mtime(repo: str, tag: str, asset: str, token: str | None) -> str:
    """updated_at of one asset on a rolling release tag.

    For upstreams that publish ONLY a rolling build (linuxdeploy's `continuous`),
    where the local pin is a hand-refreshed digest. The gate reports when the
    asset moved; it never proposes the new digest, because accepting a digest a
    scanner fetched would defeat the point of committing one.
    """
    doc = _json_get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
                    _gh_headers(token))
    for a in doc.get("assets") or []:
        if a.get("name") == asset:
            return str(a.get("updated_at"))
    raise CheckError(f"{repo}@{tag}: no asset named {asset}")


def go_dl_stable() -> str:
    doc = _json_get("https://go.dev/dl/?mode=json")
    stable = [r["version"] for r in doc if r.get("stable") and isinstance(r.get("version"), str)]
    best = None
    for v in stable:
        k = version_key(v.removeprefix("go"))
        if k and (best is None or k > best[0]):
            best = (k, v.removeprefix("go"))
    if best is None:
        raise CheckError("go.dev/dl: no stable release found")
    return best[1]


def run_currency(inv: dict, token: str | None) -> tuple:
    """Returns (failures, reports). A report never fails the job."""
    failures: list[str] = []
    reports: list[str] = []

    for row in inv["rows"]:
        cur = row.get("currency")
        if not cur:
            continue
        rid = row["id"]
        api, policy, source = cur["api"], cur["policy"], cur.get("source")
        if policy == "never" or api == "none":
            continue
        pinned = cur.get("compare_value", row.get("version", row.get("value")))
        if pinned is None:
            failures.append(f"row {rid}: currency configured but the row has no value")
            continue

        if api == "runner-image":
            newest = runner_image_latest(cur["tag_prefix"], token)
            reports.append(f"row {rid}: pinned {pinned}; newest actions/runner-images "
                           f"release for {cur['tag_prefix']} is {newest} "
                           f"({row.get('why', '')})")
            continue
        if api == "rolling-asset":
            moved = rolling_asset_mtime(source, cur["tag"], cur["asset"], token)
            reports.append(f"row {rid}: upstream {cur['asset']} on {source}@{cur['tag']} "
                           f"was last updated {moved}; the committed digest was refreshed "
                           f"{cur.get('refreshed_on', '(unrecorded)')}. Refresh by REVIEW - "
                           f"this gate never proposes a digest it fetched.")
            continue

        if api == "go-proxy":
            latest = go_latest(source)
        elif api == "npm":
            latest = npm_latest(source)
        elif api == "github-release":
            latest = gh_latest_release(source, token)
        elif api == "go-dl":
            latest = go_dl_stable()
        elif api == "python-versions":
            latest = setup_python_latest()
        else:  # pragma: no cover - validate_inventory rejects this
            failures.append(f"row {rid}: unknown api {api!r}")
            continue

        if policy == "series-track":
            # The pin IS a series (e.g. a setup-python "3.14" input): it must
            # name the current stable series, and it FAILS when upstream moves.
            parsed = parse_version(str(latest))
            if parsed is None:
                failures.append(f"row {rid}: cannot parse upstream {latest!r} as a version")
                continue
            upstream_series = f"{parsed[0]}.{parsed[1]}"
            if str(pinned) != upstream_series:
                failures.append(f"row {rid}: pinned series {pinned}, current stable series "
                                f"{upstream_series} (newest {latest})")
            continue

        if policy == "series":
            series = str(cur["series"])
            if not str(latest).lstrip("v").startswith(series + "."):
                # A newer SERIES is REPORTED, never failed. Changing series is a
                # build decision (ABI, configure flags), not a currency bump, and
                # this gate must not be able to force it - a gate that is red on
                # arrival gets its row weakened, which is how the mechanism decays.
                reports.append(f"row {rid}: newest upstream is {latest} - a newer series than "
                               f"the reviewed {series} line ({cur.get('series_note', '')})".rstrip())
                continue

        same = str(pinned).lstrip("v") == str(latest).lstrip("v")
        if policy == "report-only":
            if not same:
                reports.append(f"row {rid}: pinned {pinned}, upstream {latest} "
                               f"({row.get('why', '')})")
            continue
        if not same:
            failures.append(f"row {rid}: pinned {pinned}, latest {latest} "
                            f"({api} {source})")
            continue

        # A GitHub Action row also proves its SHA still names the tag it claims:
        # a correct-looking version comment beside a wrong commit is the failure
        # a version-only comparison cannot see.
        if api == "github-release" and cur.get("verify_tag_sha") and row.get("value"):
            sha = gh_tag_commit(source, str(latest), token)
            if sha != row["value"]:
                failures.append(f"row {rid}: pinned SHA {row['value']} but {source}@{latest} "
                                f"is {sha}")

    # Expiry rows: test each exception's CLEARING condition, and fail when a hold
    # goes stale - i.e. when its dependabot ignore could be removed. Without
    # these a documented exception silently becomes a permanent one.
    for row in inv["rows"]:
        clear = row.get("clears_when")
        if not clear:
            continue
        rid = row["id"]
        if clear.get("api") != "npm-peer":
            failures.append(f"row {rid}: unknown clears_when.api {clear.get('api')!r}")
            continue
        doc = _json_get(f"https://registry.npmjs.org/{clear['package'].replace('/', '%2f')}/latest")
        peers = doc.get("peerDependencies") or {}
        got = peers.get(clear["peer"])
        if got is None:
            failures.append(f"row {rid}: {clear['package']} no longer declares a peer on "
                            f"{clear['peer']} - the hold's premise is gone, re-argue the row")
        elif got != clear["range"]:
            failures.append(
                f"row {rid}: HOLD IS STALE - {clear['package']} now declares "
                f"{clear['peer']} {got!r} (was {clear['range']!r}). Re-check whether the "
                f"hold can be lifted and its dependabot ignore removed.")
    return failures, reports


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="DevXDK version-currency gate.")
    ap.add_argument("--inventory", default=".github/versions-inventory.toml")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--mode", choices=("consistency", "currency"), required=True)
    ap.add_argument("--today", help="ISO date override, for tests")
    ap.add_argument("--github-token", default=None)
    args = ap.parse_args(argv)
    # Prefer the environment: a token on the command line lands in the echoed
    # step command, which is a public log for this repo.
    token = args.github_token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    root = pathlib.Path(args.repo_root).resolve()
    inv_path = (root / args.inventory).resolve()

    try:
        inv = load_inventory(inv_path)
        validate_inventory(inv, inv_path)
        if args.mode == "consistency":
            today = (datetime.date.fromisoformat(args.today) if args.today
                     else datetime.date.today())
            problems = run_consistency(root, inv, inv_path, today)
            reports: list[str] = []
        else:
            problems, reports = run_currency(inv, token)
    except CheckError as e:
        sys.stderr.write(f"check_versions: FAILED {e}\n")
        return 1

    for r in reports:
        sys.stderr.write(f"check_versions: REPORT {r}\n")
    for p in problems:
        sys.stderr.write(f"check_versions: {p}\n")
    if problems:
        sys.stderr.write(f"check_versions: FAILED - {len(problems)} problem(s)\n")
        return 1
    sys.stderr.write(
        f"check_versions: OK - {len(inv['rows'])} row(s), mode {args.mode}"
        + (f", {len(reports)} report(s)" if reports else "") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
