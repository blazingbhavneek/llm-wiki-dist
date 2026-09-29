"""Live-GROWI researcher: lead agent + bounded parallel subagents + SSE events.

Agent orchestration, prompts, and the event vocabulary live in the neo version.
All SQLite/vector storage has been replaced by live GROWI operations, request-local
memoization, and a small process-local page cache. No `sqlite3`, no `GraphStore`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import unicodedata
import httpx
from contextlib import contextmanager
from urllib.parse import quote
from collections import Counter, OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from queue import Queue
from threading import Event, Lock, Thread
from typing import Any, Callable, Iterable, Iterator

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

import markdown as md
from config import Settings
from gateway import (
    Embedder,
    JevQuestion,
    LlmClient,
    Reranker,
    build_jev,
    cosine,
    jev_question_text,
    jev_toc_question,
    llm_http_client,
    normalize_scores,
)
from jev.types import JevQuestion as EngineJevQuestion, JevRequest
from growi_client import GrowiAPIError, GrowiSearchClient
from mirror import Mirror
from models import AgentAnswer, Evidence, WikiLink, WikiPage, make_link_id
from page_cache import PageCache
from walker import Walker
from prompts import (
    ANSWER_VERIFY_PROMPT,
    FOLLOWUP_ANSWER_PROMPT,
    JEV_CANDIDATE_COVERAGE_PROMPT,
    JEV_INTENT_PROMPT,
    JEV_QUERY_REWRITE_PROMPT,
    JEV_TOC_SUMMARY_PROMPT,
    MAIN_AGENT_SYSTEM_PROMPT,
    PACKED_SUBAGENT_PROMPT,
    REPORT_FOLD_PROMPT,
    ROUTER_PROMPT,
    SHALLOW_ANSWER_PROMPT,
    SUBAGENT_SYSTEM_PROMPT,
    SYNTHESIS_PROMPT,
)

log = logging.getLogger("growi_search_researcher")

IMAGE_UNIT_RE = re.compile(r"<image-unit\b[^>]*>.*?</image-unit>", re.I | re.S)
IMAGE_DESC_RE = re.compile(
    r"<image-description\b[^>]*>(.*?)</image-description>", re.I | re.S
)
IMAGE_MEDIA_RE = re.compile(r"<image-media\b[^>]*>.*?</image-media>", re.I | re.S)
HTML_IMAGE_RE = re.compile(r"<(?:img|embed)\b[^>]*>", re.I | re.S)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\([^\n)]*\)", re.I)
DATA_IMAGE_RE = re.compile(
    r"data:image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+", re.I
)
MAX_TEXT = 2500


class AgentStopped(Exception):
    """Raised when the client cancels an in-flight run."""


# ponytail: minimal prompt-injection/image stripping; neo's richer set lives
# in the graph package we deliberately do not import.
def sanitize_text(value: Any) -> str:
    text = value if isinstance(value, str) else str(value or "")

    def _unit(match: re.Match) -> str:
        desc = IMAGE_DESC_RE.search(match.group(0))
        return desc.group(1).strip() if desc else ""

    text = IMAGE_UNIT_RE.sub(_unit, text)
    text = IMAGE_MEDIA_RE.sub("", text)
    text = HTML_IMAGE_RE.sub("[image omitted]", text)
    text = MARKDOWN_IMAGE_RE.sub("[image omitted]", text)
    text = DATA_IMAGE_RE.sub("[image omitted]", text)
    return text


def sanitize_messages(messages: list[Any]) -> list[Any]:
    cleaned: list[Any] = []
    for message in messages:
        if isinstance(message, dict):
            entry = dict(message)
            if isinstance(entry.get("content"), str):
                entry["content"] = sanitize_text(entry["content"])
            elif isinstance(entry.get("content"), list):
                entry["content"] = [
                    part
                    for part in entry["content"]
                    if not (isinstance(part, dict) and str(part.get("type", "")).lower() in {"image", "image_url"})
                ]
            cleaned.append(entry)
            continue
        if isinstance(message, BaseModel):
            content = getattr(message, "content", None)
            if isinstance(content, str):
                cleaned.append(message.model_copy(update={"content": sanitize_text(content)}))
                continue
            if isinstance(content, list):
                cleaned.append(
                    message.model_copy(
                        update={
                            "content": [
                                part
                                for part in content
                                if not (isinstance(part, dict) and str(part.get("type", "")).lower() in {"image", "image_url"})
                            ]
                        }
                    )
                )
                continue
        cleaned.append(message)
    return cleaned


def clean_ref(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*[-*]\s*", "", text).strip()
    text = text.strip("`'\" \t\r\n")
    text = re.sub(r"^id\s*:\s*", "", text, flags=re.I).strip()
    if "|" in text:
        text = text.split("|", 1)[0].strip()
    text = re.sub(r"\s+\([^)]*\)\s*$", "", text).strip()
    return text.strip("`'\" \t\r\n,;")


def node_ref(page: WikiPage | None) -> dict[str, str]:
    return {"id": page.id, "title": page.title or page.id} if page else {}


def dedupe(ids: list[str]) -> list[str]:
    seen: list[str] = []
    for value in ids:
        if value and value not in seen:
            seen.append(value)
    return seen


# --- process-local page cache (bounded, TTL, in-memory only) ------------


def _norm_entity(text: str) -> str:
    """NFKC + casefolded + whitespace-collapsed entity key."""
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def _is_page_id(reference: str) -> bool:
    ref = str(reference or "").strip().removeprefix("/").removesuffix("/")
    # ponytail: 24-hex page names are treated as IDs; support names when GROWI permits them.
    return bool(re.fullmatch(r"[0-9a-fA-F]{24}", ref))


@dataclass
class _MapState:
    """One immutable snapshot of the index map; swapped atomically on refresh."""
    docs: list[md.IndexCard] = field(default_factory=list)
    cards: list[md.IndexCard] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=list)
    grams: list[set[str]] = field(default_factory=list)
    df: Counter = field(default_factory=Counter)
    entity_definers: dict[str, list[md.IndexCard]] = field(default_factory=dict)
    cards_by_document: dict[str, list[md.IndexCard]] = field(default_factory=dict)
    folders: list[md.IndexCard] = field(default_factory=list)
    parent: dict[str, str] = field(default_factory=dict)
    children: dict[str, list[md.IndexCard]] = field(default_factory=dict)
    mirror_version: int = -1
    entity_index: dict[str, list[tuple[str, str, int]]] = field(default_factory=dict)
    short_entities: list[tuple[str, str, int]] = field(default_factory=list)


class IndexMap:
    """Cards from <root>/00-目次 and every <doc>/00-目次, cached in memory with a TTL.

    Nothing is written to disk. A refresh reads 1 + N index pages in parallel and
    embeds only cards whose text changed; while it runs, questions keep using the
    previous snapshot (stale-while-revalidate). Only the very first load blocks."""

    def __init__(self, client: GrowiSearchClient, settings: Settings, embedder: Embedder | None, reranker: Reranker | None, mirror: Mirror | None = None) -> None:
        self.client, self.settings, self.embedder, self.reranker = client, settings, embedder, reranker
        self.mirror = mirror
        self._lock = Lock()
        self._expires = 0.0
        self._refreshing = False
        self._state = _MapState()
        self._vector_cache: dict[str, list[float]] = {}  # card text -> vector, reused across refreshes

    @staticmethod
    def grams(text: str) -> set[str]:
        """Keyword units that work without a Japanese tokenizer: latin/digit words plus
        character bigrams of everything else (same idea as the linker's trigram FTS)."""
        text = unicodedata.normalize("NFKC", text or "").lower()
        out = set(re.findall(r"[a-z0-9]{2,}", text))
        run = re.sub(r"[a-z0-9\s\W]+", " ", text)
        for chunk in run.split():
            out.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
            if len(chunk) == 1:
                out.add(chunk)
        return out

    @staticmethod
    def card_text(card: md.IndexCard) -> str:
        return (f"{card.document} > {card.title}\n{card.summary}\n"
                f"キーワード: {'、'.join(card.keywords)}\nエンティティ: {'、'.join(card.entities)}")

    @staticmethod
    def card_page(card: md.IndexCard) -> WikiPage:
        ref = card.target.strip("/")
        is_id = _is_page_id(ref)
        return WikiPage(id=ref if is_id else card.target, path="" if is_id else card.target,
                        title=card.title, summary=card.summary, document=card.document, cluster=card.chapter)

    def _read(self, target: str) -> WikiPage | None:
        ref = target.strip("/")
        try:
            if self.mirror is not None and self.mirror.ready:
                return self.mirror.get(page_id=ref) if _is_page_id(ref) else self.mirror.get(path=target)
            if _is_page_id(ref):
                return self.client.get_page(page_id=ref)
            return self.client.get_page(path=target)
        except GrowiAPIError as exc:
            log.info("index page read failed (%s): %s", target, exc)
            return None

    @staticmethod
    def _ref(target: str) -> str:
        return (target or "").strip().strip("/")

    def _project_root_seeds(self, root: str) -> list[tuple[WikiPage, str, md.IndexCard | None, int]]:
        """First-level <child>/00-目次 pages under root; children without one are skipped."""
        name = self.settings.index_page_name
        try:
            if self.mirror is not None and self.mirror.ready:
                children = self.mirror.children_of(root or "/")
            else:
                children = self.client.list_children(path=root or "/")
        except GrowiAPIError as exc:
            log.info("index project roots unreadable (%s): %s", root or "/", exc)
            return []
        seeds = []
        for child in children:
            if not child.path:
                continue
            target = f"{child.path.rstrip('/')}/{name}"
            sub = self._read(target)
            if sub is None or not md.is_index_page(sub.body):
                continue
            seeds.append((sub, self._ref(sub.path or target), None, 0))
        return seeds

    def _build(self) -> _MapState:
        """Fetch + parse + embed into a fresh state. Touches no shared fields except the vector cache."""
        mirror_version = self.mirror.version if self.mirror is not None and self.mirror.ready else -1
        root = self.settings.growi_root_path.rstrip("/")
        if root:
            index_path = f"{root}/{self.settings.index_page_name}"
            page = self._read(index_path)
            pending = ([(page, self._ref(page.path or index_path), None, 0)]
                       if page is not None and md.is_index_page(page.body)
                       else [])
        else:
            # GROWI / is a container of projects, not itself a project index.
            pending = self._project_root_seeds("/")
        root_refs = {ref for _page, ref, _document, _depth in pending}
        docs: list[md.IndexCard] = []
        cards: list[md.IndexCard] = []
        entity_definers: dict[str, list[md.IndexCard]] = {}
        cards_by_document: dict[str, list[md.IndexCard]] = {}
        folders: list[md.IndexCard] = []
        parent: dict[str, str] = {}
        children: dict[str, list[md.IndexCard]] = {}
        visited: set[str] = set()
        with ThreadPoolExecutor(max_workers=max(1, self.settings.growi_concurrency), thread_name_prefix="index-map") as pool:
            while pending:
                next_pages: list[tuple[WikiPage, str, md.IndexCard | None, int]] = []
                reads: list[tuple[md.IndexCard, str, md.IndexCard | None, int]] = []
                for current, current_ref, document, depth in pending:
                    if current_ref in visited:
                        continue
                    visited.add(current_ref)
                    kind = md.index_kind(current.body)
                    listed = md.parse_index(current.body)
                    children[current_ref] = listed
                    if kind == "document" or (kind == "1" and current_ref not in root_refs):
                        doc_ref = self._ref(document.target) if document else current_ref
                        title = document.title if document else current.title
                        doc_cards: list[md.IndexCard] = []
                        for card in listed:
                            ref = self._ref(card.target)
                            if card.kind in {"folder", "document"}:
                                parent[ref] = current_ref
                                if card.kind == "folder":
                                    folders.append(card)
                                    if depth < 32:
                                        reads.append((card, ref, None, depth + 1))
                                    else:
                                        log.info("index folder depth limit reached (%s)", card.target)
                                else:
                                    card.doc_ref = ref
                                    docs.append(card)
                                    if depth < 32:
                                        reads.append((card, ref, card, depth + 1))
                                continue
                            card.document = title
                            card.doc_ref = doc_ref
                            parent[ref] = current_ref
                            cards.append(card)
                            doc_cards.append(card)
                            for entity in card.entities:
                                entity_definers.setdefault(_norm_entity(entity), []).append(card)
                        cards_by_document[doc_ref] = doc_cards
                        continue
                    for card in listed:
                        ref = self._ref(card.target)
                        parent[ref] = current_ref
                        if card.kind == "folder":
                            folders.append(card)
                            if depth < 32:
                                reads.append((card, ref, None, depth + 1))
                            else:
                                log.info("index folder depth limit reached (%s)", card.target)
                        elif card.kind in {"document", ""}:
                            card.doc_ref = ref
                            docs.append(card)
                            if depth < 32:
                                reads.append((card, ref, card, depth + 1))
                results = list(pool.map(lambda item: self._read(item[0].target), reads))
                for (card, ref, document, depth), sub in zip(reads, results):
                    if sub is None or not md.is_index_page(sub.body):
                        if card.kind == "folder":
                            log.info("index folder skipped (missing or non-index page): %s", card.target)
                        continue
                    next_pages.append((sub, ref, document, depth))
                pending = next_pages
        texts = [self.card_text(c) for c in cards]
        vectors: list[list[float]] = []
        if self.embedder is not None and cards:
            cache = self._vector_cache
            missing = [t for t in dict.fromkeys(texts) if t not in cache]
            try:
                for start in range(0, len(missing), 64):
                    batch = missing[start:start + 64]
                    cache.update(zip(batch, self.embedder.embed_documents(batch)))
                vectors = [cache[t] for t in texts]
            except Exception as exc:  # noqa: BLE001 - fall back to keyword overlap
                log.info("index embed failed: %s", exc)
                vectors = []
            self._vector_cache = {t: cache[t] for t in set(texts) if t in cache}  # drop vanished cards
        grams = [self.grams(t) for t in texts]
        df: Counter = Counter()
        for g in grams:
            df.update(g)
        names = sorted({name for card in cards for name in card.entities}, key=len, reverse=True)
        entity_index: dict[str, list[tuple[str, str, int]]] = {}
        short_entities = []
        for rank, name in enumerate(names):
            normalized = _norm_entity(name)
            if len(normalized) < 2:
                short_entities.append((normalized, name, rank))
            else:
                entity_index.setdefault(normalized[:2], []).append((normalized, name, rank))
        return _MapState(docs=docs, cards=cards, vectors=vectors, grams=grams, df=df,
                         entity_definers=entity_definers, cards_by_document=cards_by_document,
                         folders=folders, parent=parent, children=children,
                         mirror_version=mirror_version, entity_index=entity_index,
                         short_entities=short_entities)

    def _refresh(self) -> None:
        state: _MapState | None = None
        try:
            state = self._build()
        except GrowiAPIError as exc:
            log.info("index map load failed: %s", exc)
        except Exception:  # noqa: BLE001 - never leave _refreshing stuck
            log.exception("index map refresh failed")
        with self._lock:
            if state is not None:
                self._state = state
            self._expires = time.monotonic() + self.settings.index_cache_ttl
            self._refreshing = False

    def snapshot(self) -> _MapState:
        """Current state; expired + warm -> refresh in the background, expired + cold -> block."""
        with self._lock:
            now = time.monotonic()
            if now <= self._expires or self._refreshing:
                return self._state
            if (self.mirror is not None and self.mirror.ready and
                    self._state.mirror_version == self.mirror.version):
                self._expires = now + self.settings.index_cache_ttl
                return self._state
            self._refreshing = True
            warm = bool(self._state.cards)
        if warm:
            Thread(target=self._refresh, name="index-map-refresh", daemon=True).start()
        else:
            self._refresh()
        with self._lock:
            return self._state

    @staticmethod
    def keyword_scores(query: str, state: _MapState) -> list[float]:
        """IDF-weighted overlap between the question's grams and each card's grams."""
        q = IndexMap.grams(query)
        n = max(1, len(state.grams))
        return [
            sum(math.log(1 + n / state.df[g]) for g in q & card) if card else 0.0
            for card in state.grams
        ]

    def card_for(self, page_id: str) -> md.IndexCard | None:
        return next((c for c in self.snapshot().cards if c.target.strip("/") == page_id), None)

    def cards_for_document(self, document_key: str) -> list[md.IndexCard]:
        return list(self.snapshot().cards_by_document.get(document_key, []))

    def definers_for(self, entity: str) -> list[md.IndexCard]:
        return list(self.snapshot().entity_definers.get(_norm_entity(entity), []))

    def card_for_target(self, target: str) -> md.IndexCard | None:
        ref = (target or "").strip().strip("/")
        return next((c for c in self.snapshot().cards if c.target.strip("/") == ref), None)

    def rank(self, query: str, k: int) -> list[dict[str, Any]]:
        state = self.snapshot()
        cards, vectors = state.cards, state.vectors
        if not cards or k <= 0:
            return []
        # Keyword channel over the 00-目次 cards (the "ES over index pages only" you would
        # otherwise want): IDF-weighted bigram overlap, no network.
        kw = self.keyword_scores(query, state)
        kw_order = sorted(range(len(cards)), key=lambda i: kw[i], reverse=True)
        fused = {i: 1.0 / (60 + pos) for pos, i in enumerate(kw_order) if kw[i] > 0}
        if vectors and self.embedder is not None:
            try:
                q = self.embedder.embed_query(query)
                sims = [cosine(q, v) for v in vectors]
                for pos, i in enumerate(sorted(range(len(cards)), key=lambda i: sims[i], reverse=True)):
                    fused[i] = fused.get(i, 0.0) + 1.0 / (60 + pos)
            except Exception as exc:  # noqa: BLE001
                log.info("query embed failed: %s", exc)
        if fused:
            order = sorted(fused, key=lambda i: fused[i], reverse=True)[: self.settings.index_map_embed_k]
        else:  # nothing matched by keyword/embedding: let the reranker judge the first cards
            order = list(range(min(len(cards), self.settings.index_map_embed_k)))
        scores = {i: 1.0 / (1 + pos) for pos, i in enumerate(order)}
        if self.reranker is not None:
            try:
                ranked = normalize_scores(self.reranker.score(query, [self.card_text(cards[i]) for i in order]))
                scores = dict(zip(order, ranked))
                order.sort(key=lambda i: scores[i], reverse=True)
            except Exception as exc:  # noqa: BLE001
                log.info("index rerank failed: %s", exc)
        results: list[dict[str, Any]] = []
        for rank, i in enumerate(order[:k], start=1):
            page = self.card_page(cards[i])
            results.append({
                "node": page, "score": scores[i],
                "why": [{"field": "index_map", "rank": rank}],
                "evidence": [Evidence(page_id=page.id, field="index_map", text=self.card_text(cards[i]),
                                         source_rank=rank, score=scores[i]).model_dump()],
            })
        return results


class RouteDecision(BaseModel):
    mode: str = "deep"
    reason: str = ""


class JevIntent(BaseModel):
    """Machine-readable scope used by the document shortlist and coverage gate."""

    target: str = ""
    scope: str = ""
    broad: bool = False
    required_evidence: list[str] = Field(default_factory=list)
    must_include_terms: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)


@dataclass
class Subrun:
    start_id: str
    index: int
    visited: list[str] = field(default_factory=list)
    read_ids: set[str] = field(default_factory=set)
    read_sections: set[tuple[str, str]] = field(default_factory=set)
    empty_streak: int = 0
    offlimits: set[str] = field(default_factory=set)  # seeds another group owns: never read here
    last_read: str = ""
    cascade: bool = False
    cascade_question: str = ""
    cascade_page_ids: set[str] = field(default_factory=set)


# --- shared per-run budgets ------------------------------------------------


