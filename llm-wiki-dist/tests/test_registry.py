from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from graph.registry import ConnectionRegistry, GrowiPageIndex


class RegistryTests(unittest.TestCase):
    def test_credentials_are_encrypted_and_public_shape_is_redacted(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"WIKI_SECRET_KEY": "test-secret"}
        ):
            registry = ConnectionRegistry(Path(temporary) / "engine.sqlite")
            connection = registry.register(
                name="docs",
                url="https://growi.example",
                api_token="very-secret-token",
                mongo_uri="mongodb://private",
            )
            loaded = registry.get("docs")
            self.assertEqual(loaded.api_token, "very-secret-token")
            self.assertEqual(loaded.mongo_uri, "mongodb://private")
            with sqlite3.connect(Path(temporary) / "engine.sqlite") as db:
                raw = db.execute(
                    "SELECT api_token_enc,mongo_uri_enc FROM growi_connections"
                ).fetchone()
            self.assertNotIn("very-secret-token", raw[0])
            self.assertNotIn("mongodb://private", raw[1])
            public = registry.public(connection)
            self.assertEqual(public["api_token"], "")
            self.assertFalse("very-secret-token" in str(public))
            self.assertTrue(public["has_token"])

    def test_missing_secret_key_refuses_to_store(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {}, clear=True
        ):
            registry = ConnectionRegistry(Path(temporary) / "engine.sqlite")
            with self.assertRaises(RuntimeError):
                registry.register(name="docs", url="https://growi.example", api_token="x")

    def test_page_index_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ, {"WIKI_SECRET_KEY": "key"}
        ):
            registry = ConnectionRegistry(Path(temporary) / "engine.sqlite")
            registry.register(name="docs", url="https://growi.example", api_token="x")
            registry.upsert_page(
                GrowiPageIndex(
                    name="docs", page_id="p", revision_id="r", path="/p"
                )
            )
            self.assertEqual(registry.page_count("docs"), 1)
            self.assertEqual(registry.pages("docs")[0].revision_id, "r")


if __name__ == "__main__":
    unittest.main()
