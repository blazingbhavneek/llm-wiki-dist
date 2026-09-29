"""growi-search researcher: finder → two dials → research rounds → compiler.

Speed rule (docs/new-growi-search.md, "Speed: run wide, then decide"): every step runs all
of its work at once and waits for all of it before the next step decides. Cheap work (JEV)
runs first; expensive work (LLM researchers) starts only after every JEV answer is in.

    FIND      local Qdrant hybrid search → rerank → JEV waves, all sections of a wave at once
              (A: "answers directly?"  B: "helps answer?"), wider waves while hits reach the tail
    DECIDE    quick exit when A is high and the question is not how/why → 1 LLM call
    RESEARCH  WHERE dial: one researcher per thread (evidence grouped by shared names/pages),
              all at once → lead check over ALL reports → HOW FAR dial: gap rounds (≤ max_rounds)
    COMPILE   one stage when the reports fit one LLM input, else L1 folds (all at once) → L2
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

import markdown as md
from config import Settings
from gateway import Embedder, Reranker, build_jev, chat_model
from growi_client import GrowiAPIError, GrowiSearchClient
from jev.types import JevQuestion, JevRequest
from models import AgentAnswer, Evidence, WikiLink, WikiPage, make_link_id
from page_cache import PageCache
from prompts import (
    FINAL_PROMPT, FOLD_PROMPT, JEV_LIST_QUESTION, JEV_MECHANISM_QUESTION, LEAD_CHECK_PROMPT, NOT_FOUND,
    QUICK_ANSWER_PROMPT, REPORT_NOW_PROMPT, RESEARCHER_PROMPT, STANDALONE_PROMPT, jev_direct_question,
    jev_helps_question,
)
from store import Store, norm
from sync import Sync

log = logging.getLogger("growi_search_researcher")

JEV_BATCH = 10            # sections per progress tick; every batch of a wave is submitted at once
RERANK_BATCH = 32
READ_CHARS = 20000        # a longer page is served whole section by section, never cut inside one
SEED_SHARE = 0.25         # share of a researcher's context its seed sections may fill (reads need the rest)
PREVIEW = 300             # UI previews and search-result blurbs only (never model evidence)

IMAGE_UNIT_RE = re.compile(r"<image-unit\b[^>]*>.*?</image-unit>", re.I | re.S)
IMAGE_DESC_RE = re.compile(r"<image-description\b[^>]*>(.*?)</image-description>", re.I | re.S)
IMAGE_MEDIA_RE = re.compile(r"<image-media\b[^>]*>.*?</image-media>", re.I | re.S)
HTML_IMAGE_RE = re.compile(r"<(?:img|embed)\b[^>]*>", re.I | re.S)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\([^\n)]*\)", re.I)
DATA_IMAGE_RE = re.compile(r"data:image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+", re.I)
PAGE_ID_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{24}(?![0-9a-fA-F])")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")


class AgentStopped(Exception):
    """Raised when the client cancels an in-flight run."""


# -- small helpers ---------------------------------------------------------------


def sanitize_text(value: Any) -> str:
    """Keep image descriptions, drop image payloads (they would blow up model inputs)."""
    text = value if isinstance(value, str) else str(value or "")

    def unit(match: re.Match) -> str:
        desc = IMAGE_DESC_RE.search(match.group(0))
        return desc.group(1).strip() if desc else ""

    text = IMAGE_UNIT_RE.sub(unit, text)
    text = IMAGE_MEDIA_RE.sub("", text)
    text = HTML_IMAGE_RE.sub("[image omitted]", text)
    text = MARKDOWN_IMAGE_RE.sub("[image omitted]", text)
    return DATA_IMAGE_RE.sub("[image omitted]", text)


def clean_ref(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*[-*]\s*", "", text).strip().strip("`'\" \t\r\n")
    text = re.sub(r"^(?:id|node_id)\s*[:：]\s*", "", text, flags=re.I).strip()
    text = text.split("|", 1)[0].strip()
    return text.strip("`'\" \t\r\n,;")


def is_page_id(ref: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{24}", (ref or "").strip("/")))


def dedupe(values: Any) -> list:
    return list(dict.fromkeys(v for v in values if v))


def node_ref(page: WikiPage | None) -> dict[str, str]:
    return {"id": page.id, "title": page.title or page.id} if page else {}


def est_tokens(text: str) -> int:
    """Rough upper estimate without the server's tokenizer: Japanese ≈ 1 token per char."""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return ascii_chars // 3 + (len(text) - ascii_chars) + 16


def pack(texts: list[str], budget: int) -> list[list[str]]:
    """Greedy groups of whole texts under ``budget`` tokens; an oversized text is split on
    paragraph boundaries into parts that fit. Nothing is dropped."""
    budget = max(budget, 1000)
    pieces: list[str] = []
    for text in texts:
        if est_tokens(text) <= budget:
            pieces.append(text)
            continue
        part = ""
        for paragraph in text.split("\n\n"):
            if part and est_tokens(part) + est_tokens(paragraph) > budget:
                pieces.append(part)
                part = paragraph
            else:
                part = f"{part}\n\n{paragraph}" if part else paragraph
        if part:
            pieces.append(part)
    groups: list[list[str]] = []
    size = 0
    for piece in pieces:
        cost = est_tokens(piece)
        if groups and size + cost <= budget:
            groups[-1].append(piece)
            size += cost
        else:
            groups.append([piece])
            size = cost
    return groups


def parse_json(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p if isinstance(p, str) else str(p.get("text", "")) for p in content
                       if isinstance(p, str) or (isinstance(p, dict) and p.get("type") in (None, "text")))
    return str(content or "")


def heading_subtree(body: str, heading: str) -> str:
    lines: list[str] = []
    capturing, base, fence = False, 0, False
    for line in body.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        match = None if fence else HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            if not capturing and heading.casefold() in match.group(2).casefold():
                capturing, base = True, level
                lines.append(line)
                continue
            if capturing and level <= base:
                break
        if capturing:
            lines.append(line)
    return "\n".join(lines)


def h2_parts(body: str) -> list[str]:
    """The page split before every H2 outside fences (lossless: joined back it is the page)."""
    parts: list[list[str]] = [[]]
    fence = False
    for line in body.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and line.startswith("## ") and parts[-1]:
            parts.append([])
        parts[-1].append(line)
    return ["\n".join(part) for part in parts if part]


def _p_yes(result: Any, default: float) -> float:
    if isinstance(result, BaseException):
        log.info("jev request failed: %s", result)
        return default
    try:
        return float(result.p_yes)
    except (AttributeError, ValueError):
        return default


# -- run state -------------------------------------------------------------------


@dataclass
class Hit:
    key: str
    rank: int
    a: float = 0.0   # answers directly
    b: float = 0.0   # helps answer

    @property
    def score(self) -> float:
        return max(self.a, self.b)


@dataclass
class Findings:
    hits: list[Hit]
    pool: list[str]
    mechanism: float
    listy: float


