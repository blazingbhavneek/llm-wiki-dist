from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from graph.growi import GrowiClient, GrowiPage, GrowiPublisher, merge_marked_sections, publish_pages
from graph.growi.client import managed_page_markdown, restore_page_links, rewrite_page_links


class GrowiPublishTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_mode_rejects_writes_outside_write_path(self):
        client = GrowiClient(
            "https://wiki.example",
            "token",
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        )
        with self.assertRaises(PermissionError):
            await publish_pages(
                client,
                [{"path": "/public/page", "body": "<!-- chunk: c -->\ntext"}],
                mode="attach",
                write_path="/inbox",
            )

    async def test_delete_pages_batches_at_growi_limit(self):
        batches: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            batches.append(json.loads(request.content)["pageIdToRevisionIdMap"])
            return httpx.Response(200)

        client = GrowiClient("https://wiki.example", "token", transport=httpx.MockTransport(handler))
        await client.delete_pages({f"page-{index}": f"revision-{index}" for index in range(45)})

        self.assertEqual([len(batch) for batch in batches], [20, 20, 5])

    async def test_one_page_gets_one_revision_for_multiple_chunks(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "page": {
                            "_id": "p1",
                            "path": "/inbox/page",
                            "revision": "r1",
                            "body": "human text\n\n<!-- chunk: c1 lines 1-2 hash:x -->\nold",
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "page": {
                        "_id": "p1",
                        "path": "/inbox/page",
                        "revision": "r2",
                        "body": request.content.decode(),
                    }
                },
            )

        client = GrowiClient(
            "https://wiki.example",
            "token",
            transport=httpx.MockTransport(handler),
        )
        result = await publish_pages(
            client,
            [
                {
                    "path": "/inbox/page",
                    "body": (
                        "<!-- chunk: c1 lines 1-2 hash:x -->\nnew one\n\n"
                        "<!-- chunk: c2 lines 3-4 hash:y -->\nnew two"
                    ),
                }
            ],
            mode="attach",
            write_path="/inbox",
        )
        self.assertEqual(len(result), 1)
        self.assertEqual([request.method for request in requests], ["GET", "PUT"])
        body = requests[1].content.decode()
        self.assertIn('"revisionId":"r1"', body)
        self.assertIn("human text", body)
        self.assertIn("new one", body)
        self.assertIn("new two", body)

    def test_unmarked_existing_text_survives_marked_merge(self):
        merged = merge_marked_sections(
            "human intro\n<!-- chunk: c lines 1-1 hash:x -->\nold",
            "<!-- chunk: c lines 1-1 hash:x -->\nnew",
        )
        self.assertEqual(merged.count("human intro"), 1)
        self.assertIn("new", merged)
        self.assertNotIn("old", merged)

    async def test_all_page_ids_are_resolved_before_links_are_published(self):
        requests: list[tuple[str, str]] = []
        ids = {
            "/wiki/docs/a": "507f1f77bcf86cd799439011",
            "/wiki/other/b": "507f191e810c19729de860ea",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content or b"{}")
            path = request.url.params.get("path") or payload.get("path", "")
            requests.append((request.method, path))
            if request.method == "GET":
                return httpx.Response(404)
            if request.method == "POST":
                return httpx.Response(201, json={"page": {"_id": ids[path], "path": path, "revision": "r1"}})
            page_id = payload["pageId"]
            return httpx.Response(200, json={"page": {"_id": page_id, "path": path, "revision": "r2", "body": payload["body"]}})

        client = GrowiClient("https://wiki.example", "token", transport=httpx.MockTransport(handler))
        pages = [
            {
                "local_path": "docs/a.md",
                "path": "/wiki/docs/a",
                "body": "<!-- chunk: a -->\n[A to B](../other/b.md)\n<!-- chunk-end: a -->",
            },
            {
                "local_path": "other/b.md",
                "path": "/wiki/other/b",
                "body": "<!-- chunk: b -->\n[B to A](../docs/a.md)\n<!-- chunk-end: b -->",
            },
        ]

        result = await publish_pages(client, pages, mode="attach", write_path="/wiki")

        methods = [method for method, _ in requests]
        self.assertGreater(methods.index("PUT"), max(index for index, method in enumerate(methods) if method == "POST"))
        self.assertEqual(result[0].page_id, ids["/wiki/docs/a"])
        self.assertIn(f'](/{ids["/wiki/other/b"]})', result[0].body)
        self.assertIn(f'](/{ids["/wiki/docs/a"]})', result[1].body)

    def test_link_rewrite_keeps_local_markdown_and_code_examples(self):
        page_id = "507f191e810c19729de860ea"
        malformed = "[outer](bad-[inner](b.md).md)"
        body = f"[live](b.md)\n{malformed}\n```md\n[example](b.md)\n```\n"
        rewritten = rewrite_page_links(body, "docs/a.md", {"docs/b.md": page_id})
        self.assertEqual(rewritten, f"[live](/{page_id})\n{malformed}\n```md\n[example](b.md)\n```\n")

    def test_remote_managed_edit_is_pulled_back_to_local_markdown(self):
        page_id = "507f191e810c19729de860ea"
        remote = GrowiPage(
            page_id=page_id,
            revision_id="r2",
            path="/wiki/docs/a",
            body=(
                "human text outside publisher ownership\n"
                "<!-- chunk: page/wiki/docs/a lines 1-2 hash:x -->\n"
                f"# edited\n\n[to b](/{page_id})\n"
                "<!-- chunk-end: page/wiki/docs/a -->"
            ),
        )

        class Client:
            async def get_page(self, *, page_id):
                return remote

        with tempfile.TemporaryDirectory() as temporary:
            wiki = Path(temporary)
            page = wiki / "docs" / "a.md"
            pristine = wiki / "docs" / "_planning" / "pages" / "a.md"
            pristine.parent.mkdir(parents=True)
            page.write_text("# old\n", encoding="utf-8")
            pristine.write_text("# old\n", encoding="utf-8")
            (pristine.parent.parent / "linker.json").write_text('{"status":"complete"}', encoding="utf-8")
            page_map = {
                "docs/a.md": {
                    "growi_path": "/wiki/docs/a",
                    "page_id": page_id,
                    "revision_id": "r1",
                }
            }
            publisher = GrowiPublisher(Client(), SimpleNamespace())
            pulled, conflicts, blocked = publisher.pull_changes(
                SimpleNamespace(wiki=wiki),
                page_map,
                {"docs"},
            )

            self.assertEqual(pulled, ["docs/a.md"])
            self.assertEqual(conflicts, [])
            self.assertEqual(blocked, set())
            self.assertEqual(page.read_text(encoding="utf-8"), "# edited\n\n[to b](a.md)\n")
            self.assertNotIn("human text outside", page.read_text(encoding="utf-8"))
            self.assertIn('"pending"', (pristine.parent.parent / "linker.json").read_text(encoding="utf-8"))
            self.assertEqual(page_map["docs/a.md"]["revision_id"], "r2")

    def test_managed_page_requires_ownership_markers(self):
        self.assertIsNone(managed_page_markdown("# manually replaced", "page/wiki/docs/a"))
        self.assertEqual(
            restore_page_links("[b](/507f191e810c19729de860ea)\n", "docs/a.md", {"507f191e810c19729de860ea": "other/b.md"}),
            "[b](../other/b.md)\n",
        )


if __name__ == "__main__":
    unittest.main()