class RunBudget:
    """A run-scoped, thread-safe page-fetch and search-call cap shared by
    the lead and all subagents. When exhausted, search snippets stay usable
    but no new page bodies are fetched."""

    def __init__(self, max_pages: int, max_searches: int) -> None:
        self.max_pages = max_pages
        self.max_searches = max_searches
        self.pages_used = 0
        self.searches_used = 0
        self._lock = Lock()

    def try_page(self) -> bool:
        with self._lock:
            if self.pages_used >= self.max_pages:
                return False
            self.pages_used += 1
            return True

    def try_search(self) -> bool:
        with self._lock:
            if self.searches_used >= self.max_searches:
                return False
            self.searches_used += 1
            return True

    @property
    def pages_exhausted(self) -> bool:
        with self._lock:
            return self.pages_used >= self.max_pages


class StageTimer:
    """Thread-safe stage totals; concurrent durations add, while total_ms is wall time."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.stage_ms: Counter = Counter()
        self.counts: Counter = Counter()
        self._lock = Lock()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = (time.monotonic() - started) * 1000
            with self._lock:
                self.stage_ms[name] += elapsed

    def count(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.counts[name] += amount

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"stage_ms": dict(self.stage_ms), "counts": dict(self.counts),
                    "total_ms": (time.monotonic() - self.started) * 1000}


def _check_stop(stop_event: Event | None) -> None:
    if stop_event is not None and stop_event.is_set():
        raise AgentStopped("agent run cancelled")


@dataclass
class JevSweepBudget:
    """Independent, thread-safe budget for the exhaustive Jev sweep.

    Zero means unlimited (exhaustive traversal is the requested default).
    Deliberately separate from RunBudget so a sweep never starves the agents
    and vice versa; cache/memo hits do not consume a unit.
    """
    max_page_reads: int = 0
    max_list_calls: int = 0
    page_reads: int = 0
    list_calls: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def try_page(self) -> bool:
        with self._lock:
            if self.max_page_reads and self.page_reads >= self.max_page_reads:
                return False
            self.page_reads += 1
            return True

    def try_list(self) -> bool:
        with self._lock:
            if self.max_list_calls and self.list_calls >= self.max_list_calls:
                return False
            self.list_calls += 1
            return True


@dataclass
class _SweepWork:
    """Shared Jev sweep state.

    The sweep runs as a producer/consumer pipeline, so every field below is
    touched by several threads; the lock covers all of them (dedupe, counters,
    confirmed seeds, frontier, and the first failure that aborts the run).
    """
    stats: dict[str, int]
    max_probability: float = 0.0
    visited: set[str] = field(default_factory=set)
    results: list[dict[str, Any]] = field(default_factory=list)
    frontier: deque = field(default_factory=deque)
    errors: list[BaseException] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock, repr=False)


_JEV_YES_LOOKING = 0.5  # a p(はい) above this counts as a verdict leaning yes
_JEV_NARROW_DOC_LIMIT = 3  # narrow question + this many TOC yes docs or fewer: skip rewrite + card scoring
TOC_NOTE_GROUPS = 5        # LLM calls the 目次 rewrite stage makes, however many pages it ranks
TOC_NOTE_GROUP_CHARS = 12000  # 目次 text one of those calls summarises
REPORT_WINDOW = 4          # L1 batch size: subagent reports per compiler call (see _run_seed_groups)
JEV_TOC_RETRIEVAL_K = 20   # scoped document 目次 shortlist before page-card scoring
JEV_FAST_SEED_LIMIT = 9    # fewer than ten readable seeds: direct compiler path
JEV_ONE_STAGE_SEED_LIMIT = 32  # through 32 seeds: explorers, then final compiler


def _yes_means(units: dict[str, Any]) -> dict[str, float]:
    """Running average of only the yes-leaning p(はい), per stage.

    Zero when nothing qualifies, which is why the count travels with it: an average
    over zero yeses and an average of zero confidence look identical otherwise.
    """
    return {
        "mean_yes_probability": (round(units["sum_yes_full"] / units["yes_full"], 4)
                                 if units["yes_full"] else 0.0),
        "mean_yes_card_probability": (round(units["sum_yes_card"] / units["yes_card"], 4)
                                      if units["yes_card"] else 0.0),
    }


def _jev_est_tokens(text: str) -> int:
    # ponytail: UTF-8 byte/2 token estimate (under-fills the 25,600 ceiling for
    # Japanese, ~3 bytes/char at ~1 token/char). Upgrade to the runtime's real
    # tokenizer only if a hosted deployment proves this too conservative.
    return max(1, (len((text or "").encode("utf-8")) + 1) // 2)


def _jev_chunks(text: str, max_tokens: int, overlap_tokens: int, prefix: str = "",
                count: Callable[[str], int] = _jev_est_tokens) -> list[str]:
    """Split a body into <=max_tokens chunks (prefix included) with overlap.

    Prefers paragraph boundaries; hard-splits a single oversized paragraph.
    Never returns an empty chunk. Page confidence = max over chunks.
    """
    body_budget = max(1, max_tokens - 512 - count(prefix))
    text = text or ""
    if count(text) <= body_budget:
        return [text]
    units: list[str] = []
    for para in re.split(r"\n[ \t]*\n", text):
        while count(para) > body_budget:
            per_char = count(para) / max(1, len(para))
            cut = max(1, int(body_budget / per_char))
            units.append(para[:cut])
            para = para[cut:]
        if para.strip():
            units.append(para)
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in units:
        unit_tokens = count(unit)
        if current and current_tokens + unit_tokens > body_budget:
            chunks.append("\n\n".join(current))
            overlap: list[str] = []
            overlap_tokens_seen = 0
            for prev in reversed(current):
                prev_tokens = count(prev)
                if overlap_tokens_seen + prev_tokens > overlap_tokens:
                    break
                overlap.insert(0, prev)
                overlap_tokens_seen += prev_tokens
            current, current_tokens = overlap, overlap_tokens_seen
        current.append(unit)
        current_tokens += unit_tokens
    if current:
        chunks.append("\n\n".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


def _base_url(value: str) -> str:
    base = (value or "").rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base


# --- agent compilation ------------------------------------------------------


def _model(settings: Settings, max_tokens: int = 0) -> Any:
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.chat_model,
        base_url=_base_url(settings.chat_base_url),
        api_key=settings.chat_api_key or "local",
        temperature=settings.chat_temperature,
        timeout=httpx.Timeout(settings.llm_timeout, pool=None),
        http_client=llm_http_client(settings.llm_max_concurrency),
        max_retries=settings.llm_max_retries,
        stream_usage=True,
        max_tokens=max_tokens or settings.llm_max_output_tokens or None,
    )


def _llm_client(settings: Settings, temperature: float) -> LlmClient:
    return LlmClient(settings.chat_model, settings.chat_base_url, settings.chat_api_key,
                     temperature=temperature, timeout=settings.llm_timeout,
                     max_tokens=settings.llm_max_output_tokens, max_concurrency=settings.llm_max_concurrency)


def _compile_agent(settings: Settings, tools: list[StructuredTool], prompt: str, stop_event: Event | None,
                   max_tokens: int = 0):
    safe_prompt = sanitize_text(prompt or "")

    def before_model(state: dict[str, Any]) -> dict[str, Any]:
        _check_stop(stop_event)
        messages = state.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        return {"llm_input_messages": sanitize_messages(messages)}

    return create_react_agent(
        _model(settings, max_tokens), tools=tools, prompt=safe_prompt, pre_model_hook=before_model, version="v2"
    )


# langgraph's create_react_agent answers with this text when it runs out of steps
# (documented in langgraph.prebuilt.chat_agent_executor; no exported constant).
STEP_LIMIT_TEXT = "Sorry, need more steps to process this request."


def _last_text(state: Any) -> str:
    for message in reversed(state.get("messages", []) if isinstance(state, dict) else []):
        content = getattr(message, "content", "")
        if isinstance(content, str) and content.strip():
            return sanitize_text(content).strip()
        if isinstance(content, list):
            text = "\n".join(
                sanitize_text(p if isinstance(p, str) else str(p.get("text", "")))
                for p in content
                if isinstance(p, (str, dict))
            ).strip()
            if text:
                return text
    return ""


def _count_steps(state: Any) -> int:
    messages = state.get("messages", []) if isinstance(state, dict) else []
    return max(1, len(messages))


def _llm_output_capped(llm: Any, max_tokens: int) -> bool:
    reason = str(getattr(llm, "last_finish_reason", "") or "").lower()
    usage = getattr(llm, "last_usage", None) or {}
    return reason in {"length", "max_tokens"} or bool(
        max_tokens and (usage.get("output_tokens") or 0) >= max_tokens)


def _clean_ids(ids: list[Any] | None) -> list[str]:
    return [clean_ref(sanitize_text(str(nid))) for nid in (ids or []) if str(nid or "").strip()]


# --- formatting -------------------------------------------------------------


def format_search_results(results: list[dict[str, Any]]) -> str:
    if not results:
        return "no pages found"
    blocks: list[str] = []
    for result in results:
        page: WikiPage = result["node"]
        lines = [
            ev_text
            for ev in result.get("evidence", [])[:4]
            if (ev_text := sanitize_text(" ".join((ev.get("text") or "").split())))[:500]
        ]
        evidence = "\n".join(f"    - {t}" for t in lines) or "    - no evidence"
        blocks.append(
            f"- node_id: `{page.id}`\n"
            f"  title: {sanitize_text(page.title)}\n"
            f"  path: {sanitize_text(page.path)}\n"
            f"  summary: {sanitize_text(page.summary)}\n"
            f"  evidence:\n{evidence}\n"
            f"  next_action: if relevant, call explore(node_ids=['{page.id}'])"
        )
    return sanitize_text("\n".join(blocks))


def format_lead_candidate(result: dict[str, Any]) -> str:
    page: WikiPage = result["node"]
    why = ", ".join(f"{w['field']}#{w['rank']}" for w in result.get("why", [])[:4]) or "n/a"
    evidence_lines = [
        f"  - [{ev['field']}] {' '.join((ev.get('text') or '').split())[:500]}"
        for ev in result.get("evidence", [])[:5]
    ]
    evidence = "\n".join(evidence_lines) or "  - no evidence"
    return (
        f"- node_id: `{page.id}`\n"
        f"  title: {page.title}\n"
        f"  path: {page.path}\n"
        f"  summary: {page.summary}\n"
        f"  why_matched: {why}\n"
        f"  evidence:\n{evidence}\n"
        f"  next_action: call explore(node_ids=['{page.id}']), or include it with other candidates"
    )


def _seed_groups(confirmed: list[dict[str, Any]], group_size: int, max_groups: int) -> list[list[dict[str, Any]]]:
    """Jev seeds -> one group per (document, <=group_size) slice, strongest seed first.

    ``confirmed`` arrives confidence-sorted, so documents line up by their best
    seed and a document with more seeds just costs more subagents. Each group is
    one subagent's whole world: it sees its own seeds and never another's.
    ``max_groups`` remains an API/configuration compatibility argument; executor
    concurrency, rather than truncation, limits how many run simultaneously.
    """
    size = max(1, group_size)
    by_document: dict[str, list[dict[str, Any]]] = {}
    for result in confirmed:
        key = result.get("document") or result["node"].path.rsplit("/", 1)[0] or "?"
        by_document.setdefault(key, []).append(result)
    groups: list[list[dict[str, Any]]] = []
    for results in by_document.values():
        for start in range(0, len(results), size):
            groups.append(results[start:start + size])
    # ``max_groups`` is retained for configuration compatibility, but accepted
    # seeds must never disappear merely because many documents produced small
    # groups.  The executor limits concurrent calls; extra groups wait in its
    # queue.
    return sorted(groups, key=lambda group: -group[0]["score"])


def _seed_refs(results: list[dict[str, Any]]) -> set[str]:
    """Both address forms of each seed, so a read is blocked whichever one is used."""
    return {ref for result in results for ref in (result["node"].id, result["node"].path) if ref}


def _reports_text(reports: list[dict[str, Any]]) -> str:
    blocks = ["サブエージェント報告書（それぞれ異なる領域を調査）:"]
    for index, report in enumerate(reports, start=1):
        cited_str = ", ".join(report.get("cited", [])) or "(なし)"
        blocks.append(
            f"\n### サブエージェント {index} — 開始: {report.get('start')}\n"
            f"{report.get('answer', '').strip()}\n根拠ページID: {cited_str}"
        )
    blocks.append("\n※各報告に列挙された項目・関数名・数値のうち質問に関係するものは省略せず、回答にすべて含めてください（一部の抜粋は不可）。"
                  "質問とは別の対象についての記述を、質問の対象に当てはめないでください。")
    return sanitize_text("\n".join(blocks))


def format_read(page: WikiPage | None, text: str, requested: str, cleaned: str) -> str:
    if page is None:
        return sanitize_text(
            f"page not found\nrequested_id: {requested}\ncleaned_id: {cleaned}"
        )
    return sanitize_text(f"id: {page.id}\ntitle: {page.title}\npath: {page.path}\nbody:\n{text}")


def _describe(result: dict[str, Any]) -> dict[str, Any]:
    page: WikiPage = result["node"]
    return {
        "node_id": page.id,
        "kind": "source",
        "title": page.title,
        "summary": page.summary,
        "evidence": [" ".join((ev.get("text") or "").split())[:280] for ev in result.get("evidence", [])[:3]],
        "evidence_fields": [ev.get("field", "") for ev in result.get("evidence", [])[:3]],
    }


# --- research session -------------------------------------------------------


class ResearchSession:
    """Per-ask execution context sharing a GROWI client and a RunBudget."""

    def __init__(self, client: GrowiSearchClient, settings: Settings, cache: PageCache, reranker: Reranker | None, index_map: IndexMap | None = None, jev: Any = None, mirror: Mirror | None = None) -> None:
        self.client = client
        self.settings = settings
        self.cache = cache
        self.reranker = reranker
        self.index_map = index_map
        self.jev = jev
        self.mirror = mirror
        self.walker = Walker(index_map, mirror, jev, settings) if jev is not None and index_map is not None else None
        self.cascade_subagents_override: int | None = None
        self.has_overrides = False
        self._cascade_sections: dict[tuple[str, str, str], list[Any]] = {}
        self._cascade_claims: dict[str, int] = {}
        self._cascade_lock = Lock()
        self.llm = _llm_client(settings, settings.chat_temperature)
        self.deterministic_llm = self._make_deterministic_llm() if settings.jev_deterministic else None
        self.budget = RunBudget(settings.max_page_fetches_per_run, settings.max_search_calls_per_run)
        self.timer = StageTimer()
        # Request-local memoization for sharing across lead + subagents.
        self._page_memo: dict[str, WikiPage | None] = {}
        self._search_memo: dict[str, list[dict[str, Any]]] = {}
        self._links_memo: dict[str, list[WikiLink]] = {}
        self._usage_cb = UsageMetadataCallbackHandler()
        self._extra_usage: list[dict[str, Any]] = []
        self._lock = Lock()
        self._gram_lock = Lock()
        self._page_grams: OrderedDict[tuple[str, str], set[str]] = OrderedDict()
        self._query_grams: dict[str, set[str]] = {}
        self._seed_context = ""
        self._seed_pieces: tuple[str, str] = ("", "")
        self._seed_ids: list[str] = []
        self._seed_raw_reports: list[dict[str, Any]] = []
        self._jev_scope_query = ""
        self._jev_intent_structured: dict[str, Any] = {}
        self._jev_early_doc_keys: set[str] = set()
        self._jev_early_mode = False

    def apply_overrides(self, overrides: dict[str, Any] | None) -> None:
        clean = _sanitize_overrides(overrides or {}, self.settings)
        self.has_overrides = bool(clean)
        self.cascade_subagents_override = (
            clean["subagent_count"] if self.settings.jev_mode == "cascade" and "subagent_count" in clean else None
        )
        if self.cascade_subagents_override is not None:
            clean.pop("subagent_count")
        if not clean:
            return
        self.settings = self.settings.model_copy(update=clean)
        if {"chat_base_url", "chat_api_key", "chat_model", "chat_temperature"} & clean.keys():
            self.llm = _llm_client(self.settings, self.settings.chat_temperature)
        if "jev_deterministic" in clean or {"chat_base_url", "chat_api_key", "chat_model"} & clean.keys():
            self.deterministic_llm = self._make_deterministic_llm() if self.settings.jev_deterministic else None

    def _make_deterministic_llm(self) -> LlmClient:
        return _llm_client(self.settings, 0)

    # -- caches & budgets ----------------------------------------------------

    def _page_key(self, page_id: str = "", path: str = "", revision: str = "") -> str:
        return f"{page_id or path}|{revision}" if (page_id or path) else ""

    def _mirror_children(self, path: str) -> list[WikiPage] | None:
        if self.mirror is not None and self.mirror.ready and path:
            self.timer.count("mirror_list")
            return self.mirror.children_of(path)
        return None

    def _fetch_page(self, *, page_id: str | None = None, path: str | None = None, revision: str = "", sweep_budget: "JevSweepBudget | None" = None) -> WikiPage | None:
        """With a mirror, run budgets limit only live fallback reads."""
        key = self._page_key(page_id or "", path or "", revision)
        with self._lock:
            memo_hit = key in self._page_memo
            memo_page = self._page_memo.get(key)
        mirror_ready = self.mirror is not None and self.mirror.ready
        if memo_hit:
            self.timer.count("page_memo_hit")
            if memo_page is None:
                return None
            if not mirror_ready:
                return memo_page
            page = self.mirror.get_cached(page_id=memo_page.id, path=None if memo_page.id else memo_page.path)
            if page is not None:
                self.timer.count("mirror_hit")
                return page

        if mirror_ready:
            page = self.mirror.get_cached(page_id=page_id, path=path)
            if page is not None:
                self.timer.count("mirror_hit")
                with self._lock:
                    self._page_memo[key] = page.model_copy(update={"body": ""})
                return page
        if self.mirror is None:
            cached = self.cache.get(((page_id or path), revision))
            if cached is not None:
                self.timer.count("page_cache_hit")
                with self._lock:
                    self._page_memo[key] = cached
                return cached

        if sweep_budget is not None:
            # Jev sweep reads pay from their own budget and still warm both
            # caches, so later lead/subagent reads cost nothing extra.
            if not sweep_budget.try_page():
                return None
        elif not self.budget.try_page():
            return None

        try:
            self.timer.count("growi_get")
            page = (self.mirror.get(page_id=page_id, path=path) if mirror_ready
                    else self.client.get_page(page_id=page_id, path=path))
        except GrowiAPIError as exc:
            log.info("page fetch failed (%s): %s", page_id or path, exc)
            page = None
        if page is not None and self.mirror is None:
            self.cache.put(((page_id or path), revision), page)
        memo_page = page.model_copy(update={"body": ""}) if page is not None and mirror_ready else page
        with self._lock:
            self._page_memo[key] = memo_page
        return page

    def _fetch_ref(self, reference: str, sweep_budget: "JevSweepBudget | None" = None) -> WikiPage | None:
        ref = str(reference or "").strip()
        page_id = ref.removeprefix("/").removesuffix("/")
        if _is_page_id(ref):
            return self._fetch_page(page_id=page_id, sweep_budget=sweep_budget)
        return (
            self._fetch_page(path=ref, sweep_budget=sweep_budget)
            if ref.startswith("/")
            else self._fetch_page(page_id=ref, sweep_budget=sweep_budget)
        )

    def _memo_page(self, page_id: str) -> WikiPage | None:
        with self._lock:
            for key, page in self._page_memo.items():
                if page is not None and (page.id == page_id or key.startswith(f"{page_id}|")):
                    return page
        return None

    def _search(self, query: str, limit: int, emit: Callable | None = None) -> list[dict[str, Any]]:
        key = f"{query}|{limit}"
        if key in self._search_memo:
            return self._search_memo[key]
        if not self.budget.try_search():
            if emit:
                emit({"type": "budget_search_exhausted", "query": query})
            self._search_memo[key] = list(self._search_memo.get(f"{query}|any", []))
            return self._search_memo[key]
        try:
            self.timer.count("growi_search")
            hits = self.client.search_pages(query, path=self.settings.growi_root_path, limit=self.settings.search_candidates)
        except GrowiAPIError as exc:
            log.info("search failed: %s", exc)
            raise
        results = self._rank_candidates(query, hits, limit)
        self._search_memo[key] = results
        return results

    def _merge_map(self, query: str, results: list[dict[str, Any]], limit: int, emit: Callable | None = None) -> list[dict[str, Any]]:
        if self.index_map is None:
            return results
        mapped = self.index_map.rank(query, limit)
        if emit:
            state = self.index_map.snapshot()
            docs, cards = state.docs, state.cards
            # The research map: which documents/pages of the 00-目次 index were picked for this question.
            emit({
                "type": "map", "documents": len(docs), "pages": len(cards), "selected": len(mapped),
                "nodes": [
                    {"id": m["node"].id, "title": m["node"].title, "document": m["node"].document,
                     "chapter": m["node"].cluster, "score": round(float(m["score"]), 3)}
                    for m in mapped
                ],
            })
        by_id = {r["node"].id: r for r in results}
        for item in mapped:
            hit = by_id.get(item["node"].id)
            if hit is None:
                results.append(item)
                by_id[item["node"].id] = item
            else:
                hit["node"].summary = hit["node"].summary or item["node"].summary
                hit["why"].append(item["why"][0])
                hit["evidence"].extend(item["evidence"])
        return results

    # -- ranking -------------------------------------------------------------

    def _rank_candidates(self, query: str, hits: list, limit: int) -> list[dict[str, Any]]:
        hits = [h for h in hits if h.page.path.rstrip("/").rsplit("/", 1)[-1] != self.settings.index_page_name]
        if not hits:
            return []
        texts = [f"{h.page.title}\n{h.page.path}\n{h.snippet}" for h in hits]
        scores: list[float]
        if self.reranker is not None:
            try:
                ranked = self.reranker.score(query, texts)
                scores = normalize_scores(ranked)
            except Exception as exc:  # noqa: BLE001 - fall back to ES order
                log.info("candidate rerank failed: %s", exc)
                scores = []
        else:
            scores = []

        results: list[dict[str, Any]] = []
        for index, hit in enumerate(hits):
            page = hit.page.model_copy()
            page.summary = hit.snippet[:500]
            page.snippet = hit.snippet
            evidence = [
                Evidence(
                    page_id=page.id,
                    field="growi_es",
                    text=hit.snippet,
                    source_rank=hit.rank,
                ).model_dump()
            ]
            score = scores[index] if scores else 1.0 / (1 + index)
            results.append(
                {"node": page, "score": score, "why": [{"field": "growi_es", "rank": hit.rank}], "evidence": evidence}
            )

        if scores:
            paired = sorted(zip(results, scores), key=lambda pair: pair[1], reverse=True)
            results = [r for r, _ in paired]
            for r, _s in zip(results, [p[1] for p in paired]):
                r["score"] = _s
        return results[:limit]

    # -- fast API surface (app + tools) -------------------------------------

    def fast_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Top-bar search: exactly one ES call, snippet candidates, no page bodies."""
        query = sanitize_text(query or "").strip()[:2000]
        if not query:
            return []
        query = " ".join(query.split())
        self.timer.count("growi_search")
        hits = self.client.search_pages(query, path=self.settings.growi_root_path, limit=self.settings.search_candidates)
        return self._merge_map(query, self._rank_candidates(query, hits, limit), limit)

    def search(self, query: str, limit: int | None = None) -> list[WikiPage]:
        limit = limit or self.settings.rerank_top_k
        return [r["node"] for r in self.search_with_evidence(query, limit)]

    def search_with_evidence(self, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        limit = limit or self.settings.rerank_top_k
        return self._search(query, limit)[:limit]

    def hydrate_shallow(self, results: list[dict[str, Any]], query: str, emit: Callable) -> list[dict[str, Any]]:
        """Fetch at most settings.shallow_page_reads bodies, then rerank their sections."""
        picks = results[: self.settings.shallow_page_reads]
        pool: list[tuple[float, WikiPage, dict[str, Any]]] = []
        for page_rank, result in enumerate(picks):
            page = self._fetch_page(page_id=result["node"].id)
            snippet_evidence = result["evidence"]
            if page is None:
                # Keep the already-present snippet; continue to next page.
                pool.append((1.0 / (60 + page_rank), result["node"], {"evidence": snippet_evidence, "why": result["why"], "section_score": 0.0}))
                continue
            sections = md.split_sections(page.body)
            for section in sections:
                if not section.body.strip():
                    continue
                text = f"{page.title} > {section.breadcrumb or section.heading}\n{section.body[:2000]}"
                pool_item: dict[str, Any] = {"page": page, "text": text, "section": section, "source": "section"}
                pool.append((0.0, page, {"section_item": pool_item, "base": result}))
        # Flatten candidate sections into one rerank pool (cap 30, higher page ranks preferred).
        section_items = [entry for entry in pool if entry[2].get("section_item")]
        snippet_entries = [entry for entry in pool if not entry[2].get("section_item")]
        capped = section_items[:30]
        scores: dict[int, float] = {}
        if capped and self.reranker is not None:
            try:
                ranked = self.reranker.score(query, [entry[2]["section_item"]["text"] for entry in capped])
                ranked = normalize_scores(ranked)
                scores = {index: score for index, score in enumerate(ranked)}
            except Exception as exc:  # noqa: BLE001 - keep ES order
                log.info("section rerank failed: %s", exc)

        by_page: dict[str, dict[str, Any]] = {}
        for index, entry in enumerate(capped):
            item = entry[2]["section_item"]
            page: WikiPage = item["page"]
            section: md.MarkdownSection = item["section"]
            result = entry[2]["base"]
            # Aggregate scores: source is 1/(60+ES rank); section score is normalized best.
            source_score = 1.0 / (60 + (result.get("why", [{}])[0].get("rank", index + 1) if result.get("why") else index + 1))
            candidate_score = float(result.get("score", 0.0))
            page_pos = scores.get(index, 0.0)
            record = by_page.setdefault(
                page.id,
                {"node": page, "sections": [], "source": source_score, "candidate": candidate_score, "best": 0.0},
            )
            record["sections"].append((page_pos, section, page))
            record["best"] = max(record["best"], page_pos)

        results: list[dict[str, Any]] = []
        for page_id, record in by_page.items():
            sections = sorted(record["sections"], key=lambda item: item[0], reverse=True)[: self.settings.evidence_per_page]
            evidence = [
                Evidence(
                    page_id=page_id,
                    field="section",
                    text=md.section_summary(section.body) if isinstance(section, str) else " ".join(section.body.split()),
                    heading=section.breadcrumb or section.heading,
                    start_line=section.start_line,
                    end_line=section.end_line,
                    source_rank=index + 1,
                    score=score,
                ).model_dump()
                for index, (score, section, _page) in enumerate(sections)
            ]
            results.append(
                {
                    "node": record["node"],
                    "score": record["source"] + record["candidate"] + record["best"],
                    "why": [{"field": "growi_es", "rank": 1}, {"field": "section", "rank": 1}],
                    "evidence": evidence,
                }
            )

        snippet_results = [
            {
                "node": entry[1],
                "score": entry[0],
                "why": entry[2]["why"],
                "evidence": entry[2]["evidence"],
            }
            for entry in snippet_entries
        ]
        combined = results + snippet_results
        combined.sort(key=lambda item: item["score"], reverse=True)
        return combined

    # -- read / follow -------------------------------------------------------

    def read_node(
        self, page_id: str, heading: str | None = None, emit: Callable | None = None, agent: int | None = None
    ) -> str:
        page = self._fetch_ref(page_id)
        if page is None:
            return format_read(None, "", page_id, page_id)
        if emit:
            emit({"type": "read", "agent": agent, "node": node_ref(page)})
        return self._render_body(page, heading)

    def _reserve_cascade_pages(self, page_ids: Iterable[str]) -> None:
        with self._cascade_lock:
            for reference in page_ids:
                reference = clean_ref(str(reference))
                if not reference:
                    continue
                key = reference if re.fullmatch(r"[0-9a-fA-F]{24}", reference) else reference.strip("/")
                self._cascade_claims.setdefault(key, -1)
                if self.mirror is not None and key == reference:
                    path = self.mirror.path_of(key)
                    if path:
                        self._cascade_claims.setdefault(path.strip("/"), -1)

    def _claim_cascade_pages(self, index: int, prompt: str) -> set[str]:
        page_ids = set(re.findall(r"\(page_id:\s*([0-9a-fA-F]{24})", prompt))
        paths = {path.strip("/") for path in re.findall(r"\bpath:\s*([^,\n)]+)", prompt) if path.strip("/")}
        with self._cascade_lock:
            for page_id in page_ids:
                if self._cascade_claims.get(page_id) in (None, -1):
                    self._cascade_claims[page_id] = index
                if self.mirror is not None:
                    path = self.mirror.path_of(page_id)
                    if path:
                        path = path.strip("/")
                        if self._cascade_claims.get(path) in (None, -1):
                            self._cascade_claims[path] = index
            for path in paths:
                if self._cascade_claims.get(path) in (None, -1):
                    self._cascade_claims[path] = index
            return {page_id for page_id in page_ids if self._cascade_claims[page_id] == index}

    def _cascade_owner(self, ref: str) -> int | None:
        key = clean_ref(ref).strip("/")
        with self._cascade_lock:
            owner = self._cascade_claims.get(key)
        if owner is not None:
            return owner
        page_id = ((self.mirror.by_path.get("/" + key) or self.mirror.by_path.get(key))
                   if self.mirror is not None else None)
        if page_id is None and self.index_map is not None:
            card = self.index_map.card_for(ref)
            page_id = getattr(card, "page_id", "") or getattr(card, "id", "") if card else None
        if page_id:
            with self._cascade_lock:
                return self._cascade_claims.get(page_id)
        return None

    def _run_packed_subagent(self, index: int, prompt: str, question: str,
                             emit: Callable, stop_event: Event | None) -> dict[str, Any]:
        owned = self._claim_cascade_pages(index, prompt)
        run = Subrun(start_id=next(iter(owned), ""), index=index, cascade=True,
                     cascade_question=question, cascade_page_ids=owned)
        return _run_subagent(self, run, question, prompt, emit, stop_event)

    def page_for_view(self, page_id: str, emit: Callable | None = None) -> WikiPage | None:
        page = self._fetch_ref(page_id)
        if page:
            page.document = page.path
            page.summary = md.section_summary(page.body) or page.title
            page.source_url = self._source_url(page)
        return page

    def children(self, *, page_id: str | None = None, path: str | None = None) -> list[WikiPage]:
        if self.mirror is not None and self.mirror.ready:
            path = path or (self.mirror.path_of(page_id) if page_id else "")
            mirrored = self._mirror_children(path)
            if mirrored is not None:
                return mirrored
        self.timer.count("growi_list")
        return self.client.list_children(page_id=page_id, path=path)

    def links_for(self, page: WikiPage) -> list[WikiLink]:
        links: list[WikiLink] = []
        seen: set[tuple[str, str]] = set()
        for parsed in md.extract_links(page.body):
            target = md.resolve_target(page.path, f"{parsed.raw_target}{'#' + parsed.fragment if parsed.fragment else ''}", self.settings.growi_root_path)
            if target is None:
                continue
            resolved_id = target.page_id
            resolved_path = target.path
            if resolved_id is None and resolved_path:
                cached = self._page_memo.get(self._page_key("", resolved_path, "")) or next(
                    (p for k, p in self._page_memo.items() if f"{resolved_path}|" in k), None
                )
                if cached:
                    resolved_id = cached.id
            if resolved_id is None and resolved_path is None:
                continue
            title = parsed.anchor or (resolved_path or "").rsplit("/", 1)[-1]
            summary = parsed.summary or (f"{parsed.anchor}（{parsed.heading}）" if parsed.heading else parsed.anchor)
            target_id = resolved_id or resolved_path or ""
            key = (target_id, parsed.fragment)
            if key in seen:
                continue
            seen.add(key)
            links.append(
                WikiLink(
                    id=make_link_id(page.id, target_id, parsed.anchor, parsed.fragment),
                    source_node_id=page.id,
                    target_node_id=target_id,
                    label=title if title else parsed.anchor,
                    summary=summary,
                    source_heading=parsed.heading,
                    fragment=parsed.fragment,
                    target_path=target.path or "",
                    kind=parsed.kind,
                )
            )
            if len(links) >= self.settings.link_expand_limit:
                break
        return links

    def follow_link(self, page_id: str, direction: str = "outgoing", limit: int | None = None, emit: Callable | None = None, agent: int | None = None) -> list[WikiLink]:
        if (direction or "outgoing").lower().strip() != "outgoing":
            raise ValueError("direction must be 'outgoing' in this version")
        key = self._page_key(page_id)
        with self._lock:
            memo = self._links_memo.get(key)
        anchor: WikiPage | None = None
        if memo is not None:
            links = memo
        else:
            anchor = self._fetch_ref(page_id)
            links = self.links_for(anchor) if anchor is not None else []
            links = [l for l in links if l.kind != "nav"]
            with self._lock:
                self._links_memo[key] = links
        if anchor is None:
            anchor = self._memo_page(page_id) or WikiPage(id=page_id, title=page_id)
        if emit:
            emit({"type": "follow_link", "agent": agent, "node": node_ref(anchor), "neighbors": len(links)})
        return links[:limit] if limit else links

    def _render_body(self, page: WikiPage, heading: str | None) -> str:
        sections = md.split_sections(page.body)
        if heading:
            subtree: list[str] = []
            capturing = False
            base_level = 0
            for raw_index, raw_line in enumerate(page.body.split("\n")):
                match = re.match(r"^(#{1,6})[ \t]+(.*?)[ \t]*$", raw_line)
                if match:
                    level = len(match.group(1))
                    if not capturing and heading.lower() in match.group(2).lower():
                        capturing = True
                        base_level = level
                        subtree.append(raw_line)
                        continue
                    if capturing and level <= base_level:
                        break
                if capturing:
                    subtree.append(raw_line)
            if subtree:
                return format_read(page, "\n".join(subtree)[: MAX_TEXT * 2], page.id, page.id)
            return format_read(page, "[節が見つかりません]", page.id, page.id)

        full = page.body
        if len(full) <= MAX_TEXT:
            return format_read(page, full, page.id, page.id)

        outline = "\n".join(f"- L{s.level} {s.heading}" for s in sections)
        preview = full[:MAX_TEXT]
        return format_read(
            page,
            f"(長いページです。全{len(full)}文字/先頭{MAX_TEXT}文字のみ表示)\n[アウトライン]\n{outline}\n[先頭抜粋]\n{preview}\n\n(続きは read(node_id, heading=...) で特定節を指定して読んでください。)",
            page.id,
            page.id,
        )

    def _source_url(self, page: WikiPage) -> str:
        encoded = "/".join(quote(seg) for seg in page.path.strip("/").split("/"))
        return f"{self.client.url}/{encoded}" if page.path else ""

    # -- Jev relevance sweep ---------------------------------------------------

    def _jev_gate(self, emit: Callable, stage: str, status: str, node: Any, probability: float,
                  document: str, chunk_count: int = 1) -> None:
        ref = {"id": node.id, "title": node.title or node.id} if isinstance(node, WikiPage) else dict(node)
        emit({
            "type": "jev_gate", "stage": stage, "status": status, "node": ref,
            "document": document, "probability": round(float(probability), 4),
            "threshold": self.settings.jev_seed_threshold if stage == "full" else self.settings.jev_threshold,
            "chunk_count": chunk_count,
        })

    @staticmethod
    def _jev_state(document: str, page: WikiPage | None = None, text: str = "",
                   entity_defs: list[dict] | None = None, cards: list[md.IndexCard] | None = None) -> dict:
        state: dict[str, Any] = {
            "document": document,
            "page": None if page is None else {
                "id": page.id, "title": page.title, "path": page.path, "text": text},
            "entity_definitions": list(entity_defs or []),
        }
        if cards:
            state["cards"] = [
                {"title": c.title, "path": c.target, "summary": c.summary,
                 "chapter": c.chapter, "keywords": c.keywords, "entities": c.entities}
                for c in cards
            ]
        return state

    def _page_entities(self, state: "_MapState", page: WikiPage) -> list[str]:
        """Known entity names occurring in a no-index page (longest first)."""
        hay = _norm_entity(f"{page.title}\n{page.body}"[:40000])
        found = {(name, rank) for normalized, name, rank in state.short_entities if normalized in hay}
        for index in range(len(hay)):
            for normalized, name, rank in state.entity_index.get(hay[index:index + 2], ()):
                if hay.startswith(normalized, index):
                    found.add((name, rank))
        return [name for name, _rank in sorted(found, key=lambda item: (-len(item[0]), item[1]))]

    def _jev_walk(self, root: WikiPage, budget: JevSweepBudget,
                  stop_event: Event | None,
                  skip_index_pages: bool = True,
                  live_list: bool = False) -> Iterator[WikiPage]:
        """Sweep-only recursive child walk; cycles stop on visited IDs/paths.

        Yields while it crawls so classification starts before enumeration ends.
        `skip_index_pages` keeps 00-目次 pages out of classification — the rewrite
        crawl turns it off because those pages are exactly what it is looking for.
        """
        seen: set[str] = set()
        queue: deque[tuple[WikiPage, bool]] = deque([(root, False)])
        while queue:
            _check_stop(stop_event)
            node, from_listing = queue.popleft()
            key = (node.id or node.path).strip("/")
            if not key or key in seen:
                continue
            seen.add(key)
            yield node
            if from_listing and node.descendant_count == 0:
                continue
            try:
                children = None if live_list else self._mirror_children(node.path)
                if children is None:
                    if not budget.try_list():
                        continue
                    self.timer.count("growi_list")
                    children = self.client.list_children(
                        page_id=node.id if not node.path else None, path=node.path or None
                    )
            except GrowiAPIError as exc:
                log.info("jev walk failed (%s): %s", node.path or node.id, exc)
                continue
            for child in children:
                if skip_index_pages and \
                        child.path.rstrip("/").rsplit("/", 1)[-1] == self.settings.index_page_name:
                    continue
                queue.append((child, True))

    def _jev_documents(self, state: "_MapState", budget: JevSweepBudget,
                       stop_event: Event | None) -> list[dict]:
        """Documents reachable through the root index, with readable page cards."""
        docs: list[dict] = []
        for doc in state.docs:
            _check_stop(stop_event)
            page = self._fetch_ref(doc.target, sweep_budget=budget)
            cards = state.cards_by_document.get(doc.doc_ref, [])
            root_path = page.path.rsplit("/", 1)[0] if page is not None and page.path else ""
            if page is not None and md.is_index_page(page.body) and cards and root_path:
                docs.append({"key": root_path, "indexed": True, "title": doc.title,
                             "cards": cards, "root": page})
        return docs

    def _jev_probs(self, items: list[tuple[dict, JevQuestion]], *,
                   return_exceptions: bool = False) -> list:
        """Score states together when the engine supports batching; retain fake compatibility."""
        if not items:
            return []
        if callable(getattr(self.jev, "decide_batch", None)):
            requests = [JevRequest(state, EngineJevQuestion(text=question.text, key=question.key))
                        for state, question in items]
            self.timer.count("jev_calls")
            self.timer.count("jev_questions", len(requests))
            try:
                if return_exceptions:
                    results = self.jev.decide_batch(requests, return_exceptions=True)
                else:
                    results = self.jev.decide_batch(requests)
            except Exception as exc:  # the sweep falls back to ES on RuntimeError, like score_many
                raise RuntimeError(f"Jev scoring failed: {exc}") from exc
            if return_exceptions:
                return [float(result.p_yes) if not isinstance(result, BaseException) else result
                        for result in results]
            return [float(result.p_yes) for result in results]
        probabilities = []
        for state, question in items:
            self.timer.count("jev_calls")
            self.timer.count("jev_questions")
            try:
                result = self.jev.score_many(state, [question])
            except Exception as exc:
                if not return_exceptions:
                    raise
                probabilities.append(exc)
                continue
            if len(result) != 1:
                raise RuntimeError("jev adapter returned misaligned probabilities")
            probabilities.append(float(result[0]))
        return probabilities

    def _jev_score_cards(self, query: str, doc: dict, emit: Callable,
                         stop_event: Event | None, work: "_SweepWork") -> list[tuple[md.IndexCard, float]]:
        """Score one lightweight state per card, batching the document once."""
        st = self.settings
        candidates: list[tuple[md.IndexCard, float]] = []
        cards = doc["cards"]
        items = []
        for card in cards:
            state = self._jev_state(doc["key"], cards=[card])
            question = JevQuestion(
                key=card.target,
                text=("これは第2段階の厳密なページ候補判定です。この文書の00-目次にある1件の項目"
                      "（タイトル・概要・キーワード）だけを見て、リンク先ページ本文が検索意図に必要な"
                      "証拠そのものを含む可能性がある場合だけ「はい」と答えてください。"
                      "単に同じ製品・分野、対象名の一度だけの言及、別対象の説明、リンク集や概要だけなら「いいえ」です。\n"
                      f"項目: {card.title} ({card.target})\n質問: {query}\n"
                      "選択肢: はい / いいえ"),
            )
            items.append((state, question))
        considered = 0
        found = 0
        _check_stop(stop_event)
        with self.timer.stage("jev_cards"):
            probs = self._jev_probs(items)
        if len(probs) != len(cards):
            raise RuntimeError("jev adapter returned misaligned probabilities")
        for card, prob in zip(cards, probs):
            considered += 1
            node = IndexMap.card_page(card)
            if prob > st.jev_threshold:
                found += 1
                self._jev_gate(emit, "card", "candidate", node, prob, doc["key"])
                candidates.append((card, prob))
            else:
                self._jev_gate(emit, "card", "pruned", node, prob, doc["key"])
        with work.lock:
            work.stats["cards_considered"] += considered
            work.stats["candidates"] += found
            work.max_probability = max(work.max_probability, *probs) if probs else work.max_probability
        return candidates

    def _jev_score_body(self, query: str, document: str, page: WikiPage,
                        entity_defs: list[dict], stop_event: Event | None) -> tuple[float, int]:
        """Full-body verdict over token chunks; page confidence = max chunk p."""
        st = self.settings
        body = sanitize_text(page.body)
        count_tokens = getattr(self.jev, "count_tokens", _jev_est_tokens)
        chunks = _jev_chunks(body, st.jev_chunk_tokens, st.jev_chunk_overlap,
                             prefix=f"{page.title}\n{page.path}", count=count_tokens)
        best = 0.0
        verdict_cache = bool(getattr(st, "jev_verdict_cache", False) and
                             self.mirror is not None and self.mirror.ready)
        cached = self.mirror.get_verdicts(query) if verdict_cache else {}
        writes: dict[str, float] = {}
        for index, chunk in enumerate(chunks):
            _check_stop(stop_event)
            cache_key = f"{page.id}:{page.revision_id}:{index}"
            if cache_key in cached:
                best = max(best, cached[cache_key])
                continue
            question = JevQuestion(
                key=f"{page.id or page.path}:{index}", text=jev_question_text(query)
            )
            kept_defs: list[dict] = []
            for definition in entity_defs:
                trial = self._jev_state(
                    document, page=page, text=chunk, entity_defs=[*kept_defs, definition]
                )
                size = _jev_est_tokens(
                    json.dumps(trial, ensure_ascii=False, default=str) + question.text
                )
                if size > st.jev_chunk_tokens:
                    break
                kept_defs.append(definition)
            state = self._jev_state(document, page=page, text=chunk, entity_defs=kept_defs)
            self.timer.count("jev_calls")
            self.timer.count("jev_questions")
            with self.timer.stage("jev_bodies"):
                probs = self.jev.score_many(state, [question])
            if len(probs) != 1:
                raise RuntimeError("jev adapter returned misaligned probabilities")
            best = max(best, probs[0])
            if verdict_cache:
                writes[cache_key] = float(probs[0])
        if writes:
            self.mirror.set_verdicts(query, writes)
        return best, len(chunks)

    @staticmethod
    def _is_toc_page(title: str) -> bool:
        """Pages that describe what a document contains: the rewriter's best clue."""
        return any(marker in (title or "") for marker in ("目次", "一覧", "index", "Index", "INDEX"))

    def _jev_project_roots(self, budget: JevSweepBudget,
                           stop_event: Event | None) -> list[WikiPage]:
        """Configured project, or first-level projects with their own root 00-目次."""
        configured = self.settings.growi_root_path.rstrip("/")
        index_name = self.settings.index_page_name
        if configured:
            index = self._fetch_ref(f"{configured}/{index_name}", sweep_budget=budget)
            return ([WikiPage(id="", path=configured, title=configured.rsplit("/", 1)[-1])]
                    if index is not None and md.is_index_page(index.body) else [])
        if not budget.try_list():
            return []
        _check_stop(stop_event)
        try:
            self.timer.count("growi_list")
            children = self.client.list_children(path="/")
        except GrowiAPIError as exc:
            log.info("jev project root listing failed: %s", exc)
            return []
        roots = []
        for child in children:
            _check_stop(stop_event)
            if not child.path:
                continue
            index = self._fetch_ref(
                f"{child.path.rstrip('/')}/{index_name}", sweep_budget=budget)
            if index is not None and md.is_index_page(index.body):
                roots.append(child)
        return roots

    def _jev_toc_blocks(self, state: "_MapState", budget: JevSweepBudget,
                        stop_event: Event | None,
                        roots: list[WikiPage] | None = None) -> list[tuple[str, str]]:
        """Every document 00-目次 below each project root, including nested ones."""
        blocks: list[tuple[str, str]] = []
        seen: set[str] = set()
        if roots is None:
            roots = self._jev_project_roots(budget, stop_event)
        root_indexes = {
            f"{root.path.rstrip('/')}/{self.settings.index_page_name}" for root in roots if root.path
        }
        for root in roots:
            if not root.path:
                continue
            for page in self._jev_walk(root, budget, stop_event,
                                       skip_index_pages=False, live_list=True):
                _check_stop(stop_event)
                path = page.path.rstrip("/")
                if (not path or path in root_indexes or
                        path.rsplit("/", 1)[-1] != self.settings.index_page_name):
                    continue
                reference = page.id or page.path
                key = reference.strip("/")
                if not key or key in seen:
                    continue
                body = page.body or ""
                if not body.strip():
                    found = self._fetch_ref(reference, sweep_budget=budget)
                    body = found.body if found is not None else ""
                if (not md.is_index_page(body) or
                        md.index_kind(body) in {"root", "folder"}):
                    continue
                seen.add(key)
                blocks.append((path.rsplit("/", 1)[0] or page.title,
                               sanitize_text(body)))
        return blocks

    def _jev_retrieve_tocs(self, query: str, blocks: list[tuple[str, str]],
                           limit: int = JEV_TOC_RETRIEVAL_K) -> list[tuple[str, str]]:
        """BM25 + vector + configured reranker shortlist over document 目次 pages."""
        if len(blocks) <= limit:
            return blocks
        texts = [f"{document}\n{text}" for document, text in blocks]

        def terms(text: str) -> list[str]:
            normalized = unicodedata.normalize("NFKC", text or "").lower()
            out = re.findall(r"[a-z0-9]{2,}", normalized)
            runs = re.sub(r"[a-z0-9\s\W]+", " ", normalized)
            for chunk in runs.split():
                out.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
                if len(chunk) == 1:
                    out.append(chunk)
            return out

        tokenized = [terms(text) for text in texts]
        document_frequency: Counter = Counter()
        for tokens in tokenized:
            document_frequency.update(set(tokens))
        query_terms = terms(query)
        query_counts = Counter(query_terms)
        document_count = max(1, len(tokenized))
        average_length = sum(len(tokens) for tokens in tokenized) / document_count or 1.0
        bm25: list[float] = []
        for tokens in tokenized:
            counts = Counter(tokens)
            length = len(tokens)
            score = 0.0
            for term, query_frequency in query_counts.items():
                frequency = counts.get(term, 0)
                df = document_frequency.get(term, 0)
                if not frequency or not df:
                    continue
                idf = math.log(1 + (document_count - df + 0.5) / (df + 0.5))
                denominator = frequency + 1.2 * (0.25 + 0.75 * length / average_length)
                score += idf * ((frequency * 2.2) / denominator) * query_frequency
            bm25.append(score)

        fused: dict[int, float] = {}

        def add_ranked(order: list[int], include: Callable[[int], bool] | None = None) -> None:
            for rank, index in enumerate(order):
                if include is not None and not include(index):
                    continue
                fused[index] = fused.get(index, 0.0) + 1.0 / (60 + rank)

        add_ranked(sorted(range(len(blocks)), key=lambda i: bm25[i], reverse=True),
                   lambda index: bm25[index] > 0)
        embedder = getattr(self.index_map, "embedder", None) if self.index_map is not None else None
        if embedder is not None:
            try:
                query_vector = embedder.embed_query(query)
                vectors = embedder.embed_documents(texts)
                similarities = [cosine(query_vector, vector) for vector in vectors]
                add_ranked(sorted(range(len(blocks)), key=lambda i: similarities[i], reverse=True))
            except Exception as exc:  # noqa: BLE001 - keyword ranking is still useful
                log.info("document 目次 vector retrieval failed: %s", exc)
        if self.reranker is not None:
            try:
                reranked = normalize_scores(self.reranker.score(query, texts))
                add_ranked(sorted(range(len(blocks)), key=lambda i: reranked[i], reverse=True))
            except Exception as exc:  # noqa: BLE001 - retrieval remains usable without reranking
                log.info("document 目次 rerank failed: %s", exc)

        # Without any retrieval signal, do not treat the first twenty documents
        # as relevant; the caller will fall back to all reachable documents.
        if not fused:
            return []
        order = sorted(fused, key=fused.get, reverse=True)
        return [blocks[index] for index in order[:limit]]

    def _jev_toc_probs(self, query: str, blocks: list[tuple[str, str]],
                       stop_event: Event | None, emit: Callable, *,
                       batch: bool = False, batch_size: int = 20) -> list[float]:
        """Rank the 目次 pages one page at a time, in <=jev_toc_chunk_tokens chunks.

        Jev refuses an over-budget input and never truncates, so the inventory must not
        ride in one batch: a single oversized 目次 fails every page scored with it. Each
        page is chunked and scored on its own, a page is the best of its chunks, and a
        chunk that still fails costs only that chunk. Progress streams as it goes: this
        is the slowest stage of a question and the one that decides every answer after it.

        With batch=True the chunks of every page ride shared engine calls of at most
        batch_size items instead of one call per page; verdicts, failure isolation
        and per-page progress are unchanged.
        """
        st = self.settings
        count = getattr(self.jev, "count_tokens", _jev_est_tokens)
        overlap = min(st.jev_chunk_overlap, st.jev_toc_chunk_tokens // 2)
        question = jev_toc_question(query)
        emit({"type": "jev_toc_scan", "documents": len(blocks)})
        if batch:
            return self._jev_toc_probs_batched(query, blocks, stop_event, emit, question,
                                               count, overlap, batch_size)
        probabilities: list[float] = []
        yeses, sum_yes = 0, 0.0
        # The bar ticks once per page, not per chunk: a page is the unit the user reads.
        for done, (document, text) in enumerate(blocks, start=1):
            _check_stop(stop_event)
            best = 0.0
            chunks = _jev_chunks(sanitize_text(text), st.jev_toc_chunk_tokens, overlap,
                                 prefix=document, count=count)
            for part, chunk in enumerate(chunks):
                try:
                    probs = self._jev_probs([({"document": document, "toc": chunk},
                                              JevQuestion(key=f"toc:{document}:{part}", text=question))])
                except Exception as exc:  # noqa: BLE001 - one unreadable page must not stop the rest
                    print(f"[growi-search] Jev 目次 score failed ({document} #{part + 1}): {exc}", flush=True)
                    emit({"type": "jev_toc_chunk", "document": document, "part": part + 1,
                          "chunks": len(chunks), "reason": str(exc)[:200]})
                    continue
                if probs:
                    best = max(best, max(probs))
            if best > _JEV_YES_LOOKING:
                yeses += 1
                sum_yes += best
            probabilities.append(best)
            emit({"type": "jev_progress", "stage": "toc", "done": done, "total": len(blocks),
                  "percent": round(100.0 * done / len(blocks), 1), "document": document,
                  "probability": round(best, 4), "yes": yeses,
                  "mean_yes_probability": round(sum_yes / yeses, 4) if yeses else 0.0})
        emit({"type": "jev_progress", "stage": "toc", "done": len(blocks), "total": len(blocks),
              "percent": 100.0, "yes": yeses,
              "mean_yes_probability": round(sum_yes / yeses, 4) if yeses else 0.0})
        return probabilities

    def _jev_toc_probs_batched(self, query: str, blocks: list[tuple[str, str]],
                               stop_event: Event | None, emit: Callable, question: str,
                               count: Callable, overlap: int, batch_size: int) -> list[float]:
        """Shared engine calls of at most batch_size chunks for the whole 目次 scan."""
        size = max(1, int(batch_size or 0))
        per_doc: list[list[tuple[dict, JevQuestion]]] = []
        for document, text in blocks:
            chunks = _jev_chunks(sanitize_text(text), self.settings.jev_toc_chunk_tokens, overlap,
                                 prefix=document, count=count)
            per_doc.append([({"document": document, "toc": chunk},
                             JevQuestion(key=f"toc:{document}:{part}", text=question))
                            for part, chunk in enumerate(chunks)])
        flat = [item for items in per_doc for item in items]
        batches = (len(flat) + size - 1) // size if flat else 0
        emit({"type": "jev_progress", "stage": "toc", "done": 0, "total": len(blocks),
              "percent": 0.0, "batches": batches, "yes": 0, "mean_yes_probability": 0.0})
        scored: list = []
        for start in range(0, len(flat), size):
            _check_stop(stop_event)
            scored.extend(self._jev_probs(flat[start:start + size], return_exceptions=True))
        probabilities: list[float] = []
        yeses, sum_yes = 0, 0.0
        # The bar still ticks once per page: a page is the unit the user reads.
        offset = 0
        for done, ((document, _text), items) in enumerate(zip(blocks, per_doc), start=1):
            _check_stop(stop_event)
            best = 0.0
            for part in range(len(items)):
                result = scored[offset + part]
                if isinstance(result, BaseException):
                    print(f"[growi-search] Jev 目次 score failed ({document} #{part + 1}): {result}", flush=True)
                    emit({"type": "jev_toc_chunk", "document": document, "part": part + 1,
                          "chunks": len(items), "reason": str(result)[:200]})
                    continue
                best = max(best, result)
            offset += len(items)
            if best > _JEV_YES_LOOKING:
                yeses += 1
                sum_yes += best
            probabilities.append(best)
            emit({"type": "jev_progress", "stage": "toc", "done": done, "total": len(blocks),
                  "percent": round(100.0 * done / len(blocks), 1), "document": document,
                  "probability": round(best, 4), "yes": yeses,
                  "mean_yes_probability": round(sum_yes / yeses, 4) if yeses else 0.0})
        emit({"type": "jev_progress", "stage": "toc", "done": len(blocks), "total": len(blocks),
              "percent": 100.0, "yes": yeses,
              "mean_yes_probability": round(sum_yes / yeses, 4) if yeses else 0.0})
        return probabilities

    def _jev_toc_note(self, query: str, document: str, text: str, intent: str = "") -> str:
        """What one document holds relative to the question, in the corpus's own words."""
        payload = json.dumps({"質問": query, "初期検索意図": intent or query,
                              "文書": document, "この文書の目次": text},
                             ensure_ascii=False)
        llm = self.deterministic_llm or self.llm
        note = llm.complete(JEV_TOC_SUMMARY_PROMPT, sanitize_text(payload))
        self._record_usage(llm)
        return sanitize_text(note).replace("\n", " ").strip()

    def _jev_toc_notes(self, query: str, winners: list[tuple[str, str]], emit: Callable,
                       intent: str = "") -> list[str]:
        """Summarise the winning 目次 pages in a few size-balanced calls, not one per page.

        The rewriter's prompt wants `[文書名] 要約` lines, so whole 目次 pages cannot go to
        it: the top 10% of a wiki this size is ~450k chars. Large pages end up on their
        own, small ones share a call, and the stage costs TOC_NOTE_GROUPS calls however
        many pages the ranking kept.
        """
        bins = min(TOC_NOTE_GROUPS, len(winners))
        groups: list[list[tuple[int, str, str]]] = [[] for _ in range(bins)]
        sizes = [0] * bins
        for rank, (document, text) in enumerate(winners):  # biggest page into the emptiest group
            slot = min(range(bins), key=lambda index: sizes[index])
            groups[slot].append((rank, document, text))
            sizes[slot] += len(text)
        notes: list[str] = [""] * bins
        titles: list[str] = [""] * bins
        emit_guard = Lock()

        def summarize(slot: int, group: list[tuple[int, str, str]]) -> None:
            group.sort()
            document = " / ".join(name for _rank, name, _text in group)
            text = "\n\n".join(f"[{name}]\n{body}" for _rank, name, body in group)
            titles[slot] = document
            try:
                notes[slot] = self._jev_toc_note(query, document, text, intent)
            except AgentStopped:
                raise
            except Exception as exc:  # noqa: BLE001 - one unread group must not blind the rewrite
                print(f"[growi-search] Jev 目次 note failed ({document}): {exc}", flush=True)
                notes[slot] = text[:400].replace("\n", " ")
            with emit_guard:
                emit({"type": "jev_toc", "document": document, "note": notes[slot]})

        with self.timer.stage("toc_notes"):
            with ThreadPoolExecutor(max_workers=bins, thread_name_prefix="jev-toc") as pool:
                futures = [pool.submit(summarize, slot, group)
                           for slot, group in enumerate(groups) if group]
                for future in futures:
                    future.result()
        return [f"[{title}] {note}" for title, note in zip(titles, notes) if note]

    def _jev_toc_digest(self, query: str, state: "_MapState", budget: JevSweepBudget,
                        stop_event: Event | None, emit: Callable) -> tuple[str, int]:
        """A whole-wiki picture for the rewriter: every document summarised against the
        question first, then one digest the rewritten question has to account for.

        One LLM call per document, in parallel, instead of one truncated dump: the
        rewrite is then written from what the entire wiki covers, not from whichever
        document's cards fit in the prompt. Without a usable LLM there is no rewrite,
        so nothing is gathered at all.
        """
        if self.llm is None or not self.settings.llm_ready:
            return "", 0  # only the rewriter consumes this
        with self.timer.stage("toc_inventory"):
            blocks = self._jev_toc_blocks(state, budget, stop_event)
        if not blocks:
            return "", 0
        notes: list[str] = [""] * len(blocks)
        emit_guard = Lock()

        def summarize(index: int, document: str, text: str) -> None:
            try:
                if self.settings.jev_toc_gate and self.jev is not None:
                    chunks = _jev_chunks(text, self.settings.jev_chunk_tokens,
                                         self.settings.jev_chunk_overlap, prefix=document,
                                         count=getattr(self.jev, "count_tokens", _jev_est_tokens))
                    try:
                        probs = []
                        self.timer.count("jev_calls", len(chunks))
                        self.timer.count("jev_questions", len(chunks))
                        with self.timer.stage("jev_toc_gate"):
                            for part, chunk in enumerate(chunks):
                                _check_stop(stop_event)
                                probs.extend(self.jev.score_many(
                                    {"document": document, "toc": chunk},
                                    [JevQuestion(key=f"toc:{document}:{part}", text=jev_toc_question(query))]))
                    except AgentStopped:
                        raise
                    except Exception as exc:  # noqa: BLE001 - a failed optional gate must keep the note
                        log.info("jev 目次 gate failed (%s); keeping note: %s", document, exc)
                        probs = [1.0]
                    if probs and max(probs) < self.settings.jev_toc_gate_threshold:
                        notes[index] = "関連なし"
                    else:
                        notes[index] = self._jev_toc_note(query, document, text)
                else:
                    notes[index] = self._jev_toc_note(query, document, text)
            except AgentStopped:
                raise
            except Exception as exc:  # noqa: BLE001 - one unread document must not blind the rewrite
                log.info("jev 目次 summary failed (%s): %s", document, exc)
                notes[index] = text[:400].replace("\n", " ")
            with emit_guard:
                emit({"type": "jev_toc", "document": document, "note": notes[index]})

        workers = min(len(blocks), max(1, self.settings.subagent_concurrency))
        with self.timer.stage("toc_notes"):
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jev-toc") as pool:
                futures = [pool.submit(summarize, index, document, text)
                           for index, (document, text) in enumerate(blocks)]
                for future in futures:
                    future.result()
        return ("\n".join(f"[{document}] {note}"
                          for index, (document, _text) in enumerate(blocks)
                          if (note := notes[index]) != "関連なし"), len(blocks))

    def _jev_intent_query(self, query: str) -> str:
        """Expand the user's request into a strict, evidence-oriented search intent."""
        if self.llm is None or not self.settings.llm_ready:
            return ""
        payload = json.dumps({"ユーザーの質問": query}, ensure_ascii=False)
        llm = self.deterministic_llm or self.llm
        try:
            intent = llm.complete_structured(JEV_INTENT_PROMPT, sanitize_text(payload), JevIntent)
            self._record_usage(llm)
            self._jev_intent_structured = intent.model_dump()
            parts = [f"対象: {intent.target}", f"範囲: {intent.scope}",
                     f"広さ: {'広い' if intent.broad else '限定的'}"]
            if intent.required_evidence:
                parts.append(f"必要な証拠: {'、'.join(intent.required_evidence)}")
            if intent.must_include_terms:
                parts.append(f"必須語: {'、'.join(intent.must_include_terms)}")
            if intent.exclusions:
                parts.append(f"除外: {'、'.join(intent.exclusions)}")
            return "。".join(part for part in parts if part).strip()
        except Exception as exc:  # noqa: BLE001 - intent expansion must not disable the sweep
            log.info("structured jev intent failed; using text intent: %s", exc)
            try:
                text = llm.complete(JEV_INTENT_PROMPT, sanitize_text(payload))
                self._record_usage(llm)
                return sanitize_text(text).replace("\n", " ").strip()
            except Exception as fallback_exc:  # noqa: BLE001 - raw query remains safe
                log.info("jev intent expansion failed; using raw question: %s", fallback_exc)
                return ""

    def _jev_target_query(self, query: str, material: str, intent: str = "") -> str:
        """Rewrite the scoped intent into a page-target question using selected TOCs."""
        if self.llm is None or not self.settings.llm_ready or not material:
            return ""
        payload = json.dumps({"元の質問": query, "初期検索意図": intent or query,
                              "構造化された初期検索意図": self._jev_intent_structured,
                              "第1段階で選別された文書の目次要約": material},
                             ensure_ascii=False)
        try:
            llm = self.deterministic_llm or self.llm
            text = llm.complete(JEV_QUERY_REWRITE_PROMPT, sanitize_text(payload))
            self._record_usage(llm)
        except Exception as exc:  # noqa: BLE001 - a missing rewrite must not fail the sweep
            log.info("jev query rewrite failed; using the question as-is: %s", exc)
            return ""
        return sanitize_text(text).replace("\n", " ").strip()

    def _jev_refine_from_seeds(self, query: str, results: list[dict[str, Any]]) -> str:
        """Make the second-pass question from the evidence found by a broad scan."""
        if not results:
            return ""
        material = "\n\n".join(
            f"[{result['node'].title}] {result['node'].summary}\n{result['node'].path}"
            for result in results
        )
        return self._jev_target_query(query, material, self._jev_scope_query or query)

    def _jev_candidates_enough(self, query: str, intent: str,
                               candidates: list[tuple[str, str]]) -> bool:
        """Ask the LLM whether the scoped shortlist can safely stand for the corpus."""
        if not candidates or self.llm is None or not self.settings.llm_ready:
            return False
        material = "\n\n".join(f"[{document}]\n{text}" for document, text in candidates)
        structured = self._jev_intent_structured
        if not structured or structured.get("broad"):
            return False
        required = [term.casefold() for term in self._jev_required_terms()]
        normalized_material = unicodedata.normalize("NFKC", material).casefold()
        if required and any(term not in normalized_material for term in required):
            return False
        payload = json.dumps({"元の質問": query, "検索意図": intent,
                              "構造化された意図": structured,
                              "候補文書の目次": material}, ensure_ascii=False)
        try:
            llm = self.deterministic_llm or self.llm
            verdict = sanitize_text(llm.complete(
                JEV_CANDIDATE_COVERAGE_PROMPT, payload)).strip()
            self._record_usage(llm)
        except Exception as exc:  # noqa: BLE001 - uncertainty must scan all documents
            log.info("jev candidate coverage check failed: %s", exc)
            return False
        return verdict.startswith("十分") and "不足" not in verdict

    def _jev_required_terms(self) -> list[str]:
        """Exact anchors a narrow structured intent requires in a readable page."""
        structured = self._jev_intent_structured
        if not structured or structured.get("broad"):
            return []
        terms = [str(term).strip() for term in structured.get("must_include_terms", [])
                 if str(term).strip() and " " not in str(term) and "　" not in str(term)]
        if not terms and structured.get("target"):
            target = str(structured["target"]).strip()
            if " " not in target and "　" not in target:
                terms = [target]
        return terms

    def _jev_prefiltered(self, query: str, page: WikiPage) -> bool:
        """Cheap gate in front of the expensive body pass.

        A page that shares no keyword unit with the question cannot hold the
        answer, so it costs one set intersection instead of a model call per chunk.
        On by default (2 shared units). If a query is phrased in words the corpus
        never uses (an English question over a Japanese manual), `prefiltered` in
        `jev_complete` climbs and seeds collapse — lower it to 1, or 0 to disable.
        """
        needed = self.settings.jev_prefilter_min_overlap
        if needed < 1:
            return False
        with self._gram_lock:
            grams = self._query_grams.get(query)
            if grams is None:
                grams = self._query_grams[query] = IndexMap.grams(query)
        if not grams:
            return False
        # Wide window on purpose: function tables and enumerations sit deep in
        # long pages, and dropping one of those is the exact failure this gate is
        # supposed to prevent. A short question ("深い") cannot produce 2 units, so
        # never demand more units than the question itself carries.
        key = page.id or page.path, page.revision_id
        with self._gram_lock:
            page_grams = self._page_grams.get(key)
            if page_grams is None:
                page_grams = IndexMap.grams(f"{page.title}\n{page.path}\n{page.body}"[:20000])
                self._page_grams[key] = page_grams
                if len(self._page_grams) > 4096:
                    self._page_grams.popitem(last=False)
            else:
                self._page_grams.move_to_end(key)
        shared = grams & page_grams
        return len(shared) < min(needed, len(grams))

    def _jev_confirm(self, query: str, reference: str, document: str, state: "_MapState",
                     source_card: md.IndexCard | None, budget: JevSweepBudget,
                     stop_event: Event | None, emit: Callable, work: "_SweepWork") -> None:
        """Card candidate or frontier target -> full-body seed confirmation.

        Runs on several sweep threads at once: only the shared-state steps take
        the sweep lock, the GROWI read and the classification stay outside it.
        """
        st = self.settings
        key = reference.strip("/")
        if not key:
            return
        with work.lock:
            if key in work.visited:
                return
            work.visited.add(key)
        page = self._fetch_ref(reference, sweep_budget=budget)
        if page is None:
            # Budget exhausted or page gone: never fabricate a seed.
            self._jev_gate(emit, "full", "unread", {"id": key, "title": key}, 0.0, document, 0)
            return
        if page.id:
            with work.lock:
                work.visited.add(page.id)
        if self._jev_prefiltered(query, page):
            with work.lock:
                work.stats["prefiltered"] += 1
            self._jev_gate(emit, "full", "prefiltered", page, 0.0, document, 0)
            return
        entities = source_card.entities if source_card is not None else self._page_entities(state, page)
        entity_defs: list[dict] = []
        seen_defs: set[tuple[str, str]] = set()
        if self.index_map is not None:
            for entity in entities:
                for definer in self.index_map.definers_for(entity):
                    marker = (entity, definer.target)
                    if marker in seen_defs or definer.target.strip("/") == key:
                        continue
                    seen_defs.add(marker)
                    entity_defs.append({"entity": entity, "title": definer.title,
                                        "path": definer.target, "summary": definer.summary})
        try:
            prob, chunk_count = self._jev_score_body(query, document, page, entity_defs, stop_event)
        except RuntimeError as exc:
            # Classifier became unusable: abort the sweep; ES is recall insurance.
            emit({"type": "jev_gate", "stage": "full", "status": "error",
                  "node": {"id": page.id, "title": page.title}, "document": document,
                  "probability": 0.0, "threshold": st.jev_seed_threshold, "chunk_count": 0})
            raise
        if prob < st.jev_seed_threshold:
            with work.lock:
                work.max_probability = max(work.max_probability, prob)
                work.stats["pages_considered"] += 1
            self._jev_gate(emit, "full", "pruned", page, prob, document, chunk_count)
            return
        with work.lock:
            work.max_probability = max(work.max_probability, prob)
            work.stats["pages_considered"] += 1
            work.stats["confirmed"] += 1
            # Ranks are stamped once the sweep ends: threads finish out of order.
            work.results.append({
                "node": page, "score": prob, "why": [], "document": document,
                "evidence": [Evidence(
                    page_id=page.id, field="jev",
                    text=f"Jev p(はい)={prob:.2f}; stage=full; chunks={chunk_count}",
                    source_rank=0, score=prob).model_dump()],
            })
            work.frontier.append((page, source_card))
        self._jev_gate(emit, "full", "confirmed", page, prob, document, chunk_count)

    def _jev_accept_card(self, card: md.IndexCard, document: str, probability: float,
                         budget: JevSweepBudget, stop_event: Event | None,
                         emit: Callable, work: "_SweepWork") -> None:
        """Hydrate a yes card, applying exact-term body validation for narrow intents."""
        _check_stop(stop_event)
        key = card.target.strip("/")
        with work.lock:
            if key in work.visited:
                return
            work.visited.add(key)
        page = self._fetch_ref(card.target, sweep_budget=budget)
        if page is None:
            self._jev_gate(emit, "read", "unread", IndexMap.card_page(card), probability, document, 0)
            return
        required_terms = self._jev_required_terms()
        if required_terms:
            searchable = unicodedata.normalize(
                "NFKC", f"{page.title}\n{page.path}\n{page.body or ''}"
            ).casefold()
            if any(term.casefold() not in searchable for term in required_terms):
                with work.lock:
                    work.stats["pages_considered"] += 1
                self._jev_gate(emit, "read", "pruned", page, probability, document, 0)
                return
        with work.lock:
            if page.id in work.visited and page.id != key:
                return
            work.visited.add(page.id)
            work.stats["pages_considered"] += 1
            work.stats["confirmed"] += 1
            work.results.append({
                "node": page, "score": probability, "why": [], "document": document,
                "evidence": [Evidence(
                    page_id=page.id, field="jev",
                    text=f"Jev p(はい)={probability:.2f}; stage=card; document index={document}",
                    source_rank=0, score=probability).model_dump()],
            })
        self._jev_gate(emit, "read", "confirmed", page, probability, document, 0)

    def _run_jev_sweep(self, query: str, emit: Callable, stop_event: Event | None,
                       force_full: bool = False) -> list[dict[str, Any]]:
        """Score reachable document-index cards and hydrate the yes pages as seeds.

        One thread scores document cards while worker threads fetch the accepted
        pages. The body is read for exploration, not classified by Jev.

        Never an answer generator: an empty list (or any failure) leaves the
        existing ES -> index map -> router -> lead path completely intact.
        """
        started = time.monotonic()
        st = self.settings
        self._jev_early_mode = False
        self._jev_early_doc_keys = set()
        workers = max(1, st.jev_workers)
        budget = JevSweepBudget(st.jev_max_page_reads, st.jev_max_list_calls)
        work = _SweepWork(stats={"documents": 0, "pages_considered": 0, "cards_considered": 0,
                                 "candidates": 0, "confirmed": 0, "entity_edges_followed": 0,
                                 "prefiltered": 0})
        status = "ok"
        print(f"[growi-search] Jev sweep: start backend={st.jev_backend} workers={workers}", flush=True)
        # tqdm-style live progress: one unit per card verdict or accepted page read.
        # The total grows as the index reveals cards, so the percentage is clamped
        # to never recede and is forced to 100 when the sweep ends.
        progress_lock = Lock()
        units = {"total": 0, "done": 0, "pct": 0.0, "scored_full": 0, "scored_card": 0,
                 "yes_full": 0, "sum_yes_full": 0.0, "yes_card": 0, "sum_yes_card": 0.0}

        def progress(add_total: int = 0, step: int = 1, stage: str = "full",
                     probability: float | None = None) -> None:
            """One tick: work discovered/done plus the running average of the yeses.

            Only verdicts leaning yes (p > 0.5) are averaged: a corpus where 95% of
            pages are irrelevant makes an all-verdict average useless. Card and body
            verdicts stay apart because a card summary and a full page body are
            different distributions. Skipped pages contribute nothing.
            """
            with progress_lock:
                units["total"] += add_total
                units["done"] += step
                if probability is not None:
                    bucket = "card" if stage == "card" else "full"
                    units[f"scored_{bucket}"] += 1
                    if probability > _JEV_YES_LOOKING:
                        units[f"yes_{bucket}"] += 1
                        units[f"sum_yes_{bucket}"] += float(probability)
                total = max(units["total"], units["done"])
                pct = 100.0 * units["done"] / total if total else 0.0
                units["pct"] = min(100.0, max(units["pct"], pct))
                scored = units["scored_full"] + units["scored_card"]
                payload = {"type": "jev_progress", "done": units["done"], "total": total,
                           "percent": round(units["pct"], 1), "scored": scored,
                           "yes": units["yes_full"] + units["yes_card"], **_yes_means(units)}
                heartbeat = (f"[growi-search] Jev sweep: {payload['percent']:.0f}% "
                             f"{units['done']}/{total} scored={scored} "
                             f"yes={payload['yes']} "
                             f"avg_p={payload['mean_yes_probability']:.2f} "
                             f"card_avg_p={payload['mean_yes_card_probability']:.2f}")
            emit(payload)
            if probability is not None and (scored == 1 or scored % 25 == 0):
                print(heartbeat, flush=True)

        def gated(event: dict[str, Any]) -> None:
            """Every jev_gate the sweep emits is one finished work unit."""
            if event.get("type") == "jev_gate":
                verdict = event.get("status") in ("confirmed", "pruned", "candidate")
                progress(stage=event.get("stage", "full"),
                         probability=event.get("probability") if verdict else None)
            emit(event)

        # The queue bound keeps the card scorer from outrunning page hydration.
        targets: Queue = Queue(maxsize=workers * 4)
        drained = object()

        def produce(state: "_MapState") -> None:
            try:
                docs = self._jev_documents(state, budget, stop_event)
                preferred = docs
                remainder: list[dict] = []
                if self._jev_early_doc_keys and not force_full:
                    wanted = {key.strip("/") for key in self._jev_early_doc_keys}
                    preferred = [doc for doc in docs if doc["key"].strip("/") in wanted]
                    remainder = [doc for doc in docs if doc["key"].strip("/") not in wanted]
                with work.lock:
                    work.stats["documents"] = len(preferred)

                def scan(scan_docs: list[dict]) -> int:
                    found = 0
                    progress(add_total=sum(len(d["cards"]) for d in scan_docs), step=0)
                    for doc in scan_docs:
                        _check_stop(stop_event)
                        if work.errors:
                            return found
                        candidates = self._jev_score_cards(jev_query, doc, gated, stop_event, work)
                        found += len(candidates)
                        progress(add_total=len(candidates), step=0)
                        for card, probability in candidates:
                            targets.put((card, doc["key"], probability))
                    return found

                found = scan(preferred)
                # Keep this inside the same producer/sweep. A shortlist with no
                # page-level yes simply falls through to the remaining documents;
                # it does not create another SSE/Jev stage.
                if not found and remainder and not work.errors:
                    with work.lock:
                        work.stats["documents"] = len(docs)
                    scan(remainder)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the sweep thread
                with work.lock:
                    work.errors.insert(0, exc)  # the first failure decides the fallback
            finally:
                for _ in range(workers):
                    targets.put(drained)

        def consume(state: "_MapState") -> None:
            while True:
                item = targets.get()
                if item is drained:
                    return
                if work.errors:
                    continue  # drain only: never block the producer while the run unwinds
                card, document, probability = item
                try:
                    self._jev_accept_card(card, document, probability, budget,
                                          stop_event, emit, work)
                except BaseException as exc:  # noqa: BLE001 - re-raised on the sweep thread
                    with work.lock:
                        work.errors.append(exc)
                finally:
                    progress(step=1, stage="read")

        try:
            state = self.index_map.snapshot() if self.index_map is not None else _MapState()
            # Rewrite against document 00-目次 pages reached from project indexes.
            # First expand the user's scope, use that intent to rank document TOCs,
            # then requery the selected TOCs into a precise page-level question.
            material, entries = "", 0
            intent = query
            requery = ""
            shortlist_enough = False
            narrow_early = False
            narrow_toc_seeds: list[tuple[str, float]] | None = None
            blocks: list[tuple[str, str]] = []
            with self.timer.stage("rewrite"):
                try:
                    blocks = self._jev_toc_blocks(state, budget, stop_event)
                    if blocks and self.jev is not None and self.settings.llm_ready:
                        intent = self._jev_intent_query(query) or query
                        emit({"type": "jev_intent", "text": intent,
                              "structured": self._jev_intent_structured})
                        print(f"[growi-search] Jev intent: {intent}", flush=True)
                        # Preserve the original scope pass: Jev evaluates every
                        # reachable document 目次 before the LLM creates the precise
                        # page-level requery.
                        # batch=True groups the scan into shared engine calls of at most
                        # batch_size items; the backend sends one state per HTTP call,
                        # up to llm2jev_concurrency in flight.
                        probs = self._jev_toc_probs(intent, blocks, stop_event, emit,
                                                          batch=True, batch_size=20)
                        ranked = sorted(zip(blocks, probs), key=lambda pair: pair[1], reverse=True)
                        yes_docs = [(document, probability)
                                    for (document, _text), probability in ranked
                                    if probability > _JEV_YES_LOOKING]
                        structured = self._jev_intent_structured or {}
                        is_narrow = isinstance(structured, dict) and structured.get("broad") is False
                        if (not force_full and is_narrow
                                and 1 <= len(yes_docs) <= _JEV_NARROW_DOC_LIMIT):
                            # Narrow question with a few TOC yes: skip notes +
                            # rewrite + card scoring. The yes documents become the
                            # seed scope directly.
                            winners = [(document, text) for (document, text), _p in ranked
                                       if any(d == document for d, _ in yes_docs)]
                            entries = len(winners)
                            material, requery = "", ""
                            jev_query = intent
                            narrow_early = True
                            narrow_toc_seeds = yes_docs
                            self._jev_early_mode = True
                            self._jev_early_doc_keys = {document for document, _ in yes_docs}
                            shortlist_enough = True
                            print(f"[growi-search] Jev narrow early exit: "
                                  f"{len(yes_docs)} TOC yes, skipping rewrite + card scoring",
                                  flush=True)
                        else:
                            selected = ranked
                            keep = max(1, math.ceil(0.1 * len(selected)))
                            cutoff = selected[keep - 1][1]
                            material_budget = TOC_NOTE_GROUPS * TOC_NOTE_GROUP_CHARS
                            winners, used = [], 0
                            for (document, text), probability in selected:  # ties at the cutoff stay in
                                if probability < cutoff or (winners and used + len(text) > material_budget):
                                    break
                                winners.append((document, text))
                                used += len(text)
                            material = "\n".join(self._jev_toc_notes(intent, winners, emit, intent))
                            entries = len(winners)
                            requery = self._jev_target_query(query, material, intent) or ""
                            jev_query = requery or intent
                            if not force_full:
                                shortlist = self._jev_retrieve_tocs(
                                    jev_query, blocks, JEV_TOC_RETRIEVAL_K)
                                enough = self._jev_candidates_enough(query, jev_query, shortlist)
                                shortlist_enough = bool(shortlist and enough)
                                if shortlist and enough:
                                    self._jev_early_mode = True
                                    self._jev_early_doc_keys = {document for document, _ in shortlist}
                                print(f"[growi-search] Jev 目次 shortlist: {len(shortlist)} candidates; "
                                      f"coverage={'enough' if enough else 'all-doc scan'}", flush=True)
                except AgentStopped:
                    raise
                except Exception as exc:  # noqa: BLE001 - rank/rewrite failure falls back to raw
                    print(f"[growi-search] Jev top-10% requery failed; using raw question: {exc}", flush=True)
                    emit({"type": "jev_toc_failed", "reason": str(exc)[:200]})
                    material, entries, requery = "", 0, ""
                # Always reported: "0 文書" used to be a silent console line, which is how a
                # dead stage looked like an empty wiki.
                emit({"type": "jev_toc_digest", "scanned": len(blocks), "kept": entries,
                      "retrieved": (len(self._jev_early_doc_keys)
                                    if self._jev_early_mode else len(blocks)),
                      "early": self._jev_early_mode,
                      "narrow": narrow_early,
                      "shortlist_enough": shortlist_enough,
                      "groups": min(TOC_NOTE_GROUPS, entries), "chars": len(material)})
                # The second-stage question is the scoped requery. Keeping the raw
                # broad question alongside it would make Jev accept topic-only pages.
                jev_query = requery or intent
                self._jev_scope_query = jev_query
            emit({"type": "jev_query", "text": jev_query, "rewritten": jev_query != query,
                  "toc_entries": entries})
            print(f"[growi-search] Jev 目次 digest: {entries} 文書 / {len(material)} chars "
                  f"under {st.growi_root_path}", flush=True)
            origin = (f"rewritten from {entries} 目次 entries" if jev_query != query
                      else "raw question — no usable 目次 rewrite")
            print(f"[growi-search] Jev query ({origin}): {jev_query}", flush=True)
            if narrow_toc_seeds is not None:
                # Narrow early exit: TOC yes docs -> page seeds without card scoring.
                # Cards are still hydrated + required-terms checked, so no unchecked seed.
                with self.timer.stage("sweep"):
                    docs = self._jev_documents(state, budget, stop_event)
                    by_key = {doc["key"].strip("/"): doc for doc in docs}
                    with work.lock:
                        work.stats["documents"] = len(narrow_toc_seeds)
                        work.max_probability = max(
                            (prob for _, prob in narrow_toc_seeds), default=work.max_probability)
                    total_cards = sum(len(by_key.get(doc.strip("/"), {}).get("cards", []))
                                      for doc, _ in narrow_toc_seeds)
                    progress(add_total=total_cards, step=0)
                    with work.lock:
                        work.stats["cards_considered"] += total_cards
                    for document, toc_prob in narrow_toc_seeds:
                        _check_stop(stop_event)
                        if work.errors:
                            break
                        entry = by_key.get(document.strip("/"))
                        if entry is None:
                            continue
                        for card in entry["cards"]:
                            _check_stop(stop_event)
                            if work.errors:
                                break
                            try:
                                self._jev_accept_card(card, entry["key"], toc_prob, budget,
                                                      stop_event, emit, work)
                            except BaseException as exc:  # noqa: BLE001 - re-raised below
                                with work.lock:
                                    work.errors.append(exc)
                            finally:
                                progress(step=1, stage="read")
                    with work.lock:
                        work.stats["candidates"] += work.stats["confirmed"]
            else:
                with self.timer.stage("sweep"):
                    crawler = Thread(target=produce, args=(state,), name="jev-crawl", daemon=True)
                    crawler.start()
                    pool = [Thread(target=consume, args=(state,), name=f"jev-score-{i}", daemon=True)
                            for i in range(workers)]
                    for worker in pool:
                        worker.start()
                    for worker in pool:
                        worker.join()
                    crawler.join()
            with work.lock:
                failure = work.errors[0] if work.errors else None
            if failure is not None:
                raise failure
        except AgentStopped:
            raise
        except (RuntimeError, GrowiAPIError) as exc:
            status = "fallback"
            with work.lock:
                work.results.clear()
                work.stats["confirmed"] = 0
            log.info("jev sweep failed; falling back to ES: %s", exc)
            emit({"type": "jev_unavailable", "reason": str(exc)[:200]})
        elapsed_ms = int((time.monotonic() - started) * 1000)
        emit({"type": "jev_progress", "done": units["done"], "total": max(units["total"], units["done"]),
              "percent": 100.0, "scored": units["scored_full"] + units["scored_card"],
              "yes": units["yes_full"] + units["yes_card"], **_yes_means(units)})
        # Confidence decides the seed order: the pipeline finishes pages out of order.
        results = sorted(work.results, key=lambda result: -result["score"])
        for ordinal, result in enumerate(results, start=1):
            result["why"] = [{"field": "jev", "rank": ordinal}]
            for evidence in result["evidence"]:
                evidence["source_rank"] = ordinal
        with work.lock:
            stats = dict(work.stats)
            max_probability = work.max_probability
        emit({"type": "jev_complete", **stats,
              "max_probability": round(max_probability, 4),
              **_yes_means(units),
              "yes": units["yes_full"] + units["yes_card"],
              "scored": units["scored_full"] + units["scored_card"],
              "page_reads": budget.page_reads, "list_calls": budget.list_calls,
              "elapsed_ms": elapsed_ms})
        print(
            f"[growi-search] Jev sweep: {status} documents={stats['documents']} "
            f"cards={stats['cards_considered']} pages={stats['pages_considered']} "
            f"confirmed={stats['confirmed']} yes={units['yes_card']} "
            f"avg_p={_yes_means(units)['mean_yes_probability']:.2f} "
            f"max_p={max_probability:.2f} elapsed_ms={elapsed_ms}",
            flush=True,
        )
        return results

    # -- ask -----------------------------------------------------------------

    def ask(
        self,
        question: str,
        on_event: Callable | None,
        stop_event: Event | None,
        context: str = "",
        cited_node_ids: list[str] | None = None,
    ) -> AgentAnswer:
        emit = on_event or (lambda _e: None)
        question = sanitize_text(question or "").strip()[:2000]
        context = sanitize_text(context or "").strip()[-30000:]
        emit({"type": "start", "question": question})

        answer = None
        reused = False
        if context:
            try:
                text = self.llm.complete(
                    FOLLOWUP_ANSWER_PROMPT,
                    json.dumps({"question": question, "prior_context": context}, ensure_ascii=False),
                ).strip()
                self._record_usage()
                if text and text != "NEEDS_RESEARCH":
                    emit({"type": "route", "mode": "reuse", "reason": "prior conversation was sufficient"})
                    reused = True
                    answer = AgentAnswer(
                        question=question,
                        answer=sanitize_text(text),
                        cited_node_ids=_clean_ids(cited_node_ids),
                        steps=1,
                    )
            except Exception as exc:  # noqa: BLE001 - fall back to normal research
                log.info("conversation reuse failed; full research: %s", exc)
        if answer is None:
            answer = self._try_route(question, emit, stop_event, question)
        if answer is None:
            with self.timer.stage("lead"):
                try:
                    answer = self._run_lead(question, emit, stop_event)
                    if not answer.answer.strip() or answer.answer.strip() == STEP_LIMIT_TEXT:
                        emit({"type": "lead_failed", "reason": "lead returned no completed answer"})
                        answer = self._degraded_answer(question, emit) or answer
                except AgentStopped:
                    raise
                except Exception as exc:  # noqa: BLE001 - a reset connection must not erase the research
                    log.info("lead agent failed: %s", exc)
                    emit({"type": "lead_failed", "reason": str(exc)[:200]})
                    answer = self._degraded_answer(question, emit)
                    if answer is None:
                        raise
        if not reused:  # a reused prior answer was checked when it was first given
            with self.timer.stage("verify"):
                answer = self._verified(question, answer, emit)
        answer = self._retain_raw_reports(answer)
        answer.cited_nodes = [self._cite(node_id) for node_id in answer.cited_node_ids]
        usages = [*self._usage_cb.usage_metadata.values(), *self._extra_usage]
        _log_usage(question, answer.answer, answer.steps, usages, self.settings)
        timing = self.timer.snapshot()
        emit({"type": "timings", **timing})
        log.info("timings %s", json.dumps(timing, ensure_ascii=False))
        return answer

    def _verified(self, question: str, answer: AgentAnswer, emit: Callable) -> AgentAnswer:
        """Check the answer against the full text of the pages it rests on.

        Agents answer from reports and snippets, i.e. from paraphrase; one more call with
        the source text removes what the pages do not say and fixes facts carried over
        from a neighbouring subject.
        """
        sources: list[str] = []
        titles: dict[str, str] = {}
        # Keep every readable cited page together. If the full verification prompt
        # exceeds the model context, preserve the draft instead of checking a subset.
        ids = sorted(dedupe(answer.cited_node_ids), key=lambda node_id: node_id not in answer.answer)
        for node_id in ids:
            # Cited ids are sometimes paths (card targets): resolve either form,
            # so a path citation is checked, not silently skipped.
            page = self._fetch_ref(node_id)
            if page is None or not (page.body or "").strip():
                continue
            titles[page.id] = page.title
            sources.append(f"--- {page.id} : {page.title} ---\n{page.body}")
        emit({"type": "verify", "pages": len(sources)})
        if not sources:
            if self._seed_raw_reports:
                return answer.model_copy(update={"answer": "", "cited_node_ids": self._seed_ids[:]})
            # A completed cascade/lead answer must not be erased merely because
            # its cited pages are temporarily unreadable during verification.
            return answer
        if self.llm is None or not self.settings.llm_ready:
            return answer
        payload = json.dumps({"question": question, "draft_answer": answer.answer,
                              "source_pages": "\n\n".join(sources)}, ensure_ascii=False)
        if self._llm_tokens(ANSWER_VERIFY_PROMPT + payload) > self._llm_input_budget():
            return answer.model_copy(update={
                "answer": answer.answer + "\n\n※原文全体がモデルの入力枠を超えるため、最終照合は省略しました。"})
        try:
            text = self.llm.complete(
                ANSWER_VERIFY_PROMPT,
                payload,
                max_tokens=self.settings.final_compiler_tokens,
            ).strip()
            self._record_usage()
        except Exception as exc:  # noqa: BLE001 - say so rather than pass it off as checked
            log.info("answer verification failed: %s", exc)
            return answer.model_copy(update={"answer": answer.answer + "\n\n※この回答は原文との照合ができませんでした。"})
        if _llm_output_capped(self.llm, self.settings.final_compiler_tokens):
            return answer.model_copy(update={"answer": answer.answer + "\n\n※原文照合の出力が上限に達したため、照合前の回答を保持しました。"})
        if not text:
            return answer
        cited = [node_id for node_id in titles if node_id in text] or list(titles)
        return AgentAnswer(question=question, answer=sanitize_text(text), cited_node_ids=cited, steps=answer.steps)

    def _cite(self, page_id: str) -> dict[str, str]:
        page = self._memo_page(page_id)
        if page is None:
            for results in self._search_memo.values():
                page = next((r["node"] for r in results if r["node"].id == page_id), None)
                if page:
                    break
        if page is None and self.index_map is not None:
            card = self.index_map.card_for(page_id)
            page = IndexMap.card_page(card) if card else None
        return {"id": page_id, "title": page.title if page else page_id, "path": page.path if page else "",
                "summary": page.summary if page else ""}

    def _record_usage(self, llm: Any = None) -> None:
        usage = getattr(llm or self.llm, "last_usage", None)
        self.timer.count("llm_calls")
        if usage:
            self.timer.count("llm_input_tokens", usage.get("input_tokens", 0))
            self.timer.count("llm_output_tokens", usage.get("output_tokens", 0))
            self._extra_usage.append(usage)

    def _llm_tokens(self, content: str) -> int:
        counter = getattr(getattr(self.llm, "llm", None), "get_num_tokens", None)
        if counter is not None:
            try:
                return int(counter(content))
            except Exception:  # noqa: BLE001 - the server may use an unknown tokenizer
                pass
        # One UTF-8 byte per token is a conservative fallback, never a slice of text.
        return len(content.encode("utf-8"))

    def _llm_input_budget(self) -> int:
        # Output budgets are generation limits only. Do not turn them into a
        # character/token cap on the evidence sent to a compiler.
        return self.settings.llm_context_tokens

    def _llm_chunks(self, material: str, system: str, payload: Callable[[str], str]) -> list[str]:
        """Partition complete input only when it exceeds the configured context; discard nothing."""
        budget = self._llm_input_budget()
        if self._llm_tokens(system + payload("")) >= budget:
            raise ValueError("LLM prompt alone exceeds the configured context budget")
        chunks: list[str] = []
        start = 0
        while start < len(material):
            low, high, end = start + 1, len(material), start
            while low <= high:
                middle = (low + high) // 2
                cost = self._llm_tokens(system + payload(material[start:middle]))
                if cost <= budget:
                    end = middle
                    low = middle + 1
                else:
                    high = middle - 1
            if end == start:
                raise ValueError("one character exceeds the configured LLM context budget")
            chunks.append(material[start:end])
            start = end
        return chunks

    def _try_route(self, question: str, emit: Callable, stop_event: Event | None, _q: str) -> AgentAnswer | None:
        self._seed_context = ""
        self._seed_pieces = ("", "")
        self._seed_ids = []
        self._seed_raw_reports = []
        self._jev_scope_query = ""
        self._jev_intent_structured = {}
        self._jev_early_doc_keys = set()
        self._jev_early_mode = False
        _check_stop(stop_event)
        if self.jev is not None:
            if self.settings.jev_mode == "cascade":
                from jev.types import JevUnavailable
                from cascade import run_cascade

                try:
                    return run_cascade(self, question, emit, stop_event)
                except JevUnavailable as exc:
                    log.info("cascade unavailable; falling back to ES route: %s", exc)
                    emit({"type": "cascade_fallback", "reason": str(exc)[:200]})
            confirmed = ([] if self.settings.jev_mode == "cascade" else
                         self._run_jev_sweep(question, emit, stop_event))
            if confirmed:
                seed_count = len(confirmed)
                if seed_count <= JEV_FAST_SEED_LIMIT:
                    explored = confirmed
                    seeds = "\n\n".join(format_lead_candidate(result) for result in explored)
                    raw_pages = self._direct_seed_material(explored)
                    self._seed_ids = dedupe([result["node"].id or result["node"].path
                                             for result in explored])
                    self._seed_context = f"{seeds}\n\n{raw_pages}"
                    self._seed_pieces = (seeds, raw_pages)
                    self._seed_raw_reports = []
                    emit({"type": "candidates", "count": seed_count,
                          "nodes": [node_ref(result["node"]) for result in explored]})
                    emit({"type": "compiling", "mode": "direct"})
                    return self._synthesize_reports(
                        question, "", raw_pages, set(self._seed_ids), emit)
                large_path = seed_count > JEV_ONE_STAGE_SEED_LIMIT
                if large_path:
                    refined = self._jev_refine_from_seeds(question, confirmed)
                    if refined:
                        emit({"type": "jev_adaptive", "mode": "refined",
                              "reason": "broad seed set requires a second Jev pass",
                              "seeds": seed_count})
                        first = confirmed
                        second = self._run_jev_sweep(refined, emit, stop_event, force_full=True)
                        by_ref = {result["node"].id or result["node"].path: result
                                  for result in first}
                        for result in second:
                            by_ref.setdefault(result["node"].id or result["node"].path, result)
                        confirmed = sorted(by_ref.values(), key=lambda result: -result["score"])
                        seed_count = len(confirmed)
                groups = _seed_groups(confirmed, self.settings.jev_subagent_group_size,
                                      self.settings.jev_subagent_groups)
                explored = [result for group in groups for result in group]
                emit({"type": "candidates", "count": len(explored),
                      "nodes": [node_ref(result["node"]) for result in explored]})
                seeds = "\n\n".join(format_lead_candidate(result) for result in explored)
                # One subagent per document slice; the merged reports are what the
                # final compiler answers from, including the one-explorer case.
                scoped_question = self._jev_scope_query or question
                with self.timer.stage("seed_groups"):
                    reports, cited = self._run_seed_groups(
                        groups, confirmed, scoped_question, emit, stop_event,
                        levelwise=large_path)
                self._seed_context = f"{seeds}\n\n{reports}"
                self._seed_pieces = (seeds, reports)
                self._seed_ids = dedupe([result["node"].id for result in explored] + cited)
                # Exploration and both compiler levels are already complete.
                # Always run the final report synthesizer now; sending this back
                # through the tool-using lead can repeat research or stop without
                # calling finish, and an environment override must not re-enable it.
                emit({"type": "compiling",
                      "mode": "two_stage" if large_path else "one_stage",
                      "seed_count": seed_count})
                answer = self._synthesize_reports(question, seeds, reports,
                                                   set(self._seed_ids), emit)
                if answer is not None:
                    return answer
                return None  # force the deep lead path on the sweep's own findings
        if self.budget.pages_exhausted:
            emit({"type": "budget", "pages_used": self.budget.pages_used, "message": "ページ取得上限到達"})
        with self.timer.stage("es_route"):
            results = self.search_with_evidence(question, self.settings.rerank_top_k)
            results = self._merge_map(question, results, self.settings.index_map_top_k, emit)
        if not results:
            emit({"type": "route", "mode": "deep", "reason": "no candidates"})
            return None
        emit({"type": "candidates", "count": len(results), "nodes": [node_ref(r["node"]) for r in results]})
        payload = {"question": question, "candidates": [_describe(r) for r in results]}
        try:
            with self.timer.stage("es_route"):
                decision = self.llm.complete_structured(ROUTER_PROMPT, json.dumps(payload, ensure_ascii=False), RouteDecision)
            self._record_usage()
            if not isinstance(decision, RouteDecision):
                decision = RouteDecision.model_validate(decision)
        except Exception as exc:  # noqa: BLE001 - default to deep
            log.info("router failed; deep mode: %s", exc)
            return None
        mode = (decision.mode or "deep").strip().lower()
        if mode == "reuse":
            mode = "shallow"
        emit({"type": "route", "mode": mode, "reason": decision.reason})
        _check_stop(stop_event)

        if mode == "deep":
            self._seed_context = "\n\n".join(format_lead_candidate(r) for r in results)
            self._seed_ids = [r["node"].id for r in results]
            return None

        emit({"type": "budget", "pages_used": self.budget.pages_used})
        hydrated = self.hydrate_shallow(results, question, emit)
        with self.timer.stage("shallow_answer"):
            answer = self._answer_shallow(question, hydrated, emit)
        if answer is not None:
            return answer
        # Shallow failed: fall through to deep with fresh hydrated candidates.
        self._seed_context = "\n\n".join(format_lead_candidate(r) for r in hydrated)
        self._seed_ids = [r["node"].id for r in hydrated]
        return None

    def _answer_shallow(self, question: str, results: list[dict[str, Any]], emit: Callable) -> AgentAnswer | None:
        top = results[: max(1, self.settings.shallow_page_reads + 3)]
        context = {
            "question": question,
            "notes": [
                {
                    "node_id": r["node"].id,
                    "title": r["node"].title,
                    "path": r["node"].path,
                    "summary": r["node"].summary,
                    "evidence": [" ".join((ev.get("text") or "").split()) for ev in r.get("evidence", [])],
                    "body": sanitize_text(r["node"].body)[:MAX_TEXT],
                }
                for r in top
            ],
        }
        emit({"type": "compiling"})
        try:
            text = self.llm.stream(
                SHALLOW_ANSWER_PROMPT, json.dumps(context, ensure_ascii=False),
                lambda piece: emit({"type": "answer_delta", "text": piece}),
            ).strip()
            self._record_usage()
        except Exception as exc:  # noqa: BLE001 - fall back to lead agent
            log.info("shallow answer failed; deep mode: %s", exc)
            return None
        if not text:
            return None
        return AgentAnswer(question=question, answer=sanitize_text(text), cited_node_ids=[r["node"].id for r in top], steps=1)

    def _degraded_answer(self, question: str, emit: Callable) -> AgentAnswer | None:
        """Keep answering when the lead fails or its full context cannot fit.

        Answer from the seed evidence and the compiled reports instead of throwing the
        whole run away. With no evidence at all there is nothing to degrade to, and the
        caller re-raises so the failure stays visible.
        """
        seeds, reports = self._seed_pieces
        if not (seeds or reports):
            return None
        answer = self._synthesize_reports(question, seeds, reports, set(self._seed_ids), emit)
        if answer is not None:
            return answer.model_copy(update={
                "answer": answer.answer + "\n\n※調査結果から直接回答を作成しました。"})
        body = (reports or seeds).strip()
        return AgentAnswer(question=question,
                           answer=f"（回答の統合が完了しなかったため、調査結果をそのまま返します）\n\n{body}",
                           cited_node_ids=list(self._seed_ids), steps=0)

    def _retain_raw_reports(self, answer: AgentAnswer) -> AgentAnswer:
        """Use raw explorer reports only to rescue an actually empty final answer."""
        if answer.answer.strip() or not self._seed_raw_reports:
            return answer
        reports = self._seed_pieces[1].strip() or _reports_text(self._seed_raw_reports)
        cited = dedupe([*answer.cited_node_ids,
                        *(page_id for report in self._seed_raw_reports
                          for page_id in report.get("cited", []))])
        footer = "\n\n引用:\n\n" + "\n\n".join(
            f"{page_id} : {self._cite(page_id)['title']}" for page_id in cited) if cited else ""
        return answer.model_copy(update={
            "answer": f"{reports}{footer}",
            "cited_node_ids": cited,
        })

    @staticmethod
    def _direct_seed_material(results: list[dict[str, Any]]) -> str:
        """Keep every small-path seed body available to the single compiler call."""
        parts = ["必読ページ原文（以下の全ページを確認して回答してください）:"]
        for result in results:
            page: WikiPage = result["node"]
            parts.append(
                f"\n--- {page.id or page.path} : {page.title} ---\n"
                f"path: {page.path}\n{page.body or page.summary or ''}"
            )
        return sanitize_text("\n".join(parts))

    def _synthesize_reports(self, question: str, seeds: str, reports: str,
                            allowed_ids: set[str], emit: Callable) -> AgentAnswer | None:
        """Answer from every compiled slice without sending an oversized model input."""
        # The explorers -> L1 -> L2 hierarchy exists so the final compiler does not
        # receive dozens of raw reports or every Jev candidate. When L2 exists it is
        # the complete final-compiler input; seeds are only the no-report fallback.
        material = reports.strip() or seeds.strip()
        if not material:
            return None
        payload_for = lambda chunk: json.dumps({"question": question, "reports": chunk}, ensure_ascii=False)
        output_tokens = self.settings.final_compiler_tokens
        try:
            chunks = self._llm_chunks(material, SYNTHESIS_PROMPT, payload_for)
        except ValueError:
            return None
        bodies: list[str] = []
        cited: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            payload = payload_for(chunk)
            try:
                text = self.llm.stream(
                    SYNTHESIS_PROMPT, payload,
                    lambda piece: emit({"type": "answer_delta", "text": piece}),
                    max_tokens=output_tokens,
                ).strip()
                self._record_usage()
                if _llm_output_capped(self.llm, output_tokens):
                    text = ""
            except Exception as exc:  # noqa: BLE001 - keep the complete compiled slice
                log.info("report synthesis slice %s failed: %s", index, exc)
                text = ""
            body, marker, citations = text.partition("引用:")
            matched = []
            if marker:
                for line in citations.splitlines():
                    match = re.match(r"^\s*(?:[-*]\s*)?([0-9a-fA-F]{24})\s*:\s*.+?\s*$", line)
                    if match and match.group(1) in allowed_ids:
                        matched.append(match.group(1))
            # Citation formatting is not a validity test for the answer body. Falling
            # back to `chunk` here used to expose the complete internal seed prompt.
            bodies.append((body.strip() if text else chunk.strip()))
            cited.extend(matched)
        report_ids = dedupe(re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{24}(?![0-9a-fA-F])", reports))
        cited = dedupe(cited) or [page_id for page_id in report_ids if page_id in allowed_ids]
        cited = cited or [page_id for page_id in self._seed_ids if page_id in allowed_ids]
        if not cited:
            body_text = "\n\n".join(bodies).strip()
            if body_text:
                return AgentAnswer(question=question, answer=sanitize_text(body_text),
                                   cited_node_ids=[], steps=1)
            return None
        canonical = "\n\n".join(f"{page_id} : {self._cite(page_id)['title']}" for page_id in cited)
        body_text = "\n\n".join(bodies)
        answer = f"{body_text}\n\n引用:\n\n{canonical}"
        return AgentAnswer(question=question, answer=sanitize_text(answer), cited_node_ids=cited, steps=1)

    def _run_lead(self, question: str, emit: Callable, stop_event: Event | None) -> AgentAnswer:
        ctx = _LeadContext(self, question, emit, stop_event)
        content = _seeded(self.settings, question, self._seed_context, self._seed_ids)
        if (self._seed_raw_reports
                and self._llm_tokens(MAIN_AGENT_SYSTEM_PROMPT + content) > self._llm_input_budget()):
            emit({"type": "lead_failed", "reason": "compiled reports exceed the lead input budget"})
            return self._degraded_answer(question, emit)
        agent = _compile_agent(self.settings, _lead_tools(ctx), MAIN_AGENT_SYSTEM_PROMPT, stop_event)
        emit({"type": "route", "mode": "deep", "reason": "lead agent started"})
        state = agent.invoke(
            {"messages": [{"role": "user", "content": sanitize_text(content)}]},
            config={
                "recursion_limit": max(50, self.settings.agent_max_steps * 2 + 4),
                "max_concurrency": self.settings.agent_tool_concurrency,
                "callbacks": [self._usage_cb],
            },
        )
        self.timer.count("agent_messages", _count_steps(state))
        finished = ctx.finished
        state_text = _last_text(state).strip()
        if not finished.get("answer") and self._seed_raw_reports:
            if state_text and state_text != STEP_LIMIT_TEXT:
                # Some OpenAI-compatible servers return a complete final message
                # instead of the requested finish tool call. That is still a usable
                # answer and must not discard the completed explorer/compiler work.
                finished["answer"] = state_text
            else:
                emit({"type": "lead_failed", "reason": "lead returned no answer"})
                degraded = self._degraded_answer(question, emit)
                if degraded is not None:
                    return degraded
        answer_text = finished.get("answer") or state_text
        cited = dedupe([*finished.get("cited_node_ids", []), *ctx.evidence])
        if not cited and self._seed_ids:
            # The lead may answer directly from the pre-explored Jev reports without
            # calling explore or passing cited_node_ids to finish. Keep those source
            # IDs so verification does not mistake a missing citation for no evidence.
            cited = self._seed_ids[:]
            print(f"[growi-search] Lead omitted citations; verifying against {len(cited)} seed/report pages", flush=True)
        return AgentAnswer(question=question, answer=sanitize_text(answer_text), cited_node_ids=cited, steps=_count_steps(state))

    def _run_subagents(self, raw_ids: list[Any], question: str, evidence: list[str], emit: Callable, stop_event: Event | None) -> str:
        starts = self._distinct_starts(raw_ids)
        if not starts:
            return "no starting pages found under the remaining budget. Retry, or answer from what you already have."
        emit({"type": "subagents_spawned", "starts": [node_ref(self._memo_page(s)) for s in starts]})
        reports: list[dict | None] = [None] * len(starts)
        assignments = [(s, [o for o in starts if o != s]) for s in starts]

        emit_guard = Lock()

        def safe_emit(event: dict[str, Any]) -> None:
            with emit_guard:
                emit(event)

        max_workers = min(len(assignments), max(1, self.settings.subagent_concurrency))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="subagent") as executor:
            futures = {}
            for pos, (start, siblings) in enumerate(assignments):
                futures[executor.submit(self._run_single_subagent, start, siblings, question, pos + 1, safe_emit, stop_event)] = pos
            for future in as_completed(futures):
                pos = futures[future]
                try:
                    reports[pos] = future.result()
                except AgentStopped:
                    for pending in futures:
                        pending.cancel()
                    raise
                except Exception as exc:  # noqa: BLE001 - partial failure is OK
                    log.info("subagent %s failed: %s", pos, exc)
                    reports[pos] = {"start": assignments[pos][0], "answer": f"(サブエージェント失敗: {exc})", "cited": []}

        final = [report for report in reports if report]
        for report in final:
            evidence.extend(report.get("cited", []))
        return _reports_text(final)

    def _distinct_starts(self, raw_ids: list[Any]) -> list[str]:
        resolved: list[str] = []
        seen: set[str] = set()
        for raw in raw_ids or []:
            page = self._fetch_ref(clean_ref(str(raw)))
            if page is None:
                continue
            if page.id not in seen:
                seen.add(page.id)
                resolved.append(page.id)
            if len(resolved) >= self.settings.subagent_count:
                break
        return resolved

    def _run_single_subagent(self, start: str, siblings: list[str], question: str, index: int, emit: Callable, stop_event: Event | None) -> dict:
        run = Subrun(start_id=start, index=index)
        start_page = self._fetch_page(page_id=start)
        if start_page:
            emit({"type": "subagent_start", "agent": index, "node": node_ref(start_page)})
        prompt = (
            f"質問: {question}\n\n"
            f"担当の開始ページID: {start}\n"
            f"他のエージェントの領域（探索しないこと）: {', '.join(siblings) or '（なし）'}\n\n"
            "まず開始ページを読み、follow_link でリンクをたどり、担当領域のページ数件を読んで、この領域がその質問について何を述べているかを報告してください。"
        )
        return _run_subagent(self, run, question, prompt, emit, stop_event)


    def _run_seed_groups(self, groups: list[list[dict[str, Any]]], confirmed: list[dict[str, Any]], question: str,
                         emit: Callable, stop_event: Event | None,
                         levelwise: bool | None = None) -> tuple[str, list[str]]:
        """Explore every Jev seed group in parallel with disjoint seed sets.

        Returns the merged report text plus every page id the groups cited, so the
        sweep outcome (not a fresh narrow search) is what the lead answers from.
        """
        emit({"type": "subagents_spawned", "starts": [node_ref(group[0]["node"]) for group in groups]})
        owned = [_seed_refs(group) for group in groups]
        everyone = set().union(*owned)
        # Seeds the sweep already read but that fell outside their document's
        # first slice: still free to read (cache hit), so the agent is told about
        # them instead of the answer silently losing everything past slice one.
        kept = {id(result) for group in groups for result in group}
        reports: list[dict | None] = [None] * len(groups)
        emit_guard = Lock()

        def safe_emit(event: dict[str, Any]) -> None:
            with emit_guard:
                emit(event)

        max_workers = min(len(groups), max(1, self.settings.subagent_concurrency))
        # The adaptive route explicitly selects the compiler depth from the
        # number of accepted seeds. Keep the old group-count fallback for other
        # callers of this helper.
        levelwise = len(groups) >= 16 if levelwise is None else levelwise
        by_doc: dict[str, list[int]] = {}
        for pos, group in enumerate(groups):
            by_doc.setdefault(group[0].get("document") or "", []).append(pos)
        batches: list[list[int]] = []
        if levelwise:
            # L1 batches are fixed up front: same-document groups fold together, at
            # most REPORT_WINDOW per batch. Each batch is submitted to its own
            # compiler pool as soon as all of its explorers finish.
            for positions in by_doc.values():
                batches.extend(positions[i:i + REPORT_WINDOW] for i in range(0, len(positions), REPORT_WINDOW))
        l1: list[str | None] = [None] * len(batches)
        l1_futures: dict[Any, int] = {}
        compiler_executor = (
            ThreadPoolExecutor(
                max_workers=min(len(batches), max(1, self.settings.compiler_concurrency)),
                thread_name_prefix="report-compiler",
            )
            if batches else None
        )

        try:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="seed-group") as executor:
                futures = {}
                for pos, group in enumerate(groups):
                    blocked = everyone - owned[pos]  # another group owns those pages
                    document = group[0].get("document") or ""
                    extra = [result["node"].id for result in confirmed
                             if (result.get("document") or "") == document and id(result) not in kept]
                    futures[executor.submit(self._run_group_subagent, pos + 1, group, blocked, extra,
                                            question, safe_emit, stop_event)] = pos
                for future in as_completed(futures):
                    pos = futures[future]
                    try:
                        reports[pos] = future.result()
                    except AgentStopped:
                        for pending in futures:
                            pending.cancel()
                        raise
                    except Exception as exc:  # noqa: BLE001 - partial failure is OK
                        log.info("seed group %s failed: %s", pos + 1, exc)
                        reports[pos] = {"start": groups[pos][0]["node"].id,
                                        "answer": f"(グループ調査失敗: {exc})", "cited": []}
                    if not levelwise:
                        continue
                    for b, batch in enumerate(batches):
                        if l1[b] is not None or not all(reports[i] for i in batch):
                            continue
                        if len(batch) == 1:
                            l1[b] = _reports_text([reports[batch[0]]])
                        elif compiler_executor is not None:
                            # The compiler pool is independent from the explorer
                            # pool, so a slow fold cannot hold up other explorers or
                            # prevent a newly completed batch from being folded.
                            l1[b] = ""  # submitted sentinel
                            l1_futures[compiler_executor.submit(
                                self._fold_reports,
                                question, "", [reports[i] for i in batch], safe_emit,
                                self.settings.report_fold_tokens,
                            )] = b

            if compiler_executor is not None:
                for future in as_completed(l1_futures):
                    b = l1_futures[future]
                    try:
                        l1[b] = future.result()
                    except Exception as exc:  # noqa: BLE001 - preserve the exact batch on fold failure
                        log.info("level-1 report compiler %s failed: %s", b + 1, exc)
                        l1[b] = _reports_text([reports[i] for i in batches[b] if reports[i]])
        finally:
            if compiler_executor is not None:
                compiler_executor.shutdown(wait=True, cancel_futures=True)
        final = [report for report in reports if report]
        self._seed_raw_reports = [dict(report) for report in final]
        cited = dedupe([cited_id for report in final for cited_id in report.get("cited", [])])
        if not levelwise:
            return _reports_text(final), cited
        parts = [text for text in l1 if text]
        if len(parts) == 1:
            return parts[0] or _reports_text(final), cited
        # L2 receives every completed L1 batch. Its larger budget keeps the merged
        # report intact before the separate final answer compiler runs.
        merged = self._fold_reports(question, "", [
            {"start": f"中間報告バッチ{b + 1}", "answer": text, "cited": []}
            for b, text in enumerate(l1) if text], safe_emit,
            self.settings.final_compiler_tokens) if parts else ""
        return (merged or _reports_text(final)), cited

    def _fold_reports(self, question: str, draft: str, reports: list[dict[str, Any]],
                      emit: Callable, max_output_tokens: int | None = None) -> str:
        """Fold reports into one merged report: one L1 batch, or the L2 final pass.

        Every input character reaches one compiler call. Failed, empty, or capped
        calls keep their exact input slice so a compiler response can never discard
        source details merely because generation ended early.
        """
        raw = _reports_text(reports)
        combined = "\n\n".join(part for part in (draft, raw) if part)
        if self.llm is None or not self.settings.llm_ready:
            return combined
        payload_for = lambda chunk: json.dumps(
            {"質問": question, "これまでの統合報告": "", "新規報告": chunk}, ensure_ascii=False)
        output_tokens = max_output_tokens or self.settings.report_fold_tokens
        try:
            chunks = self._llm_chunks(combined, REPORT_FOLD_PROMPT, payload_for)
        except ValueError:
            return combined
        folded: list[str] = []
        for chunk in chunks:
            payload = payload_for(chunk)
            try:
                part = sanitize_text(
                    self.llm.complete(REPORT_FOLD_PROMPT, payload, max_tokens=output_tokens)
                ).strip()
                self._record_usage()
                if _llm_output_capped(self.llm, output_tokens):
                    part = ""  # A cut-off summary cannot replace its complete input.
            except Exception as exc:  # noqa: BLE001 - never lose reports to a failed fold
                log.info("report fold failed; keeping the raw chunk: %s", exc)
                part = ""
            folded.append(part or chunk)
        merged = "\n\n".join(folded)
        print(f"[growi-search] Report fold: {len(reports)} reports, {len(chunks)} calls, "
              f"{len(combined)} input chars, {len(merged)} output chars", flush=True)
        emit({"type": "reports_folded", "reports": len(reports), "chars": len(merged)})
        return merged

    def _run_group_subagent(self, index: int, group: list[dict[str, Any]], offlimits: set[str],
                            extra: list[str],
                            question: str, emit: Callable, stop_event: Event | None) -> dict:
        """One subagent, one document slice: it sees its own seeds and nothing else."""
        document = group[0].get("document") or ""
        seeds = "\n\n".join(format_lead_candidate(result) for result in group)
        backlog = (
            f"\n同じドキュメント内の追加候補ページ {len(extra)} 件（スイープが既に読み取り済みで、取得はキャッシュ済み・"
            f"コストゼロです。必要なら read で本文を確認し、列挙されている項目を漏らさず拾ってください）:\n"
            f"{', '.join(extra)}\n"
            if extra else ""
        )
        run = Subrun(start_id=group[0]["node"].id, index=index, offlimits=offlimits)
        emit({"type": "subagent_start", "agent": index, "node": node_ref(group[0]["node"]),
              "document": document})
        prompt = (
            f"質問: {question}\n\n"
            f"担当ドキュメント: {document or '（不明）'}\n"
            f"担当シードページ（{len(group)}件 / このドキュメントのみが担当範囲です）:\n{seeds}\n\n"
            f"{backlog}"
            "他のグループが担当するページは読み取りがブロックされています（重複調査を防ぐため）。"
            "担当シードを読み直すのではなく、シードの発リンクや子ページなど、まだ誰も読んでいないパスを追い、"
            "このドキュメントが検索意図について何を述べているかを報告してください。"
            "質問の対象・範囲に合わない一般論や、名前が出るだけの記述は回答に数えないでください。"
            "一覧や複数項目を求める場合は、各項目が対象ライブラリに属する根拠を確認してください。"
            "特定の項目を求める場合は、指定された対象と必要な詳細を定義する本文だけを根拠にしてください。"
            "質問が一覧・列挙を求める場合、報告には見つけた項目を省略せず全部書いてください。"
        )
        return _run_subagent(self, run, question, prompt, emit, stop_event)


# --- lead/subagent tool bindings (mirrors neo) ------------------------------


@dataclass
class _LeadContext:
    session: ResearchSession
    question: str
    emit: Callable
    stop_event: Event | None
    evidence: list[str] = field(default_factory=list)
    finished: dict[str, Any] = field(default_factory=lambda: {"answer": "", "cited_node_ids": []})


class LeadSearchArgs(BaseModel):
    text: str = Field(..., description="検索クエリテキスト")


class LeadExploreArgs(BaseModel):
    node_ids: list[str] = Field(..., description="探索する正確なページID。検索結果のIDを使用。")


class LeadFinishArgs(BaseModel):
    answer: str = Field(..., description="最終回答")
    cited_node_ids: list[str] | None = Field(default=None, description="回答を裏付けるページID")


class ReadArgs(BaseModel):
    node_id: str = Field(..., description="ページID")
    heading: str | None = Field(default=None, description="読み出したい節のタイトル（省略可）")


class FindArgs(BaseModel):
    description: str = Field(..., description="探したいページの内容")


class FollowLinkArgs(BaseModel):
    node_id: str = Field(..., description="発リンクを取得するページID")
    direction: str = Field(default="outgoing", description="'outgoing' のみ対応")


class FinishArgs(BaseModel):
    answer: str = Field(..., description="調査結果レポート")
    cited_node_ids: list[str] | None = Field(default=None, description="読んだページID")


def _lead_search(ctx: _LeadContext, text: str) -> str:
    _check_stop(ctx.stop_event)
    session = ctx.session
    query = sanitize_text(str(text or "")).strip()
    ctx.emit({"type": "search", "phase": "main", "query": query})
    try:
        results = session.search_with_evidence(query, session.settings.rerank_top_k)
    except Exception as exc:  # noqa: BLE001
        log.info("lead search failed: %s", exc)
        results = []
    if not results and query and query != ctx.question.strip():
        ctx.emit({"type": "search", "phase": "main", "query": ctx.question})
        try:
            results = session.search_with_evidence(ctx.question, session.settings.rerank_top_k)
        except Exception as exc:  # noqa: BLE001
            log.info("lead question retry failed: %s", exc)
    return format_search_results(results)


def _lead_explore(ctx: _LeadContext, node_ids: list[str]) -> str:
    _check_stop(ctx.stop_event)
    cleaned = _clean_ids(node_ids)
    if not cleaned:
        return "有効なIDがありません。最初に検索してください。"
    # TEMPORARILY DISABLED: Elasticsearch/lead-driven subagents.
    # Keep the implementation below intact so this can be restored after the
    # Jev-scoped subagent path is validated.
    return "Elasticsearchから起動するサブエージェントは一時的に無効です。検索結果だけで finish してください。"
    # return ctx.session._run_subagents(cleaned, ctx.question, ctx.evidence, ctx.emit, ctx.stop_event)


def _lead_finish(ctx: _LeadContext, answer: str, cited_node_ids: list[str] | None = None) -> str:
    _check_stop(ctx.stop_event)
    ctx.finished["answer"] = sanitize_text(str(answer or "")).strip()
    ctx.finished["cited_node_ids"] = _clean_ids(cited_node_ids)
    return sanitize_text(json.dumps(ctx.finished, ensure_ascii=False))


def _lead_tools(ctx: _LeadContext) -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            lambda text: _lead_search(ctx, text), name="search",
            description="GROWI をキーワード検索して候補ページを返します。", args_schema=LeadSearchArgs
        ),
        # TEMPORARILY DISABLED: Elasticsearch-driven explorer creation.
        # StructuredTool.from_function(
        #     lambda node_ids: _lead_explore(ctx, node_ids), name="explore",
        #     description="指定ページID群をサブエージェントで並列深掘りします。", args_schema=LeadExploreArgs
        # ),
        StructuredTool.from_function(
            lambda answer, cited_node_ids=None: _lead_finish(ctx, answer, cited_node_ids),
            name="finish",
            description="最終回答と引用ページIDを出力して終了します。",
            args_schema=LeadFinishArgs,
            return_direct=True,
        ),
    ]


