"""H2 chunking and one-call metadata extraction."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from graph.common.hashing import short_hash
from graph.common.markdown import strip_big_tables, strip_image_media
from graph.wiki.page import fence_flags
from graph.wiki.storage import read_json, write_json_atomic, write_text_atomic

from .prompts import CHUNK_META_VERSION, chunk_meta_prompt
from .wire import ChunkBehaviour, ChunkEntity, ChunkMeta

META_MAX_OUTPUT_TOKENS = 4000


def normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


@dataclass
class RawChunk:
    ordinal: int
    heading: str
    line_start: int
    line_end: int
    text: str


@dataclass
class Chunk:
    chunk_id: str
    document: str
    team: str
    page_rel: str
    filename: str
    title: str
    ordinal: int
    heading: str
    line_start: int
    line_end: int
    text: str
    text_sha256: str
    meta: ChunkMeta = field(default_factory=ChunkMeta)

    @property
    def summary(self) -> str:
        return self.meta.summary

    @property
    def keywords(self) -> list[str]:
        return self.meta.keywords

    @property
    def entity(self) -> str:
        return self.meta.entity

    @property
    def claims(self) -> list[str]:
        return self.meta.claims

    @property
    def bridge_probe(self) -> str:
        return self.meta.bridge_probe

    @property
    def entities(self) -> list[ChunkEntity]:
        return self.meta.entities

    @property
    def behaviours(self) -> list[ChunkBehaviour]:
        return self.meta.behaviours

    @property
    def model_text(self) -> str:
        return model_text(self)


def split_page(text: str) -> list[RawChunk]:
    lines = text.splitlines()
    flags = fence_flags(lines)
    starts = [i for i, line in enumerate(lines) if not flags[i] and line.startswith("## ")]
    bounds = [0] + starts + [len(lines)]
    chunks: list[RawChunk] = []
    for s, e in zip(bounds, bounds[1:]):
        body = "\n".join(lines[s:e]).strip("\n")
        if not body.strip():
            continue
        heading = lines[s][3:].strip() if s in starts else ""
        chunks.append(RawChunk(len(chunks), heading, s + 1, e, body))
    return chunks


def model_text(chunk: RawChunk | Chunk | str) -> str:
    text = chunk if isinstance(chunk, str) else chunk.text
    marker = text.rfind("\n---\n")
    if marker >= 0 and ("前のページ" in text[marker:] or "次のページ" in text[marker:]):
        text = text[:marker]
    return strip_big_tables(strip_image_media(text))[:12000]


def chunk_id(document: str, filename: str, ordinal: int) -> str:
    return "lchunk-" + short_hash(f"{document}\0{filename}\0{ordinal}", 20)


def make_chunks(document: str, team: str, filename: str, text: str) -> list[Chunk]:
    title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ") and line[2:].strip()), Path(filename).stem)
    page_rel = f"{document}/{filename}"
    return [
        Chunk(
            chunk_id=chunk_id(document, filename, raw.ordinal), document=document, team=team,
            page_rel=page_rel, filename=filename, title=title, ordinal=raw.ordinal,
            heading=raw.heading, line_start=raw.line_start, line_end=raw.line_end,
            text=raw.text, text_sha256=short_hash(raw.text, 64),
        )
        for raw in split_page(text)
    ]


def validate_meta(meta: ChunkMeta, text: str) -> ChunkMeta:
    collapse = lambda value, limit: " ".join((value or "").split())[:limit]
    keywords: list[str] = []
    seen: set[str] = set()
    for value in meta.keywords:
        value = " ".join((value or "").strip().split())
        key = value.casefold()
        if value and key not in seen:
            seen.add(key); keywords.append(value)
        if len(keywords) == 12:
            break
    claims: list[str] = []
    seen.clear()
    for value in meta.claims:
        value = collapse(value, 1000)
        key = value.casefold()
        if value and key not in seen:
            seen.add(key); claims.append(value)
        if len(claims) == 20:
            break
    entities: list[ChunkEntity] = []
    seen_names: set[str] = set()
    for item in meta.entities:
        name = (item.name or "").strip()
        if len(name) < 2 or not item.kind.strip() or name not in text:
            continue
        key = normalize_name(name)
        if key in seen_names:
            continue
        seen_names.add(key)
        entities.append(item.model_copy(update={"name": name, "kind": item.kind.strip()}))
        if len(entities) == 20:
            break
    entity_names = {normalize_name(item.name): item.name for item in entities}
    behaviours: list[ChunkBehaviour] = []
    seen_behaviours: set[tuple[str, str, str]] = set()
    for item in meta.behaviours:
        subject = entity_names.get(normalize_name(item.subject))
        action = " ".join((item.action or "").split())[:60]
        if not subject or not action:
            continue
        obj = entity_names.get(normalize_name(item.object), "")
        key = (normalize_name(subject), normalize_name(action), normalize_name(obj))
        if key in seen_behaviours:
            continue
        seen_behaviours.add(key)
        behaviours.append(ChunkBehaviour(subject=subject, action=action, object=obj))
        if len(behaviours) == 20:
            break
    return ChunkMeta(
        summary=collapse(meta.summary, 1000), keywords=keywords, entity=collapse(meta.entity, 1000),
        claims=claims, bridge_probe=collapse(meta.bridge_probe, 1000), entities=entities,
        behaviours=behaviours,
    )


def _meta_from_json(value: dict[str, Any]) -> ChunkMeta:
    try:
        return ChunkMeta.model_validate(value)
    except Exception:
        return ChunkMeta()


def cache_by_hash(path: Path) -> dict[str, ChunkMeta]:
    data = read_json(path, default={})
    result: dict[str, ChunkMeta] = {}
    for page in data.get("pages", []):
        for item in page.get("chunks", []):
            if item.get("text_sha256"):
                result[item["text_sha256"]] = _meta_from_json(item)
    return result


def to_json(document: str, team: str, chunks: list[Chunk]) -> dict[str, Any]:
    pages: dict[str, dict[str, Any]] = {}
    for item in chunks:
        page = pages.setdefault(item.filename, {"filename": item.filename, "title": item.title, "original_sha256": "", "chunks": []})
        page["chunks"].append({
            "chunk_id": item.chunk_id, "ordinal": item.ordinal, "heading": item.heading,
            "line_start": item.line_start, "line_end": item.line_end, "text_sha256": item.text_sha256,
            **item.meta.model_dump(mode="json"),
        })
    return {"schema_version": 1, "meta_version": CHUNK_META_VERSION, "document": document, "team": team, "pages": list(pages.values())}


def snapshot_originals(doc_dir: Path) -> dict[str, str]:
    planning = Path(doc_dir) / "_planning"
    originals = planning / "pages"
    if originals.exists():
        existing = sorted(originals.glob("*.md"))
        if existing:
            return {page.name: short_hash(page.read_text(encoding="utf-8"), 64) for page in existing}
    if originals.exists():
        shutil.rmtree(originals)
    originals.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}
    for page in sorted(Path(doc_dir).glob("*.md")):
        text = page.read_text(encoding="utf-8")
        write_text_atomic(originals / page.name, text)
        result[page.name] = short_hash(text, 64)
    return result


async def describe_all(chunks: list[Chunk], *, model: Any, output_language: str, concurrency: int, cache: dict[str, ChunkMeta] | None = None, artifact_dir: Path | None = None, stop_check: Callable[[], bool] | None = None) -> tuple[int, int]:
    cache = cache or {}
    sem = asyncio.Semaphore(max(1, concurrency))
    calls = fallbacks = 0

    async def call(item: Chunk) -> tuple[Chunk, bool, bool]:
        nonlocal calls
        if item.text_sha256 in cache:
            item.meta = validate_meta(cache[item.text_sha256], item.text)
            return item, False, False
        if stop_check and stop_check():
            raise RuntimeError("linker cancelled")
        prompt = chunk_meta_prompt(page_title=item.title, heading=item.heading, document=item.document, text=item.model_text, output_language=output_language)
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            write_text_atomic(artifact_dir / f"meta-{item.ordinal}-{Path(item.filename).stem}.prompt.md", prompt.render())
        async with sem:
            calls += 1
            try:
                try:
                    result = await model.structured(ChunkMeta, prompt.messages(), max_output_tokens=META_MAX_OUTPUT_TOKENS)
                except TypeError:
                    result = await model.structured(ChunkMeta, prompt.messages())
                item.meta = validate_meta(result if isinstance(result, ChunkMeta) else ChunkMeta.model_validate(result), item.text)
                fallback = False
            except Exception as exc:
                item.meta = ChunkMeta(summary=item.model_text[:300])
                fallback = True
                if artifact_dir:
                    write_text_atomic(artifact_dir / f"meta-{item.ordinal}-{Path(item.filename).stem}-error.txt", f"{type(exc).__name__}: {exc}")
            if artifact_dir:
                write_json_atomic(artifact_dir / f"meta-{item.ordinal}-{Path(item.filename).stem}.json", item.meta)
            return item, True, fallback

    results = await asyncio.gather(*(call(item) for item in chunks))
    fallbacks = sum(1 for _, called, failed in results if called and failed)
    return calls, fallbacks


__all__ = ["Chunk", "RawChunk", "cache_by_hash", "chunk_id", "describe_all", "make_chunks", "model_text", "normalize_name", "snapshot_originals", "split_page", "to_json", "validate_meta"]
