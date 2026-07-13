from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from starlette.responses import JSONResponse

import app as backend_app
import mcp_server
from graph.librarian import WriteJob


class EchoRouteApp:
    async def __call__(self, scope, receive, send) -> None:
        state = scope["state"]
        response = JSONResponse(
            {
                "db": state["wiki_db"],
                "backend": state["wiki_backend_origin"],
                "prefix": state["wiki_backend_prefix"],
                "path": scope["path"],
                "root_path": scope["root_path"],
            }
        )
        await response(scope, receive, send)


class WikiRoutingAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_dir = Path(self.tmp.name)
        (self.db_dir / "alpha.sqlite").touch()
        (self.db_dir / "beta.sqlite").touch()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def make_app(self, **overrides):
        options = {
            "prefix": "/llm-wiki",
            "backend_origin": "http://backend:8000",
            "default_db": "alpha",
            "db_dir": self.db_dir,
        }
        options.update(overrides)
        return mcp_server.WikiRoutingApp(EchoRouteApp(), **options)

    async def request(self, app, path: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://mcp.test"
        ) as client:
            return await client.post(path)

    async def test_named_routes_select_isolated_wikis_concurrently(self) -> None:
        app = self.make_app()
        alpha, beta = await asyncio.gather(
            self.request(app, "/llm-wiki/alpha/mcp"),
            self.request(app, "/llm-wiki/beta/mcp/"),
        )

        self.assertEqual(alpha.status_code, 200)
        self.assertEqual(beta.status_code, 200)
        self.assertEqual(alpha.json()["db"], "alpha")
        self.assertEqual(beta.json()["db"], "beta")
        self.assertEqual(alpha.json()["path"], "/mcp")
        self.assertEqual(beta.json()["root_path"], "/llm-wiki/beta")

    async def test_legacy_route_uses_default_wiki(self) -> None:
        response = await self.request(self.make_app(), "/mcp")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["db"], "alpha")

    async def test_unknown_or_invalid_wikis_are_rejected(self) -> None:
        app = self.make_app()
        missing = await self.request(app, "/llm-wiki/missing/mcp")
        invalid = await self.request(app, "/llm-wiki/bad$name/mcp")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(invalid.status_code, 404)

    async def test_allowlist_blocks_other_existing_wikis(self) -> None:
        app = self.make_app(allowed_wikis=frozenset({"alpha"}))
        allowed = await self.request(app, "/llm-wiki/alpha/mcp")
        blocked = await self.request(app, "/llm-wiki/beta/mcp")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(blocked.status_code, 404)

    async def test_allow_new_wikis_is_explicit(self) -> None:
        app = self.make_app(allow_new_wikis=True)
        response = await self.request(app, "/llm-wiki/new-wiki/mcp")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["db"], "new-wiki")


class FastMcpRouteIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_route_completes_mcp_handshake_and_lists_tools(self) -> None:
        router = mcp_server.create_app(
            prefix="/llm-wiki",
            backend_origin="http://backend:8000",
            default_db="alpha",
            allow_new_wikis=True,
        )

        def client_factory(headers=None, timeout=None, auth=None, **kwargs):
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=router),
                headers=headers,
                timeout=timeout,
                auth=auth,
                **kwargs,
            )

        transport = StreamableHttpTransport(
            "http://mcp.test/llm-wiki/alpha/mcp",
            httpx_client_factory=client_factory,
        )
        submit = AsyncMock(
            return_value={"id": "job-route", "status": "queued", "position": 1}
        )
        with patch.object(mcp_server, "_backend_json", submit):
            async with router.app.router.lifespan_context(router.app):
                async with Client(transport) as client:
                    names = {tool.name for tool in await client.list_tools()}
                    result = await client.call_tool(
                        "queue_agent_note",
                        {
                            "body": "Routed answer",
                            "source_node_ids": ["node:1"],
                            "question": "Which wiki?",
                        },
                    )
                    await client.close()

        self.assertEqual(
            names,
            {"hybrid_search", "read_nodes", "explore_links", "queue_agent_note"},
        )
        rendered = "".join(getattr(block, "text", "") for block in result.content)
        self.assertIn("**Wiki:** `alpha`", rendered)
        submit.assert_awaited_once()


