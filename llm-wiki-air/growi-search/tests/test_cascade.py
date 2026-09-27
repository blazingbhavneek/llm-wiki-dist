from __future__ import annotations

import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from cascade import _p, _profile, _render_bin, pack_evidence, run_cascade
from gateway import JevQuestion
from jev.types import JevUnavailable, JevResult
from markdown import IndexCard
from models import WikiPage
from walker import WalkHit


class FakeEngine:
    def __init__(self, shape=(0.0, 0.0), section_scores=None, stop=0.0):
        self.shape = list(shape)
        self.section_scores = section_scores or {}
        self.stop = stop
        self.score_calls = []
        self.batch_calls = []

    def score_many(self, state, questions):
        self.score_calls.append((state, list(questions)))
        if len(self.score_calls) == 1:
            return self.shape[:len(questions)]
        return [self.stop for _ in questions]

    def decide_batch(self, requests):
        self.batch_calls.append(list(requests))
        out = []
        for request in requests:
            state = request.state
            text = state.get("page", {}).get("text", "") if isinstance(state, dict) else ""
            heading = state.get("page", {}).get("heading", "") if isinstance(state, dict) else ""
            probability = self.section_scores.get(heading, self.section_scores.get(text, .8))
            out.append(JevResult(request.question.key, "はい", {"false": 1-probability, "true": probability},
                                probability, probability, 1))
        return out

    def count_tokens(self, text):
        return max(1, len(text))


class CascadeHelpersTest(unittest.TestCase):
    def test_profiles_from_shape_questions(self):
        session = SimpleNamespace(jev=FakeEngine((0.0, .9)), settings=SimpleNamespace(
            cascade_subagents=15, cascade_context_tokens=48000, cascade_max_docs=40))
        profile = _profile(session, "q")
        self.assertEqual((profile["subagents"], profile["context_tokens"], profile["max_docs"], profile["early_stop"]),
                         (3, 32000, 10, True))
        session.jev = FakeEngine((.9, 0.0))
        profile = _profile(session, "list")
        self.assertEqual((profile["subagents"], profile["context_tokens"], profile["max_docs"], profile["early_stop"]),
                         (20, 96000, 100, False))
        session.jev = FakeEngine((0.0, 0.0))
        session.cascade_subagents_override = 99
        self.assertEqual(_profile(session, "default")["subagents"], 32)

    def test_score_many_float_contract(self):
        self.assertEqual(_p(.83), .83)

    def _evidence(self, doc, page, heading, probability, order):
        section = SimpleNamespace(heading=heading, body=heading)
        return {"document": doc, "page": SimpleNamespace(id=page, title=page, path="/" + page), "section": section,
                "p": probability, "page_order": order, "section_order": order, "tokens": 4}

    def test_packing_keeps_documents_whole_and_orders_by_p(self):
        evidence = [self._evidence("a", "a1", "a-low", .3, 1),
                    self._evidence("a", "a2", "a-high", .9, 0),
                    self._evidence("b", "b1", "b-high", .8, 0)]
        groups, dropped = pack_evidence(evidence, 2, 8)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0][0]["p"], .9)
        self.assertEqual({item["document"] for item in groups[0]}, {"a"})

    def test_overflow_drops_lowest_probability(self):
        evidence = [self._evidence("a", f"p{i}", f"s{i}", p, i)
                    for i, p in enumerate((.1, .9, .2, .8))]
        groups, dropped = pack_evidence(evidence, 1, 8)
        kept = [item["p"] for group in groups for item in group]
        self.assertEqual(dropped, 2)
        self.assertEqual(sorted(kept), [.8, .9])

    def test_overflow_count_is_in_packed_prompt(self):
        page = SimpleNamespace(id="p", title="Page")
        evidence = [self._evidence("doc", f"p{i}", f"h{i}", p, i) for i, p in enumerate((.9, .8, .1))]
        groups, dropped = pack_evidence(evidence, 1, 8)
        prompt = _render_bin(groups[0], dropped, [])
        self.assertIn("1 件省略", prompt)

    def test_oversized_document_splits_only_at_page_boundaries(self):
        evidence = [self._evidence("doc", f"p{i}", f"h{i}", p, i)
                    for i, p in enumerate((.9, .8, .7))]
        groups, dropped = pack_evidence(evidence, 2, 4)
        self.assertEqual(dropped, 1)
        self.assertEqual({item["page"].id for group in groups for item in group}, {"p0", "p1"})
        self.assertEqual([len({item["page"].id for item in group}) for group in groups], [1, 1])


