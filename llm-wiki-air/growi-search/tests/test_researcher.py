"""Researcher tests with fake GROWI / reranker / LLM. No network, no DB."""

import os
import json
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Lock
from unittest import mock

os.environ.setdefault("GROWI_URL", "http://growi.test")
os.environ.setdefault("GROWI_TOKEN", "t")

import httpx

import gateway as G
import researcher as R
from config import Settings
from growi_client import GrowiAPIError, SearchHit
from models import AgentAnswer, WikiPage
from prompts import JEV_QUERY_REWRITE_PROMPT
from prompts import JEV_TOC_SUMMARY_PROMPT

ID1 = "507f1f77bcf86cd799439011"
ID2 = "507f1f77bcf86cd799439012"
ID3 = "507f1f77bcf86cd799439013"
ID4 = "507f1f77bcf86cd799439014"
ID5 = "507f1f77bcf86cd799439015"


def page_of(pid, path, body="", **kw):
    return WikiPage(id=pid, path=path, title=path.rsplit("/", 1)[-1], body=body, **kw)


def hit(pid, path, snippet, rank):
    p = page_of(pid, path)
    p.snippet = snippet
    return SearchHit(p, snippet, rank)


PAGES = {
    ID1: page_of(ID1, "/Moove/001-概要", "# 概要\n概要本文\n## 関連資料\n[B](/" + ID2 + ") — 詳細資料\n[C](/Moove/three)\n"),
    ID2: page_of(ID2, "/Moove/002-詳細", "# 詳細\n詳細本文\n"),
    ID3: page_of(ID3, "/Moove/three", "# Three\n本文three\n"),
    ID4: page_of(ID4, "/Moove/four", "# Four\n" + ("长篇内容 " * 700)),
    ID5: page_of(ID5, "/Moove/five", "# Five\n本文five\n"),
}


class FakeClient:
    url = "http://growi.test"

    def __init__(self):
        self.search_calls = []
        self.page_calls = []
        self.fail_pages: set[str] = set()

    def search_pages(self, query, *, path, limit, offset=0):
        self.search_calls.append((query, path, limit))
        return [hit(ID1, "/Moove/001-概要", "概要スニペット", 1), hit(ID2, "/Moove/002-詳細", "詳細スニペット", 2), hit(ID5, "/Moove/five", " five snippet", 3)]

    def get_page(self, *, page_id=None, path=None):
        self.page_calls.append(page_id or path)
        key = page_id or path
        if key in self.fail_pages:
            raise GrowiAPIError(500, "GET", "/_api/v3/page", "boom")
        return PAGES.get(key)

    def list_children(self, *, page_id=None, path=None):
        self.page_calls.append(page_id or path)
        return [PAGES[ID3]]


class IndexClient(FakeClient):
    ROOT_ID = "507f1f77bcf86cd799439021"
    DOC_A_ID = "507f1f77bcf86cd799439022"
    DOC_B_ID = "507f1f77bcf86cd799439023"

    def __init__(self):
        super().__init__()
        self.index_pages = {
            self.ROOT_ID: page_of(
                self.ROOT_ID,
                "/Moove/00-目次",
                '<span hidden data-llm-wiki-index="root"></span>\n'
                f"- [A](/{self.DOC_A_ID}) — A summary\n"
                f"- [B](/{self.DOC_B_ID}) — B summary\n",
            ),
            self.DOC_A_ID: page_of(
                self.DOC_A_ID,
                "/Moove/A/00-目次",
                '<span hidden data-llm-wiki-index="document"></span>\n'
                f"- [Alpha](/6aab1ff0d4652631606ec418) — alpha\n"
                "  - キーワード: alpha, beta\n",
            ),
            self.DOC_B_ID: page_of(
                self.DOC_B_ID,
                "/Moove/B/00-目次",
                '<span hidden data-llm-wiki-index="document"></span>\n'
                f"- [Beta](/6aab1ff0d4652631606ec419) — beta\n"
                "  - キーワード: beta, gamma\n",
            ),
        }

    def get_page(self, *, page_id=None, path=None):
        self.page_calls.append(page_id or path)
        key = page_id or path
        if key == "/Moove/00-目次":
            return self.index_pages[self.ROOT_ID]
        if key == "/Moove/A/00-目次":
            return self.index_pages[self.DOC_A_ID]
        if key == "/Moove/B/00-目次":
            return self.index_pages[self.DOC_B_ID]
        if key == self.DOC_A_ID:
            return self.index_pages[self.DOC_A_ID]
        if key == self.DOC_B_ID:
            return self.index_pages[self.DOC_B_ID]
        if key in {"6aab1ff0d4652631606ec418", "6aab1ff0d4652631606ec419"}:
            return page_of(key, f"/Moove/{key}", body=f"# {key}\nbody")
        return super().get_page(page_id=page_id, path=path)

    def search_pages(self, query, *, path, limit, offset=0):
        return [
            hit("507f1f77bcf86cd799439024", "/Moove/00-目次", "index should be hidden", 1),
        ]


class FakeReranker:
    """Prefers documents containing ``prefer``; reverses order otherwise."""

    def __init__(self, prefer=None):
        self.prefer = prefer
        self.calls = 0

    def score(self, query, texts):
        self.calls += 1
        if self.prefer is None:
            return list(reversed(range(len(texts))))
        return [float(t.count(self.prefer)) for t in texts]


class IndexMapTests(unittest.TestCase):
    def make_index(self, reranker=None):
        client = IndexClient()
        settings = Settings(growi_url="http://growi.test", growi_token="t", growi_root_path="/Moove", index_cache_ttl=120)
        index = R.IndexMap(client, settings, None, reranker)
        return client, settings, index

    def test_term_overlap_and_reranker(self):
        _client, _settings, index = self.make_index()
        self.assertEqual(index.rank("gamma", 2)[0]["node"].title, "Beta")
        _client, _settings, index = self.make_index(FakeReranker(prefer="alpha"))
        self.assertEqual(index.rank("anything", 2)[0]["node"].title, "Alpha")

    def test_refresh_reads_root_and_each_document_once(self):
        client, _settings, index = self.make_index()
        index.rank("beta", 2)
        self.assertEqual(len(client.page_calls), 3)
        index.rank("alpha", 2)
        self.assertEqual(len(client.page_calls), 3)

    def test_fast_search_merges_map_and_hides_index_pages(self):
        client, settings, index = self.make_index()
        session = R.ResearchSession(client, settings, R.PageCache(120, 256), None, index_map=index)
        ids = [item["node"].id for item in session.fast_search("beta", 5)]
        self.assertNotIn(IndexClient.ROOT_ID, ids)
        self.assertIn("6aab1ff0d4652631606ec419", ids)

    def test_route_emits_map_evidence(self):
        client, settings, index = self.make_index()
        session = R.ResearchSession(client, settings, R.PageCache(120, 256), None, index_map=index)
        session.llm = FakeLLM(route_mode="deep")
        events = []
        session._try_route("beta", events.append, None, "beta")
        self.assertTrue(any(event.get("type") == "map" for event in events))
        self.assertIn("index_map", session.llm.structured_calls[-1][1])

    def test_citations_use_pages_read_during_the_run(self):
        session = make_session()
        session.read_node(ID1)
        session._try_route = lambda *_args: AgentAnswer(question="q", answer="a", cited_node_ids=[ID1], steps=1)
        answer = session.ask("q", None, None)
        self.assertEqual(answer.cited_nodes[0]["title"], PAGES[ID1].title)