@dataclass
class _SubContext:
    session: ResearchSession
    run: Subrun
    emit: Callable
    stop_event: Event | None
    finished: dict[str, Any] = field(default_factory=dict)


def _sub_search(ctx: _SubContext, text: str) -> str:
    _check_stop(ctx.stop_event)
    session, run = ctx.session, ctx.run
    query = sanitize_text(str(text or "")).strip()
    ctx.emit({"type": "search", "phase": "sub", "agent": run.index, "query": query})
    try:
        nodes = session.search(query, session.settings.rerank_top_k)
    except Exception as exc:  # noqa: BLE001
        log.info("sub search failed: %s", exc)
        nodes = []
    run.visited.extend(n.id for n in nodes)
    if nodes:
        run.empty_streak = 0
        return sanitize_text(
            "\n".join(
                f"- node_id: `{n.id}`\n  title: {sanitize_text(str(n.title))}\n  path: {sanitize_text(str(n.path))}\n  summary: {sanitize_text(str(n.summary))}\n  next_action: read(node_id='{n.id}') if relevant"
                for n in nodes
            )
        )
    run.empty_streak += 1
    if run.empty_streak >= session.settings.agent_patience:
        return "検索結果なしが連続しています。finish を呼び、これまで読んだ根拠で最も良い回答を提出してください。"
    return "no pages found"


