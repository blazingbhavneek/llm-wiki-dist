from __future__ import annotations

import threading
import unittest
import hashlib
from types import SimpleNamespace

from markdown import IndexCard
from models import WikiPage
from walker import Walker


ROOT = "/Moove/teamA"
INDEX = ROOT + "/00-目次"


class FakeMirror:
    ready = True

    def __init__(self, pages=(), children=None):
        self.pages = {page.path: page for page in pages}
        self.children = children or {}

    def path_of(self, page_id):
        return next((page.path for page in self.pages.values() if page.id == page_id), "")

    def get(self, *, page_id=None, path=None):
        if page_id:
            return next((p for p in self.pages.values() if p.id == page_id), None)
        return self.pages.get(path)

    def children_of(self, path):
        return list(self.children.get(path.rstrip("/") or "/", []))


class FakeEngine:
    def __init__(self, probs=None):
        self.probs = probs or {}
        self.questions = []
        self.batch_sizes = []

    def decide_batch(self, requests):
        self.batch_sizes.append(len(requests))
        out = []
        for request in requests:
            question = request.question
            ref = question.key
            kind = "route" if "フォルダまたは文書" in question.text else "page"
            full_ref = "/" + ref.strip("/")
            self.questions.append((kind, full_ref))
            out.append(SimpleNamespace(p_yes=self.probs.get((kind, ref), self.probs.get((kind, full_ref), 0.0))))
        return out


def page(path, body="body", title=None, parent_id=None, descendants=0):
    page_id = hashlib.md5(path.encode()).hexdigest()[:24]
    return WikiPage(id=page_id, path=path, title=title or path.rsplit("/", 1)[-1],
                    body=body, summary="summary", parent_id=parent_id, descendant_count=descendants)


def card(path, kind="", title=None, entities=None):
    return IndexCard(target=path, title=title or path.rsplit("/", 1)[-1], kind=kind,
                     summary="summary", entities=entities or [])


class FakeMap:
    def __init__(self, children, parent=None, cards=(), defs=None, client=None):
        self.state = SimpleNamespace(children={k.strip("/"): v for k, v in children.items()},
                                     parent={k.strip("/"): v.strip("/") for k, v in (parent or {}).items()}, cards=list(cards))
        self.defs = defs or {}
        self.client = client

    def snapshot(self):
        return self.state

    def definers_for(self, entity):
        return list(self.defs.get(entity, []))


def settings(**overrides):
    values = dict(growi_root_path=ROOT, index_page_name="00-目次", walker_k=3,
                  walker_threshold=0.5, walker_route_threshold=0.15,
                  walker_min_children=2, walker_max_items=150)
    values.update(overrides)
    return SimpleNamespace(**values)


def basic_tree(route_probs=None, page_probs=None):
    folder_a, folder_n = ROOT + "/A", ROOT + "/N"
    doc_a, doc_n = folder_a + "/00-目次", folder_n + "/00-目次"
    a1, a2, n1 = folder_a + "/page1", folder_a + "/page2", folder_n + "/news"
    root_cards = [card(folder_a, "folder", "API"), card(folder_n, "folder", "News")]
    folder_cards = [card(doc_a, "document", "API docs")]
    news_folder_cards = [card(doc_n, "document", "News docs")]
    api_pages = [card(a1, title="API guide"), card(a2, title="API values")]
    news_pages = [card(n1, title="News")]
    children = {INDEX: root_cards, folder_a: folder_cards, folder_n: news_folder_cards,
                doc_a: api_pages, doc_n: news_pages}
    parent = {folder_a: INDEX, folder_n: INDEX, doc_a: folder_a, doc_n: folder_n,
              a1: doc_a, a2: doc_a, n1: doc_n}
    pages = [page(p) for p in (a1, a2, n1)]
    mirror = FakeMirror(pages)
    index = FakeMap(children, parent, cards=api_pages + news_pages)
    probs = {(kind, ref): p for kind, table in (("route", route_probs or {}), ("page", page_probs or {})) for ref, p in table.items()}
    return Walker(index, mirror, FakeEngine(probs), settings()), index, mirror