class LinkKinds(unittest.TestCase):
    def test_follow_link_filters_navigation_but_links_for_keeps_it(self):
        nav_id = "507f1f77bcf86cd799439025"
        PAGES[nav_id] = page_of(
            nav_id,
            "/Moove/nav",
            f"前のページ: [a](/{ID2}) ｜ 次のページ: [b](/{ID3})\n- [t](/{ID5}) — related\n",
        )
        try:
            session = make_session()
            all_links = session.links_for(PAGES[nav_id])
            followed = session.follow_link(nav_id)
            self.assertEqual([link.kind for link in all_links], ["nav", "nav", "markdown"])
            self.assertEqual([link.kind for link in followed], ["markdown"])
        finally:
            PAGES.pop(nav_id, None)


class FakeLLM:
    def __init__(self, route_mode="deep", answer="回答です\n\n引用:\n" + ID1):
        self.route_mode = route_mode
        self.answer = answer
        self.complete_calls = []
        self.structured_calls = []

    def complete(self, prompt, payload):
        self.complete_calls.append((prompt, payload))
        return self.answer

    def complete_structured(self, prompt, payload, schema):
        self.structured_calls.append((prompt, payload))
        return schema(mode=self.route_mode, reason="test")


def make_session(settings=None, client=None, reranker=None, llm=None):
    settings = settings or Settings(growi_url="http://growi.test", growi_token="t", growi_root_path="/Moove")
    session = R.ResearchSession(client or FakeClient(), settings, R.PageCache(120, 256), reranker)
    session.llm = llm or FakeLLM()
    return session


class Ranking(unittest.TestCase):
    def test_es_order_without_reranker(self):
        session = make_session()
        results = session.fast_search("q", 5)
        self.assertEqual([r["node"].id for r in results], [ID1, ID2, ID5])

    def test_reranker_reorders(self):
        session = make_session(reranker=FakeReranker(prefer="詳細"))
        results = session.fast_search("q", 5)
        self.assertEqual(results[0]["node"].id, ID2)

    def test_fast_search_hydrates_no_pages(self):
        client = FakeClient()
        session = make_session(client=client)
        session.fast_search("q", 5)
        self.assertEqual(client.page_calls, [])

    def test_reranker_failure_falls_back(self):
        class Broken(FakeReranker):
            def score(self, query, texts):
                raise RuntimeError("rerank down")

        session = make_session(reranker=Broken())
        results = session.fast_search("q", 5)
        self.assertEqual([r["node"].id for r in results], [ID1, ID2, ID5])

    def test_failed_page_keeps_snippet(self):
        client = FakeClient()
        client.fail_pages.add(ID1)
        session = make_session(client=client)
        results = session.search_with_evidence("q", 3)
        out = session.hydrate_shallow(results, "q", lambda e: None)
        self.assertTrue(any(r["node"].id == ID2 for r in out))


class Budgets(unittest.TestCase):
    def test_shallow_hydrate_cap(self):
        client = FakeClient()
        session = make_session(reranker=None, client=client)
        results = session.search_with_evidence("q", 3)
        session.hydrate_shallow(results, "q", lambda e: None)
        # shallow_page_reads default = 2, and the 3rd result must not be fetched.
        self.assertLessEqual(len([c for c in client.page_calls if c in PAGES]), 2)
        self.assertNotIn(ID5, client.page_calls)

    def test_budget_shared_across_threads(self):
        import threading

        budget = R.RunBudget(3, 2)
        ok_pages = []
        ok_searches = []
        barrier = Event()

        def worker():
            barrier.wait()
            ok_pages.append(budget.try_page())
            ok_searches.append(budget.try_search())

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        barrier.set()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for v in ok_pages if v), 3)
        self.assertEqual(sum(1 for v in ok_searches if v), 2)

    def test_duplicate_operations_memoized(self):
        client = FakeClient()
        session = make_session(client=client)
        session.search_with_evidence("same", 3)
        session.search_with_evidence("same", 3)
        self.assertEqual(len(client.search_calls), 1)
        session.read_node(ID1)
        session.read_node(ID1)
        self.assertEqual(len([c for c in client.page_calls if c == ID1]), 1)


class ReadFollow(unittest.TestCase):
    def test_read_small_full_page(self):
        session = make_session()
        text = session.read_node(ID2)
        self.assertIn("詳細本文", text)

    def test_read_oversized_outline(self):
        session = make_session()
        text = session.read_node(ID4)
        self.assertIn("アウトライン", text)
        self.assertIn("read(node_id", text)

    def test_heading_subtree(self):
        session = make_session()
        text = session.read_node(ID1, heading="関連資料")
        self.assertIn("## 関連資料", text)
        self.assertIn(ID2, text)
        self.assertNotIn("# 概要", text)

    def test_follow_link_summaries_no_target_fetch(self):
        client = FakeClient()
        session = make_session(client=client)
        session.read_node(ID1)  # fetch source only
        client.page_calls.clear()
        links = session.follow_link(ID1)
        labels = {link.label: link for link in links}
        self.assertIn("B", labels)
        self.assertEqual(labels["B"].summary, "詳細資料")
        self.assertEqual(labels["B"].target_node_id, ID2)
        # C resolves by path without a page fetch (target unknown id).
        self.assertEqual([c for c in client.page_calls if c == ID3], [])

    def test_linked_page_fetched_only_after_read(self):
        client = FakeClient()
        session = make_session(client=client)
        session.follow_link(ID1)
        self.assertNotIn(ID2, client.page_calls)
        session.read_node(ID2)
        self.assertIn(ID2, client.page_calls)

    def test_follow_link_rejects_incoming(self):
        session = make_session()
        with self.assertRaises(ValueError):
            session.follow_link(ID1, direction="incoming")


