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
from urllib.parse import quote
from collections import Counter, OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from queue import Queue
from threading import Event, Lock, Thread
from typing import Any, Callable, Iterator

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
    normalize_scores,
)
from growi_client import GrowiAPIError, GrowiSearchClient
from models import AgentAnswer, Evidence, WikiLink, WikiPage, make_link_id
from prompts import (
    FOLLOWUP_ANSWER_PROMPT,
    JEV_QUERY_REWRITE_PROMPT,
    JEV_TOC_SUMMARY_PROMPT,
    MAIN_AGENT_SYSTEM_PROMPT,
    ROUTER_PROMPT,
    SHALLOW_ANSWER_PROMPT,
    SUBAGENT_SYSTEM_PROMPT,
)

log = logging.getLogger("growi_search_researcher")

IMAGE_UNIT_RE = re.compile(r"<image-unit\b[^>]*>.*?</image-unit>", re.I | re.S)
IMAGE_DESC_RE = re.compile(
    r"<image-description\b[^>]*>(.*?)</image-description>", re.I | re.S
)
IMAGE_MEDIA_RE = re.compile(r"<image-media\b[^>]*>.*?</image-media>", re.I | re.S)
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


class PageCache:
    def __init__(self, ttl: int, max_items: int) -> None:
        self._ttl = ttl
        self._max = max_items
        self._data: OrderedDict[tuple[str, str], tuple[float, WikiPage]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: tuple[str, str]) -> WikiPage | None:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expires, page = item
            if time.monotonic() > expires:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return page

    def put(self, key: tuple[str, str], page: WikiPage) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, page.model_copy(deep=True))
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def size(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def _norm_entity(text: str) -> str:
    """NFKC + casefolded + whitespace-collapsed entity key."""
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


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


class IndexMap:
    """Cards from <root>/00-目次 and every <doc>/00-目次, cached in memory with a TTL.

    Nothing is written to disk. A refresh reads 1 + N index pages in parallel and
    embeds only cards whose text changed; while it runs, questions keep using the
    previous snapshot (stale-while-revalidate). Only the very first load blocks."""

    def __init__(self, client: GrowiSearchClient, settings: Settings, embedder: Embedder | None, reranker: Reranker | None) -> None:
        self.client, self.settings, self.embedder, self.reranker = client, settings, embedder, reranker
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
        is_id = bool(re.fullmatch(r"[0-9a-fA-F]{24}", ref))
        return WikiPage(id=ref if is_id else card.target, path="" if is_id else card.target,
                        title=card.title, summary=card.summary, document=card.document, cluster=card.chapter)

    def _read(self, target: str) -> WikiPage | None:
        ref = target.strip("/")
        try:
            if re.fullmatch(r"[0-9a-fA-F]{24}", ref):
                return self.client.get_page(page_id=ref)
            return self.client.get_page(path=target)
        except GrowiAPIError as exc:
            log.info("index page read failed (%s): %s", target, exc)
            return None

    def _build(self) -> _MapState:
        """Fetch + parse + embed into a fresh state. Touches no shared fields except the vector cache."""
        root = self.settings.growi_root_path.rstrip("/")
        page = self.client.get_page(path=f"{root}/{self.settings.index_page_name}")
        if page is None or not md.is_index_page(page.body):
            return _MapState()
        docs = md.parse_index(page.body)
        with ThreadPoolExecutor(max_workers=max(1, self.settings.growi_concurrency), thread_name_prefix="index-map") as pool:
            subs = list(pool.map(lambda doc: self._read(doc.target), docs))
        cards: list[md.IndexCard] = []
        entity_definers: dict[str, list[md.IndexCard]] = {}
        cards_by_document: dict[str, list[md.IndexCard]] = {}
        for doc, sub in zip(docs, subs):
            if sub is None or not md.is_index_page(sub.body):
                continue
            doc_cards: list[md.IndexCard] = []
            for card in md.parse_index(sub.body):
                card.document = doc.title
                cards.append(card)
                doc_cards.append(card)
                for entity in card.entities:
                    entity_definers.setdefault(_norm_entity(entity), []).append(card)
            cards_by_document[doc.title] = doc_cards
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
        return _MapState(docs=docs, cards=cards, vectors=vectors, grams=grams, df=df,
                         entity_definers=entity_definers, cards_by_document=cards_by_document)

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
            if time.monotonic() <= self._expires or self._refreshing:
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


@dataclass
class Subrun:
    start_id: str
    index: int
    visited: list[str] = field(default_factory=list)
    read_ids: set[str] = field(default_factory=set)
    empty_streak: int = 0
    offlimits: set[str] = field(default_factory=set)  # seeds another group owns: never read here


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


def _jev_chunks(text: str, max_tokens: int, overlap_tokens: int, prefix: str = "") -> list[str]:
    """Split a body into <=max_tokens chunks (prefix included) with overlap.

    Prefers paragraph boundaries; hard-splits a single oversized paragraph.
    Never returns an empty chunk. Page confidence = max over chunks.
    """
    body_budget = max(1, max_tokens - 512 - _jev_est_tokens(prefix))
    text = text or ""
    if _jev_est_tokens(text) <= body_budget:
        return [text]
    units: list[str] = []
    for para in re.split(r"\n[ \t]*\n", text):
        while _jev_est_tokens(para) > body_budget:
            per_char = _jev_est_tokens(para) / max(1, len(para))
            cut = max(1, int(body_budget / per_char))
            units.append(para[:cut])
            para = para[cut:]
        if para.strip():
            units.append(para)
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in units:
        unit_tokens = _jev_est_tokens(unit)
        if current and current_tokens + unit_tokens > body_budget:
            chunks.append("\n\n".join(current))
            overlap: list[str] = []
            overlap_tokens_seen = 0
            for prev in reversed(current):
                prev_tokens = _jev_est_tokens(prev)
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


def _model(settings: Settings) -> Any:
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.chat_model,
        base_url=_base_url(settings.chat_base_url),
        api_key=settings.chat_api_key or "local",
        temperature=settings.chat_temperature,
        timeout=300,
        max_retries=0,
        stream_usage=True,
    )


