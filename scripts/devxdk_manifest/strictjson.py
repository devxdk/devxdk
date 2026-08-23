"""Strict JSON parsing whose PROPERTY is: the Python and the Go parse of a
signed document are the same document.

Every rule below is one known way that property fails. The list is the classes
we have proven, not a proof that no seventh exists — so treat a new divergence
as a missing rule here, not as an exception to make somewhere else:

  1. duplicate members — both decoders keep the last, deterministically, but an
     ordering key must not have two admissible spellings;
  2. case-fold aliases — ``{"revision":6,"Revision":100}`` reads 6 in Python and
     100 in Go, because Go's decoder matches both to the tagged field and keeps
     the last. A history gate ordering that document at 6 would make a client
     record 100, after which the genuine revision 7 is *lower* than the mark and
     that component is refused permanently;
  3. non-ASCII member names — rejected outright, which is what lets rule 2 be
     defined as ASCII folding. Go folds with SimpleFold semantics and Python's
     ``str.casefold()`` does *full* Unicode folding ('ß' -> 'ss'), so a Unicode
     rule would make the two implementations disagree about the rule meant to
     make them agree;
  4. NaN / Infinity / -Infinity — Python accepts all three by default; Go
     rejects them as invalid JSON. The pipeline would sign a manifest no client
     can parse at all;
  5. string ENCODING — invalid UTF-8 bytes and unpaired surrogate escapes. Go's
     decoder rewrites both to U+FFFD while Python keeps a lone surrogate
     verbatim, so ``{"version":"\\ud800"}`` is accepted by both and yields two
     different strings. Both fixes must sit OUTSIDE the parser, and the obvious
     placement is wrong on both sides: handing ``bytes`` to ``json.loads`` looks
     strict but sniffs UTF-16/UTF-32, so the ``.decode("utf-8")`` here IS the
     encoding gate; and a token walk cannot see either case because unquoting
     has already happened.

Class 6 — numbers that are not the integer Go expects (``1.5``, a bignum,
``true``) — is deliberately NOT here. This module is schema-neutral: it also
parses upstream release feeds and ``gh api`` output, where a float is correct
JSON. That class belongs in the domain validators, via
``schema.require_positive_int64``.

Standard library only, and it imports nothing from this package: ``schema``
imports it, and ``merge`` imports ``schema``.
"""

from __future__ import annotations

import json
import string

__all__ = ["StrictJSONError", "loads", "load", "split_json_arrays"]


class StrictJSONError(ValueError):
    """A document the two languages would not parse identically.

    A ``ValueError`` subclass for convenience, but every failure boundary
    catches it BY NAME — the existing handlers catch specific types, so a bare
    ValueError there is an uncaught traceback instead of the clean
    "FAILED (nothing written)" refusal those scripts advertise.
    """


# A-Z -> a-z and nothing else. Deliberately not str.casefold(): see rule 3.
_ASCII_FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _object_pairs_hook(pairs):
    """Reject duplicate and case-fold-equivalent member names, per object.

    json.loads calls this for every object at every depth, so the recursion in
    the rule comes for free.
    """
    seen = {}
    out = {}
    for key, value in pairs:
        if not key.isascii():
            raise StrictJSONError(f"member name {key!r} contains a non-ASCII character")
        folded = key.translate(_ASCII_FOLD)
        prev = seen.get(folded)
        if prev is not None:
            if prev == key:
                raise StrictJSONError(f"duplicate member {key!r}")
            raise StrictJSONError(
                f"member names {prev!r} and {key!r} are case-fold equivalent "
                "(Go's decoder matches both to the same field and keeps the last)"
            )
        seen[folded] = key
        out[key] = value
    return out


def _reject_constant(name):
    raise StrictJSONError(f"{name} is not valid JSON (Go rejects it outright)")


def _check_surrogate_escapes(text):
    """Reject a \\uD800-\\uDFFF escape that is not part of a valid pair.

    Implemented as a scan over the raw text because the parser cannot help: by
    the time json.loads hands back a string the escape has been decoded. The
    scan tracks string state so it is not fooled by an escaped backslash —
    ``"\\\\uD800"`` is a literal backslash followed by the text ``uD800`` and
    must be ACCEPTED, which a naive substring search rejects.
    """
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if not in_string:
            if ch == '"':
                in_string = True
            i += 1
            continue
        if ch == "\\":
            if i + 1 >= n:
                return  # truncated; json.loads reports it
            if text[i + 1] != "u":
                i += 2  # \\ \" \n ... — consumes the escaped character too
                continue
            hexpart = text[i + 2:i + 6]
            if len(hexpart) < 4:
                return  # truncated; json.loads reports it
            try:
                cp = int(hexpart, 16)  # hex digits ARE case-insensitive, unlike member names
            except ValueError:
                return  # malformed escape; json.loads reports it
            if 0xD800 <= cp <= 0xDBFF:
                if text[i + 6:i + 8] == "\\u" and len(text[i + 8:i + 12]) == 4:
                    try:
                        low = int(text[i + 8:i + 12], 16)
                    except ValueError:
                        low = -1
                    if 0xDC00 <= low <= 0xDFFF:
                        i += 12  # a valid pair
                        continue
                raise StrictJSONError(
                    f"high surrogate escape \\u{hexpart} has no low surrogate "
                    "(Go would replace it with U+FFFD; Python would not)"
                )
            if 0xDC00 <= cp <= 0xDFFF:
                raise StrictJSONError(
                    f"lone low surrogate escape \\u{hexpart} "
                    "(Go would replace it with U+FFFD; Python would not)"
                )
            i += 6
            continue
        if ch == '"':
            in_string = False
        i += 1


def _text(raw):
    """Decode to str, strictly. This call is the UTF-8 gate, not the parser."""
    if isinstance(raw, str):
        try:
            raw.encode("utf-8")  # a str carrying a lone surrogate fails here
        except UnicodeEncodeError as e:
            raise StrictJSONError(f"not valid UTF-8: {e}") from e
        return raw
    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            return bytes(raw).decode("utf-8")
        except UnicodeDecodeError as e:
            raise StrictJSONError(f"not valid UTF-8: {e}") from e
    raise StrictJSONError(f"expected bytes or str, got {type(raw).__name__}")


def loads(raw):
    """Parse `raw` (bytes preferred, str accepted) under all five syntax rules.

    Raises StrictJSONError for those; a plain syntax error still raises
    json.JSONDecodeError, so existing handlers that catch it keep working.
    """
    text = _text(raw)
    _check_surrogate_escapes(text)
    return json.loads(
        text,
        object_pairs_hook=_object_pairs_hook,
        parse_constant=_reject_constant,
    )


def load(path):
    """Strict-parse a file. read_bytes, never a text read: universal-newlines
    mode would translate CRLF on the way in, and callers here compare bytes."""
    with open(path, "rb") as fh:
        return loads(fh.read())


def split_json_arrays(raw):
    """Yield each top-level value from a concatenation of them.

    `gh --paginate` emits one JSON array per page with nothing between them, so
    the pages have to be walked incrementally. A generator, like the permissive
    version it replaces — which means a refusal surfaces at the consumer's `for`
    loop, not at the call that creates it.
    """
    text = _text(raw)
    _check_surrogate_escapes(text)
    dec = json.JSONDecoder(
        object_pairs_hook=_object_pairs_hook,
        parse_constant=_reject_constant,
    )
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \r\n\t":
            i += 1
        if i >= n:
            break
        obj, end = dec.raw_decode(text, i)
        yield obj
        i = end
