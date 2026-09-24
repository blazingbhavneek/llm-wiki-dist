"""End-to-end linker orchestration."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from graph.common.hashing import short_hash
from graph.config import app_concurrency
from graph.wiki.page import strip_reader_references
from graph.wiki.storage import read_json, write_json_atomic

from . import chunks
from .catalog import Catalog, LinkerModeMismatch
from .legacy import Candidate
from .prompts import CHUNK_META_VERSION, EDGE_VERSION_LEGACY, EDGE_VERSION_NEO
from .render import (
    BIG_DOCUMENT_LINES, INTERNAL_SUMMARY_TERMS, MAX_BIG_INLINE_ENTRIES,
    MAX_FOOTER_ENTRIES, MAX_INLINE_ENTRIES, USEFUL_LABELS, RenderEdge,
    MAX_NEO_BEHAVIOUR_INLINE_ENTRIES, footer_edges, render_page, write_if_changed,
)

Progress = Callable[[dict[str, Any]], None] | None
StopCheck = Callable[[], bool] | None
MAX_EDGE_CANDIDATES = 8
MAX_EDGES_PER_TARGET = 2
MAX_PAGE_CANDIDATES = 40


def _concurrency(settings: Any) -> int:
    return max(
        1,
        int(
            getattr(settings, "wiki_linker_concurrency", 0)
            or getattr(settings, "wiki_rewrite_concurrency", 0)
            or getattr(settings, "concurrency", app_concurrency())
        ),
    )


class LinkerCancelled(RuntimeError):
    pass


@dataclass
class LinkResult:
    touched_documents: list[str]
    edges_added: int = 0
    edges_removed: int = 0
    meta_calls: int = 0
    edge_calls: int = 0
    meta_fallbacks: int = 0
    affected_pages: list[str] | None = None

    @property
    def touched(self) -> list[str]:
        return self.touched_documents


def _document(project: Any, rel: str) -> str:
    return Path(project.wiki_dir(rel)).relative_to(project.wiki).as_posix()


def _team(document: str) -> str:
    return document.split("/", 1)[0] if "/" in document else "general"


def _row_meta(row: Any) -> Any:
    from .wire import ChunkMeta
    from .chunks import _meta_from_json
    return _meta_from_json({"summary": row["summary"], "keywords": json.loads(row["keywords_json"] or "[]"), "entity": row["entity"], "claims": json.loads(row["claims_json"] or "[]"), "bridge_probe": row["bridge_probe"], "entities": json.loads(row["entities_json"] or "[]"), "behaviours": json.loads(row["behaviours_json"] or "[]")})


def _row_chunk(row: Any) -> chunks.Chunk:
    return chunks.Chunk(
        chunk_id=row["chunk_id"], document=row["document"], team=row["team"], page_rel=row["page_rel"],
        filename=row["page_rel"].rsplit("/", 1)[-1], title=row["page_rel"].rsplit("/", 1)[-1], ordinal=row["ordinal"],
        heading=row["heading"], line_start=row["line_start"], line_end=row["line_end"], text=row["body"],
        text_sha256=row["text_sha256"], meta=_row_meta(row),
    )


def _edge_id(a: str, b: str) -> str:
    return "ledge-" + short_hash("\0".join(sorted((a, b))), 20)


def _neo_entity_edge_is_valid(catalog: Catalog, edge: dict[str, Any]) -> bool:
    """Check a stored deterministic entity edge against current chunk metadata."""

    source = str(edge["source"])
    via = json.loads(edge["via_json"] or "[]")
    if source not in {"use", "define"} or not via:
        return False
    row_a = catalog.chunk(str(edge["chunk_a"]))
    row_b = catalog.chunk(str(edge["chunk_b"]))
    if row_a is None or row_b is None:
        return False
    name = chunks.normalize_name(str(via[0]))

    def has_role(row: Any, role: str) -> bool:
        return any(
            chunks.normalize_name(str(entity.get("name", ""))) == name
            and entity.get("role") == role
            for entity in json.loads(row["entities_json"] or "[]")
        )

    role_a, role_b = ("uses", "defines") if source == "use" else ("defines", "uses")
    return has_role(row_a, role_a) and has_role(row_b, role_b)


def _edge_from_row(row: Any, page_rel: str) -> RenderEdge:
    if row["page_a_rel"] == page_rel:
        return RenderEdge(row["edge_id"], row["page_b_rel"], row["page_b_title"], row["page_b_heading"], row["label"], row["summary"], True, row["source"], json.loads(row["via_json"] or "[]"))
    return RenderEdge(row["edge_id"], row["page_a_rel"], row["page_a_title"], row["page_a_heading"], row["label"], row["summary"], False, row["source"], json.loads(row["via_json"] or "[]"))


def _raw_rel(catalog: Catalog, document: str) -> str:
    row = catalog.conn.execute("SELECT raw_rel FROM documents WHERE document=?", (document,)).fetchone()
    return str(row[0]) if row and row[0] else document


def _big_document(project: Any, document: str) -> bool:
    originals = Path(project.wiki) / document / "_planning" / "pages"
    return sum(len(page.read_text(encoding="utf-8").splitlines()) for page in originals.glob("*.md")) >= BIG_DOCUMENT_LINES


def _navigation_path(project: Any, document: str) -> Path:
    return Path(project.wiki) / document / "_planning" / "navigation.json"


def _navigation(project: Any, document: str) -> dict[str, Any]:
    return read_json(_navigation_path(project, document), default={"schema_version": 1, "pages": {}})


def _valid_choices(current: list[dict[str, Any]], edges: list[RenderEdge], *, inline_limit: int) -> list[dict[str, Any]]:
    by_id = {edge.edge_id: edge for edge in edges}
    seen_ids: set[str] = set()
    seen_pages: set[str] = set()
    inline = footer = 0
    result: list[dict[str, Any]] = []
    for raw in current:
        edge_id = str(raw.get("edge_id", ""))
        edge = by_id.get(edge_id)
        if edge is None or edge_id in seen_ids or edge.peer_page_rel in seen_pages:
            continue
        placement = "inline" if raw.get("placement") == "inline" and inline < inline_limit else "footer"
        if placement == "footer" and footer >= MAX_FOOTER_ENTRIES:
            continue
        inline += placement == "inline"
        footer += placement == "footer"
        summary = str(raw.get("summary", "")).strip() or edge.summary
        if any(term in summary for term in INTERNAL_SUMMARY_TERMS):
            summary = edge.summary
        result.append({"edge_id": edge_id, "placement": placement, "anchor": str(raw.get("anchor", "")).strip(), "summary": summary})
        seen_ids.add(edge_id)
        seen_pages.add(edge.peer_page_rel)
    return result


async def _curate_page(
    *, page_rel: str, original: str, edges: list[RenderEdge], current: list[dict[str, Any]],
    previous_candidates: list[str], model: Any, settings: Any, big_document: bool, mode: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    from .prompts import reference_plan_messages
    from .wire import PageReferencePlan

    candidates = footer_edges(edges, page_rel=page_rel, limit=None)
    inline_limit = MAX_NEO_BEHAVIOUR_INLINE_ENTRIES if mode == "neo" else MAX_BIG_INLINE_ENTRIES if big_document else MAX_INLINE_ENTRIES
    current = _valid_choices(current, candidates, inline_limit=inline_limit)
    current_ids = {choice["edge_id"] for choice in current}
    # Always show the existing choices, then the strongest remaining candidates.
    by_id = {edge.edge_id: edge for edge in candidates}
    pool = [by_id[choice["edge_id"]] for choice in current if choice["edge_id"] in by_id]
    pool.extend(edge for edge in candidates if edge.edge_id not in current_ids)
    pool = pool[:MAX_PAGE_CANDIDATES]
    candidate_ids = sorted(edge.edge_id for edge in pool)
    if candidate_ids == previous_candidates:
        return current, candidate_ids
    if not pool:
        return [], []
    if model is None:
        return (current or [{"edge_id": edge.edge_id, "placement": "footer", "anchor": "", "summary": edge.summary} for edge in pool[:3]]), candidate_ids
    current_payload = [{**choice, "target": by_id[choice["edge_id"]].peer_title} for choice in current if choice["edge_id"] in by_id]
    candidate_payload = []
    for edge in pool:
        candidate_payload.append({
            "edge_id": edge.edge_id,
            "state": "current" if edge.edge_id in current_ids else "new",
            "target_page": edge.peer_title,
            "target_section": edge.peer_heading,
            "evidence": edge.summary,
            "relation_hint": edge.label,
        })
    messages = reference_plan_messages(
        page={"path": page_rel, "content": original[:12000]}, current=current_payload,
        candidates=candidate_payload, inline_limit=inline_limit, footer_limit=MAX_FOOTER_ENTRIES,
        output_language=str(getattr(settings, "wiki_output_language", "Japanese (日本語)")),
        behaviour_only=mode == "neo",
    )
    try:
        try:
            plan = await model.structured(PageReferencePlan, messages, max_output_tokens=4000)
        except TypeError:
            plan = await model.structured(PageReferencePlan, messages)
        plan = plan if isinstance(plan, PageReferencePlan) else PageReferencePlan.model_validate(plan)
        proposed = [item.model_dump(mode="json") for item in plan.references]
        for item in proposed:
            if item.get("placement") == "inline" and str(item.get("anchor", "")).strip() not in original:
                item["placement"] = "footer"
                item["anchor"] = ""
        return _valid_choices(proposed, pool, inline_limit=inline_limit), candidate_ids
    except Exception:
        # A transient curator failure must not erase good links already visible to readers.
        return (current or [{"edge_id": edge.edge_id, "placement": "footer", "anchor": "", "summary": edge.summary} for edge in pool[:3]]), candidate_ids


async def render_pages(
    project: Any, catalog: Catalog, pages: set[str], *, model: Any, settings: Any, mode: str,
    on_progress: Progress = None,
) -> set[str]:
    from .prompts import REFERENCE_PLAN_VERSION

    concurrency = _concurrency(settings)
    semaphore = asyncio.Semaphore(concurrency)
    jobs: list[tuple[str, str, Path, str, list[RenderEdge], dict[str, Any], bool]] = []
    navigation_by_doc: dict[str, dict[str, Any]] = {}
    completed = 0
    for page_rel in sorted(pages):
        doc, filename = page_rel.rsplit("/", 1)
        original_path = Path(project.wiki) / doc / "_planning" / "pages" / filename
        if not original_path.exists():
            continue
        nav = navigation_by_doc.setdefault(doc, _navigation(project, doc))
        page_state = nav.setdefault("pages", {}).get(filename, {})
        edges = [_edge_from_row(row, page_rel) for row in catalog.edges_for_page(page_rel)]
        jobs.append((page_rel, doc, original_path, strip_reader_references(original_path.read_text(encoding="utf-8")), edges, page_state, _big_document(project, doc)))

    async def curate(job: tuple[str, str, Path, str, list[RenderEdge], dict[str, Any], bool]):
        nonlocal completed
        page_rel, _doc, _path, original, edges, state, big = job
        curation_edges = edges if mode == "legacy" else [edge for edge in edges if edge.source not in {"use", "define"}]
        async with semaphore:
            choices, candidate_ids = await _curate_page(
                page_rel=page_rel, original=original, edges=curation_edges,
                current=list(state.get("references", [])),
                previous_candidates=list(state.get("candidate_ids", [])) if state.get("version") == REFERENCE_PLAN_VERSION else [],
                model=model, settings=settings, big_document=big, mode=mode,
            )
        completed += 1
        if on_progress:
            on_progress({"stage": "linker", "step": "page_curated", "page": page_rel, "current": completed, "total": len(jobs)})
        return choices, candidate_ids

    results = await asyncio.gather(*(curate(job) for job in jobs))
    touched: set[str] = set()
    for job, (choices, candidate_ids) in zip(jobs, results):
        page_rel, doc, original_path, original, edges, _state, big = job
        navigation_by_doc[doc].setdefault("pages", {})[original_path.name] = {"version": REFERENCE_PLAN_VERSION, "candidate_ids": candidate_ids, "references": choices}
        rendered = render_page(original, page_rel=page_rel, edges=edges, mode=mode, big_document=big, choices=choices)
        if write_if_changed(Path(project.wiki) / page_rel, rendered):
            touched.add(doc)
    for doc, navigation in navigation_by_doc.items():
        navigation["schema_version"] = 1
        write_json_atomic(_navigation_path(project, doc), navigation)
    return touched


async def _filter_groups(catalog: Catalog, model: Any, target: Any, candidates_: list[Candidate], mode: str, version: str, artifact_dir: Path | None, stop_check: StopCheck, output_language: str = "", strict: bool = False) -> tuple[list[dict[str, Any]], int]:
    from .legacy import EDGE_GROUP_SIZE
    from .prompts import legacy_edge_messages, neo_edge_messages
    from .wire import EdgeSuggestions, NeoEdgeSuggestions

    accepted: list[dict[str, Any]] = []
    calls = 0
    selected = candidates_[:MAX_EDGE_CANDIDATES]
    if mode == "neo":
        entity_candidates = [candidate for candidate in candidates_ if candidate.source in {"use", "define"}]
        behaviour_candidates = [candidate for candidate in candidates_ if candidate.source not in {"use", "define"}]
        selected = entity_candidates + behaviour_candidates[:MAX_EDGE_CANDIDATES]
    for offset in range(0, len(selected), EDGE_GROUP_SIZE):
        if stop_check and stop_check():
            raise LinkerCancelled("cancelled during edge filtering")
        group = selected[offset : offset + EDGE_GROUP_SIZE]
        pairs = [(item, catalog.chunk(item.chunk_id)) for item in group]
        pairs = [(item, row) for item, row in pairs if row is not None]
        rows = [row for _item, row in pairs]
        target_row = catalog.chunk(target.chunk_id)
        if not target_row or not rows:
            continue
        target_view = {
            "id": target.chunk_id, "title": f"{target.title} › {target.heading}".rstrip(" ›"),
            "summary": target.summary, "keywords": target.keywords, "header": target.document, "body": target.model_text[:4000],
        }
        artifact_name = f"edge-{Path(target.filename).stem}-{target.ordinal}-{offset // EDGE_GROUP_SIZE}"
        if mode == "neo":
            target_view = {"chunk_id": target.chunk_id, "title": target.title, "heading": target.heading, "summary": target.summary, "entities": json.loads(target_row["entities_json"] or "[]"), "behaviours": json.loads(target_row["behaviours_json"] or "[]"), "body": target.model_text[:3000]}
            candidate_views = []
            for item, row in pairs:
                candidate_views.append({"chunk_id": item.chunk_id, "title": row["page_rel"].rsplit("/", 1)[-1], "heading": row["heading"], "summary": row["summary"], "entities": json.loads(row["entities_json"] or "[]"), "behaviours": json.loads(row["behaviours_json"] or "[]"), "via": item.via, "body": row["body"][:1000]})
            messages, schema = neo_edge_messages(target_view, candidate_views, output_language=output_language), NeoEdgeSuggestions
        else:
            candidate_views = []
            for item, row in pairs:
                candidate_views.append({"id": item.chunk_id, "title": f"{row['page_rel'].rsplit('/', 1)[-1]} › {row['heading']}".rstrip(" ›"), "summary": row["summary"], "keywords": json.loads(row["keywords_json"] or "[]"), "header": row["document"], "body": row["body"][:1200]})
            messages, schema = legacy_edge_messages(target_view, candidate_views, output_language=output_language), EdgeSuggestions
        if artifact_dir:
            from graph.wiki.storage import write_text_atomic
            write_text_atomic(artifact_dir / f"{artifact_name}.prompt.md", "\n".join(str(getattr(message, "content", message)) for message in messages))
        calls += 1
        try:
            try:
                result = await model.structured(schema, messages, max_output_tokens=2000)
            except TypeError:
                result = await model.structured(schema, messages)
            result = result if isinstance(result, schema) else schema.model_validate(result)
        except Exception as exc:
            if artifact_dir:
                from graph.wiki.storage import write_text_atomic
                write_text_atomic(artifact_dir / f"{artifact_name}-error.txt", f"{type(exc).__name__}: {exc}")
            if strict:
                raise
            continue
        allowed = {item.chunk_id for item, _row in pairs}
        for suggestion in result.edges:
            target_id = getattr(suggestion, "target_node_id", None) or getattr(suggestion, "target_chunk_id", None)
            if target_id not in allowed or target_id == target.chunk_id:
                continue
            item = next(item for item, _row in pairs if item.chunk_id == target_id)
            label = str(suggestion.label).strip() or "related"
            summary = str(suggestion.summary).strip()
            if label.lower() not in USEFUL_LABELS or not summary:
                continue
            accepted.append({"chunk_a": target.chunk_id, "chunk_b": target_id, "label": label, "summary": summary, "source": item.source, "via": item.via})
    if mode == "neo":
        entity_edges = [edge for edge in accepted if edge["source"] in {"use", "define"}]
        behaviour_edges = [edge for edge in accepted if edge["source"] not in {"use", "define"}]
        return entity_edges + behaviour_edges[:MAX_EDGES_PER_TARGET], calls
    return accepted[:MAX_EDGES_PER_TARGET], calls


async def link_document(
    project: Any, rel: str, *, model: Any, embedder: Any, settings: Any,
    on_progress: Progress = None, stop_check: StopCheck = None, render: bool = True,
    changed_pages: set[str] | None = None,
) -> LinkResult:
    started = time.monotonic()
    document = _document(project, rel)
    changed_page_rels = None
    if changed_pages:
        changed_page_rels = {
            page if page.startswith(document + "/") else f"{document}/{page}"
            for page in changed_pages
            if page.startswith(document + "/") or "/" not in page
        }
    document_changed_pages = {
        page for page in (changed_page_rels or ()) if page.startswith(document + "/")
    }
    team = _team(document)
    mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
    if mode not in {"legacy", "neo"}:
        raise ValueError("wiki_linker_mode must be legacy or neo")
    planning = Path(project.wiki_dir(rel)) / "_planning"
    planning.mkdir(parents=True, exist_ok=True)
    run_id = "lrun-" + uuid.uuid4().hex[:20]
    source_marker = read_json(planning / "source.json", default={})
    id_seed = str(source_marker.get("id_seed") or document)
    # A document without a complete marker (first run, failed run, rebuild) gets a
    # candidate pass for every chunk, even ones the catalog already knows from
    # bootstrapping; metadata is still reused through the chunks.json cache.
    previous_marker = read_json(planning / "linker.json", default={})
    previously_complete = (
        previous_marker.get("status") == "complete"
        or previous_marker.get("resume") is True
    )
    write_json_atomic(planning / "linker.json", {"schema_version": 2, "status": "pending", "mode": mode, "run_id": run_id})
    if on_progress:
        on_progress({"stage": "linker", "step": "pending", "document": rel})
    catalog: Catalog | None = None
    try:
        catalog = Catalog.open(project.linker_database, mode=mode)
        with catalog.lock(project):
            catalog.sync_from_planning(project, skip_document=document)
            chunk_cache_path = planning / "chunks.json"
            incremental_scope = changed_page_rels is not None and previously_complete
            refresh_metadata = (
                not incremental_scope
                and model is not None
                and read_json(chunk_cache_path, default={}).get("meta_version") != CHUNK_META_VERSION
            )
            previous_cache = chunks.cache_by_hash(chunk_cache_path)
            original_hashes = chunks.snapshot_originals(project.wiki_dir(rel))
            all_chunks: list[chunks.Chunk] = []
            for page in sorted((planning / "pages").glob("*.md")):
                all_chunks.extend(chunks.make_chunks(document, team, page.name, page.read_text(encoding="utf-8"), id_seed=id_seed))
            old_rows = {row["chunk_id"]: row for row in catalog.chunks_for_document(document)}
            old_edges_by_id: dict[str, dict[str, Any]] = {}
            old_page_rels: dict[str, str] = {}
            if incremental_scope and old_rows:
                old_page_rels = {
                    str(row["chunk_id"]): str(row["page_rel"])
                    for row in catalog.conn.execute("SELECT chunk_id,page_rel FROM chunks")
                }
                ids = list(old_rows)
                marks = ",".join("?" for _ in ids)
                for edge in catalog.conn.execute(
                    f"SELECT * FROM edges WHERE chunk_a IN ({marks}) OR chunk_b IN ({marks}) ORDER BY edge_id",
                    [*ids, *ids],
                ).fetchall():
                    stored = dict(edge)
                    old_edges_by_id[str(edge["edge_id"])] = stored
            for item in all_chunks:
                if item.text_sha256 in previous_cache:
                    item.meta = previous_cache[item.text_sha256]
                elif not refresh_metadata and item.chunk_id in old_rows:
                    item.meta = _row_meta(old_rows[item.chunk_id])
                    if incremental_scope:
                        item.meta = chunks.validate_meta(item.meta, item.text)
            diff = catalog.reconcile(document, all_chunks, team=team, raw_rel=rel, page_hashes=original_hashes)
            stale_ids = set(diff["new"]) | set(diff["changed"])
            if changed_page_rels is not None:
                stale_ids.intersection_update(
                    item.chunk_id for item in all_chunks if item.page_rel in changed_page_rels
                )
            if incremental_scope:
                to_describe = []
            else:
                to_describe = [item for item in all_chunks if refresh_metadata or not previously_complete or item.chunk_id in stale_ids]
            reported_chunks = len(stale_ids) if incremental_scope else len(to_describe)
            if on_progress:
                on_progress({
                    "stage": "linker", "step": "chunks", "document": rel,
                    "current": reported_chunks, "total": reported_chunks,
                    "catalog_total": len(all_chunks),
                })
            run_dir = Path(project.state_dir(rel)) / "work" / "linker" / run_id
            meta_calls, meta_fallbacks = (0, 0)
            revised_ids: set[str] = set()
            output_language = str(getattr(settings, "wiki_output_language", "Japanese (日本語)"))
            if to_describe and model is not None:
                before_meta = {item.chunk_id: item.meta.model_dump_json() for item in all_chunks}
                meta_calls, meta_fallbacks = await chunks.describe_all(to_describe, model=model, output_language=output_language, concurrency=_concurrency(settings), cache=previous_cache, artifact_dir=run_dir, stop_check=stop_check)
                revised_ids = {item.chunk_id for item in all_chunks if item.meta.model_dump_json() != before_meta[item.chunk_id]}
                if changed_page_rels is not None and not refresh_metadata:
                    to_describe = [
                        item for item in all_chunks
                        if item.chunk_id in stale_ids or item.chunk_id in revised_ids
                    ]
                else:
                    to_describe = [item for item in all_chunks if item.chunk_id in stale_ids or item.chunk_id in revised_ids or not previously_complete]
            chunk_data = chunks.to_json(document, team, all_chunks, id_seed=id_seed)
            chunk_data["raw_rel"] = rel
            for page in chunk_data["pages"]:
                page["original_sha256"] = original_hashes.get(page["filename"], "")
            write_json_atomic(planning / "chunks.json", chunk_data)
            catalog.upsert_chunks(all_chunks)
            # A rebuilt catalog has no edges for this document yet; links.json (kept
            # across republish) restores the ones whose endpoint text is unchanged.
            catalog.restore_edges(planning / "links.json")
            revised_peers = {peer for chunk_id in revised_ids for peer in catalog.edge_peers(chunk_id)}
            metadata_edges_removed = catalog.delete_edges_for(revised_ids)
            diff["edges_removed"] = int(diff.get("edges_removed", 0)) + metadata_edges_removed
            if not incremental_scope:
                catalog.embed_pending(embedder, team=team)
            changed_ids = stale_ids | revised_ids
            affected_ids = changed_ids | set(diff["removed"])
            relevant_edges = [
                edge for edge in old_edges_by_id.values()
                if str(edge["chunk_a"]) in affected_ids or str(edge["chunk_b"]) in affected_ids
            ]
            visible_edge_pages: dict[str, set[str]] = {}
            if incremental_scope and mode == "neo":
                edge_documents = {
                    page_rel.rsplit("/", 1)[0]
                    for edge in relevant_edges
                    for chunk_id in (str(edge["chunk_a"]), str(edge["chunk_b"]))
                    for page_rel in [old_page_rels.get(chunk_id, "")]
                    if page_rel
                }
                navigation_by_document = {
                    doc: _navigation(project, doc) for doc in edge_documents
                }
                for edge in relevant_edges:
                    edge_id = str(edge["edge_id"])
                    for chunk_id in (str(edge["chunk_a"]), str(edge["chunk_b"])):
                        page_rel = old_page_rels.get(chunk_id, "")
                        if not page_rel:
                            continue
                        doc, filename = page_rel.rsplit("/", 1)
                        state = navigation_by_document.get(doc, {}).get("pages", {}).get(filename, {})
                        if any(str(choice.get("edge_id")) == edge_id for choice in state.get("references", [])):
                            visible_edge_pages.setdefault(edge_id, set()).add(page_rel)

            edge_version = EDGE_VERSION_NEO if mode == "neo" else EDGE_VERSION_LEGACY
            edge_rows: list[dict[str, Any]] = []
            incremental_candidate_edges: dict[tuple[str, str], dict[str, Any]] = {}
            candidates_for: list[tuple[Any, list[Candidate]]] = []
            if incremental_scope:
                grouped: dict[str, tuple[Any, list[Candidate]]] = {}
                all_by_id = {item.chunk_id: item for item in all_chunks}
                for edge in relevant_edges:
                    if stop_check and stop_check():
                        raise LinkerCancelled("cancelled during candidates")
                    if catalog.chunk(str(edge["chunk_a"])) is None or catalog.chunk(str(edge["chunk_b"])) is None:
                        continue
                    source = str(edge["source"])
                    if mode == "neo" and source in {"use", "define"}:
                        if _neo_entity_edge_is_valid(catalog, edge):
                            edge_rows.append(edge)
                        continue
                    edge_id = str(edge["edge_id"])
                    # Neo behaviour edges that are not selected on either page are
                    # catalog-only candidates. Keep them without spending an LLM call.
                    if mode == "neo" and edge_id not in visible_edge_pages:
                        edge_rows.append(edge)
                        continue
                    target_id = str(edge["chunk_a"])
                    candidate_id = str(edge["chunk_b"])
                    target = all_by_id.get(target_id)
                    if target is None:
                        target_row = catalog.chunk(target_id)
                        if target_row is None:
                            continue
                        target = _row_chunk(target_row)
                    if catalog.chunk(candidate_id) is None:
                        continue
                    if model is None:
                        edge_rows.append(edge)
                        continue
                    group = grouped.setdefault(target_id, (target, []))[1]
                    group.append(Candidate(
                        candidate_id,
                        source,
                        json.loads(edge["via_json"] or "[]"),
                        label=str(edge["label"]),
                        summary=str(edge["summary"]),
                    ))
                    incremental_candidate_edges[(target_id, candidate_id)] = edge
                candidates_for = list(grouped.values())
            else:
                for item in to_describe:
                    if stop_check and stop_check():
                        raise LinkerCancelled("cancelled during candidates")
                    if mode == "neo":
                        from .neo import candidates as find_candidates
                        found = find_candidates(catalog, item, team=team)
                    else:
                        from .legacy import candidates as find_candidates
                        found = find_candidates(catalog, item, team=team)
                    candidates_for.append((item, found))
            if incremental_scope and on_progress:
                on_progress({
                    "stage": "linker", "step": "incremental_scope", "document": rel,
                    "changed_chunks": len(changed_ids),
                    "existing_edges": len(relevant_edges),
                    "checked_edges": sum(len(found) for _item, found in candidates_for),
                })
            unresolved: list[tuple[chunks.Chunk, list[Candidate]]] = []
            for item, found in candidates_for:
                pending: list[Candidate] = []
                for candidate in found:
                    row = catalog.chunk(candidate.chunk_id)
                    if row is None:
                        continue
                    if candidate.programmatic:
                        edge_rows.append({"chunk_a": item.chunk_id, "chunk_b": candidate.chunk_id, "label": candidate.label or "related", "summary": candidate.summary, "source": candidate.source, "via": candidate.via, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                        continue
                    decision = catalog.edge_decision_get(item.text_sha256, row["text_sha256"], mode, edge_version)
                    if decision:
                        if decision["accepted"]:
                            previous = incremental_candidate_edges.get((item.chunk_id, candidate.chunk_id))
                            edge_rows.append(previous or {"chunk_a": item.chunk_id, "chunk_b": candidate.chunk_id, "label": decision["label"], "summary": decision["summary"], "source": candidate.source, "via": candidate.via})
                    elif model is not None:
                        pending.append(candidate)
                if pending:
                    unresolved.append((item, pending))
            edge_calls = 0
            concurrency = _concurrency(settings)
            semaphore = asyncio.Semaphore(concurrency)
            completed = 0

            async def filter_target(item: chunks.Chunk, pending: list[Candidate]) -> tuple[list[dict[str, Any]], int]:
                nonlocal completed
                async with semaphore:
                    result = await _filter_groups(
                        catalog, model, item, pending, mode, edge_version,
                        run_dir, stop_check, output_language, strict=incremental_scope,
                    )
                completed += 1
                if on_progress:
                    on_progress({"stage": "linker", "step": "edge_target_done", "document": rel, "current": completed, "total": len(unresolved)})
                return result

            filtered = await asyncio.gather(*(filter_target(item, pending) for item, pending in unresolved))
            for (item, pending), (accepted, calls) in zip(unresolved, filtered):
                edge_calls += calls
                for candidate in pending:
                    row = catalog.chunk(candidate.chunk_id)
                    if row is None:
                        continue
                    matches = [edge for edge in accepted if edge["chunk_b"] == candidate.chunk_id]
                    if matches:
                        best = matches[0]
                        previous = incremental_candidate_edges.get((item.chunk_id, candidate.chunk_id))
                        if previous is not None:
                            best = previous
                        edge_rows.append(best)
                        catalog.edge_decision_put(item.text_sha256, row["text_sha256"], mode, edge_version, True, best["label"], best["summary"])
                    else:
                        catalog.edge_decision_put(item.text_sha256, row["text_sha256"], mode, edge_version, False, "", "")
            inserted_edges = 0
            for edge in edge_rows:
                inserted_edges += int(catalog.insert_edge(edge, commit=False))
            catalog.conn.commit()
            if incremental_scope:
                kept_edge_ids = {str(edge["edge_id"]) for edge in edge_rows if edge.get("edge_id")}
                changed_link_pages: set[str] = set()
                for edge in relevant_edges:
                    edge_id = str(edge["edge_id"])
                    if edge_id in kept_edge_ids:
                        continue
                    source = str(edge["source"])
                    if mode == "neo" and source in {"use", "define"}:
                        user_id = str(edge["chunk_a"] if source == "use" else edge["chunk_b"])
                        if old_page_rels.get(user_id):
                            changed_link_pages.add(old_page_rels[user_id])
                    elif mode == "neo":
                        changed_link_pages.update(visible_edge_pages.get(edge_id, set()))
                    else:
                        changed_link_pages.update(
                            old_page_rels[chunk_id]
                            for chunk_id in (str(edge["chunk_a"]), str(edge["chunk_b"]))
                            if chunk_id in old_page_rels
                        )
                touched_chunk_ids: set[str] = set()
            else:
                changed_link_pages = set()
                touched_chunk_ids = set(diff["peers_before"]) | revised_peers
                for edge in edge_rows:
                    touched_chunk_ids.update((edge["chunk_a"], edge["chunk_b"]))
            if changed_page_rels is not None and not refresh_metadata:
                pages: set[str] = {
                    item.page_rel for item in all_chunks if item.chunk_id in changed_ids
                }
            else:
                pages = {
                    item.page_rel
                    for item in all_chunks
                    if not previously_complete or item.chunk_id in changed_ids
                }
            pages.update(
                str(old_rows[chunk_id]["page_rel"])
                for chunk_id in diff["removed"]
                if chunk_id in old_rows
            )
            pages.update(document_changed_pages)
            pages.update(changed_link_pages)
            pages.update(filter(None, (catalog.page_of(cid) for cid in touched_chunk_ids)))
            touched_docs: set[str] = set()
            if render:
                rendered_docs = await render_pages(project, catalog, pages, model=model, settings=settings, mode=mode, on_progress=on_progress)
                touched_docs.update(_raw_rel(catalog, doc) for doc in rendered_docs if doc != document)
            all_docs = {
                document,
                *{page.rsplit("/", 1)[0] for page in pages},
                *{
                    page_rel.rsplit("/", 1)[0]
                    for edge in relevant_edges
                    for chunk_id in (str(edge["chunk_a"]), str(edge["chunk_b"]))
                    for page_rel in [old_page_rels.get(chunk_id, "")]
                    if page_rel
                },
            }
            catalog.write_links_json(project, all_docs)
            complete = {"schema_version": 2, "status": "complete" if render else "render_pending", "mode": mode, "scope": "incremental" if incremental_scope else "full", "meta_version": CHUNK_META_VERSION, "edge_version": edge_version, "run_id": run_id, "chunks_total": len(all_chunks), "chunks_new": len(diff["new"]) + len(diff["changed"]), "meta_calls": meta_calls, "edge_calls": edge_calls, "meta_fallbacks": meta_fallbacks, "edges_added": inserted_edges, "edges_removed": diff.get("edges_removed", 0), "touched_documents": sorted(touched_docs), "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            write_json_atomic(planning / "linker.json", complete)
            if on_progress:
                on_progress({"stage": "linker", "step": "done", "document": rel, "edges": len(edge_rows)})
            return LinkResult(sorted(touched_docs), inserted_edges, int(diff.get("edges_removed", 0)), meta_calls, edge_calls, meta_fallbacks, sorted(pages))
    except Exception as exc:
        write_json_atomic(planning / "linker.json", {"schema_version": 2, "status": "failed", "mode": mode, "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"[:500]})
        if on_progress:
            on_progress({"stage": "linker", "step": "failed", "document": rel, "error": str(exc)[:200]})
        raise
    finally:
        if catalog is not None:
            catalog.close()


async def link_documents(
    project: Any, rels: list[str], *, model: Any, embedder: Any, settings: Any,
    on_progress: Progress = None, stop_check: StopCheck = None,
    changed_pages: set[str] | None = None,
) -> LinkResult:
    """Link a batch, then curate and write every affected page exactly once."""
    aggregate = LinkResult([])
    pages: set[str] = set()
    for rel in rels:
        result = await link_document(
            project, rel, model=model, embedder=embedder, settings=settings,
            on_progress=on_progress, stop_check=stop_check, render=False,
            changed_pages=changed_pages,
        )
        pages.update(result.affected_pages or [])
        aggregate.edges_added += result.edges_added
        aggregate.edges_removed += result.edges_removed
        aggregate.meta_calls += result.meta_calls
        aggregate.edge_calls += result.edge_calls
        aggregate.meta_fallbacks += result.meta_fallbacks
    mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
    if pages:
        catalog = Catalog.open(project.linker_database, mode=mode)
        try:
            with catalog.lock(project):
                touched_docs = await render_pages(project, catalog, pages, model=model, settings=settings, mode=mode, on_progress=on_progress)
                aggregate.touched_documents = sorted(_raw_rel(catalog, doc) for doc in touched_docs)
                catalog.write_links_json(project, {page.rsplit("/", 1)[0] for page in pages})
        finally:
            catalog.close()
    for rel in rels:
        marker = Path(project.wiki_dir(rel)) / "_planning" / "linker.json"
        data = read_json(marker, default={})
        if data.get("status") == "render_pending":
            data["status"] = "complete"
            write_json_atomic(marker, data)
    aggregate.affected_pages = sorted(pages)
    return aggregate


def remove_document(project: Any, rel: str) -> list[str]:
    """Drop a document's chunks/edges and re-render every peer it was linked to."""
    document = _document(project, rel)
    if not Path(project.linker_database).exists():
        return []
    catalog = Catalog.open(project.linker_database, mode=Catalog.stored_mode(project.linker_database) or "legacy")
    try:
        with catalog.lock(project):
            peers = catalog.delete_document(document)
            touched: set[str] = set()
            for peer in peers:
                folder = Path(project.wiki) / peer
                originals = folder / "_planning" / "pages"
                big_document = _big_document(project, peer)
                navigation = _navigation(project, peer)
                for original in sorted(originals.glob("*.md")):
                    page_rel = f"{peer}/{original.name}"
                    edges = [_edge_from_row(row, page_rel) for row in catalog.edges_for_page(page_rel)]
                    page_state = navigation.setdefault("pages", {}).get(original.name, {})
                    choices = _valid_choices(list(page_state.get("references", [])), footer_edges(edges, page_rel=page_rel, limit=None), inline_limit=MAX_BIG_INLINE_ENTRIES if big_document else MAX_INLINE_ENTRIES)
                    navigation.setdefault("pages", {})[original.name] = {"candidate_ids": [], "references": choices}
                    rendered = render_page(original.read_text(encoding="utf-8"), page_rel=page_rel, edges=edges, mode=catalog.meta("mode") or "legacy", big_document=big_document, choices=choices)
                    if write_if_changed(folder / original.name, rendered):
                        touched.add(_raw_rel(catalog, peer))
                write_json_atomic(_navigation_path(project, peer), navigation)
                catalog.write_links_json(project, [peer])
            return sorted(touched)
    finally:
        catalog.close()


__all__ = ["LinkResult", "LinkerCancelled", "LinkerModeMismatch", "link_document", "link_documents", "remove_document"]
