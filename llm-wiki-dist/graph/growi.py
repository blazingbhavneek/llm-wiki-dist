"""Small, deliberately boring client for GROWI REST API v3."""

from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel


class GrowiAPIError(RuntimeError):
    def __init__(self, status_code: int, method: str, path: str, detail: str = "") -> None:
        self.status_code = status_code
        self.method = method
        self.path = path
        super().__init__(f"GROWI {method} {path} failed with HTTP {status_code}: {detail}")


class GrowiPage(BaseModel):
    page_id: str
    revision_id: str
    path: str
    title: str = ""
    body: str = ""
    updated_at: str = ""


_CHUNK_MARKER_RE = re.compile(r"^<!-- chunk: (?P<id>[^ ]+).*?-->\s*$", re.MULTILINE)
_CHUNK_END_RE = re.compile(r"^<!-- chunk-end: (?P<id>[^ ]+) -->[ \t]*$", re.MULTILINE)


class GrowiClient:
    """Read-only GROWI client; write methods are enabled in WP-12."""

    def __init__(
        self,
        url: str,
        api_token: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.api_token = api_token
        self.timeout = timeout
        self.transport = transport

    @property
    def api_base(self) -> str:
        return f"{self.url}/_api/v3/"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_token:
            # GROWI's v3 API documents bearer access-token authentication.
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            headers=self._headers(),
            transport=self.transport,
        ) as client:
            response = await client.request(
                method,
                urljoin(self.api_base, path.lstrip("/")),
                params=params,
                json=json_body,
            )
        if response.is_error:
            detail = response.text[:500]
            raise GrowiAPIError(response.status_code, method, path, detail)
        return response

    @staticmethod
    def _page_from_payload(payload: Any) -> GrowiPage:
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            payload = {}
        page = payload.get("page", payload)
        if isinstance(page, list):
            page = page[0] if page else {}
        if not isinstance(page, dict):
            page = {}
        revision = page.get("revision")
        if isinstance(revision, dict):
            revision_id = (
                revision.get("_id")
                or revision.get("id")
                or revision.get("revisionId")
                or ""
            )
            body = page.get("body") or revision.get("body") or ""
        else:
            revision_id = (
                page.get("revisionId")
                or page.get("revision_id")
                or revision
                or ""
            )
            body = page.get("body") or ""
        path = str(page.get("path") or "")
        return GrowiPage(
            page_id=str(page.get("_id") or page.get("id") or page.get("pageId") or ""),
            revision_id=str(revision_id),
            path=path,
            title=str(page.get("title") or path.rstrip("/").split("/")[-1] or ""),
            body=str(body),
            updated_at=str(page.get("updatedAt") or page.get("updated_at") or ""),
        )

    async def health(self) -> bool:
        try:
            response = await self._request("GET", "/healthcheck")
        except (GrowiAPIError, httpx.HTTPError):
            return False
        return response.is_success

    async def get_page(
        self,
        path: str | None = None,
        page_id: str | None = None,
    ) -> GrowiPage | None:
        if bool(path) == bool(page_id):
            raise ValueError("provide exactly one of path or page_id")
        params = {"path": path} if path else {"pageId": page_id}
        try:
            response = await self._request("GET", "/page", params=params)
        except GrowiAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        page = self._page_from_payload(response.json())
        return page if page.page_id or page.path else None

    async def list_pages(
        self,
        root_path: str = "/",
        *,
        page: int | None = None,
        limit: int = 100,
        updated_after: str | None = None,
        cursor: str | None = None,
    ) -> tuple[list[GrowiPage], int | str | None]:
        # ``page=None`` preserves the pre-GROWI-plan cursor API for callers
        # still on the old sync path; list_all_pages uses the real page API.
        params: dict[str, Any] = {"path": root_path, "limit": limit}
        if page is not None:
            params["page"] = page
        if updated_after:
            params["updatedAfter"] = updated_after
        if cursor:
            params["cursor"] = cursor
        response = await self._request("GET", "/pages/list", params=params)
        payload = response.json()
        if not isinstance(payload, dict):
            return [], None
        raw_pages = payload.get("pages") or payload.get("docs") or []
        pages = [self._page_from_payload(item) for item in raw_pages if isinstance(item, dict)]
        if page is not None:
            return pages, int(payload.get("totalCount") or len(pages))
        paginate = payload.get("paginateResult") or {}
        raw_pages = raw_pages or paginate.get("docs", [])
        pages = [self._page_from_payload(item) for item in raw_pages if isinstance(item, dict)]
        next_cursor = payload.get("nextCursor") or payload.get("next_cursor")
        if next_cursor is None:
            next_page = payload.get("nextPage")
            if next_page is not None:
                next_cursor = str(next_page)
        return pages, str(next_cursor) if next_cursor else None

    async def list_all_pages(self, root_path: str = "/") -> list[GrowiPage]:
        seen: dict[str, GrowiPage] = {}
        page = 1
        while True:
            batch, total = await self.list_pages(root_path, page=page)
            for item in batch:
                if item.page_id:
                    seen.setdefault(item.page_id, item)
            if not batch or len(seen) >= int(total) or page * 100 >= int(total):
                return list(seen.values())
            page += 1

    async def create_page(self, path: str, body: str) -> GrowiPage:
        response = await self._request(
            "POST",
            "/page/",
            json_body={"path": path, "body": body},
        )
        return self._page_from_payload(response.json())

    async def update_page(self, page_id: str, revision_id: str, body: str) -> GrowiPage:
        response = await self._request(
            "PUT",
            "/page/",
            json_body={
                "pageId": page_id,
                "revisionId": revision_id,
                "body": body,
                "origin": "editor",
            },
        )
        return self._page_from_payload(response.json())

    async def delete_pages(self, page_ids_to_revisions: dict[str, str]) -> None:
        if page_ids_to_revisions:
            await self._request(
                "POST",
                "/pages/delete",
                json_body={"pageIdToRevisionIdMap": page_ids_to_revisions},
            )


