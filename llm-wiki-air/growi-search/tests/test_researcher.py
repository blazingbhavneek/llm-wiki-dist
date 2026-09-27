"""Researcher tests with fake GROWI / reranker / LLM. No network, no DB."""

import os
import json
import tempfile
import time
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from unittest import mock

os.environ.setdefault("GROWI_URL", "http://growi.test")
os.environ.setdefault("GROWI_TOKEN", "t")

import httpx
import gateway as G
import researcher as R
from config import Settings
import jev
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

    def stream(self, system, user, on_delta):
        text = self.complete(system, user)
        on_delta(text)
        return text

    def complete_structured(self, prompt, payload, schema):
        self.structured_calls.append((prompt, payload))
        return schema(mode=self.route_mode, reason="test")


def make_session(settings=None, client=None, reranker=None, llm=None):
    settings = settings or Settings(growi_url="http://growi.test", growi_token="t", growi_root_path="/Moove")
    session = R.ResearchSession(client or FakeClient(), settings, R.PageCache(120, 256), reranker)
    session.llm = llm or FakeLLM()
    return session


class AnswerVerificationTests(unittest.TestCase):
    def session(self, llm):
        return make_session(Settings(growi_url="http://growi.test", growi_token="t", growi_root_path="/Moove",
                                     chat_base_url="http://llm.test/v1", chat_model="m"), llm=llm)

    def draft(self, cited):
        from models import AgentAnswer
        return AgentAnswer(question="q", answer="下書き", cited_node_ids=cited, steps=3)

    def test_answer_is_checked_against_the_full_source_text(self):
        llm = FakeLLM(answer="原文どおりの回答\n\n引用:\n" + ID1 + " : 概要")
        out = self.session(llm)._verified("q", self.draft([ID1, ID2]), lambda _e: None)
        self.assertEqual(out.answer, "原文どおりの回答\n\n引用:\n" + ID1 + " : 概要")
        self.assertEqual(out.cited_node_ids, [ID1])  # only pages the checked answer still cites
        payload = llm.complete_calls[0][1]
        self.assertIn("概要本文", payload)
        self.assertIn("詳細本文", payload)

    def test_cited_pages_are_checked_first_and_a_large_page_cannot_crowd_them_out(self):
        llm = FakeLLM(answer="ok\n\n引用:\n" + ID1)
        draft = self.draft([ID4, ID1]).model_copy(update={"answer": "下書き\n\n引用:\n" + ID1 + " : 概要"})
        with mock.patch.object(R, "VERIFY_SOURCE_CHARS", 1000):
            self.session(llm)._verified("q", draft, lambda _e: None)
        payload = llm.complete_calls[0][1]
        self.assertIn("概要本文", payload)
        self.assertNotIn("长篇内容", payload)

    def test_no_source_pages_means_no_answer(self):
        llm = FakeLLM()
        out = self.session(llm)._verified("q", self.draft([]), lambda _e: None)
        self.assertTrue(out.answer.startswith("Wiki内で"))
        self.assertEqual(llm.complete_calls, [])

    def test_failed_check_is_disclosed(self):
        class Broken(FakeLLM):
            def complete(self, prompt, payload):
                raise RuntimeError("down")
        out = self.session(Broken())._verified("q", self.draft([ID1]), lambda _e: None)
        self.assertIn("照合ができませんでした", out.answer)

    def test_a_section_of_a_page_already_read_can_still_be_read(self):
        ctx = R._SubContext(session=make_session(), run=R.Subrun(start_id=ID1, index=1), emit=lambda _e: None, stop_event=None)
        self.assertIn("概要本文", R._sub_read(ctx, ID1))
        section = R._sub_read(ctx, ID1, "関連資料")
        self.assertNotIn("既に読みました", section)
        self.assertIn("詳細資料", section)
        self.assertIn("既に読みました", R._sub_read(ctx, ID1, "関連資料"))
        self.assertIn("既に読みました", R._sub_read(ctx, ID1))
        self.assertEqual(ctx.run.visited, [ID1])

    def test_min_reads_pushes_back_once_then_lets_the_agent_finish(self):
        session = make_session(settings=Settings(growi_url="http://growi.test", growi_token="t",
                                                 growi_root_path="/Moove", subagent_min_reads=4))
        ctx = R._SubContext(session=session, run=R.Subrun(start_id=ID1, index=1), emit=lambda _e: None, stop_event=None)
        self.assertIn("少なくとも", R._sub_finish(ctx, "報告"))
        self.assertNotIn("answer", ctx.finished)
        self.assertEqual(R._sub_finish(ctx, "報告"), "finished; do not call tools anymore")
        self.assertEqual(ctx.finished["answer"], "報告")


