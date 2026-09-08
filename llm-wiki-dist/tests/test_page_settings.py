from __future__ import annotations

import unittest
from unittest import mock

from graph.core import NodeType, Settings


class PageSettingsTests(unittest.TestCase):
    def test_page_features_are_inert_by_default(self) -> None:
        settings = Settings()
        self.assertEqual(settings.ingest_mode, "chunks")
        self.assertFalse(settings.page_stitch)
        self.assertEqual(settings.vector_backend, "sqlite")
        self.assertEqual(NodeType.page.value, "page")

    def test_page_switches_are_read_from_environment(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "WIKI_INGEST_MODE": "pages",
                "WIKI_PAGE_STITCH": "1",
                "WIKI_VECTOR_BACKEND": "qdrant",
                "QDRANT_URL": "http://qdrant:6333",
            },
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.ingest_mode, "pages")
        self.assertTrue(settings.page_stitch)
        self.assertEqual(settings.vector_backend, "qdrant")
        self.assertEqual(settings.qdrant_url, "http://qdrant:6333")


if __name__ == "__main__":
    unittest.main()