@dataclass
class Task:
    seeds: list[str]
    focus: str = ""    # a gap from the lead check; empty in round 1


@dataclass
class Report:
    agent: int
    round: int
    focus: str
    findings: str
    open_questions: list[str] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)

    def text(self, title: Callable[[str], str]) -> str:
        lines = [f"### 調査 {self.agent}（ラウンド {self.round}{'・課題: ' + self.focus if self.focus else ''}）",
                 self.findings.strip() or "（報告なし）"]
        if self.open_questions:
            lines += ["#### まだ分からないこと", *(f"- {q}" for q in self.open_questions)]
        if self.cited:
            lines += ["#### 根拠ページ", *(f"- {pid} : {title(pid)}" for pid in self.cited)]
        return "\n".join(lines)


class Run:
    """One question: its settings, event sink, LLM calls, page reads and citable pages."""

    def __init__(self, service: "Researcher", question: str, emit: Callable[[dict], None], settings: Settings) -> None:
        self.service, self.settings, self.emit = service, settings, emit
        self.question = question
        self.deadline = time.monotonic() + settings.run_seconds
        self.stopped = False
        self.usage = UsageMetadataCallbackHandler()
        self.llm_calls = 0
        self.agents = 0
        self.jev_done = 0
        self.titles: dict[str, str] = {}      # citable page id -> title (pages this run saw)
        self._reads: dict[str, asyncio.Future] = {}
        self.research = asyncio.Semaphore(settings.subagent_concurrency)
        self.started = time.monotonic()
        self.trace: dict[str, Any] = {"question": question, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                      "seconds": {}, "route": ""}

    def mark(self, step: str) -> None:
        """Seconds since the question arrived when ``step`` finished (for the trace)."""
        self.trace["seconds"][step] = round(time.monotonic() - self.started, 2)

    def cite(self, page_id: str, title: str) -> None:
        if is_page_id(page_id) and page_id not in self.titles:
            self.titles[page_id] = title or page_id

    def title(self, page_id: str) -> str:
        entry = self.service.store.page(page_id) or {}
        return self.titles.get(page_id) or entry.get("page_title") or page_id

    async def complete(self, system: str, user: str, max_tokens: int) -> str:
        async with self.service.llm_slots:
            reply = await chat_model(self.settings, max_tokens).ainvoke(
                [SystemMessage(system), HumanMessage(user)], config={"callbacks": [self.usage]})
        self.llm_calls += 1
        return _content_text(reply.content).strip()

    async def stream(self, system: str, user: str, max_tokens: int) -> str:
        parts: list[str] = []
        async with self.service.llm_slots:
            async for chunk in chat_model(self.settings, max_tokens).astream(
                    [SystemMessage(system), HumanMessage(user)], config={"callbacks": [self.usage]}):
                piece = _content_text(chunk.content)
                if piece:
                    parts.append(piece)
                    self.emit({"type": "answer_delta", "text": piece})
        self.llm_calls += 1
        return "".join(parts).strip()

    async def page(self, ref: str) -> WikiPage | None:
        """Read once per run even when several researchers ask for the same page at once."""
        ref = clean_ref(ref)
        if not ref:
            return None
        future = self._reads.get(ref)
        if future is None:
            future = asyncio.ensure_future(self.service.read_ref(ref))
            self._reads[ref] = future
        page = await asyncio.shield(future)
        if page is not None:
            self.cite(page.id, page.title)
        return page


# -- tool argument schemas -------------------------------------------------------


class ReadArgs(BaseModel):
    node_id: str = Field(description="page id (or path) to read")
    heading: str | None = Field(default=None, description="read only this section (optional)")


class NodeArgs(BaseModel):
    node_id: str = Field(description="page id (or path)")


class SearchArgs(BaseModel):
    text: str = Field(description="what to look for")


class DefinitionArgs(BaseModel):
    name: str = Field(description="function name, term or other name")


class FinishArgs(BaseModel):
    findings: str = Field(description="what you found, with the page_id of every source")
    open_questions: list[str] = Field(default_factory=list, description="what is still not understood")
    cited_node_ids: list[str] = Field(default_factory=list, description="page ids you used")


# -- overrides -------------------------------------------------------------------

_TRACE_SETTINGS = ("jev_help_threshold", "jev_direct_threshold", "search_pool", "rerank_pool", "wave_size",
                   "max_waves", "quick_max_pages", "max_threads", "max_rounds", "max_gaps", "run_seconds",
                   "subagent_concurrency", "subagent_max_steps", "chat_model")
_OVERRIDE_KEYS = {"chat_base_url", "chat_api_key", "chat_model", "chat_temperature", "subagent_concurrency",
                  "subagent_max_steps"}
_BLOCKED_NETS = ("127.", "169.254.", "100.100.", "metadata", "localhost")


def _validate_llm_url(url: str, settings: Settings) -> None:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    allowed = settings.allowed_hosts
    if host.startswith(_BLOCKED_NETS) or host.endswith(".local"):
        raise ValueError("LLM endpoint host is not permitted")
    if not allowed:
        raise ValueError("LLM base_url is not configured on the server.")
    if host not in allowed and not any(host.endswith("." + h.lstrip(".")) for h in allowed):
        raise ValueError("LLM endpoint host is not permitted")


def _sanitize_overrides(overrides: dict[str, Any] | None, settings: Settings) -> dict[str, Any]:
    """Only the chat endpoint and researcher knobs change per request; the frontend's older
    knobs are ignored. Concurrency never exceeds this service's LLM slots."""
    clean: dict[str, Any] = {}
    for key, raw in (overrides or {}).items():
        if raw is None or raw == "" or key not in _OVERRIDE_KEYS:
            continue
        try:
            if key == "chat_temperature":
                clean[key] = max(0.0, min(2.0, float(raw)))
            elif key == "subagent_concurrency":
                clean[key] = max(1, min(int(float(raw)), settings.llm_max_concurrency))
            elif key == "subagent_max_steps":
                clean[key] = max(4, min(int(float(raw)), 100))
            else:
                clean[key] = str(raw).strip()
        except (TypeError, ValueError):
            continue
    if "chat_base_url" in clean:
        _validate_llm_url(clean["chat_base_url"], settings)
    return clean


# -- the service -----------------------------------------------------------------


