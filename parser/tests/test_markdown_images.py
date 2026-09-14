from __future__ import annotations

import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from utils.image_unit import count_image_units, strip_image_media
from utils.markdown_images import _ref_alt_target, embed_markdown_images

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
