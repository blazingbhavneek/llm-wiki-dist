"""Shared canonical-store and continuation helpers for dataset adapters."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import benchmark as legacy


Loader = Callable[[SimpleNamespace], legacy.DatasetBundle]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _append_checkpoint(path: Path, value: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_mapping(path: Path) -> dict[str, dict[str, str]]:
    rows: dict[str, dict[str, str]] = {}
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                # A killed append can leave only its final line incomplete.
                break
            if isinstance(value, dict) and value.get("id") and value.get("path"):
                rows[str(value["id"])] = {
                    "id": str(value["id"]),
                    "path": str(value["path"]),
                    "sha256": str(value.get("sha256") or ""),
                }
    return rows


def _document_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_canonical(datastore: Path) -> legacy.DatasetBundle:
    manifest_path = datastore / "manifest.json"
    if not manifest_path.is_file():
        raise legacy.BenchmarkError(
            f"dataset is not prepared: {datastore}; run the ingest command first"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mapping = json.loads(
        (datastore / "canonical" / "documents.json").read_text(encoding="utf-8")
    )
    documents = [
        legacy.Document(
            str(item["id"]),
            Path(item["path"]).read_text(encoding="utf-8"),
        )
        for item in mapping
    ]
    questions = [
        legacy.Question(**row)
        for row in legacy.jsonl_load(datastore / "canonical" / "questions.jsonl")
    ]
    return legacy.DatasetBundle(
        documents,
        questions,
        str(manifest.get("corpus_scope") or "combined"),
    )


def prepare_dataset(
    name: str,
    datastore: Path,
    args: SimpleNamespace,
    loader: Loader,
) -> legacy.DatasetBundle:
    """Load all records and checkpoint canonical documents one at a time.

    Source downloads are themselves atomic and cached by each adapter.  The
    canonical journal makes the more expensive document materialization
    restartable without introducing timestamped datastore versions.
    """

    state_path = datastore / "dataset-state.json"
    manifest_path = datastore / "manifest.json"
    if manifest_path.is_file():
        state = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.is_file()
            else {}
        )
        if state.get("status") == "complete":
            return load_canonical(datastore)

    datastore.mkdir(parents=True, exist_ok=True)
    legacy.json_dump(
        state_path,
        {
            "dataset": name,
            "status": "loading",
            "updated_at": _now(),
            "completed_documents": 0,
        },
    )
    bundle = loader(args)
    documents = [legacy.normalize_long_prose_layout(item) for item in bundle.documents]
    # All three clients receive one complete dataset-level store.  Dataset
    # source labels remain on questions for reporting, not index partitioning.
    bundle = legacy.DatasetBundle(documents, bundle.questions, "combined")
    fingerprint = legacy.dataset_fingerprint(bundle.documents, bundle.questions)

    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("dataset_fingerprint") != fingerprint:
            raise legacy.BenchmarkError(
                f"{datastore} contains a different {name} dataset; "
                "remove that datastore explicitly before rebuilding it"
            )

    canonical = datastore / "canonical"
    corpus = canonical / "corpus"
    journal = canonical / "documents.checkpoint.jsonl"
    completed = _load_mapping(journal)
    mapping: list[dict[str, str]] = []
    for index, document in enumerate(bundle.documents, start=1):
        digest = _document_digest(document.text)
        path = corpus / f"{legacy.safe_name(document.id)}.txt"
        prior = completed.get(document.id)
        reusable = (
            prior is not None
            and prior.get("sha256") == digest
            and path.is_file()
        )
        if not reusable:
            _atomic_text(path, document.text)
            record = {
                "id": document.id,
                "path": str(path.resolve()),
                "sha256": digest,
            }
            _append_checkpoint(journal, record)
            completed[document.id] = record
        mapping.append({"id": document.id, "path": str(path.resolve())})
        legacy.json_dump(
            state_path,
            {
                "dataset": name,
                "status": "materializing",
                "updated_at": _now(),
                "completed_documents": index,
                "total_documents": len(bundle.documents),
                "total_questions": len(bundle.questions),
            },
        )

    legacy.json_dump(canonical / "documents.json", mapping)
    legacy.jsonl_dump(
        canonical / "questions.jsonl",
        (asdict(question) for question in bundle.questions),
    )
    manifest = {
        "dataset": name,
        "dataset_fingerprint": fingerprint,
        "documents": len(bundle.documents),
        "questions": len(bundle.questions),
        "characters": sum(len(item.text) for item in bundle.documents),
        "words": sum(len(item.text.split()) for item in bundle.documents),
        "corpus_scope": "combined",
        "created_at": _now(),
    }
    legacy.json_dump(manifest_path, manifest)
    legacy.json_dump(
        state_path,
        {
            "dataset": name,
            "status": "complete",
            "updated_at": _now(),
            "completed_documents": len(bundle.documents),
            "total_documents": len(bundle.documents),
            "total_questions": len(bundle.questions),
            "dataset_fingerprint": fingerprint,
        },
    )
    return bundle

