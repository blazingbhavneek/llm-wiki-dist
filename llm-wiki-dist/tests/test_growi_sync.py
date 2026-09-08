from __future__ import annotations

import unittest
from types import SimpleNamespace

from graph.growi import GrowiPage, registry_page, sync_growi_pages


class FakeRegistry:
    def __init__(self, pages):
        self._pages = list(pages)
        self.cursor = "old"
        self.recorded = []

    def pages(self, _name):
        return list(self._pages)

    def upsert_page(self, page):
        self._pages = [item for item in self._pages if item.page_id != page.page_id]
        self._pages.append(page)

    def delete_page(self, _name, page_id):
        self._pages = [item for item in self._pages if item.page_id != page_id]

    def record_sync(self, _name, **kwargs):
        self.recorded.append(kwargs)
        self.cursor = kwargs["cursor"]


class FakeClient:
    def __init__(self, listing, full):
        self.listing = listing
        self.full = full
        self.gets = []

    async def list_pages(self, *_args, **_kwargs):
        return self.listing, "new"

    async def get_page(self, *, page_id):
        self.gets.append(page_id)
        return self.full.get(page_id)


class GrowiSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_revision_does_no_page_work(self):
        old = GrowiPage(page_id="p", revision_id="r1", path="/p")
        registry = FakeRegistry([registry_page("docs", old)])
        client = FakeClient([old], {"p": old})
        seen = []
        result = await sync_growi_pages(
            client,
            registry,
            SimpleNamespace(name="docs", root_path="/", last_sync_at=None, sync_cursor=None),
            on_page=lambda *_: seen.append("page"),
            on_delete=lambda *_: seen.append("delete"),
        )
        self.assertEqual(client.gets, [])
        self.assertEqual(seen, [])
        self.assertEqual(result["changed"], 0)

    async def test_changed_and_deleted_pages_are_scoped(self):
        old_changed = GrowiPage(page_id="p1", revision_id="r1", path="/one")
        old_deleted = GrowiPage(page_id="p2", revision_id="r1", path="/two")
        new_changed = GrowiPage(page_id="p1", revision_id="r2", path="/one", body="new")
        registry = FakeRegistry([
            registry_page("docs", old_changed),
            registry_page("docs", old_deleted),
        ])
        client = FakeClient([new_changed], {"p1": new_changed})
        pages = []
        deleted = []
        result = await sync_growi_pages(
            client,
            registry,
            SimpleNamespace(name="docs", root_path="/", last_sync_at=None, sync_cursor=None),
            on_page=lambda page, _old: pages.append(page.page_id),
            on_delete=lambda page: deleted.append(page.page_id),
        )
        self.assertEqual(client.gets, ["p1"])
        self.assertEqual(pages, ["p1"])
        self.assertEqual(deleted, ["p2"])
        self.assertEqual(result["changed"], 1)
        self.assertEqual(result["deleted"], 1)

    async def test_callback_failure_does_not_advance_cursor(self):
        new = GrowiPage(page_id="p", revision_id="r2", path="/p")
        registry = FakeRegistry([registry_page("docs", GrowiPage(page_id="p", revision_id="r1", path="/p"))])
        client = FakeClient([new], {"p": new})

        async def fail(*_args):
            raise RuntimeError("index failed")

        with self.assertRaises(RuntimeError):
            await sync_growi_pages(
                client,
                registry,
                SimpleNamespace(name="docs", root_path="/", last_sync_at=None, sync_cursor="old"),
                on_page=fail,
                on_delete=lambda *_: None,
            )
        self.assertEqual(registry.recorded, [])


if __name__ == "__main__":
    unittest.main()
