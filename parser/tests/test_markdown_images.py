from __future__ import annotations

import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from utils.image_unit import (
    count_image_units,
    deduplicate_image_descriptions,
    strip_image_media,
)
from utils.markdown_images import (
    _ref_alt_target,
    embed_markdown_data_urls,
    embed_markdown_images,
)

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _StubWorkers:
    async def run_network(self, fn, *args, **kwargs):
        return await fn(*args, **kwargs)


async def _describe(_data_url: str, alt_text: str) -> str:
    return f"description for {alt_text or 'unlabelled'}"


class RefParsingTests(unittest.TestCase):
    def test_markdown_and_html_forms_yield_alt_and_target(self) -> None:
        import re

        from utils.markdown_images import _IMAGE_REF_RE

        md = re.search(_IMAGE_REF_RE, "![a cat](img/cat.png)")
        html = re.search(_IMAGE_REF_RE, '<img src="img/cat.png" alt="a cat" width="30"/>')
        bare = re.search(_IMAGE_REF_RE, '<img style="x" src=img/cat.png>')

        self.assertEqual(_ref_alt_target(md), ("a cat", "img/cat.png"))
        self.assertEqual(_ref_alt_target(html), ("a cat", "img/cat.png"))
        self.assertEqual(_ref_alt_target(bare), ("", "img/cat.png"))

    def test_img_tag_without_src_is_ignored(self) -> None:
        import re

        from utils.markdown_images import _IMAGE_REF_RE

        self.assertIsNone(
            _ref_alt_target(re.search(_IMAGE_REF_RE, "<img alt='broken'>"))
        )


class EmbedHtmlImageTests(unittest.IsolatedAsyncioTestCase):
    def test_generic_embedding_converts_pandoc_html_images_to_markdown(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "media").mkdir()
            (root / "media" / "image1.png").write_bytes(_PNG_BYTES)
            markdown_path = root / "document.md"
            markdown = '<img src="media/image1.png" alt="the diagram" width="30">'

            result = embed_markdown_data_urls(markdown, markdown_path, root)

        self.assertTrue(result.startswith("![the diagram](data:image/png;base64,"))
        self.assertNotIn("media/image1.png", result)

    async def test_pandoc_style_img_tags_are_embedded_and_described(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "media").mkdir()
            (root / "media" / "image1.png").write_bytes(_PNG_BYTES)
            markdown_path = root / "document.md"
            (root / "media" / "image2.png").write_bytes(_PNG_BYTES)
            markdown = (
                'Intro.\n\n'
                '<img src="media/image1.png" alt="the diagram" style="width:2.9in" />\n\n'
                'Reused inline: <img src="media/image1.png" />\n\n'
                '![a caption](media/image2.png)\n'
            )
            markdown_path.write_text(markdown, encoding="utf-8")

            result = await embed_markdown_images(
                markdown,
                markdown_path,
                root,
                _StubWorkers(),
                _describe,
            )

        self.assertEqual(count_image_units(result), 3)
        self.assertEqual(result.count("data:image/png;base64,"), 3)
        # each unique file is described once, with its first-seen alt text
        self.assertIn("description for the diagram", result)
        self.assertIn("description for a caption", result)
        self.assertNotIn('<img src="media/image1.png"', result)  # raw refs gone
        self.assertNotIn('<img src="media/image2.png"', result)

        stripped = strip_image_media(result)
        self.assertNotIn("base64", stripped)
        self.assertIn("description for a caption", stripped)

    async def test_reused_asset_can_emit_its_description_only_once(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "same.png").write_bytes(_PNG_BYTES)
            markdown_path = root / "document.md"
            markdown = "![first](same.png)\n\n![second](same.png)"
            markdown_path.write_text(markdown, encoding="utf-8")

            result = await embed_markdown_images(
                markdown,
                markdown_path,
                root,
                _StubWorkers(),
                _describe,
                repeat_descriptions=False,
            )

        self.assertEqual(count_image_units(result), 2)
        self.assertEqual(result.count("description for first"), 1)
        self.assertNotIn("description for second", result)

    async def test_duplicate_descriptions_are_suppressed_after_context_use(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "same.png").write_bytes(_PNG_BYTES)
            markdown_path = root / "document.md"
            markdown = "![first](same.png)\n\n![second](same.png)"
            markdown_path.write_text(markdown, encoding="utf-8")
            embedded = await embed_markdown_images(
                markdown, markdown_path, root, _StubWorkers(), _describe
            )

        self.assertEqual(embedded.count("description for first"), 2)
        deduplicated = deduplicate_image_descriptions(embedded)
        self.assertEqual(deduplicated.count("description for first"), 1)
        self.assertEqual(count_image_units(deduplicated), 2)

    async def test_reused_asset_is_described_at_first_eligible_occurrence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "same.png").write_bytes(_PNG_BYTES)
            markdown_path = root / "document.md"
            markdown = "![tiny](same.png)\n\n![large](same.png)"
            markdown_path.write_text(markdown, encoding="utf-8")
            result = await embed_markdown_images(
                markdown,
                markdown_path,
                root,
                _StubWorkers(),
                _describe,
                should_describe=lambda alt: alt == "large",
            )

        self.assertNotIn("description for tiny", result)
        self.assertEqual(result.count("description for large"), 1)

    async def test_unresolvable_refs_are_left_untouched(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            markdown_path = root / "document.md"
            markdown = (
                '<img src="https://example.com/x.png">\n\n'
                '<img src="../escape.png">\n\n'
                '![missing](nope.png)\n'
            )
            markdown_path.write_text(markdown, encoding="utf-8")

            result = await embed_markdown_images(
                markdown, markdown_path, root, _StubWorkers(), _describe
            )

        self.assertEqual(result, markdown)


if __name__ == "__main__":
    unittest.main()
