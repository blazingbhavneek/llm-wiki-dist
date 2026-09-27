"""Mirror tests use the WP-06 client contract and never start its background thread."""

import gzip
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from growi_client import GrowiAPIError
from mirror import Mirror
from models import WikiPage


def gid(value):
    return value if len(value) == 24 and all(c in "0123456789abcdef" for c in value.lower()) else hashlib.sha256(value.encode()).hexdigest()[:24]


class FakeGrowi:
    url = "http://growi.test"
    api_token = "tok"

    def __init__(self, pages=None):
        self.pages = {page.id: page for page in (pages or [])}
        self.events = []
        self.calls = {"get_page": 0, "list_descendants": 0, "activity": 0, "recent_pages": 0}
        self.forbid_activity = False
        self.raise_missing = False
        self._lock = threading.Lock()

    def get_page(self, *, page_id=None, path=None):
        with self._lock:
            self.calls["get_page"] += 1
            page = next((page for page in self.pages.values() if page.id == page_id), None) if page_id else next(
                (page for page in self.pages.values() if page.path == path), None
            )
            if page is None and self.raise_missing:
                raise GrowiAPIError(404, "GET", "/_api/v3/page")
            return page.model_copy(deep=True) if page else None

    def _descendants(self, path):
        prefix = path.rstrip("/") + "/"
        return sorted((page for page in self.pages.values()
                       if page.path == path or page.path.startswith(prefix)),
                      key=lambda page: page.updated_at, reverse=True)

    def list_descendants(self, path, *, limit=500, page=1):
        with self._lock:
            self.calls["list_descendants"] += 1
            rows = self._descendants(path)
        start = (page - 1) * limit
        return [row.model_copy(deep=True) for row in rows[start:start + limit]], len(rows)

    def iter_descendants(self, path, *, limit=500):
        page = 1
        seen = set()
        while True:
            rows, total = self.list_descendants(path, limit=limit, page=page)
            for row in rows:
                if row.id not in seen:
                    seen.add(row.id)
                    yield row
            if len(rows) < limit or page * limit >= total:
                return
            page += 1

    def activity(self, *, limit=100, offset=0, actions=None):
        with self._lock:
            self.calls["activity"] += 1
            if self.forbid_activity:
                raise GrowiAPIError(403, "GET", "/_api/v3/activity", "forbidden")
            return [dict(event) for event in self.events[offset:offset + limit]]

    def recent_pages(self, *, limit=100, offset=0):
        with self._lock:
            self.calls["recent_pages"] += 1
            pages = sorted(self.pages.values(), key=lambda page: page.updated_at, reverse=True)
            return [page.model_copy(deep=True) for page in pages[offset:offset + limit]]

    def event(self, action, target, created_at="2024-02-01T00:00:00Z"):
        self.events.insert(0, {"_id": f"event-{len(self.events)}", "action": action,
                               "target": gid(target), "createdAt": created_at})


def page(page_id, path, body="body", revision="r1", updated_at="2024-01-01T00:00:00Z", **kw):
    return WikiPage(id=gid(page_id), path=path, title=path.rsplit("/", 1)[-1], body=body,
                    revision_id=gid(revision), updated_at=updated_at, **kw)


def settings(directory, **kw):
    values = dict(mirror_dir=directory, growi_root_path="/Moove", mirror_changes="audit",
                  mirror_poll_seconds=10, mirror_relist_seconds=1800,
                  mirror_warm_concurrency=4, mirror_list_limit=500, page_cache_mb=1)
    values.update(kw)
    return SimpleNamespace(**values)