_GROWI_BAD = re.compile(r"[\^$*+#<>%?\\]")
_LINES_RE = re.compile(r"^<!-- chunk: [^ ]+ lines (\d+)-(\d+)", re.MULTILINE)


def growi_segment(name: str) -> str:
    cleaned = _GROWI_BAD.sub("-", name.strip()).strip("/")
    if cleaned.lower().endswith(".md"):
        cleaned = cleaned[:-3]
    return cleaned or "-"


def growi_path(*parts: str) -> str:
    segments = [growi_segment(seg) for part in parts for seg in part.split("/") if seg.strip()]
    return "/" + "/".join(segments)


def team_of_path(path: str, write_path: str) -> str | None:
    base = "/" + write_path.strip("/")
    rest = path
    if base != "/":
        if not path.startswith(base.rstrip("/") + "/"):
            return None
        rest = path[len(base):]
    head, sep, _tail = rest.strip("/").partition("/")
    return head if sep and head else None


def wrap_page(body: str, *, page_id: str, ranges: list[tuple[int, int]]) -> str:
    if _CHUNK_MARKER_RE.search(body):
        return body
    start = min((a for a, _ in ranges), default=0)
    end = max((b for _, b in ranges), default=0)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    marker = f"<!-- chunk: {page_id} lines {start}-{end} hash:{digest} -->"
    return f"{marker}\n{body.rstrip()}\n<!-- chunk-end: {page_id} -->"


def source_ranges(body: str) -> list[tuple[int, int]]:
    return [(int(a), int(b)) for a, b in _LINES_RE.findall(body) if int(b) >= int(a) > 0]


def assert_publish_path(path: str, *, mode: str, write_path: str, root_path: str = "/") -> None:
    """Reject writes outside the configured attach/own boundary."""
    normalized = "/" + posixpath.normpath(path).lstrip("/")
    boundary = "/" + posixpath.normpath(write_path or "/").lstrip("/")
    if mode == "attach" and not (
        normalized == boundary or normalized.startswith(boundary.rstrip("/") + "/")
    ):
        raise PermissionError(
            f"attach-mode GROWI write outside write_path: {normalized} not under {boundary}"
        )
    if mode == "own":
        own_boundary = "/" + posixpath.normpath(root_path or "/").lstrip("/")
        if not (
            own_boundary == "/"
            or normalized == own_boundary
            or normalized.startswith(own_boundary.rstrip("/") + "/")
        ):
            raise PermissionError(
                f"own-mode GROWI write outside root_path: {normalized} not under {own_boundary}"
            )


def _marked_sections(body: str) -> dict[str, tuple[int, int]]:
    matches = list(_CHUNK_MARKER_RE.finditer(body))
    sections: dict[str, tuple[int, int]] = {}
    for index, match in enumerate(matches):
        boundary = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        end = _CHUNK_END_RE.search(body, match.end(), boundary)
        sections[match.group("id")] = (match.start(), end.end() if end and end.group("id") == match.group("id") else boundary)
    return sections


def merge_marked_sections(existing: str, additions: str) -> str:
    """Replace/append only chunk-marked sections; preserve all other text."""
    additions_sections = _marked_sections(additions)
    if not additions_sections:
        raise ValueError("GROWI publish body contains no chunk markers")
    result = existing
    for chunk_id, (start, end) in sorted(
        additions_sections.items(), key=lambda item: item[1][0], reverse=True
    ):
        new_section = additions[start:end]
        existing_sections = _marked_sections(result)
        if chunk_id in existing_sections:
            old_start, old_end = existing_sections[chunk_id]
            if old_end < len(result) and not new_section.endswith("\n"):
                new_section += "\n\n"
            result = result[:old_start] + new_section + result[old_end:]
        else:
            separator = "" if not result or result.endswith("\n") else "\n"
            result = result + separator + "\n" + new_section
    return result


