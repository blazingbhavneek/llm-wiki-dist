"""Behavioral WP-17 acceptance tests with a deterministic fake wiki/Jev engine."""

from __future__ import annotations

from threading import Event, Lock, Thread
import unittest
from types import SimpleNamespace

from cascade import run_cascade
from jev.types import JevResult
from markdown import IndexCard
from models import WikiPage
from walker import WalkHit


def page_id(number: int) -> str:
    return f"{number:024x}"


class FakeEngine:
    def __init__(self, shape=(0.0, 0.0), stop=0.0):
        self.shape = list(shape)
        self.stop = stop
        self.profile_called = False

    def score_many(self, state, questions):
        if not self.profile_called:
            self.profile_called = True
            return self.shape[:len(questions)]
        return [self.stop] * len(questions)

    def decide_batch(self, requests):
        return [JevResult(request.question.key, "はい", {"false": .1, "true": .9}, .9, .9, 1)
                for request in requests]

    @staticmethod
    def count_tokens(text):
        return max(1, len(text))


class FakeLLM:
    def complete(self, *_args):
        return "rewritten"

    @staticmethod
    def stream(_system, _payload, on_delta):
        answer = "Answer\n\n引用:\n"
        on_delta(answer)
        return answer


class CascadeAcceptanceTests(unittest.TestCase):
    def make_session(self, pages, *, shapes=(0.0, 0.0), stop=0.0,
                     subagents=2, context_tokens=10000, concurrency=2,
                     folders=(), children=None, definer_map=None, route_ids=None):
        cards = [IndexCard(target=page.path, title=page.title, document=page.document,
                           doc_ref=page.document, entities=list(getattr(page, "entities", [])))
                 for page in pages.values()]
        state = SimpleNamespace(cards=cards, folders=list(folders),
                                children=children or {"root/00-目次": []})

        class WalkerFake:
            def collect(_self, *args, **kwargs):
                return [WalkHit(page.id, page.path, page.title, "", .9, [])
                        for page in pages.values() if route_ids is None or page.id in route_ids]

        by_path = {page.path: page for page in pages.values()}
        session = SimpleNamespace(
            settings=SimpleNamespace(
                answer_cache=False, growi_root_path="/root", index_page_name="00-目次",
                walker_threshold=.2, jev_seed_threshold=.8, jev_chunk_tokens=1000,
                jev_chunk_overlap=0, subagent_concurrency=concurrency,
                cascade_section_threshold=.3, cascade_early_stop=.9,
                cascade_subagents=subagents, cascade_context_tokens=context_tokens,
                cascade_max_docs=100,
            ),
            jev=FakeEngine(shapes, stop), llm=FakeLLM(), walker=WalkerFake(),
            index_map=SimpleNamespace(
                snapshot=lambda: state,
                definers_for=lambda entity: list((definer_map or {}).get(entity, [])),
            ),
            _fetch_page=lambda *, page_id=None, path=None, **_kw:
                pages.get(page_id or "") or by_path.get(path or ""),
            _jev_score_body=lambda *_args: (.9, 1),
            _record_usage=lambda: None,
            mirror=None,
            has_overrides=False,
        )
        return session

    @staticmethod
    def wiki_pages(specs):
        return {
            page_id(number): WikiPage(
                id=page_id(number), path=path, title=title,
                body=f"# {title}\n## Facts\n{text}", document=doc, revision_id="r1",
            )
            for number, (path, title, text, doc) in specs.items()
        }

    def test_all_documents_reach_q4_when_documents_exceed_subagents(self):
        pages = self.wiki_pages({
            1: ("/root/doc1/page1", "One", "fact one", "/root/doc1"),
            2: ("/root/doc2/page2", "Two", "fact two", "/root/doc2"),
            3: ("/root/doc3/page3", "Three", "fact three", "/root/doc3"),
        })
        session = self.make_session(pages, subagents=1)
        prompts = []
        session._run_packed_subagent = lambda _i, prompt, *_args: (
            prompts.append(prompt) or {"answer": "report", "cited": []})

        run_cascade(session, "question", lambda _event: None)

        self.assertEqual(len(prompts), 1)
        for page in pages.values():
            self.assertIn(page.id, prompts[0])

    def test_oversized_document_uses_spare_bins_at_page_boundaries(self):
        pages = self.wiki_pages({
            1: ("/root/doc/page1", "One", "123456", "/root/doc"),
            2: ("/root/doc/page2", "Two", "abcdef", "/root/doc"),
            3: ("/root/doc/page3", "Three", "uvwxyz", "/root/doc"),
        })
        session = self.make_session(pages, subagents=2, context_tokens=8)
        prompts = []
        session._run_packed_subagent = lambda _i, prompt, *_args: (
            prompts.append(prompt) or {"answer": "report", "cited": []})

        run_cascade(session, "question", lambda _event: None)

        self.assertEqual(len(prompts), 2)
        # Page IDs in the evidence portion identify evidence ownership; next-read
        # suggestions can legitimately repeat IDs across bins.
        evidence_blocks = [prompt.split("担当する証拠:", 1)[1].split("次に読む候補:", 1)[0]
                           for prompt in prompts]
        page_appearances = {page.id: sum(page.id in block for block in evidence_blocks)
                            for page in pages.values()}
        self.assertTrue(all(count <= 1 for count in page_appearances.values()), page_appearances)
        self.assertEqual(sum(page_appearances.values()), 2)  # third page was overflow, not split

    def test_q5_starts_while_another_document_is_still_verifying(self):
        pages = self.wiki_pages({
            1: ("/root/doc1/page1", "One", "fact one", "/root/doc1"),
            2: ("/root/doc2/page2", "Two", "fact two", "/root/doc2"),
        })
        session = self.make_session(pages, subagents=2)
        started = Event()
        release_second = Event()
        errors = []

        def verify(_question, _document, page, *_args):
            if page.id == page_id(2):
                release_second.wait(3)
            return .9, 1

        session._jev_score_body = verify
        session._run_packed_subagent = lambda *_args: (started.set() or {"answer": "report", "cited": []})

        def run():
            try:
                run_cascade(session, "question", lambda _event: None)
            except Exception as exc:  # surfaced in the main test thread
                errors.append(exc)

        thread = Thread(target=run, daemon=True)
        thread.start()
        started_before_release = started.wait(2)
        release_second.set()
        thread.join(3)
        self.assertTrue(started_before_release, "first document's packed agent waited for second verification")
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_early_stop_cancels_queued_packed_work(self):
        pages = self.wiki_pages({
            number: (f"/root/doc{number}/page{number}", str(number), f"fact {number}", f"/root/doc{number}")
            for number in range(1, 4)
        })
        session = self.make_session(pages, subagents=3, concurrency=1, stop=.99)
        lock = Lock()
        started = []
        release_second = Event()
        stopped = Event()
        errors = []

        def runner(index, *_args):
            with lock:
                started.append(index)
                order = len(started)
            if order == 2:
                release_second.wait(3)
            return {"answer": "report", "cited": []}

        def emit(event):
            if event["type"] == "cascade_early_stop":
                stopped.set()

        session._run_packed_subagent = runner

        def run():
            try:
                run_cascade(session, "question", emit)
            except Exception as exc:
                errors.append(exc)

        thread = Thread(target=run, daemon=True)
        thread.start()
        early_stop_emitted = stopped.wait(2)
        release_second.set()
        thread.join(3)
        self.assertTrue(early_stop_emitted)
        self.assertFalse(thread.is_alive())
        self.assertLessEqual(len(started), 2)
        self.assertEqual(errors, [])

    def test_list_profile_never_early_stops(self):
        pages = self.wiki_pages({
            number: (f"/root/doc{number}/page{number}", str(number), f"fact {number}", f"/root/doc{number}")
            for number in range(1, 5)
        })
        session = self.make_session(pages, shapes=(.9, 0.0), stop=.99)
        started = []
        events = []
        session._run_packed_subagent = lambda index, *_args: (
            started.append(index) or {"answer": "report", "cited": []})

        run_cascade(session, "list all", events.append)

        self.assertEqual(len(started), len(pages))
        self.assertNotIn("cascade_early_stop", [event["type"] for event in events])

    def test_definer_rescue_stays_inside_team_folder(self):
        paths = {
            1: ("/Moove/teamA/doc1/source", "source", "entity info", "/Moove/teamA/doc1"),
            2: ("/Moove/teamA/doc2/definer", "same-team", "definition", "/Moove/teamA/doc2"),
            3: ("/Moove/teamB/doc/definer", "other-team", "definition", "/Moove/teamB/doc"),
        }
        pages = self.wiki_pages(paths)
        source_card = IndexCard(target=pages[page_id(1)].path, title="source", document="doc1",
                                doc_ref="/Moove/teamA/doc1", entities=["SharedEntity"])
        same_team = IndexCard(target=pages[page_id(2)].path, title="same-team", document="doc2",
                              doc_ref="/Moove/teamA/doc2")
        other_team = IndexCard(target=pages[page_id(3)].path, title="other-team", document="doc",
                               doc_ref="/Moove/teamB/doc")
        folder_a = IndexCard(target="/Moove/teamA", title="Team A", kind="folder")
        folder_b = IndexCard(target="/Moove/teamB", title="Team B", kind="folder")
        state_children = {
            "Moove/teamA": [IndexCard(target="/Moove/teamA/doc1", title="doc1", kind="document")],
            "Moove/teamB": [IndexCard(target="/Moove/teamB/doc", title="doc", kind="document")],
            "Moove/00-目次": [folder_a, folder_b],
        }
        session = self.make_session(
            pages, folders=[folder_a, folder_b], children=state_children,
            definer_map={"SharedEntity": [same_team, other_team]},
            route_ids=[page_id(1)],
        )
        session.settings.growi_root_path = "/"
        # The source card carries the entity edge from the published index.
        snapshot = session.index_map.snapshot()
        snapshot.cards = [source_card, same_team, other_team]
        scored = []
        session._jev_score_body = lambda _q, _d, page, *_args: (scored.append(page.id) or (.9, 1))
        session._run_packed_subagent = lambda *_args: {"answer": "report", "cited": []}

        events = []
        run_cascade(session, "question", events.append)

        self.assertIn(page_id(2), scored)
        self.assertNotIn(page_id(3), scored)
        rescue_docs = [event["document"] for event in events
                       if event["type"] == "route" and event.get("rescued")]
        self.assertTrue(rescue_docs)
        self.assertTrue(all("teamA" in document for document in rescue_docs))

    def test_ambiguous_team_scope_fails_closed_for_definer_rescue(self):
        pages = self.wiki_pages({
            1: ("/Moove/teamA/doc/source", "source", "entity info", "/Moove/teamA/doc"),
            2: ("/Moove/teamB/doc/definer", "definer", "definition", "/Moove/teamB/doc"),
        })
        source_card = IndexCard(target=pages[page_id(1)].path, title="source", document="doc",
                                doc_ref="/Moove/teamA/doc", entities=["SharedEntity"])
        definer = IndexCard(target=pages[page_id(2)].path, title="definer", document="doc",
                            doc_ref="/Moove/teamB/doc")
        session = self.make_session(
            pages, folders=[], children={}, definer_map={"SharedEntity": [definer]},
            route_ids=[page_id(1)],
        )
        session.settings.growi_root_path = "/"
        session.index_map.snapshot().cards = [source_card, definer]
        session._run_packed_subagent = lambda *_args: {"answer": "report", "cited": []}
        scored = []
        session._jev_score_body = lambda _q, _d, page, *_args: (scored.append(page.id) or (.9, 1))
        events = []

        run_cascade(session, "question", events.append)

        self.assertEqual(scored, [page_id(1)])
        self.assertFalse(any(event.get("rescued") for event in events))


if __name__ == "__main__":
    unittest.main()