class Cache(unittest.TestCase):
    def test_ttl_expiry_and_max_size(self):
        cache = R.PageCache(ttl=0.05, max_items=2)
        cache.put(("a", ""), page_of("a", "/a"))
        cache.put(("b", ""), page_of("b", "/b"))
        self.assertIsNotNone(cache.get(("a", "")))
        cache.put(("c", ""), page_of("c", "/c"))
        self.assertEqual(cache.size(), 2)
        time.sleep(0.08)
        self.assertIsNone(cache.get(("b", "")))

    def test_cache_is_reused_by_later_sessions(self):
        client = FakeClient()
        cache = R.PageCache(ttl=120, max_items=2)
        R.ResearchSession(client, Settings(), cache, None).read_node(ID1)
        R.ResearchSession(client, Settings(), cache, None).read_node(ID1)
        self.assertEqual(client.page_calls, [ID1])


class PathReads(unittest.TestCase):
    def test_path_reference_uses_one_path_request(self):
        class PathClient(FakeClient):
            def get_page(self, *, page_id=None, path=None):
                self.page_calls.append((page_id, path))
                return PAGES[ID3] if path == PAGES[ID3].path else None

        client = PathClient()
        text = make_session(client=client).read_node(PAGES[ID3].path)
        self.assertIn("本文three", text)
        self.assertEqual(client.page_calls, [(None, PAGES[ID3].path)])


class AskRouting(unittest.TestCase):
    def _events(self, session):
        events = []
        return events, (events.append)

    def test_routing_hydrates_no_pages(self):
        client = FakeClient()
        llm = FakeLLM(route_mode="deep")
        session = make_session(client=client, llm=llm)
        events, emit = self._events(session)
        session._try_route("質問", emit, None, "質問")
        self.assertFalse(any("route" == e.get("type") and e["mode"] == "deep" and client.page_calls for e in events[:3]))
        self.assertEqual(client.page_calls, [])  # router phase reads zero bodies

    def test_followup_reuses_conversation(self):
        session = make_session(llm=FakeLLM(answer="follow-up"))
        events, emit = self._events(session)
        answer = session.ask("question", emit, None, "User: earlier", [ID1])
        self.assertEqual((answer.answer, answer.cited_node_ids), ("follow-up", [ID1]))
        self.assertTrue(any(event.get("mode") == "reuse" for event in events))

    def test_shallow_answer_citations(self):
        client = FakeClient()
        llm = FakeLLM(route_mode="shallow", answer="shallow回答\n\n引用:\n" + ID1)
        session = make_session(client=client, llm=llm)
        events, emit = self._events(session)
        answer = session._try_route("質問", emit, None, "質問")
        self.assertIsNotNone(answer)
        self.assertIn("shallow回答", answer.answer)
        self.assertEqual(answer.cited_node_ids[:1], [ID1])

    def test_section_rerank_selects_evidence(self):
        client = FakeClient()
        session = make_session(client=client, reranker=FakeReranker(prefer="詳細"))
        results = session.search_with_evidence("q", 3)
        out = session.hydrate_shallow(results, "詳細について", lambda e: None)
        top = out[0]
        self.assertEqual(top["node"].id, ID2)
        self.assertTrue(all(ev["field"] == "section" for ev in top["evidence"]))

    def test_evidence_cap_per_page(self):
        body = "\n".join(f"## sec{i}\n詳細content {i}\n" for i in range(10))
        PAGES[ID2] = page_of(ID2, "/Moove/002-詳細", body)
        try:
            session = make_session(reranker=FakeReranker(prefer="詳細"))
            results = session.search_with_evidence("q", 3)
            out = session.hydrate_shallow(results, "詳細", lambda e: None)
            for record in out:
                if record["node"].id == ID2:
                    self.assertLessEqual(len(record["evidence"]), session.settings.evidence_per_page)
        finally:
            PAGES[ID2] = page_of(ID2, "/Moove/002-詳細", "# 詳細\n詳細本文\n")

    def test_deep_route_sets_seed_and_runs_subagents(self):
        client = FakeClient()
        llm = FakeLLM(route_mode="deep")
        session = make_session(client=client, llm=llm)
        events, emit = self._events(session)
        result = session._try_route("質問", emit, None, "質問")
        self.assertIsNone(result)  # deep -> lead agent path
        self.assertTrue(session._seed_ids)
        events.clear()
        from unittest.mock import patch

        with patch.object(R, "_run_subagent", return_value={"start": ID2, "answer": "報告", "cited": [ID2]}):
            report = session._run_subagents([ID2, ID3], "質問", [], emit, None)
        self.assertIn("サブエージェント", report)
        types = [e["type"] for e in events]
        self.assertIn("subagents_spawned", types)


class Overrides(unittest.TestCase):
    def test_bad_host_rejected(self):
        settings = Settings(growi_url="http://growi.test", growi_token="t", allowed_llm_hosts="")
        with self.assertRaises(ValueError):
            R._sanitize_overrides({"chat_base_url": "http://evil.example/v1"}, settings)

    def test_blocked_metadata_host(self):
        settings = Settings(allowed_llm_hosts="169.254.169.254")
        with self.assertRaises(ValueError):
            R._sanitize_overrides({"chat_base_url": "http://169.254.169.254/v1"}, settings)

    def test_blocked_loopback_host(self):
        settings = Settings(allowed_llm_hosts="127.0.0.1")
        with self.assertRaises(ValueError):
            R._sanitize_overrides({"chat_base_url": "http://127.0.0.1/v1"}, settings)

    def test_unknown_key_ignored(self):
        self.assertEqual(R._sanitize_overrides({"nonsense": 1}, Settings()), {})

    def test_extended_overrides_are_hard_capped(self):
        values = R._sanitize_overrides({"subagent_count": 9, "subagent_max_steps": 35}, Settings())
        self.assertEqual(values["subagent_count"], 6)
        self.assertEqual(values["subagent_max_steps"], 35)

    def test_own_chat_host_is_allowed_without_allowlist(self):
        settings = Settings(chat_base_url="http://growi.test", allowed_llm_hosts="")
        values = R._sanitize_overrides({"chat_base_url": "http://growi.test/v1"}, settings)
        self.assertEqual(values["chat_base_url"], "http://growi.test/v1")

    def test_overrides_do_not_mutate_defaults(self):
        settings = Settings(growi_url="http://growi.test", growi_token="t", chat_model="base-model", subagent_count=2)
        session = make_session(settings=settings)
        session.apply_overrides({"chat_model": "other", "subagent_count": 99})
        self.assertEqual(session.settings.chat_model, "other")
        self.assertEqual(session.llm.model, "other")
        self.assertEqual(session.settings.subagent_count, 6)
        self.assertEqual(settings.chat_model, "base-model")
        other = make_session(settings=settings)
        self.assertEqual(other.settings.chat_model, "base-model")