def _sub_read(ctx: _SubContext, node_id: str, heading: str | None = None) -> Any:
    _check_stop(ctx.stop_event)
    session, run = ctx.session, ctx.run
    cleaned = clean_ref(sanitize_text(str(node_id or "")))
    if not cleaned:
        return format_read(None, "", node_id, cleaned)

    if cleaned in run.offlimits:
        return sanitize_text(
            "そのページは他の担当グループのシードです（重複調査を防ぐため読み取り不可）。"
            "自担当ドキュメント内のまだ読んでいないパスをたどってください。"
        )
    owner = session._cascade_owner(cleaned) if run.cascade else None
    if owner is not None and owner != run.index:
        return sanitize_text("このページは別の担当 bin に割り当てられています。自分の証拠だけを使って finish してください。")
    # A long page is first shown cut short; one of its sections is new text, not a re-read,
    # so only an exact repeat is refused (refusing sections left agents looping to the step limit).
    section = (heading or "").strip().casefold()
    again = cleaned in run.read_ids
    if again and (not section or (cleaned, section) in run.read_sections):
        return sanitize_text(f"{cleaned} は既に読みました。別のページを読むか follow_link/finish を呼んでください。")
    if not again and len(run.read_ids) >= session.settings.subagent_max_reads:
        return sanitize_text(
            f"読み取り上限到達 ({len(run.read_ids)}/{session.settings.subagent_max_reads})。finish で回答してください。"
        )
    page = session._fetch_ref(cleaned)
    if page is not None:
        if section:
            run.read_sections.add((cleaned, section))
        run.last_read = page.id
        run.empty_streak = 0
        if not again:
            run.read_ids.add(page.id)
            run.visited.append(page.id)
            ctx.emit({"type": "read", "agent": run.index, "node": node_ref(page)})
    if page is not None and run.cascade:
        sections = session._cascade_sections.get((page.id, page.revision_id, run.cascade_question))
        if sections:
            selected = [section for section in sections
                        if not heading or heading.casefold() in str(getattr(section, "heading", "")).casefold()]
            text = "\n\n".join(f"## {section.heading}\n{section.body}" for section in selected)
        else:
            text = session._render_body(page, heading)
    else:
        text = session._render_body(page, heading) if page is not None else format_read(None, "", node_id, cleaned)
    return sanitize_text(text)