def corpus():
    return [page("a", "/Moove/A", descendant_count=1),
            page("ax", "/Moove/A/x"), page("b", "/Moove/B"),
            page("c", "/Moove/C"), page("d", "/Moove/D")]


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = FakeGrowi(corpus())
        self.mirror = Mirror(self.client, settings(self.temp.name))

    def test_first_sync_lists_and_warms(self):
        self.mirror.sync_once()
        self.assertEqual(len(self.mirror.rows), 5)
        self.assertEqual(len(list(self.mirror.pages_dir.rglob("*.json.gz"))), 5)
        reads = self.client.calls["get_page"]
        for page_id in self.mirror.rows:
            self.assertTrue(self.mirror.get(page_id=page_id).body)
        self.assertEqual(self.client.calls["get_page"], reads)

    def test_update_via_audit_log(self):
        self.mirror.sync_once()
        old_file = self.mirror._body_file(gid("b"), gid("r1"))
        self.client.pages[gid("b")] = page("b", "/Moove/B", "changed", "r2", "2024-02-01T00:00:00Z")
        self.client.event("PAGE_UPDATE", "b")
        self.mirror.sync_once()
        self.assertEqual(self.mirror.get(page_id=gid("b")).body, "changed")
        self.mirror.last_relist = 0
        self.mirror.sync_once()
        self.assertFalse(old_file.exists())

    def test_recursive_rename_moves_the_subtree(self):
        self.mirror.sync_once()
        self.client.pages[gid("a")].path = "/Moove/Z"
        self.client.pages[gid("ax")].path = "/Moove/Z/x"
        self.client.event("PAGE_RECURSIVELY_RENAME", "a")
        self.mirror.sync_once()
        paths = {row.path for row in self.mirror.rows.values()}
        self.assertIn("/Moove/Z/x", paths)
        self.assertFalse(any(path == "/Moove/A" or path.startswith("/Moove/A/") for path in paths))
        self.assertEqual(self.mirror.get(path="/Moove/Z/x").body, "body")

    def test_relist_refreshes_same_revision_rename_metadata(self):
        mirror = Mirror(self.client, settings(self.temp.name, mirror_changes="recent"))
        mirror.sync_once()
        original = self.client.pages[gid("b")]
        renamed = original.model_copy(deep=True, update={"path": "/Moove/Z", "title": "Z"})
        self.client.pages[gid("b")] = renamed
        mirror.last_relist = 0
        mirror.sync_once()
        self.assertIsNone(mirror.get(path="/Moove/B"))
        refreshed = mirror.get(path="/Moove/Z")
        self.assertEqual(refreshed.path, "/Moove/Z")
        self.assertEqual(refreshed.title, "Z")
        self.assertEqual(refreshed.body, original.body)

    def test_delete_via_audit_log(self):
        self.mirror.sync_once()
        self.client.pages.pop(gid("b"))
        self.client.event("PAGE_DELETE", "b")
        self.mirror.sync_once()
        self.assertNotIn(gid("b"), self.mirror.rows)
        self.assertIsNone(self.mirror.get(page_id=gid("b")))

    def test_delete_404_is_applied_once(self):
        self.mirror.sync_once()
        self.client.pages.pop(gid("b"))
        self.client.raise_missing = True
        self.client.event("PAGE_DELETE", "b")
        self.mirror.sync_once()
        self.assertNotIn(gid("b"), self.mirror.rows)

    def test_invalid_body_filename_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            self.mirror._body_file("../../escape", "r1")
        self.mirror._upsert(WikiPage(id="../../escape", revision_id=gid("r1"), path="/Moove/bad"))
        self.assertFalse((Path(self.temp.name).parent / "escape.r1.json.gz").exists())

    def test_first_sync_does_not_replay_old_history(self):
        self.client.event("PAGE_UPDATE", "b", "2023-12-01T00:00:00Z")
        self.mirror.sync_once()
        self.assertEqual(self.client.calls["get_page"], 5)  # the warm-up only

    def test_warm_up_writes_the_catalog_once(self):
        writes = []
        save = self.mirror._save_catalog_locked
        self.mirror._save_catalog_locked = lambda: (writes.append(1), save())
        self.mirror.sync_once()
        self.assertEqual(len(writes), 1)  # the relist; never once per warmed page

    def test_single_page_delete_keeps_its_children(self):
        self.mirror.sync_once()
        self.client.pages.pop(gid("a"))
        self.client.event("PAGE_DELETE", "a")
        reads = self.client.calls["get_page"]
        self.mirror.sync_once()
        self.assertNotIn(gid("a"), self.mirror.rows)
        self.assertEqual(self.mirror.rows[gid("ax")].path, "/Moove/A/x")
        self.assertEqual(self.client.calls["get_page"], reads + 1)  # the deleted page only

    def test_forbidden_audit_falls_back_to_recent(self):
        self.client.forbid_activity = True
        self.mirror = Mirror(self.client, settings(self.temp.name, mirror_changes="auto"))
        self.mirror.sync_once()
        meta = json.loads(self.mirror.meta_file.read_text())
        self.assertEqual(meta["changes"], "recent")
        self.client.pages[gid("b")] = page("b", "/Moove/B", "new", "r2", "2024-03-01T00:00:00Z")
        self.mirror.sync_once()
        self.assertEqual(self.mirror.get(page_id=gid("b")).body, "new")

    def test_folders_without_a_page_are_listed(self):
        self.client.pages[gid("f")] = page("f", "/Moove/Folder/Doc/001")
        self.mirror.sync_once()
        folder = next(child for child in self.mirror.children_of("/Moove") if child.path == "/Moove/Folder")
        self.assertEqual((folder.id, folder.descendant_count), ("", 1))
        self.assertEqual([c.path for c in self.mirror.children_of("/Moove/Folder")], ["/Moove/Folder/Doc"])
        self.assertEqual([c.path for c in self.mirror.children_of("/Moove/Folder/Doc")], ["/Moove/Folder/Doc/001"])

    def test_disabled_audit_log_falls_back_to_recent(self):
        def disabled(**_kwargs):
            raise GrowiAPIError(405, "GET", "/_api/v3/activity", '{"errors":[{"message":"AuditLog is not enabled"}]}')
        self.client.activity = disabled
        self.mirror = Mirror(self.client, settings(self.temp.name, mirror_changes="auto"))
        self.mirror.sync_once()
        self.assertTrue(self.mirror.ready)
        self.assertEqual(json.loads(self.mirror.meta_file.read_text())["changes"], "recent")

    def test_restart_serves_from_disk_before_any_call(self):
        self.mirror.sync_once()
        fresh = FakeGrowi(corpus())
        restarted = Mirror(fresh, settings(self.temp.name))
        restarted._load()
        self.assertTrue(restarted.ready)
        self.assertEqual(restarted.get(page_id=gid("b")).body, "body")
        self.assertEqual(sum(fresh.calls.values()), 0)

    def test_namespace_never_contains_the_token(self):
        self.mirror.sync_once()
        other = Mirror(FakeGrowi(corpus()), settings(self.temp.name))
        other.client.api_token = "SEKRET-123"
        other = Mirror(other.client, settings(self.temp.name))
        self.assertNotIn("SEKRET-123", str(self.mirror.directory))
        self.assertNotEqual(self.mirror.directory, other.directory)
        self.assertFalse(any("SEKRET-123" in str(path) for path in Path(self.temp.name).rglob("*")))

    def test_out_of_scope_pages_are_never_stored(self):
        self.client.pages["outside"] = page("outside", "/Elsewhere/outside")
        self.mirror.sync_once()
        self.assertNotIn(gid("outside"), self.mirror.rows)
        self.assertFalse(list(self.mirror.pages_dir.rglob("outside.*.json.gz")))

    def test_children_of_matches_listing_shape(self):
        self.mirror.sync_once()
        children = self.mirror.children_of("/Moove")
        self.assertEqual({item.id for item in children}, {gid(key) for key in ("a", "b", "c", "d")})
        self.assertEqual(next(item for item in children if item.id == gid("a")).descendant_count, 1)
        self.assertEqual(next(item for item in children if item.id == gid("a")).body, "")

    def test_no_tmp_files_left(self):
        self.mirror.sync_once()
        self.mirror.sync_once()
        self.assertFalse(list(Path(self.temp.name).rglob("*.tmp-*")))


if __name__ == "__main__":
    unittest.main()
