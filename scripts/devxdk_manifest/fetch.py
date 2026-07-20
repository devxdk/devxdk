"""Robust HTTP fetching for the scrape adapters (standard library only).

Every upstream call goes through here so the robustness policy is uniform:
explicit timeouts, bounded retries with backoff, a response-size cap (a hostile
or broken feed can't exhaust memory), and Link-header pagination followed to
exhaustion. The default ``Fetcher`` hits the network; tests inject a fake with
the same surface so no adapter test touches a live feed.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 60
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 1.5
# 64 MiB: larger than any manifest/index we read, small enough to bound memory.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
USER_AGENT = "devxdk-manifest-scraper"


class FetchError(RuntimeError):
    """A fetch failed after exhausting retries, or exceeded the size cap."""


class Fetcher:
    """Live HTTP fetcher. Inject a fake with the same methods to test offline."""

    def __init__(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        max_bytes: int = DEFAULT_MAX_BYTES,
        opener=None,
        sleep=time.sleep,
    ):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.max_bytes = max_bytes
        self._open = opener or urllib.request.urlopen
        self._sleep = sleep

    # -- raw reads ---------------------------------------------------------

    def get_bytes(self, url: str, headers: dict | None = None) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        last = None
        for attempt in range(self.retries):
            try:
                with self._open(req, timeout=self.timeout) as resp:
                    return self._read_capped(resp)
            except (urllib.error.URLError, OSError, FetchError) as e:
                last = e
                if attempt + 1 < self.retries:
                    self._sleep(self.backoff ** attempt)
        raise FetchError(f"GET {url} failed after {self.retries} attempts: {last}")

    def get_text(self, url: str, headers: dict | None = None) -> str:
        return self.get_bytes(url, headers).decode("utf-8")

    def get_json(self, url: str, headers: dict | None = None):
        return json.loads(self.get_text(url, headers))

    def _read_capped(self, resp) -> bytes:
        # Read one extra byte so an at-cap body is still detectable as over-cap.
        data = resp.read(self.max_bytes + 1)
        if len(data) > self.max_bytes:
            raise FetchError(f"response exceeds {self.max_bytes} byte cap")
        return data

    # -- size probe (Node has no size in its feed) -------------------------

    def remote_size(self, url: str) -> int:
        """Total size via HEAD, falling back to a ranged GET (Content-Range), so
        a server that omits Content-Length on HEAD doesn't silently yield 0.
        Returns 0 (size check disabled client-side) only when both fail."""
        size = self._head_size(url)
        if size:
            return size
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"}
            )
            with self._open(req, timeout=self.timeout) as resp:
                cr = resp.headers.get("Content-Range")  # "bytes 0-0/123456"
                if cr and "/" in cr:
                    total = cr.rsplit("/", 1)[-1]
                    if total.isdigit():
                        return int(total)
                length = resp.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > 1:
                    return int(length)
        except (urllib.error.URLError, ValueError, OSError):
            pass
        return 0

    def _head_size(self, url: str) -> int:
        try:
            req = urllib.request.Request(
                url, method="HEAD", headers={"User-Agent": USER_AGENT}
            )
            with self._open(req, timeout=self.timeout) as resp:
                length = resp.headers.get("Content-Length")
                return int(length) if length else 0
        except (urllib.error.URLError, ValueError, OSError):
            return 0

    # -- pagination --------------------------------------------------------

    def get_json_paginated(self, url: str, headers: dict | None = None, max_pages: int = 100):
        """Follow RFC-5988 ``Link: rel="next"`` to exhaustion, concatenating JSON
        arrays. GitHub list endpoints default to 30 items/page, so any list we
        read (release assets, commits) must page through — a target beyond page 1
        would otherwise be silently truncated."""
        out = []
        next_url = url
        pages = 0
        while next_url and pages < max_pages:
            req = urllib.request.Request(
                next_url, headers={"User-Agent": USER_AGENT, **(headers or {})}
            )
            last = None
            for attempt in range(self.retries):
                try:
                    with self._open(req, timeout=self.timeout) as resp:
                        body = self._read_capped(resp)
                        page = json.loads(body.decode("utf-8"))
                        if not isinstance(page, list):
                            raise FetchError(f"paginated GET {next_url}: expected a JSON array")
                        out.extend(page)
                        next_url = _parse_next_link(resp.headers.get("Link"))
                        break
                except (urllib.error.URLError, OSError, FetchError) as e:
                    last = e
                    if attempt + 1 < self.retries:
                        self._sleep(self.backoff ** attempt)
            else:
                raise FetchError(f"paginated GET {next_url} failed: {last}")
            pages += 1
        if next_url and pages >= max_pages:
            raise FetchError(f"pagination exceeded {max_pages} pages for {url}")
        return out


def _parse_next_link(link_header: str | None) -> str | None:
    """Extract the rel="next" URL from an RFC-5988 Link header, if present."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url_part = segments[0].strip()
        if not (url_part.startswith("<") and url_part.endswith(">")):
            continue
        for seg in segments[1:]:
            if seg.strip() in ('rel="next"', "rel=next"):
                return url_part[1:-1]
    return None