class BackendProxyTests(unittest.IsolatedAsyncioTestCase):
    def request_context(self, db: str = "alpha"):
        state = SimpleNamespace(
            wiki_db=db,
            wiki_backend_origin="http://backend:8000",
            wiki_backend_prefix="/llm-wiki",
        )
        return SimpleNamespace(state=state)

    async def test_backend_request_uses_request_scoped_wiki(self) -> None:
        calls = []

        class StubClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def request(self, method, url, **kwargs):
                calls.append((method, url, kwargs))
                return httpx.Response(200, json=[{"id": "node:1"}])

        with (
            patch.object(
                mcp_server, "get_http_request", return_value=self.request_context()
            ),
            patch.object(mcp_server.httpx, "AsyncClient", StubClient),
        ):
            result = await mcp_server._backend_json(
                "GET", "/api/search", params={"q": "locks", "limit": 5}
            )

        self.assertEqual(result, [{"id": "node:1"}])
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(
            calls[0][1], "http://backend:8000/llm-wiki/alpha/api/search"
        )
        self.assertEqual(calls[0][2]["params"], {"q": "locks", "limit": 5})

    async def test_queue_agent_note_returns_after_one_submission(self) -> None:
        submit = AsyncMock(
            return_value={
                "id": "job-1",
                "type": "create_exogenous",
                "status": "queued",
                "position": 2,
            }
        )
        with (
            patch.object(mcp_server, "_backend_json", submit),
            patch.object(
                mcp_server,
                "_request_backend",
                return_value=("alpha", "http://backend:8000", "/llm-wiki"),
            ),
        ):
            result = await mcp_server._queue_agent_note(
                "# Durable answer",
                ["node:1", "node:1", "node:2"],
                "  Why locks?  ",
            )

        submit.assert_awaited_once_with(
            "POST",
            "/api/exogenous",
            payload={
                "body": "# Durable answer",
                "source_node_ids": ["node:1", "node:2"],
                "origin": "agent:mcp",
                "question": "Why locks?",
            },
        )
        self.assertIn("`job-1`", result)
        self.assertIn("`queued`", result)
        self.assertIn("do not poll", result)

    async def test_backend_error_unwraps_fastapi_detail(self) -> None:
        class StubClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def request(self, *_args, **_kwargs):
                return httpx.Response(
                    429,
                    json={
                        "detail": {
                            "message": "write queue is full",
                            "code": "queue_full",
                        }
                    },
                )

        with (
            patch.object(
                mcp_server, "get_http_request", return_value=self.request_context()
            ),
            patch.object(mcp_server.httpx, "AsyncClient", StubClient),
        ):
            with self.assertRaises(mcp_server.BackendRequestError) as raised:
                await mcp_server._backend_json("POST", "/api/exogenous", payload={})

        self.assertEqual(str(raised.exception), "write queue is full")
        self.assertEqual(raised.exception.code, "queue_full")

    async def test_cold_backend_waits_for_ready_then_retries_submission(self) -> None:
        calls = []

        class StubClient:
            def __init__(self, **_kwargs):
                self.responses = iter(
                    [
                        httpx.Response(
                            503,
                            json={
                                "detail": {
                                    "detail": "server is not ready (starting)",
                                    "code": "not_ready",
                                }
                            },
                        ),
                        httpx.Response(
                            200, json={"ready": False, "stage": "starting"}
                        ),
                        httpx.Response(200, json={"ready": True, "stage": "ready"}),
                        httpx.Response(200, json={"id": "job-cold", "status": "queued"}),
                    ]
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def request(self, method, url, **kwargs):
                calls.append((method, url, kwargs))
                return next(self.responses)

        with (
            patch.object(
                mcp_server, "get_http_request", return_value=self.request_context()
            ),
            patch.object(mcp_server.httpx, "AsyncClient", StubClient),
            patch.object(mcp_server.asyncio, "sleep", AsyncMock()),
        ):
            result = await mcp_server._backend_json(
                "POST", "/api/exogenous", payload={"body": "answer"}
            )

        self.assertEqual(result["id"], "job-cold")
        self.assertEqual(
            [call[0:2] for call in calls],
            [
                ("POST", "http://backend:8000/llm-wiki/alpha/api/exogenous"),
                ("GET", "http://backend:8000/llm-wiki/alpha/api/ready"),
                ("GET", "http://backend:8000/llm-wiki/alpha/api/ready"),
                ("POST", "http://backend:8000/llm-wiki/alpha/api/exogenous"),
            ],
        )

    async def test_queue_agent_note_rejects_empty_body_without_submission(self) -> None:
        submit = AsyncMock()
        with patch.object(mcp_server, "_backend_json", submit):
            with self.assertRaisesRegex(ValueError, "must not be empty"):
                await mcp_server._queue_agent_note("   ")
        submit.assert_not_awaited()

    async def test_queue_full_error_is_not_hidden(self) -> None:
        submit = AsyncMock(
            side_effect=mcp_server.BackendRequestError(
                429, "write queue is full", "queue_full"
            )
        )
        with patch.object(mcp_server, "_backend_json", submit):
            with self.assertRaises(mcp_server.BackendRequestError) as raised:
                await mcp_server._queue_agent_note("Useful answer", ["node:1"])
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.code, "queue_full")


