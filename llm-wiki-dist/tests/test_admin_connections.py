from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app


class AdminConnectionEndpointTests(unittest.TestCase):
    def test_crud_redacts_credentials_and_detach_does_not_call_growi(self):
        async def run():
            with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
                os.environ, {"WIKI_SECRET_KEY": "test-secret"}
            ):
                with mock.patch.object(app, "GROWI_ENABLED", True), mock.patch.object(
                    app, "GROWI_ENGINE_DB", Path(temporary) / "engine.sqlite"
                ), mock.patch.object(app, "_GROWI_REGISTRY", None):
                    created = await app.admin_register_connection(
                        "docs",
                        app.GrowiConnectionBody(
                            url="https://growi.example",
                            api_token="secret-token",
                        ),
                        app.ADMIN_PASSWORD,
                    )
                    self.assertNotIn("secret-token", str(created))
                    listed = await app.admin_list_connections(app.ADMIN_PASSWORD)
                    self.assertEqual(len(listed["connections"]), 1)
                    patched = await app.admin_patch_connection(
                        "docs",
                        app.GrowiConnectionPatch(url="https://new.example"),
                        app.ADMIN_PASSWORD,
                    )
                    self.assertEqual(patched["url"], "https://new.example")
                    deleted = await app.admin_delete_connection(
                        "docs", app.ADMIN_PASSWORD
                    )
                    self.assertTrue(deleted["growi_untouched"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