def _sub_follow(ctx: _SubContext, node_id: str, direction: str = "outgoing") -> str:
    _check_stop(ctx.stop_event)
    session, run = ctx.session, ctx.run
    cleaned = clean_ref(sanitize_text(str(node_id or "")))
    try:
        links = session.follow_link(cleaned, direction="outgoing", emit=ctx.emit, agent=run.index)
    except ValueError as exc:
        return f"error: {exc}"
    if links:
        run.empty_streak = 0
        run.visited.extend(l.target_node_id for l in links if l.target_node_id)
    if not links:
        return "このページに発リンクはありません"
    return sanitize_text(
        "\n".join(f"- [{sanitize_text(l.label)}] {l.target_node_id} | {sanitize_text(l.summary)}" for l in links)
    )


def _sub_find(ctx: _SubContext, description: str) -> str:
    _check_stop(ctx.stop_event)
    walker = ctx.session.walker
    if walker is None:
        return "find は利用できません"
    query = sanitize_text(str(description or "")).strip()
    try:
        hits = walker.find(query, start_ref=ctx.run.last_read or ctx.run.start_id,
                           stop_event=ctx.stop_event, emit=ctx.emit, agent=ctx.run.index)
    except Exception as exc:  # noqa: BLE001 - search helper must not abort the agent
        log.info("sub find failed: %s", exc)
        hits = []
    if not hits:
        return "該当するページは見つかりませんでした。別の言い方で find するか、これまでの根拠で finish してください。"
    return sanitize_text("\n".join(
        f"- node_id: `{hit.page_id}`  title: {hit.title}  path: {hit.path}  p={hit.p:.2f}  summary: {hit.summary}  next_action: read(node_id='{hit.page_id}')"
        for hit in hits
    ))


