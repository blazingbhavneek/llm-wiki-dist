"""HTTP client for the separate doc-parser service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from graph.wiki.images import reuse_image_descriptions

from .xlsm import apply_manifest, build_manifest

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
    manifest = build_manifest(path)
    with Path(path).open("rb") as handle:
        response = requests.post(
            f"{base_url.rstrip('/')}{LLM_WIKI_PARSE_PATH}",
            params={
                "images": "true",
                "describe_images": "false" if previous_markdown is not None else "true",
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
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("doc-parser returned invalid JSON") from exc
    if not isinstance(payload, dict) or "markdown" not in payload:
        raise RuntimeError(f"doc-parser: {payload.get('error', 'missing markdown') if isinstance(payload, dict) else 'invalid response'}")
    markdown = str(payload["markdown"])
    # ``pages`` is validated when present so a parser regression is caught
    # early, but the graph pipeline is not required to consume it yet. When a
    # manifest reorders markdown, pages would have to be reordered identically;
    # until a downstream consumer needs pages, the pipeline keeps returning the
    # manifest-applied markdown only, exactly as before.
    pages = payload.get("pages")
    if pages is not None and (not isinstance(pages, list) or not all(isinstance(page, str) for page in pages)):
        raise RuntimeError("doc-parser: pages must be a list of strings")
    markdown = apply_manifest(markdown, manifest) if manifest else markdown
    if previous_markdown is not None:
        markdown = reuse_image_descriptions(
            previous_markdown,
            markdown,
            lambda data_url, alt: _describe_image(data_url, alt, settings),
        )
    return markdown


__all__ = ["UnsupportedDocument", "parse_document", "LLM_WIKI_PARSE_PATH"]
