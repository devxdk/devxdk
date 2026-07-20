"""Robustness tests for the HTTP adapter: size cap, bounded retries, and
Link-header pagination — all with a fake opener, never a live feed."""

import unittest

from devxdk_manifest import fetch


class FakeResp:
    def __init__(self, body=b"", headers=None):
        self._body = body
        self._pos = 0
        self.headers = headers or {}

    def read(self, n=-1):
        if n is None or n < 0:
            data = self._body[self._pos:]
            self._pos = len(self._body)
            return data
        data = self._body[self._pos:self._pos + n]
        self._pos += len(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestFetch(unittest.TestCase):
    def test_size_cap(self):
        opener = lambda req, timeout=None: FakeResp(b"x" * 100)
        f = fetch.Fetcher(max_bytes=10, opener=opener, sleep=lambda s: None)
        with self.assertRaises(fetch.FetchError):
            f.get_bytes("https://example.com/big")

    def test_at_cap_ok(self):
        opener = lambda req, timeout=None: FakeResp(b"x" * 10)
        f = fetch.Fetcher(max_bytes=10, opener=opener, sleep=lambda s: None)
        self.assertEqual(f.get_bytes("https://example.com/ok"), b"x" * 10)

    def test_retry_then_success(self):
        calls = {"n": 0}

        def opener(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("transient")
            return FakeResp(b'{"ok": true}')

        f = fetch.Fetcher(retries=3, opener=opener, sleep=lambda s: None)
        self.assertEqual(f.get_json("https://example.com/x"), {"ok": True})
        self.assertEqual(calls["n"], 3)

    def test_retry_exhausted(self):
        def opener(req, timeout=None):
            raise OSError("down")

        f = fetch.Fetcher(retries=2, opener=opener, sleep=lambda s: None)
        with self.assertRaises(fetch.FetchError):
            f.get_text("https://example.com/down")

    def test_pagination_follows_link(self):
        p1 = FakeResp(b'[1, 2]', headers={"Link": '<https://api/x?page=2>; rel="next"'})
        p2 = FakeResp(b'[3, 4]', headers={})
        pages = {"https://api/x": p1, "https://api/x?page=2": p2}

        def opener(req, timeout=None):
            return pages[req.full_url]

        f = fetch.Fetcher(opener=opener, sleep=lambda s: None)
        self.assertEqual(f.get_json_paginated("https://api/x"), [1, 2, 3, 4])

    def test_pagination_cap(self):
        # A feed that always advertises a next page must not loop unbounded.
        resp = FakeResp(b'[1]', headers={"Link": '<https://api/x?page=2>; rel="next"'})

        def opener(req, timeout=None):
            return FakeResp(b'[1]', headers={"Link": '<https://api/x?next>; rel="next"'})

        f = fetch.Fetcher(opener=opener, sleep=lambda s: None)
        with self.assertRaises(fetch.FetchError):
            f.get_json_paginated("https://api/x", max_pages=3)


if __name__ == "__main__":
    unittest.main()
