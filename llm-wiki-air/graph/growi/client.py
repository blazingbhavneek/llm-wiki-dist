"""Small, deliberately boring client for GROWI REST API v3."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import html
import json
import mimetypes
import posixpath
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel

from graph.common.markdown import LINKS_FOOTER_END, LINKS_FOOTER_START
from graph.wiki.page import strip_reader_references


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


_CHUNK_MARKER_RE = re.compile(
    r'^(?:<!-- chunk: (?P<comment_id>[^ ]+).*?-->|'
    r'<span hidden data-llm-wiki-chunk="(?P<span_payload>[^"]+)"></span>)[ \t]*$',
    re.MULTILINE,
)
_CHUNK_END_RE = re.compile(
    r'^(?:<!-- chunk-end: (?P<comment_id>[^ ]+) -->|'
    r'<span hidden data-llm-wiki-chunk-end="(?P<span_payload>[^"]+)"></span>)[ \t]*$',
    re.MULTILINE,
)


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
        data: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
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
                data=data,
                files=files,
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
        items = list(page_ids_to_revisions.items())
        for start in range(0, len(items), 20):
            await self._request(
                "POST",
                "/pages/delete",
                json_body={"pageIdToRevisionIdMap": dict(items[start : start + 20])},
            )

    async def list_attachments(self, page_id: str) -> dict[str, str]:
        attachments: dict[str, str] = {}
        page_number = 1
        while True:
            response = await self._request(
                "GET",
                "/attachment/list",
                params={"pageId": page_id, "pageNumber": page_number, "limit": 100},
            )
            payload = response.json()
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            result = data.get("paginateResult", data) if isinstance(data, dict) else {}
            docs = result.get("docs", []) if isinstance(result, dict) else []
            for item in docs:
                if not isinstance(item, dict) or not item.get("originalName"):
                    continue
                attachment_id = item.get("_id") or item.get("id")
                path = item.get("filePathProxied") or (f"/attachment/{attachment_id}" if attachment_id else "")
                if path:
                    attachments[str(item["originalName"])] = str(path)
            if len(docs) < 100 or page_number >= int(result.get("totalPages") or page_number):
                return attachments
            page_number += 1

    async def upload_attachment(self, page_id: str, name: str, content: bytes, mime: str) -> str:
        response = await self._request(
            "POST",
            "/attachment",
            data={"page_id": page_id},
            files={"file": (name, content, mime)},
        )
        payload = response.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        attachment = data.get("attachment", {}) if isinstance(data, dict) else {}
        attachment_id = attachment.get("_id") or attachment.get("id")
        path = attachment.get("filePathProxied") or (f"/attachment/{attachment_id}" if attachment_id else "")
        if not path:
            raise ValueError("GROWI attachment response contains no image path")
        return str(path)


_GROWI_BAD = re.compile(r"[\^$*+#<>%?\\]")
_LINES_RE = re.compile(
    r'^(?:<!-- chunk: [^ ]+ lines |'
    r'<span hidden data-llm-wiki-chunk="[^ ]+ lines )(\d+)-(\d+)',
    re.MULTILINE,
)
_MARKDOWN_LINK_RE = re.compile(
    r"(?<!!)(?P<prefix>\[[^\]\n]*\]\()(?P<target>(?:(?!\]\().)*?\.md)(?P<fragment>#[^)\n]*)?\)"
)
_PERMALINK_RE = re.compile(
    r"(?<!!)(?P<prefix>\[[^\]\n]*\]\()/(?P<page_id>[^/#)\n]+)(?P<fragment>#[^)\n]*)?\)"
)
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_IMAGE_UNIT_RE = re.compile(r"<image-unit\b[^>]*>.*?</image-unit>", re.I | re.S)
_IMAGE_DESCRIPTION_RE = re.compile(r"<image-description\b[^>]*>(.*?)</image-description>", re.I | re.S)
_IMAGE_DATA_RE = re.compile(
    r'''<img\b[^>]*\bsrc=["']data:(?P<mime>image/[a-z0-9.+-]+);base64,(?P<data>[a-z0-9+/=\s]+)["']''',
    re.I,
)
_CHUNK_COMMENT_RE = re.compile(r"^<!-- chunk: (?P<payload>.*?)-->[ \t]*$", re.MULTILINE)
_CHUNK_END_COMMENT_RE = re.compile(
    r"^<!-- chunk-end: (?P<payload>.*?)-->[ \t]*$", re.MULTILINE
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"(?<!`)`(?P<text>[^`\n]+)`(?!`)")


def _growi_markdown(body: str) -> str:
    """Remove comments while retaining invisible ownership markers."""

    body = _CHUNK_COMMENT_RE.sub(
        lambda match: '<span hidden data-llm-wiki-chunk="'
        + html.escape(match.group("payload").strip(), quote=True)
        + '"></span>',
        body,
    )
    body = _CHUNK_END_COMMENT_RE.sub(
        lambda match: '<span hidden data-llm-wiki-chunk-end="'
        + html.escape(match.group("payload").strip(), quote=True)
        + '"></span>',
        body,
    )
    body = _HTML_COMMENT_RE.sub("", body)
    return _rewrite_outside_fences(
        body, _INLINE_CODE_RE, lambda match: match.group("text")
    )


def _marker_id(match: re.Match[str]) -> str:
    return match.group("comment_id") or html.unescape(match.group("span_payload")).split()[0]


def _image_description(unit: str) -> str:
    match = _IMAGE_DESCRIPTION_RE.search(unit)
    text = html.unescape(re.sub(r"<[^>]+>", "", match.group(1))) if match else ""
    return " ".join(text.split()) or "画像"


def _markdown_image(description: str, path: str = "") -> str:
    alt = description.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return f"![{alt}]({path})"


def _image_fallback(unit: str) -> str:
    return _markdown_image(_image_description(unit))


def _image_fallbacks(body: str) -> str:
    return _IMAGE_UNIT_RE.sub(lambda match: _image_fallback(match.group(0)), body)


async def _publish_images(client: GrowiClient, body: str, page_id: str) -> str:
    matches = list(_IMAGE_UNIT_RE.finditer(body))
    if not matches:
        return body
    attachments = await client.list_attachments(page_id)
    output: list[str] = []
    end = 0
    for match in matches:
        output.append(body[end:match.start()])
        unit = match.group(0)
        image = _IMAGE_DATA_RE.search(unit)
        description = _image_description(unit)
        if image is None:
            output.append(_image_fallback(unit))
            end = match.end()
            continue
        mime = image.group("mime").lower()
        try:
            content = base64.b64decode("".join(image.group("data").split()), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid embedded image data") from exc
        extension = mimetypes.guess_extension(mime) or ".bin"
        if extension == ".jpe":
            extension = ".jpg"
        name = f"llm-wiki-{hashlib.sha256(content).hexdigest()[:20]}{extension}"
        path = attachments.get(name)
        if path is None:
            path = await client.upload_attachment(page_id, name, content, mime)
            attachments[name] = path
        output.append(_markdown_image(description, path))
        end = match.end()
    output.append(body[end:])
    return "".join(output)


def growi_segment(name: str) -> str:
    cleaned = _GROWI_BAD.sub("-", name.strip()).strip("/")
    if cleaned.lower().endswith(".md"):
        cleaned = cleaned[:-3]
    return cleaned or "-"


def growi_path(*parts: str) -> str:
    segments = [growi_segment(seg) for part in parts for seg in part.split("/") if seg.strip()]
    return "/" + "/".join(segments)


def rewrite_page_links(body: str, source_rel: str, page_ids: dict[str, str]) -> str:
    """Translate generated local Markdown links to stable GROWI permalinks."""
    source_dir = posixpath.dirname(source_rel)

    def replace(match: re.Match[str]) -> str:
        target = match.group("target")
        resolved = posixpath.normpath(posixpath.join(source_dir, target))
        page_id = page_ids.get(resolved)
        if not page_id:
            return match.group(0)
        return f'{match.group("prefix")}/{page_id}{match.group("fragment") or ""})'

    return _rewrite_outside_fences(body, _MARKDOWN_LINK_RE, replace)


def restore_page_links(body: str, source_rel: str, page_paths: dict[str, str]) -> str:
    """Translate GROWI permalinks back to local relative Markdown paths."""
    source_dir = posixpath.dirname(source_rel)

    def replace(match: re.Match[str]) -> str:
        target = page_paths.get(match.group("page_id"))
        if not target:
            return match.group(0)
        relative = posixpath.relpath(target, source_dir or ".")
        return f'{match.group("prefix")}{relative}{match.group("fragment") or ""})'

    return _rewrite_outside_fences(body, _PERMALINK_RE, replace)


def _rewrite_outside_fences(body: str, pattern: re.Pattern[str], replace: Any) -> str:
    fence: str | None = None
    output: list[str] = []
    for line in body.splitlines(keepends=True):
        marker = _FENCE_RE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            output.append(line)
        elif line.startswith(("    ", "\t")):
            output.append(line)
        else:
            def outside_link(match: re.Match[str]) -> str:
                prefix = line[:match.start()]
                if prefix.rfind("](") > prefix.rfind(")"):
                    return match.group(0)
                return replace(match)

            output.append(line if fence is not None else pattern.sub(outside_link, line))
    return "".join(output)


def managed_page_markdown(body: str, marker_id: str) -> str | None:
    """Recover locally editable content while excluding unowned remote text."""
    sections = _marked_sections(body)
    if not sections:
        return None
    recovered: list[str] = []
    for chunk_id, (start, end) in sorted(sections.items(), key=lambda item: item[1][0]):
        section = body[start:end]
        if chunk_id in {marker_id, marker_id + "-links"}:
            lines = section.splitlines()
            if lines and _CHUNK_MARKER_RE.fullmatch(lines[0]):
                lines.pop(0)
            if lines and _CHUNK_END_RE.fullmatch(lines[-1]):
                lines.pop()
            section = "\n".join(lines)
        recovered.append(section.strip("\n"))
    return "\n\n".join(part for part in recovered if part).rstrip() + "\n"


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


def split_footer(body: str) -> tuple[str, str]:
    """Split the managed linker footer from the generated body."""
    start = body.find(LINKS_FOOTER_START)
    if start < 0:
        return body, ""
    end = body.find(LINKS_FOOTER_END, start)
    if end < 0:
        raise ValueError("unterminated llm-wiki-links footer")
    end += len(LINKS_FOOTER_END)
    return body[:start].rstrip("\n") + "\n", body[start:end]


def wrap_links(footer: str, *, page_id: str) -> str:
    digest = hashlib.sha256(footer.encode("utf-8")).hexdigest()[:12]
    return (
        f"<!-- chunk: {page_id}-links hash:{digest} -->\n"
        f"{footer.rstrip()}\n"
        f"<!-- chunk-end: {page_id}-links -->"
    )


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
        marker_id = _marker_id(match)
        sections[marker_id] = (
            match.start(),
            end.end() if end and _marker_id(end) == marker_id else boundary,
        )
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


def _complete_page(page: GrowiPage, path: str, body: str) -> GrowiPage:
    if page.path and page.body:
        return page
    return page.model_copy(update={"path": page.path or path, "body": page.body or body})


async def publish_pages(
    client: GrowiClient,
    pages: list[dict[str, str]],
    *,
    mode: str,
    write_path: str,
    root_path: str = "/",
    known_page_ids: dict[str, str] | None = None,
) -> list[GrowiPage]:
    """Resolve every page ID first, then publish stable permalink bodies."""
    current: dict[str, GrowiPage] = {}
    for item in pages:
        path = item["path"]
        body = item["body"]
        assert_publish_path(path, mode=mode, write_path=write_path, root_path=root_path)
        existing = await client.get_page(path=path)
        if existing is None:
            initial_body = _growi_markdown(_image_fallbacks(body))
            existing = _complete_page(await client.create_page(path, initial_body), path, initial_body)
        if not existing.page_id:
            raise ValueError(f"GROWI returned no page ID for {path}")
        current[path] = existing

    page_ids = dict(known_page_ids or {})
    page_ids.update({
        item["local_path"]: current[item["path"]].page_id
        for item in pages
        if item.get("local_path") and current[item["path"]].page_id
    })
    results: list[GrowiPage] = []
    for item in pages:
        path = item["path"]
        existing = current[path]
        body = rewrite_page_links(item["body"], item.get("local_path", ""), page_ids)
        body = await _publish_images(client, body, existing.page_id)
        merged = _growi_markdown(merge_marked_sections(existing.body, body))
        if merged == existing.body:
            results.append(existing)
            continue
        try:
            results.append(_complete_page(
                await client.update_page(existing.page_id, existing.revision_id, merged), path, merged
            ))
        except GrowiAPIError as exc:
            if exc.status_code != 409:
                raise
            refreshed = await client.get_page(path=path)
            if refreshed is None:
                body = _growi_markdown(body)
                results.append(_complete_page(await client.create_page(path, body), path, body))
                continue
            retry_body = _growi_markdown(merge_marked_sections(refreshed.body, body))
            results.append(_complete_page(
                await client.update_page(refreshed.page_id, refreshed.revision_id, retry_body), path, retry_body
            ))
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

    def _document_pages(self, project: Any, rel: str) -> list[dict[str, str]]:
        folder = project.wiki_dir(rel)
        doc_path = self.doc_path(project, rel)
        ranges = _coverage_ranges(folder)
        pages: list[dict[str, str]] = []
        for md in sorted(folder.glob("*.md")):
            name = growi_segment(md.name)
            body = strip_reader_references(md.read_text(encoding="utf-8"))
            page_path = f"{doc_path}/{name}"
            main, footer = split_footer(body)
            page_id = "page" + page_path.replace(" ", "_")
            pages.append({
                "local_path": md.relative_to(project.wiki).as_posix(),
                "path": page_path,
                "body": wrap_page(main, page_id=page_id, ranges=ranges.get(_canonical(md.name), []))
                + "\n\n"
                + wrap_links(footer, page_id=page_id),
            })
        return pages

    def publish_documents(
        self,
        project: Any,
        rels: list[str],
        known_pages: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, GrowiPage]:
        pages: list[dict[str, str]] = []
        document_paths: dict[str, set[str]] = {}
        for rel in dict.fromkeys(rels):
            document_pages = self._document_pages(project, rel)
            pages.extend(document_pages)
            document_paths[self.doc_path(project, rel)] = {page["path"] for page in document_pages}
        if len({page["path"] for page in pages}) != len(pages):
            raise ValueError("multiple local wiki pages resolve to the same GROWI path")
        results = asyncio.run(publish_pages(
            self.client,
            pages,
            mode=self.connection.mode,
            write_path=self.connection.write_path,
            root_path=self.connection.root_path,
            known_page_ids={
                path: str(row.get("page_id"))
                for path, row in (known_pages or {}).items()
                if row.get("page_id")
            },
        ))
        for doc_path, keep in document_paths.items():
            asyncio.run(self._trash_under(doc_path, keep=keep))
        return {item["local_path"]: page for item, page in zip(pages, results)}

    def discover_documents(self, project: Any, rels: list[str]) -> dict[str, GrowiPage]:
        pages = [page for rel in dict.fromkeys(rels) for page in self._document_pages(project, rel)]

        async def fetch() -> list[GrowiPage | None]:
            return [await self.client.get_page(path=page["path"]) for page in pages]

        return {
            item["local_path"]: page
            for item, page in zip(pages, asyncio.run(fetch()))
            if page is not None
        }

    def publish_document(self, project: Any, rel: str) -> list[GrowiPage]:
        return list(self.publish_documents(project, [rel]).values())

    def pull_changes(
        self,
        project: Any,
        published_pages: dict[str, dict[str, Any]],
        unchanged_documents: set[str],
    ) -> tuple[list[str], list[str], set[str]]:
        """Pull non-conflicting edits to publisher-owned page sections."""
        async def fetch() -> dict[str, GrowiPage | None]:
            return {
                local_path: await self.client.get_page(page_id=str(row.get("page_id", "")))
                for local_path, row in published_pages.items()
                if row.get("page_id")
            }

        remote = asyncio.run(fetch())
        page_paths = {
            str(row.get("page_id")): local_path
            for local_path, row in published_pages.items()
            if row.get("page_id")
        }
        pulled: list[str] = []
        conflicts: list[str] = []
        blocked: set[str] = set()
        from graph.wiki.storage import read_json, write_json_atomic, write_text_atomic

        wiki_root = Path(project.wiki).resolve()
        for local_path, page in remote.items():
            row = published_pages[local_path]
            if page is None or page.revision_id == row.get("revision_id"):
                continue
            document = posixpath.dirname(local_path)
            if document not in unchanged_documents:
                blocked.add(document)
                conflicts.append(f"{local_path}: local and GROWI pages both changed")
                continue
            target = (wiki_root / local_path).resolve()
            try:
                target.relative_to(wiki_root)
            except ValueError:
                blocked.add(document)
                conflicts.append(f"{local_path}: invalid local page path")
                continue
            marker_id = "page" + str(row.get("growi_path") or page.path).replace(" ", "_")
            markdown = managed_page_markdown(page.body, marker_id)
            if markdown is None:
                blocked.add(document)
                conflicts.append(f"{local_path}: GROWI ownership markers were removed")
                continue
            markdown = restore_page_links(markdown, local_path, page_paths)
            write_text_atomic(target, markdown)
            pristine = target.parent / "_planning" / "pages" / target.name
            if pristine.exists():
                main, _footer = split_footer(markdown)
                write_text_atomic(pristine, main)
                source_marker = target.parent / "_planning" / "source.json"
                try:
                    raw_rel = str(read_json(source_marker)["raw"])
                except (OSError, KeyError, TypeError, ValueError):
                    raw_rel = ""
                if raw_rel:
                    state_root = Path(project.state_dir(raw_rel))
                    state_page = state_root / "wiki" / target.name
                    if state_page.exists():
                        write_text_atomic(state_page, main)
                        for sidecar in (state_root / "state" / "pages").glob("*.json"):
                            state = read_json(sidecar, default={})
                            if state.get("filename") == target.name:
                                state["content_sha256"] = hashlib.sha256(main.encode("utf-8")).hexdigest()
                                write_json_atomic(sidecar, state)
                                break
            marker = target.parent / "_planning" / "linker.json"
            state = read_json(marker, default={})
            if state.get("status") == "complete":
                state["status"] = "pending"
                write_json_atomic(marker, state)
            row["growi_path"] = page.path
            row["page_id"] = page.page_id
            row["revision_id"] = page.revision_id
            pulled.append(local_path)
        return pulled, conflicts, blocked

    def delete_document(self, project: Any, rel: str) -> int:
        return asyncio.run(self._trash_under(self.doc_path(project, rel), keep=set()))

    def reset(self) -> int:
        """Trash every publisher-marked page below the configured write path."""
        return asyncio.run(self._trash_under(growi_path(self.connection.write_path), keep=set()))

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