def _compile_agent(settings: Settings, tools: list[StructuredTool], prompt: str, stop_event: Event | None):
    safe_prompt = sanitize_text(prompt or "")

    def before_model(state: dict[str, Any]) -> dict[str, Any]:
        _check_stop(stop_event)
        messages = state.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        return {"llm_input_messages": sanitize_messages(messages)}

    return create_react_agent(
        _model(settings), tools=tools, prompt=safe_prompt, pre_model_hook=before_model, version="v2"
    )


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
    return sorted(groups, key=lambda group: -group[0]["score"])[:max(1, max_groups)]


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
    blocks.append("\n※各報告に列挙された項目・関数名・数値は省略せず、回答にすべて含めてください（一部の抜粋は不可）。")
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

    def __init__(self, client: GrowiSearchClient, settings: Settings, cache: PageCache, reranker: Reranker | None, index_map: IndexMap | None = None, jev: Any = None) -> None:
        self.client = client
        self.settings = settings
        self.cache = cache
        self.reranker = reranker
        self.index_map = index_map
        self.jev = jev
        self.llm = LlmClient(
            settings.chat_model,
            settings.chat_base_url,
            settings.chat_api_key,
            temperature=settings.chat_temperature,
        )
        self.budget = RunBudget(settings.max_page_fetches_per_run, settings.max_search_calls_per_run)
        # Request-local memoization for sharing across lead + subagents.
        self._page_memo: dict[str, WikiPage | None] = {}
        self._search_memo: dict[str, list[dict[str, Any]]] = {}
        self._links_memo: dict[str, list[WikiLink]] = {}
        self._usage_cb = UsageMetadataCallbackHandler()
        self._extra_usage: list[dict[str, Any]] = []
        self._lock = Lock()
        self._seed_context = ""
        self._seed_ids: list[str] = []

    def apply_overrides(self, overrides: dict[str, Any] | None) -> None:
        clean = _sanitize_overrides(overrides or {}, self.settings)
        if not clean:
            return
        self.settings = self.settings.model_copy(update=clean)
        if {"chat_base_url", "chat_api_key", "chat_model", "chat_temperature"} & clean.keys():
            self.llm = LlmClient(
                self.settings.chat_model,
                self.settings.chat_base_url,
                self.settings.chat_api_key,
                temperature=self.settings.chat_temperature,
            )

    # -- caches & budgets ----------------------------------------------------

    def _page_key(self, page_id: str = "", path: str = "", revision: str = "") -> str:
        return f"{page_id or path}|{revision}" if (page_id or path) else ""

    def _fetch_page(self, *, page_id: str | None = None, path: str | None = None, revision: str = "", sweep_budget: "JevSweepBudget | None" = None) -> WikiPage | None:
        key = self._page_key(page_id or "", path or "", revision)
        with self._lock:
            if key in self._page_memo:
                return self._page_memo[key]

        cached = self.cache.get(((page_id or path), revision))
        if cached is not None:
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
            page = self.client.get_page(page_id=page_id, path=path)
        except GrowiAPIError as exc:
            log.info("page fetch failed (%s): %s", page_id or path, exc)
            page = None
        if page is not None:
            self.cache.put(((page_id or path), revision), page)
        with self._lock:
            self._page_memo[key] = page
        return page

    def _fetch_ref(self, reference: str, sweep_budget: "JevSweepBudget | None" = None) -> WikiPage | None:
        return (
            self._fetch_page(path=reference, sweep_budget=sweep_budget)
            if reference.startswith("/")
            else self._fetch_page(page_id=reference, sweep_budget=sweep_budget)
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

    def page_for_view(self, page_id: str, emit: Callable | None = None) -> WikiPage | None:
        page = self._fetch_ref(page_id)
        if page:
            page.document = page.path
            page.summary = md.section_summary(page.body) or page.title
            page.source_url = self._source_url(page)
        return page

    def children(self, *, page_id: str | None = None, path: str | None = None) -> list[WikiPage]:
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
            "threshold": self.settings.jev_threshold if stage == "card" else self.settings.jev_seed_threshold,
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

    @staticmethod
    def _known_entities(state: "_MapState") -> list[str]:
        return sorted({e for card in state.cards for e in card.entities}, key=len, reverse=True)

    def _page_entities(self, state: "_MapState", page: WikiPage) -> list[str]:
        """Known entity names occurring in a no-index page (longest first)."""
        hay = _norm_entity(f"{page.title}\n{page.body}"[:40000])
        return [name for name in self._known_entities(state) if _norm_entity(name) in hay]

    def _jev_walk(self, root: WikiPage, budget: JevSweepBudget,
                  stop_event: Event | None,
                  skip_index_pages: bool = True) -> Iterator[WikiPage]:
        """Sweep-only recursive child walk; cycles stop on visited IDs/paths.

        Yields while it crawls so classification starts before enumeration ends.
        `skip_index_pages` keeps 00-目次 pages out of classification — the rewrite
        crawl turns it off because those pages are exactly what it is looking for.
        """
        seen: set[str] = set()
        queue: deque[WikiPage] = deque([root])
        while queue:
            _check_stop(stop_event)
            node = queue.popleft()
            key = (node.id or node.path).strip("/")
            if not key or key in seen:
                continue
            seen.add(key)
            yield node
            if not budget.try_list():
                continue
            try:
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
                queue.append(child)

    def _jev_documents(self, state: "_MapState", budget: JevSweepBudget,
                       stop_event: Event | None) -> list[dict]:
        """Every visible document: valid-index docs first, then no-index roots.

        A document whose index fetch failed is never silently omitted; it falls
        back to recursive page enumeration.
        """
        docs: list[dict] = []
        keys: set[str] = set()
        for doc in state.docs:
            page = self._fetch_ref(doc.target, sweep_budget=budget)
            cards = state.cards_by_document.get(doc.title, [])
            root_path = page.path.rsplit("/", 1)[0] if page is not None and page.path else ""
            if page is not None and md.is_index_page(page.body) and cards and root_path:
                keys.add(root_path.rstrip("/"))
                docs.append({"key": root_path, "indexed": True, "title": doc.title,
                             "cards": cards, "root": page})
            else:
                root = None
                if page is not None and page.path:
                    root_path = page.path
                    if root_path.rstrip("/").rsplit("/", 1)[-1] == self.settings.index_page_name:
                        root_path = root_path.rsplit("/", 1)[0]  # document dir, not the index page
                    root = WikiPage(id="", path=root_path)
                elif doc.target.startswith("/"):
                    root = WikiPage(id="", path=doc.target)
                key = ((root.path if root is not None else "") or doc.target).rstrip("/")
                if key and key not in keys:
                    keys.add(key)
                    docs.append({"key": key, "indexed": False, "title": doc.title,
                                 "cards": [], "root": root or WikiPage(id="", path="")})
        # No-index fallback: every first-level child of the visible root that is
        # not already an indexed document is its own document root.
        if budget.try_list():
            _check_stop(stop_event)
            try:
                children = self.client.list_children(path=self.settings.growi_root_path)
            except GrowiAPIError as exc:
                log.info("jev root listing failed: %s", exc)
                children = []
            for child in children:
                key = child.path.rstrip("/")
                if not key or key in keys:
                    continue
                keys.add(key)
                docs.append({"key": key, "indexed": False, "title": child.title,
                             "cards": [], "root": child})
        return docs

    def _jev_score_cards(self, query: str, doc: dict, emit: Callable,
                         stop_event: Event | None, work: "_SweepWork") -> list[md.IndexCard]:
        """Batched card-stage scoring; returns cards above jev_threshold."""
        st = self.settings
        candidates: list[md.IndexCard] = []
        cards = doc["cards"]
        batches: list[list[md.IndexCard]] = []
        batch: list[md.IndexCard] = []
        for card in cards:
            trial = [*batch, card]
            trial_state = self._jev_state(doc["key"], cards=trial)
            trial_questions = [
                jev_question_text(query, subject=f"{item.title} ({item.target})")
                for item in trial
            ]
            size = _jev_est_tokens(
                json.dumps(trial_state, ensure_ascii=False, default=str) + "\n".join(trial_questions)
            )
            if batch and (len(batch) >= st.jev_batch_size or size > st.jev_chunk_tokens):
                batches.append(batch)
                batch = [card]
            else:
                batch = trial
        if batch:
            batches.append(batch)
        considered = 0
        found = 0
        for batch in batches:
            _check_stop(stop_event)
            state = self._jev_state(doc["key"], cards=batch)
            questions = [
                JevQuestion(
                    key=card.target,
                    text=jev_question_text(query, subject=f"{card.title} ({card.target})"),
                )
                for card in batch
            ]
            probs = self.jev.score_many(state, questions)
            if len(probs) != len(batch):
                raise RuntimeError("jev adapter returned misaligned probabilities")
            for card, prob in zip(batch, probs):
                considered += 1
                node = IndexMap.card_page(card)
                if prob > st.jev_threshold:
                    found += 1
                    self._jev_gate(emit, "card", "candidate", node, prob, doc["key"])
                    candidates.append(card)
                else:
                    self._jev_gate(emit, "card", "pruned", node, prob, doc["key"])
        with work.lock:
            work.stats["cards_considered"] += considered
            work.stats["candidates"] += found
        return candidates

    def _jev_score_body(self, query: str, document: str, page: WikiPage,
                        entity_defs: list[dict], stop_event: Event | None) -> tuple[float, int]:
        """Full-body verdict over token chunks; page confidence = max chunk p."""
        st = self.settings
        body = sanitize_text(page.body)
        chunks = _jev_chunks(body, st.jev_chunk_tokens, st.jev_chunk_overlap,
                             prefix=f"{page.title}\n{page.path}")
        best = 0.0
        for index, chunk in enumerate(chunks):
            _check_stop(stop_event)
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
            probs = self.jev.score_many(state, [question])
            if len(probs) != 1:
                raise RuntimeError("jev adapter returned misaligned probabilities")
            best = max(best, probs[0])
        return best, len(chunks)

    @staticmethod
    def _is_toc_page(title: str) -> bool:
        """Pages that describe what a document contains: the rewriter's best clue."""
        return any(marker in (title or "") for marker in ("目次", "一覧", "index", "Index", "INDEX"))

    def _jev_toc_blocks(self, state: "_MapState", budget: JevSweepBudget,
                        stop_event: Event | None) -> list[tuple[str, str]]:
        """(name, full text) for every 00-目次 / 一覧 page in the wiki, at any depth.

        Recursive on purpose: subdirectories carry their own 00-目次, so a first-level
        scan describes one chapter and reports the rest of the wiki as 関連なし. Each
        index page goes to its summariser whole, and the reads pay from the sweep's
        budget and warm its cache, so a page the sweep scores later is not fetched twice.
        """
        blocks: list[tuple[str, str]] = []
        seen: set[str] = set()
        try:
            roots = self.client.list_children(path=self.settings.growi_root_path)
        except GrowiAPIError as exc:
            log.info("jev rewrite inventory failed (root listing): %s", exc)
            return []
        for root in roots:  # each top-level subtree, walked to every depth
            if not root.id and not root.path:
                continue
            for page in self._jev_walk(root, budget, stop_event, skip_index_pages=False):
                if not self._is_toc_page(page.title):
                    continue
                reference = page.id or page.path
                key = reference.strip("/")
                if not key or key in seen:
                    continue
                seen.add(key)
                body = page.body or ""
                if not body.strip():  # listings carry no body; the 目次 itself must be read
                    found = self._fetch_ref(reference, sweep_budget=budget)
                    body = found.body if found is not None else ""
                name = page.path.rsplit("/", 1)[0] or page.title or page.path
                blocks.append((name, sanitize_text(body)))
        return blocks

    def _jev_toc_note(self, query: str, document: str, text: str) -> str:
        """What one document holds relative to the question, in the corpus's own words."""
        payload = json.dumps({"質問": query, "文書": document, "この文書の目次": text},
                             ensure_ascii=False)
        note = self.llm.complete(JEV_TOC_SUMMARY_PROMPT, sanitize_text(payload))
        self._record_usage()
        return sanitize_text(note).replace("\n", " ").strip()[:500]

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
        blocks = self._jev_toc_blocks(state, budget, stop_event)
        if not blocks:
            return "", 0
        notes: list[str] = [""] * len(blocks)
        emit_guard = Lock()

        def summarize(index: int, document: str, text: str) -> None:
            try:
                notes[index] = self._jev_toc_note(query, document, text)
            except Exception as exc:  # noqa: BLE001 - one unread document must not blind the rewrite
                log.info("jev 目次 summary failed (%s): %s", document, exc)
                notes[index] = text[:400].replace("\n", " ")
            with emit_guard:
                emit({"type": "jev_toc", "document": document, "note": notes[index]})

        workers = min(len(blocks), max(1, self.settings.jev_workers))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jev-toc") as pool:
            futures = [pool.submit(summarize, index, document, text)
                       for index, (document, text) in enumerate(blocks)]
            for future in futures:
                future.result()
        return ("\n".join(f"[{document}] {notes[index]}"
                          for index, (document, _text) in enumerate(blocks)), len(blocks))

    def _jev_target_query(self, query: str, material: str) -> str:
        """Rewrite the question into a page-target question, in the corpus's vocabulary.

        The sweep asks every page the same question, so that wording decides what
        the classifier is even able to say yes to: the raw question is normally in
        the wrong shape (English against a Japanese manual, a whole-answer lookup
        phrased for an enumeration). Empty string means "no rewrite", and then the
        raw question is used.
        """
        if self.llm is None or not self.settings.llm_ready or not material:
            return ""
        payload = json.dumps({"質問": query, "この Wiki に存在するページ（目次）": material},
                             ensure_ascii=False)
        try:
            text = self.llm.complete(JEV_QUERY_REWRITE_PROMPT, sanitize_text(payload))
            self._record_usage()
        except Exception as exc:  # noqa: BLE001 - a missing rewrite must not fail the sweep
            log.info("jev query rewrite failed; using the question as-is: %s", exc)
            return ""
        return sanitize_text(text).replace("\n", " ").strip()[:400]

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
        grams = IndexMap.grams(query)
        if not grams:
            return False
        # Wide window on purpose: function tables and enumerations sit deep in
        # long pages, and dropping one of those is the exact failure this gate is
        # supposed to prevent. A short question ("深い") cannot produce 2 units, so
        # never demand more units than the question itself carries.
        shared = grams & IndexMap.grams(f"{page.title}\n{page.path}\n{page.body}"[:20000])
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

    def _run_jev_sweep(self, query: str, emit: Callable, stop_event: Event | None) -> list[dict[str, Any]]:
        """Exhaustive per-question relevance sweep; returns confirmed seeds.

        Crawl and classification are pipelined: one thread enumerates documents,
        cards and pages and publishes full-read targets on a bounded queue while
        `jev_workers` threads fetch and classify them, so GROWI traversal and page
        reads overlap classification instead of running before it.

        Never an answer generator: an empty list (or any failure) leaves the
        existing ES -> index map -> router -> lead path completely intact.
        """
        started = time.monotonic()
        st = self.settings
        workers = max(1, st.jev_workers)
        budget = JevSweepBudget(st.jev_max_page_reads, st.jev_max_list_calls)
        work = _SweepWork(stats={"documents": 0, "pages_considered": 0, "cards_considered": 0,
                                 "candidates": 0, "confirmed": 0, "entity_edges_followed": 0,
                                 "prefiltered": 0})
        status = "ok"
        print(f"[growi-search] Jev sweep: start backend={st.jev_backend} workers={workers}", flush=True)
        # tqdm-style live progress: one unit per classification gate. The total
        # grows as the crawl discovers cards/pages, so the percentage is clamped
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

        # The queue bound is the pipeline's backpressure: the crawl never runs
        # further ahead than the classifiers can consume.
        targets: Queue = Queue(maxsize=workers * 4)
        drained = object()

        def produce(state: "_MapState") -> None:
            try:
                docs = self._jev_documents(state, budget, stop_event)
                with work.lock:
                    work.stats["documents"] = len(docs)
                progress(add_total=sum(len(d["cards"]) for d in docs if d["indexed"]), step=0)
                for doc in docs:
                    _check_stop(stop_event)
                    if work.errors:
                        return
                    if doc["indexed"]:
                        candidates = self._jev_score_cards(jev_query, doc, gated, stop_event, work)
                        progress(add_total=len(candidates), step=0)  # each candidate costs a full read
                        for card in candidates:
                            targets.put((card.target, doc["key"], card))
                    else:
                        for page in self._jev_walk(doc["root"], budget, stop_event):
                            progress(add_total=1, step=0)  # the crawl just found one more page
                            targets.put((page.id or page.path, doc["key"], None))
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
                reference, document, card = item
                try:
                    self._jev_confirm(jev_query, reference, document, state, card,
                                      budget, stop_event, gated, work)
                except BaseException as exc:  # noqa: BLE001 - re-raised on the sweep thread
                    with work.lock:
                        work.errors.append(exc)

        try:
            state = self.index_map.snapshot() if self.index_map is not None else _MapState()
            # What Jev is actually asked: the question rewritten against the real
            # inventory of this wiki's 00-目次 pages. The rewritten text also feeds
            # the keyword prefilter, which is how an English question can still match
            # a Japanese manual.
            try:
                material, entries = self._jev_toc_digest(query, state, budget, stop_event, emit)
            except AgentStopped:
                raise
            except Exception as exc:  # noqa: BLE001 - a broken inventory must not fail the question
                log.info("jev 目次 digest failed; asking the raw question: %s", exc)
                material, entries = "", 0
            jev_query = self._jev_target_query(query, material) or query
            emit({"type": "jev_query", "text": jev_query, "rewritten": jev_query != query,
                  "toc_entries": entries})
            print(f"[growi-search] Jev 目次 digest: {entries} 文書 / {len(material)} chars "
                  f"under {st.growi_root_path}", flush=True)
            origin = (f"rewritten from {entries} 目次 entries" if jev_query != query
                      else "raw question — no 目次 inventory to rewrite from")
            print(f"[growi-search] Jev query ({origin}): {jev_query}", flush=True)
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
            # Phase B: entity-defining edges across document boundaries.
            while work.frontier:
                _check_stop(stop_event)
                page, card = work.frontier.popleft()
                entities = card.entities if card is not None else self._page_entities(state, page)
                if self.index_map is None:
                    continue
                for entity in entities:
                    for definer in self.index_map.definers_for(entity):
                        work.stats["entity_edges_followed"] += 1
                        if definer.target.strip("/") in work.visited:
                            continue
                        progress(add_total=1, step=0)  # edge target just discovered
                        self._jev_confirm(jev_query, definer.target, definer.document, state, definer,
                                          budget, stop_event, gated, work)
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
            f"skipped={stats['prefiltered']} "
            f"confirmed={stats['confirmed']} yes={units['yes_full']} "
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
        if context:
            try:
                text = self.llm.complete(
                    FOLLOWUP_ANSWER_PROMPT,
                    json.dumps({"question": question, "prior_context": context}, ensure_ascii=False),
                ).strip()
                self._record_usage()
                if text and text != "NEEDS_RESEARCH":
                    emit({"type": "route", "mode": "reuse", "reason": "prior conversation was sufficient"})
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
            answer = self._run_lead(question, emit, stop_event)
        answer.cited_nodes = [self._cite(node_id) for node_id in answer.cited_node_ids]
        usages = [*self._usage_cb.usage_metadata.values(), *self._extra_usage]
        _log_usage(question, answer.answer, answer.steps, usages, self.settings)
        return answer

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

    def _record_usage(self) -> None:
        if getattr(self.llm, "last_usage", None):
            self._extra_usage.append(self.llm.last_usage)

    def _try_route(self, question: str, emit: Callable, stop_event: Event | None, _q: str) -> AgentAnswer | None:
        self._seed_context = ""
        self._seed_ids = []
        _check_stop(stop_event)
        if self.jev is not None:
            confirmed = self._run_jev_sweep(question, emit, stop_event)
            if confirmed:
                groups = _seed_groups(confirmed, self.settings.jev_subagent_group_size,
                                      self.settings.jev_subagent_groups)
                explored = [result for group in groups for result in group]
                emit({"type": "candidates", "count": len(explored),
                      "nodes": [node_ref(result["node"]) for result in explored]})
                seeds = "\n\n".join(format_lead_candidate(result) for result in explored)
                if len(explored) > 1:
                    # One subagent per document slice; the merged reports are what the
                    # lead answers from, so the sweep's findings drive the answer.
                    reports, cited = self._run_seed_groups(groups, question, emit, stop_event)
                    self._seed_context = f"{seeds}\n\n{reports}"
                    self._seed_ids = dedupe([result["node"].id for result in explored] + cited)
                else:
                    self._seed_context = seeds
                    self._seed_ids = [result["node"].id for result in explored]
                return None  # force the deep lead path on the sweep's own findings
        if self.budget.pages_exhausted:
            emit({"type": "budget", "pages_used": self.budget.pages_used, "message": "ページ取得上限到達"})
        results = self.search_with_evidence(question, self.settings.rerank_top_k)
        results = self._merge_map(question, results, self.settings.index_map_top_k, emit)
        if not results:
            emit({"type": "route", "mode": "deep", "reason": "no candidates"})
            return None
        emit({"type": "candidates", "count": len(results), "nodes": [node_ref(r["node"]) for r in results]})
        payload = {"question": question, "candidates": [_describe(r) for r in results]}
        try:
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
            text = self.llm.complete(SHALLOW_ANSWER_PROMPT, json.dumps(context, ensure_ascii=False)).strip()
            self._record_usage()
        except Exception as exc:  # noqa: BLE001 - fall back to lead agent
            log.info("shallow answer failed; deep mode: %s", exc)
            return None
        if not text:
            return None
        return AgentAnswer(question=question, answer=sanitize_text(text), cited_node_ids=[r["node"].id for r in top], steps=1)

    def _run_lead(self, question: str, emit: Callable, stop_event: Event | None) -> AgentAnswer:
        ctx = _LeadContext(self, question, emit, stop_event)
        agent = _compile_agent(self.settings, _lead_tools(ctx), MAIN_AGENT_SYSTEM_PROMPT, stop_event)
        emit({"type": "route", "mode": "deep", "reason": "lead agent started"})
        content = _seeded(self.settings, question, self._seed_context, self._seed_ids)
        state = agent.invoke(
            {"messages": [{"role": "user", "content": sanitize_text(content)}]},
            config={
                "recursion_limit": max(50, self.settings.agent_max_steps * 2 + 4),
                "max_concurrency": 1,
                "callbacks": [self._usage_cb],
            },
        )
        finished = ctx.finished
        answer_text = finished.get("answer") or _last_text(state)
        cited = dedupe([*finished.get("cited_node_ids", []), *ctx.evidence])
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


    def _run_seed_groups(self, groups: list[list[dict[str, Any]]], question: str,
                         emit: Callable, stop_event: Event | None) -> tuple[str, list[str]]:
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
        by_document: dict[str, list[dict[str, Any]]] = {}
        for group in groups:
            for result in group:
                by_document.setdefault(result.get("document") or "?", []).append(result)
        reports: list[dict | None] = [None] * len(groups)
        emit_guard = Lock()

        def safe_emit(event: dict[str, Any]) -> None:
            with emit_guard:
                emit(event)

        max_workers = min(len(groups), max(1, self.settings.subagent_concurrency))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="seed-group") as executor:
            futures = {}
            for pos, group in enumerate(groups):
                blocked = everyone - owned[pos]  # another group owns those pages
                mine = {id(result) for result in group}
                document = group[0].get("document") or ""
                extra = [result["node"].id for result in by_document.get(document, [])
                         if id(result) not in mine]
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
        final = [report for report in reports if report]
        cited = dedupe([cited_id for report in final for cited_id in report.get("cited", [])])
        return _reports_text(final), cited

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
            "このドキュメントが質問について何を述べているかを報告してください。"
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
    return ctx.session._run_subagents(cleaned, ctx.question, ctx.evidence, ctx.emit, ctx.stop_event)


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
        StructuredTool.from_function(
            lambda node_ids: _lead_explore(ctx, node_ids), name="explore",
            description="指定ページID群をサブエージェントで並列深掘りします。", args_schema=LeadExploreArgs
        ),
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
    if cleaned in run.read_ids:
        return sanitize_text(f"{cleaned} は既に読みました。別のページを読むか follow_link/finish を呼んでください。")
    if len(run.read_ids) >= session.settings.subagent_max_reads:
        return sanitize_text(
            f"読み取り上限到達 ({len(run.read_ids)}/{session.settings.subagent_max_reads})。finish で回答してください。"
        )
    page = session._fetch_ref(cleaned)
    if page is not None:
        run.read_ids.add(page.id)
        run.visited.append(page.id)
        run.empty_streak = 0
        ctx.emit({"type": "read", "agent": run.index, "node": node_ref(page)})
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


