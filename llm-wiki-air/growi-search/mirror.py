"""Disk-backed, periodically refreshed copy of pages in one GROWI scope."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from typing import Any

from growi_client import PAGE_ACTIONS, GrowiAPIError, GrowiSearchClient, _in_scope
from models import WikiPage
from page_cache import PageCache

log = logging.getLogger("growi_search_mirror")

STRUCTURAL_ACTIONS = {
    "PAGE_RENAME", "PAGE_RECURSIVELY_RENAME", "PAGE_DELETE", "PAGE_DELETE_COMPLETELY",
    "PAGE_RECURSIVELY_DELETE", "PAGE_RECURSIVELY_DELETE_COMPLETELY",
    "PAGE_RECURSIVELY_REVERT",
}
CONTENT_ACTIONS = {"PAGE_CREATE", "PAGE_UPDATE", "PAGE_DUPLICATE", "PAGE_REVERT"}


@dataclass(frozen=True)
class Row:
    id: str
    path: str
    revision: str
    updated_at: str
    descendant_count: int


class Mirror:
    def __init__(self, client: GrowiSearchClient, settings: Any) -> None:
        self.client, self.settings = client, settings
        self.root_path = settings.growi_root_path or "/"
        token = hashlib.sha256(client.api_token.encode()).hexdigest()
        namespace = hashlib.sha256(
            f"{client.url}\n{self.root_path}\n{token}".encode()
        ).hexdigest()[:16]
        self.directory = Path(settings.mirror_dir) / namespace if settings.mirror_dir else None
        self.pages_dir = self.directory / "pages" if self.directory else None
        self.catalog_file = self.directory / "catalog.json.gz" if self.directory else None
        self.meta_file = self.directory / "meta.json" if self.directory else None
        self.rows: dict[str, Row] = {}
        self.by_path: dict[str, str] = {}
        self._children: dict[str, list[Row]] = {}
        self._descendant_counts: dict[str, int] = {}
        self.catalog_version = 0
        self.ready = False
        self.changes = getattr(settings, "mirror_changes", "auto")
        self.watermark = ""
        self.watermark_ids: set[str] = set()
        self.last_relist = 0.0
        self._loaded = False
        self._relist_due = False
        self._dirty = False  # catalog changed since the last catalog.json.gz write
        self._lock = Lock()
        self._sync_lock = Lock()
        self._verdict_lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._page_cache = PageCache(None, 1_000_000,
                                     max(0, int(getattr(settings, "page_cache_mb", 64))) * 1024 * 1024)

    @property
    def version(self) -> int:
        with self._lock:
            return self.catalog_version

    def start(self) -> None:
        if self.directory is None or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="growi-search-mirror", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not current_thread():
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception:  # noqa: BLE001 - the next poll retries transient failures
                log.exception("mirror sync failed")
            self._stop.wait(max(1, getattr(self.settings, "mirror_poll_seconds", 10)))

    def sync_once(self) -> None:
        if self.directory is None:
            return
        with self._sync_lock:
            if not self._loaded:
                self._load()
            if not self.watermark:
                # Before the first relist, so edits made while it runs are replayed after it.
                self._poll_changes()
            relisted = False
            now = time.time()
            if not self.ready or self._relist_due or now - self.last_relist >= getattr(self.settings, "mirror_relist_seconds", 1800):
                self._relist()
                relisted = True
            self._warm_missing()
            self._poll_changes()
            if relisted:
                self._collect_garbage()

    def _load(self) -> None:
        self._loaded = True
        assert self.catalog_file and self.meta_file
        if self.meta_file.exists():
            try:
                meta = json.loads(self.meta_file.read_text())
                self.changes = meta.get("changes", self.changes)
                self.watermark = meta.get("watermark", "")
                self.watermark_ids = set(meta.get("watermark_ids", []))
                self.last_relist = float(meta.get("last_relist", 0))
                self.catalog_version = int(meta.get("catalog_version", 0))
            except (OSError, ValueError, TypeError):
                log.info("mirror metadata unreadable; rebuilding catalog")
        if self.catalog_file.exists():
            try:
                with gzip.open(self.catalog_file, "rt", encoding="utf-8") as stream:
                    rows = [Row(**raw) for raw in json.load(stream)]
                self._swap(rows, bump=False)
                self.ready = True
            except (OSError, ValueError, TypeError, KeyError):
                log.info("mirror catalog unreadable; rebuilding")

    def _relist(self) -> None:
        rows = []
        for page in self.client.iter_descendants(
            self.root_path, limit=getattr(self.settings, "mirror_list_limit", 500)
        ):
            if page.id and page.path and _in_scope(page.path, self.root_path):
                rows.append(self._row(page))
        with self._lock:
            self._swap(rows, bump=True)
            self.ready = True
            self.last_relist = time.time()
            self._relist_due = False
            self._save_catalog_locked()

    def _row(self, page: WikiPage) -> Row:
        return Row(page.id, page.path, page.revision_id, page.updated_at, page.descendant_count)

    def _swap(self, rows: list[Row], *, bump: bool) -> None:
        new_rows = {row.id: row for row in rows}
        for page_id, row in new_rows.items():
            old = self.rows.get(page_id)
            if old and old.path != row.path:
                self._page_cache.remove((page_id, old.revision))
                if old.revision == row.revision:
                    try:
                        self._body_file(page_id, row.revision).unlink(missing_ok=True)
                    except ValueError:
                        pass
        self.rows = new_rows
        self.by_path = {row.path: row.id for row in rows}
        self._reindex_locked()
        if bump:
            self.catalog_version += 1

    def _reindex_locked(self) -> None:
        children: dict[str, list[Row]] = {}
        counts: dict[str, int] = {}
        pages = {row.path.rstrip("/") for row in self.rows.values()}
        folders: set[str] = set()
        for row in self.rows.values():
            parent = row.path.rstrip("/").rsplit("/", 1)[0] or "/"
            children.setdefault(parent, []).append(row)
            parts = row.path.strip("/").split("/")
            for index in range(1, len(parts)):
                ancestor = "/" + "/".join(parts[:index])
                counts[ancestor] = counts.get(ancestor, 0) + 1
                # GROWI lists a path that only has pages below it (an empty page) as a folder;
                # the catalog holds real pages only, so list such folders here by path.
                if ancestor not in pages and ancestor not in folders:
                    folders.add(ancestor)
                    children.setdefault(ancestor.rsplit("/", 1)[0] or "/", []).append(Row("", ancestor, "", "", 0))
        for values in children.values():
            values.sort(key=lambda child: child.path)
        self._children = children
        self._descendant_counts = counts

    def _save_catalog_locked(self) -> None:
        assert self.catalog_file
        self._atomic(self.catalog_file, json.dumps([asdict(row) for row in self.rows.values()], ensure_ascii=False).encode(), gzip_file=True)
        self._dirty = False
        self._save_meta_locked()

    def _persist_locked(self) -> None:
        # The catalog is O(corpus): write it once per poll, never once per page.
        if self._dirty:
            self._save_catalog_locked()
        else:
            self._save_meta_locked()

    def _save_meta_locked(self) -> None:
        assert self.meta_file
        meta = {
            "version": 1, "root_path": self.root_path, "growi_url": self.client.url,
            "changes": self.changes, "watermark": self.watermark,
            "watermark_ids": sorted(self.watermark_ids), "last_relist": self.last_relist,
            "catalog_version": self.catalog_version,
        }
        self._atomic(self.meta_file, json.dumps(meta, ensure_ascii=False).encode())

    @staticmethod
    def _atomic(path: Path, data: bytes, *, gzip_file: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.tmp-{os.getpid()}-", dir=path.parent)
        os.close(fd)
        temp = Path(temp_name)
        try:
            opener = gzip.open if gzip_file else open
            with opener(temp, "wb") as stream:
                stream.write(data)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def _body_file(self, page_id: str, revision: str) -> Path:
        if not re.fullmatch(r"[0-9a-fA-F]{24}", page_id or "") or not re.fullmatch(r"[0-9a-fA-F]{24}", revision or ""):
            raise ValueError("invalid GROWI page or revision id")
        assert self.pages_dir
        return self.pages_dir / page_id[:2] / f"{page_id}.{revision}.json.gz"

    def _store_body(self, page: WikiPage) -> None:
        payload = {
            "id": page.id, "revision": page.revision_id, "path": page.path,
            "title": page.title, "body": page.body, "updated_at": page.updated_at,
        }
        self._atomic(self._body_file(page.id, page.revision_id),
                     json.dumps(payload, ensure_ascii=False).encode(), gzip_file=True)

    def _upsert(self, page: WikiPage, *, reindex: bool = True) -> None:
        if not page.id or not page.path or not _in_scope(page.path, self.root_path):
            return
        try:
            self._body_file(page.id, page.revision_id)
        except ValueError:
            log.info("ignoring page with invalid id or revision (%s)", page.path)
            return
        row = self._row(page)
        with self._lock:
            old = self.rows.get(page.id)
            if old and old.path != row.path:
                self.by_path.pop(old.path, None)
            self.rows[page.id] = row
            self.by_path[row.path] = row.id
            if old != row:
                self.catalog_version += 1
                self._dirty = True
                if reindex and (old is None or old.path != row.path or old.descendant_count != row.descendant_count):
                    self._reindex_locked()
            self._page_cache.remove((page.id, page.revision_id))
        self._store_body(page)
        self._cache_page(page)

    def _drop_path(self, path: str) -> dict[str, Row]:
        prefix = path.rstrip("/") + "/"
        with self._lock:
            removed = {page_id: row for page_id, row in self.rows.items()
                       if row.path == path or row.path.startswith(prefix)}
            for row in removed.values():
                del self.rows[row.id]
                self.by_path.pop(row.path, None)
            if removed:
                self.catalog_version += 1
                self._dirty = True
                self._reindex_locked()
        return removed

    def _warm_missing(self) -> None:
        with self._lock:
            rows = list(self.rows.values())
        todo = []
        for row in rows:
            try:
                missing = not self._body_file(row.id, row.revision).exists()
            except ValueError:
                continue
            if missing:
                todo.append((row.id, row.revision))
        workers = max(1, int(getattr(self.settings, "mirror_warm_concurrency", 8)))
        version = self.version
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="growi-mirror-warm") as pool:
            for offset in range(0, len(todo), workers * 2):
                if self._stop.is_set():
                    return
                batch = todo[offset:offset + workers * 2]
                futures = {pool.submit(self.client.get_page, page_id=page_id): (page_id, revision)
                           for page_id, revision in batch}
                for future in as_completed(futures):
                    if self._stop.is_set():
                        for pending in futures:
                            pending.cancel()
                        return
                    try:
                        page = future.result()
                        if page:
                            self._upsert(page, reindex=False)
                    except Exception as exc:  # noqa: BLE001 - later reads fall back live
                        log.info("mirror warm failed (%s): %s", futures[future][0], exc)
                if offset and offset % 500 < workers * 2:
                    log.info("mirror warmed %d/%d pages", min(offset + len(batch), len(todo)), len(todo))
        if self.version != version:
            with self._lock:
                self._reindex_locked()

    def _poll_changes(self) -> None:
        if self.changes == "recent":
            self._poll_recent()
            return
        try:
            self._poll_audit()
        except GrowiAPIError as exc:
            # 404: no activity API; 405: "AuditLog is not enabled" (AUDIT_LOG_ENABLED=false).
            if exc.status_code not in {400, 401, 403, 404, 405}:
                raise
            if self.changes == "auto":
                self.changes = "recent"
                log.info("GROWI audit log unavailable; falling back to recent pages")
                with self._lock:
                    self._save_meta_locked()
                self._poll_recent()
            else:
                log.error("GROWI audit log unavailable: %s", exc)

    def _poll_audit(self) -> None:
        if not self.watermark:
            # First run: the relist is the baseline, so old history is not replayed.
            docs = self.client.activity(limit=1, offset=0, actions=list(PAGE_ACTIONS))
            self.watermark = str(docs[0].get("createdAt") or "0") if docs else "0"
            self.watermark_ids = {str(docs[0].get("_id") or "")} if docs else set()
            with self._lock:
                self._save_meta_locked()
            return
        records = []
        reached_boundary = False
        capped = True
        for page in range(20):
            docs = self.client.activity(limit=100, offset=page * 100, actions=list(PAGE_ACTIONS))
            if not docs:
                capped = False
                break
            for doc in docs:
                stamp = str(doc.get("createdAt") or "")
                event_id = str(doc.get("_id") or "")
                if self.watermark and (stamp < self.watermark or
                                       (stamp == self.watermark and event_id in self.watermark_ids)):
                    reached_boundary = True
                    break
                records.append(doc)
            if reached_boundary or len(docs) < 100:
                capped = False
                break
        if capped:
            self._relist_due = True
        newest = max((str(doc.get("createdAt") or "") for doc in records), default="")
        if newest:
            ids = {str(doc.get("_id") or "") for doc in records
                   if str(doc.get("createdAt") or "") == newest}
            if newest == self.watermark:
                ids |= self.watermark_ids
            self.watermark, self.watermark_ids = newest, ids
        handled: set[str] = set()
        for doc in records:
            action = str(doc.get("action") or "")
            if action == "PAGE_EMPTY_TRASH":
                continue
            target = doc.get("target")
            if isinstance(target, dict):
                target = target.get("_id") or target.get("id")
            page_id = str(target or "")
            if not page_id or page_id in handled:
                continue
            handled.add(page_id)
            if action in STRUCTURAL_ACTIONS:
                self._apply_structural(page_id)
            elif action in CONTENT_ACTIONS:
                self._refresh_page(page_id)
        with self._lock:
            self._persist_locked()

    def _apply_structural(self, page_id: str) -> None:
        with self._lock:
            old = self.rows.get(page_id)
        removed = self._drop_path(old.path) if old else {}
        try:
            page = self.client.get_page(page_id=page_id)
        except GrowiAPIError as exc:
            if exc.status_code != 404:
                raise
            page = None
        # The old path too: a non-recursive delete or rename leaves the children there.
        roots = [old.path] if old else []
        if page and page.path and _in_scope(page.path, self.root_path) and page.path not in roots:
            roots.append(page.path)
        limit = getattr(self.settings, "mirror_list_limit", 500)
        rows = {row.id: self._row(row) for root in roots
                for row in self.client.iter_descendants(root, limit=limit)
                if row.id and row.path and _in_scope(row.path, self.root_path)}
        if page and page.id and page.path and _in_scope(page.path, self.root_path):
            rows.setdefault(page.id, self._row(page))
        for row in rows.values():
            if removed.get(row.id) == row:  # untouched child: its body file is still valid
                with self._lock:
                    self.rows[row.id] = row
                    self.by_path[row.path] = row.id
                continue
            current = self.client.get_page(page_id=row.id)
            if current:
                self._upsert(current, reindex=False)
        with self._lock:
            self._reindex_locked()

    def _refresh_page(self, page_id: str) -> None:
        try:
            page = self.client.get_page(page_id=page_id)
        except GrowiAPIError as exc:
            if exc.status_code != 404:
                raise
            page = None
        if page and page.path and _in_scope(page.path, self.root_path):
            self._upsert(page)
        else:
            with self._lock:
                row = self.rows.get(page_id)
            if row:
                self._drop_path(row.path)

    def _poll_recent(self) -> None:
        if not self.watermark:
            # First run: the relist is the baseline, so old edits are not replayed.
            pages = self.client.recent_pages(limit=100, offset=0)
            self.watermark = max((page.updated_at for page in pages), default="") or "0"
            with self._lock:
                self._save_meta_locked()
            return
        offset = 0
        newest = self.watermark
        while True:
            pages = self.client.recent_pages(limit=100, offset=offset)
            for page in pages:
                if self.watermark and page.updated_at and page.updated_at < self.watermark:
                    self.watermark = max(newest, page.updated_at)
                    with self._lock:
                        self._persist_locked()
                    return
                newest = max(newest, page.updated_at)
                with self._lock:
                    old = self.rows.get(page.id)
                if old is None or old.revision != page.revision_id:
                    self._refresh_page(page.id)
            if len(pages) < 100:
                break
            offset += 100
        self.watermark = newest
        with self._lock:
            self._persist_locked()

    def _collect_garbage(self) -> None:
        assert self.pages_dir and self.directory
        with self._lock:
            live = {(row.id, row.revision) for row in self.rows.values()}
        if not self.directory.exists():
            return
        now = time.time()
        for path in self.directory.rglob("*"):
            if not path.is_file():
                continue
            if ".tmp-" in path.name:
                if now - path.stat().st_mtime > 3600:
                    path.unlink(missing_ok=True)
                continue
            if path.is_relative_to(self.directory / "verdicts") and path.suffixes[-2:] == [".json", ".gz"]:
                if now - path.stat().st_mtime > 30 * 24 * 60 * 60:
                    path.unlink(missing_ok=True)
                continue
            if not path.is_relative_to(self.pages_dir) or path.suffixes[-2:] != [".json", ".gz"]:
                continue
            stem = path.name[:-len(".json.gz")]
            page_id, _, revision = stem.partition(".")
            if (page_id, revision) not in live:
                path.unlink(missing_ok=True)

    def _verdict_file(self, query: str) -> Path:
        digest = hashlib.sha1(query.encode("utf-8")).hexdigest()
        return self.directory / "verdicts" / digest[:2] / f"{digest}.json.gz"

    def get_verdicts(self, query: str) -> dict[str, float]:
        if self.directory is None:
            return {}
        path = self._verdict_file(query)
        try:
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                return {str(key): float(value) for key, value in json.load(stream).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def set_verdicts(self, query: str, values: dict[str, float]) -> None:
        if self.directory is None or not values:
            return
        path = self._verdict_file(query)
        with self._verdict_lock:
            current = self.get_verdicts(query)
            current.update(values)
            self._atomic(path, json.dumps(current).encode(), gzip_file=True)

    def revision_of(self, page_id: str) -> str:
        with self._lock:
            row = self.rows.get(page_id)
            return row.revision if row else ""

    def path_of(self, page_id: str) -> str:
        with self._lock:
            row = self.rows.get(page_id)
            return row.path if row else ""

    def children_of(self, path: str) -> list[WikiPage]:
        with self._lock:
            children = list(self._children.get(path.rstrip("/") or "/", []))
        return [WikiPage(id=row.id, path=row.path, title=row.path.rstrip("/").rsplit("/", 1)[-1],
                         revision_id=row.revision, updated_at=row.updated_at,
                         descendant_count=self._descendant_counts.get(row.path, 0), document=row.path)
                for row in children]

    def get(self, *, page_id: str | None = None, path: str | None = None) -> WikiPage | None:
        cached = self.get_cached(page_id=page_id, path=path)
        if cached is not None:
            return cached
        page = self.client.get_page(page_id=page_id, path=path)
        if page and page.path and _in_scope(page.path, self.root_path):
            page.document = page.path
            self._upsert(page)
            return page.model_copy(deep=True)
        return None

    def get_cached(self, *, page_id: str | None = None, path: str | None = None) -> WikiPage | None:
        if bool(page_id) == bool(path):
            raise ValueError("exactly one of page_id / path is required")
        with self._lock:
            if not self.ready:
                row = None
            else:
                resolved = page_id or self.by_path.get(path or "")
                row = self.rows.get(resolved or "")
        if row:
            page = self._cached_page(row) or self._read_body(row)
            if page:
                self._cache_page(page)
                return page.model_copy(deep=True)
        return None

    def _read_body(self, row: Row) -> WikiPage | None:
        try:
            path = self._body_file(row.id, row.revision)
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                data = json.load(stream)
            return WikiPage(id=data["id"], revision_id=data["revision"], path=data["path"],
                            title=data["title"], body=data["body"], updated_at=data["updated_at"],
                            descendant_count=row.descendant_count, document=data["path"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _cache_page(self, page: WikiPage) -> None:
        self._page_cache.put((page.id, page.revision_id), page)

    def _cached_page(self, row: Row) -> WikiPage | None:
        page = self._page_cache.get((row.id, row.revision))
        if page:
            page.descendant_count = row.descendant_count
        return page