def _sub_finish(ctx: _SubContext, answer: str, cited_node_ids: list[str] | None = None) -> str:
    _check_stop(ctx.stop_event)
    run = ctx.run
    # TEMPORARILY DISABLED: minimum-read enforcement. Keep this block for
    # restoration after the Jev-scoped explorer path is validated.
    # minimum = 0 if run.cascade else ctx.session.settings.subagent_min_reads
    # if len(run.read_ids) < minimum and not ctx.finished.get("pushed_back"):
    #     ctx.finished["pushed_back"] = True
    #     return (
    #         f"読んだページは {len(run.read_ids)} 件です。少なくとも "
    #         f"{minimum} 件読んでから finish してください。"
    #     )
    ctx.finished["answer"] = sanitize_text(str(answer or "")).strip()
    ctx.finished["cited_node_ids"] = _clean_ids(cited_node_ids)
    return "finished; do not call tools anymore"


def _sub_tools(ctx: _SubContext) -> list[StructuredTool]:
    tools = [
        StructuredTool.from_function(lambda text: _sub_search(ctx, text), name="search", description="GROWI を検索します。", args_schema=LeadSearchArgs),
        StructuredTool.from_function(lambda node_id, heading=None: _sub_read(ctx, node_id, heading), name="read", description="ページ本文（必要なら特定節）を読み出します。", args_schema=ReadArgs),
        StructuredTool.from_function(lambda node_id, direction="outgoing": _sub_follow(ctx, node_id, direction), name="follow_link", description="ページから発リンク（outgoing のみ）を取得します。", args_schema=FollowLinkArgs),
        StructuredTool.from_function(lambda answer, cited_node_ids=None: _sub_finish(ctx, answer, cited_node_ids), name="finish", description="担当領域の調査結果を報告して終了します。", args_schema=FinishArgs),
    ]
    if ctx.session.walker is not None:
        tools.insert(-1, StructuredTool.from_function(lambda description: _sub_find(ctx, description),
            name="find", description="周辺ページを内容から探します。", args_schema=FindArgs))
    return tools


