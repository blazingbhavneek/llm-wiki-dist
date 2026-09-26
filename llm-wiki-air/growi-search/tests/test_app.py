"""API tests with fake services injected via app.state. No GROWI, no models."""

import asyncio
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

os.environ["GROWI_URL"] = "http://growi.test"
os.environ["GROWI_TOKEN"] = "SEKRET-9f3a"

import httpx
from fastapi.testclient import TestClient

import app as appmod
from config import Settings
from models import AgentAnswer, WikiPage

TOKEN = "SEKRET-9f3a"
ID1 = "507f1f77bcf86cd799439011"


def mock_transport(callback=None):
    callback = callback or (lambda request: httpx.Response(200, json={"ok": True}))

    def handler(request):
        return callback(request)

    return httpx.MockTransport(handler)


class FakeResearcher:
    def __init__(self, answer=None, error=None):
        self.answers = [answer or AgentAnswer(question="q", answer="ANSWER-TEXT", cited_node_ids=[ID1], steps=2)]
        self.error = error
        self.calls = []
        self.search_limit = None
        self.overrides_passed = None

    def validate_overrides(self, overrides):
        if overrides and "chat_base_url" in overrides and "evil" in overrides["chat_base_url"]:
            raise ValueError("LLM endpoint host is not permitted")

    async def fast_search(self, query, limit):
        self.calls.append(("search", query))
        self.search_limit = limit
        node = WikiPage(id=ID1, path="/Moove/001-概要", title="001-概要", body="")
        return [{"node": node, "score": 0.5, "evidence": [{"page_id": ID1, "field": "growi_es", "text": "スニペット"}]}]

    async def node_view(self, page_id):
        self.calls.append(("node", page_id))
        if page_id == "missing":
            return None
        return {**WikiPage(id=page_id, path="/Moove/x", title="x", body="本文").public_dict(), "links": [{"id": "edge:1", "source_node_id": page_id, "target_node_id": ID1, "label": "L"}]}

    async def children(self, *, page_id=None, path=None):
        self.calls.append(("children", page_id, path))
        return [
            WikiPage(id="507f1f77bcf86cd799439099", path="/Moove/x/00-目次", title="00-目次"),
            WikiPage(id=ID1, path="/Moove/x/child", title="child"),
        ]

    async def document_view(self, path):
        self.calls.append(("document", path))
        return {
            "path": path,
            "title": "doc",
            "pages": [{**WikiPage(id=ID1, path=f"{path}/child", title="child").public_dict(), "summary": "card summary", "keywords": ["one"]}],
        }

    async def ask(self, question, on_event=None, overrides=None, stop_event=None, context="", cited_node_ids=None):
        self.calls.append(("ask", question))
        self.overrides_passed = overrides
        if self.error:
            raise self.error
        if on_event:
            on_event({"type": "start", "question": question})
            on_event({"type": "search", "phase": "main", "query": question})
        return self.answers[0]


def make_app(settings=None, researcher=None, transport=None):
    settings = settings or Settings(growi_url="http://growi.test", growi_token=TOKEN, prefix="")
    fake = researcher or FakeResearcher()
    # Inject through create_app so lifespan keeps the fake.
    application = appmod.create_app(settings, transport=transport or mock_transport(), researcher=fake)
    return application, fake


def parse_sse(text):
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


class Readiness(unittest.TestCase):
    def test_capabilities(self):
        application, _ = make_app()
        with TestClient(application) as client:
            data = client.get("/api/ready").json()
            self.assertEqual(
                set(data), {"ready", "growi", "search", "llm", "reranker", "embedder", "jev", "root_path"}
            )
            self.assertTrue(data["growi"])
            self.assertFalse(data["llm"])  # no chat_base_url in test settings
            self.assertFalse(data["reranker"])

    def test_token_never_leaks(self):
        application, _ = make_app()
        with TestClient(application) as client:
            for url in ("/api/ready", "/api/growi", "/health", "/api/health"):
                self.assertNotIn(TOKEN, client.get(url).text, url)
            self.assertNotIn(TOKEN, client.post("/api/ask", json={"question": "x"}).text)


