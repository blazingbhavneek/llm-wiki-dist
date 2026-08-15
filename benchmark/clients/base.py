"""Common client contracts and persistent ingestion metadata."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Protocol, Sequence

import benchmark as legacy


ResultCallback = Callable[[dict[str, Any]], None]


class ClientModule(Protocol):
    NAME: str

    def ingest(
        self,
        workspace: Path,
        bundle: legacy.DatasetBundle,
        document_mapping: Sequence[dict[str, str]],
        manifest: dict[str, Any],
        args: SimpleNamespace,
    ) -> dict[str, Any]: ...

    def query(
        self,
        workspace: Path,
        questions: Sequence[legacy.Question],
        args: SimpleNamespace,
        on_result: ResultCallback | None = None,
    ) -> list[dict[str, Any]]: ...


def client_config_fingerprint(name: str, args: SimpleNamespace) -> str:
    common = {
        "client": name,
        "chat_base_url": args.chat_base_url,
        "chat_model": args.chat_model,
        "embed_base_url": args.embed_base_url,
        "embed_model": args.embed_model,
        "embed_dim": args.embed_dim,
        "rerank_base_url": getattr(args, "rerank_base_url", ""),
        "rerank_model": getattr(args, "rerank_model", ""),
        "temperature": args.temperature,
    }
    if name == "vanilla":
        common.update(
            {
                "chunk_tokens": args.chunk_tokens,
                "chunk_overlap": args.chunk_overlap,
                "hybrid_search": "dense+bm25-rrf",
            }
        )
    elif name == "graphrag":
        common.update(
            {
                "method": "standard",
                "query_method": args.graphrag_method,
                "command": args.graphrag_command,
                "settings": args.graphrag_settings,
            }
        )
    payload = json.dumps(common, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_complete_ingestion(
    workspace: Path,
    *,
    name: str,
    dataset_fingerprint: str,
    config_fingerprint: str,
) -> dict[str, Any] | None:
    path = workspace / "ingestion.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        value.get("status") == "complete"
        and value.get("client") == name
        and value.get("dataset_fingerprint") == dataset_fingerprint
        and value.get("config_fingerprint") == config_fingerprint
    ):
        return value
    return None


def assert_compatible_state(
    workspace: Path,
    *,
    name: str,
    dataset_fingerprint: str,
    config_fingerprint: str,
) -> dict[str, Any]:
    state_path = workspace / "ingestion-state.json"
    state: dict[str, Any] = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise legacy.BenchmarkError(
                f"invalid ingestion state for {name}: {state_path}"
            ) from exc
        if state.get("dataset_fingerprint") not in {None, dataset_fingerprint}:
            raise legacy.BenchmarkError(
                f"{workspace} belongs to a different dataset; refusing to mix indexes"
            )
        if state.get("config_fingerprint") not in {None, config_fingerprint}:
            raise legacy.BenchmarkError(
                f"{workspace} was built with different {name} settings"
            )
    return state


def write_state(
    workspace: Path,
    *,
    name: str,
    dataset_fingerprint: str,
    config_fingerprint: str,
    status: str,
    **details: Any,
) -> None:
    legacy.json_dump(
        workspace / "ingestion-state.json",
        {
            "client": name,
            "dataset_fingerprint": dataset_fingerprint,
            "config_fingerprint": config_fingerprint,
            "status": status,
            **details,
        },
    )


def usage_total(categories: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(categories, dict):
        return legacy.summed_usage([])
    return legacy.summed_usage(
        value for value in categories.values() if isinstance(value, dict)
    )