def _run_subagent(session: ResearchSession, run: Subrun, question: str, prompt: str, emit: Callable, stop_event: Event | None) -> dict:
    ctx = _SubContext(session=session, run=run, emit=emit, stop_event=stop_event)
    agent = _compile_agent(session.settings, _sub_tools(ctx),
                           PACKED_SUBAGENT_PROMPT if run.cascade else SUBAGENT_SYSTEM_PROMPT, stop_event,
                           max_tokens=session.settings.subagent_report_tokens)
    state = agent.invoke(
        {"messages": [{"role": "user", "content": sanitize_text(prompt)}]},
        config={
            "recursion_limit": max(4, session.settings.cascade_subagent_steps * 2 + 2) if run.cascade
            else max(30, session.settings.subagent_max_steps * 2 + 6),
            "max_concurrency": session.settings.agent_tool_concurrency,
            "callbacks": [session._usage_cb],
        },
    )
    session.timer.count("agent_messages", _count_steps(state))
    answer = sanitize_text(ctx.finished.get("answer") or "").strip() or _last_text(state)
    if answer == STEP_LIMIT_TEXT:  # out of steps: that text is not findings
        answer = ""
    cited = _clean_ids(ctx.finished.get("cited_node_ids", [])) or dedupe(run.visited)
    if not run.cascade:
        emit({"type": "subagent_done", "agent": run.index, "cited": cited})
    return {"start": run.start_id, "answer": answer or "(このサブエージェントは調査を完了できず、報告はありません)",
            "cited": cited, "finished": bool(ctx.finished.get("answer"))}