class Search(unittest.TestCase):
    def test_empty_query(self):
        application, fake = make_app()
        with TestClient(application) as client:
            self.assertEqual(client.get("/api/search?q=").json(), [])
            self.assertEqual(fake.calls, [])

    def test_clamps_limit(self):
        application, fake = make_app()
        with TestClient(application) as client:
            out = client.get("/api/search?q=x&limit=9999").json()
            self.assertEqual(fake.search_limit, 50)
            # page-compatible shape: node fields + score/evidence, body empty
            self.assertEqual(out[0]["id"], ID1)
            self.assertEqual(out[0]["body"], "")
            self.assertEqual(out[0]["evidence"][0]["text"], "スニペット")
            self.assertIsInstance(out[0]["score"], float)
            client.get("/api/search?q=x&limit=bogus")
            self.assertEqual(fake.search_limit, 12)


class Pages(unittest.TestCase):
    def test_children_exactly_one(self):
        application, _ = make_app()
        with TestClient(application) as client:
            self.assertEqual(client.get("/api/pages/children").status_code, 400)
            self.assertEqual(client.get("/api/pages/children?page_id=a&path=/b").status_code, 400)
            ok = client.get("/api/pages/children?path=/Moove")
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.json()["children"][0]["path"], "/Moove/x/child")

    def test_document_view(self):
        application, _ = make_app()
        with TestClient(application) as client:
            out = client.get("/api/document?path=/Moove/doc")
            self.assertEqual(out.status_code, 200)
            self.assertEqual(out.json()["pages"][0]["summary"], "card summary")
            self.assertEqual(out.json()["pages"][0]["keywords"], ["one"])

    def test_node_includes_links_without_second_fetch(self):
        application, fake = make_app()
        with TestClient(application) as client:
            node = client.get(f"/api/node/{ID1}").json()
            self.assertEqual(node["links"][0]["target_node_id"], ID1)
            self.assertEqual([c for c in fake.calls if c[0] == "node"], [("node", ID1)])

    def test_page_404(self):
        application, _ = make_app()
        with TestClient(application) as client:
            res = client.get("/api/node/missing")
            self.assertEqual(res.status_code, 404)
            self.assertEqual(res.json()["code"], "page_not_found")


class Ask(unittest.TestCase):
    def test_json_contract(self):
        application, _ = make_app()
        with TestClient(application) as client:
            res = client.post("/api/ask", json={"question": "質問", "overrides": {"subagent_count": 1}})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(
                res.json(),
                {"question": "q", "answer": "ANSWER-TEXT", "cited_node_ids": [ID1], "cited_nodes": [], "steps": 2},
            )

    def test_empty_question_and_bad_override(self):
        application, _ = make_app()
        with TestClient(application) as client:
            self.assertEqual(client.post("/api/ask", json={"question": " "}).status_code, 400)
            bad = client.post("/api/ask", json={"question": "x", "overrides": {"chat_base_url": "http://evil.example"}})
            self.assertEqual(bad.status_code, 400)
            # Also blocked on the streaming route.
            self.assertEqual(client.post("/api/ask/stream", json={"question": "x", "overrides": {"chat_base_url": "http://evil.example"}}).status_code, 400)

    def test_growi_error_mapping(self):
        from growi_client import GrowiAPIError

        for status, expected in ((401, 502), (0, 503), (500, 502)):
            fake = FakeResearcher(error=GrowiAPIError(status, "GET", "/x", "boom"))
            application, _ = make_app(researcher=fake)
            with TestClient(application) as client:
                res = client.post("/api/ask", json={"question": "x"})
                self.assertEqual(res.status_code, expected, status)
                self.assertIn("code", res.json())