class Cancellation(unittest.TestCase):
    def test_stop_event_cancels_tool_loop(self):
        session = make_session()
        stop = Event()
        ctx = R._LeadContext(session, "q", lambda e: None, stop)
        stop.set()
        with self.assertRaises(R.AgentStopped):
            R._lead_search(ctx, "テキスト")

    def test_subagent_read_budget_enforced(self):
        session = make_session()
        run = R.Subrun(start_id=ID1, index=1)
        stop = Event()
        events = []
        ctx = R._SubContext(session=session, run=run, emit=events.append, stop_event=stop)
        replies = [R._sub_read(ctx, pid) for pid in (ID1, ID2, ID3, ID4, ID5)]
        self.assertTrue(any("読み取り上限" in reply for reply in replies))
        self.assertLessEqual(len(run.read_ids), session.settings.subagent_max_reads)


class NoForbiddenImports(unittest.TestCase):
    def test_no_db_modules(self):
        import ast
        import pathlib

        here = pathlib.Path(__file__).resolve().parent.parent
        banned = {"sqlite3", "sqlite_vec", "GraphStore", "Librarian"}
        for file in sorted(here.glob("*.py")):
            for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names = {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = {node.module.split(".")[0], *(alias.name for alias in node.names)}
                else:
                    continue
                self.assertFalse(
                    names & banned,
                    f"{file.name} imports forbidden symbol: {names & banned}",
                )


# --- Jev: adapters, helpers, sweep ------------------------------------------


def jev_settings(**kw):
    base = dict(growi_url="http://growi.test", growi_token="t", growi_root_path="/Moove",
                jev_enabled=True, jev_threshold=0.5, jev_seed_threshold=0.8)
    base.update(kw)
    return Settings(**base)


class JevAdapterTests(unittest.TestCase):
    def hosted(self, handler, api_key=""):
        settings = jev_settings(jev_backend="hosted", jev_base_url="http://jev.test", jev_api_key=api_key)
        return G.HostedJevClassifier(settings, transport=httpx.MockTransport(handler))

    def test_question_rendering(self):
        text = G.jev_question_text("予算はいくらですか", subject="001-概要")
        self.assertIn("予算はいくらですか", text)
        self.assertIn("はい / いいえ", text)
        self.assertIn("対象: 001-概要", text)

    def test_hosted_request_shape_and_order(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"probabilities": [{"yes": 0.9}, {"yes": 0.2}]})

        clf = self.hosted(handler, api_key="sekret")
        probs = clf.score_many({"document": "/d"},
                               [G.JevQuestion("a", "q1"), G.JevQuestion("b", "q2")])
        self.assertEqual(seen["path"], "/score")
        self.assertEqual(seen["auth"], "Bearer sekret")
        self.assertEqual(probs, [0.9, 0.2])  # request order preserved
        body = seen["body"]
        self.assertEqual(body["model"], "chaoliangUNSW/Jev-Style-0.8B-Decision-v3")
        self.assertEqual(body["options"], {"yes": "はい", "no": "いいえ"})
        self.assertEqual(body["category"], "noul")
        self.assertEqual(body["many_mode"], "batched")
        self.assertEqual([q["key"] for q in body["questions"]], ["a", "b"])

    def test_hosted_no_auth_header_without_key(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"probabilities": [0.5]})

        self.hosted(handler).score_many({}, [G.JevQuestion("a", "q")])
        self.assertIsNone(seen["auth"])

    def test_hosted_keyed_results_aligned_by_key(self):
        def handler(request):
            return httpx.Response(200, json={"results": [
                {"key": "b", "probabilities": {"はい": 0.25}},
                {"key": "a", "probabilities": {"はい": 0.75}},
            ]})

        probs = self.hosted(handler).score_many(
            {}, [G.JevQuestion("a", "q"), G.JevQuestion("b", "q")])
        self.assertEqual(probs, [0.75, 0.25])

    def test_hosted_rejects_bad_shapes(self):
        qa, qb = G.JevQuestion("a", "q"), G.JevQuestion("b", "q")
        cases = [
            ([qa, qb], {"probabilities": [0.5]}),                      # wrong length
            ([qa], b'{"probabilities": [NaN]}'),                        # NaN
            ([qa], {"probabilities": [1.5]}),                          # out of range
            ([qa], {"results": [{"key": "a", "probabilities": {"はい": 0.5}},
                                 {"key": "a", "probabilities": {"はい": 0.4}}]}),  # duplicate keys
            ([qa], {"results": [{"key": "z", "probabilities": {"はい": 0.5}}]}),   # missing key
            ([qa], {"unexpected": True}),                              # unknown shape
        ]
        for questions, body in cases:
            clf = self.hosted(lambda request, b=body: httpx.Response(
                200, content=b if isinstance(b, bytes) else json.dumps(b).encode()))
            with self.assertRaises(RuntimeError, msg=str(body)):
                clf.score_many({}, questions)

    def test_hosted_4xx_not_retried_5xx_retried_once(self):
        calls = []
        clf = self.hosted(lambda request: (calls.append(1), httpx.Response(400))[1])
        with self.assertRaises(RuntimeError):
            clf.score_many({}, [G.JevQuestion("a", "q")])
        self.assertEqual(len(calls), 1)
        calls.clear()
        clf = self.hosted(lambda request: (calls.append(1), httpx.Response(503))[1])
        with self.assertRaises(RuntimeError):
            clf.score_many({}, [G.JevQuestion("a", "q")])
        self.assertEqual(len(calls), 2)

    def test_local_runtime_uses_official_decide_many_noul_api(self):
        class Noul:
            def __init__(self):
                self.calls = []

            def decide_many(self, state, questions):
                self.calls.append(questions)
                return [{"probabilities": {"true": 0.83, "false": 0.17}} for _ in questions]

        runtime = Noul()
        clf = G.LocalJevClassifier(runtime)
        probs = clf.score_many({"document": "/d"},
                               [G.JevQuestion("a", "q1"), G.JevQuestion("b", "q2")])
        self.assertEqual(probs, [0.83, 0.83])
        self.assertEqual(runtime.calls[0], [
            {"t": "noul", "ins": "q1", "crit": None},
            {"t": "noul", "ins": "q2", "crit": None},
        ])

        class Single:
            def __init__(self):
                self.qtypes = []

            def decide(self, state, question, qtype=None):
                self.qtypes.append(qtype)
                return {"probabilities": {"true": 0.83}}

        runtime2 = Single()
        clf2 = G.LocalJevClassifier(runtime2)
        self.assertEqual(clf2.score_many({}, [G.JevQuestion("a", "q")]), [0.83])
        self.assertEqual(runtime2.qtypes, ["noul"])

        class Bad(Noul):
            def decide_many(self, state, questions):
                return [{"probabilities": {"true": 2.0}}]

        with self.assertRaises(RuntimeError):
            G.LocalJevClassifier(Bad()).score_many({}, [G.JevQuestion("a", "q")])

    def test_build_jev_factory(self):
        self.assertIsNone(G.build_jev(Settings(jev_enabled=False, jev_base_url="http://x")))
        self.assertIsNone(G.build_jev(Settings(jev_enabled=True, jev_backend="hosted")))
        with mock.patch.object(G.LocalJevClassifier, "build", return_value=None) as local:
            self.assertIsNone(G.build_jev(Settings(jev_enabled=True, jev_backend="local",
                                                   jev_local_path=str(Path(tempfile.gettempdir()) / "nope-jev"))))
            self.assertIsNone(G.build_jev(Settings(jev_enabled=True, jev_backend="auto")))
            self.assertEqual(local.call_count, 2)
        clf = G.build_jev(Settings(jev_enabled=True, jev_backend="hosted", jev_base_url="http://x"))
        self.assertIsInstance(clf, G.HostedJevClassifier)
        clf2 = G.build_jev(Settings(jev_enabled=True, jev_backend="auto", jev_base_url="http://x"))
        self.assertIsInstance(clf2, G.HostedJevClassifier)

    def test_local_build_loads_runtime_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "jev_style_decision.py").write_text(
                "class JevStyleDecision:\n"
                "    def __init__(self, path): self.path = path\n"
                "    def decide_many(self, state, questions):\n"
                "        return [{'probabilities': {'true': 0.42}} for q in questions]\n",
                encoding="utf-8")
            with mock.patch.object(G, "_jev_local_snapshot", return_value=Path(tmp)):
                clf = G.build_jev(Settings(jev_enabled=True, jev_backend="local", jev_local_path=tmp))
            self.assertIsInstance(clf, G.LocalJevClassifier)
            self.assertEqual(clf.score_many({}, [G.JevQuestion("a", "q")]), [0.42])

    def test_local_snapshot_uses_complete_directory_without_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for name in G.JEV_REQUIRED_FILES:
                (path / name).touch()
            self.assertEqual(G._jev_local_snapshot(jev_settings(jev_local_path=tmp)), path)

    def test_settings_env_validation_and_public_dict(self):
        env = {"WIKI_JEV_ENABLED": "1", "WIKI_JEV_BACKEND": "hosted",
               "WIKI_JEV_BASE_URL": "http://jev.internal:8080", "WIKI_JEV_API_KEY": "topsecret",
               "WIKI_JEV_THRESHOLD": "0.6", "WIKI_JEV_SEED_THRESHOLD": "0.9"}
        with mock.patch.dict(os.environ, env, clear=False):
            st = Settings.from_env()
        self.assertTrue(st.jev_enabled)
        self.assertEqual((st.jev_threshold, st.jev_seed_threshold), (0.6, 0.9))
        pub = st.public_dict()
        self.assertNotIn("jev_api_key", pub)
        self.assertNotIn("jev_local_path", pub)
        self.assertTrue(pub["jev_enabled"])
        bad = jev_settings(jev_threshold=0.6, jev_seed_threshold=0.2)
        with self.assertRaises(ValueError):
            bad.validate_strict()

    def test_chunk_settings_validated(self):
        with self.assertRaises(ValueError):
            jev_settings(jev_chunk_tokens=100, jev_chunk_overlap=999999).validate_strict()