async def publish_pages(
    client: GrowiClient,
    pages: list[dict[str, str]],
    *,
    mode: str,
    write_path: str,
    root_path: str = "/",
) -> list[GrowiPage]:
    """Publish each page once, retrying one stale-revision conflict."""
    results: list[GrowiPage] = []
    for item in pages:
        path = item["path"]
        body = item["body"]
        assert_publish_path(path, mode=mode, write_path=write_path, root_path=root_path)
        existing = await client.get_page(path=path)
        if existing is None:
            results.append(await client.create_page(path, body))
            continue
        merged = merge_marked_sections(existing.body, body)
        try:
            results.append(await client.update_page(existing.page_id, existing.revision_id, merged))
        except GrowiAPIError as exc:
            if exc.status_code != 409:
                raise
            refreshed = await client.get_page(path=path)
            if refreshed is None:
                results.append(await client.create_page(path, body))
                continue
            retry_body = merge_marked_sections(refreshed.body, body)
            results.append(
                await client.update_page(refreshed.page_id, refreshed.revision_id, retry_body)
            )
    return results


_NUMBERED_RE = re.compile(r"^\d+-(.+)$")


def _canonical(filename: str) -> str:
    match = _NUMBERED_RE.match(filename)
    return match.group(1) if match else filename


def _coverage_ranges(folder: Path) -> dict[str, list[tuple[int, int]]]:
    path = Path(folder) / "_planning" / "coverage.json"
    if not path.exists():
        return {}
    try:
        files = json.loads(path.read_text(encoding="utf-8")).get("files", [])
    except (OSError, ValueError):
        return {}
    out: dict[str, list[tuple[int, int]]] = {}
    for item in files:
        start, end = item.get("source_start"), item.get("source_end")
        if item.get("filename") and start is not None and end is not None:
            out.setdefault(str(item["filename"]), []).append((int(start), int(end)))
    return out


class GrowiPublisher:
    """Publish one generated document and trash only pages marked by us."""

    def __init__(self, client: GrowiClient, connection: Any) -> None:
        self.client = client
        self.connection = connection

    def doc_path(self, project: Any, rel: str) -> str:
        folder = project.wiki_dir(rel).relative_to(project.wiki).as_posix()
        return growi_path(self.connection.write_path, folder)

    def publish_document(self, project: Any, rel: str) -> list[GrowiPage]:
        folder = project.wiki_dir(rel)
        doc_path = self.doc_path(project, rel)
        ranges = _coverage_ranges(folder)
        pages: list[dict[str, str]] = []
        for md in sorted(folder.glob("*.md")):
            name = growi_segment(md.name)
            body = md.read_text(encoding="utf-8")
            page_path = f"{doc_path}/{name}"
            pages.append({
                "path": page_path,
                "body": wrap_page(
                    body,
                    page_id="page" + page_path.replace(" ", "_"),
                    ranges=ranges.get(_canonical(md.name), []),
                ),
            })
        results = asyncio.run(publish_pages(
            self.client,
            pages,
            mode=self.connection.mode,
            write_path=self.connection.write_path,
            root_path=self.connection.root_path,
        ))
        asyncio.run(self._trash_under(doc_path, keep={p["path"] for p in pages}))
        return results

    def delete_document(self, project: Any, rel: str) -> int:
        return asyncio.run(self._trash_under(self.doc_path(project, rel), keep=set()))

    async def _trash_under(self, doc_path: str, *, keep: set[str]) -> int:
        doomed: dict[str, str] = {}
        for listed in await self.client.list_all_pages(doc_path):
            if listed.path == doc_path or listed.path in keep:
                continue
            full = await self.client.get_page(page_id=listed.page_id)
            if full and _CHUNK_MARKER_RE.search(full.body):
                doomed[full.page_id] = full.revision_id
        await self.client.delete_pages(doomed)
        return len(doomed)