class BackendQueueContractTests(unittest.IsolatedAsyncioTestCase):
    class RecordingLibrarian:
        def __init__(self, job_id="shared-job") -> None:
            self.job_id = job_id
            self.calls = []

        async def enqueue(self, type_, payload):
            self.calls.append((type_, payload))
            return WriteJob(id=self.job_id, type=type_, payload=payload)

        def queue_position(self, _job_id):
            return 1

    async def test_exogenous_endpoint_only_enqueues_and_returns(self) -> None:
        librarian = self.RecordingLibrarian()
        payload = backend_app.ExogenousBody(
            body="Durable answer",
            source_node_ids=["node:1"],
            origin="agent:mcp",
            question="Why?",
        )

        with patch.object(backend_app, "writes", return_value=librarian):
            result = await backend_app.create_exogenous(payload)

        self.assertEqual(result["id"], "shared-job")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["position"], 1)
        self.assertEqual(
            librarian.calls,
            [
                (
                    "create_exogenous",
                    {
                        "body": "Durable answer",
                        "source_node_ids": ["node:1"],
                        "origin": "agent:mcp",
                        "question": "Why?",
                    },
                )
            ],
        )

    async def test_backend_route_selects_each_wikis_own_queue(self) -> None:
        alpha = self.RecordingLibrarian("alpha-job")
        beta = self.RecordingLibrarian("beta-job")
        backend_app.STACKS.update(
            {
                "alpha": {"librarian": alpha},
                "beta": {"librarian": beta},
            }
        )
        backend_app.stages.update({"alpha": "ready", "beta": "ready"})

        try:
            transport = httpx.ASGITransport(app=backend_app.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://backend.test"
            ) as client:
                alpha_response, beta_response = await asyncio.gather(
                    client.post(
                        "/llm-wiki/alpha/api/exogenous",
                        json={"body": "Alpha note", "source_node_ids": []},
                    ),
                    client.post(
                        "/llm-wiki/beta/api/exogenous",
                        json={"body": "Beta note", "source_node_ids": []},
                    ),
                )
        finally:
            backend_app.STACKS.pop("alpha", None)
            backend_app.STACKS.pop("beta", None)
            backend_app.stages.pop("alpha", None)
            backend_app.stages.pop("beta", None)

        self.assertEqual(alpha_response.json()["id"], "alpha-job")
        self.assertEqual(beta_response.json()["id"], "beta-job")
        self.assertEqual(alpha.calls[0][1]["body"], "Alpha note")
        self.assertEqual(beta.calls[0][1]["body"], "Beta note")


class BackendReadContractTests(unittest.IsolatedAsyncioTestCase):
    class FakeNode:
        def model_dump(self):
            return {
                "id": "node:1",
                "title": "Locks",
                "summary": "Lock ordering",
                "type": "endogenous",
                "cluster": "Concurrency",
                "body": "large-body-must-not-cross-the-proxy",
            }

    async def test_link_limit_is_applied_inside_backend(self) -> None:
        researcher = SimpleNamespace(
            follow_link=AsyncMock(
                return_value=[({"label": "reference"}, self.FakeNode())]
            )
        )

        with patch.object(backend_app, "reads", return_value=researcher):
            result = await backend_app.node_links("node:1", limit=7, compact=True)

        self.assertNotIn("body", result[0]["node"])
        self.assertEqual(result[0]["node"]["id"], "node:1")
        researcher.follow_link.assert_awaited_once_with(
            "node:1", label=None, direction="both", limit=7
        )

    async def test_compact_search_omits_node_body(self) -> None:
        researcher = SimpleNamespace(search=AsyncMock(return_value=[self.FakeNode()]))

        with patch.object(backend_app, "reads", return_value=researcher):
            result = await backend_app.search("locks", limit=3, compact=True)

        self.assertEqual(result[0]["id"], "node:1")
        self.assertNotIn("body", result[0])
        researcher.search.assert_awaited_once_with("locks", 3)


if __name__ == "__main__":
    unittest.main()
