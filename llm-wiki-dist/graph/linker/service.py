"""End-to-end linker orchestration."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from graph.common.hashing import short_hash
from graph.wiki.storage import read_json, write_json_atomic

from . import chunks
from .catalog import Catalog, LinkerModeMismatch
from .legacy import Candidate
from .prompts import CHUNK_META_VERSION, EDGE_VERSION_LEGACY, EDGE_VERSION_NEO
from .render import RenderEdge, render_page, write_if_changed

Progress = Callable[[dict[str, Any]], None] | None
StopCheck = Callable[[], bool] | None


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


def _row_chunk(row: Any) -> Any:
    return SimpleNamespace(
        chunk_id=row["chunk_id"], document=row["document"], team=row["team"], page_rel=row["page_rel"],
        filename=row["page_rel"].rsplit("/", 1)[-1], title=row["page_rel"].rsplit("/", 1)[-1], ordinal=row["ordinal"],
        heading=row["heading"], line_start=row["line_start"], line_end=row["line_end"], text=row["body"],
        text_sha256=row["text_sha256"], meta=_row_meta(row), model_text=row["body"],
    )


def _edge_id(a: str, b: str) -> str:
    return "ledge-" + short_hash("\0".join(sorted((a, b))), 20)


def _edge_from_row(row: Any, page_rel: str) -> RenderEdge:
    if row["page_a_rel"] == page_rel:
        return RenderEdge(row["edge_id"], row["page_b_rel"], row["page_b_title"], row["page_b_heading"], row["label"], row["summary"], True, row["source"], json.loads(row["via_json"] or "[]"))
    return RenderEdge(row["edge_id"], row["page_a_rel"], row["page_a_title"], row["page_a_heading"], row["label"], row["summary"], False, row["source"], json.loads(row["via_json"] or "[]"))


def _raw_rel(catalog: Catalog, document: str) -> str:
    row = catalog.conn.execute("SELECT raw_rel FROM documents WHERE document=?", (document,)).fetchone()
    return str(row[0]) if row and row[0] else document


async def _filter_groups(catalog: Catalog, model: Any, target: Any, candidates_: list[Candidate], mode: str, version: str, artifact_dir: Path | None, stop_check: StopCheck, output_language: str = "") -> tuple[list[dict[str, Any]], int]:
    from .legacy import EDGE_GROUP_SIZE
    from .prompts import legacy_edge_messages, neo_edge_messages
    from .wire import EdgeSuggestions, NeoEdgeSuggestions

    accepted: list[dict[str, Any]] = []
    calls = 0
    for offset in range(0, len(candidates_), EDGE_GROUP_SIZE):
        if stop_check and stop_check():
            raise LinkerCancelled("cancelled during edge filtering")
        group = candidates_[offset : offset + EDGE_GROUP_SIZE]
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
            write_text_atomic(artifact_dir / f"edge-{target.filename}-{offset // EDGE_GROUP_SIZE}.prompt.md", "\n".join(str(getattr(message, "content", message)) for message in messages))
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
                write_text_atomic(artifact_dir / f"edge-{target.filename}-{offset // EDGE_GROUP_SIZE}-error.txt", f"{type(exc).__name__}: {exc}")
            continue
        allowed = {item.chunk_id for item, _row in pairs}
        for suggestion in result.edges:
            target_id = getattr(suggestion, "target_node_id", None) or getattr(suggestion, "target_chunk_id", None)
            if target_id not in allowed or target_id == target.chunk_id:
                continue
            item = next(item for item, _row in pairs if item.chunk_id == target_id)
            label = str(suggestion.label).strip() or "related"
            summary = str(suggestion.summary).strip()
            if mode == "neo" and not summary:
                continue
            accepted.append({"chunk_a": target.chunk_id, "chunk_b": target_id, "label": label, "summary": summary, "source": item.source, "via": item.via})
    return accepted, calls


async def link_document(project: Any, rel: str, *, model: Any, embedder: Any, settings: Any, on_progress: Progress = None, stop_check: StopCheck = None) -> LinkResult:
    started = time.monotonic()
    document = _document(project, rel)
    team = _team(document)
    mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
    if mode not in {"legacy", "neo"}:
        raise ValueError("wiki_linker_mode must be legacy or neo")
    planning = Path(project.wiki_dir(rel)) / "_planning"
    planning.mkdir(parents=True, exist_ok=True)
    run_id = "lrun-" + uuid.uuid4().hex[:20]
    # A document without a complete marker (first run, failed run, rebuild) gets a
    # candidate pass for every chunk, even ones the catalog already knows from
    # bootstrapping; metadata is still reused through the chunks.json cache.
    previously_complete = read_json(planning / "linker.json", default={}).get("status") == "complete"
    write_json_atomic(planning / "linker.json", {"schema_version": 2, "status": "pending", "mode": mode, "run_id": run_id})
    if on_progress:
        on_progress({"stage": "linker", "step": "pending", "document": rel})
    catalog: Catalog | None = None
    try:
        catalog = Catalog.open(project.linker_database, mode=mode)
        with catalog.lock(project):
            catalog.sync_from_planning(project, skip_document=document)
            chunk_cache_path = planning / "chunks.json"
            refresh_metadata = model is not None and read_json(chunk_cache_path, default={}).get("meta_version") != CHUNK_META_VERSION
            previous_cache = chunks.cache_by_hash(chunk_cache_path)
            original_hashes = chunks.snapshot_originals(project.wiki_dir(rel))
            all_chunks: list[chunks.Chunk] = []
            for page in sorted((planning / "pages").glob("*.md")):
                all_chunks.extend(chunks.make_chunks(document, team, page.name, page.read_text(encoding="utf-8")))
            old_rows = {row["chunk_id"]: row for row in catalog.chunks_for_document(document)}
            for item in all_chunks:
                if item.text_sha256 in previous_cache:
                    item.meta = previous_cache[item.text_sha256]
                elif not refresh_metadata and item.chunk_id in old_rows:
                    item.meta = _row_meta(old_rows[item.chunk_id])
            diff = catalog.reconcile(document, all_chunks, team=team, raw_rel=rel, page_hashes=original_hashes)
            if on_progress:
                on_progress({"stage": "linker", "step": "chunks", "document": rel, "current": len(all_chunks), "total": len(all_chunks)})
            stale_ids = set(diff["new"]) | set(diff["changed"])
            to_describe = [item for item in all_chunks if refresh_metadata or not previously_complete or item.chunk_id in stale_ids]
            run_dir = Path(project.state_dir(rel)) / "work" / "linker" / run_id
            meta_calls, meta_fallbacks = (0, 0)
            revised_ids: set[str] = set()
            output_language = str(getattr(settings, "wiki_output_language", "Japanese (日本語)"))
            if to_describe and model is not None:
                before_meta = {item.chunk_id: item.meta.model_dump_json() for item in all_chunks}
                meta_calls, meta_fallbacks = await chunks.describe_all(all_chunks, model=model, output_language=output_language, concurrency=int(getattr(settings, "wiki_linker_concurrency", 0) or getattr(settings, "wiki_rewrite_concurrency", 4) or 4), cache=previous_cache, artifact_dir=run_dir, stop_check=stop_check)
                revised_ids = {item.chunk_id for item in all_chunks if item.meta.model_dump_json() != before_meta[item.chunk_id]}
                to_describe = [item for item in all_chunks if item.chunk_id in stale_ids or item.chunk_id in revised_ids or not previously_complete]
            chunk_data = chunks.to_json(document, team, all_chunks)
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
            catalog.embed_pending(embedder, team=team)
            candidates_for: list[tuple[chunks.Chunk, list[Candidate]]] = []
            for item in to_describe:
                if stop_check and stop_check():
                    raise LinkerCancelled("cancelled during candidates")
                if mode == "neo":
                    from .neo import candidates as find_candidates
                else:
                    from .legacy import candidates as find_candidates
                found = find_candidates(catalog, item, team=team)
                candidates_for.append((item, found))
            edge_version = EDGE_VERSION_NEO if mode == "neo" else EDGE_VERSION_LEGACY
            edge_rows: list[dict[str, Any]] = []
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
                            edge_rows.append({"chunk_a": item.chunk_id, "chunk_b": candidate.chunk_id, "label": decision["label"], "summary": decision["summary"], "source": candidate.source, "via": candidate.via})
                    elif model is not None:
                        pending.append(candidate)
                if pending:
                    unresolved.append((item, pending))
            edge_calls = 0
            for item, pending in unresolved:
                accepted, calls = await _filter_groups(catalog, model, item, pending, mode, edge_version, run_dir, stop_check, output_language)
                edge_calls += calls
                accepted_keys = {(edge["chunk_b"], edge["label"], edge["summary"]) for edge in accepted}
                for candidate in pending:
                    row = catalog.chunk(candidate.chunk_id)
                    if row is None:
                        continue
                    matches = [edge for edge in accepted if edge["chunk_b"] == candidate.chunk_id]
                    if matches:
                        edge_rows.extend(matches)
                        best = matches[0]
                        catalog.edge_decision_put(item.text_sha256, row["text_sha256"], mode, edge_version, True, best["label"], best["summary"])
                    else:
                        catalog.edge_decision_put(item.text_sha256, row["text_sha256"], mode, edge_version, False, "", "")
            inserted_edges = 0
            for edge in edge_rows:
                inserted_edges += int(catalog.insert_edge(edge, commit=False))
            catalog.conn.commit()
            touched_chunk_ids = set(diff["peers_before"]) | revised_peers
            for edge in edge_rows:
                touched_chunk_ids.update((edge["chunk_a"], edge["chunk_b"]))
            pages: set[str] = {item.page_rel for item in all_chunks}
            pages.update(filter(None, (catalog.page_of(cid) for cid in touched_chunk_ids)))
            touched_docs: set[str] = set()
            for page_rel in sorted(pages):
                original_path = Path(project.wiki) / page_rel.rsplit("/", 1)[0] / "_planning" / "pages" / page_rel.rsplit("/", 1)[1]
                if not original_path.exists():
                    continue
                rendered = render_page(original_path.read_text(encoding="utf-8"), page_rel=page_rel, edges=[_edge_from_row(row, page_rel) for row in catalog.edges_for_page(page_rel)], mode=mode)
                published = Path(project.wiki) / page_rel
                if write_if_changed(published, rendered):
                    doc = page_rel.rsplit("/", 1)[0]
                    if doc != document:
                        touched_docs.add(_raw_rel(catalog, doc))
            all_docs = {document, *{page.rsplit("/", 1)[0] for page in pages}}
            catalog.write_links_json(project, all_docs)
            complete = {"schema_version": 2, "status": "complete", "mode": mode, "meta_version": CHUNK_META_VERSION, "edge_version": edge_version, "run_id": run_id, "chunks_total": len(all_chunks), "chunks_new": len(diff["new"]) + len(diff["changed"]), "meta_calls": meta_calls, "edge_calls": edge_calls, "meta_fallbacks": meta_fallbacks, "edges_added": inserted_edges, "edges_removed": diff.get("edges_removed", 0), "touched_documents": sorted(touched_docs), "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            write_json_atomic(planning / "linker.json", complete)
            if on_progress:
                on_progress({"stage": "linker", "step": "done", "document": rel, "edges": len(edge_rows)})
            return LinkResult(sorted(touched_docs), inserted_edges, int(diff.get("edges_removed", 0)), meta_calls, edge_calls, meta_fallbacks)
    except Exception as exc:
        write_json_atomic(planning / "linker.json", {"schema_version": 2, "status": "failed", "mode": mode, "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"[:500]})
        if on_progress:
            on_progress({"stage": "linker", "step": "failed", "document": rel, "error": str(exc)[:200]})
        raise
    finally:
        if catalog is not None:
            catalog.close()


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
                for original in sorted(originals.glob("*.md")):
                    page_rel = f"{peer}/{original.name}"
                    rendered = render_page(original.read_text(encoding="utf-8"), page_rel=page_rel, edges=[_edge_from_row(row, page_rel) for row in catalog.edges_for_page(page_rel)], mode=catalog.meta("mode") or "legacy")
                    if write_if_changed(folder / original.name, rendered):
                        touched.add(_raw_rel(catalog, peer))
                catalog.write_links_json(project, [peer])
            return sorted(touched)
    finally:
        catalog.close()


__all__ = ["LinkResult", "LinkerCancelled", "LinkerModeMismatch", "link_document", "remove_document"]