class CascadeRunTest(unittest.TestCase):
    def _session(self, bodies, route_ids=None, section_scores=None, cache=False):
        pages = {key: WikiPage(id=key, path=f"/root/doc/{key}", title=key,
                               body=value, document="doc", revision_id="r1")
                 for key, value in bodies.items()}
        cards = [IndexCard(target=page.path, title=page.title, document="doc")
                 for page in pages.values()]
        state = SimpleNamespace(cards=cards, folders=[], children={"root/00-目次": []})
        engine = FakeEngine((0.0, 0.0), section_scores=section_scores)
        events = []

        class WalkerFake:
            def collect(self, *args, **kwargs):
                ids = route_ids if route_ids is not None else list(pages)
                return [WalkHit(pages[key].id, pages[key].path, pages[key].title, "", .9, [])
                        for key in ids]

        class LLM:
            def complete(self, *_args):
                return "rewritten"
            def stream(self, _system, _user, on_delta):
                answer = "Answer\n\n引用:\n"
                for page_id in pages:
                    answer += f"{page_id} : {page_id}\n"
                on_delta(answer)
                return answer

        session = SimpleNamespace(
            settings=SimpleNamespace(answer_cache=cache, growi_root_path="/root",
                index_page_name="00-目次", walker_threshold=.2, jev_seed_threshold=.8,
                jev_chunk_tokens=1000, jev_chunk_overlap=0, subagent_concurrency=2,
                cascade_section_threshold=.3, cascade_early_stop=.9,
                cascade_subagents=2, cascade_context_tokens=10000, cascade_max_docs=10),
            jev=engine, llm=LLM(), walker=WalkerFake(),
            index_map=SimpleNamespace(snapshot=lambda: state, definers_for=lambda _entity: []),
            _fetch_page=lambda **kwargs: pages.get(kwargs.get("page_id") or next(
                (key for key, page in pages.items() if page.path == kwargs.get("path")), "")),
            _jev_score_body=lambda *_args: (.9, 1), _record_usage=lambda: None,
            _run_packed_subagent=lambda _i, prompt, _q, _emit, _stop:
                {"answer": prompt, "cited": list(pages)},
            mirror=None)
        session.events = events
        return session, pages

    def test_pruned_folder_never_scored(self):
        session, pages = self._session({"a": "# A\n## good\nselected", "news": "# N\n## unrelated\nother"},
                                       route_ids=["a"])
        scored = []
        session._jev_score_body = lambda _q, _d, page, *_args: (scored.append(page.id) or .9, 1)
        run_cascade(session, "question", lambda _e: None)
        self.assertEqual(scored, ["a"])

    def test_sections_selected_and_best_kept(self):
        body = "# Page\n## pass\none\n## low\ntwo"
        session, _ = self._session({"a": body}, section_scores={"Page": .0, "pass": .8, "low": .1})
        prompts = []
        session._run_packed_subagent = lambda _i, prompt, *_args: (prompts.append(prompt) or {"answer": "report", "cited": ["a"]})
        run_cascade(session, "q", lambda _e: None)
        self.assertIn("### a › pass", prompts[0]); self.assertNotIn("### a › low", prompts[0])

    def test_best_section_kept_when_none_pass(self):
        body = "# Page\n## first\none\n## second\ntwo"
        session, _ = self._session({"a": body}, section_scores={"Page": .0, "first": .1, "second": .2})
        prompts = []
        session._run_packed_subagent = lambda _i, prompt, *_args: (prompts.append(prompt) or {"answer": "report", "cited": ["a"]})
        run_cascade(session, "q", lambda _e: None)
        self.assertIn("### a › second", prompts[0]); self.assertNotIn("### a › first", prompts[0])

    def test_failed_subagent_does_not_abort_cascade(self):
        session, _ = self._session({"a": "# A\n## H\ntext"})
        def broken(*_args):
            raise RuntimeError("llm down")
        session._run_packed_subagent = broken
        answer = run_cascade(session, "q", lambda _e: None)
        self.assertIn("Answer", answer.answer)

    def test_fallback_to_es_when_no_seeds(self):
        session, _ = self._session({"a": "# A\n## H\ntext"})
        session._jev_score_body = lambda *_args: (.1, 1)
        with self.assertRaises(JevUnavailable):
            run_cascade(session, "q", lambda _e: None)

    def test_synthesis_streams_and_cites_only_known_answer_ids(self):
        known = "a" * 24
        session, _ = self._session({known: "# A\n## H\ntext"})
        unknown = "f" * 24
        def fake_stream(_system, _payload, on_delta):
            answer_text = f"Answer\n\n引用:\n{known} : A\n{unknown} : Unknown\n"
            on_delta(answer_text)
            return answer_text
        session.llm.stream = fake_stream
        events = []
        answer = run_cascade(session, "q", events.append)
        self.assertTrue(any(event["type"] == "answer_delta" and "text" in event for event in events))
        self.assertEqual(answer.cited_node_ids, [known])
        self.assertNotIn(unknown, answer.answer)
        self.assertTrue(any(e["type"] == "jev_gate" and e["status"] == "confirmed" for e in events))

    def test_answer_cache_hit_uses_mirror_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            session, _ = self._session({"a": "# A\n## H\ntext"}, cache=True)
            session.mirror = SimpleNamespace(directory=Path(tmp), version=2, ready=True)
            from cascade import _cache_path
            path = _cache_path(session, "question")
            path.parent.mkdir(parents=True)
            import json
            answer = {"question": "question", "answer": "cached", "cited_node_ids": [], "cited_nodes": [], "steps": 0}
            path.write_text(json.dumps({"version": 2, "answer": answer}), encoding="utf-8")
            from cascade import _read_cache
            self.assertEqual(_read_cache(session, "question").answer, "cached")

    def test_definer_rescue_stays_within_team_and_marks_route(self):
        seed_id, def_id, other_id = "a" * 24, "b" * 24, "c" * 24
        session, pages = self._session({
            seed_id: "# Seed\n## H\nuses Widget",
            def_id: "# Def\n## H\nWidget definition",
            other_id: "# Other\n## H\nWidget definition"})
        pages[seed_id] = pages[seed_id].model_copy(update={"path": f"/root/teamA/doc1/{seed_id}"})
        pages[def_id] = pages[def_id].model_copy(update={"path": f"/root/teamA/doc2/{def_id}"})
        pages[other_id] = pages[other_id].model_copy(update={"path": f"/root/teamB/doc/{other_id}"})
        cards = [
            IndexCard(target=pages[seed_id].path, title="Seed", entities=["Widget"]),
            IndexCard(target=pages[def_id].path, title="Def"),
            IndexCard(target=pages[other_id].path, title="Other"),
        ]
        state = SimpleNamespace(cards=cards,
            folders=[IndexCard(target="/root/teamA", title="Team A", kind="folder"),
                     IndexCard(target="/root/teamB", title="Team B", kind="folder")],
            children={"root/teamA": [IndexCard(target="/root/teamA/doc1", title="Doc", kind="document")],
                      "root/teamB": [IndexCard(target="/root/teamB/doc", title="Doc", kind="document")]})
        session.index_map.snapshot = lambda: state
        session.index_map.definers_for = lambda _entity: cards[1:]
        session.walker.collect = lambda *_args, **_kwargs: [
            WalkHit(seed_id, pages[seed_id].path, "Seed", "", .9, [])]
        seen = []
        session._jev_score_body = lambda _q, _d, page, *_args: (seen.append(page.id) or .9, 1)
        events = []
        run_cascade(session, "q", events.append)
        self.assertIn(def_id, seen)
        self.assertNotIn(other_id, seen)
        self.assertTrue(any(e.get("rescued") and e.get("kept") for e in events))

    def test_ambiguous_team_tree_disables_definer_rescue(self):
        seed_id, def_id = "a" * 24, "b" * 24
        session, pages = self._session({seed_id: "# Seed\n## H\nuses Widget",
                                        def_id: "# Def\n## H\nWidget definition"})
        pages[seed_id] = pages[seed_id].model_copy(update={"path": f"/root/one/{seed_id}"})
        pages[def_id] = pages[def_id].model_copy(update={"path": f"/root/two/{def_id}"})
        cards = [IndexCard(target=pages[seed_id].path, title="Seed", entities=["Widget"]),
                 IndexCard(target=pages[def_id].path, title="Def")]
        state = SimpleNamespace(cards=cards, folders=[], children={})
        session.index_map.snapshot = lambda: state
        session.index_map.definers_for = lambda _entity: [cards[1]]
        session.walker.collect = lambda *_args, **_kwargs: [
            WalkHit(seed_id, pages[seed_id].path, "Seed", "", .9, [])]
        seen = []
        session._jev_score_body = lambda _q, _d, page, *_args: (seen.append(page.id) or .9, 1)
        run_cascade(session, "q", lambda _e: None)
        self.assertNotIn(def_id, seen)

    def test_bins_start_before_all_documents_verify(self):
        first, blocked = "a" * 24, "b" * 24
        session, pages = self._session({first: "# A\n## H\nalpha", blocked: "# B\n## H\nbeta"},
                                       route_ids=[first, blocked])
        for key, team_doc in ((first, "docA"), (blocked, "docB")):
            pages[key] = pages[key].model_copy(update={"path": f"/root/{team_doc}/{key}"})
        for card in session.index_map.snapshot().cards:
            key = next(k for k, page in pages.items() if card.title == page.title)
            card.target = pages[key].path
        from threading import Event
        release, started = Event(), Event()
        original = session._jev_score_body
        def score(_q, _d, page, *args):
            if page.id == blocked:
                release.wait(2)
            return (.9, 1)
        session._jev_score_body = score
        session._run_packed_subagent = lambda *_args: (started.set() or {"answer": "report", "cited": [first]})
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_cascade, session, "q", lambda _e: None)
            try:
                self.assertTrue(started.wait(1), "first packed worker waited for the blocked document")
            finally:
                release.set()
            future.result(timeout=3)

    def test_early_stop_cancels_queued_bins(self):
        ids = [f"{i:024x}" for i in range(1, 5)]
        session, pages = self._session({key: f"# {key}\n## H\ncontent" for key in ids}, route_ids=ids)
        for key in ids:
            pages[key] = pages[key].model_copy(update={"path": f"/root/{key}/{key}"})
        for card in session.index_map.snapshot().cards:
            key = next(k for k, page in pages.items() if card.title == page.title)
            card.target = pages[key].path
        session.settings.cascade_subagents = 3
        session.jev.stop = .99
        starts = []
        session._run_packed_subagent = lambda index, *_args: (starts.append(index) or {"answer": "done", "cited": []})
        events = []
        run_cascade(session, "q", events.append)
        self.assertLessEqual(len(starts), 2)
        self.assertTrue(any(e["type"] == "cascade_early_stop" for e in events))

    def test_list_profile_never_early_stops(self):
        ids = ["a" * 24, "b" * 24, "c" * 24]
        session, pages = self._session({key: f"# {key}\n## H\ncontent" for key in ids}, route_ids=ids)
        for key in ids:
            pages[key] = pages[key].model_copy(update={"path": f"/root/{key}/{key}"})
        for card in session.index_map.snapshot().cards:
            key = next(k for k, page in pages.items() if card.title == page.title)
            card.target = pages[key].path
        session.jev.shape = [.99, 0.0]
        session.jev.stop = .99
        starts = []
        session._run_packed_subagent = lambda index, *_args: (starts.append(index) or {"answer": "done", "cited": []})
        events = []
        run_cascade(session, "list", events.append)
        self.assertNotIn("cascade_early_stop", [e["type"] for e in events])
        self.assertGreaterEqual(len(starts), 2)

    def test_documents_after_subagent_cap_reach_remaining_bins(self):
        ids = [f"{i:024x}" for i in range(1, 5)]
        session, pages = self._session({key: f"# {key}\n## H\ncontent {key}" for key in ids}, route_ids=ids)
        for key in ids:
            pages[key] = pages[key].model_copy(update={"path": f"/root/doc-{key}/{key}"})
        for card in session.index_map.snapshot().cards:
            key = next(k for k, page in pages.items() if card.title == page.title)
            card.target = pages[key].path
        session.settings.cascade_subagents = 2
        prompts = []
        session._run_packed_subagent = lambda _index, prompt, *_args: (prompts.append(prompt) or {"answer": "done", "cited": []})
        run_cascade(session, "q", lambda _e: None)
        self.assertTrue(any(ids[-1] in prompt for prompt in prompts))

    def test_single_subagent_still_gets_later_document_evidence(self):
        ids = ["a" * 24, "b" * 24, "c" * 24]
        session, pages = self._session({key: f"# {key}\n## H\ncontent {key}" for key in ids}, route_ids=ids)
        for key in ids:
            pages[key] = pages[key].model_copy(update={"path": f"/root/doc-{key}/{key}"})
        for card in session.index_map.snapshot().cards:
            key = next(k for k, page in pages.items() if card.title == page.title)
            card.target = pages[key].path
        session.settings.cascade_subagents = 1
        prompts = []
        session._run_packed_subagent = lambda _index, prompt, *_args: (prompts.append(prompt) or {"answer": "done", "cited": []})
        run_cascade(session, "q", lambda _e: None)
        self.assertEqual(len(prompts), 1)
        self.assertTrue(all(key in prompts[0] for key in ids))


if __name__ == "__main__":
    unittest.main()