def _sub_finish(ctx: _SubContext, answer: str, cited_node_ids: list[str] | None = None) -> str:
    _check_stop(ctx.stop_event)
    run = ctx.run
    if len(run.read_ids) < ctx.session.settings.subagent_min_reads:
        return (
            f"読んだページは {len(run.read_ids)} 件です。少なくとも "
            f"{ctx.session.settings.subagent_min_reads} 件読んでから finish してください。"
        )
    ctx.finished["answer"] = sanitize_text(str(answer or "")).strip()
    ctx.finished["cited_node_ids"] = _clean_ids(cited_node_ids)
    return "finished; do not call tools anymore"


def _sub_tools(ctx: _SubContext) -> list[StructuredTool]:
    return [
        StructuredTool.from_function(lambda text: _sub_search(ctx, text), name="search", description="GROWI を検索します。", args_schema=LeadSearchArgs),
        StructuredTool.from_function(lambda node_id, heading=None: _sub_read(ctx, node_id, heading), name="read", description="ページ本文（必要なら特定節）を読み出します。", args_schema=ReadArgs),
        StructuredTool.from_function(lambda node_id, direction="outgoing": _sub_follow(ctx, node_id, direction), name="follow_link", description="ページから発リンク（outgoing のみ）を取得します。", args_schema=FollowLinkArgs),
        StructuredTool.from_function(lambda answer, cited_node_ids=None: _sub_finish(ctx, answer, cited_node_ids), name="finish", description="担当領域の調査結果を報告して終了します。", args_schema=FinishArgs),
    ]


