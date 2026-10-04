"""Keep the local index in step with GROWI by following the 目次 hash tree.

    every sync_seconds, per project: read the root 目次 only
      → a child whose hash equals what we indexed is skipped with everything below it
      → walk down only into changed folders/documents
      → changed document: list its pages once, fetch only pages whose revision or metadata
        changed, re-index their sections; drop points of pages that disappeared
    every revision_sweep_seconds: list each document's pages once and re-index pages whose
    GROWI revision moved (hand edits made in GROWI between builder runs)

Nothing here uses the audit log, /pages/recent or a full relist. A failed GROWI call aborts
only this pass (nothing is deleted on a failed pass) and the next pass retries.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import markdown as md
from config import Settings
from common import mokuji_data
from growi_client import GrowiAPIError, GrowiSearchClient
from models import WikiPage
from store import Store, norm

log = logging.getLogger("growi_search_sync")

WINDOW_CHARS = 3000  # fits the embedder (ruri-v3: 8192 tokens) and one JEV request


def split_windows(text: str, limit: int = WINDOW_CHARS) -> list[str]:
    """Split a long section into windows on paragraph boundaries; never drops a line.

    A fenced block stays whole unless it alone is too long; a long Markdown table is split
    into row groups that each repeat the header row, so every window stays readable.
    """
    if len(text) <= limit:
        return [text]
    paragraphs: list[str] = []
    buffer: list[str] = []
    inside = False
    for line in text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            inside = not inside
        buffer.append(line)
        if not inside and not line.strip():
            paragraphs.append("\n".join(buffer))
            buffer = []
    if buffer:
        paragraphs.append("\n".join(buffer))
    pieces: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= limit:
            pieces.append(paragraph)
            continue
        lines = paragraph.split("\n")
        header = lines[:2] if len(lines) > 2 and lines[0].lstrip().startswith("|") else []
        chunk, size = list(header), sum(len(x) + 1 for x in header)
        for line in lines[len(header):]:
            if size + len(line) + 1 > limit and len(chunk) > len(header):
                pieces.append("\n".join(chunk))
                chunk, size = list(header), sum(len(x) + 1 for x in header)
            chunk.append(line)
            size += len(line) + 1
        if len(chunk) > len(header):
            pieces.append("\n".join(chunk))
    windows: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) + 1 > limit:
            windows.append(current)
            current = piece
        else:
            current = f"{current}\n{piece}" if current else piece
    if current.strip():
        windows.append(current)
    return [w.strip("\n") for w in windows if w.strip()] or [text]


def _match_section(ordinal: int, heading: str, records: list[dict[str, Any]], used: set[int]) -> dict[str, Any]:
    """The builder's record for this section: same ordinal and heading, else same heading."""
    for record in records:
        if record.get("ordinal") == ordinal and record.get("heading", "") == heading and id(record) not in used:
            used.add(id(record))
            return record
    for record in records:  # a hand edit in GROWI moved sections around
        if record.get("heading", "") == heading and id(record) not in used:
            used.add(id(record))
            return record
    return {}


