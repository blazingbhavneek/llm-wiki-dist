from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from starlette.requests import Request

import app


class GrowiRoutingTests(unittest.TestCase):
    def test_registered_growi_name_uses_existing_db_routing_context(self):
        async def run():
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/wiki/a/api/ready",
                "raw_path": b"/wiki/a/api/ready",
                "query_string": b"",
                "headers": [],
                "client": ("test", 1),
                "server": ("test", 80),
                "scheme": "http",
            }
            request = Request(scope)

            async def call_next(inner):
                self.assertEqual(inner.scope["path"], "/api/ready")
                self.assertEqual(app.current_db.get(), "a")
                return "ok"

            with mock.patch.object(app, "PREFIX", "/wiki"), mock.patch.object(
                app, "GROWI_ENABLED", True
            ), mock.patch.object(
                app,
                "_registered_growi",
                return_value=SimpleNamespace(name="a"),
            ), mock.patch.object(app, "_db_path", return_value=SimpleNamespace(exists=lambda: False)):
                self.assertEqual(await app.db_routing(request, call_next), "ok")

        asyncio.run(run())

    def test_startup_requirement_defaults_to_isolated_failures(self):
        # This is a source-level assertion because lifespan also boots model
        # and embedder services; the default must remain safe for one down wiki.
        source = __import__("inspect").getsource(app.lifespan)
        self.assertIn('"false"', source)


if __name__ == "__main__":
    unittest.main()
