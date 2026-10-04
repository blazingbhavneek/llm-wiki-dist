"""Best-first, team-scoped Jev traversal over index cards and mirrored pages."""

from __future__ import annotations

import heapq
import itertools
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

import markdown as md
from jev.types import JevQuestion, JevRequest
from models import WikiPage
from gateway import jev_page_question, jev_route_question

log = logging.getLogger(__name__)


@dataclass
class WalkHit:
    page_id: str
    path: str
    title: str
    summary: str
    p: float
    trail: list[str]


class Walker:
    def __init__(self, index_map: Any, mirror: Any, engine: Any, settings: Any) -> None:
        self.index_map, self.mirror, self.engine, self.settings = index_map, mirror, engine, settings

    @staticmethod
    def _ref(value: str) -> str:
        return (value or "").strip().strip("/")

    def _root(self) -> str:
        root = (getattr(self.settings, "growi_root_path", "/") or "/").rstrip("/")
        name = getattr(self.settings, "index_page_name", "00-目次")
        return self._ref(f"{root}/{name}")

    def _in_scope(self, ref: str) -> bool:
        path = ref
        if re.fullmatch(r"[0-9a-fA-F]{24}", ref):
            path = self.mirror.path_of(ref) if self.mirror is not None else ""
        path = "/" + path.strip("/")
        root = "/" + (getattr(self.settings, "growi_root_path", "/") or "/").strip("/")
        return bool(path) and (root == "/" or path == root or path.startswith(root + "/"))

    def _get_page(self, ref: str) -> WikiPage | None:
        if not ref:
            return None
        is_id = bool(re.fullmatch(r"[0-9a-fA-F]{24}", ref))
        if self.mirror is not None:
            return self.mirror.get(page_id=ref) if is_id else self.mirror.get(path="/" + ref)
        client = getattr(self.index_map, "client", None)
        if client is None:
            return None
        return client.get_page(page_id=ref) if is_id else client.get_page(path="/" + ref)

    def _children(self, path: str) -> list[WikiPage]:
        if self.mirror is not None and self.mirror.ready:
            return self.mirror.children_of(path)
        client = getattr(self.index_map, "client", None)
        return client.list_children(path=path) if client is not None else []

    def _listing_path(self, ref: str) -> str:
        path = (self.mirror.path_of(ref) if self.mirror is not None and
                re.fullmatch(r"[0-9a-fA-F]{24}", ref) else "") or "/" + ref.strip("/")
        index_name = getattr(self.settings, "index_page_name", "00-目次")
        return path.rsplit("/", 1)[0] if path.rsplit("/", 1)[-1] == index_name else path

    def _start(self, start_ref: str | None, state: Any) -> tuple[str, WikiPage | None]:
        if not start_ref:
            return self._root(), None
        page = self._get_page(self._ref(start_ref))
        ref = self._ref(page.path) if page and page.path else self._ref(start_ref)
        if page and ref in state.children:
            return ref, page
        parent = state.parent.get(ref)
        if parent:
            return parent, page
        # An unindexed page starts from its nearest in-scope folder index.
        path = page.path if page else ("/" + ref if not ref.startswith("/") else ref)
        root = (getattr(self.settings, "growi_root_path", "/") or "/").rstrip("/")
        while path and path != root and path != "/":
            path = path.rsplit("/", 1)[0] or "/"
            candidate = self._ref(f"{path}/{getattr(self.settings, 'index_page_name', '00-目次')}")
            if candidate in state.children:
                return candidate, page
        return self._root(), page

    @staticmethod
    def _card_state(parent: str, card: Any) -> dict:
        return {"document": "/" + parent, "card": {
            "kind": getattr(card, "kind", ""), "title": card.title,
            "path": card.target, "summary": card.summary,
            "chapter": card.chapter, "keywords": card.keywords,
            "entities": card.entities, "contents": card.contents,
        }}

    @staticmethod
    def _page_state(page: WikiPage) -> dict:
        return {"page": {"title": page.title, "path": page.path,
                         "text": (page.body or "")[:1500]}}

    def _score(self, cards: list[tuple[str, Any, str]], parent: str, description: str,
               *, route: bool, remaining: int) -> list[tuple[str, Any, float, str]]:
        cards = cards[:remaining]
        if not cards:
            return []
        question_text = jev_route_question if route else jev_page_question
        requests = [JevRequest(self._card_state(parent, card),
                               JevQuestion(text=question_text(description), key=ref))
                    for ref, card, _kind in cards]
        answers = self.engine.decide_batch(requests)
        return [(ref, card, float(result.p_yes), kind)
                for (ref, card, kind), result in zip(cards, answers)]

    def _score_mixed(self, cards: list[tuple[str, Any, str]], parent: str, description: str,
                     remaining: int) -> list[tuple[str, Any, float, str]]:
        cards = cards[:remaining]
        requests = [JevRequest(self._card_state(parent, card), JevQuestion(
            text=(jev_page_question if kind == "page" else jev_route_question)(description), key=ref))
            for ref, card, kind in cards]
        answers = self.engine.decide_batch(requests) if requests else []
        return [(ref, card, float(result.p_yes), kind)
                for (ref, card, kind), result in zip(cards, answers)]

    def find(self, description: str, *, start_ref: str | None, k: int | None = None,
             stop_event: Any = None, emit: Callable | None = None, agent: int | None = None) -> list[WalkHit]:
        return self._walk(description, start_ref=start_ref, k=self.settings.walker_k if k is None else k,
                          page_threshold=self.settings.walker_threshold, max_items=self.settings.walker_max_items,
                          stop_event=stop_event, emit=emit, agent=agent)

    def collect(self, description: str, *, start_ref: str | None, max_docs: int,
                page_threshold: float, stop_event: Any = None,
                emit: Callable | None = None) -> list[WalkHit]:
        return self._walk(description, start_ref=start_ref, k=None, max_docs=max_docs,
                          page_threshold=page_threshold, max_items=self.settings.walker_max_items,
                          stop_event=stop_event, emit=emit)

    def _walk(self, description: str, *, start_ref: str | None, k: int | None, page_threshold: float,
              max_items: int, stop_event: Any, emit: Callable | None, agent: int | None = None,
              max_docs: int | None = None) -> list[WalkHit]:
        state = self.index_map.snapshot()
        root = self._root()
        start, start_page = self._start(start_ref, state)
        if not self._in_scope(start):
            start = root
        if not self._in_scope(start):
            return []
        serial = itertools.count()
        frontier: list[tuple[float, int, int, str, str, Any, list[str]]] = []
        heapq.heappush(frontier, (-1.0, 0, next(serial), start, "tree", None, [start]))
        scored: dict[str, float] = {}
        expanded: set[str] = set()
        results: dict[str, WalkHit] = {}
        documents_seen: set[str] = set()
        doc_refs = {self._ref(doc.target) for doc in getattr(state, "docs", [])}
        doc_refs.update(getattr(state, "cards_by_document", {}).keys())
        folder_refs = {self._ref(folder.target) for folder in getattr(state, "folders", [])}
        questions = 0
        start_doc = start
        current_refs = {self._ref(start_ref or "")}
        if start_page is not None:
            page_ref = self._ref(start_page.path)
            current_refs.update({self._ref(start_page.path), self._ref(start_page.id)})
            start_doc = state.parent.get(page_ref, start)
        related_added = False

        def push(ref: str, priority: float, depth: int, kind: str, card: Any, trail: list[str]) -> None:
            ref = self._ref(ref)
            if ref and ref not in expanded and self._in_scope(ref):
                heapq.heappush(frontier, (-priority, depth, next(serial), ref, kind, card, trail))

        if getattr(self.settings, "walker_es_rescue", False):
            client = getattr(self.index_map, "client", None)
            if client is not None:
                try:
                    for hit in client.search_pages(description,
                            path=getattr(self.settings, "growi_root_path", "/"), limit=10):
                        page = getattr(hit, "page", None)
                        if page is not None:
                            push(page.id or page.path, 0.5, 1, "page", None, [start, self._ref(page.path or page.id)])
                except Exception as exc:  # noqa: BLE001 - optional rescue must not break the walk
                    log.info("walker ES rescue failed: %s", exc)

        while frontier and questions < max_items:
            if stop_event is not None and stop_event.is_set():
                break
            if k is not None and len(results) >= k and \
                    -frontier[0][0] < sorted((hit.p for hit in results.values()), reverse=True)[k - 1]:
                break
            negp, depth, _order, ref, node_kind, card, trail = heapq.heappop(frontier)
            if ref in expanded:
                continue
            expanded.add(ref)
            cards = state.children.get(ref, [])

            if node_kind == "page":
                if ref in scored:
                    continue
                page = self._get_page(ref)
                if page is None:
                    continue
                state_body = self._card_state(state.parent.get(ref, ""), card) if card is not None else self._page_state(page)
                result = self.engine.decide_batch([JevRequest(state_body,
                    JevQuestion(text=jev_page_question(description), key=ref))])[0]
                questions += 1
                probability = float(result.p_yes)
                scored[ref] = probability
                if probability >= page_threshold and ref not in current_refs:
                    results[ref] = WalkHit(page.id, page.path, page.title, page.summary or "", probability, trail)
                continue

            if node_kind != "page" and ref not in state.children:
                path = self._listing_path(ref)
                pages = self._children(path)
                if not pages:
                    continue
                for page in pages:
                    child_ref = self._ref(page.id or page.path)
                    if page.descendant_count:
                        # Missing-index folders are never pruned; descend via the mirror.
                        push(self._ref(page.path), max(0.0, -negp), depth + 1, "tree", page, trail + [child_ref])
                    else:
                        push(child_ref, max(0.0, -negp), depth + 1, "page", None, trail + [child_ref])
                continue

            is_doc = ref != root and (ref in doc_refs or node_kind == "document" or
                (node_kind == "tree" and ref not in folder_refs and bool(cards) and
                 any(not getattr(item, "kind", "") for item in cards)))
            if is_doc:
                if max_docs is not None:
                    documents_seen.add(ref)
                    if len(documents_seen) > max_docs:
                        continue
                pending = [(self._ref(item.target), item,
                            getattr(item, "kind", "") or "page") for item in cards
                           if self._ref(item.target) not in scored]
                remaining = max_items - questions
                evaluated = self._score_mixed(pending, ref, description, remaining)
                questions += len(evaluated)
                route_candidates = []
                for page_ref, page_card, probability, kind in evaluated:
                    scored[page_ref] = probability
                    if kind == "page" and probability >= page_threshold and page_ref not in current_refs:
                        page = self._get_page(page_ref)
                        results[page_ref] = WalkHit(
                            page.id if page else (page_ref if re.fullmatch(r"[0-9a-fA-F]{24}", page_ref) else ""),
                            page.path if page else (page_card.target if page_card.target.startswith("/") else "/" + page_card.target),
                            page.title if page else page_card.title,
                            page.summary if page and page.summary else page_card.summary,
                            probability, trail + [page_ref])
                    elif kind in {"folder", "document"}:
                        route_candidates.append((page_ref, page_card, probability, kind))
                best_routes = {item[0] for item in sorted(route_candidates, key=lambda item: item[2], reverse=True)
                               [:self.settings.walker_min_children]}
                for child_ref, child_card, probability, kind in route_candidates:
                    kept = probability >= self.settings.walker_route_threshold or child_ref in best_routes
                    if emit:
                        emit({"type": "route", "node": child_ref, "p": probability, "kept": kept})
                    if kept:
                        push(child_ref, probability, depth + 1, kind, child_card, trail + [child_ref])
                parent = state.parent.get(ref)
                if parent and self._within_root(parent, root):
                    push(parent, -negp, max(0, depth - 1), "tree", None, trail[:-1] or [parent])
                if start_page is not None and ref == start_doc and not related_added:
                    related_added = True
                    self._related(start_page, state, description, push, trail)
                continue

            if cards:
                pending = [(self._ref(item.target), item,
                            getattr(item, "kind", "") or ("page" if is_doc else "document"))
                           for item in cards if self._ref(item.target) not in scored]
                evaluated = self._score(pending, ref, description, route=True,
                                        remaining=max_items - questions)
                questions += len(evaluated)
                ordered, route_candidates = [], []
                for child_ref, child_card, probability, kind in evaluated:
                    scored[child_ref] = probability
                    ordered.append((child_ref, child_card, probability, kind))
                    if kind in {"folder", "document"}:
                        route_candidates.append((child_ref, child_card, probability, kind))
                best = {item[0] for item in sorted(route_candidates, key=lambda item: item[2], reverse=True)
                        [:self.settings.walker_min_children]}
                for child_ref, child_card, probability, kind in ordered:
                    if kind in {"folder", "document"}:
                        kept = probability >= self.settings.walker_route_threshold or child_ref in best
                        if emit:
                            emit({"type": "route", "node": child_ref, "p": probability, "kept": kept})
                        if kept:
                            push(child_ref, probability, depth + 1, kind, child_card, trail + [child_ref])
                    elif kind == "page":
                        push(child_ref, probability, depth + 1, "page", child_card, trail + [child_ref])
            elif not cards and node_kind != "page":
                path = self._listing_path(ref)
                for page in self._children(path):
                    child_ref = self._ref(page.id or page.path)
                    push(child_ref, -negp, depth + 1, "page", page, trail + [child_ref])

        hits = sorted(results.values(), key=lambda hit: hit.p, reverse=True)
        if k is not None:
            hits = hits[:k]
        if emit:
            emit({"type": "find", "agent": agent, "description": description,
                  "results": [{"id": hit.page_id, "title": hit.title, "p": hit.p} for hit in hits],
                  "questions": questions})
        return hits

    def _within_root(self, ref: str, root: str) -> bool:
        return self._in_scope(ref)

    def _related(self, page: WikiPage, state: Any, description: str,
                 push: Callable, trail: list[str]) -> None:
        for link in md.extract_links(page.body):
            target = md.resolve_target(page.path, link.raw_target, getattr(self.settings, "growi_root_path", "/"))
            if target:
                ref = target.page_id or target.path
                push(ref, 0.5, 1, "page", None, trail + [self._ref(ref)])
        entities = set()
        card = next((item for item in state.cards if self._ref(item.target) == self._ref(page.path)), None)
        if card:
            entities.update(card.entities)
        for entity in entities:
            for definer in self.index_map.definers_for(entity):
                push(definer.target, 0.5, 1, "page", definer, trail + [self._ref(definer.target)])