def page_specs(project: str, doc_ref: str, doc_name: str, order: int, record: dict[str, Any],
               sections: list[dict[str, Any]], page: WikiPage) -> list[dict[str, Any]]:
    """Every point of one page: section windows, facts, and the page entry itself."""
    page_id = record["id"]
    title = record.get("title") or page.title
    base = {"project": project, "document": doc_ref, "doc_name": doc_name, "page_id": page_id,
            "page_title": title, "page_path": page.path or record.get("path", ""), "revision": page.revision_id}
    specs: list[dict[str, Any]] = []
    page_terms: list[str] = []
    used: set[int] = set()
    for ordinal, heading, text in mokuji_data.search_sections(page.body):
        meta = _match_section(ordinal, heading, sections, used)
        entities = meta.get("entities", [])
        names = [e.get("name", "") for e in entities if e.get("name")]
        defines = sorted({norm(e["name"]) for e in entities if e.get("name") and e.get("role") == "defines"})
        uses = sorted({norm(n) for n in names} - set(defines))
        terms = [*meta.get("search_terms", []), *meta.get("keywords", []), *names]
        page_terms.extend(terms)
        gist = "\n".join(x for x in [heading, meta.get("summary", ""), meta.get("kind", ""), *meta.get("points", [])] if x)
        key = f"s|{page_id}|{ordinal}"
        windows = split_windows(text)
        for w, window in enumerate(windows):
            payload = {**base, "level": "section", "section": key, "ordinal": ordinal, "window": w,
                       "windows": len(windows), "heading": heading, "text": window,
                       "summary": meta.get("summary", ""), "kind": meta.get("kind", "")}
            dense = {"text": f"{title} > {heading}\n{window}"}
            sparse = {"words": f"{heading}\n{window}"}
            if w == 0:
                payload.update({"points": meta.get("points", []), "keywords": meta.get("keywords", []),
                                "search_terms": meta.get("search_terms", []), "entities": entities,
                                "behaviours": meta.get("behaviours", []), "bridge": meta.get("bridge", ""),
                                "defines": defines, "uses": uses})
                if gist.strip():
                    dense["gist"] = f"{title} > {gist}"
                if meta.get("bridge"):
                    dense["bridge"] = meta["bridge"]
                sparse["terms"] = " ".join([title, heading, *terms])
            specs.append({"key": f"{key}|{w}", "payload": payload, "dense": dense, "sparse": sparse})
        for i, fact in enumerate(meta.get("facts", [])):
            specs.append({"key": f"f|{page_id}|{ordinal}|{i}",
                          "payload": {**base, "level": "fact", "section": key, "heading": heading, "text": fact},
                          "dense": {"text": f"{title} > {heading}: {fact}"}, "sparse": {"words": fact}})
    kinds = record.get("kind") or []
    points = record.get("points") or []
    text = "\n".join(x for x in [title, record.get("summary", ""), "、".join(kinds), *points] if x)
    specs.append({"key": f"p|{page_id}",
                  "payload": {**base, "level": "page", "order": order, "summary": record.get("summary", ""),
                              "chapter": record.get("chapter", ""), "kind": kinds, "points": points, "text": text},
                  "dense": {"text": text},
                  "sparse": {"terms": " ".join([title, record.get("chapter", ""), *kinds, *page_terms])}})
    return specs


