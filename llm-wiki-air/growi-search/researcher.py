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
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
from typing import Any, Callable

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

import markdown as md
from config import Settings
from gateway import Embedder, LlmClient, Reranker, cosine, normalize_scores
from growi_client import GrowiAPIError, GrowiSearchClient
from models import AgentAnswer, Evidence, WikiLink, WikiPage, make_link_id
from prompts import (
    FOLLOWUP_ANSWER_PROMPT,
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


@dataclass
class _MapState:
    """One immutable snapshot of the index map; swapped atomically on refresh."""
    docs: list[md.IndexCard] = field(default_factory=list)
    cards: list[md.IndexCard] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=list)
    grams: list[set[str]] = field(default_factory=list)
    df: Counter = field(default_factory=Counter)


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
        for doc, sub in zip(docs, subs):
            if sub is None or not md.is_index_page(sub.body):
                continue
            for card in md.parse_index(sub.body):
                card.document = doc.title
                cards.append(card)
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
        return _MapState(docs=docs, cards=cards, vectors=vectors, grams=grams, df=df)

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

    def __init__(self, client: GrowiSearchClient, settings: Settings, cache: PageCache, reranker: Reranker | None, index_map: IndexMap | None = None) -> None:
        self.client = client
        self.settings = settings
        self.cache = cache
        self.reranker = reranker
        self.index_map = index_map
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

    def _fetch_page(self, *, page_id: str | None = None, path: str | None = None, revision: str = "") -> WikiPage | None:
        key = self._page_key(page_id or "", path or "", revision)
        with self._lock:
            if key in self._page_memo:
                return self._page_memo[key]

        cached = self.cache.get(((page_id or path), revision))
        if cached is not None:
            with self._lock:
                self._page_memo[key] = cached
            return cached

        if not self.budget.try_page():
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

    def _fetch_ref(self, reference: str) -> WikiPage | None:
        return (
            self._fetch_page(path=reference)
            if reference.startswith("/")
            else self._fetch_page(page_id=reference)
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
        blocks = ["サブエージェント報告書（それぞれ異なる領域を調査）:"]
        for index, report in enumerate(final, start=1):
            cited_str = ", ".join(report.get("cited", [])) or "(なし)"
            blocks.append(
                f"\n### サブエージェント {index} — 開始: {report.get('start')}\n{report.get('answer','').strip()}\n根拠ページID: {cited_str}"
            )
        return sanitize_text("\n".join(blocks))

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
        self.client = client
        self.read_sem = asyncio.Semaphore(settings.service_max_reads)
        self.agent_sem = asyncio.Semaphore(settings.service_max_agents)

    def session(self, overrides: dict | None = None) -> ResearchSession:
        session = ResearchSession(self.client, self.settings, self.cache, self.reranker, index_map=self.index_map)
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
