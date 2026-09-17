"""Researcher tests with fake GROWI / reranker / LLM. No network, no DB."""

import os
import time
import unittest
from threading import Event

os.environ.setdefault("GROWI_URL", "http://growi.test")
os.environ.setdefault("GROWI_TOKEN", "t")

import researcher as R
from config import Settings
from growi_client import GrowiAPIError, SearchHit
from models import AgentAnswer, WikiPage

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


if __name__ == "__main__":
    unittest.main()
