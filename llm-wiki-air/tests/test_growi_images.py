import asyncio
import base64
import unittest

import httpx

from graph.growi.client import (
    GrowiClient,
    GrowiPage,
    managed_page_markdown,
    publish_pages,
    wrap_page,
)


class GrowiImagePublishTests(unittest.TestCase):
    def test_publish_removes_inline_code_but_preserves_code_blocks(self) -> None:
        body = """文章中の `AA29` と `重要語`。

```text
keep `inside fence`
```

    keep `inside indented block`
"""

        from graph.growi.client import _growi_markdown

        published = _growi_markdown(body)
        self.assertIn("文章中の AA29 と 重要語。", published)
        self.assertIn("keep `inside fence`", published)
        self.assertIn("    keep `inside indented block`", published)

    def test_attachment_upload_uses_growi_multipart_api(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": {"attachment": {"_id": "image1"}}})

        client = GrowiClient("http://growi.invalid", "token", transport=httpx.MockTransport(handler))
        path = asyncio.run(client.upload_attachment("page1", "image.jpg", b"image bytes", "image/jpeg"))

        self.assertEqual(path, "/attachment/image1")
        self.assertEqual(requests[0].url.path, "/_api/v3/attachment")
        self.assertIn(b'name="page_id"', requests[0].content)
        self.assertIn(b'filename="image.jpg"', requests[0].content)

    def test_embedded_image_is_uploaded_once_and_rewritten_with_japanese_alt_text(self) -> None:
        payload = base64.b64encode(b"image bytes").decode("ascii")
        unit = (
            '<image-unit><image-media><img src="data:image/jpeg;base64,' + payload
            + '" alt=""></image-media><image-description>処理の流れを示す図</image-description></image-unit>'
        )

        class Client:
            def __init__(self) -> None:
                self.created_body = ""
                self.updated_body = ""
                self.uploads: list[tuple[str, str, bytes, str]] = []

            async def get_page(self, **_kwargs):
                return None

            async def create_page(self, path, body):
                self.created_body = body
                return GrowiPage(page_id="page1", revision_id="rev1", path=path, body=body)

            async def list_attachments(self, _page_id):
                return {}

            async def upload_attachment(self, page_id, name, content, mime):
                self.uploads.append((page_id, name, content, mime))
                return "/attachment/image1"

            async def update_page(self, _page_id, _revision_id, body):
                self.updated_body = body
                return GrowiPage(page_id="page1", revision_id="rev2", path="/docs/page", body=body)

        client = Client()
        body = wrap_page(
            f"<!-- sheet-context:start -->\n{unit}\n\n{unit}\n<!-- sheet-context:end -->",
            page_id="page/docs/page",
            ranges=[],
        )
        asyncio.run(publish_pages(
            client,
            [{"path": "/docs/page", "local_path": "page.md", "body": body}],
            mode="attach",
            write_path="/docs",
        ))

        self.assertNotIn("data:image", client.created_body)
        self.assertEqual(len(client.uploads), 1)
        self.assertEqual(client.uploads[0][2:], (b"image bytes", "image/jpeg"))
        self.assertEqual(client.created_body.count("![処理の流れを示す図]()"), 2)
        self.assertEqual(client.updated_body.count("![処理の流れを示す図](/attachment/image1)"), 2)
        self.assertNotIn("<!--", client.created_body)
        self.assertNotIn("<!--", client.updated_body)
        self.assertIsNotNone(
            managed_page_markdown(client.updated_body, "page/docs/page")
        )
        self.assertNotIn("<image-unit>", client.updated_body)

    def test_image_description_is_markdown_alt_text_when_media_is_unavailable(self) -> None:
        unit = "<image-unit><image-description>経営目標へ至る実行計画</image-description></image-unit>"

        class Client:
            def __init__(self) -> None:
                self.created_body = ""
                self.updated_body = ""

            async def get_page(self, **_kwargs):
                return None

            async def create_page(self, path, body):
                self.created_body = body
                return GrowiPage(page_id="page1", revision_id="rev1", path=path, body=body)

            async def list_attachments(self, _page_id):
                return {}

            async def update_page(self, _page_id, _revision_id, body):
                self.updated_body = body
                return GrowiPage(page_id="page1", revision_id="rev2", path="/docs/page", body=body)

        client = Client()
        body = wrap_page(unit, page_id="page/docs/page", ranges=[])
        asyncio.run(publish_pages(
            client,
            [{"path": "/docs/page", "local_path": "page.md", "body": body}],
            mode="attach",
            write_path="/docs",
        ))

        published = client.updated_body or client.created_body
        self.assertIn("![経営目標へ至る実行計画]()", published)
        self.assertNotIn("llm-wiki-image-description", published)
        self.assertNotIn("*経営目標へ至る実行計画*", published)
        self.assertNotIn("<image-description>", published)


if __name__ == "__main__":
    unittest.main()
