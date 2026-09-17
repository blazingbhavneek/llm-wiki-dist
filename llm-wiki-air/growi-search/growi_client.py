"""Synchronous read-only GROWI REST client (search + page reads + children).

Verified against the live instance: Elasticsearch search lives at
``GET /_api/search`` (the same route the official MCP server calls); page reads
and the immediate-child listing live under ``/_api/v3``. No write methods, no
full-corpus enumeration, nothing that lists every page.
"""

from __future__ import annotations

from threading import BoundedSemaphore
from typing import Any

import httpx

from markdown import strip_search_highlights
from models import WikiPage


class GrowiAPIError(RuntimeError):
    def __init__(self, status_code: int, method: str, path: str, detail: str = "") -> None:
        self.status_code = status_code
        self.method = method
        self.path = path
        # Keep the detail short and token-free.
        super().__init__(f"GROWI {method} {path} failed with HTTP {status_code}: {(detail or '')[:300]}")


class SearchHit:
    """One normalized Elasticsearch hit: page shell + cleaned snippet."""

    def __init__(self, page: WikiPage, snippet: str, rank: int) -> None:
        self.page = page
        self.snippet = snippet
        self.rank = rank


def _in_scope(path: str, root: str) -> bool:
    if not root or root == "/":
        return True
    boundary = root.rstrip("/")
    return path == boundary or path.startswith(boundary + "/") or path.startswith(boundary + "%2F")