class Sync:
    def __init__(self, client: GrowiSearchClient, settings: Settings, store: Store) -> None:
        self.client, self.settings, self.store = client, settings, store
        self.ready = False
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._roots: list[str] = []
        self._roots_at = 0.0
        self._sweep_at = time.monotonic()
        self._warned: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=settings.growi_concurrency, thread_name_prefix="index-read")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="index-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def status(self) -> dict[str, Any]:
        return {"ready": self.ready, "error": self.last_error, **self.store.counts()}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
                self.last_error = ""
            except Exception as exc:  # noqa: BLE001 - retried on the next pass
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("index sync pass failed (retrying in %ss): %s", self.settings.sync_seconds, self.last_error)
            self._stop.wait(self.settings.sync_seconds)

    # -- one pass -------------------------------------------------------------

    def run_once(self) -> None:
        now = time.monotonic()
        if not self._roots or now - self._roots_at >= self.settings.revision_sweep_seconds:
            self._roots = self._discover_roots()
            self._roots_at = now
        state = self.store.state
        seen: dict[str, set[str]] = {"refs": set(), "docs": set(), "folders": set()}
        for ref in self._roots:
            page = self.client.get_page(path=ref)
            data = mokuji_data.parse(page.body) if page is not None else None
            if data is None:
                if page is not None:  # present but unreadable (a hand edit?): keep what we have
                    self._mark_seen(ref, seen)
                continue
            project = ref.rsplit("/", 1)[0] or "/"
            if state["blocks"].get(ref) == data.hash:
                self._mark_seen(ref, seen)
                continue
            self._walk(project, ref, data, seen)
            state["blocks"][ref] = data.hash
            self.store.save_state()
        if now - self._sweep_at >= self.settings.revision_sweep_seconds:
            self._sweep_at = now
            self._sweep_revisions(seen)
        self._remove_unseen(seen)
        if not self.ready:
            log.info("index ready: %s", self.store.counts())
        self.ready = True

    def _discover_roots(self) -> list[str]:
        """Each project has its own root 00-目次; GROWI / is only a container."""
        name = self.settings.index_page_name
        configured = self.settings.growi_root_path.rstrip("/")
        candidates = ([f"{configured}/{name}"] if configured else
                      [f"{child.path.rstrip('/')}/{name}" for child in self.client.list_children(path="/") if child.path])
        roots = []
        for ref in candidates:
            page = self.client.get_page(path=ref)
            if page is None:
                continue
            if mokuji_data.parse(page.body) is not None:
                roots.append(ref)
            elif md.is_index_page(page.body) and ref not in self._warned:
                self._warned.add(ref)
                log.warning("%s has no search data block: republish it with the new builder (`main.py index`)", ref)
        return roots

    def _mark_seen(self, ref: str, seen: dict[str, set[str]]) -> None:
        state = self.store.state
        stack = [ref]
        while stack:
            current = stack.pop()
            if current in seen["refs"]:
                continue
            seen["refs"].add(current)
            if current in state["docs"]:
                seen["docs"].add(current)
            if current in state.get("folders", {}):
                seen["folders"].add(current)
            stack.extend(state["tree"].get(current, []))

    def _walk(self, project: str, ref: str, data: mokuji_data.MokujiData, seen: dict[str, set[str]]) -> None:
        state = self.store.state
        seen["refs"].add(ref)
        state["tree"][ref] = [child.get("ref") for child in data.children if child.get("ref")]
        if data.level == "document":
            self._index_document(project, ref, data)
            seen["docs"].add(ref)
        for child in data.children:
            child_ref = child.get("ref")
            if not child_ref:
                continue
            if child.get("kind") == "folder":
                self._index_folder(project, child_ref, child)
                seen["folders"].add(child_ref)
            if state["blocks"].get(child_ref) == child.get("hash"):
                self._mark_seen(child_ref, seen)
                continue
            page = self.client.get_page(path=child_ref)
            child_data = mokuji_data.parse(page.body) if page is not None else None
            if child_data is None:
                if page is not None:  # present but unreadable (a hand edit?): keep what we have
                    self._mark_seen(child_ref, seen)
                continue
            self._walk(project, child_ref, child_data, seen)
            state["blocks"][child_ref] = child.get("hash") or child_data.hash
            self.store.save_state()

    def _index_folder(self, project: str, ref: str, child: dict[str, Any]) -> None:
        name, summary = child.get("name", ""), child.get("summary", "")
        text = f"{name}\n{summary}".strip()
        self.store.upsert([{"key": f"F|{ref}", "dense": {"text": text}, "sparse": {"terms": text},
                            "payload": {"level": "folder", "project": project, "document": "", "ref": ref,
                                        "name": name, "summary": summary, "text": text}}])
        self.store.state.setdefault("folders", {})[ref] = {"project": project}

    def _fetch(self, page_id: str) -> WikiPage | None:
        try:
            return self.client.get_page(page_id=page_id)
        except GrowiAPIError as exc:  # the revision sweep retries it
            log.warning("page %s unreadable, kept its old index entries: %s", page_id, exc)
            return None

    def _index_document(self, project: str, ref: str, data: mokuji_data.MokujiData) -> None:
        state = self.store.state
        doc_path = ref.rsplit("/", 1)[0]
        try:
            live = {p.id: p.revision_id for p in self.client.list_children(path=doc_path)}
        except GrowiAPIError as exc:
            log.info("document listing failed (%s), using builder revisions: %s", doc_path, exc)
            live = {}
        records = [page for page in data.pages if page.get("id")]
        by_page: dict[str, list[dict[str, Any]]] = {}
        for section in data.sections:
            by_page.setdefault(section.get("page", ""), []).append(section)
        changed = []
        for order, record in enumerate(records):
            page_id = record["id"]
            meta = mokuji_data.block_hash([record, *by_page.get(page_id, [])])
            row = state["pages"].get(page_id) or {}
            revision = live.get(page_id) or record.get("revision", "")
            if (row.get("doc") == ref and row.get("revision") == revision and row.get("meta") == meta
                    and row.get("order") == order):
                continue
            changed.append((order, record, meta))
        embedded = 0
        pages = self._pool.map(self._fetch, [record["id"] for _order, record, _meta in changed])
        for (order, record, meta), page in zip(changed, pages):
            if page is None or self._stop.is_set():
                continue
            page_id = record["id"]
            specs = page_specs(project, ref, data.name, order, record, by_page.get(page_id, []), page)
            keys = [spec["key"] for spec in specs]
            embedded += self.store.upsert(specs)
            old = (state["pages"].get(page_id) or {}).get("keys", [])
            self.store.delete(set(old) - set(keys))
            state["pages"][page_id] = {"doc": ref, "revision": page.revision_id or live.get(page_id, ""),
                                       "meta": meta, "order": order, "keys": keys}
        wanted = {record["id"] for record in records}
        for page_id, row in list(state["pages"].items()):
            if row.get("doc") == ref and page_id not in wanted:
                self.store.delete(row.get("keys", []))
                del state["pages"][page_id]
        chapters = list(dict.fromkeys(r.get("chapter", "") for r in records if r.get("chapter")))
        titles = [r.get("title", "") for r in records]
        kinds = list(dict.fromkeys(k for r in records for k in r.get("kind") or []))
        text = "\n".join(x for x in [data.name, "、".join(chapters), "、".join(titles), "、".join(kinds)] if x)
        self.store.upsert([{"key": f"d|{ref}", "dense": {"text": text},
                            "sparse": {"terms": " ".join([data.name, *chapters, *titles, *kinds])},
                            "payload": {"level": "document", "project": project, "document": ref,
                                        "doc_name": data.name, "path": doc_path, "text": text}}])
        state["docs"][ref] = {"project": project, "name": data.name, "path": doc_path, "pages": sorted(wanted)}
        self.store.save_state()
        if changed:
            log.info("indexed %s: %d/%d pages changed, %d texts embedded", ref, len(changed), len(records), embedded)

    def _sweep_revisions(self, seen: dict[str, set[str]]) -> None:
        """Catch hand edits made in GROWI between builder runs: one listing per document."""
        state = self.store.state
        for ref, doc in list(state["docs"].items()):
            if ref not in seen["docs"] or self._stop.is_set():
                continue
            try:
                live = {p.id: p.revision_id for p in self.client.list_children(path=doc["path"])}
            except GrowiAPIError as exc:
                log.info("revision sweep skipped %s: %s", ref, exc)
                continue
            stale = any(
                page_id in live and live[page_id] != (state["pages"].get(page_id) or {}).get("revision")
                for page_id in doc.get("pages", [])
            )
            if not stale:
                continue
            page = self.client.get_page(path=ref)
            data = mokuji_data.parse(page.body) if page is not None else None
            if data is not None:
                self._index_document(doc["project"], ref, data)

    def _remove_unseen(self, seen: dict[str, set[str]]) -> None:
        state = self.store.state
        changed = False
        for ref in [r for r in state["docs"] if r not in seen["docs"]]:
            for page_id, row in list(state["pages"].items()):
                if row.get("doc") == ref:
                    self.store.delete(row.get("keys", []))
                    del state["pages"][page_id]
            self.store.delete([f"d|{ref}"])
            del state["docs"][ref]
            changed = True
            log.info("removed document %s from the index", ref)
        folders = state.setdefault("folders", {})
        for ref in [r for r in folders if r not in seen["folders"]]:
            self.store.delete([f"F|{ref}"])
            del folders[ref]
            changed = True
        for ref in [r for r in state["blocks"] if r not in seen["refs"]]:
            state["blocks"].pop(ref, None)
            state["tree"].pop(ref, None)
            changed = True
        if changed:
            self.store.save_state()