async def _sync_growi_pages_legacy(
    client: GrowiClient,
    registry: Any,
    connection: Any,
    *,
    on_page: Any,
    on_delete: Any,
    on_rename: Any | None = None,
) -> dict[str, int | str | None]:
    """Incrementally sync a GROWI page listing into a local index.

    The sync cursor is recorded only after every page callback succeeds. A
    crash therefore repeats work safely instead of skipping an unseen page.
    """
    # A changed-only listing cannot prove that an absent page was deleted.
    # Enumerate the root on each poll, then fetch bodies only for new/revised
    # pages.  The cursor is still honored for pagination within this crawl.
    remote_by_id: dict[str, GrowiPage] = {}
    cursor: str | None = None
    next_cursor: str | None = None
    while True:
        batch, batch_cursor = await client.list_pages(
            connection.root_path,
            updated_after=None,
            cursor=cursor,
        )
        for page in batch:
            remote_by_id[page.page_id] = page
        next_cursor = batch_cursor
        if not batch_cursor or batch_cursor == cursor:
            break
        cursor = batch_cursor
    remote = list(remote_by_id.values())
    local = {item.page_id: item for item in registry.pages(connection.name)}
    remote_ids = {page.page_id for page in remote}
    added = changed = renamed = deleted = 0

    for listed in remote:
        previous = local.get(listed.page_id)
        if previous and previous.revision_id == listed.revision_id:
            if previous.path != listed.path:
                if on_rename is not None:
                    await _maybe_await(on_rename, previous, listed)
                registry.upsert_page(
                    registry_page(
                        connection.name,
                        listed,
                        indexed_at=previous.indexed_at,
                    )
                )
                renamed += 1
            continue

        full = await client.get_page(page_id=listed.page_id)
        if full is None:
            continue
        await _maybe_await(on_page, full, previous)
        registry.upsert_page(registry_page(connection.name, full))
        if previous is None:
            added += 1
        else:
            changed += 1

    for page_id, previous in local.items():
        if page_id in remote_ids:
            continue
        await _maybe_await(on_delete, previous)
        registry.delete_page(connection.name, page_id)
        deleted += 1

    # This is intentionally last: callback failures leave the old cursor in
    # place, so a subsequent run cannot skip the failed page.
    registry.record_sync(
        connection.name,
        cursor=next_cursor,
        synced_at=datetime.now(timezone.utc).isoformat(),
        error=None,
    )
    return {
        "added": added,
        "changed": changed,
        "renamed": renamed,
        "deleted": deleted,
        "cursor": next_cursor,
    }


def _parent(path: str) -> str:
    return posixpath.dirname(path.rstrip("/")) or "/"


async def sync_growi_pages(
    client: GrowiClient,
    registry: Any,
    connection: Any,
    *,
    on_document: Any | None = None,
    on_delete_document: Any | None = None,
    on_page: Any | None = None,
    on_delete: Any | None = None,
    on_rename: Any | None = None,
) -> dict[str, Any]:
    """Diff GROWI and revise each changed document folder atomically."""
    if on_document is None or on_delete_document is None:
        return await _sync_growi_pages_legacy(
            client, registry, connection,
            on_page=on_page or (lambda *_: None),
            on_delete=on_delete or (lambda *_: None),
            on_rename=on_rename,
        )
    remote = {p.page_id: p for p in await client.list_all_pages(connection.root_path)}
    local = {p.page_id: p for p in registry.pages(connection.name)}
    touched: set[str] = set()
    for page_id, page in remote.items():
        previous = local.get(page_id)
        if previous is None or previous.revision_id != page.revision_id or previous.path != page.path:
            touched.add(_parent(page.path))
            if previous is not None:
                touched.add(_parent(previous.path))
    for page_id, previous in local.items():
        if page_id not in remote:
            touched.add(_parent(previous.path))
    by_document: dict[str, list[GrowiPage]] = {}
    for page in remote.values():
        by_document.setdefault(_parent(page.path), []).append(page)
    revised = deleted = 0
    for document in sorted(touched):
        listed = sorted(by_document.get(document, []), key=lambda p: p.path)
        if not listed:
            await _maybe_await(on_delete_document, document)
            deleted += 1
        else:
            full = [await client.get_page(page_id=p.page_id) for p in listed]
            await _maybe_await(on_document, document, [p for p in full if p is not None])
            revised += 1
        for page in listed:
            registry.upsert_page(registry_page(connection.name, page))
        for page_id, previous in local.items():
            if page_id not in remote and _parent(previous.path) == document:
                registry.delete_page(connection.name, page_id)
    registry.record_sync(
        connection.name,
        cursor=None,
        synced_at=datetime.now(timezone.utc).isoformat(),
        error=None,
    )
    return {"pages": len(remote), "documents_revised": revised, "documents_deleted": deleted}


async def _maybe_await(callback: Any, *args: Any) -> Any:
    result = callback(*args)
    if hasattr(result, "__await__"):
        return await result
    return result


def registry_page(name: str, page: GrowiPage, *, indexed_at: str | None = None) -> Any:
    from .registry import GrowiPageIndex

    return GrowiPageIndex(
        name=name,
        page_id=page.page_id,
        revision_id=page.revision_id,
        path=page.path,
        indexed_at=indexed_at or datetime.now(timezone.utc).isoformat(),
    )