def _run_subagent(session: ResearchSession, run: Subrun, question: str, prompt: str, emit: Callable, stop_event: Event | None) -> dict:
    ctx = _SubContext(session=session, run=run, emit=emit, stop_event=stop_event)
    agent = _compile_agent(session.settings, _sub_tools(ctx), SUBAGENT_SYSTEM_PROMPT, stop_event)
    state = agent.invoke(
        {"messages": [{"role": "user", "content": sanitize_text(prompt)}]},
        config={
            "recursion_limit": max(30, session.settings.subagent_max_steps * 2 + 6),
            "max_concurrency": 1,
            "callbacks": [session._usage_cb],
        },
    )
    answer = sanitize_text(ctx.finished.get("answer") or "").strip() or _last_text(state)
    cited = _clean_ids(ctx.finished.get("cited_node_ids", [])) or dedupe(run.visited)
    emit({"type": "subagent_done", "agent": run.index, "cited": cited})
    return {"start": run.start_id, "answer": answer or "(報告なし)", "cited": cited, "finished": bool(ctx.finished.get("answer"))}


# --- override sanitization / SSRF guard ------------------------------------


_OVERRIDE_MAX = {
    "subagent_count": 6, "subagent_concurrency": 4, "subagent_min_reads": 10, "subagent_max_reads": 20,
    "subagent_max_steps": 40, "agent_max_steps": 60, "agent_patience": 30, "rerank_top_k": 40,
    "search_candidates": 50, "shallow_page_reads": 3, "index_map_top_k": 40,
}
_OVERRIDE_KEYS = {"chat_base_url", "chat_api_key", "chat_model", "chat_temperature", *_OVERRIDE_MAX}


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
            elif key in _OVERRIDE_MAX:
                clean[key] = max(1, min(int(float(raw)), _OVERRIDE_MAX[key]))
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
        self.cache = PageCache(settings.page_cache_ttl, settings.page_cache_max)
        self.reranker = reranker
        self.embedder = embedder
        self.index_map = IndexMap(client, settings, embedder, reranker)
        self.jev = build_jev(settings)
        self.client = client
        self.read_sem = asyncio.Semaphore(settings.service_max_reads)
        self.agent_sem = asyncio.Semaphore(settings.service_max_agents)

    def session(self, overrides: dict | None = None) -> ResearchSession:
        session = ResearchSession(self.client, self.settings, self.cache, self.reranker,
                                  index_map=self.index_map, jev=self.jev)
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
            index = self.client.get_page(path=f"{path.rstrip('/')}/{st.index_page_name}")
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
