"""Tests for the component-Release reconciliation engine (fake GitHub API)."""

import hashlib
import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

from devxdk_manifest import releasepub  # noqa: E402


class FakeAPI:
    """In-memory GitHub Releases, exercising every path the engine drives.

    Assets carry a real sha256 (from the uploaded bytes) surfaced as a
    `digest`, unless the name is registered `no_digest` (forces the
    download+rehash fallback) or `bad_upload` (returns a wrong digest on
    upload, to prove the post-upload verify)."""

    def __init__(self, releases=None, no_digest=(), bad_upload=()):
        self.releases = releases or {}  # tag -> {id, draft, prerelease, assets:[...]}
        self._next_id = 1000
        self.no_digest = set(no_digest)
        self.bad_upload = set(bad_upload)
        self.log = []

    def _id(self):
        self._next_id += 1
        return self._next_id

    def get_release(self, tag):
        return self.releases.get(tag)

    def create_release(self, tag, *, prerelease):
        self.log.append(("create", tag, prerelease))
        rel = {"id": self._id(), "draft": True, "prerelease": prerelease, "assets": []}
        self.releases[tag] = rel
        return rel

    def _digest_for(self, name, data):
        if name in self.no_digest:
            return None
        if name in self.bad_upload:
            return "sha256:" + "0" * 64
        return "sha256:" + hashlib.sha256(data).hexdigest()

    def upload_asset(self, release_id, name, path):
        self.log.append(("upload", name))
        data = pathlib.Path(path).read_bytes()
        asset = {"id": self._id(), "name": name, "size": len(data),
                 "digest": self._digest_for(name, data), "_bytes": data}
        for rel in self.releases.values():
            if rel["id"] == release_id:
                rel["assets"].append(asset)
        return asset

    def delete_asset(self, asset_id):
        self.log.append(("delete", asset_id))
        for rel in self.releases.values():
            rel["assets"] = [a for a in rel["assets"] if a["id"] != asset_id]

    def publish_release(self, release_id):
        self.log.append(("publish", release_id))
        for rel in self.releases.values():
            if rel["id"] == release_id:
                rel["draft"] = False

    def download_asset(self, asset):
        return asset["_bytes"]