class WalkerTests(unittest.TestCase):
    def test_irrelevant_folder_pruned_whole(self):
        walker, _, _ = basic_tree({ROOT + "/A": 0.9, ROOT + "/N": 0.05,
                                    ROOT + "/A/00-目次": 0.9})
        walker.settings.walker_min_children = 1
        walker.find("API", start_ref=None)
        self.assertNotIn(("page", ROOT + "/N/news"), walker.engine.questions)

    def test_beam_minimum_expands_best_two(self):
        walker, _, _ = basic_tree({ROOT + "/A": 0.1, ROOT + "/N": 0.1,
                                    ROOT + "/A/00-目次": 0.1, ROOT + "/N/00-目次": 0.1})
        walker.find("API", start_ref=None)
        self.assertIn(("route", ROOT + "/A/00-目次"), walker.engine.questions)
        self.assertIn(("route", ROOT + "/N/00-目次"), walker.engine.questions)

    def test_upward_step_finds_sibling_document(self):
        folder = ROOT + "/A"
        doc_a, doc_b = folder + "/00-目次", folder + "/B/00-目次"
        start, hit = folder + "/start", folder + "/B/answer"
        cards = [card(doc_a, "document"), card(doc_b, "document")]
        children = {folder: [card(doc_a, "document"), card(doc_b, "document")],
                    doc_a: [card(folder + "/start")], doc_b: [card(hit)]}
        index = FakeMap(children, {doc_a: folder, folder: INDEX, doc_b: folder, hit: doc_b, start: doc_a})
        mirror = FakeMirror([page(folder + "/start"), page(hit)])
        walker = Walker(index, mirror, FakeEngine({("page", hit): 0.95}), settings())
        found = walker.find("answer", start_ref=folder + "/start")
        self.assertEqual([item.path for item in found], [hit])

    def test_never_above_search_root(self):
        outside = "/Moove/teamB/00-目次"
        walker, _, _ = basic_tree({ROOT + "/A": 0.9, ROOT + "/N": 0.9})
        walker.find("x", start_ref=outside)
        self.assertTrue(all(ref.startswith(ROOT + "/") for _, ref in walker.engine.questions))

    def test_each_ref_scored_once(self):
        walker, index, mirror = basic_tree({ROOT + "/A": 0.9, ROOT + "/N": 0.9,
                                           ROOT + "/A/00-目次": 0.9, ROOT + "/N/00-目次": 0.9})
        target = ROOT + "/A/page1"
        start_page = page(ROOT + "/A/page2", f"[same]({target})")
        mirror.pages[start_page.path] = start_page
        walker.find("x", start_ref=start_page.path)
        self.assertEqual([ref for kind, ref in walker.engine.questions if kind == "page" and ref == target].__len__(), 1)

    def test_find_never_returns_its_start_page(self):
        walker, _, mirror = basic_tree(page_probs={ROOT + "/A/page1": 0.99})
        start = mirror.get(path=ROOT + "/A/page1")
        self.assertEqual(walker.find("same page", start_ref=start.id), [])

    def test_route_event_marks_beam_minimum_as_kept(self):
        walker, _, _ = basic_tree({ROOT + "/A": 0.1, ROOT + "/N": 0.1})
        walker.settings.walker_min_children = 1
        events = []
        walker.find("x", start_ref=None, emit=events.append)
        routes = [event for event in events if event["type"] == "route"
                  and event["node"] in {ROOT.strip("/") + "/A", ROOT.strip("/") + "/N"}]
        self.assertEqual(sum(event["kept"] for event in routes), 1)

    def test_legacy_root_untyped_cards_are_routed_as_documents(self):
        doc = ROOT + "/legacy/00-目次"
        target = ROOT + "/legacy/page"
        index = FakeMap({INDEX: [card(doc)], doc: [card(target)]}, {doc: INDEX, target: doc})
        mirror = FakeMirror([page(target)])
        engine = FakeEngine({("route", doc): 0.9, ("page", target): 0.9})
        walker = Walker(index, mirror, engine, settings())
        self.assertEqual([hit.path for hit in walker.find("x", start_ref=None)], [target])
        self.assertEqual(engine.questions, [("route", doc), ("page", target)])

    def test_budget_stops_walk(self):
        walker, _, _ = basic_tree({ROOT + "/A": 0.9, ROOT + "/N": 0.9,
                                  ROOT + "/A/00-目次": 0.9, ROOT + "/N/00-目次": 0.9},
                                 {ROOT + "/A/page1": 0.9, ROOT + "/A/page2": 0.8})
        walker.settings.walker_max_items = 5
        walker.find("API", start_ref=None)
        self.assertLessEqual(len(walker.engine.questions), 5)

    def test_early_stop_when_nothing_can_beat_results(self):
        walker, _, _ = basic_tree({ROOT + "/A": 0.9, ROOT + "/N": 0.3,
                                    ROOT + "/A/00-目次": 0.9},
                                 {ROOT + "/A/page1": 0.9, ROOT + "/A/page2": 0.9})
        walker.find("API", start_ref=None, k=2)
        self.assertNotIn(("page", ROOT + "/N/news"), walker.engine.questions)

    def test_missing_index_folder_expanded_from_mirror(self):
        folder = ROOT + "/legacy"
        item = page(folder + "/page")
        root_cards = [card(folder, "folder")]
        index = FakeMap({INDEX: root_cards}, {folder: INDEX})
        mirror = FakeMirror([item], {folder: [item]})
        walker = Walker(index, mirror, FakeEngine({("page", item.id): 0.9}), settings())
        self.assertEqual([hit.path for hit in walker.find("x", start_ref=None)], [item.path])

    def test_missing_document_index_lists_its_directory(self):
        directory = ROOT + "/legacy"
        index_ref = directory + "/00-目次"
        item = page(directory + "/page")
        tree = FakeMap({INDEX: [card(index_ref, "document")]}, {})
        mirror = FakeMirror([item], {directory: [item]})
        walker = Walker(tree, mirror, FakeEngine({("page", item.id): 0.9}), settings())
        self.assertEqual([hit.path for hit in walker.find("x", start_ref=None)], [item.path])

    def test_document_index_routes_subfolders_and_scores_pages_in_one_batch(self):
        folder = ROOT + "/A"
        doc, subfolder = folder + "/00-目次", folder + "/sub"
        subdoc, direct, nested = subfolder + "/00-目次", folder + "/direct", folder + "/sub/nested"
        index = FakeMap({INDEX: [card(folder, "folder")], folder: [card(doc, "document")],
                         doc: [card(direct), card(subfolder, "folder")],
                         subfolder: [card(subdoc, "document")], subdoc: [card(nested)]},
                        {folder: INDEX, doc: folder, direct: doc, subfolder: doc,
                         subdoc: subfolder, nested: subdoc})
        mirror = FakeMirror([page(direct), page(nested)])
        engine = FakeEngine({("route", subfolder): 0.9, ("route", subdoc): 0.9, ("page", direct): 0.9,
                             ("page", nested): 0.95})
        walker = Walker(index, mirror, engine, settings())
        found = walker.find("x", start_ref=None)
        self.assertEqual({hit.path for hit in found}, {direct, nested})
        self.assertEqual(engine.batch_sizes, [1, 1, 2, 1, 1])

    def test_collect_returns_all_pages_from_at_most_max_docs(self):
        walker, _, _ = basic_tree({ROOT + "/A": 0.9, ROOT + "/N": 0.9,
                                   ROOT + "/A/00-目次": 0.9, ROOT + "/N/00-目次": 0.9},
                                  {ROOT + "/A/page1": 0.9, ROOT + "/A/page2": 0.8,
                                   ROOT + "/N/news": 0.9})
        found = walker.collect("x", start_ref=None, max_docs=2, page_threshold=0.5)
        self.assertEqual(len(found), 3)


if __name__ == "__main__":
    unittest.main()
