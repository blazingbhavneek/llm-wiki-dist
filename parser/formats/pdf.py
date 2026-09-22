"""PDF parsing through MinerU with parallel image descriptions."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import time
import zipfile
from pathlib import Path
from pathlib import PurePosixPath

import httpx

from client.llm import LLMClient
from formats.base import BaseParser, ExtractedDocument, ParseOptions, ParseProfile
from utils.markdown_images import embed_markdown_images
from workers import Workers

logger = logging.getLogger("doc-parser.pdf")

_PDF_HEADER = b"%PDF-"

# Explicit page-boundary markers that a MinerU backend may emit into the
# Markdown when no content-list JSON is available. Match the ``N`` as either
# a plain page number or a page number prefixed by a leading ``page``; the
# marker can appear on any line and its span does not depend on Markdown
# structure so a backend can produce it from any source-of-truth.
_PAGE_MARKER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"<!--\s*page[-_ ]?(?:break|index)?\s*[:=]\s*(\d+)\s*-->", re.IGNORECASE),
    re.compile(r"^<!--\s*page\s+(\d+)\s*-->\s*$", re.IGNORECASE | re.MULTILINE),
)
_PDF_PAGE_HEADING_RE = re.compile(r"^##\s*PDF ページ\s+(\d+)\s*$", re.MULTILINE)


class MineruError(RuntimeError):
    """MinerU did not produce a usable Markdown document."""


def _find_markdown(output_dir: Path, input_stem: str) -> Path:
    candidates = sorted(output_dir.rglob("*.md"))
    if not candidates:
        raise MineruError("MinerU completed without producing a Markdown file")

    preferred = [path for path in candidates if path.stem == input_stem]
    if len(preferred) == 1:
        return preferred[0]
    if len(candidates) == 1:
        return candidates[0]

    relative = ", ".join(str(path.relative_to(output_dir)) for path in candidates[:8])
    raise MineruError(f"MinerU produced multiple Markdown files: {relative}")


def _render_content_list(blocks: list[dict]) -> list[str]:
    """Render each ``page_idx`` group as one raw Markdown string.

    Blocks keep their emitted order; block fields (``text``, ``img_path``,
    ``table_body``, captions) are the same fields already used by MinerU's
    own document Markdown, so a page's raw references match the raw references
    in the full document.
    """
    pages: dict[int, list[str]] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        raw_index = block.get("page_idx", block.get("page"))
        try:
            page_index = int(raw_index)
        except (TypeError, ValueError):
            continue
        rendered: list[str] = []
        block_type = block.get("type")
        if block_type == "image":
            img_path = block.get("img_path") or ""
            caption_lines = [str(c) for c in (block.get("img_caption") or []) if str(c).strip()]
            alt = " / ".join(caption_lines) or "PDF ページ画像"
            if img_path:
                rendered.append(f"![{alt}]({img_path})")
            if caption_lines:
                rendered.append("\n".join(caption_lines))
            image_footnote = block.get("img_footnote") or []
            if image_footnote:
                rendered.append("\n".join(str(line) for line in image_footnote if str(line).strip()))
        elif block_type == "table":
            body = str(block.get("table_body") or "").strip()
            caption_lines = [str(c) for c in (block.get("table_caption") or []) if str(c).strip()]
            if caption_lines:
                rendered.append("\n".join(caption_lines))
            if body:
                rendered.append(body)
        else:
            text = str(block.get("text") or "").strip()
            level = block.get("text_level") or 0
            if text:
                if isinstance(level, int) and 1 <= level <= 6:
                    rendered.append(f"{'#' * level} {text}")
                else:
                    rendered.append(text)
        if rendered:
            pages.setdefault(page_index, []).append("\n\n".join(rendered))
    return ["\n\n".join(pages[index]) for index in sorted(pages)]


def _split_by_markers(markdown: str) -> list[str]:
    """Split Markdown into pages using an installed backend's explicit markers.

    Supports both HTML comment page boundaries and ``## PDF ページ N`` style
    headings. If no marker appears, returns an empty list so the caller can
    report a clear ``MineruError`` rather than produce false pages.
    """
    for pattern in _PAGE_MARKER_RES:
        splits = pattern.split(markdown)
        # ``re.split`` with a capture emits [pre, num, section, num, ...].
        if len(splits) > 2 and any(int(splits[i]) >= 1 for i in range(1, len(splits), 2)):
            sections: list[str] = []
            for index in range(2, len(splits), 2):
                section = splits[index].strip()
                if section:
                    sections.append(section)
            if sections:
                return sections
    splits = _PDF_PAGE_HEADING_RE.split(markdown)
    if len(splits) > 1 and any(splits[i] for i in range(1, len(splits), 2)):
        sections = []
        for pair_index in range(1, len(splits), 2):
            page_number = int(splits[pair_index])
            body = splits[pair_index + 1] if pair_index + 1 < len(splits) else ""
            section = f"## PDF ページ {page_number}\n\n{body.strip()}".strip()
            if section:
                sections.append(section)
        return sections
    return []


def split_mineru_pages(markdown_path: Path, output_dir: Path) -> list[str]:
    """Return MinerU's per-page Markdown strings for a parsed PDF.

    Prefers the ``content_list.json`` that the ``pipeline``/``vlm``/
    ``hybrid`` backends emit alongside the Markdown. Each block carries a
    ``page_idx`` and either a ``text``/``img_path``/``table_body`` field, so
    grouping by page reproduces the raw relative image references that appear
    in the document Markdown. When no content-list is available, explicit
    page-boundary markers within the Markdown are used instead. Never infers
    boundaries from headings or blank lines alone.
    """
    preferred: list[Path] = []
    fallback: list[Path] = []
    for candidate in output_dir.rglob("*.json"):
        name = candidate.name.casefold()
        if name.endswith("content_list.json") or name == "content_list.json":
            preferred.append(candidate)
        elif "content_list" in name:
            fallback.append(candidate)
    for content_path in [*sorted(preferred), *sorted(fallback)]:
        try:
            payload = json.loads(content_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload = payload.get("pdf_info") or payload.get("content_list") or []
        if not isinstance(payload, list):
            continue
        pages = _render_content_list(payload)
        if pages:
            return pages

    try:
        markdown_text = markdown_path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - markdown path is created earlier
        raise MineruError(f"MinerU Markdown is unreadable at {markdown_path}: {exc}") from exc

    marker_pages = _split_by_markers(markdown_text)
    if marker_pages:
        return marker_pages

    raise MineruError(
        "MinerU produced no per-page artifact: neither a content-list JSON "
        f"(looked under {output_dir} for content_list.json) nor explicit page "
        f"markers were found in {markdown_path.name}. "
        "Enable MinerU's content_list output or add backend page markers."
    )


def _mineru_api_base_url() -> str:
    url = os.getenv("MINERU_API_URL", "").strip().rstrip("/")
    if not url:
        raise MineruError("MINERU_API_URL must be configured")
    return url if url.endswith("/v1") else f"{url}/v1"


def _safe_extract_zip(payload: bytes, destination: Path) -> None:
    """Extract a MinerU result ZIP without allowing path traversal."""
    root = destination.resolve()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.infolist():
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise MineruError(f"MinerU returned an unsafe ZIP entry: {member.filename}")
            target = (root / Path(*relative.parts)).resolve()
            if target != root and root not in target.parents:
                raise MineruError(f"MinerU returned an unsafe ZIP entry: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))


def _v4_middle_json_to_content_list(payload: dict, output_dir: Path) -> list[dict]:
    """Translate MinerU v4 page blocks to the artifact shape used by this parser."""
    image_files = {
        path.name: path.relative_to(output_dir).as_posix()
        for path in output_dir.glob("images/*")
        if path.is_file()
    }
    blocks: list[dict] = []
    for page in payload.get("pages", []):
        page_idx = page.get("page_idx")
        for block in page.get("blocks", []):
            block_type = block.get("type")
            content = block.get("content") or []
            if block_type == "table":
                captions = [
                    str(item.get("content", ""))
                    for item in content
                    if item.get("type") == "table_caption"
                ]
                bodies = [
                    str(item.get("content", ""))
                    for item in content
                    if item.get("type") == "table_body"
                ]
                blocks.append(
                    {
                        "page_idx": page_idx,
                        "type": "table",
                        "table_caption": captions,
                        "table_body": "\n".join(bodies),
                    }
                )
                continue
            if block_type == "image":
                body = next(
                    (item for item in content if item.get("type") == "image_body"),
                    None,
                )
                body_index = body.get("index") if body else None
                prefix = f"page_{page_idx}_image_body_{body_index}."
                image_name = next(
                    (name for name in image_files if name.startswith(prefix)),
                    None,
                )
                captions = [
                    str(item.get("content", ""))
                    for item in content
                    if item.get("type") == "image_caption"
                ]
                if image_name:
                    blocks.append(
                        {
                            "page_idx": page_idx,
                            "type": "image",
                            "img_path": image_files[image_name],
                            "img_caption": captions,
                        }
                    )
                elif captions:
                    blocks.append(
                        {
                            "page_idx": page_idx,
                            "type": "text",
                            "text": "\n".join(captions),
                        }
                    )
                continue
            text = "\n".join(
                str(item.get("content", ""))
                for item in content
                if item.get("type") == "text"
            ).strip()
            if text:
                blocks.append(
                    {
                        "page_idx": page_idx,
                        "type": "text",
                        "text": text,
                        "text_level": block.get("level", 0),
                    }
                )
    return blocks


def _run_mineru_api_once(pdf_path: str, output_dir: str) -> str:
    source = Path(pdf_path)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    base_url = _mineru_api_base_url()
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    timeout_s = float(os.getenv("MINERU_TIMEOUT_SECONDS", "1800"))
    tier = os.getenv("MINERU_API_TIER", "advanced").strip() or "advanced"
    timeout = httpx.Timeout(connect=10, read=60, write=300, pool=30)

    with httpx.Client(
        base_url=base_url,
        timeout=timeout,
        trust_env=False,
    ) as client:
        health = client.get("/health")
        if health.status_code != 200:
            raise MineruError(
                f"MinerU API health failed: {health.status_code} {health.text[:500]}"
            )
        health_payload = health.json()
        if health_payload.get("status") not in {"ok", "healthy"}:
            raise MineruError(f"MinerU API is not healthy: {health.text[:1000]}")

        upload = client.post(
            "/uploads",
            json={
                "filename": source.name,
                "bytes": len(payload),
                "mime_type": "application/pdf",
                "purpose": "parse",
                "sha256sum": digest,
            },
        )
        upload.raise_for_status()
        upload_payload = upload.json()
        upload_id = upload_payload["id"]
        if upload_payload.get("status") == "completed" and upload_payload.get("file"):
            file_id = upload_payload["file"]["id"]
        else:
            upload_url = upload_payload.get("upload_url") or f"/uploads/{upload_id}/content"
            content = client.put(
                upload_url,
                content=payload,
                headers=upload_payload.get("upload_headers") or {
                    "Content-Type": "application/octet-stream"
                },
            )
            content.raise_for_status()
            complete = client.post(
                f"/uploads/{upload_id}/complete",
                json={"sha256sum": digest},
            )
            complete.raise_for_status()
            file_id = complete.json()["file"]["id"]

        job = client.post(
            "/parse/jobs",
            json={
                "files": [
                    {
                        "source": {"type": "file_id", "file_id": file_id},
                        "page_range": "all",
                    }
                ],
                # MinerU v4 selects accuracy through tiers rather than the old
                # CLI backend names (pipeline/hybrid).
                "tier": tier,
                "ocr_mode": "auto",
                "output_formats": ["markdown", "middle_json", "zip"],
            },
        )
        job.raise_for_status()
        job_id = job.json()["job_id"]
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = client.get(f"/parse/jobs/{job_id}")
            status.raise_for_status()
            status_payload = status.json()
            state = status_payload.get("status")
            if state in {"completed", "partial", "failed", "canceled"}:
                break
            time.sleep(1)
        else:
            raise MineruError(f"MinerU API timed out after {timeout_s:g} seconds")

        if state not in {"completed", "partial"}:
            raise MineruError(json.dumps(status_payload, ensure_ascii=False)[:4000])
        file_result = status_payload.get("files", [{}])[0]
        outputs = file_result.get("output_files") or {}
        zip_reference = outputs.get("zip")
        if zip_reference:
            result_zip = client.get(f"/files/{zip_reference['file_id']}/content")
            result_zip.raise_for_status()
            _safe_extract_zip(result_zip.content, destination)
        else:
            for kind, filename in (("markdown", f"{source.stem}.md"), ("middle_json", "middle_json.json")):
                reference = outputs.get(kind)
                if not reference:
                    raise MineruError(f"MinerU API response omitted {kind}")
                result = client.get(f"/files/{reference['file_id']}/content")
                result.raise_for_status()
                (destination / filename).write_bytes(result.content)

    markdown_candidates = sorted(destination.glob("*.md"))
    if not markdown_candidates:
        raise MineruError("MinerU API returned no Markdown file")
    markdown_path = next(
        (path for path in markdown_candidates if path.name == "markdown.md"),
        markdown_candidates[0],
    )
    middle_path = destination / "middle_json.json"
    if middle_path.exists():
        try:
            middle_payload = json.loads(middle_path.read_text(encoding="utf-8"))
            content_list = _v4_middle_json_to_content_list(middle_payload, destination)
            (destination / "content_list.json").write_text(
                json.dumps(content_list, ensure_ascii=False),
                encoding="utf-8",
            )
        except (OSError, json.JSONDecodeError, TypeError, AttributeError) as exc:
            raise MineruError(f"MinerU API returned invalid middle JSON: {exc}") from exc
    return str(markdown_path)


def run_mineru(pdf_path: str, output_dir: str) -> str:
    """Submit a PDF to the configured MinerU v4 API and return Markdown."""
    _mineru_api_base_url()
    for attempt in range(2):
        try:
            return _run_mineru_api_once(pdf_path, output_dir)
        except (
            MineruError,
            httpx.HTTPError,
            ValueError,
            KeyError,
            AttributeError,
        ) as exc:
            if attempt == 0:
                logger.warning("MinerU API failed; retrying once in 10 seconds: %s", exc)
                time.sleep(10)
                continue
            if isinstance(exc, MineruError):
                raise
            raise MineruError(f"MinerU API request failed: {exc}") from exc
    raise AssertionError("unreachable")


class PdfParser(BaseParser):
    name = "pdf"
    stream_response = True

    @classmethod
    def detect(cls, data: bytes) -> bool:
        return _PDF_HEADER in data[:1024]

    async def _extract(
        self,
        data: bytes,
        image_dir: str,
        options: ParseOptions,
        workers: Workers,
    ) -> ExtractedDocument:
        work_dir = Path(image_dir)
        pdf_path = work_dir / "document.pdf"
        output_dir = work_dir / "mineru-output"
        pdf_path.write_bytes(data)

        markdown_path = Path(
            await workers.run_gpu(
                run_mineru,
                str(pdf_path),
                str(output_dir),
            )
        )
        markdown = markdown_path.read_text(encoding="utf-8")

        if options.profile == ParseProfile.GENERIC:
            # Return raw references plus resolved paths; BaseParser.parse embeds
            # Markdown data URLs into the full document and every page. Page
            # extraction is generic-only: llm-wiki keeps its historical
            # behavior and must not newly depend on a page artifact existing.
            raw_pages = split_mineru_pages(markdown_path, output_dir)
            return ExtractedDocument(
                markdown=markdown,
                pages=raw_pages,
                markdown_path=markdown_path,
                asset_root=output_dir,
            )

        client = (
            LLMClient(
                base_url=options.llm_base_url,
                api_key=options.llm_api_key,
                model=options.llm_model,
            )
            if options.describe_images
            else None
        )
        try:
            # Embed the full document and its page views in one pass. This
            # keeps descriptions identical while scheduling only one LLM call
            # per unique image asset.
            try:
                raw_pages = split_mineru_pages(markdown_path, output_dir)
            except MineruError as exc:
                logger.warning("llm-wiki PDF pages unavailable: %s", exc)
                raw_pages = []
            boundary = "\n<!-- doc-parser-pdf-page-boundary -->\n"
            parts = [markdown, *raw_pages]
            if any(boundary in part for part in parts):
                raise MineruError("reserved PDF page boundary appeared in MinerU output")
            embedded_parts = (
                await embed_markdown_images(
                    boundary.join(parts),
                    markdown_path,
                    output_dir,
                    workers,
                    client.describe_image if client is not None else None,
                )
            ).split(boundary)
            embedded, *embedded_pages = embedded_parts
            return ExtractedDocument(
                markdown=embedded,
                pages=embedded_pages or [embedded],
            )
        finally:
            if client is not None:
                await client.close()