class SSE(unittest.TestCase):
    def test_stream_events(self):
        application, _ = make_app()
        with TestClient(application) as client:
            with client.stream("POST", "/api/ask/stream", json={"question": "質問"}) as res:
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.headers["content-type"], "text/event-stream; charset=utf-8")
                body = "".join(res.iter_text())
        events = parse_sse(body)
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "run")
        self.assertIn("search", types)
        self.assertIn("answer", types)
        self.assertEqual(types[-1], "done")
        answer = next(e for e in events if e["type"] == "answer")
        self.assertEqual(answer["answer"], "ANSWER-TEXT")
        self.assertTrue(events[0]["run_id"])

    def test_stop_endpoint(self):
        application, _ = make_app()
        with TestClient(application) as client:
            first = threading.Event()
            second = threading.Event()
            application.state.runs["run-a"] = (None, first)
            application.state.runs["run-b"] = (None, second)
            res = client.post("/api/agent-runs/run-a/stop")
            self.assertEqual(res.status_code, 200)
            self.assertTrue(first.is_set())
            self.assertFalse(second.is_set())
            again = client.post("/api/agent-runs/run-a/stop")  # repeat-safe
            self.assertEqual(again.status_code, 200)
            self.assertEqual(client.post("/api/agent-runs/zzz/stop").status_code, 404)

    def test_disconnect_sets_stop(self):
        import queue as _q

        out: _q.Queue = _q.Queue()
        stop = threading.Event()
        out.put({"type": "answer"})
        out.put(appmod.SENTINEL)

        async def never():
            return True  # immediately disconnected

        async def collect():
            return [frame async for frame in appmod.sse_frames(out, "r1", stop, never)]

        frames = asyncio.run(collect())
        self.assertTrue(stop.is_set())
        self.assertIn('{"type": "done"}', frames[-1])

    def test_queue_timeout_returns_none(self):
        import queue as _q

        self.assertIsNone(appmod._queue_get(_q.Queue(), 0.05))


class NewReadOnlyRoutes(unittest.TestCase):
    ATTACHMENT_ID = "507f1f77bcf86cd799439026"

    def test_settings_hides_secrets(self):
        application, _ = make_app()
        with TestClient(application) as client:
            data = client.get("/api/settings").json()
            self.assertIn("chat_model", data)
            for secret in ("growi_token", "chat_api_key", "rerank_api_key"):
                self.assertNotIn(secret, data)

    def test_attachment_proxy_and_validation(self):
        def handler(request):
            if request.url.path.endswith(self.ATTACHMENT_ID):
                return httpx.Response(200, content=b"PNG", headers={"content-type": "image/png"})
            if request.url.path.endswith("507f1f77bcf86cd799439027"):
                return httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"})
            return httpx.Response(200, json={"ok": True})

        application, _ = make_app(transport=mock_transport(handler))
        with TestClient(application) as client:
            response = client.get(f"/api/attachment/{self.ATTACHMENT_ID}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"PNG")
            self.assertEqual(response.headers["content-type"], "image/png")
            self.assertEqual(client.get("/api/attachment/not-an-id").status_code, 404)
            html = client.get("/api/attachment/507f1f77bcf86cd799439027")
            self.assertEqual(html.status_code, 502)


class StaticAndPrefix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / "index.html").write_text("<html>SPA</html>")
        cls._real_dist = appmod.FRONTEND_DIST
        appmod.FRONTEND_DIST = cls.tmp

    @classmethod
    def tearDownClass(cls):
        appmod.FRONTEND_DIST = cls._real_dist
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_spa_fallback_and_api_404(self):
        application, _ = make_app()
        with TestClient(application) as client:
            self.assertIn("SPA", client.get("/").text)
            res = client.get("/some/client/route")
            self.assertEqual(res.status_code, 404)  # static mount: no html5 rewrite
            api404 = client.get("/api/definitely-not-here")
            self.assertEqual(api404.status_code, 404)
            self.assertNotIn("SPA", api404.text)

    def test_cors_not_wildcard(self):
        application, _ = make_app()
        with TestClient(application) as client:
            res = client.get("/api/ready", headers={"Origin": "http://evil.example"})
            self.assertNotEqual(res.headers.get("access-control-allow-origin"), "*")

    def test_prefix(self):
        settings = Settings(growi_url="http://growi.test", growi_token=TOKEN, prefix="/growi-search")
        application, _ = make_app(settings=settings)
        with TestClient(application) as client:
            self.assertEqual(client.get("/growi-search/api/ready").status_code, 200)
            self.assertEqual(client.get("/growi-search/health").status_code, 200)
            redirect = client.get("/growi-search", follow_redirects=False)
            self.assertEqual(redirect.status_code, 307)
            self.assertEqual(redirect.headers["location"], "/growi-search/")
            # Prefix must strip only once: doubled prefix is not rewritten twice.
            self.assertEqual(client.get("/growi-search/growi-search/api/ready").status_code, 404)


if __name__ == "__main__":
    unittest.main()
