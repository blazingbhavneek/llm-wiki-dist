"""`main.py index`: publish one index page per document plus a root index for growi-search.

Reads wiki/<doc>/_planning/{manifest,coverage,chunks}.json and metadata/pipeline.json.
Writes metadata/index/**.md locally and <doc>/00-目次 + <root>/00-目次 pages in GROWI.
Never touches wiki/, the ledger or _planning/, and the pages carry no chunk marker,
so sync/watch/publish/pull/trash never see them.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from graph.growi.client import GrowiClient, GrowiPage, assert_publish_path, growi_path
from graph.wiki.storage import read_json, write_text_atomic
from graph.workspace.project import Project, open_project
from publisher.ledger import load_ledger
from publisher.pipeline import _connection, _folders, _lock, _publisher

INDEX_NAME = "00-目次"  # sorts before 001-…; growi-search reads it (WIKI_INDEX_PAGE_NAME)
MARKER = '<span hidden data-llm-wiki-index="{kind}"></span>'
MAX_KEYWORDS_PER_PAGE = 12
MAX_ENTITIES_PER_PAGE = 8
MAX_ROOT_KEYWORDS = 20
_PREFIX_RE = re.compile(r"^\d+-")
_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")


def _one_line(text: Any, limit: int = 300) -> str:
    return " ".join(str(text or "").split())[:limit]


def _join(values: list[str], limit: int) -> str:
    seen: list[str] = []
    for value in values:
        clean = _one_line(value, 80)
        if clean and clean not in seen:
            seen.append(clean)
    return "、".join(seen[:limit])


def document_cards(folder: Path) -> list[dict[str, Any]]:
    """One card per published page, in page order, from the planning files."""
    planning = folder / "_planning"
    manifest = {f["filename"]: f for f in read_json(planning / "manifest.json", default={}).get("files", []) if f.get("filename")}
    coverage = {f["filename"]: f for f in read_json(planning / "coverage.json", default={}).get("files", []) if f.get("filename")}
    chunks = {p["filename"]: p for p in read_json(planning / "chunks.json", default={}).get("pages", []) if p.get("filename")}
    cards: list[dict[str, Any]] = []
    for page in sorted(folder.glob("*.md")):
        filename = page.name
        if filename.startswith("00-"):  # never index the index itself
            continue
        cov = coverage.get(_PREFIX_RE.sub("", filename), {})
        page_chunks = chunks.get(filename, {}).get("chunks", [])
        title = manifest.get(filename, {}).get("title") or cov.get("title")
        if not title:
            title = next((l[2:].strip() for l in page.read_text(encoding="utf-8").splitlines() if l.startswith("# ")), filename[:-3])
        cards.append({
            "filename": filename,
            "title": _one_line(title, 200),
            "summary": _one_line(cov.get("summary") or next((c.get("summary") for c in page_chunks if c.get("summary")), "")),
            "chapter": _one_line(cov.get("header", ""), 120),
            "keywords": [k for c in page_chunks for k in c.get("keywords", [])],
            "entities": [e["name"] for c in page_chunks for e in c.get("entities", []) if e.get("role") == "defines" and e.get("name")],
        })
    return cards


def render_document_index(title: str, cards: list[dict[str, Any]], link_for: Callable[[str], str]) -> str:
    lines = [f"# {_one_line(title, 200)}", "", MARKER.format(kind="document"), "", "ページは原文での登場順に並んでいます。", ""]
    for card in cards:
        lines.append(f"- [{card['title']}]({link_for(card['filename'])}) — {card['summary'] or '要約なし'}")
        if card["chapter"]:
            lines.append(f"  - 章: {card['chapter']}")
        if card["keywords"]:
            lines.append(f"  - キーワード: {_join(card['keywords'], MAX_KEYWORDS_PER_PAGE)}")
        if card["entities"]:
            lines.append(f"  - エンティティ: {_join(card['entities'], MAX_ENTITIES_PER_PAGE)}")
    return "\n".join(lines) + "\n"


def render_root_index(target: str, docs: list[dict[str, Any]]) -> str:
    lines = [f"# {target}", "", MARKER.format(kind="root"), "", "文書ごとの索引ページ一覧です。", ""]
    for doc in docs:
        lines.append(f"- [{doc['document']}]({doc['link']}) — {doc['summary'] or '要約なし'}")
        lines.append(f"  - ページ数: {doc['pages']}")
        if doc["keywords"]:
            lines.append(f"  - キーワード: {_join(doc['keywords'], MAX_ROOT_KEYWORDS)}")
    return "\n".join(lines) + "\n"


async def _upsert(client: GrowiClient, path: str, body: str, *, mode: str, write_path: str, root_path: str) -> GrowiPage:
    assert_publish_path(path, mode=mode, write_path=write_path, root_path=root_path)
    existing = await client.get_page(path=path)
    if existing is None:
        return await client.create_page(path, body)
    if existing.body.strip() == body.strip():
        return existing
    return await client.update_page(existing.page_id, existing.revision_id, body)


async def _delete_if_index(client: GrowiClient, path: str) -> bool:
    page = await client.get_page(path=path)
    if page is None or 'data-llm-wiki-index="' not in page.body:
        return False
    await client.delete_pages({page.page_id: page.revision_id})
    return True


def _scoped_folders(project: Project, only: list[str] | None) -> dict[str, Path]:
    folders = _folders(project)
    if only is None:
        return folders
    wanted = {project.wiki_dir(rel.strip().lstrip("/")).relative_to(project.wiki).as_posix() for rel in only}
    return {document: folder for document, folder in folders.items() if document in wanted}


def build_index(settings: Any, *, only: list[str] | None = None, publish: bool = True,
                on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    project = open_project(settings)
    connection = _connection(settings)
    publisher = _publisher(settings) if publish else None
    if publish and publisher is None:
        raise RuntimeError("GROWI_URL is required for index (use --no-publish to only write metadata/index/)")
    run_id = "idx-" + uuid.uuid4().hex[:16]
    done: list[dict[str, Any]] = []
    failures: list[str] = []
    root_docs: list[dict[str, Any]] = []
    with _lock(project):
        ledger = load_ledger(project.metadata / "pipeline.json")
        folders = _scoped_folders(project, only)
        for position, (document, folder) in enumerate(sorted(folders.items()), start=1):
            doc_path = growi_path(connection.write_path, document) if connection else f"/{document}"

            def link_for(filename: str, _doc=document, _doc_path=doc_path) -> str:
                row = ledger.published_pages.get(f"{_doc}/{filename}", {})
                return f"/{row['page_id']}" if row.get("page_id") else growi_path(_doc_path, filename)

            cards = document_cards(folder)
            body = render_document_index(Path(document).name, cards, link_for)
            write_text_atomic(project.metadata / "index" / document / "index.md", body)
            link = growi_path(doc_path, INDEX_NAME)
            if publisher is not None:
                try:
                    page = asyncio.run(_upsert(publisher.client, link, body, mode=connection.mode,
                                               write_path=connection.write_path, root_path=connection.root_path))
                    link = f"/{page.page_id}"
                except Exception as exc:  # one document must not stop the others
                    failures.append(f"{document}: {type(exc).__name__}: {exc}")
                    continue
            done.append({"document": document, "pages": len(cards), "status": "indexed" if publisher else "written"})
            if on_progress:
                on_progress({"stage": "index", "step": "document", "current": position, "total": len(folders), "document": document})
            keywords = Counter(k for card in cards for k in card["keywords"])
            root_docs.append({
                "document": document, "link": link, "pages": len(cards),
                "summary": next((c["summary"] for c in cards if c["summary"]), ""),
                "keywords": [k for k, _ in keywords.most_common(MAX_ROOT_KEYWORDS)],
            })
        target = str(settings.target_name).strip("/")
        root_body = render_root_index(target, root_docs)
        write_text_atomic(project.metadata / "index" / "index.md", root_body)
        if publisher is not None:
            try:
                asyncio.run(_upsert(publisher.client, growi_path(connection.write_path, INDEX_NAME), root_body,
                                    mode=connection.mode, write_path=connection.write_path, root_path=connection.root_path))
            except Exception as exc:
                failures.append(f"root index: {type(exc).__name__}: {exc}")
    return {"run_id": run_id, "done": done, "failures": failures}


def delete_index_pages(settings: Any) -> dict[str, Any]:
    """Remove every page carrying the index marker (called by `index --delete` and `reset`)."""
    project = open_project(settings)
    connection = _connection(settings)
    publisher = _publisher(settings)
    if publisher is None:
        raise RuntimeError("GROWI_URL is required")
    paths = [growi_path(connection.write_path, document, INDEX_NAME) for document in _folders(project)]
    paths.append(growi_path(connection.write_path, INDEX_NAME))
    deleted = [path for path in paths if asyncio.run(_delete_if_index(publisher.client, path))]
    return {"run_id": "idx-del-" + uuid.uuid4().hex[:16], "done": [{"deleted": deleted}], "failures": []}


__all__ = ["build_index", "delete_index_pages", "document_cards", "render_document_index", "render_root_index"]