class TimingTests(unittest.TestCase):
    def test_stage_timer_sums_across_threads(self):
        timer = R.StageTimer()
        threads = [Thread(target=lambda: self._spend_stage(timer)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertGreaterEqual(timer.snapshot()["stage_ms"]["x"], 90)

    @staticmethod
    def _spend_stage(timer):
        with timer.stage("x"):
            time.sleep(0.05)

    def test_ask_emits_one_timings_event(self):
        session = make_session()
        session._try_route = lambda *_args: AgentAnswer(question="q", answer="a", cited_node_ids=[], steps=1)
        events = []
        session.ask("q", events.append, None)
        timings = [event for event in events if event.get("type") == "timings"]
        self.assertEqual(len(timings), 1)
        self.assertEqual(set(timings[0]), {"type", "stage_ms", "counts", "total_ms"})

    def test_sweep_counts_jev_questions_and_growi_reads(self):
        _client, session = JevSweepTests().make(jev=FakeJev(["概説"]))
        session._run_jev_sweep("概説について", lambda _event: None, None)
        timing = session.timer.snapshot()
        self.assertIn("sweep", timing["stage_ms"])
        self.assertGreater(timing["counts"]["jev_questions"], 0)
        self.assertGreater(timing["counts"]["growi_get"], 0)


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

    def test_byte_bound_evicts_oldest(self):
        cache = R.PageCache(ttl=120, max_items=10, max_bytes=2 * (1000 + 512))
        pages = [page_of(str(i), f"/{i}", "x" * 1000) for i in range(3)]
        for page in pages:
            cache.put((page.id, ""), page)
        self.assertIsNone(cache.get(("0", "")))
        self.assertIsNotNone(cache.get(("1", "")))
        self.assertIsNotNone(cache.get(("2", "")))


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

    def test_permalink_reference_reads_by_page_id(self):
        client = IndexClient()
        settings = Settings(growi_url="http://growi.test", growi_token="t", growi_root_path="/Moove")
        session = R.ResearchSession(client, settings, R.PageCache(120, 256), None)
        self.assertIsNotNone(session._fetch_ref("/" + IndexClient.DOC_A_ID))
        self.assertEqual(client.page_calls[-1], IndexClient.DOC_A_ID)


class MirrorSessionTests(unittest.TestCase):
    def _mirror(self):
        from tests.test_mirror import FakeGrowi, page, settings as mirror_settings

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        client = FakeGrowi([page(ID1, "/Moove/root", "root body", "rev1", descendant_count=1),
                            page(ID2, "/Moove/root/child", "child body", "rev1")])
        mirror = R.Mirror(client, mirror_settings(temp.name))
        mirror.sync_once()
        return client, mirror

    def test_session_reads_through_the_mirror(self):
        mirror_client, mirror = self._mirror()
        session_client = FakeClient()
        session = R.ResearchSession(session_client, Settings(growi_root_path="/Moove"),
                                    R.PageCache(120, 256), None, mirror=mirror)
        page = session._fetch_page(page_id=ID1)
        self.assertEqual(page.body, "root body")
        self.assertEqual(session_client.page_calls, [])
        self.assertEqual(session.timer.counts["mirror_hit"], 1)
        self.assertEqual(session.budget.pages_used, 0)
        self.assertEqual(mirror_client.calls["get_page"], 2)

    def test_walk_uses_mirror_children(self):
        _mirror_client, mirror = self._mirror()
        session_client = FakeClient()
        session = R.ResearchSession(session_client, Settings(growi_root_path="/Moove"),
                                    R.PageCache(120, 256), None, mirror=mirror)
        walked = list(session._jev_walk(page_of(ID1, "/Moove/root", descendant_count=1),
                                        R.JevSweepBudget(), None))
        self.assertEqual([p.path for p in walked], ["/Moove/root", "/Moove/root/child"])
        self.assertEqual(session_client.page_calls, [])
        self.assertEqual(session.timer.counts["mirror_list"], 1)

    def test_memo_holds_no_bodies_with_mirror(self):
        _mirror_client, mirror = self._mirror()
        session = R.ResearchSession(FakeClient(), Settings(growi_root_path="/Moove"),
                                    R.PageCache(120, 256), None, mirror=mirror)
        first = session._fetch_page(page_id=ID1)
        self.assertEqual(session._page_memo[ID1 + "|"].body, "")
        self.assertEqual(session._fetch_page(page_id=ID1).body, first.body)


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

    def test_shallow_answer_streams_deltas(self):
        class StreamingLLM(FakeLLM):
            def stream(self, system, user, on_delta):
                for piece in ("a", "b"):
                    on_delta(piece)
                return "ab"

        session = make_session(llm=StreamingLLM())
        events, emit = self._events(session)
        answer = session._answer_shallow("q", [{"node": PAGES[ID1], "evidence": []}], emit)
        self.assertEqual([event["text"] for event in events if event["type"] == "answer_delta"], ["a", "b"])
        self.assertEqual(answer.answer, "ab")

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
    def test_subagent_concurrency_override_is_capped_by_the_ceiling(self):
        settings = Settings(llm_max_concurrency=6)
        self.assertEqual(R._sanitize_overrides({"subagent_concurrency": 50}, settings)["subagent_concurrency"], 6)
        self.assertEqual(R._sanitize_overrides({"subagent_concurrency": 3}, settings)["subagent_concurrency"], 3)

    def test_env_ceiling_clamps_the_per_question_default(self):
        values = {"WIKI_SEARCH_LLM_MAX_CONCURRENCY": "3", "WIKI_SUBAGENT_CONCURRENCY": "10"}
        with mock.patch.dict(os.environ, values, clear=True):
            settings = Settings.from_env()
        self.assertEqual((settings.llm_max_concurrency, settings.subagent_concurrency), (3, 3))

    def test_removed_jev_batch_setting_warns_and_is_ignored(self):
        with mock.patch.dict(os.environ, {"WIKI_JEV_BATCH_SIZE": "7"}, clear=True):
            with self.assertLogs("growi_search_config", level="WARNING") as logs:
                settings = Settings.from_env()
        self.assertIn("deprecated", logs.output[0])
        self.assertFalse(hasattr(settings, "jev_batch_size"))

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

    def test_cascade_subagent_override_has_separate_32_cap(self):
        settings = Settings(jev_mode="cascade", cascade_subagents=12)
        self.assertEqual(R._sanitize_overrides({"subagent_count": 50}, settings)["subagent_count"], 32)
        session = make_session(settings=settings)
        session.apply_overrides({"subagent_count": 50})
        self.assertEqual(session.cascade_subagents_override, 32)
        self.assertEqual(session.settings.cascade_subagents, 12)
        other = make_session(settings=settings)
        other.apply_overrides({})
        self.assertIsNone(other.cascade_subagents_override)

    def test_packed_runner_claims_evidence_and_uses_cascade_controls(self):
        session = make_session(settings=Settings(jev_mode="cascade"), client=FakeClient())
        prompt = f"evidence (page_id: {IDA}, p=0.9)"
        captured = {}

        def fake(_session, run, _question, _prompt, _emit, _stop):
            captured["run"] = run
            return {"answer": "report", "cited": [IDA]}

        with mock.patch.object(R, "_run_subagent", side_effect=fake):
            result = session._run_packed_subagent(4, prompt, "question", lambda _e: None, None)
        run = captured["run"]
        self.assertTrue(run.cascade)
        self.assertEqual(run.cascade_question, "question")
        self.assertEqual(run.cascade_page_ids, {IDA})
        self.assertEqual(session._cascade_owner(IDA), 4)
        self.assertEqual(result["answer"], "report")


class LlmCeilingTests(unittest.TestCase):
    def setUp(self):
        if G._LLM_HTTP_CLIENT is not None:
            G._LLM_HTTP_CLIENT.close()
        G._LLM_HTTP_CLIENT = None

    def tearDown(self):
        if G._LLM_HTTP_CLIENT is not None:
            G._LLM_HTTP_CLIENT.close()
        G._LLM_HTTP_CLIENT = None

    def test_process_never_exceeds_the_ceiling(self):
        state = {"active": 0, "peak": 0}
        lock = Lock()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with lock:
                    state["active"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                time.sleep(0.2)
                payload = json.dumps({
                    "id": "completion", "object": "chat.completion", "created": 1, "model": "m",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                with lock:
                    state["active"] -= 1

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        clients = [G.LlmClient("m", f"http://127.0.0.1:{server.server_port}/v1", "k",
                               max_concurrency=2, retry_attempts=0) for _ in range(6)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda llm: llm.complete("system", "user"), clients))
        self.assertEqual(replies, ["ok"] * 6)
        self.assertEqual(state["peak"], 2)


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


class SubFindTests(unittest.TestCase):
    def test_find_output_event_and_read_budget(self):
        session = make_session()
        class WalkerStub:
            def find(self, description, **kw):
                kw["emit"]({"type": "find", "agent": kw["agent"], "description": description,
                            "results": [{"id": ID2}], "questions": 1})
                return [type("Hit", (), {"page_id": ID2, "title": "詳細", "path": "/Moove/詳細",
                                          "p": 0.83, "summary": "手順"})()]
        session.walker = WalkerStub()
        run = R.Subrun(start_id=ID1, index=2)
        before = set(run.read_ids)
        events = []
        ctx = R._SubContext(session, run, events.append, None)
        result = R._sub_find(ctx, "設定手順")
        self.assertEqual(result, f"- node_id: `{ID2}`  title: 詳細  path: /Moove/詳細  p=0.83  summary: 手順  next_action: read(node_id='{ID2}')")
        self.assertEqual(run.read_ids, before)
        self.assertEqual(events[0]["type"], "find")
        self.assertEqual(events[0]["agent"], 2)

    def test_find_tool_only_when_walker_available(self):
        session = make_session()
        run = R.Subrun(start_id=ID1, index=1)
        ctx = R._SubContext(session, run, lambda _event: None, None)
        self.assertNotIn("find", {tool.name for tool in R._sub_tools(ctx)})
        session.walker = object()
        self.assertIn("find", {tool.name for tool in R._sub_tools(ctx)})


class ExhaustiveTuningTests(unittest.TestCase):
    def test_deterministic_client_handles_notes_and_rewrite(self):
        settings = jev_settings(jev_deterministic=True, chat_base_url="http://llm.test/v1", chat_model="test")
        session = make_session(settings=settings)
        deterministic, normal = FakeLLM(answer="stable"), FakeLLM(answer="nondeterministic")
        session.deterministic_llm, session.llm = deterministic, normal
        self.assertEqual(session._jev_toc_note("q", "doc", "toc"), "stable")
        self.assertEqual(session._jev_target_query("q", "material"), "stable")
        self.assertEqual(len(deterministic.complete_calls), 2)
        self.assertEqual(normal.complete_calls, [])

    def test_toc_gate_skips_irrelevant_notes_and_rewrite_material(self):
        class Gate:
            def score_many(self, state, questions):
                return [0.9 if state["document"] == "C" else 0.05]

        session = make_session(settings=jev_settings(jev_toc_gate=True, jev_toc_gate_threshold=0.2,
                                                   chat_base_url="http://llm.test/v1", chat_model="test"),
                               llm=FakeLLM(answer="note"))
        session.jev = Gate()
        session._jev_toc_blocks = lambda *_args: [(name, f"toc {name}") for name in "ABC"]
        events = []
        material, count = session._jev_toc_digest("question", R._MapState(), R.JevSweepBudget(), None, events.append)
        self.assertEqual(count, 3)
        self.assertEqual(len(session.llm.complete_calls), 1)
        self.assertIn("[C] note", material)
        self.assertNotIn("[A]", material)
        self.assertEqual({e["document"]: e["note"] for e in events},
                         {"A": "関連なし", "B": "関連なし", "C": "note"})
        session._jev_target_query("question", material)
        self.assertIn('"[C] note"', session.llm.complete_calls[-1][1])
        self.assertNotIn("[A]", session.llm.complete_calls[-1][1])

    def test_toc_gate_error_falls_back_to_note(self):
        class BrokenGate:
            def score_many(self, _state, _questions):
                raise RuntimeError("offline")

        session = make_session(settings=jev_settings(jev_toc_gate=True,
            chat_base_url="http://llm.test/v1", chat_model="test"), llm=FakeLLM(answer="note"))
        session.jev = BrokenGate()
        session._jev_toc_blocks = lambda *_args: [("A", "toc A")]
        material, _count = session._jev_toc_digest("q", R._MapState(), R.JevSweepBudget(), None, lambda _e: None)
        self.assertIn("[A] note", material)
        self.assertEqual(len(session.llm.complete_calls), 1)

    def test_verdict_cache_skips_repeat_body_questions(self):
        from tests.test_mirror import FakeGrowi, settings as mirror_settings
        from mirror import Mirror

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        mirror = Mirror(FakeGrowi(), mirror_settings(temp.name))
        mirror.ready = True

        class CountingJev(FakeJev):
            def __init__(self):
                super().__init__([])
                self.calls = 0

            def score_many(self, state, questions):
                self.calls += 1
                return [0.9 for _ in questions]

        jev = CountingJev()
        settings = jev_settings(jev_verdict_cache=True)
        session = R.ResearchSession(FakeClient(), settings, R.PageCache(120, 256), None, jev=jev, mirror=mirror)
        page = page_of(ID1, "/Moove/page", "body text", revision_id="rev1")
        first = session._jev_score_body("question", "doc", page, [], None)
        second = session._jev_score_body("question", "doc", page, [], None)
        self.assertEqual(first, second)
        self.assertEqual(jev.calls, 1)
        self.assertEqual(len(mirror.get_verdicts("question")), 1)

    def test_synthesis_uses_reports_streams_and_filters_citations(self):
        settings = jev_settings(lead_after_reports="synthesis")
        llm = FakeLLM(answer=f"Answer\n\n引用:\n\n{ID1} : wrong title\n\n{ID2} : known\n\n{ID3} : unknown")
        session = make_session(settings=settings, llm=llm)
        session._page_memo.update({ID1: PAGES[ID1], ID2: PAGES[ID2]})
        session.jev = FakeJev([])
        session._run_jev_sweep = lambda *_args: [
            {"node": PAGES[ID1], "score": 0.95, "document": "A", "why": [], "evidence": []},
            {"node": PAGES[ID2], "score": 0.90, "document": "B", "why": [], "evidence": []},
        ]
        session._run_seed_groups = lambda *_args: ("reports", [ID2])
        events = []
        with mock.patch.object(R, "_compile_agent", side_effect=AssertionError("lead should not run")):
            answer = session._try_route("question", events.append, None, "question")
        self.assertEqual(answer.cited_node_ids, [ID1, ID2])
        self.assertIn(f"{ID1} : {PAGES[ID1].title}", answer.answer)
        self.assertNotIn("wrong title", answer.answer)
        self.assertNotIn(ID3, answer.answer)
        self.assertTrue(any(event["type"] == "answer_delta" for event in events))

    def test_agent_tool_concurrency_reaches_both_graph_invocations(self):
        settings = Settings(growi_url="http://growi.test", growi_token="t", growi_root_path="/Moove",
                            agent_tool_concurrency=3)
        session = make_session(settings=settings)
        configs = []

        class Agent:
            def invoke(self, _payload, config):
                configs.append(config)
                return {"messages": [{"content": "answer"}]}

        with mock.patch.object(R, "_compile_agent", return_value=Agent()):
            session._run_lead("q", lambda _event: None, None)
            session._run_single_subagent(ID1, [], "q", 1, lambda _event: None, None)
        self.assertEqual([config["max_concurrency"] for config in configs], [3, 3])


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
    def setUp(self):
        jev.reset_engine()

    def tearDown(self):
        jev.reset_engine()

    def test_question_rendering(self):
        text = G.jev_question_text("予算はいくらですか", subject="001-概要")
        self.assertIn("予算はいくらですか", text)
        self.assertIn("はい / いいえ", text)
        self.assertIn("対象: 001-概要", text)

    def test_build_jev_factory(self):
        self.assertIsNone(G.build_jev(Settings(jev_enabled=False, jev_base_url="http://x")))
        self.assertIsNone(G.build_jev(Settings(jev_enabled=True, jev_backend="hosted")))

        class Backend:
            name = "hosted"
            def prepare(self, request): raise AssertionError("not used")
            def run(self, batch): raise AssertionError("not used")
            def count_tokens(self, text): return 0
            def close(self): pass

        with mock.patch("jev.backends.make_backend", return_value=Backend()) as factory:
            engine = G.build_jev(Settings(jev_enabled=True, jev_backend="hosted", jev_base_url="http://x"))
            self.assertIsInstance(engine, jev.JevEngine)
            self.assertEqual(engine.backend.name, "hosted")
            self.assertEqual(factory.call_count, 1)

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

    def test_custom_counter_used(self):
        chunks = R._jev_chunks("abcdefghij", 518, 0, count=len)
        self.assertEqual(chunks, ["abcdef", "ghij"])


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
            "/Moove": [page_of("f-a", "/Moove/A", descendant_count=2),
                        page_of("f-b", "/Moove/B", descendant_count=2),
                        page_of("f-c", "/Moove/C", descendant_count=4)],
            "/Moove/A": [self.pages["/Moove/A/00-目次"], self.pages[IDA]],
            "/Moove/B": [self.pages["/Moove/B/00-目次"], self.pages[IDB]],
            "/Moove/C": [self.pages["/Moove/C/note"], page_of("f-cs", "/Moove/C/sub", descendant_count=2)],
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
            page.descendant_count = 1
            self.pages[page.path] = page
            self.pages[page.id] = page
        self.children_map["/Moove/C"] = [self.pages["/Moove/C/note"],
                                         page_of("f-cs", "/Moove/C/sub", descendant_count=2), *wide]


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

    def test_cascade_reads_selected_sections_and_blocks_other_bin_claims(self):
        client = JevClient()
        page = page_of(IDA, "/Moove/A/page1",
                       "# First\nnot selected\n## Chosen\nselected content\n## Other\nother content\n",
                       revision_id="rev-1")
        client.pages[IDA] = client.pages[page.path] = page
        _client, session = self.make(client=client, jev=FakeJev(["概説"]))
        rewritten = "rewritten question"
        selected = R.md.split_sections("## Chosen\nselected content\n")
        session._cascade_sections[(IDA, "rev-1", rewritten)] = selected
        prompt_a = f"(page_id: {IDA}, path: /Moove/A/page1, p=0.9)"
        prompt_b = f"(page_id: {IDB}, path: /Moove/B/define-governance, p=0.8)"
        session._reserve_cascade_pages([IDA, IDB, "/Moove/A/page1", "/Moove/B/define-governance"])
        session._claim_cascade_pages(1, prompt_a)
        self.assertEqual(session._cascade_owner(IDB), -1)
        self.assertEqual(session._cascade_owner("/Moove/B/define-governance"), -1)
        run = R.Subrun(start_id=IDA, index=1, cascade=True,
                       cascade_question=rewritten, cascade_page_ids={IDA})
        ctx = R._SubContext(session=session, run=run, emit=lambda _event: None, stop_event=None)

        before = list(client.page_calls)
        blocked = R._sub_read(ctx, IDB)
        self.assertIn("別の担当 bin", blocked)
        self.assertIn("別の担当 bin", R._sub_read(ctx, "/Moove/B/define-governance"))
        self.assertEqual(client.page_calls, before)

        session._claim_cascade_pages(2, prompt_b)
        self.assertEqual(session._cascade_owner(IDB), 2)

        text = R._sub_read(ctx, IDA)
        self.assertIn("selected content", text)
        self.assertNotIn("not selected", text)
        self.assertNotIn("other content", text)

    def test_walk_does_not_list_leaves(self):
        client, session = self.make()
        list(session._jev_walk(page_of("f-c", "/Moove/C"), R.JevSweepBudget(), None))
        self.assertEqual(client.list_calls, ["/Moove/C", "/Moove/C/sub"])

    def test_cascade_unavailable_emits_fallback_and_uses_es_route(self):
        from jev.types import JevUnavailable

        client, session = self.make(jev=FakeJev(["概説"]), jev_mode="cascade")
        events = []
        with mock.patch("cascade.run_cascade", side_effect=JevUnavailable("no seeds")), \
             mock.patch.object(session, "_run_jev_sweep", side_effect=AssertionError("cascade must skip exhaustive Jev")):
            result = session._try_route("概説について", events.append, None, "概説について")
        self.assertIsNone(result)  # FakeLLM routes deep after the ES candidates
        self.assertTrue(client.search_calls)
        self.assertEqual(next(e["reason"] for e in events if e["type"] == "cascade_fallback"), "no seeds")
        self.assertTrue(any(e.get("type") == "route" for e in events))

    def test_document_fallback_skips_loaded_tree_refs_but_keeps_missing_siblings(self):
        from markdown import IndexCard

        client, session = self.make()
        indexed_path = "/Moove/indexed/00-目次"
        indexed = page_of(ID1, indexed_path, '<span data-llm-wiki-index="document"></span>')
        rows = [page_of(ID2, "/Moove/indexed"), page_of(ID3, "/Moove/legacy"),
                page_of(ID4, "/Moove/missing")]
        doc = IndexCard(target=indexed_path, title="indexed", doc_ref="doc-ref")
        loaded_folder, missing_folder = IndexCard(target="/Moove/loaded", title="loaded", kind="folder"), \
            IndexCard(target="/Moove/missing", title="missing", kind="folder")
        state = R._MapState(docs=[doc], folders=[loaded_folder, missing_folder],
                            cards_by_document={"doc-ref": [IndexCard(target="/Moove/indexed/page", title="p")]},
                            children={"Moove/loaded": []})
        session._fetch_ref = lambda *args, **kwargs: indexed
        session._mirror_children = lambda path: rows
        docs = session._jev_documents(state, R.JevSweepBudget(), None)
        self.assertEqual([item["key"] for item in docs], ["/Moove/indexed", "/Moove/legacy", "/Moove/missing"])

    def test_index_map_entity_catalog(self):
        client, session = self.make()
        index = session.index_map
        self.assertEqual(len(index.definers_for("ガバナンス")), 2)
        self.assertEqual(len(index.definers_for(" ｶﾞﾊﾞﾅﾝｽ ")), 2)  # NFKC/case/whitespace normalized
        self.assertEqual(len(index.cards_for_document("Moove/A/00-目次")), 1)
        self.assertIsNotNone(index.card_for_target("/Moove/A/page1"))

    def test_entity_index_matches_old_scan(self):
        names = ["A", "AB", "ABC", "ガバナンス", "ｶﾞﾊﾞﾅﾝｽ", "概説", "B"]
        normalized = [(R._norm_entity(name), name, rank) for rank, name in
                      enumerate(sorted(set(names), key=len, reverse=True))]
        state = R._MapState(
            entity_index={key: [item for item in normalized if len(item[0]) >= 2 and item[0][:2] == key]
                          for key in {item[0][:2] for item in normalized if len(item[0]) >= 2}},
            short_entities=[item for item in normalized if len(item[0]) < 2],
        )
        session = self.make()[1]
        for text in ("ABC と AB と A", "ｶﾞﾊﾞﾅﾝｽの概説", "かなとB、ガバナンス"):
            page = page_of("entity-test", "/entity", text)
            hay = R._norm_entity(f"{page.title}\n{page.body}"[:40000])
            expected = [name for _norm, name, _rank in
                        sorted((item for item in normalized if item[0] in hay),
                               key=lambda item: (-len(item[1]), item[2]))]
            actual = session._page_entities(state, page)
            self.assertEqual(actual, expected)

    def test_prefilter_grams_cached_per_revision(self):
        _client, session = self.make(jev=FakeJev(["概説"]), jev_prefilter_min_overlap=2)
        page = page_of(IDN, "/Moove/C/note", "概説に関する本文", revision_id="revision-1")
        page_input = f"{page.title}\n{page.path}\n{page.body}"[:20000]
        original = R.IndexMap.grams
        with mock.patch.object(R.IndexMap, "grams", wraps=original) as grams:
            session._jev_prefiltered("概説について", page)
            session._jev_prefiltered("概説について", page)
        calls = [call.args[0] for call in grams.call_args_list]
        self.assertEqual(calls.count(page_input), 1)

    def test_frontier_runs_levels_in_parallel(self):
        from markdown import IndexCard

        class FrontierClient:
            def __init__(self):
                pages = [page_of(pid, f"/Moove/{name}", "body", revision_id="rev1")
                         for pid, name in ((IDA, "A"), (IDB, "B"),
                                           ("507f1f77bcf86cd799439115", "D1"),
                                           ("507f1f77bcf86cd799439116", "D2"))]
                self.pages = {page.path: page for page in pages}
                self.by_id = {page.id: page for page in pages}

            def get_page(self, *, page_id=None, path=None):
                return self.by_id.get(page_id) if page_id else self.pages.get(path)

            def list_children(self, *, page_id=None, path=None):
                return [self.pages["/Moove/A"], self.pages["/Moove/B"]] if path == "/Moove" else []

        class Definers:
            def snapshot(self):
                return R._MapState()

            def definers_for(self, entity):
                if entity != "E":
                    return []
                return [IndexCard(target=f"/Moove/D{i}", title=f"D{i}", document=f"/Moove/D{i}")
                        for i in (1, 2)]

        class SlowJev(FakeJev):
            def score_many(self, state, questions):
                time.sleep(0.05)
                return [0.99 for _ in questions]

        client = FrontierClient()
        settings = jev_settings(jev_workers=4, jev_prefilter_min_overlap=0)
        session = R.ResearchSession(client, settings, R.PageCache(120, 256), None,
                                    index_map=Definers(), jev=SlowJev([]))
        session._jev_toc_digest = lambda *_args: ("", 0)
        session._jev_target_query = lambda query, _material: query
        session._page_entities = lambda _state, page: ["E"] if page.path == "/Moove/A" else []
        results = session._run_jev_sweep("q", lambda _event: None, None)
        ids = {result["node"].id for result in results}
        self.assertEqual(ids, {IDA, IDB, "507f1f77bcf86cd799439115", "507f1f77bcf86cd799439116"})
        self.assertLess(session.timer.stage_ms["frontier"], 90)

    def test_card_states_hold_one_card(self):
        from markdown import IndexCard

        class RecordingJev(FakeJev):
            def __init__(self):
                super().__init__([])
                self.state_sizes = []

            def score_many(self, state, questions):
                self.state_sizes.append(len(state.get("cards", [])))
                return [0.9 for _ in questions]

        fake = RecordingJev()
        _client, session = self.make(jev=fake)
        cards = [IndexCard(target=f"/doc/{index}", title=f"card{index}") for index in range(3)]
        session._jev_score_cards("q", {"key": "/doc", "cards": cards}, lambda _event: None,
                                 None, R._SweepWork({"cards_considered": 0, "candidates": 0}))
        self.assertEqual(fake.state_sizes, [1, 1, 1])

    def test_card_stage_one_batch_per_document(self):
        from types import SimpleNamespace
        from markdown import IndexCard

        class BatchJev:
            def __init__(self):
                self.calls = []

            def decide_batch(self, requests):
                self.calls.append(requests)
                return [SimpleNamespace(p_yes=0.9) for _request in requests]

        fake = BatchJev()
        _client, session = self.make(jev=fake)
        cards = [IndexCard(target=f"/doc/{index}", title=f"card{index}") for index in range(3)]
        session._jev_score_cards("q", {"key": "/doc", "cards": cards}, lambda _event: None,
                                 None, R._SweepWork({"cards_considered": 0, "candidates": 0}))
        session._jev_score_cards("q", {"key": "/other", "cards": cards}, lambda _event: None,
                                 None, R._SweepWork({"cards_considered": 0, "candidates": 0}))
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual([len(requests) for requests in fake.calls], [3, 3])
        self.assertEqual([[len(request.state["cards"]) for request in requests]
                          for requests in fake.calls], [[1, 1, 1], [1, 1, 1]])

    def test_tree_index_is_followed_to_page_cards(self):
        class TreeIndexClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.pages = {
                    "/Moove/00-目次": page_of("root", "/Moove/00-目次", '<span hidden data-llm-wiki-index="root"></span>\n- [teamA](/Moove/teamA/00-目次) — チーム\n  - 種別: フォルダ\n'),
                    "/Moove/teamA/00-目次": page_of("team", "/Moove/teamA/00-目次", '<span hidden data-llm-wiki-index="folder"></span>\n- [A](/Moove/teamA/A/00-目次) — alpha\n  - 種別: 文書\n'),
                    "/Moove/teamA/A/00-目次": page_of("doc", "/Moove/teamA/A/00-目次", '<span hidden data-llm-wiki-index="document"></span>\n- [Alpha](/Moove/teamA/A/page1) — alpha\n  - キーワード: alpha\n'),
                }

            def get_page(self, *, page_id=None, path=None):
                key = page_id or path
                self.page_calls.append(key)
                return self.pages.get(key)

        client = TreeIndexClient()
        settings = Settings(growi_url="http://growi.test", growi_token="t", growi_root_path="/Moove")
        index = R.IndexMap(client, settings, None, None)
        self.assertEqual(index.rank("alpha", 2)[0]["node"].title, "Alpha")
        state = index.snapshot()
        root_ref = "Moove/00-目次"
        team_ref = "Moove/teamA/00-目次"
        doc_ref = "Moove/teamA/A/00-目次"
        self.assertEqual(state.children[root_ref][0].kind, "folder")
        self.assertEqual(state.parent[team_ref], root_ref)
        self.assertEqual(state.parent[doc_ref], team_ref)
        self.assertEqual(state.parent["Moove/teamA/A/page1"], doc_ref)

    def test_document_index_subfolders_are_followed(self):
        class CollisionClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.pages = {
                    "/Moove/00-目次": page_of("r", "/Moove/00-目次", '<span hidden data-llm-wiki-index="root"></span>\n- [teamA](/Moove/teamA/00-目次) — チーム\n  - 種別: フォルダ\n'),
                    "/Moove/teamA/00-目次": page_of("t", "/Moove/teamA/00-目次", '<span hidden data-llm-wiki-index="folder"></span>\n- [x](/Moove/teamA/x/00-目次) — chapter\n  - 種別: 文書\n'),
                    "/Moove/teamA/x/00-目次": page_of("x", "/Moove/teamA/x/00-目次", '<span hidden data-llm-wiki-index="document"></span>\n- [Page X](/Moove/teamA/x/page) — alpha\n- [y](/Moove/teamA/x/y/00-目次) — child\n  - 種別: フォルダ\n'),
                    "/Moove/teamA/x/y/00-目次": page_of("y", "/Moove/teamA/x/y/00-目次", '<span hidden data-llm-wiki-index="folder"></span>\n- [z](/Moove/teamA/x/y/z/00-目次) — deep\n  - 種別: 文書\n'),
                    "/Moove/teamA/x/y/z/00-目次": page_of("z", "/Moove/teamA/x/y/z/00-目次", '<span hidden data-llm-wiki-index="document"></span>\n- [Deep page](/Moove/teamA/x/y/z/page) — alpha\n'),
                }

            def get_page(self, *, page_id=None, path=None):
                key = page_id or path
                self.page_calls.append(key)
                return self.pages.get(key)

        settings = Settings(growi_url="http://growi.test", growi_token="t", growi_root_path="/Moove")
        index = R.IndexMap(CollisionClient(), settings, None, None)
        self.assertIn("Deep page", [item["node"].title for item in index.rank("alpha", 5)])
        state = index.snapshot()
        self.assertEqual(state.children["Moove/teamA/x/00-目次"][1].kind, "folder")
        self.assertEqual(state.parent["Moove/teamA/x/y/00-目次"], "Moove/teamA/x/00-目次")
        self.assertEqual(state.parent["Moove/teamA/x/y/z/00-目次"], "Moove/teamA/x/y/00-目次")

    def test_cards_by_document_keyed_by_ref(self):
        class DuplicateTitleClient(FakeClient):
            def __init__(self):
                super().__init__()
                marker = '<span hidden data-llm-wiki-index="'
                self.pages = {
                    "/Moove/00-目次": page_of("r", "/Moove/00-目次", marker + 'root"></span>\n- [teamA](/Moove/teamA/00-目次) — チーム\n  - 種別: フォルダ\n- [teamB](/Moove/teamB/00-目次) — チーム\n  - 種別: フォルダ\n'),
                    "/Moove/teamA/00-目次": page_of("fa", "/Moove/teamA/00-目次", marker + 'folder"></span>\n- [Same](/Moove/teamA/Same/00-目次) — A\n  - 種別: 文書\n'),
                    "/Moove/teamB/00-目次": page_of("fb", "/Moove/teamB/00-目次", marker + 'folder"></span>\n- [Same](/Moove/teamB/Same/00-目次) — B\n  - 種別: 文書\n'),
                    "/Moove/teamA/Same/00-目次": page_of("da", "/Moove/teamA/Same/00-目次", marker + 'document"></span>\n- [A page](/Moove/teamA/Same/a) — alpha\n'),
                    "/Moove/teamB/Same/00-目次": page_of("db", "/Moove/teamB/Same/00-目次", marker + 'document"></span>\n- [B page](/Moove/teamB/Same/b) — beta\n'),
                }

            def get_page(self, *, page_id=None, path=None):
                key = page_id or path
                self.page_calls.append(key)
                return self.pages.get(key)

        settings = Settings(growi_url="http://growi.test", growi_token="t", growi_root_path="/Moove")
        state = R.IndexMap(DuplicateTitleClient(), settings, None, None).snapshot()
        self.assertEqual(len(state.cards_by_document), 2)
        self.assertEqual({cards[0].document for cards in state.cards_by_document.values()}, {"Same"})
        self.assertEqual({cards[0].doc_ref for cards in state.cards_by_document.values()},
                         {"Moove/teamA/Same/00-目次", "Moove/teamB/Same/00-目次"})

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

    def test_seed_group_backlog_lists_only_readable_leftovers(self):
        confirmed = [
            {"node": page_of(f"a{i}", f"/Moove/A/{i}"), "score": 0.99 - i * 0.01,
             "document": "/Moove/A", "why": [], "evidence": []}
            for i in range(7)
        ]
        confirmed.append({"node": page_of("b0", "/Moove/B/0"), "score": 0.985,
                          "document": "/Moove/B", "why": [], "evidence": []})
        client, session = self.make()
        groups = R._seed_groups(confirmed, group_size=5, max_groups=2)
        captured = {}

        def fake(_session, run, _question, prompt, _emit, _stop):
            captured[run.start_id] = (prompt, run.offlimits)
            return {"start": run.start_id, "answer": "報告", "cited": []}

        with mock.patch.object(R, "_run_subagent", side_effect=fake):
            session._run_seed_groups(groups, confirmed, "質問", lambda _e: None, None)
        a_prompt, a_offlimits = captured["a0"]
        self.assertIn("追加候補ページ 2 件", a_prompt)
        self.assertIn("a5", a_prompt)
        self.assertIn("a6", a_prompt)
        self.assertNotIn("a5", a_offlimits)
        self.assertNotIn("a6", a_offlimits)
        b_prompt, _b_offlimits = captured["b0"]
        self.assertNotIn("追加候補ページ", b_prompt)

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

    def test_toc_digest_skips_folder_indexes(self):
        client, session = self.make()
        client.children_map["/Moove/A"].append(page_of(
            "f-folder-index", "/Moove/A/sub/00-目次",
            '<span hidden data-llm-wiki-index="folder"></span>\n- [deep](/Moove/A/sub/deep) — repeated content\n'))
        blocks = session._jev_toc_blocks(R._MapState(), R.JevSweepBudget(0, 0), None)
        names = {name for name, _text in blocks}
        self.assertIn("/Moove/A", names)
        self.assertNotIn("/Moove/A/sub", names)


if __name__ == "__main__":
    unittest.main()