class JevChunkTests(unittest.TestCase):
    def test_small_page_single_chunk(self):
        self.assertEqual(R._jev_chunks("短い本文", 25600, 10000), ["短い本文"])

    def test_max_respected_overlap_and_no_empty(self):
        text = "\n\n".join(f"段落{i}\n" + ("あ" * 200) for i in range(60))
        prefix = "タイトル /Moove/long"
        chunks = R._jev_chunks(text, max_tokens=2000, overlap_tokens=400, prefix=prefix)
        budget = 2000 - 512 - R._jev_est_tokens(prefix)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(R._jev_est_tokens(chunk), budget)
            self.assertTrue(chunk.strip())
        # overlap: the next chunk starts with the previous chunk's last paragraph
        self.assertEqual(chunks[1].split("\n\n")[0], chunks[0].split("\n\n")[-1])

    def test_oversized_single_paragraph_hard_split(self):
        chunks = R._jev_chunks("う" * 20000, max_tokens=1000, overlap_tokens=0)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(R._jev_est_tokens(chunk), 1000 - 512)


IDA = "507f1f77bcf86cd799439111"  # /Moove/A/page1 (doc A, entity ガバナンス)
IDB = "507f1f77bcf86cd799439112"  # /Moove/B/define-governance (doc B definer)
IDN = "507f1f77bcf86cd799439113"  # /Moove/C/note (no-index doc)
IDSD = "507f1f77bcf86cd799439114"  # /Moove/C/sub/deep
INDEX_FLAG = '<span hidden data-llm-wiki-index="1"></span>\n'


class JevClient:
    """Docs A and B have 00-目次 indexes; doc C has none."""

    url = "http://growi.test"

    def __init__(self):
        self.page_calls = []
        self.list_calls = []
        self.search_calls = []
        self.pages = {
            "/Moove/00-目次": page_of("507f1f77bcf86cd799439121", "/Moove/00-目次",
                                       INDEX_FLAG + "- [A](/Moove/A/00-目次) — A\n- [B](/Moove/B/00-目次) — B\n"),
            "/Moove/A/00-目次": page_of("507f1f77bcf86cd799439122", "/Moove/A/00-目次",
                                         INDEX_FLAG + "- [page1](/Moove/A/page1) — 概説\n  - エンティティ: ガバナンス\n"),
            "/Moove/B/00-目次": page_of("507f1f77bcf86cd799439123", "/Moove/B/00-目次",
                                         INDEX_FLAG + "- [define-governance](/Moove/B/define-governance) — 定義\n  - エンティティ: ガバナンス\n"),
            IDA: page_of(IDA, "/Moove/A/page1", "# page1\n概説 本文\n"),
            IDB: page_of(IDB, "/Moove/B/define-governance", "# define\nガバナンスの定義\n"),
            "/Moove/C/note": page_of(IDN, "/Moove/C/note", "ノート ガバナンス 参照\n"),
            "/Moove/C/sub/deep": page_of(IDSD, "/Moove/C/sub/deep", "深い 詳細 ノート\n"),
        }
        for page in list(self.pages.values()):  # id and path both resolve to the same page
            if page.path:
                self.pages.setdefault(page.path, page)
            if page.id:
                self.pages.setdefault(page.id, page)
        self.children_map = {
            "/Moove": [page_of("f-a", "/Moove/A"), page_of("f-b", "/Moove/B"), page_of("f-c", "/Moove/C")],
            "/Moove/A": [self.pages["/Moove/A/00-目次"], self.pages[IDA]],
            "/Moove/B": [self.pages["/Moove/B/00-目次"], self.pages[IDB]],
            "/Moove/C": [self.pages["/Moove/C/note"], page_of("f-cs", "/Moove/C/sub")],
            "/Moove/C/sub": [page_of("f-cidx", "/Moove/C/sub/00-目次",
                                      INDEX_FLAG + "- [deep](/Moove/C/sub/deep) — 深い\n"),
                             self.pages["/Moove/C/sub/deep"]],
        }

    def get_page(self, *, page_id=None, path=None):
        self.page_calls.append(page_id or path)
        return self.pages.get(page_id or path)

    def list_children(self, *, page_id=None, path=None):
        # Same argument contract as GrowiSearchClient.list_children: passing both
        # (or neither) is a caller bug that must fail here, not in production.
        if bool(page_id) == bool(path):
            raise ValueError("exactly one of page_id / path is required")
        self.list_calls.append(page_id or path)
        return list(self.children_map.get(path or "", []))

    def search_pages(self, query, *, path, limit, offset=0):
        self.search_calls.append((query, path, limit))
        return [hit(IDA, "/Moove/A/page1", "概説スニペット", 1)]


