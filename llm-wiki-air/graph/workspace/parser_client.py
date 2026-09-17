"""HTTP client for the separate doc-parser service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from .xlsm import apply_manifest, build_manifest


class UnsupportedDocument(RuntimeError):
    pass


def parse_document(path: Path, *, base_url: str, settings: Any, timeout_s: float = 7200) -> str:
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
            f"{base_url.rstrip('/')}/parse",
            params={"images": "true", "describe_images": "true"},
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
    return apply_manifest(markdown, manifest) if manifest else markdown


__all__ = ["UnsupportedDocument", "parse_document"]