class Researcher:
    def __init__(self, client: GrowiSearchClient, settings: Settings, reranker: Reranker | None,
                 embedder: Embedder | None = None) -> None:
        self.client, self.settings, self.reranker, self.embedder = client, settings, reranker, embedder
        self.store = Store(settings, embedder)
        self.sync = Sync(client, settings, self.store)
        self.jev = build_jev(settings)
        self.cache = PageCache(settings.page_cache_ttl, settings.page_cache_max,
                               max_bytes=settings.page_cache_mb * 1024 * 1024)
        # One slot per LLM request this service may have open on the shared server.
        self.llm_slots = asyncio.Semaphore(settings.llm_max_concurrency)
        self.agent_sem = asyncio.Semaphore(settings.service_max_agents)
        self.growi_sem = asyncio.Semaphore(settings.growi_concurrency)
        self.rerank_sem = asyncio.Semaphore(4)

    def start(self) -> None:
        self.sync.start()

    def close(self) -> None:
        self.sync.stop()
        self.store.close()

    def validate_overrides(self, overrides: dict | None) -> None:
        """Raise ValueError (-> HTTP 400) for disallowed hosts."""
        _sanitize_overrides(overrides, self.settings)

    def _payload(self, key: str) -> dict[str, Any]:
        return self.store.get(key) or {}

    def _page_of(self, key: str) -> str:
        return self._payload(key).get("page_id", "")

    # -- GROWI reads and views ---------------------------------------------------

    async def read_ref(self, ref: str) -> WikiPage | None:
        ref = clean_ref(ref)
        key = ("id", ref.strip("/")) if is_page_id(ref) else ("path", ref)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        async with self.growi_sem:
            if key[0] == "id":
                page = await asyncio.to_thread(self.client.get_page, page_id=key[1])
            else:
                page = await asyncio.to_thread(self.client.get_page, path=ref)
        if page is not None:
            page.source_url = self.source_url(page)
            self.cache.put(key, page)
            self.cache.put(("id", page.id), page)
        return page

    def source_url(self, page: WikiPage) -> str:
        encoded = "/".join(quote(seg) for seg in page.path.strip("/").split("/"))
        return f"{self.client.url}/{encoded}" if page.path else ""

    def links_for(self, page: WikiPage) -> list[WikiLink]:
        links: list[WikiLink] = []
        seen: set[tuple[str, str]] = set()
        for parsed in md.extract_links(page.body):
            if parsed.kind == "nav":
                continue
            raw = f"{parsed.raw_target}{'#' + parsed.fragment if parsed.fragment else ''}"
            target = md.resolve_target(page.path, raw, self.settings.growi_root_path)
            if target is None:
                continue
            target_id = target.page_id or target.path or ""
            if not target_id or (target_id, parsed.fragment) in seen:
                continue
            seen.add((target_id, parsed.fragment))
            links.append(WikiLink(
                id=make_link_id(page.id, target_id, parsed.anchor, parsed.fragment),
                source_node_id=page.id, target_node_id=target_id, label=parsed.anchor or target_id,
                summary=parsed.summary or (f"{parsed.anchor}（{parsed.heading}）" if parsed.heading else parsed.anchor),
                source_heading=parsed.heading, fragment=parsed.fragment, target_path=target.path or "",
                kind=parsed.kind))
        return links

    async def node_view(self, page_id: str) -> dict | None:
        page = await self.read_ref(page_id)
        if page is None:
            return None
        entry = self.store.page(page.id) or {}
        page.document = entry.get("doc_name") or page.path
        page.summary = entry.get("summary") or md.section_summary(page.body) or page.title
        return {**page.public_dict(), "links": [link.model_dump() for link in self.links_for(page)]}

    async def read_page(self, page_id: str) -> WikiPage | None:
        return await self.read_ref(page_id)

    async def children(self, *, page_id: str | None = None, path: str | None = None) -> list[WikiPage]:
        async with self.growi_sem:
            return await asyncio.to_thread(self.client.list_children, page_id=page_id, path=path)

    async def document_view(self, path: str) -> dict:
        children = [c for c in await self.children(path=path)
                    if c.path.rstrip("/").rsplit("/", 1)[-1] != self.settings.index_page_name]
        pages = []
        for child in sorted(children, key=lambda c: c.path):
            row = child.public_dict()
            entry = self.store.page(child.id)
            if entry:
                row.update({"summary": entry.get("summary", ""), "keywords": entry.get("kind", []),
                            "cluster": entry.get("chapter", "")})
            row["source_url"] = self.source_url(child)
            pages.append(row)
        folder = WikiPage(id="", path=path)
        return {"path": path, "title": path.rstrip("/").rsplit("/", 1)[-1], "pages": pages,
                "source_url": self.source_url(folder)}

    async def _embed_query(self, text: str) -> list[float] | None:
        if self.embedder is None:
            return None
        try:
            return await asyncio.to_thread(self.embedder.embed_query, text)
        except Exception as exc:  # noqa: BLE001 - the BM25 channels still answer
            log.warning("query embedding failed, searching by keywords only: %s", exc)
            return None

    async def _search(self, text: str, limit: int, levels: tuple[str, ...]) -> list[dict[str, Any]]:
        vector = await self._embed_query(text)
        return await asyncio.to_thread(self.store.search, text, vector, limit=limit, levels=levels)

    def _section_keys(self, rows: list[dict[str, Any]]) -> list[str]:
        """Search rows as section windows: a fact stands for the section it came from."""
        keys: list[str] = []
        for row in rows:
            if row.get("level") == "section":
                keys.append(row.get("key", ""))
            elif row.get("level") == "fact":
                keys.extend(self.store.windows_of_section(row.get("section", ""))[:1])
        return dedupe(keys)

    async def fast_search(self, query: str, limit: int) -> list[dict]:
        """/api/search: hybrid search grouped by page; no JEV, no LLM."""
        rows = await self._search(query, limit * 6, ("section", "fact", "page"))
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            page_id = row.get("page_id")
            if not page_id:
                continue
            entry = out.get(page_id)
            if entry is None:
                if len(out) >= limit:
                    continue
                meta = self.store.page(page_id) or {}
                node = WikiPage(id=page_id, path=row.get("page_path", ""), title=row.get("page_title", ""),
                                revision_id=row.get("revision", ""), summary=meta.get("summary", ""),
                                document=row.get("doc_name", ""), cluster=meta.get("chapter", ""))
                node.source_url = self.source_url(node)
                entry = out[page_id] = {"node": node, "score": row.get("score", 0.0), "evidence": []}
            if row.get("level") != "page" and len(entry["evidence"]) < 2:
                entry["evidence"].append(Evidence(
                    page_id=page_id, field="section", text=str(row.get("text", ""))[:PREVIEW],
                    heading=row.get("heading", ""), score=float(row.get("score", 0.0))).model_dump())
        return list(out.values())

    # -- ask -------------------------------------------------------------------

    async def ask(self, question, on_event=None, overrides=None, stop_event=None, context="",
                  cited_node_ids=None) -> AgentAnswer:
        settings = self.settings.model_copy(update=_sanitize_overrides(overrides, self.settings))
        emit = on_event or (lambda _event: None)
        if self.agent_sem.locked():
            emit({"type": "queued_for_agent"})
        async with self.agent_sem:
            run = Run(self, question.strip(), emit, settings)
            task = asyncio.current_task()
            watcher = asyncio.create_task(self._watch(stop_event, task, run)) if stop_event is not None else None
            started = time.monotonic()
            answer: AgentAnswer | None = None
            try:
                answer = await self._answer(run, context or "", cited_node_ids or [])
                return answer
            except asyncio.CancelledError:
                if run.stopped:
                    task.uncancel()
                    raise AgentStopped() from None
                raise
            finally:
                if watcher is not None:
                    watcher.cancel()
                self._log_usage(run, answer, time.monotonic() - started)
                self._record(run, answer)

    @staticmethod
    async def _watch(stop_event: Any, task: asyncio.Task, run: Run) -> None:
        """The stop button (or a closed browser) cancels every task of the run at once."""
        while not stop_event.is_set():
            await asyncio.sleep(0.25)
        run.stopped = True
        task.cancel()

    async def _answer(self, run: Run, context: str, cited_node_ids: list[str]) -> AgentAnswer:
        st = run.settings
        if not st.llm_ready:
            raise RuntimeError("LLM is unavailable")
        if context.strip():
            direct = await self._standalone(run, context, cited_node_ids)
            run.trace["standalone"] = run.question
            if direct is not None:
                run.trace["route"] = "conversation"
                return direct
        if not self.store.counts()["sections"]:
            return AgentAnswer(question=run.question,
                               answer="検索用の索引を作成中です。しばらくしてからもう一度お試しください。")

        found = await self._find(run)

        # DECIDE: quick exit only when the answer is written in one place and it is not a how/why question.
        direct = [h for h in found.hits if h.a >= st.jev_direct_threshold]
        direct_pages = dedupe(self._page_of(h.key) for h in direct)
        hit_pages = {self._page_of(h.key) for h in found.hits}
        quick = bool(direct and found.mechanism < 0.5 and len(direct_pages) <= st.quick_max_pages
                     and (found.listy < 0.5 or hit_pages <= set(direct_pages)))
        run.trace["decision"] = {"direct_pages": direct_pages, "hit_pages": sorted(hit_pages), "quick_exit": quick}
        if quick:
            answer = await self._quick(run, direct_pages)
            if answer is not None:
                run.trace["route"] = "quick"
                return answer
            run.trace["decision"]["quick_fell_back"] = True

        run.trace["route"] = "research"
        run.emit({"type": "route", "mode": "deep"})
        if found.hits:
            threads = self._threads(found.hits, st)
            run.trace["threads"] = [[h.key for h in thread] for thread in threads]
            tasks = [Task(seeds=[h.key for h in thread]) for thread in threads]
        elif found.pool:  # JEV found nothing: one researcher follows the best-ranked leads
            run.trace["threads"] = [found.pool[:5]]
            tasks = [Task(seeds=found.pool[:5])]
        else:
            run.trace["route"] = "not_found"
            return AgentAnswer(question=run.question, answer=NOT_FOUND)

        reports = await self._round(run, tasks, 1)
        rounds = 1
        while rounds < st.max_rounds and time.monotonic() < run.deadline:
            complete, gaps = await self._lead_check(run, reports)
            if complete or not gaps:
                break
            tasks = await self._gap_tasks(run, gaps, found)
            if not tasks:
                break
            rounds += 1
            reports += await self._round(run, tasks, rounds)

        text = await self._compile(run, reports)
        if not text.strip():  # no information loss: show the research itself
            text = ("回答の生成に失敗したため、調査報告をそのまま示します。\n\n"
                    + "\n\n".join(r.text(run.title) for r in reports))
        fallback = [pid for r in reports for pid in r.cited] + [self._page_of(h.key) for h in found.hits]
        return self._finish(run, text, fallback)

    async def _standalone(self, run: Run, context: str, cited_node_ids: list[str]) -> AgentAnswer | None:
        """A follow-up either answers from the conversation or becomes a standalone question."""
        text = await run.complete(STANDALONE_PROMPT, f"これまでの会話:\n{context}\n\n新しい質問: {run.question}", 8192)
        first, _sep, rest = text.strip().partition("\n")
        if first.strip().upper().startswith("ANSWER") and rest.strip():
            for pid in cited_node_ids:
                run.cite(pid, "")
            ids = [pid for pid in dedupe(cited_node_ids) if is_page_id(pid)]
            return AgentAnswer(question=run.question, answer=sanitize_text(rest.strip()), cited_node_ids=ids,
                               cited_nodes=[self._cited_node(run, pid) for pid in ids], steps=1)
        match = re.search(r"QUESTION\s*[:：]\s*(.+)", text)
        if match and match.group(1).strip():
            run.question = match.group(1).strip()
        return None

    # -- FIND --------------------------------------------------------------------

    @staticmethod
    def _helpful(hit: Hit, st: Settings) -> bool:
        return hit.a >= st.jev_direct_threshold or hit.b >= st.jev_help_threshold

    async def _find(self, run: Run) -> Findings:
        st, q = run.settings, run.question
        profile = asyncio.ensure_future(self._profile(q))  # JEV on the question itself, beside wave 1
        run.emit({"type": "search", "query": q})
        rows = await self._search(q, st.search_pool, ("section", "fact", "page", "document"))
        pool, pinned, hot_docs = self._candidates(q, rows)
        run.emit({"type": "candidates", "count": len({self._page_of(k) for k in pool})})
        pool = await self._rerank(q, pool, pinned, st.rerank_pool)
        rank = {key: i for i, key in enumerate(pool)}

        run.trace["find"] = {"candidates": len(pool), "pinned": pinned, "hot_documents": hot_docs,
                             "ranked": [self._trace_key(k) for k in pool[:60]], "waves": []}
        if self.jev is None:
            profile.cancel()
            run.emit({"type": "jev_unavailable"})
            hits = [Hit(key, i, 0.0, 1.0) for i, key in enumerate(pool[:20])]
            for hit in hits:
                run.cite(self._page_of(hit.key), self._payload(hit.key).get("page_title", ""))
            run.mark("find")
            return Findings(hits=hits, pool=pool, mechanism=1.0, listy=0.0)

        scored: dict[str, Hit] = {}
        queue = list(pool)
        for _wave in range(st.max_waves):
            wave: list[str] = []
            while queue and len(wave) < st.wave_size:
                key = queue.pop(0)
                if key not in scored and key not in wave and self.store.get(key):
                    wave.append(key)
            if not wave:
                break
            for key, (a, b) in zip(wave, await self._jev_wave(run, q, wave)):
                scored[key] = Hit(key, rank.get(key, len(rank) + len(scored)), a, b)
            run.trace["find"]["waves"].append([{**self._trace_key(k), "a": round(scored[k].a, 3),
                                                "b": round(scored[k].b, 3)} for k in wave])
            wave_hits = [i for i, key in enumerate(wave) if self._helpful(scored[key], st)]
            found_any = any(self._helpful(h, st) for h in scored.values())
            if found_any and not any(i >= len(wave) // 2 for i in wave_hits):
                break  # the lower half of this wave found nothing new: FIND is done
            # Go wider, neighbours of what hit first (lists usually sit together in one chapter).
            neighbours = self._neighbours([wave[i] for i in wave_hits], hot_docs, scored)
            queue = neighbours + [key for key in queue if key not in set(neighbours)]

        mechanism, listy = await profile
        hits = sorted((h for h in scored.values() if self._helpful(h, st)),
                      key=lambda h: (h.a < st.jev_direct_threshold, -h.score, h.rank))
        run.trace["find"].update({"mechanism": round(mechanism, 3), "list": round(listy, 3), "hits": len(hits)})
        run.mark("find")
        for hit in hits:
            run.cite(self._page_of(hit.key), self._payload(hit.key).get("page_title", ""))
        pages = dedupe(self._page_of(h.key) for h in hits)
        run.emit({"type": "jev_complete", "pages_considered": len(scored), "prefiltered": 0, "yes": len(hits),
                  "mean_yes_probability": round(sum(h.b for h in scored.values()) / max(1, len(scored)), 3),
                  "max_probability": round(max((h.a for h in scored.values()), default=0.0), 3),
                  "confirmed": sum(1 for h in hits if h.a >= st.jev_direct_threshold)})
        run.emit({"type": "map", "documents": len({self._payload(h.key).get("document") for h in hits}),
                  "pages": len(pages), "selected": len(hits),
                  "nodes": [{"id": pid, "title": run.title(pid)} for pid in pages]})
        return Findings(hits=hits, pool=pool, mechanism=mechanism, listy=listy)

    async def _profile(self, question: str) -> tuple[float, float]:
        """JEV reads the question itself: is it how/why (mechanism)? does it want a full list?"""
        if self.jev is None:
            return 1.0, 0.0
        state = {"question": question}
        results = await self.jev.adecide_batch([
            JevRequest(state, JevQuestion(text=JEV_MECHANISM_QUESTION, key="mechanism")),
            JevRequest(state, JevQuestion(text=JEV_LIST_QUESTION, key="list")),
        ], return_exceptions=True)
        return _p_yes(results[0], 1.0), _p_yes(results[1], 0.0)

    def _candidates(self, question: str, rows: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
        """Section windows in search order with exact names first, the pinned name hits, and
        documents that matched as a whole."""
        keys: list[str] = []
        seen: set[str] = set()

        def add(key: str) -> None:
            if key and key not in seen and self.store.get(key):
                seen.add(key)
                keys.append(key)

        names = self.store.names_in(question)
        for name in names:  # sections that define a name written in the question
            for key in self.store.defines.get(name, []):
                for window in self.store.windows_of_section(key.rsplit("|", 1)[0]):
                    add(window)
        pinned = list(keys)
        hot_docs: list[str] = []
        page_hits: list[str] = []
        for row in rows:
            level = row.get("level")
            if level == "section":
                add(row.get("key", ""))
            elif level == "fact":
                for window in self.store.windows_of_section(row.get("section", "")):
                    add(window)
            elif level == "page":
                page_hits.append(row.get("page_id", ""))
            elif level == "document":
                hot_docs.append(row.get("document", ""))
        for name in names:  # then sections that use it
            for key in self.store.uses.get(name, []):
                add(key)
        for page_id in page_hits:  # then the rest of pages that matched as a whole
            for key in self.store.page_windows.get(page_id, []):
                add(key)
        return keys, pinned, dedupe(hot_docs)

    async def _rerank(self, question: str, keys: list[str], pinned: list[str], limit: int) -> list[str]:
        head = [key for key in keys if key not in set(pinned)][:limit]
        if self.reranker is None or not head:
            return keys

        async def score(batch: list[str]) -> list[float]:
            texts = [f"{self._payload(k).get('page_title', '')} > {self._payload(k).get('heading', '')}\n"
                     f"{self._payload(k).get('text', '')}" for k in batch]
            async with self.rerank_sem:
                return await asyncio.to_thread(self.reranker.score, question, texts)

        batches = [head[i:i + RERANK_BATCH] for i in range(0, len(head), RERANK_BATCH)]
        try:
            scores = [s for part in await asyncio.gather(*(score(b) for b in batches)) for s in part]
        except Exception as exc:  # noqa: BLE001 - keep the fusion order
            log.warning("rerank failed, keeping fusion order: %s", exc)
            return keys
        ranked = [key for key, _s in sorted(zip(head, scores), key=lambda pair: -pair[1])]
        return dedupe([*pinned, *ranked, *keys])

    async def _jev_wave(self, run: Run, question: str, keys: list[str]) -> list[tuple[float, float]]:
        """Score every section of the wave at once with both questions over one shared state."""
        qa = JevQuestion(text=jev_direct_question(question), key="a")
        qb = JevQuestion(text=jev_helps_question(question), key="b")
        results: list[tuple[float, float]] = [(0.0, 0.0)] * len(keys)
        total = run.jev_done + len(keys)

        async def batch(start: int) -> None:
            requests = []
            for key in keys[start:start + JEV_BATCH]:
                p = self._payload(key)
                state = {"document": p.get("doc_name", ""),
                         "page": {"title": p.get("page_title", ""), "path": p.get("page_path", "")},
                         "section": {"heading": p.get("heading", ""), "kind": p.get("kind", ""),
                                     "summary": p.get("summary", ""), "text": sanitize_text(p.get("text", ""))}}
                requests += [JevRequest(state, qa), JevRequest(state, qb)]
            out = await self.jev.adecide_batch(requests, return_exceptions=True)
            for i in range(0, len(out), 2):
                results[start + i // 2] = (_p_yes(out[i], 0.0), _p_yes(out[i + 1], 0.0))
            run.jev_done += len(out) // 2
            yes = sum(1 for a, b in results if a >= run.settings.jev_direct_threshold or b >= run.settings.jev_help_threshold)
            run.emit({"type": "jev_progress", "done": run.jev_done, "total": total, "yes": yes,
                      "percent": int(100 * run.jev_done / max(1, total)), "stage": "sweep"})

        await asyncio.gather(*(batch(start) for start in range(0, len(keys), JEV_BATCH)))
        return results

    def _trace_key(self, key: str) -> dict[str, Any]:
        p = self._payload(key)
        return {"key": key, "page": p.get("page_title", ""), "heading": p.get("heading", "")}

    def _neighbours(self, hit_keys: list[str], hot_docs: list[str], scored: dict[str, Hit]) -> list[str]:
        """The rest of the pages that hit, then nearby pages of the same documents."""
        pages_hit = dedupe(self._page_of(k) for k in hit_keys)
        out: list[str] = [key for pid in pages_hit for key in self.store.page_windows.get(pid, [])]
        for doc in dedupe([self._payload(k).get("document") for k in hit_keys] + hot_docs):
            order = self.store.doc_pages.get(doc, [])
            anchors = [order.index(pid) for pid in pages_hit if pid in order]
            for i in sorted(range(len(order)), key=lambda i: min((abs(i - a) for a in anchors), default=i)):
                out.extend(self.store.page_windows.get(order[i], []))
        return [key for key in dedupe(out) if key not in scored]

    # -- quick exit ----------------------------------------------------------------

    async def _quick(self, run: Run, page_ids: list[str]) -> AgentAnswer | None:
        st = run.settings
        read = await asyncio.gather(*(run.page(pid) for pid in page_ids), return_exceptions=True)
        pages = [p for p in read if isinstance(p, WikiPage)]
        if len(pages) < len(page_ids):
            return None  # a page could not be read: let the researchers work around it
        material = "\n\n".join(f"--- {p.id} : {p.title} ---\n{sanitize_text(p.body)}" for p in pages)
        user = f"質問: {run.question}\n\n{material}"
        if est_tokens(QUICK_ANSWER_PROMPT + user) > st.llm_context_tokens - st.final_compiler_tokens:
            return None  # the pages do not fit one call: research them instead
        run.emit({"type": "route", "mode": "shallow"})
        run.emit({"type": "compiling"})
        text = await run.stream(QUICK_ANSWER_PROMPT, user, st.final_compiler_tokens)
        return self._finish(run, text, [p.id for p in pages]) if text else None

    # -- WHERE dial: threads -------------------------------------------------------

    def _threads(self, hits: list[Hit], st: Settings) -> list[list[Hit]]:
        """Group evidence by connection (same page, shared names), not by document."""
        parent = {h.key: h.key for h in hits}

        def find(key: str) -> str:
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        def union(a: str, b: str) -> None:
            parent[find(a)] = find(b)

        first_of_page: dict[str, str] = {}
        names: dict[str, list[str]] = {}
        for hit in hits:
            payload = self._payload(hit.key)
            head = self.store.get(payload.get("section", "") + "|0") or payload
            page_id = payload.get("page_id", "")
            if page_id in first_of_page:
                union(hit.key, first_of_page[page_id])
            else:
                first_of_page[page_id] = hit.key
            for name in set(head.get("defines", [])) | set(head.get("uses", [])):
                names.setdefault(name, []).append(hit.key)
        common = max(3, int(0.4 * len(first_of_page)))
        for keys in names.values():
            pages = {self._page_of(k) for k in keys}
            if 1 < len(pages) <= common:  # a name found in almost every hit connects nothing
                for key in keys[1:]:
                    union(key, keys[0])
        groups: dict[str, list[Hit]] = {}
        for hit in hits:
            groups.setdefault(find(hit.key), []).append(hit)

        # One researcher's seeds must fit its context beside its own reading and report.
        seed_budget = int((st.llm_context_tokens - st.subagent_report_tokens) * SEED_SHARE)
        sized: list[list[Hit]] = []
        for group in groups.values():
            current: list[Hit] = []
            size = 0
            for hit in sorted(group, key=lambda h: (self._page_of(h.key), h.key)):
                cost = est_tokens(self._payload(hit.key).get("text", ""))
                if current and size + cost > seed_budget:
                    sized.append(current)
                    current, size = [], 0
                current.append(hit)
                size += cost
            if current:
                sized.append(current)
        while len(sized) > st.max_threads:  # merge the smallest threads, same document first
            sized.sort(key=len)
            small = sized.pop(0)
            docs = {self._payload(h.key).get("document") for h in small}
            j = next((i for i, g in enumerate(sized) if docs & {self._payload(h.key).get("document") for h in g}), 0)
            sized[j] = sized[j] + small
        return sorted(sized, key=lambda g: (-max(h.score for h in g), min(h.rank for h in g)))

    # -- research rounds -------------------------------------------------------------

    async def _round(self, run: Run, tasks: list[Task], round_no: int) -> list[Report]:
        first = run.agents
        run.agents += len(tasks)
        run.emit({"type": "subagents_spawned", "starts": [self._page_of(t.seeds[0]) if t.seeds else "" for t in tasks]})
        results = await asyncio.gather(*(self._research(run, task, first + i + 1, round_no)
                                         for i, task in enumerate(tasks)), return_exceptions=True)
        reports: list[Report] = []
        for index, (task, result) in enumerate(zip(tasks, results), start=first + 1):
            if isinstance(result, Report):
                reports.append(result)
                continue
            log.warning("researcher %d failed: %s: %s", index, type(result).__name__, result)
            run.trace.setdefault("researchers", []).append({"agent": index, "round": round_no, "focus": task.focus,
                                                            "seeds": task.seeds, "error": f"{type(result).__name__}: {result}"})
            # No information loss: a failed researcher hands its seed sections on as they are.
            reports.append(Report(agent=index, round=round_no, focus=task.focus,
                                  findings="（調査員が失敗したため、手がかりの節をそのまま記載）\n\n" + self._seed_text(task),
                                  cited=[pid for pid in dedupe(self._page_of(k) for k in task.seeds) if pid in run.titles]))
        run.mark(f"round {round_no}")
        return reports

    def _seed_text(self, task: Task) -> str:
        blocks = []
        for key in task.seeds:
            p = self.store.get(key)
            if p:
                blocks.append(f"#### node_id: {p['page_id']} | {p.get('page_title', '')} › {p.get('heading', '')}\n"
                              f"{sanitize_text(p.get('text', ''))}")
        return "\n\n".join(blocks)

    async def _research(self, run: Run, task: Task, index: int, round_no: int) -> Report:
        st = run.settings
        async with run.research, self.llm_slots:  # a researcher holds one LLM slot for its whole life
            first = self._payload(task.seeds[0]) if task.seeds else {}
            run.emit({"type": "subagent_start", "agent": index,
                      "node": {"id": first.get("page_id", ""), "title": first.get("page_title", "")}})
            ctx: dict[str, Any] = {"finished": None, "read": set()}
            model = chat_model(st, st.subagent_report_tokens)
            agent = create_react_agent(model, tools=self._tools(run, index, ctx), prompt=RESEARCHER_PROMPT)
            prompt = [f"質問: {run.question}"]
            if task.focus:
                prompt.append(f"あなたの担当課題（前の調査で残った問い）: {task.focus}")
            prompt.append("担当の手がかり（事前に見つかった節）:\n\n" + self._seed_text(task))
            try:
                state = await agent.ainvoke({"messages": [HumanMessage("\n\n".join(prompt))]},
                                            config={"recursion_limit": st.subagent_max_steps * 2 + 6,
                                                    "callbacks": [run.usage]})
            except GraphRecursionError:
                state = {"messages": []}
            run.llm_calls += sum(1 for m in state.get("messages", []) if getattr(m, "type", "") == "ai")
            finished = ctx["finished"]
            if finished is None:
                finished = {"findings": await self._report_now(run, model, state), "open_questions": [],
                            "cited_node_ids": []}
        findings = sanitize_text(finished.get("findings") or "")
        cited = dedupe([clean_ref(x) for x in finished.get("cited_node_ids") or []] + PAGE_ID_RE.findall(findings))
        report = Report(agent=index, round=round_no, focus=task.focus, findings=findings,
                        open_questions=[str(q).strip() for q in finished.get("open_questions") or [] if str(q).strip()],
                        cited=[pid for pid in cited if pid in run.titles])
        run.emit({"type": "subagent_done", "agent": index, "cited": report.cited, "report": findings[:PREVIEW]})
        run.trace.setdefault("researchers", []).append({
            "agent": index, "round": round_no, "focus": task.focus, "seeds": task.seeds,
            "read": sorted(ctx["read"]), "tool_calls": sum(isinstance(m, ToolMessage) for m in state.get("messages", [])),
            "called_finish": ctx["finished"] is not None, "findings": findings,
            "open_questions": report.open_questions, "cited": report.cited})
        return report

    async def _report_now(self, run: Run, model: Any, state: dict[str, Any]) -> str:
        """The researcher ended without finish (step limit, or plain text): keep what it learned."""
        messages = state.get("messages", [])
        last = messages[-1] if messages else None
        text = _content_text(getattr(last, "content", "")).strip()
        if text and getattr(last, "type", "") == "ai" and not getattr(last, "tool_calls", None) \
                and "need more steps" not in text:
            return text
        reads = "\n\n".join(f"[{m.name}]\n{_content_text(m.content)}" for m in messages if isinstance(m, ToolMessage))
        reply = await model.ainvoke([SystemMessage(REPORT_NOW_PROMPT),
                                     HumanMessage(f"質問: {run.question}\n\n読んだ内容:\n{reads}")],
                                    config={"callbacks": [run.usage]})
        run.llm_calls += 1
        return _content_text(reply.content).strip()

    def _tools(self, run: Run, index: int, ctx: dict[str, Any]) -> list[StructuredTool]:
        async def read(node_id: str, heading: str | None = None) -> str:
            try:
                page = await run.page(node_id)
            except GrowiAPIError as exc:
                return f"読み込みに失敗しました: {exc}"
            if page is None:
                return f"{clean_ref(node_id)} は見つかりませんでした。"
            if page.id not in ctx["read"]:
                ctx["read"].add(page.id)
                run.emit({"type": "read", "agent": index, "node": node_ref(page)})
            head = f"node_id: {page.id}\ntitle: {page.title}\npath: {page.path}\n\n"
            body = sanitize_text(page.body)
            if heading:
                part = heading_subtree(body, heading)
                if part:
                    return head + part
                outline = "\n".join(f"- {line}" for line in body.split("\n") if HEADING_RE.match(line))
                return head + f"[節「{heading}」が見つかりません] 見出し一覧:\n{outline}"
            if len(body) <= READ_CHARS:
                return head + body
            shown, rest, size = [], [], 0
            for part in h2_parts(body):
                if not rest and (not shown or size + len(part) <= READ_CHARS):
                    shown.append(part)
                    size += len(part)
                else:
                    rest.append(part.split("\n", 1)[0].lstrip("# ").strip())
            return (head + "\n".join(shown) + "\n\n(長いページのため、次の節はまだ表示していません。"
                    "read(node_id, heading=...) で読んでください: " + "、".join(rest) + ")")

        async def follow_link(node_id: str) -> str:
            try:
                page = await run.page(node_id)
            except GrowiAPIError as exc:
                return f"読み込みに失敗しました: {exc}"
            if page is None:
                return f"{clean_ref(node_id)} は見つかりませんでした。"
            links = self.links_for(page)
            run.emit({"type": "follow_link", "agent": index, "node": node_ref(page), "neighbors": len(links)})
            if not links:
                return "このページに発リンクはありません。"
            lines = []
            for link in links:
                title = (self.store.page(link.target_node_id) or {}).get("page_title") or link.label
                run.cite(link.target_node_id, title)
                lines.append(f"- node_id: `{link.target_node_id}`  {title} — {link.summary}")
            return "\n".join(lines)

        async def search(text: str) -> str:
            run.emit({"type": "search", "agent": index, "query": text})
            keys = self._section_keys(await self._search(text, 24, ("section", "fact")))[:8]
            return self._format_sections(run, keys) or "見つかりませんでした。別の言い方で検索してください。"

        async def definition(name: str) -> str:
            keys = list(self.store.defines.get(norm(name), []))
            if not keys:
                rows = await self._search(name, 24, ("section", "fact"))
                keys = [k for k in self._section_keys(rows) if norm(name) in norm(self._payload(k).get("text", ""))]
            keys = dedupe(keys)[:5]
            run.emit({"type": "find", "agent": index, "description": name,
                      "results": [self._page_of(k) for k in keys]})
            if not keys:
                return f"「{name}」を定義している節は見つかりませんでした。search で探してください。"
            for key in keys:
                run.cite(self._page_of(key), self._payload(key).get("page_title", ""))
            # The two likeliest definitions come with their full text: saves a read round trip.
            text = "\n\n".join(self._seed_text(Task(seeds=self.store.windows_of_section(k.rsplit("|", 1)[0])))
                               for k in keys[:2])
            return text + ("\n\nほかの候補:\n" + self._format_sections(run, keys[2:]) if keys[2:] else "")

        async def finish(findings: str, open_questions: list[str] | None = None,
                         cited_node_ids: list[str] | None = None) -> str:
            ctx["finished"] = {"findings": findings, "open_questions": open_questions or [],
                               "cited_node_ids": cited_node_ids or []}
            return "報告を受け付けました。"

        return [
            StructuredTool.from_function(coroutine=read, name="read", args_schema=ReadArgs,
                                         description="ページ本文（heading を渡すとその節だけ）を読みます。"),
            StructuredTool.from_function(coroutine=follow_link, name="follow_link", args_schema=NodeArgs,
                                         description="ページから出ているリンク先の一覧を返します。"),
            StructuredTool.from_function(coroutine=search, name="search", args_schema=SearchArgs,
                                         description="Wiki全体から関係しそうな節を検索します。"),
            StructuredTool.from_function(coroutine=definition, name="definition", args_schema=DefinitionArgs,
                                         description="関数名・用語などを定義している節を探します。"),
            StructuredTool.from_function(coroutine=finish, name="finish", args_schema=FinishArgs, return_direct=True,
                                         description="調査結果・まだ分からないこと・根拠ページIDを報告して終了します。"),
        ]

    def _format_sections(self, run: Run, keys: list[str]) -> str:
        lines = []
        for key in keys:
            p = self.store.get(key)
            if not p:
                continue
            run.cite(p["page_id"], p.get("page_title", ""))
            about = p.get("summary") or " ".join(str(p.get("text", "")).split())[:PREVIEW]
            kind = f"  [{p['kind']}]" if p.get("kind") else ""
            lines.append(f"- node_id: `{p['page_id']}`  {p.get('page_title', '')} › {p.get('heading', '')}{kind}\n  {about}")
        return "\n".join(lines)

    # -- HOW FAR dial: lead check and gap rounds ---------------------------------------

    async def _lead_check(self, run: Run, reports: list[Report]) -> tuple[bool, list[dict[str, Any]]]:
        """One call over ALL reports (split only when they cannot fit one input)."""
        st = run.settings
        head = f"質問: {run.question}\n\n"
        budget = st.llm_context_tokens - st.lead_check_tokens - est_tokens(LEAD_CHECK_PROMPT + head)
        groups = pack([r.text(run.title) for r in reports], budget)
        outputs = await asyncio.gather(*(run.complete(LEAD_CHECK_PROMPT, head + "\n\n".join(g), st.lead_check_tokens)
                                         for g in groups))
        complete, gaps = True, []
        for output in outputs:
            data = parse_json(output)
            if data is None:
                continue  # an unreadable verdict must not loop the research
            complete = complete and bool(data.get("complete", True))
            listed = data.get("gaps") if isinstance(data.get("gaps"), list) else []
            gaps += [g for g in listed if isinstance(g, dict) and str(g.get("question") or "").strip()]
        run.trace.setdefault("lead_checks", []).append({
            "calls": len(groups), "complete": complete, "gaps": gaps, "kept": min(len(gaps), st.max_gaps),
            "unreadable": sum(1 for output in outputs if parse_json(output) is None)})
        run.mark(f"lead check {len(run.trace['lead_checks'])}")
        return complete, gaps[:st.max_gaps]

    async def _gap_tasks(self, run: Run, gaps: list[dict[str, Any]], found: Findings) -> list[Task]:
        """Each gap: the sections on both sides of it plus a fresh search on its words."""
        by_page: dict[str, list[str]] = {}
        for hit in found.hits:
            by_page.setdefault(self._page_of(hit.key), []).append(hit.key)

        async def one(gap: dict[str, Any]) -> Task | None:
            question = str(gap.get("question") or "").strip()
            words = str(gap.get("search") or question).strip()
            run.emit({"type": "search", "query": words})
            refs = gap.get("seed_node_ids") or []
            seeds: list[str] = []
            for ref in [refs] if isinstance(refs, str) else refs:
                page_id = clean_ref(ref)
                seeds += by_page.get(page_id) or self.store.page_windows.get(page_id, [])[:2]
            seeds += self._section_keys(await self._search(words, 12, ("section", "fact")))[:5]
            seeds = dedupe(seeds)
            for key in seeds:
                run.cite(self._page_of(key), self._payload(key).get("page_title", ""))
            return Task(seeds=seeds, focus=question) if seeds else None

        return [task for task in await asyncio.gather(*(one(g) for g in gaps)) if task is not None]

    # -- COMPILER ----------------------------------------------------------------------

    async def _compile(self, run: Run, reports: list[Report]) -> str:
        """One stage when everything fits one input; else L1 folds, all at once, until it does.
        The final stage sees only fold outputs, never the raw reports next to them."""
        st = run.settings
        head = f"質問: {run.question}\n\n"
        level = [r.text(run.title) for r in reports]
        run.trace["compile"] = {"reports": len(level), "report_tokens": sum(est_tokens(t) for t in level), "folds": []}
        final_budget = st.llm_context_tokens - st.final_compiler_tokens - est_tokens(FINAL_PROMPT + head)
        fold_budget = st.llm_context_tokens - st.report_fold_tokens - est_tokens(FOLD_PROMPT + head)
        while sum(est_tokens(t) for t in level) > final_budget:
            groups = pack(level, fold_budget)
            folded = await asyncio.gather(*(run.complete(FOLD_PROMPT, head + "\n\n".join(g), st.report_fold_tokens)
                                            for g in groups))
            run.emit({"type": "reports_folded", "reports": len(level), "chars": sum(len(f) for f in folded)})
            run.trace["compile"]["folds"].append({"groups": len(groups), "tokens_out": sum(est_tokens(t) for t in folded),
                                                  "outputs": list(folded)})
            shrank = sum(est_tokens(t) for t in folded) < sum(est_tokens(t) for t in level)
            level = list(folded)
            if not shrank:
                break  # folding stopped shrinking: answer from what we have
        run.emit({"type": "compiling"})
        return await run.stream(FINAL_PROMPT, head + "\n\n".join(level), st.final_compiler_tokens)

    # -- answer and citations -------------------------------------------------------

    def _cited_node(self, run: Run, page_id: str) -> dict[str, str]:
        entry = self.store.page(page_id) or {}
        return {"id": page_id, "title": run.title(page_id), "path": entry.get("page_path", ""),
                "summary": entry.get("summary", "")}

    def _finish(self, run: Run, text: str, fallback: list[str]) -> AgentAnswer:
        """Check citations against the pages this run actually saw; write them canonically."""
        body, marker, citations = text.rpartition("引用:")
        if not marker:
            body, citations = text, ""
        cited = [pid for pid in dedupe(PAGE_ID_RE.findall(citations)) if pid in run.titles]
        cited = cited or [pid for pid in dedupe(PAGE_ID_RE.findall(body)) if pid in run.titles]
        cited = cited or [pid for pid in dedupe(fallback) if pid in run.titles]
        answer = body.rstrip()
        if cited:
            answer += "\n\n引用:\n\n" + "\n\n".join(f"{pid} : {run.title(pid)}" for pid in cited)
        return AgentAnswer(question=run.question, answer=sanitize_text(answer or NOT_FOUND), cited_node_ids=cited,
                           cited_nodes=[self._cited_node(run, pid) for pid in cited], steps=run.llm_calls)

    def _record(self, run: Run, answer: AgentAnswer | None) -> None:
        """One summary line per question, and the full trace file when WIKI_SEARCH_TRACE_DIR is set."""
        run.mark("end")
        find = run.trace.get("find", {})
        log.info("question done in %.1fs: route=%s scored=%d hits=%d threads=%d researchers=%d llm_calls=%d stopped=%s",
                 run.trace["seconds"]["end"], run.trace.get("route") or "-",
                 sum(len(w) for w in find.get("waves", [])), find.get("hits", 0), len(run.trace.get("threads", [])),
                 run.agents, run.llm_calls, run.stopped)
        if not self.settings.trace_dir:
            return
        run.trace.update({"llm_calls": run.llm_calls, "researchers_total": run.agents, "stopped": run.stopped,
                          "settings": {k: getattr(run.settings, k) for k in _TRACE_SETTINGS},
                          "answer": {"cited": answer.cited_node_ids, "text": answer.answer} if answer else None})
        slug = re.sub(r"\W+", "_", run.trace["question"])[:40].strip("_") or "question"
        path = Path(self.settings.trace_dir) / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(run.trace, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        except OSError as exc:
            log.info("trace write failed: %s", exc)

    def _log_usage(self, run: Run, answer: AgentAnswer | None, seconds: float) -> None:
        if not self.settings.usage_log_path:
            return
        totals = {"input_tokens": 0, "output_tokens": 0}
        for usage in run.usage.usage_metadata.values():
            totals["input_tokens"] += usage.get("input_tokens", 0) or 0
            totals["output_tokens"] += usage.get("output_tokens", 0) or 0
        try:
            with open(self.settings.usage_log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"query": run.question, "llm_calls": run.llm_calls, "researchers": run.agents,
                                         "seconds": round(seconds, 1), "answered": bool(answer and answer.answer),
                                         **totals}, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.info("usage log failed: %s", exc)
