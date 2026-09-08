"""Small, deliberately boring client for GROWI REST API v3."""

from __future__ import annotations

import posixpath
import re
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
        updated_after: str | None = None,
        cursor: str | None = None,
    ) -> tuple[list[GrowiPage], str | None]:
        params: dict[str, Any] = {"path": root_path, "limit": 100}
        if updated_after:
            params["updatedAfter"] = updated_after
        if cursor:
            params["cursor"] = cursor
        response = await self._request("GET", "/pages/list", params=params)
        payload = response.json()
        if not isinstance(payload, dict):
            return [], None
        raw_pages = payload.get("pages") or payload.get("docs") or payload.get("paginateResult", {}).get("docs", [])
        pages = [self._page_from_payload(item) for item in raw_pages if isinstance(item, dict)]
        next_cursor = payload.get("nextCursor") or payload.get("next_cursor")
        if next_cursor is None:
            next_page = payload.get("nextPage")
            if next_page is not None:
                next_cursor = str(next_page)
        return pages, str(next_cursor) if next_cursor else None

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
    return {
        match.group("id"): (
            match.start(),
            matches[index + 1].start() if index + 1 < len(matches) else len(body),
        )
        for index, match in enumerate(matches)
    }


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
