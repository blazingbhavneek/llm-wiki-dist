from __future__ import annotations

import unittest

import httpx

from graph.growi import GrowiClient, GrowiPage


class GrowiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_requests_use_v3_and_bearer_auth(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/healthcheck"):
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(
                200,
                json={
                    "page": {
                        "_id": "p1",
                        "path": "/docs/a",
                        "revision": "r1",
                        "body": "# A",
                    }
                },
            )

        client = GrowiClient(
            "https://wiki.example/",
            "secret-token",
            transport=httpx.MockTransport(handler),
        )
        self.assertTrue(await client.health())
        page = await client.get_page(path="/docs/a")
        self.assertIsInstance(page, GrowiPage)
        self.assertEqual(page.revision_id, "r1")
        self.assertEqual(requests[0].url.path, "/_api/v3/healthcheck")
        self.assertEqual(requests[1].url.params["path"], "/docs/a")
        self.assertEqual(requests[1].headers["authorization"], "Bearer secret-token")

    async def test_list_pages_returns_cursor_and_normalizes_documents(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {"_id": "p1", "path": "/a", "revision": "r1", "updatedAt": "now"}
                    ],
                    "nextCursor": "next",
                },
            )

        client = GrowiClient("https://wiki.example", "token", transport=httpx.MockTransport(handler))
        pages, cursor = await client.list_pages()
        self.assertEqual([page.page_id for page in pages], ["p1"])
        self.assertEqual(cursor, "next")

    async def test_writes_are_not_enabled_in_read_only_package(self):
        client = GrowiClient("https://wiki.example", "token")
        with self.assertRaises(NotImplementedError):
            await client.create_page("/a", "body")


if __name__ == "__main__":
    unittest.main()
