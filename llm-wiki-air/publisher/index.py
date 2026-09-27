"""`main.py index`: publish one index page per document plus a root index for growi-search.

Reads wiki/<doc>/_planning/{manifest,coverage,chunks}.json and metadata/pipeline.json.
Writes metadata/index/**.md locally and <doc>/00-目次 + <root>/00-目次 pages in GROWI.
Every publish sweep refreshes the pages it published (and trashes the index page of a
deleted document), so an explicit `index` run is only needed to repair or dry-run them.
Never touches wiki/, the ledger or _planning/, and the pages carry no chunk marker,
so sync/watch/publish/pull/trash never see them.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from graph.growi.client import GrowiClient, GrowiPage, assert_publish_path, growi_path
from graph.wiki.storage import read_json, write_text_atomic
from graph.workspace.project import Project, open_project, team_of
from publisher.ledger import Ledger, load_ledger
from publisher.pipeline import _connection, _folders, _lock, _publisher

INDEX_NAME = "00-目次"  # sorts before 001-…; growi-search reads it (WIKI_INDEX_PAGE_NAME)
MARKER = '<span hidden data-llm-wiki-index="{kind}"></span>'
MAX_KEYWORDS_PER_PAGE = 12
MAX_ENTITIES_PER_PAGE = 8
MAX_DOCUMENT_CHAPTERS = 20
MAX_DOCUMENT_KEYWORDS = 30
MAX_DOCUMENT_ENTITIES = 20
MAX_FOLDER_CONTENTS = 20
MAX_FOLDER_CHAPTERS = 15
MAX_FOLDER_KEYWORDS = 30
MAX_FOLDER_ENTITIES = 20
_PREFIX_RE = re.compile(r"^\d+-")
_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")

log = logging.getLogger(__name__)


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


def render_document_index(title: str, cards: list[dict[str, Any]], link_for: Callable[[str], str], related: list[tuple[str, str]] | None = None) -> str:
    lines = [f"# {_one_line(title, 200)}", "", MARKER.format(kind="document"), "", "ページは原文での登場順に並んでいます。", ""]
    for card in cards:
        lines.append(f"- [{card['title']}]({link_for(card['filename'])}) — {card['summary'] or '要約なし'}")
        if card["chapter"]:
            lines.append(f"  - 章: {card['chapter']}")
        if card["keywords"]:
            lines.append(f"  - キーワード: {_join(card['keywords'], MAX_KEYWORDS_PER_PAGE)}")
        if card["entities"]:
            lines.append(f"  - エンティティ: {_join(card['entities'], MAX_ENTITIES_PER_PAGE)}")
    if related:
        lines.extend(["", "## 関連文書", ""])
        lines.extend(f"- [{name}]({link})" for name, link in related)
    return "\n".join(lines) + "\n"


def _related_documents(settings: Any, folders: dict[str, Path], summaries: dict[str, dict[str, Any]], connection: Any | None) -> dict[str, list[tuple[str, str]]]:
    if getattr(settings, "wiki_linker_judge", "llm") != "jev" or not getattr(settings, "wiki_index_related_docs", False):
        return {}
    cache_path = Path(settings.data_root) / "metadata" / "index" / "relations.json"
    cache = read_json(cache_path, default={})
    decisions = cache.setdefault("decisions", {})
    tokens = {doc: {str(x).casefold() for x in summary["keywords"] + summary["entities"] if str(x).strip()}
              for doc, summary in summaries.items()}
    ranked: dict[str, list[tuple[float, str]]] = {doc: [] for doc in folders}
    teams: dict[str, list[str]] = {}
    for doc in sorted(folders):
        teams.setdefault(team_of(doc), []).append(doc)
    # ponytail: all pairs inside one team; add a keyword index if a team outgrows a few thousand documents.
    for docs in teams.values():
        for i, a in enumerate(docs):
            for b in docs[i + 1:]:
                union = tokens[a] | tokens[b]
                score = len(tokens[a] & tokens[b]) / len(union) if union else 0
                if score >= .15:
                    ranked[a].append((score, b)); ranked[b].append((score, a))
    pairs = {tuple(sorted((doc, other))) for doc, found in ranked.items()
             for _score, other in sorted(found, key=lambda pair: (-pair[0], pair[1]))[:10]}
    cards = {}
    pending = []
    for a, b in sorted(pairs):
        card_a = summaries[a]
        card_b = summaries[b]
        text_a = "\n".join([card_a["name"], card_a["scope"], *card_a["keywords"], *card_a["entities"]])
        text_b = "\n".join([card_b["name"], card_b["scope"], *card_b["keywords"], *card_b["entities"]])
        key = hashlib.sha1((text_a + "\0" + text_b).encode("utf-8")).hexdigest()
        cards[(a, b)] = (key, text_a, text_b)
        if key not in decisions:
            pending.append((key, text_a, text_b))
    if pending:
        try:
            from jev import get_engine
            engine = get_engine()
            from graph.linker.jev_judge import related_documents
            accepted = asyncio.run(related_documents(engine, pending, settings))
            decisions.update({key: key in accepted for key, _a, _b in pending})
            write_text_atomic(cache_path, json.dumps(cache, ensure_ascii=False, indent=2) + "\n")
        except Exception as exc:
            log.warning("Jev related document judge failed: %s", exc)
    output: dict[str, list[tuple[str, str]]] = {doc: [] for doc in folders}
    for (a, b), (key, _text_a, _text_b) in cards.items():
        if decisions.get(key):
            output[a].append((summaries[b]["name"], _index_link(connection, b)))
            output[b].append((summaries[a]["name"], _index_link(connection, a)))
    return output


@dataclass
class FolderNode:
    folders: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)


def folder_tree(documents: list[str]) -> dict[str, FolderNode]:
    tree = {"": FolderNode()}
    for document in documents:
        parts = Path(document).parts
        parent = ""
        for part in parts[:-1]:
            folder = f"{parent}/{part}".strip("/")
            tree.setdefault(folder, FolderNode())
            if folder not in tree[parent].folders:
                tree[parent].folders.append(folder)
            parent = folder
        tree[parent].documents.append(document)
    for node in tree.values():
        node.folders.sort()
        node.documents.sort()
    return tree


def document_summary(document: str, cards: list[dict[str, Any]]) -> dict[str, Any]:
    chapters = list(dict.fromkeys(c["chapter"] for c in cards if c.get("chapter")))
    title = _join(chapters, MAX_DOCUMENT_CHAPTERS) or _join([c["title"] for c in cards[:5]], 5) or "要約なし"
    return {"name": Path(document).name, "pages": len(cards), "chapters": chapters,
            "keywords": [k for card in cards for k in card.get("keywords", [])],
            "entities": [e for card in cards for e in card.get("entities", [])],
            "scope": _one_line(title, 300)}


def folder_summary(folder: str, tree: dict[str, FolderNode], summaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    node = tree[folder]
    documents: list[str] = []
    stack = [folder]
    while stack:
        current = stack.pop()
        documents.extend(tree[current].documents)
        stack.extend(tree[current].folders)
    documents.sort()
    document_summaries = [summaries[doc] for doc in documents]
    contents = sorted([Path(p).name for p in node.folders if p not in node.documents]
                      + [summaries[p]["name"] for p in node.documents])
    chapters = list(dict.fromkeys(chapter for summary in document_summaries for chapter in summary["chapters"]))
    keywords = Counter(value for summary in document_summaries for value in summary["keywords"])
    entities = Counter(value for summary in document_summaries for value in summary["entities"])
    return {"name": Path(folder).name if folder else "", "documents": len(documents),
            "pages": sum(summary["pages"] for summary in document_summaries), "contents": contents,
            "chapters": chapters[:MAX_FOLDER_CHAPTERS],
            "keywords": [value for value, _ in keywords.most_common(MAX_FOLDER_KEYWORDS)],
            "entities": [value for value, _ in entities.most_common(MAX_FOLDER_ENTITIES)],
            "scope": _one_line("、".join(contents), 300)}


def render_document_card(summary: dict[str, Any], link: str) -> str:
    lines = [f"- [{summary['name']}]({link}) — {summary['scope']}", "  - 種別: 文書",
             f"  - ページ数: {summary['pages']}"]
    for label, key in (("章", "chapters"), ("キーワード", "keywords"), ("エンティティ", "entities")):
        limit = {"chapters": MAX_DOCUMENT_CHAPTERS, "keywords": MAX_DOCUMENT_KEYWORDS,
                 "entities": MAX_DOCUMENT_ENTITIES}[key]
        values = summary[key]
        if key != "chapters":
            values = [value for value, _ in Counter(values).most_common(limit)]
        value = _join(values, limit)
        if value:
            lines.append(f"  - {label}: {value}")
    return "\n".join(lines)


def render_folder_card(summary: dict[str, Any], link: str, *, name_only: bool) -> str:
    if name_only:
        return f"- [{summary['name']}]({link}) — チーム\n  - 種別: フォルダ"
    lines = [f"- [{summary['name']}]({link}) — {summary['scope']}", "  - 種別: フォルダ",
             f"  - 文書数: {summary['documents']}", f"  - ページ数: {summary['pages']}"]
    contents = summary["contents"]
    if contents:
        value = _join(contents[:MAX_FOLDER_CONTENTS], MAX_FOLDER_CONTENTS)
        if len(contents) > MAX_FOLDER_CONTENTS:
            value += f"（他{len(contents) - MAX_FOLDER_CONTENTS}件）"
        lines.append(f"  - 内容: {value}")
    for label, key in (("章", "chapters"), ("キーワード", "keywords"), ("エンティティ", "entities")):
        limit = MAX_FOLDER_CHAPTERS if key == "chapters" else MAX_FOLDER_KEYWORDS if key == "keywords" else MAX_FOLDER_ENTITIES
        value = _join(summary[key], limit)
        if value:
            lines.append(f"  - {label}: {value}")
    return "\n".join(lines)


def render_folder_index(title: str, kind: str, child_cards: list[str]) -> str:
    lines = [f"# {_one_line(title, 200)}", "", MARKER.format(kind=kind), "", "このフォルダに含まれるフォルダと文書の索引です。", ""]
    for card in child_cards:
        lines.extend(card.splitlines())
    return "\n".join(lines) + "\n"


async def _upsert(client: GrowiClient, path: str, body: str, *, mode: str, write_path: str, root_path: str) -> tuple[GrowiPage, bool]:
    """Create or update one index page; the second value says whether GROWI was written."""
    assert_publish_path(path, mode=mode, write_path=write_path, root_path=root_path)
    existing = await client.get_page(path=path)
    if existing is None:
        return await client.create_page(path, body), True
    if existing.body.strip() == body.strip():
        return existing, False
    return await client.update_page(existing.page_id, existing.revision_id, body), True


async def _delete_if_index(client: GrowiClient, path: str) -> bool:
    page = await client.get_page(path=path)
    if page is None or 'data-llm-wiki-index="' not in page.body:
        return False
    await client.delete_pages({page.page_id: page.revision_id})
    return True


def _index_link(connection: Any | None, rel: str, name: str = INDEX_NAME) -> str:
    if connection:
        return growi_path(connection.write_path, rel, name)
    return growi_path(rel, name)


def _folder_cards(folder: str, tree: dict[str, FolderNode], summaries: dict[str, dict[str, Any]],
                  connection: Any | None, *, root: bool = False) -> list[str]:
    node = tree[folder]
    blocks: list[str] = []
    teams = {team_of(document) for document in summaries if team_of(document) != "general"} if root else set()
    for child in sorted(node.folders):
        # A source document can also be a parent folder. Its child folders live in
        # the document index's サブフォルダ section instead of another index page.
        if child in summaries:
            continue
        summary = folder_summary(child, tree, summaries)
        blocks.append(render_folder_card(summary, _index_link(connection, child),
                                         name_only=root and Path(child).name in teams))
    for document in sorted(node.documents, key=str):
        blocks.append(render_document_card(summaries[document], _index_link(connection, document)))
    return blocks


def _subfolder_section(document: str, tree: dict[str, FolderNode], summaries: dict[str, dict[str, Any]],
                       connection: Any | None) -> str:
    if document not in tree:
        return ""
    blocks = _folder_cards(document, tree, summaries, connection)
    return "\n## サブフォルダ\n\n" + "\n".join(blocks) + "\n" if blocks else ""


def build_index(settings: Any, *, only: list[str] | None = None, publish: bool = True,
                on_progress: Callable[[dict[str, Any]], None] | None = None,
                locked: bool = False, ledger: Ledger | None = None) -> dict[str, Any]:
    """Refresh document indexes and the containing folder index tree.

    ``only`` scopes document writes and their ancestor folder writes.
    ``locked`` and ``ledger`` let a publish sweep call this while it already holds the
    project lock, linking against the page IDs that sweep has just published.
    """
    project = open_project(settings)
    connection = _connection(settings)
    publisher = _publisher(settings) if publish else None
    if publish and publisher is None:
        raise RuntimeError("GROWI_URL is required for index (use --no-publish to only write metadata/index/)")
    run_id = "idx-" + uuid.uuid4().hex[:16]
    done: list[dict[str, Any]] = []
    failures: list[str] = []
    folders = _folders(project)
    paths = list(folders)
    tree = folder_tree(paths)
    cards_by_document = {doc: document_cards(path) for doc, path in folders.items()}
    summaries = {doc: document_summary(doc, cards_by_document[doc]) for doc in folders}
    related = _related_documents(settings, folders, summaries, connection)
    scoped = set(paths) if only is None else {
        project.wiki_dir(rel.strip().lstrip("/")).relative_to(project.wiki).as_posix() for rel in only
    }
    affected = set(tree) if only is None else {""}
    for document in scoped:
        parts = Path(document).parts
        affected.update("/".join(parts[:i]) for i in range(1, len(parts)))
    affected.update(scoped & set(tree))
    # Keep a collision document fresh when its child-folder listing changes.
    doc_scope = scoped | (set(summaries) & affected)
    if getattr(settings, "wiki_linker_judge", "llm") == "jev" and getattr(settings, "wiki_index_related_docs", False):
        doc_scope = set(paths)
    total = len(doc_scope) + len(affected)
    indexed = 0
    with contextlib.nullcontext() if locked else _lock(project):
        ledger = ledger if ledger is not None else load_ledger(project.metadata / "pipeline.json")
        target = str(settings.target_name).strip("/")
        for document, folder in sorted(folders.items()):
            doc_path = growi_path(connection.write_path, document) if connection else f"/{document}"
            cards = cards_by_document[document]
            if document not in doc_scope:
                continue

            def link_for(filename: str, _doc=document, _doc_path=doc_path) -> str:
                row = ledger.published_pages.get(f"{_doc}/{filename}", {})
                return f"/{row['page_id']}" if row.get("page_id") else growi_path(_doc_path, filename)

            body = render_document_index(Path(document).name, cards, link_for, related.get(document, []))
            body += _subfolder_section(document, tree, summaries, connection)
            write_text_atomic(project.metadata / "index" / document / "index.md", body)
            status = "written"
            if publisher is not None:
                try:
                    page, changed = asyncio.run(_upsert(publisher.client, _index_link(connection, document), body, mode=connection.mode,
                                                        write_path=connection.write_path, root_path=connection.root_path))
                    status = "indexed" if changed else "unchanged"
                except Exception as exc:  # one document must not stop the others
                    failures.append(f"{document}: {type(exc).__name__}: {exc}")
                    continue
            indexed += 1
            done.append({"document": document, "pages": len(cards), "status": status})
            if on_progress:
                on_progress({"stage": "index", "step": "document", "current": indexed, "total": total, "document": document})
        for folder in sorted(affected - set(summaries), key=str):
            index_path = project.metadata / "index" / (folder if folder else "") / "index.md"
            if folder not in tree:
                index_path.unlink(missing_ok=True)
                status = "deleted"
                if publisher is not None:
                    try:
                        if asyncio.run(_delete_if_index(publisher.client, _index_link(connection, folder))):
                            status = "deleted"
                    except Exception as exc:
                        failures.append(f"folder {folder}: {type(exc).__name__}: {exc}")
                done.append({"folder": folder, "status": status})
                continue
            if folder and not tree[folder].documents and not tree[folder].folders:
                index_path.unlink(missing_ok=True)
                status = "deleted"
                if publisher is not None:
                    try:
                        asyncio.run(_delete_if_index(publisher.client, _index_link(connection, folder)))
                    except Exception as exc:
                        failures.append(f"folder {folder}: {type(exc).__name__}: {exc}")
                done.append({"folder": folder, "status": status})
                continue
            title = target if not folder else Path(folder).name
            kind = "root" if not folder else "folder"
            body = render_folder_index(title, kind,
                                       _folder_cards(folder, tree, summaries, connection, root=not folder))
            write_text_atomic(index_path, body)
            status = "written"
            try:
                if publisher is not None:
                    _, changed = asyncio.run(_upsert(
                        publisher.client, _index_link(connection, folder), body,
                        mode=connection.mode, write_path=connection.write_path, root_path=connection.root_path))
                    status = "indexed" if changed else "unchanged"
            except Exception as exc:
                failures.append(f"folder {folder}: {type(exc).__name__}: {exc}")
            done.append({"folder": folder, "status": status})
            indexed += 1
            if on_progress:
                on_progress({"stage": "index", "step": "folder", "current": indexed, "total": total, "folder": folder})
    return {"run_id": run_id, "done": done, "failures": failures}


def delete_index_pages(settings: Any) -> dict[str, Any]:
    """Remove every page carrying the index marker (called by `index --delete` and `reset`)."""
    project = open_project(settings)
    connection = _connection(settings)
    publisher = _publisher(settings)
    if publisher is None:
        raise RuntimeError("GROWI_URL is required")
    documents = list(_folders(project))
    tree = folder_tree(documents)
    paths = [growi_path(connection.write_path, document, INDEX_NAME) for document in documents]
    paths.extend(growi_path(connection.write_path, folder, INDEX_NAME)
                 for folder in tree if folder and folder not in documents)
    paths.append(growi_path(connection.write_path, INDEX_NAME))
    deleted = [path for path in paths if asyncio.run(_delete_if_index(publisher.client, path))]
    return {"run_id": "idx-del-" + uuid.uuid4().hex[:16], "done": [{"deleted": deleted}], "failures": []}


def delete_document_index(publisher: Any, document: str) -> None:
    """Trash one document's index page after its wiki folder is removed.

    Index pages carry no chunk marker, so `delete_document` leaves them behind. This is
    derived output: a failure here is logged, never raised into the deletion that caused it.
    """
    try:
        path = growi_path(publisher.connection.write_path, document, INDEX_NAME)
        asyncio.run(_delete_if_index(publisher.client, path))
    except Exception as exc:
        log.warning("index page for %s: %s: %s", document, type(exc).__name__, exc)


__all__ = ["FolderNode", "build_index", "delete_document_index", "delete_index_pages", "document_cards", "document_summary", "folder_summary", "folder_tree", "render_document_card", "render_document_index", "render_folder_card", "render_folder_index"]