def _write(d, name, content):
    p = pathlib.Path(d) / name
    p.write_bytes(content)
    return str(p), hashlib.sha256(content).hexdigest()


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def _member(self, name, content, object_code):
        path, sha = _write(self.d, name, content)
        return (name, path, sha, object_code)

    def test_fresh_release_source_first_then_undraft(self):
        api = FakeAPI()
        members = [
            self._member("redis-8.8.0-src.tar.gz", b"SOURCE", False),
            self._member("redis-8.8.0-windows-amd64.zip", b"OBJECT", True),
        ]
        actions = releasepub.reconcile_release(api, "redis-8.8.0", prerelease=False,
                                               members=members, referenced_names=set())
        # Created draft, uploaded source BEFORE object, then undrafted.
        self.assertEqual([a for a in api.log if a[0] in ("create", "upload", "publish")],
                         [("create", "redis-8.8.0", False), ("upload", "redis-8.8.0-src.tar.gz"),
                          ("upload", "redis-8.8.0-windows-amd64.zip"), ("publish", 1001)])
        self.assertFalse(api.releases["redis-8.8.0"]["draft"])
        self.assertIn(("undraft", "redis-8.8.0"), actions)

    def test_object_before_source_is_rejected(self):
        api = FakeAPI()
        members = [
            self._member("redis-8.8.0-windows-amd64.zip", b"OBJECT", True),
            self._member("redis-8.8.0-src.tar.gz", b"SOURCE", False),
        ]
        with self.assertRaises(releasepub.ReleaseError):
            releasepub.reconcile_release(api, "redis-8.8.0", prerelease=False,
                                         members=members, referenced_names=set())

    def test_adopt_matching_existing(self):
        path, sha = _write(self.d, "redis-8.8.0-windows-amd64.zip", b"OBJECT")
        api = FakeAPI(releases={"redis-8.8.0": {"id": 1, "draft": False, "assets": [
            {"id": 2, "name": "redis-8.8.0-windows-amd64.zip", "size": 6,
             "digest": "sha256:" + sha, "_bytes": b"OBJECT"}]}})
        actions = releasepub.reconcile_release(
            api, "redis-8.8.0", prerelease=False,
            members=[("redis-8.8.0-windows-amd64.zip", path, sha, True)],
            referenced_names=set())
        self.assertIn(("adopt", "redis-8.8.0-windows-amd64.zip"), actions)
        self.assertNotIn("upload", [a[0] for a in api.log])

    def test_referenced_mismatch_is_hard_error(self):
        path, sha = _write(self.d, "redis-8.8.0-windows-amd64.zip", b"NEWBYTES")
        api = FakeAPI(releases={"redis-8.8.0": {"id": 1, "draft": False, "assets": [
            {"id": 2, "name": "redis-8.8.0-windows-amd64.zip", "size": 3,
             "digest": "sha256:" + "a" * 64, "_bytes": b"OLD"}]}})
        with self.assertRaises(releasepub.ReleaseError):
            releasepub.reconcile_release(
                api, "redis-8.8.0", prerelease=False,
                members=[("redis-8.8.0-windows-amd64.zip", path, sha, True)],
                referenced_names={"redis-8.8.0-windows-amd64.zip"})

    def test_unreferenced_published_mismatch_is_hard_error(self):
        path, sha = _write(self.d, "redis-8.8.0-windows-amd64.zip", b"NEWBYTES")
        api = FakeAPI(releases={"redis-8.8.0": {"id": 1, "draft": False, "assets": [
            {"id": 2, "name": "redis-8.8.0-windows-amd64.zip", "size": 3,
             "digest": "sha256:" + "a" * 64, "_bytes": b"OLD"}]}})
        with self.assertRaises(releasepub.ReleaseError):
            releasepub.reconcile_release(
                api, "redis-8.8.0", prerelease=False,
                members=[("redis-8.8.0-windows-amd64.zip", path, sha, True)],
                referenced_names=set())

    def test_starter_remnant_deleted_and_reuploaded(self):
        path, sha = _write(self.d, "redis-8.8.0-windows-amd64.zip", b"OBJECT")
        api = FakeAPI(releases={"redis-8.8.0": {"id": 1, "draft": True, "assets": [
            {"id": 2, "name": "redis-8.8.0-windows-amd64.zip", "size": 0,
             "state": "starter", "digest": None}]}})
        actions = releasepub.reconcile_release(
            api, "redis-8.8.0", prerelease=False,
            members=[("redis-8.8.0-windows-amd64.zip", path, sha, True)],
            referenced_names=set())
        self.assertIn(("delete-remnant", "redis-8.8.0-windows-amd64.zip"), actions)
        self.assertIn(("upload", "redis-8.8.0-windows-amd64.zip"), actions)

    def test_digest_fallback_download_and_rehash(self):
        path, sha = _write(self.d, "redis-8.8.0-windows-amd64.zip", b"OBJECT")
        api = FakeAPI(releases={"redis-8.8.0": {"id": 1, "draft": False, "assets": [
            {"id": 2, "name": "redis-8.8.0-windows-amd64.zip", "size": 6,
             "digest": None, "_bytes": b"OBJECT"}]}})  # no digest -> download+rehash
        actions = releasepub.reconcile_release(
            api, "redis-8.8.0", prerelease=False,
            members=[("redis-8.8.0-windows-amd64.zip", path, sha, True)],
            referenced_names=set())
        self.assertIn(("adopt", "redis-8.8.0-windows-amd64.zip"), actions)

    def test_post_upload_digest_mismatch_raises(self):
        path, sha = _write(self.d, "x.zip", b"OBJECT")
        api = FakeAPI(bad_upload={"x.zip"})
        with self.assertRaises(releasepub.ReleaseError):
            releasepub.reconcile_release(api, "redis-8.8.0", prerelease=False,
                                         members=[("x.zip", path, sha, True)],
                                         referenced_names=set())

    def test_cleanup_unreferenced_starter_orphan(self):
        path, sha = _write(self.d, "redis-8.8.0-windows-amd64.zip", b"OBJECT")
        api = FakeAPI(releases={"redis-8.8.0": {"id": 1, "draft": True, "assets": [
            {"id": 9, "name": "stale-orphan.zip", "size": 0, "state": "starter", "digest": None}]}})
        actions = releasepub.reconcile_release(
            api, "redis-8.8.0", prerelease=False,
            members=[("redis-8.8.0-windows-amd64.zip", path, sha, True)],
            referenced_names=set())
        self.assertIn(("cleanup-starter", "stale-orphan.zip"), actions)


class TestBuildMembers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = pathlib.Path(self.tmp.name)

    def test_default_single_archive(self):
        (self.d / "php-8.4.23-windows-amd64.zip").write_bytes(b"z")
        meta = {"archive": "php-8.4.23-windows-amd64.zip", "sha256": "AB" * 32}
        members = releasepub.build_members(meta, self.d)
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0][0], "php-8.4.23-windows-amd64.zip")
        self.assertTrue(members[0][3])  # object_code
        self.assertEqual(members[0][2], "ab" * 32)  # lowercased

    def test_declared_assets_source_first(self):
        meta = {"release_assets": [
            {"name": "redis-8.8.0-windows-amd64.zip", "sha256": "a" * 64, "object_code": True},
            {"name": "redis-8.8.0-src.tar.gz", "sha256": "b" * 64, "object_code": False},
        ]}
        members = releasepub.build_members(meta, self.d)
        self.assertEqual([m[0] for m in members],
                         ["redis-8.8.0-src.tar.gz", "redis-8.8.0-windows-amd64.zip"])

    def test_adopt_has_no_members(self):
        # An adopt meta re-hosts nothing (the manifest references the upstream
        # URL), so it has no release members even though it carries a url/sha.
        meta = {"ordering_kind": "adopted", "url": "https://x/y.tar.gz",
                "sha256": "c" * 64, "size_bytes": 1}
        self.assertEqual(releasepub.build_members(meta, self.d), [])


class TestReferencedNames(unittest.TestCase):
    def test_extracts_download_urls_for_tag(self):
        releases = [{"platforms": {
            "windows/amd64": {"url": "https://github.com/devxdk/devxdk/releases/download/redis-8.8.0/redis-8.8.0-windows-amd64.zip"},
            "linux/amd64": {"url": "https://github.com/devxdk/devxdk/releases/download/redis-8.8.0-r2/other.tar.gz"},
        }}]
        got = releasepub.referenced_asset_names(releases, "redis-8.8.0")
        self.assertEqual(got, {"redis-8.8.0-windows-amd64.zip"})


if __name__ == "__main__":
    unittest.main()
