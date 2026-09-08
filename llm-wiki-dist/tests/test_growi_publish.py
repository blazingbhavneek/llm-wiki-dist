from __future__ import annotations

import unittest

import httpx

from graph.growi import GrowiClient, merge_marked_sections, publish_pages


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


if __name__ == "__main__":
    unittest.main()