# --- override sanitization / SSRF guard ------------------------------------


_OVERRIDE_MAX = {
    "subagent_count": 6, "subagent_min_reads": 10, "subagent_max_reads": 20,
    "subagent_max_steps": 40, "agent_max_steps": 60, "agent_patience": 30, "rerank_top_k": 40,
    "search_candidates": 50, "shallow_page_reads": 3, "index_map_top_k": 40,
}
_OVERRIDE_KEYS = {"chat_base_url", "chat_api_key", "chat_model", "chat_temperature", "subagent_concurrency", *_OVERRIDE_MAX}


def _sanitize_overrides(overrides: dict[str, Any], settings: Settings) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, raw in (overrides or {}).items():
        if raw is None or raw == "":
            continue
        if key not in _OVERRIDE_KEYS:
            log.info("ignoring unknown override key: %s", key)
            continue
        try:
            if key == "chat_temperature":
                clean[key] = max(0.0, min(2.0, float(raw)))
            elif key == "subagent_concurrency":
                clean[key] = max(1, min(int(float(raw)), settings.llm_max_concurrency))
            elif key in _OVERRIDE_MAX:
                cap = 32 if key == "subagent_count" and settings.jev_mode == "cascade" else _OVERRIDE_MAX[key]
                clean[key] = max(1, min(int(float(raw)), cap))
            else:
                text = str(raw).strip()
                if not text:
                    continue
                clean[key] = text
        except (TypeError, ValueError):
            continue

    if "chat_base_url" in clean:
        _validate_llm_url(clean["chat_base_url"], settings)
    return clean


_BLOCKED_NETS = ("127.", "169.254.", "100.100.", "metadata", "localhost")


def _validate_llm_url(url: str, settings: Settings) -> None:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = settings.allowed_hosts
    if host.startswith(_BLOCKED_NETS) or host.endswith(".local"):
        raise ValueError("LLM endpoint host is not permitted")
    if not allowed:
        raise ValueError("LLM base_url is not configured on the server.")
    if host not in allowed and not any(host.endswith("." + h.lstrip(".")) for h in allowed):
        raise ValueError("LLM endpoint host is not permitted")


def _log_usage(question: str, answer: str, steps: int, usages: list[dict], settings: Settings) -> None:
    path = settings.usage_log_path
    if not path:
        return
    totals = {"input_tokens": 0, "output_tokens": 0}
    for usage in usages:
        totals["input_tokens"] += usage.get("input_tokens") or 0
        totals["output_tokens"] += usage.get("output_tokens") or 0
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"query": question, "steps": steps, **totals}, ensure_ascii=False) + "\n"
            )
    except OSError as exc:
        log.info("usage log failed: %s", exc)


def _seeded(settings: Settings, question: str, seed_context: str, seed_ids: list[str]) -> str:
    if not seed_context.strip():
        return question
    return (
        f"{question}\n\n初期候補ノード:\n{seed_context}\n\n候補ID: {', '.join(seed_ids)}\n"
    )


# --- concurrent research service --------------------------------------------


class Researcher:
    def __init__(self, client: GrowiSearchClient, settings: Settings, reranker: Reranker | None, embedder: Embedder | None = None) -> None:
        self.settings = settings
        self.mirror = Mirror(client, settings) if getattr(settings, "mirror_dir", "") else None
        max_bytes = None if self.mirror else int(getattr(settings, "page_cache_mb", 64)) * 1024 * 1024
        self.cache = PageCache(settings.page_cache_ttl, settings.page_cache_max, max_bytes=max_bytes)
        self.reranker = reranker
        self.embedder = embedder
        self.index_map = IndexMap(client, settings, embedder, reranker, mirror=self.mirror)
        self.jev = build_jev(settings)
        self.client = client
        self.read_sem = asyncio.Semaphore(settings.service_max_reads)
        self.agent_sem = asyncio.Semaphore(settings.service_max_agents)

    def session(self, overrides: dict | None = None) -> ResearchSession:
        session = ResearchSession(self.client, self.settings, self.cache, self.reranker,
                                  index_map=self.index_map, jev=self.jev, mirror=self.mirror)
        session.apply_overrides(overrides)
        return session

    def validate_overrides(self, overrides: dict | None) -> None:
        """Raise ValueError (-> HTTP 400) for unknown keys or disallowed hosts."""
        _sanitize_overrides(overrides or {}, self.settings)

    async def node_view(self, page_id: str) -> dict | None:
        def work(session: ResearchSession) -> dict | None:
            page = session.page_for_view(page_id)
            if page is None:
                return None
            return {**page.public_dict(), "links": [link.model_dump() for link in session.links_for(page)]}

        return await self._read(work)

    async def _read(self, fn: Callable[["ResearchSession"], Any]) -> Any:
        async with self.read_sem:
            session = self.session()
            return await asyncio.to_thread(lambda: fn(session))

    async def fast_search(self, query: str, limit: int) -> list[dict]:
        return await self._read(lambda s: s.fast_search(query, limit))

    async def read_page(self, page_id: str) -> WikiPage | None:
        return await self._read(lambda s: s.page_for_view(page_id))

    async def children(self, *, page_id: str | None = None, path: str | None = None) -> list[WikiPage]:
        return await self._read(lambda s: s.children(page_id=page_id, path=path))

    async def document_view(self, path: str) -> dict:
        st = self.settings

        def work(session: ResearchSession) -> dict:
            children = [c for c in session.children(path=path) if c.path.rstrip("/").rsplit("/", 1)[-1] != st.index_page_name]
            index_path = f"{path.rstrip('/')}/{st.index_page_name}"
            index = (self.mirror.get(path=index_path) if self.mirror and self.mirror.ready
                     else self.client.get_page(path=index_path))
            cards = md.parse_index(index.body) if index and md.is_index_page(index.body) else []
            by_ref = {c.target.strip("/"): c for c in cards}
            pages = []
            for child in sorted(children, key=lambda c: c.path):
                row = child.public_dict()
                card = by_ref.get(child.id) or by_ref.get(child.path.strip("/"))
                if card:
                    row.update({"summary": card.summary, "keywords": card.keywords, "cluster": card.chapter})
                row["source_url"] = session._source_url(child)
                pages.append(row)
            folder = WikiPage(id="", path=path)
            return {"path": path, "title": path.rstrip("/").rsplit("/", 1)[-1], "pages": pages, "source_url": session._source_url(folder)}

        return await self._read(work)

    async def ask(self, question, on_event=None, overrides=None, stop_event=None, context="", cited_node_ids=None) -> AgentAnswer:
        def work():
            session = self.session(overrides)
            return session.ask(question, on_event, stop_event, context, cited_node_ids)

        if on_event and self.agent_sem.locked():
            on_event({"type": "queued_for_agent"})
        async with self.agent_sem:
            return await asyncio.to_thread(work)