class FakeJev:
    """High probability when a yes term appears in the card state or page body."""

    def __init__(self, yes_terms, high=0.95, low=0.05):
        self.yes_terms = yes_terms
        self.high, self.low = high, low
        self.scored_keys = []

    def score_many(self, state, questions):
        cards = {c["path"]: c for c in state.get("cards") or []}
        page_text = (state.get("page") or {}).get("text", "")
        out = []
        for question in questions:
            self.scored_keys.append(question.key)
            card = cards.get(question.key)
            blob = json.dumps(card, ensure_ascii=False) if card else page_text
            out.append(self.high if any(t in blob for t in self.yes_terms) else self.low)
        return out


REWRITTEN = "Moove ライブラリが提供する関数（ets_ 系・pmf_ 系）の一覧と説明が本文に記載されているか？"


class RecordingJev(FakeJev):
    """FakeJev that also keeps the exact question text every page was asked."""

    def __init__(self, yes_terms, **kwargs):
        super().__init__(yes_terms, **kwargs)
        self.questions: list[str] = []

    def score_many(self, state, questions):
        self.questions.extend(question.text for question in questions)
        return super().score_many(state, questions)


class RewritingLLM(FakeLLM):
    """Answers the per-document 目次 summary and the rewrite, as their prompts ask."""

    def complete(self, prompt, payload):
        self.complete_calls.append((prompt, payload))
        if prompt == JEV_QUERY_REWRITE_PROMPT:
            return REWRITTEN
        if prompt == JEV_TOC_SUMMARY_PROMPT:
            return "関数・API の一覧と説明を含む。"
        return self.answer


class BrokenJev:
    def score_many(self, state, questions):
        raise RuntimeError("endpoint down")


class WideClient(JevClient):
    """Doc C has no index and 30 children: the crawl outruns classification."""

    def __init__(self):
        super().__init__()
        wide = [page_of(f"c{i:02d}", f"/Moove/C/p{i:02d}", f"ページ{i} 本文\n") for i in range(30)]
        for page in wide:
            self.pages[page.path] = page
            self.pages[page.id] = page
        self.children_map["/Moove/C"] = [self.pages["/Moove/C/note"],
                                         page_of("f-cs", "/Moove/C/sub"), *wide]