class GrowiSearchClient:
    def __init__(
        self,
        url: str,
        api_token: str,
        *,
        root_path: str = "/",
        timeout: float = 30.0,
        max_concurrency: int = 6,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.url = (url or "").rstrip("/")
        self.api_token = (api_token or "").strip()
        if self.api_token.lower().startswith("api token:"):
            self.api_token = self.api_token.split(":", 1)[1].strip()
        self.root_path = root_path or "/"
        self.timeout = timeout
        self._request_slots = BoundedSemaphore(max(1, max_concurrency))
        self._client = httpx.Client(
            base_url=self.url,
            timeout=timeout,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # -- low-level -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            with self._request_slots:
                response = self._client.request(method, path, params=clean, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise GrowiAPIError(0, method, path, "timeout") from exc
        except httpx.HTTPError as exc:
            raise GrowiAPIError(0, method, path, str(exc)) from exc

        if response.is_error:
            raise GrowiAPIError(response.status_code, method, path, response.text)
        try:
            # GROWI may return an HTML error page with a 200 status; treat that
            # as a bad response if JSON parsing fails.
            return response.json()
        except ValueError as exc:
            raise GrowiAPIError(
                response.status_code, method, path, "response is not JSON"
            ) from exc

    # -- normalization -------------------------------------------------------

    @staticmethod
    def _page_dict(raw: dict[str, Any]) -> WikiPage:
        path = str(raw.get("path") or "")
        revision = raw.get("revision")
        revision_id = ""
        if isinstance(revision, dict):
            revision_id = str(revision.get("_id") or revision.get("id") or "")
        elif isinstance(revision, str):
            revision_id = revision
        return WikiPage(
            id=str(raw.get("_id") or raw.get("id") or raw.get("pageId") or ""),
            revision_id=revision_id,
            path=path,
            title=str(
                raw.get("title") or path.rstrip("/").split("/")[-1] or path or ""
            ),
            parent_id=(str(raw["parent"]) if raw.get("parent") else None),
            descendant_count=int(raw.get("descendantCount") or 0),
            is_empty=bool(raw.get("isEmpty")),
            updated_at=str(raw.get("updatedAt") or raw.get("updated_at") or ""),
        )

    @staticmethod
    def _from_page_payload(payload: Any) -> WikiPage | None:
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            return None
        page = payload.get("page", payload)
        if isinstance(page, list):
            page = page[0] if page else {}
        if not isinstance(page, dict):
            return None
        if str(page.get("status") or "").lower() == "deleted":
            return None
        wiki = GrowiSearchClient._page_dict(page)
        revision = page.get("revision")
        if isinstance(revision, dict):
            wiki.body = str(page.get("body") or revision.get("body") or "")
        else:
            wiki.body = str(page.get("body") or "")
        return wiki if (wiki.id or wiki.path) else None

    # -- public API ----------------------------------------------------------

    def health(self) -> bool:
        try:
            payload = self._request("GET", "/_api/v3/healthcheck")
        except GrowiAPIError:
            return False
        if isinstance(payload, dict):
            return str(payload.get("status", "up")).lower() in {"up", "okay", "ok", "true"} or payload.get("ok") is not False
        return True

    def fetch_attachment(self, attachment_id: str) -> tuple[bytes, str] | None:
        """Bytes + content-type of one GROWI attachment, or None when missing."""
        headers = {**self._headers(), "Accept": "*/*"}
        try:
            with self._request_slots:
                response = self._client.get(
                    f"/attachment/{attachment_id}",
                    headers=headers,
                    params={"access_token": self.api_token},
                    follow_redirects=True,
                )
        except httpx.HTTPError as exc:
            raise GrowiAPIError(0, "GET", "/attachment", str(exc)) from exc
        if response.status_code == 404:
            return None
        content_type = response.headers.get("content-type", "application/octet-stream")
        if response.is_error or content_type.startswith("text/html"):
            raise GrowiAPIError(
                response.status_code if response.is_error else 401,
                "GET",
                "/attachment",
                "not an attachment response",
            )
        return response.content, content_type

    def search_pages(
        self,
        query: str,
        *,
        path: str,
        limit: int,
        offset: int = 0,
    ) -> list[SearchHit]:
        payload = self._request(
            "GET",
            "/_api/search",
            {"q": query, "path": path or "/", "limit": limit, "offset": offset},
        )
        # Response wrapper variants: {..., "data": [...]} or a bare list.
        if isinstance(payload, dict):
            raw_data = payload.get("data") or payload.get("docs") or []
        elif isinstance(payload, list):
            raw_data = payload
        else:
            raw_data = []
        # Older shapes nest data one more level: {"data": {"data": [...]}}.
        if isinstance(raw_data, dict):
            raw_data = raw_data.get("data") or raw_data.get("docs") or []

        hits: list[SearchHit] = []
        seen: set[str] = set()
        for item in raw_data:
            if not isinstance(item, dict):
                continue
            # Hit shape 1: {"data": {page}, "meta": {elasticSearchResult}}.
            # Hit shape 2: bare page document.
            inner = item.get("data") if isinstance(item.get("data"), dict) else item
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            es = meta.get("elasticSearchResult") if isinstance(meta, dict) else None
            snippet = ""
            if isinstance(es, dict):
                snippet = strip_search_highlights(str(es.get("snippet") or ""))
            wiki = self._page_dict(inner)
            # ES can still return recently deleted pages; skip them.
            if str(inner.get("status") or "").lower() == "deleted":
                continue
            # Only skip a hit that has no usable text; empty-folder pages
            # (isEmpty, descendantCount>0) are still meaningful results.
            if not wiki.id and not wiki.path:
                continue
            if wiki.id in seen:
                continue
            if not _in_scope(wiki.path, self.root_path):
                continue
            seen.add(wiki.id or wiki.path)
            wiki.snippet = snippet.strip()
            hits.append(SearchHit(wiki, wiki.snippet, len(hits) + 1))
            if len(hits) >= limit:
                break
        return hits

    def get_page(
        self, *, page_id: str | None = None, path: str | None = None
    ) -> WikiPage | None:
        if bool(page_id) == bool(path):
            raise ValueError("exactly one of page_id / path is required")
        params = {"pageId": page_id} if page_id else {"path": path}
        try:
            payload = self._request("GET", "/_api/v3/page", params)
        except GrowiAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        wiki = self._from_page_payload(payload)
        if wiki and wiki.path and not _in_scope(wiki.path, self.root_path):
            return None
        if wiki and wiki.path:
            wiki.document = wiki.path
        return wiki

    def list_children(
        self, *, page_id: str | None = None, path: str | None = None
    ) -> list[WikiPage]:
        if bool(page_id) == bool(path):
            raise ValueError("exactly one of page_id / path is required")
        params = {"id": page_id} if page_id else {"path": path}
        try:
            payload = self._request("GET", "/_api/v3/page-listing/children", params)
        except GrowiAPIError as exc:
            if exc.status_code == 404:
                return []
            raise
        raw_children: Any = []
        if isinstance(payload, dict):
            raw_children = payload.get("children") or payload.get("data") or []
        elif isinstance(payload, list):
            raw_children = payload
        out: list[WikiPage] = []
        for item in raw_children:
            if not isinstance(item, dict):
                continue
            wiki = self._page_dict(item)
            if not wiki.path or not _in_scope(wiki.path, self.root_path):
                continue
            wiki.document = wiki.path
            out.append(wiki)
        return out
