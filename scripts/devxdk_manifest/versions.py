"""Version parsing and the product-specific natural-order comparator.

This is a faithful Python port of the Go comparator in the app repo's
``internal/version`` package. The two implementations are pinned together by the
shared test-vector list in ``testdata/version-vectors.json`` (vendored
byte-identically from ``internal/version/testdata/version-vectors.json``): any
disagreement fails the version-vector test on either side, so the manifest
pipeline can never sort or de-duplicate a release differently than the client.

Three comparison domains sit on top of ONE natural-order primitive
(``compare_natural``):

  * ``compare`` — the lenient release comparator (``parse`` + precedence). Used
    to sort a component's releases and to run the manifest alias guard.
  * ``compare_provider_key`` — opaque provider ordering keys (adopted-asset
    source versions like ``17.4-2``): split on dots AND dashes, digit runs
    compared numerically, never parsed as a release identity.

Standard library only.
"""

from __future__ import annotations


def _cmp_int(a: int, b: int) -> int:
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def _str_compare(a: str, b: str) -> int:
    # Mirrors Go's strings.Compare (byte order). The identifiers this touches are
    # lowercased ASCII, for which Python's str ordering matches byte ordering.
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


class Version:
    """A parsed dotted version with an optional prerelease tag."""

    __slots__ = ("major", "minor", "patch", "prerelease", "raw")

    def __init__(self, major: int, minor: int, patch: int, prerelease: str, raw: str):
        self.major = major
        self.minor = minor
        self.patch = patch
        self.prerelease = prerelease
        self.raw = raw

    def is_prerelease(self) -> bool:
        return self.prerelease != ""

    def major_string(self) -> str:
        return str(self.major)

    def major_minor_string(self) -> str:
        return f"{self.major}.{self.minor}"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Version({self.raw!r})"


class ParseError(ValueError):
    """Raised when a string is not a parseable dotted version."""


def parse(s: str) -> Version:
    """Parse ``MAJOR[.MINOR[.PATCH]][-PRERELEASE]``.

    A leading ``v`` or ``go`` is tolerated; missing minor/patch default to 0;
    build metadata (``+...``) is discarded. Deliberately lenient — identity
    decisions run through the strict validators, not this parser.
    """
    raw = s
    t = s.strip()
    if t.startswith("v"):
        t = t[1:]
    if t.startswith("go"):
        t = t[2:]

    core = t
    pre = ""
    # Split '+' off FIRST (it follows the prerelease) so "1.2.3-rc.1+build"
    # parses with prerelease "rc.1", not "rc.1+build".
    if "+" in core:
        core = core.split("+", 1)[0]
    if "-" in core:
        core, pre = core.split("-", 1)

    parts = core.split(".")
    if len(parts) > 3:
        raise ParseError(f'version "{raw}": want 1-3 dotted components')

    nums = [0, 0, 0]
    last = len(parts) - 1
    for i, p in enumerate(parts):
        split = len(p)
        for j in range(len(p)):
            if not ("0" <= p[j] <= "9"):
                split = j
                break
        if split == 0:
            raise ParseError(f'version "{raw}": non-numeric component "{p}"')
        # A trailing non-numeric run is a prerelease only on the LAST component
        # ("8.6.0RC9"); on an earlier component it is malformed ("8a.5.6").
        if split < len(p) and i != last:
            raise ParseError(f'version "{raw}": non-numeric component "{p}"')
        nums[i] = int(p[:split])
        if split < len(p) and pre == "":
            pre = p[split:]

    return Version(nums[0], nums[1], nums[2], pre, raw)


def try_parse(s: str):
    """parse() returning None instead of raising — for lenient filters."""
    try:
        return parse(s)
    except ParseError:
        return None


def compare(v: Version, o: Version) -> int:
    """Order two versions by precedence (-1/0/+1).

    A stable release outranks the same core with a prerelease tag; prerelease
    tags order under the natural-order rules documented on the module.
    """
    c = _cmp_int(v.major, o.major)
    if c:
        return c
    c = _cmp_int(v.minor, o.minor)
    if c:
        return c
    c = _cmp_int(v.patch, o.patch)
    if c:
        return c
    if v.prerelease == "" and o.prerelease == "":
        return 0
    if v.prerelease == "":
        return 1  # stable > prerelease
    if o.prerelease == "":
        return -1
    return _compare_prerelease(v.prerelease, o.prerelease)


def compare_str(a: str, b: str) -> int:
    """compare() over raw strings (parsing both). Raises on an unparseable side."""
    return compare(parse(a), parse(b))


def _compare_prerelease(a: str, b: str) -> int:
    ai = a.lower().split(".")
    bi = b.lower().split(".")
    for i in range(min(len(ai), len(bi))):
        c = compare_natural(ai[i], bi[i])
        if c:
            return c
    return _cmp_int(len(ai), len(bi))  # fewer identifiers sort first


def compare_natural(a: str, b: str) -> int:
    """Compare one identifier pair, each maximal digit run as a number
    ("rc10" > "rc9"; leading zeros compare numerically equal) and non-digit
    runs byte-wise."""
    while a != "" and b != "":
        ar, arest, adig = _split_run(a)
        br, brest, bdig = _split_run(b)
        if adig and bdig:
            at, bt = ar.lstrip("0"), br.lstrip("0")
            c = _cmp_int(len(at), len(bt))
            if c:
                return c
            c = _str_compare(at, bt)
            if c:
                return c
        else:
            c = _str_compare(ar, br)
            if c:
                return c
        a, b = arest, brest
    return _cmp_int(len(a), len(b))  # the shorter identifier sorts first


def _split_run(s: str):
    """Cut the leading maximal all-digit or no-digit run off s."""
    is_digit = "0" <= s[0] <= "9"
    i = 1
    while i < len(s) and (("0" <= s[i] <= "9") == is_digit):
        i += 1
    return s[:i], s[i:], is_digit


def compare_provider_key(a: str, b: str) -> int:
    """Order two opaque provider source-version keys (adopted assets).

    Split on dots AND dashes, then compare each token naturally, so
    ``17.4-2`` < ``17.4-10``. Never parses the string as a release identity.
    """
    at = _split_provider(a)
    bt = _split_provider(b)
    for i in range(min(len(at), len(bt))):
        c = compare_natural(at[i], bt[i])
        if c:
            return c
    return _cmp_int(len(at), len(bt))


def _split_provider(s: str):
    out = []
    cur = ""
    for ch in s:
        if ch == "." or ch == "-":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def sort_descending(versions):
    """Return versions newest-first (stable Version objects)."""
    import functools

    return sorted(versions, key=functools.cmp_to_key(compare), reverse=True)