class JevSweepTests(unittest.TestCase):
    def make(self, client=None, jev=None, **settings_kw):
        client = client or JevClient()
        # These fixtures ask about entities that live in card summaries, not page
        # bodies, so the keyword gate is off unless a test opts in explicitly.
        settings_kw.setdefault("jev_prefilter_min_overlap", 0)
        settings = jev_settings(**settings_kw)
        index = R.IndexMap(client, settings, None, None)
        session = R.ResearchSession(client, settings, R.PageCache(120, 256), None,
                                    index_map=index, jev=jev)
        session.llm = FakeLLM(route_mode="deep")
        return client, session

    def sweep(self, session, query, client=None):
        events = []
        results = session._run_jev_sweep(query, events.append, None)
        return results, events

    def test_index_map_entity_catalog(self):
        client, session = self.make()
        index = session.index_map
        self.assertEqual(len(index.definers_for("ガバナンス")), 2)
        self.assertEqual(len(index.definers_for(" ｶﾞﾊﾞﾅﾝｽ ")), 2)  # NFKC/case/whitespace normalized
        self.assertEqual(len(index.cards_for_document("A")), 1)
        self.assertIsNotNone(index.card_for_target("/Moove/A/page1"))

    def test_card_candidate_gets_exactly_one_full_read(self):
        client, session = self.make(jev=FakeJev(["概説"]))
        results, events = self.sweep(session, "概説について")
        self.assertEqual([r["node"].id for r in results], [IDA])
        self.assertEqual(results[0]["evidence"][0]["field"], "jev")
        # card stage pass -> exactly one full-body confirmation
        self.assertEqual(client.page_calls.count("/Moove/A/page1"), 1)

    def test_card_pass_below_seed_threshold_pruned(self):
        # Card summary matches (candidate) but a strict seed threshold rejects it.
        client, session = self.make(jev=FakeJev(["概説"], high=0.65), jev_seed_threshold=0.8)
        results, events = self.sweep(session, "概説について")
        self.assertEqual(results, [])
        self.assertTrue(any(e.get("stage") == "card" and e.get("status") == "candidate" for e in events))
        self.assertTrue(any(e.get("stage") == "full" and e.get("status") == "pruned" for e in events))

    def test_entity_definer_followed_across_documents(self):
        # B is confirmed via its card; the shared entity pulls in A's definer card.
        client, session = self.make(jev=FakeJev(["定義"]))
        results, events = self.sweep(session, "定義について")
        self.assertEqual([r["node"].id for r in results], [IDB])
        complete = next(e for e in events if e.get("type") == "jev_complete")
        self.assertGreater(complete["entity_edges_followed"], 0)
        self.assertIn("/Moove/A/page1", client.page_calls)  # A read via the entity edge only

    def test_low_confidence_definer_prunes_descendants(self):
        client, session = self.make(jev=FakeJev(["定義"]))
        results, _events = self.sweep(session, "定義について")
        self.assertNotIn(IDA, [r["node"].id for r in results])  # A pruned -> no further frontier

    def test_entity_cycle_terminates(self):
        # A and B share ガバナンス and both confirm; each is processed exactly once.
        client, session = self.make(jev=FakeJev(["概説", "定義"]))
        results, _events = self.sweep(session, "ガバナンス")
        self.assertEqual(sorted(r["node"].id for r in results), sorted([IDA, IDB]))
        self.assertEqual(client.page_calls.count("/Moove/A/page1"), 1)
        self.assertEqual(client.page_calls.count("/Moove/B/define-governance"), 1)

    def test_no_index_document_recursively_scored(self):
        client, session = self.make(jev=FakeJev(["深い"]))
        results, _events = self.sweep(session, "深い")
        self.assertEqual([r["node"].id for r in results], [IDSD])  # nested child found
        # no markdown link on the deep page: no invented entity edges (B never read)
        self.assertNotIn(IDB, client.page_calls)
        self.assertNotIn("/Moove/B/define-governance", client.page_calls)

    def test_jev_seeds_lead_and_emits_events(self):
        client, session = self.make(jev=FakeJev(["概説"]))
        events = []
        out = session._try_route("概説について", events.append, None, "概説について")
        self.assertIsNone(out)  # forced deep lead path
        self.assertEqual(session._seed_ids, [IDA])
        self.assertIn("jev", session._seed_context)
        types = [e.get("type") for e in events]
        self.assertIn("jev_gate", types)
        self.assertIn("jev_complete", types)
        self.assertIn("candidates", types)
        # sweep page reads never touch the normal RunBudget and stay cached.
        self.assertEqual(session.budget.pages_used, 0)
        session.read_node("/Moove/A/page1")  # served from the sweep's memo
        self.assertEqual(session.budget.pages_used, 0)

    def test_adapter_failure_falls_back_to_es(self):
        client, session = self.make(jev=BrokenJev())
        events = []
        session._try_route("概説について", events.append, None, "概説について")
        self.assertTrue(any(e.get("type") == "jev_unavailable" for e in events))
        self.assertTrue(client.search_calls)  # ES lane still ran

    def test_disabled_jev_makes_no_calls(self):
        client, session = self.make(jev=None)
        events = []
        session._try_route("概説について", events.append, None, "概説について")
        self.assertFalse([e for e in events if str(e.get("type", "")).startswith("jev")])
        self.assertTrue(client.search_calls)

    def test_sweep_budget_denies_reads_without_failing(self):
        client, session = self.make(jev=FakeJev(["概説"]), jev_max_page_reads=1)
        events = []
        out = session._try_route("概説について", events.append, None, "概説について")
        complete = next(e for e in events if e.get("type") == "jev_complete")
        self.assertLessEqual(complete["page_reads"], 1)
        self.assertTrue(any(e.get("status") == "unread" for e in events))
        self.assertTrue(client.search_calls)  # ES fallback took over
        self.assertIsNone(out)

    def test_live_progress_is_monotone_and_completes(self):
        _client, session = self.make(jev=FakeJev(["概説"]))
        _results, events = self.sweep(session, "概説について")
        progress = [e for e in events if e.get("type") == "jev_progress"]
        self.assertGreater(len(progress), 2)
        percents = [e["percent"] for e in progress]
        self.assertEqual(percents, sorted(percents))  # the bar never recedes as work is discovered
        self.assertTrue(all(0 <= p <= 100 for p in percents))
        self.assertTrue(all(e["done"] <= e["total"] for e in progress))
        self.assertTrue(any(0 < p < 100 for p in percents))  # it actually moves
        self.assertEqual(percents[-1], 100.0)

    def test_classification_overlaps_the_crawl(self):
        # A single worker + a 4-deep queue: classification must start while the
        # sweep is still enumerating pages, not after the whole tree is listed.
        client = WideClient()
        _session_client, session = self.make(client=client, jev=FakeJev(["深い"]), jev_workers=1)
        page_calls = []
        original = session.jev.score_many

        def spy(state, questions):
            if state.get("page"):
                page_calls.append(len(client.list_calls))
            return original(state, questions)

        session.jev.score_many = spy
        results, _events = self.sweep(session, "深い")
        self.assertEqual([r["node"].id for r in results], [IDSD])
        self.assertTrue(page_calls)
        self.assertLess(min(page_calls), len(client.list_calls))

    def test_workers_classify_concurrently(self):
        client, session = self.make(client=WideClient(), jev=FakeJev(["深い"]), jev_workers=4)
        original = session.jev.score_many
        inside = {"live": 0, "peak": 0}
        gate = Lock()

        def spy(state, questions):
            if not state.get("page"):
                return original(state, questions)
            with gate:
                inside["live"] += 1
                inside["peak"] = max(inside["peak"], inside["live"])
            time.sleep(0.02)  # hold the fake model long enough for overlap to show
            with gate:
                inside["live"] -= 1
            return original(state, questions)

        session.jev.score_many = spy
        results, _events = self.sweep(session, "深い")
        self.assertEqual([r["node"].id for r in results], [IDSD])
        self.assertGreaterEqual(inside["peak"], 2)  # more than one page in flight


    def test_seed_groups_slice_by_document(self):
        seeds = [{"node": page_of("a1", "/Moove/A/1"), "score": 0.95, "document": "/Moove/A"},
                 {"node": page_of("b1", "/Moove/B/1"), "score": 0.90, "document": "/Moove/B"},
                 {"node": page_of("a2", "/Moove/A/2"), "score": 0.88, "document": "/Moove/A"}]
        seeds += [{"node": page_of(f"a{i}", f"/Moove/A/{i}"), "score": 0.8 - i / 100, "document": "/Moove/A"}
                  for i in range(3, 12)]
        groups = R._seed_groups(seeds, group_size=5, max_groups=8)
        self.assertTrue(all(len(group) <= 5 for group in groups))          # one agent, <=5 seeds
        self.assertTrue(all(len({r["document"] for r in group}) == 1 for group in groups))
        self.assertEqual(groups[0][0]["node"].id, "a1")                    # best seed leads
        self.assertEqual(sum(len(group) for group in groups), len(seeds))  # nothing dropped
        self.assertEqual(len(R._seed_groups(seeds, group_size=5, max_groups=2)), 2)

    def test_jev_seeds_fan_out_one_agent_per_document(self):
        # A and B each confirm exactly one seed -> exactly two groups, disjoint seeds.
        _client, session = self.make(jev=FakeJev(["概説", "定義"]))
        runs = []

        def fake_subagent(_session, run, _question, prompt, _emit, _stop):
            runs.append(run)
            return {"start": run.start_id, "answer": "報告", "cited": [run.start_id]}

        with mock.patch.object(R, "_run_subagent", side_effect=fake_subagent):
            out = session._try_route("ガバナンス", [].append, None, "ガバナンス")
        self.assertIsNone(out)  # deep lead path, driven by the sweep's findings
        self.assertEqual(len(runs), 2)
        for run in runs:
            self.assertNotIn(run.start_id, run.offlimits)  # own seed stays readable
        self.assertEqual({run.start_id for run in runs}, {IDA, IDB})
        self.assertTrue(all(run.offlimits for run in runs))  # the other group's seed is blocked
        self.assertIn("サブエージェント報告書", session._seed_context)  # reports merged for the lead
        self.assertEqual(sorted(session._seed_ids), sorted([IDA, IDB]))

    def test_group_subagent_cannot_read_another_groups_seed(self):
        client, session = self.make(jev=FakeJev(["概説"]))
        run = R.Subrun(start_id=IDA, index=1, offlimits={IDB, "/Moove/B/define-governance"})
        ctx = R._SubContext(session=session, run=run, emit=lambda _e: None, stop_event=None)
        self.assertIn("他の担当グループ", R._sub_read(ctx, IDB))
        self.assertNotIn(IDB, client.page_calls)  # blocked before any GROWI read
        self.assertEqual(run.read_ids, set())
        R._sub_read(ctx, IDA)
        self.assertEqual(run.read_ids, {IDA})  # its own seed still works

    def test_prefilter_skips_pages_without_the_question_vocabulary(self):
        # Doc C has no index: the whole subtree is swept, so it shows both outcomes.
        client, session = self.make(jev=FakeJev(["ガバナンス"]), jev_prefilter_min_overlap=2)
        quiet = page_of("507f1f77bcf86cd799439099", "/Moove/C/z", "ただの別資料")
        client.pages[quiet.path] = quiet
        client.pages[quiet.id] = quiet
        client.children_map["/Moove/C"] = [client.pages["/Moove/C/note"], quiet]
        _results, events = self.sweep(session, "ガバナンス 一覧")
        statuses = {(e["node"]["title"], e["status"]) for e in events
                    if e.get("type") == "jev_gate" and e.get("stage") == "full"}
        self.assertIn(("note", "confirmed"), statuses)    # shares the vocabulary -> scored
        self.assertIn(("z", "prefiltered"), statuses)     # no shared keyword -> no model call
        complete = next(e for e in events if e.get("type") == "jev_complete")
        self.assertGreaterEqual(complete["prefiltered"], 1)
        self.assertEqual(complete["max_probability"], 0.95)

    def test_group_prompt_lists_the_already_read_document_backlog(self):
        _client, session = self.make(jev=FakeJev(["概説"]))
        group = [{"node": page_of(IDA, "/Moove/A/page1"), "score": 0.9, "document": "/Moove/A",
                  "why": [], "evidence": []}]
        captured = {}

        def fake(_session, run, _question, prompt, _emit, _stop):
            captured["prompt"] = prompt
            return {"start": run.start_id, "answer": "報告", "cited": []}

        with mock.patch.object(R, "_run_subagent", side_effect=fake):
            session._run_group_subagent(1, group, {IDB}, [ID2, ID3], "質問", lambda _e: None, None)
        self.assertIn("追加候補ページ 2 件", captured["prompt"])
        self.assertIn(ID2, captured["prompt"])
        self.assertIn("省略せず", captured["prompt"])  # enumeration answers must not sample

    def test_jev_asks_the_rewritten_query_not_the_raw_question(self):
        # Every document's 目次 is summarised against the question first, and the
        # rewrite is written from all of those notes. Its wording is what every page
        # is asked: the raw question never reaches the classifier.
        jev = RecordingJev(["概説"])
        _client, session = self.make(jev=jev, chat_base_url="http://llm.test", chat_model="m")
        llm = RewritingLLM()
        session.llm = llm
        _results, events = self.sweep(session, "give me all functions")
        query_event = next(e for e in events if e["type"] == "jev_query")
        self.assertTrue(query_event["rewritten"])
        self.assertEqual(query_event["toc_entries"], 3)  # A, B and the nested sub 目次
        self.assertTrue(all(REWRITTEN in text for text in jev.questions))
        self.assertFalse(any("give me all functions" in text for text in jev.questions))
        summaries = [json.loads(payload) for prompt, payload in llm.complete_calls
                     if prompt == JEV_TOC_SUMMARY_PROMPT]
        self.assertEqual(sorted(note["文書"] for note in summaries),
                         ["/Moove/A", "/Moove/B", "/Moove/C/sub"])
        self.assertTrue(all(note["この文書の目次"] for note in summaries))  # each saw its own 目次
        self.assertEqual({note["質問"] for note in summaries}, {"give me all functions"})
        digest = next(json.loads(payload) for prompt, payload in llm.complete_calls
                      if prompt == JEV_QUERY_REWRITE_PROMPT)["この Wiki に存在するページ（目次）"]
        for document in ("/Moove/A", "/Moove/B", "/Moove/C/sub"):
            self.assertIn(f"[{document}]", digest)  # no document left out of the rewrite
        self.assertIn("関数・API", digest)  # the notes, not the raw cards, feed the rewrite

    def test_running_average_of_yes_probability_is_reported(self):
        _client, session = self.make(jev=FakeJev(["概説"]))
        _results, events = self.sweep(session, "概説について")
        ticks = [e for e in events if e["type"] == "jev_progress" and e.get("scored")]
        self.assertTrue(ticks)
        self.assertTrue(all(0.0 <= tick["mean_yes_probability"] <= 1.0 for tick in ticks))
        gates = [e for e in events if e.get("type") == "jev_gate"]
        verdicts = [e for e in gates if e["status"] in ("confirmed", "pruned", "candidate")]
        body = [e["probability"] for e in verdicts if e["stage"] == "full"]
        yeses = [p for p in body if p > 0.5]
        complete = next(e for e in events if e["type"] == "jev_complete")
        self.assertEqual(complete["scored"], len(verdicts))
        self.assertEqual(complete["yes"], len(yeses) + sum(
            1 for e in verdicts if e["stage"] == "card" and e["probability"] > 0.5))
        self.assertLess(len(yeses), len(body))  # the run really did score noes too
        self.assertAlmostEqual(complete["mean_yes_probability"],
                               round(sum(yeses) / len(yeses), 4), places=4)

    def test_sweep_without_index_map_rewrites_from_a_live_inventory(self):
        # Every 00-目次 is found by walking the tree to every depth (path only, never
        # both list_children arguments at once) — a first-level scan would miss the
        # nested one and report half the wiki as 関連なし.
        jev = RecordingJev(["概説"])
        _client, session = self.make(jev=jev, chat_base_url="http://llm.test", chat_model="m")
        session.index_map = None
        llm = RewritingLLM()
        session.llm = llm
        blocks = session._jev_toc_blocks(R._MapState(), R.JevSweepBudget(0, 0), None)
        self.assertEqual(sorted(document for document, _text in blocks),
                         ["/Moove/A", "/Moove/B", "/Moove/C/sub"])
        self.assertIn("page1", dict(blocks)["/Moove/A"])
        self.assertIn("深い", dict(blocks)["/Moove/C/sub"])  # two levels down
        _results, events = self.sweep(session, "give me all functions")
        self.assertTrue(next(e for e in events if e["type"] == "jev_query")["rewritten"])
        digest = next(json.loads(payload) for prompt, payload in llm.complete_calls
                      if prompt == JEV_QUERY_REWRITE_PROMPT)["この Wiki に存在するページ（目次）"]
        for document in ("/Moove/A", "/Moove/B", "/Moove/C/sub"):
            self.assertIn(f"[{document}]", digest)


if __name__ == "__main__":
    unittest.main()
