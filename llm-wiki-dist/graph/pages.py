"""Page assembly for the optional ``ingest_mode=pages`` pipeline.

The module deliberately treats :class:`ChunkRef.body` as immutable source
material.  Models see summaries and headings only; Python owns routing,
assembly, frontmatter, and all integrity checks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Literal, Sequence

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from .core import short_hash
from .chunk import (
    ChunkPlanningError,
    ChunkSummary,
    ConceptFilePlan,
    JobCancelled,
    init_manifest,
    plan_concept_files_streaming,
    range_to_markdown,
    structured_ainvoke,
    utc_now_iso,
    write_json,
)


StopCheck = Callable[[], bool] | None


class ChunkRef(BaseModel):
    """One verbatim source slice."""

    id: str
    title: str = ""
    summary: str = ""
    topics: list[str] = Field(default_factory=list)
    source_start: int
    source_end: int
    body: str


class ShelfPage(BaseModel):
    """An empty destination page, optionally serving as a chapter."""

    id: str
    title: str
    description: str = ""
    parent_id: str | None = None
    path: str = ""
    chunk_ids: list[str] = Field(default_factory=list)
    is_chapter: bool = False


class Shelf(BaseModel):
    pages: list[ShelfPage] = Field(default_factory=list)


class RouteDecision(BaseModel):
    chunk_id: str
    page_id: str | None = None
    heading: str = ""
    confidence: float = 0.0


class StitchOp(BaseModel):
    op: Literal[
        "insert_intro",
        "set_heading",
        "insert_transition_before",
        "add_see_also",
        "mark_duplicate",
        "reorder",
    ]
    section_id: str | None = None
    text: str = ""
    target_ids: list[str] = Field(default_factory=list)


class _Topic(BaseModel):
    title: str = ""
    description: str = ""
    chunk_ids: list[str] = Field(default_factory=list)


class _LocalOutline(BaseModel):
    topics: list[_Topic] = Field(default_factory=list)


class _Chapter(BaseModel):
    title: str = ""
    description: str = ""
    pages: list[_Topic] = Field(default_factory=list)


class _ShelfTree(BaseModel):
    chapters: list[_Chapter] = Field(default_factory=list)


class _RouteResult(BaseModel):
    page_id: str | None = None
    heading: str = ""
    confidence: float = 0.0


class _StitchResult(BaseModel):
    operations: list[StitchOp] = Field(default_factory=list)


def _stop(stop_check: StopCheck) -> None:
    if stop_check and stop_check():
        raise JobCancelled("page pipeline cancelled")


def _message_pair(system: str, human: str) -> list[Any]:
    return [SystemMessage(content=system), HumanMessage(content=human)]


async def summarize_chunks(
    llm: Any,
    chunks: list[ChunkRef],
    *,
    concurrency: int = 8,
    stop_check: StopCheck = None,
) -> list[ChunkRef]:
    """Fill missing summaries/topics without ever sending a chunk body."""

    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def one(index: int, chunk: ChunkRef) -> ChunkRef:
        _stop(stop_check)
        if chunk.summary.strip() and chunk.topics:
            return chunk
        async with semaphore:
            _stop(stop_check)
            prompt = (
                "タスク: 次の技術文書断片の要約と主題語を作成してください。\n"
                "本文そのものは入力していません。与えられたタイトルだけから断定できない事実を足さず、"
                "短く具体的にしてください。\n\n"
                f"断片ID: {chunk.id}\n"
                f"タイトル: {chunk.title}\n"
                f"既存要約: {chunk.summary}\n"
                f"位置: {chunk.source_start}-{chunk.source_end}\n"
                "返す要約は日本語、topics は3〜5個です。"
            )
            raw = await structured_ainvoke(
                llm,
                ChunkSummary,
                _message_pair(
                    "あなたは技術文書の索引担当者です。構造化出力のみを返してください。",
                    prompt,
                ),
                max_output_tokens=180,
            )
            summary = ChunkSummary.model_validate(raw)
            updated = chunk.model_copy(deep=True)
            if not updated.summary.strip():
                updated.summary = summary.summary.strip()
            if not updated.topics:
                updated.topics = [item.strip() for item in summary.topics if item.strip()]
            return updated

    return list(await asyncio.gather(*(one(index, item) for index, item in enumerate(chunks))))


async def plan_shelf(
    llm: Any,
    chunks: list[ChunkRef],
    *,
    batch_size: int = 100,
    stop_check: StopCheck = None,
) -> Shelf:
    """Build the empty chapter/page shelf from summaries only."""

    _stop(stop_check)
    if any(not chunk.summary.strip() for chunk in chunks):
        chunks = await summarize_chunks(llm, chunks, stop_check=stop_check)

    batches = [chunks[i : i + batch_size] for i in range(0, len(chunks), batch_size)]
    if not batches:
        return Shelf()

    semaphore = asyncio.Semaphore(max(1, min(len(batches), 8)))

    async def outline(batch: list[ChunkRef], index: int) -> _LocalOutline:
        async with semaphore:
            _stop(stop_check)
            lines = [
                f"[{item.id}] {item.title} — {item.summary}"
                for item in batch
            ]
            prompt = (
                "タスク: 以下は資料を分割した断片の要約一覧です。5〜15個のトピックに分類してください。\n"
                "厳格な要件:\n"
                "- 各トピックに日本語の title と一行 description を付ける\n"
                "- 断片IDをちょうど1つのトピックに割り当てる\n"
                "- 位置ではなく内容で分類し、断片を落とさない\n\n"
                "断片一覧:\n" + "\n".join(lines)
            )
            raw = await structured_ainvoke(
                llm,
                _LocalOutline,
                _message_pair(
                    "あなたは技術文書の目次を設計する専門家です。構造化出力のみを返してください。",
                    prompt,
                ),
                max_output_tokens=1600,
            )
            return _LocalOutline.model_validate(raw)

    local = list(await asyncio.gather(*(outline(batch, i) for i, batch in enumerate(batches))))
    _stop(stop_check)

    outline_lines: list[str] = []
    for index, item in enumerate(local, start=1):
        outline_lines.append(f"## 案{index}")
        for topic in item.topics:
            outline_lines.append(
                f"- {topic.title}: {topic.description} (断片: {', '.join(topic.chunk_ids)})"
            )
    merge_prompt = (
        "タスク: 複数の局所目次案を1つの2階層ツリー（章 -> ページ）に統合してください。\n"
        "厳格な要件:\n"
        "- 同じ内容の別名は統合する\n"
        "- 各章には2〜15ページを置く\n"
        "- すべての断片IDを、ちょうど1つのページに残す\n"
        "- title と description は日本語\n\n"
        + "\n".join(outline_lines)
    )
    raw = await structured_ainvoke(
        llm,
        _ShelfTree,
        _message_pair(
            "あなたは技術文書全体の章立てを統合する編集者です。構造化出力のみを返してください。",
            merge_prompt,
        ),
        max_output_tokens=3000,
    )
    tree = _ShelfTree.model_validate(raw)
    expected = {chunk.id for chunk in chunks}
    seen: set[str] = set()
    pages: list[ShelfPage] = []
    for chapter in tree.chapters:
        chapter_title = chapter.title.strip() or "その他"
        chapter_id = short_hash(chapter_title)
        for topic in chapter.pages:
            ids = [item for item in topic.chunk_ids if item in expected and item not in seen]
            seen.update(ids)
            pages.append(
                ShelfPage(
                    id=short_hash(f"{topic.title}|{chapter_title}"),
                    title=topic.title.strip() or "その他",
                    description=topic.description.strip(),
                    parent_id=chapter_id,
                    chunk_ids=ids,
                )
            )
        if not any(page.parent_id == chapter_id for page in pages):
            continue
        pages.insert(
            len(pages) - sum(1 for page in pages if page.parent_id == chapter_id),
            ShelfPage(
                id=chapter_id,
                title=chapter_title,
                description=chapter.description.strip(),
                is_chapter=True,
            ),
        )

    # LLMs occasionally omit an ID despite the prompt. Preserve every source
    # chunk by assigning omissions to a deterministic catch-all page.
    missing = [chunk.id for chunk in chunks if chunk.id not in seen]
    if missing:
        root = next((page for page in pages if page.is_chapter), None)
        if root is None:
            root = ShelfPage(id=short_hash("その他"), title="その他", is_chapter=True)
            pages.insert(0, root)
        catch = next(
            (page for page in pages if not page.is_chapter and page.parent_id == root.id and page.title == "その他"),
            None,
        )
        if catch is None:
            catch = ShelfPage(
                id=short_hash(f"その他|{root.title}"),
                title="その他",
                description="まだ分類されていない断片。",
                parent_id=root.id,
            )
            pages.append(catch)
        catch.chunk_ids.extend(missing)

    return finalize_shelf(Shelf(pages=pages))


def finalize_shelf(shelf: Shelf) -> Shelf:
    """Assign stable paths/ids, deduplicate titles, and remove empty chapters."""

    chapters = [page for page in shelf.pages if page.is_chapter]
    leaves = [page for page in shelf.pages if not page.is_chapter]
    if leaves and not chapters:
        root = ShelfPage(
            id=short_hash("その他"),
            title="その他",
            description="資料のページ",
            is_chapter=True,
        )
        chapters = [root]
        for page in leaves:
            page.parent_id = root.id
    else:
        known_chapters = {chapter.id for chapter in chapters}
        dangling = [page for page in leaves if page.parent_id not in known_chapters]
        if dangling:
            root = next((chapter for chapter in chapters if chapter.title == "その他"), None)
            if root is None:
                root = ShelfPage(
                    id=short_hash("その他"),
                    title="その他",
                    description="分類されていない資料のページ",
                    is_chapter=True,
                )
                chapters.append(root)
            for page in dangling:
                page.parent_id = root.id
    result: list[ShelfPage] = []
    used_paths: set[str] = set()
    used_ids: set[str] = set()
    for chapter in chapters:
        children = [page for page in leaves if page.parent_id == chapter.id and page.chunk_ids]
        if not children:
            continue
        chapter_title = chapter.title.strip() or "その他"
        chapter_id = short_hash(chapter_title)
        chapter_copy = chapter.model_copy(deep=True)
        chapter_copy.id = chapter_id
        chapter_copy.path = f"/{chapter_title.strip('/') or 'その他'}"
        result.append(chapter_copy)
        used_paths.add(chapter_copy.path)
        for index, page in enumerate(children, start=1):
            title = page.title.strip() or "その他"
            base = f"/{chapter_title.strip('/')}/{title.strip('/') or 'その他'}"
            path = base
            suffix = 2
            while path in used_paths:
                path = f"{base}-{suffix}"
                suffix += 1
            page_copy = page.model_copy(deep=True)
            page_copy.id = short_hash(f"{title}|{chapter_title}")
            if page_copy.id in used_ids:
                page_copy.id = short_hash(f"{title}|{chapter_title}|{index}")
            page_copy.parent_id = chapter_id
            page_copy.path = path
            result.append(page_copy)
            used_ids.add(page_copy.id)
            used_paths.add(path)
    return Shelf(pages=result)


def _embed(embedder: Any, text: str) -> list[float]:
    if hasattr(embedder, "embed_document"):
        return list(embedder.embed_document(text))
    return list(embedder.embed_query(text))


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    denominator = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
    if not denominator:
        return 0.0
    return sum(x * y for x, y in zip(left, right)) / denominator


async def route_chunks(
    llm: Any,
    embedder: Any,
    chunks: list[ChunkRef],
    shelf: Shelf,
    *,
    candidates: int = 5,
    min_score: float = 0.25,
    concurrency: int = 8,
    stop_check: StopCheck = None,
) -> tuple[list[RouteDecision], list[ChunkRef]]:
    """Route each chunk using one cached page-description embedding matrix."""

    leaf_pages = [page for page in shelf.pages if not page.is_chapter]
    if not leaf_pages:
        return [RouteDecision(chunk_id=chunk.id) for chunk in chunks], list(chunks)

    page_vectors = [
        _embed(embedder, f"{page.title}\n{page.description}") for page in leaf_pages
    ]
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def route_one(chunk: ChunkRef) -> RouteDecision:
        async with semaphore:
            _stop(stop_check)
            vector = _embed(embedder, chunk.summary or chunk.title)
            ranked = sorted(
                (
                    (_cosine(vector, page_vector), page)
                    for page, page_vector in zip(leaf_pages, page_vectors)
                ),
                key=lambda item: item[0],
                reverse=True,
            )[: max(1, int(candidates))]
            prompt = (
                "次の断片を候補ページの1つに割り当ててください。本文は見ていません。\n\n"
                f"断片: {chunk.id}\nタイトル: {chunk.title}\n要約: {chunk.summary}\n\n"
                "候補:\n"
                + "\n".join(
                    f"[{page.id}] {page.title} — {page.description} (score={score:.3f})"
                    for score, page in ranked
                )
                + "\nどれも適さなければ page_id は null にしてください。"
            )
            try:
                raw = await structured_ainvoke(
                    llm,
                    _RouteResult,
                    _message_pair(
                        "あなたは文書断片の振り分け担当です。構造化出力のみを返してください。",
                        prompt,
                    ),
                    max_output_tokens=120,
                )
                decision = _RouteResult.model_validate(raw)
            except Exception:
                return RouteDecision(chunk_id=chunk.id)
            score_by_id = {page.id: score for score, page in ranked}
            score = score_by_id.get(decision.page_id or "", 0.0)
            if decision.page_id not in score_by_id or score < min_score:
                return RouteDecision(
                    chunk_id=chunk.id,
                    heading=decision.heading.strip(),
                    confidence=decision.confidence,
                )
            return RouteDecision(
                chunk_id=chunk.id,
                page_id=decision.page_id,
                heading=decision.heading.strip() or chunk.title,
                confidence=max(0.0, min(1.0, decision.confidence)),
            )

    decisions = list(await asyncio.gather(*(route_one(chunk) for chunk in chunks)))
    by_id = {chunk.id: chunk for chunk in chunks}
    routed_ids = {decision.chunk_id for decision in decisions if decision.page_id}
    if len(routed_ids) != len(decisions):
        parked = [by_id[decision.chunk_id] for decision in decisions if not decision.page_id]
    else:
        parked = []
    if len({decision.chunk_id for decision in decisions}) != len(chunks):
        raise RuntimeError("page router produced duplicate or missing chunk decisions")
    return decisions, parked


async def absorb_parked(
    llm: Any,
    parked: list[ChunkRef],
    shelf: Shelf,
    stop_check: StopCheck = None,
) -> tuple[Shelf, list[RouteDecision]]:
    """Give parked chunks a final conservative pass, then keep every chunk."""

    if not parked:
        return shelf, []
    decisions, still_parked = await route_chunks(
        llm,
        _SummaryEmbedder(parked),
        parked,
        shelf,
        candidates=max(1, min(5, len([p for p in shelf.pages if not p.is_chapter]))),
        min_score=0.0,
        concurrency=4,
        stop_check=stop_check,
    )
    target = next((page for page in shelf.pages if not page.is_chapter and page.title == "その他"), None)
    if target is None:
        chapter = next((page for page in shelf.pages if page.is_chapter), None)
        if chapter is None:
            chapter = ShelfPage(id=short_hash("その他"), title="その他", is_chapter=True, path="/その他")
            shelf.pages.insert(0, chapter)
        target = ShelfPage(
            id=short_hash(f"その他|{chapter.title}"),
            title="その他",
            description="未分類の資料断片。",
            parent_id=chapter.id,
            path=f"{chapter.path}/その他",
        )
        shelf.pages.append(target)
    for decision in decisions:
        if not decision.page_id:
            decision.page_id = target.id
            decision.heading = decision.heading or "その他"
            target.chunk_ids.append(decision.chunk_id)
    return shelf, decisions


class _SummaryEmbedder:
    """Fallback embedder for a second routing pass without constructing a model."""

    def __init__(self, chunks: list[ChunkRef]):
        self._terms: dict[str, set[str]] = {
            chunk.id: set((chunk.summary + " " + " ".join(chunk.topics)).lower().split())
            for chunk in chunks
        }

    def embed_document(self, text: str) -> list[float]:
        terms = set(text.lower().split())
        vocabulary = sorted(set().union(*self._terms.values(), terms))
        return [1.0 if word in terms else 0.0 for word in vocabulary]


def _page_line_count(page: ShelfPage, chunks_by_id: dict[str, ChunkRef]) -> int:
    return sum(
        max(0, chunk.source_end - chunk.source_start + 1)
        for chunk_id in page.chunk_ids
        if (chunk := chunks_by_id.get(chunk_id)) is not None
    )


def size_pages(shelf: Shelf, chunks_by_id: dict[str, ChunkRef], settings: Any) -> Shelf:
    """Merge undersized pages and split oversized pages only at chunk boundaries."""

    pages = [page.model_copy(deep=True) for page in shelf.pages]
    leaves = [page for page in pages if not page.is_chapter]
    min_chunks = max(1, int(getattr(settings, "page_min_chunks", 3)))
    max_chunks = max(min_chunks, int(getattr(settings, "page_max_chunks", 8)))
    min_lines = max(1, int(getattr(settings, "page_min_lines", 150)))
    max_lines = max(min_lines, int(getattr(settings, "page_max_lines", 900)))

    expanded: list[ShelfPage] = []
    for page in leaves:
        current: list[str] = []
        current_lines = 0
        for chunk_id in page.chunk_ids:
            chunk = chunks_by_id[chunk_id]
            chunk_lines = max(1, chunk.source_end - chunk.source_start + 1)
            if current and (len(current) >= max_chunks or current_lines + chunk_lines > max_lines):
                expanded.append(
                    page.model_copy(
                        update={
                            "id": short_hash(f"{page.id}|{len(expanded)}"),
                            "title": page.title if not current else f"{page.title} {len(expanded) + 1}",
                            "chunk_ids": current,
                        }
                    )
                )
                current, current_lines = [], 0
            current.append(chunk_id)
            current_lines += chunk_lines
        if current:
            expanded.append(page.model_copy(update={"chunk_ids": current}))

    # Merge pages that violate the lower bounds into the nearest sibling that
    # still has room. If no sibling has room, keeping the atomic page is safer
    # than exceeding a hard maximum and losing a chunk.
    changed = True
    while changed:
        changed = False
        for page in list(expanded):
            if len(page.chunk_ids) >= min_chunks and _page_line_count(page, chunks_by_id) >= min_lines:
                continue
            siblings = [candidate for candidate in expanded if candidate is not page and candidate.parent_id == page.parent_id]
            siblings.sort(key=lambda candidate: abs(
                (chunks_by_id[candidate.chunk_ids[0]].source_start if candidate.chunk_ids else 0)
                - (chunks_by_id[page.chunk_ids[0]].source_start if page.chunk_ids else 0)
            ))
            target = next(
                (
                    candidate
                    for candidate in siblings
                    if len(candidate.chunk_ids) + len(page.chunk_ids) <= max_chunks
                    and _page_line_count(candidate, chunks_by_id) + _page_line_count(page, chunks_by_id) <= max_lines
                ),
                None,
            )
            if target is None:
                continue
            target.chunk_ids.extend(page.chunk_ids)
            target.chunk_ids.sort(key=lambda chunk_id: chunks_by_id[chunk_id].source_start)
            expanded.remove(page)
            changed = True
            break

    # Rebuild chapter entries and stable paths after sizing.
    chapters = {page.id: page for page in pages if page.is_chapter}
    return finalize_shelf(Shelf(pages=[*chapters.values(), *expanded]))


def _marker(chunk: ChunkRef) -> str:
    digest = hashlib.sha1(chunk.body.encode("utf-8")).hexdigest()[:8]
    return f"<!-- chunk: {chunk.id} lines {chunk.source_start}-{chunk.source_end} hash:{digest} -->"


def assemble_page(
    page: ShelfPage,
    decisions: Sequence[RouteDecision],
    chunks_by_id: dict[str, ChunkRef],
) -> str:
    decision_by_chunk = {decision.chunk_id: decision for decision in decisions if decision.page_id == page.id}
    chunks = [chunks_by_id[chunk_id] for chunk_id in page.chunk_ids if chunk_id in chunks_by_id]
    chunks.sort(key=lambda chunk: chunk.source_start)
    sections: list[str] = []
    for chunk in chunks:
        decision = decision_by_chunk.get(chunk.id)
        heading = (decision.heading if decision else chunk.title).strip() or "内容"
        heading = heading.removeprefix("## ").strip()
        sections.append(f"## {heading}\n\n{_marker(chunk)}\n{chunk.body}")
    return "\n\n".join(sections)


def _safe_frontmatter(value: str) -> str:
    return value.replace("\n", " ").replace("\r", " ").replace('"', "'").strip()


def write_pages_output(
    *,
    out_dir: Path,
    shelf: Shelf,
    decisions: Sequence[RouteDecision],
    chunks_by_id: dict[str, ChunkRef],
    document_name: str,
    source_line_count: int,
    assembled_bodies: dict[str, str] | None = None,
    stitch_enabled: bool = False,
) -> SimpleNamespace:
    """Write the existing ``ingest_md_output`` contract for assembled pages."""

    docs_dir = out_dir / "docs"
    planning_dir = out_dir / "_planning"
    docs_dir.mkdir(parents=True, exist_ok=True)
    planning_dir.mkdir(parents=True, exist_ok=True)
    leaves = [page for page in shelf.pages if not page.is_chapter and page.chunk_ids]
    coverage_files: list[dict[str, Any]] = []
    metadata_files: list[dict[str, Any]] = []
    manifest = init_manifest(Path(document_name))
    manifest["planning"] = {
        "ingest_mode": "pages",
        "strategy": "shelf_route_assemble",
        "file_count": len(leaves),
        "chunk_count": len(chunks_by_id),
        "stitch_enabled": bool(stitch_enabled),
    }

    for index, page in enumerate(leaves, start=1):
        filename = f"{index:02d}-{page.title}.md"
        filename = re.sub(r"[\\/:*?\"<>|]", "-", filename)
        body = (assembled_bodies or {}).get(page.id) or assemble_page(page, decisions, chunks_by_id)
        ranges = [
            [chunks_by_id[chunk_id].source_start, chunks_by_id[chunk_id].source_end]
            for chunk_id in page.chunk_ids
            if chunk_id in chunks_by_id
        ]
        parent = next((candidate for candidate in shelf.pages if candidate.id == page.parent_id), None)
        header = parent.title if parent else "一般"
        frontmatter = "\n".join(
            [
                "---",
                f'title: "{_safe_frontmatter(page.title)}"',
                f'summary: "{_safe_frontmatter(page.description)}"',
                f'header: "{_safe_frontmatter(header)}"',
                f"source_lines: {json.dumps(json.dumps(ranges, ensure_ascii=False), ensure_ascii=False)}",
                "---",
                "",
            ]
        )
        (docs_dir / filename).write_text(frontmatter + body + "\n", encoding="utf-8")
        coverage_files.append(
            {
                "title": page.title,
                "filename": filename,
                "summary": page.description,
                "header": header,
            }
        )
        metadata_files.append({"name": filename, "header": header})
        manifest["files"].append(
            {
                "filename": filename,
                "title": page.title,
                "chunk_ids": list(page.chunk_ids),
                "source_ranges": ranges,
            }
        )

    write_json(
        planning_dir / "metadata.json",
        {
            "original_file_name": document_name,
            "inferred_file_name": document_name,
            "files": metadata_files,
        },
    )
    write_json(
        planning_dir / "coverage.json",
        {
            "source_line_count": source_line_count,
            "file_count": len(coverage_files),
            "files": coverage_files,
        },
    )
    manifest["updated_at"] = utc_now_iso()
    write_json(planning_dir / "manifest.json", manifest)
    write_json(
        planning_dir / "shelf.json",
        {"pages": [page.model_dump() for page in shelf.pages]},
    )
    write_json(
        planning_dir / "routing.json",
        {"decisions": [decision.model_dump() for decision in decisions]},
    )
    return SimpleNamespace(
        out_dir=out_dir,
        file_count=len(coverage_files),
        coverage=coverage_files,
        manifest=manifest,
    )


def assert_pages_preserve_chunks(
    shelf: Shelf,
    decisions: Sequence[RouteDecision],
    chunks_by_id: dict[str, ChunkRef],
    out_dir: Path,
) -> None:
    page_bodies = [path.read_text(encoding="utf-8") for path in sorted((out_dir / "docs").glob("*.md"))]
    for chunk_id, chunk in chunks_by_id.items():
        occurrences = sum(body.count(chunk.body) for body in page_bodies)
        if occurrences != 1:
            raise RuntimeError(
                f"chunk {chunk_id} appears {occurrences} times in assembled pages"
            )
    expected = set(chunks_by_id)
    assigned = {
        decision.chunk_id
        for decision in decisions
        if decision.page_id is not None
    }
    if assigned != expected:
        raise RuntimeError(f"page routing lost chunks: {sorted(expected - assigned)}")


def _chunk_segment_map(body: str) -> dict[str, str]:
    matches = list(re.finditer(r"^<!-- chunk: ([^ ]+) .*? -->\n", body, re.MULTILINE))
    return {
        match.group(1): body[match.end() : (matches[index + 1].start() if index + 1 < len(matches) else len(body))]
        for index, match in enumerate(matches)
    }


def apply_stitch_ops(body: str, ops: Sequence[StitchOp]) -> str:
    """Apply only structural operations; chunk payloads are never rewritten."""

    protected = _chunk_segment_map(body)
    result = body
    for operation in ops:
        text = operation.text.strip()
        if any(segment.strip() and segment.strip() in text for segment in protected.values()):
            raise RuntimeError("stitch operation attempted to rewrite verbatim chunk text")
        if operation.op == "insert_intro" and text:
            result = text + "\n\n" + result
        elif operation.op == "add_see_also" and operation.target_ids:
            links = "\n".join(f"- {item}" for item in operation.target_ids)
            result = result.rstrip() + "\n\n## 関連ページ\n" + links + "\n"
        elif operation.op == "mark_duplicate" and operation.section_id:
            result = result.replace(
                f"<!-- chunk: {operation.section_id}",
                f"<!-- duplicate: {operation.section_id} -->\n<!-- chunk: {operation.section_id}",
                1,
            )
        elif operation.op == "set_heading" and operation.section_id and text:
            # Headings are replaced only in the prefix immediately before the
            # requested marker. This cannot touch the chunk payload.
            marker = f"<!-- chunk: {operation.section_id}"
            marker_at = result.find(marker)
            if marker_at >= 0:
                prefix = result[:marker_at]
                headings = list(re.finditer(r"^## .*?$", prefix, re.MULTILINE))
                if headings:
                    heading = headings[-1]
                    result = prefix[: heading.start()] + f"## {text}" + prefix[heading.end() :] + result[marker_at:]
        elif operation.op == "insert_transition_before" and operation.section_id and text:
            marker = f"<!-- chunk: {operation.section_id}"
            marker_at = result.find(marker)
            if marker_at >= 0:
                heading_at = result.rfind("## ", 0, marker_at)
                result = result[:heading_at] + text + "\n\n" + result[heading_at:]

    after = _chunk_segment_map(result)
    if protected != after:
        raise RuntimeError("stitch operation changed verbatim chunk text")
    return result


async def stitch_page(
    llm: Any,
    page: ShelfPage,
    section_summaries: list[dict[str, Any]],
    sibling_titles: list[str],
    stop_check: StopCheck = None,
) -> list[StitchOp]:
    _stop(stop_check)
    prompt = (
        f"ページ「{page.title}」を編集操作だけで整えてください。本文は入力していません。\n"
        f"セクション: {json.dumps(section_summaries, ensure_ascii=False)}\n"
        f"同階層の他ページ: {', '.join(sibling_titles)}\n"
        "本文の書き換え、要約、削除は禁止です。許可された操作だけを返してください。"
    )
    raw = await structured_ainvoke(
        llm,
        _StitchResult,
        _message_pair(
            "あなたはWikiページの構成編集者です。構造化出力のみを返してください。",
            prompt,
        ),
        max_output_tokens=500,
    )
    return _StitchResult.model_validate(raw).operations


async def arun_pages_pipeline(
    *,
    source_text: str,
    document_name: str,
    out_dir: Path,
    llm: Any,
    embedder: Any,
    settings: Any,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    stop_check: StopCheck = None,
) -> SimpleNamespace:
    source_lines = source_text.splitlines()
    if not source_lines:
        return write_pages_output(
            out_dir=out_dir,
            shelf=Shelf(),
            decisions=[],
            chunks_by_id={},
            document_name=document_name,
            source_line_count=0,
        )
    _stop(stop_check)
    if on_progress:
        on_progress({"stage": "ページ分割", "step": "chunking"})
    plans = await plan_concept_files_streaming(
        llm=llm,
        source_lines=source_lines,
        target_lines=100,
        max_extra=30,
        concurrency=getattr(settings, "page_route_concurrency", 8),
        stop_check=stop_check,
    )
    chunks = [
        ChunkRef(
            id=short_hash(f"{document_name}|{plan.source_start}-{plan.source_end}"),
            title=plan.title,
            summary=plan.summary,
            source_start=plan.source_start,
            source_end=plan.source_end,
            body=range_to_markdown(source_lines, [plan.source_start, plan.source_end]),
        )
        for plan in sorted(plans, key=lambda item: (item.source_start, item.source_end))
    ]
    chunks = await summarize_chunks(
        llm,
        chunks,
        concurrency=getattr(settings, "page_route_concurrency", 8),
        stop_check=stop_check,
    )
    if on_progress:
        on_progress({"stage": "ページ分割", "step": "shelf", "chunk_count": len(chunks)})
    shelf = await plan_shelf(llm, chunks, stop_check=stop_check)
    decisions, parked = await route_chunks(
        llm,
        embedder,
        chunks,
        shelf,
        candidates=getattr(settings, "page_route_candidates", 5),
        min_score=getattr(settings, "page_route_min_score", 0.25),
        concurrency=getattr(settings, "page_route_concurrency", 8),
        stop_check=stop_check,
    )
    if parked:
        shelf, recovered = await absorb_parked(llm, parked, shelf, stop_check=stop_check)
        by_id = {decision.chunk_id: decision for decision in decisions}
        by_id.update({decision.chunk_id: decision for decision in recovered})
        decisions = [by_id[chunk.id] for chunk in chunks]
    chunks_by_id = {chunk.id: chunk for chunk in chunks}
    shelf = size_pages(shelf, chunks_by_id, settings)
    # Sizing can change page ids; bind decisions to the resulting page by the
    # chunk assignment rather than silently dropping a section.
    for decision in decisions:
        if decision.page_id and not any(page.id == decision.page_id for page in shelf.pages):
            owner = next((page for page in shelf.pages if decision.chunk_id in page.chunk_ids), None)
            decision.page_id = owner.id if owner else None
    assembled: dict[str, str] = {}
    if getattr(settings, "page_stitch", False):
        for page in [item for item in shelf.pages if not item.is_chapter and item.chunk_ids]:
            ops = await stitch_page(
                llm,
                page,
                [
                    {
                        "section_id": chunk.id,
                        "heading": next((item.heading for item in decisions if item.chunk_id == chunk.id), chunk.title),
                        "summary": chunk.summary,
                    }
                    for chunk in (chunks_by_id[chunk_id] for chunk_id in page.chunk_ids)
                ],
                [item.title for item in shelf.pages if not item.is_chapter and item.parent_id == page.parent_id and item.id != page.id],
                stop_check=stop_check,
            )
            assembled[page.id] = apply_stitch_ops(assemble_page(page, decisions, chunks_by_id), ops)
    result = write_pages_output(
        out_dir=out_dir,
        shelf=shelf,
        decisions=decisions,
        chunks_by_id=chunks_by_id,
        document_name=document_name,
        source_line_count=len(source_lines),
        assembled_bodies=assembled,
        stitch_enabled=bool(getattr(settings, "page_stitch", False)),
    )
    assert_pages_preserve_chunks(shelf, decisions, chunks_by_id, out_dir)
    return result


def run_pages_pipeline(**kwargs: Any) -> SimpleNamespace:
    from .chunk import _run_async_blocking

    return _run_async_blocking(arun_pages_pipeline(**kwargs))
