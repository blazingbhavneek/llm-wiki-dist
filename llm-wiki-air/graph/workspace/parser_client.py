"""HTTP client for the separate doc-parser service."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import requests

from graph.wiki.images import reuse_image_descriptions

from .xlsm import apply_manifest, build_manifest

log = logging.getLogger(__name__)


def safe_endpoint(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))

# The graph pipeline always requests the llm-wiki profile so its historical
# image-unit, description, and XLSM lineage behavior is preserved. The generic
# /parse route produces ordinary Markdown and must never be used here.
LLM_WIKI_PARSE_PATH = "/parse/llm-wiki"
IMAGE_DESCRIPTION_PROMPT = (
    "このドキュメント内の画像を、詳細かつ事実的に日本語で説明してください。"
    "図、グラフ、表、スクリーンショットなどの意味のある視覚的な関係を解説し、"
    "判読可能なテキストは文字起こししてください。説明文のみを返してください。"
)


class UnsupportedDocument(RuntimeError):
    pass


def _describe_image(data_url: str, alt: str, settings: Any) -> str:
    prompt = IMAGE_DESCRIPTION_PROMPT
    if alt.strip():
        prompt += f"\nThe document's image alt text is: {alt.strip()}"
    base_url = str(getattr(settings, "chat_base_url", "")).rstrip("/")
    if not base_url:
        raise RuntimeError("image description requires WIKI_CHAT_BASE_URL")
    url = (
        base_url
        if base_url.endswith("/chat/completions")
        else f"{base_url}/chat/completions"
    )
    response = requests.post(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f'Bearer {getattr(settings, "chat_api_key", "")}',
        },
        json={
            "model": str(getattr(settings, "chat_model", "")),
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                ],
            }],
            "max_tokens": 1024,
            "temperature": 0.2,
            "stream": False,
        },
        timeout=(30, float(getattr(settings, "wiki_request_timeout", 300))),
    )
    response.raise_for_status()
    payload = response.json()
    choices = payload.get("choices", []) if isinstance(payload, dict) else []
    content = choices[0].get("message", {}).get("content", "") if choices else ""
    if isinstance(content, list):
        content = "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    description = str(content).strip()
    if not description:
        raise RuntimeError("image description response was empty")
    return description


def parse_document(
    path: Path,
    *,
    base_url: str,
    settings: Any,
    timeout_s: float = 7200,
    previous_markdown: str | None = None,
    describe_images: bool | None = None,
    validate_markdown: Callable[[str], None] | None = None,
) -> str:
    headers = {
        key: value
        for key, value in {
            "X-LLM-Base-URL": getattr(settings, "chat_base_url", ""),
            "X-LLM-API-Key": getattr(settings, "chat_api_key", ""),
            "X-LLM-Model": getattr(settings, "chat_model", ""),
        }.items()
        if value
    }
    if describe_images is None:
        # Settings flag (WIKI_PARSER_DESCRIBE_IMAGES) lets converts run with
        # no vision endpoint; explicit callers still win. Missing attribute
        # means the historical default: describe on fresh converts.
        describe_images = bool(getattr(settings, "parser_describe_images", True))
    manifest = build_manifest(path)
    endpoints = [base_url.rstrip("/")]
    fallback = str(getattr(settings, "parser_fallback_base_url", "") or "").strip().rstrip("/")
    if fallback and fallback not in endpoints:
        endpoints.append(fallback)
    for index, endpoint in enumerate(endpoints):
        started = time.monotonic()
        # Opening/preparing a local source is not an endpoint failure. Each
        # request gets a fresh stream of the same immutable queued source.
        with Path(path).open("rb") as handle:
            try:
                log.info("stage=parser endpoint=%s attempt=%d file=%s timeout=%.0fs",
                         safe_endpoint(endpoint), index + 1, path.name, timeout_s)
                response = requests.post(
                    f"{endpoint}{LLM_WIKI_PARSE_PATH}",
                    params={
                        "images": "true",
                        "describe_images": "true" if (describe_images and previous_markdown is None) else "false",
                    },
                    headers=headers,
                    data={"manifest": json.dumps(manifest, ensure_ascii=False)} if manifest else None,
                    files={"file": (Path(path).name, handle)},
                    timeout=(30, timeout_s),
                )
                if response.status_code == 415:
                    raise UnsupportedDocument(response.text[:200])
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise RuntimeError("doc-parser returned invalid JSON") from exc
                if not isinstance(payload, dict) or "markdown" not in payload:
                    raise RuntimeError("doc-parser: missing markdown or invalid response")
                markdown = payload["markdown"]
                if not isinstance(markdown, str) or not markdown.strip():
                    raise RuntimeError("doc-parser: markdown must be a nonempty string")
                pages = payload.get("pages")
                if pages is not None and (not isinstance(pages, list) or not all(isinstance(page, str) for page in pages)):
                    raise RuntimeError("doc-parser: pages must be a list of strings")
                markdown = apply_manifest(markdown, manifest) if manifest else markdown
                if not markdown.strip():
                    raise RuntimeError("doc-parser: manifest produced empty markdown")
                if validate_markdown is not None:
                    validate_markdown(markdown)
            except Exception:
                log.exception("stage=parser endpoint=%s elapsed=%.2fs fallback=%s",
                              safe_endpoint(endpoint), time.monotonic() - started, index + 1 < len(endpoints))
                if index + 1 == len(endpoints):
                    raise
            else:
                log.info("stage=parser endpoint=%s elapsed=%.2fs characters=%d",
                         safe_endpoint(endpoint), time.monotonic() - started, len(markdown))
                break
    if previous_markdown is not None:
        if describe_images:
            describe = lambda data_url, alt: _describe_image(data_url, alt, settings)
        else:
            # No vision endpoint: still reuse cached descriptions, but leave
            # unseen images undescribed instead of calling the LLM.
            describe = lambda _data_url, _alt: ""
        markdown = reuse_image_descriptions(
            previous_markdown,
            markdown,
            describe,
            repeat_descriptions=Path(path).suffix.lower() != ".pptx",
        )
    return markdown


__all__ = ["UnsupportedDocument", "parse_document", "LLM_WIKI_PARSE_PATH"]
