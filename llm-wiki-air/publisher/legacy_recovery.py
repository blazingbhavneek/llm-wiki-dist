"""Byte-verifiable recovery of pure ancestors from project Git history."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from graph.wiki.storage import sha256_text
from publisher.human_changes import HumanStore, editable, footer_span, marker_matches, now

RECOVERY_ALGORITHM = "git-pure-ancestor-v1"


def _git(project: Any, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(project.root), *args], check=True, capture_output=True
    ).stdout


def _show(project: Any, commit: str, path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(project.root), "show", f"{commit}:{path}"],
        check=False,
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


def _json(project: Any, commit: str, path: str) -> dict[str, Any] | None:
    payload = _show(project, commit, path)
    if payload is None:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _sidecars(project: Any, commit: str, directory: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(project.root), "ls-tree", "-r", "--name-only", commit, "--", directory],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines() if result.returncode == 0 else []


def _object_id(project: Any, commit: str, path: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(project.root), "rev-parse", f"{commit}:{path}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def recover_legacy_ancestor(store: HumanStore, raw_rel: str) -> dict[str, Any]:
    """Recover only when Git proves one uncontaminated pure page ancestry."""

    project = store.project
    if not (project.root / ".git").is_dir():
        return {"status": "legacy_pinned", "reason": "no_history"}
    document = store.document(raw_rel)
    legacy = [row for row in document.get("edits", []) if row.get("status") == "legacy_pinned"]
    if not legacy:
        return {"status": "no_legacy_pin", "recovered": 0}
    try:
        commits = _git(project, "rev-list", "--all").decode().splitlines()
    except (subprocess.CalledProcessError, UnicodeDecodeError):
        return {"status": "legacy_pinned", "reason": "history_unavailable"}
    wiki_rel = project.wiki_dir(raw_rel).relative_to(project.root).as_posix()
    state_rel = project.state_dir(raw_rel).relative_to(project.root).as_posix()
    current_source_id = str(document.get("source_id") or "")
    current_seed = str(document.get("document_id_seed") or "")
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for edit in legacy:
        filename = Path(str(edit.get("anchor", {}).get("old_local_path") or "")).name
        for commit in commits:
            ledger = _json(project, commit, "metadata/pipeline.json")
            stamp = _json(project, commit, f"{wiki_rel}/_planning/source.json")
            if ledger is None or stamp is None:
                continue
            source_rows = [
                row for row in (ledger.get("sources") or {}).values()
                if isinstance(row, dict)
                and str(row.get("source_id") or "") == current_source_id
                and str(row.get("id_seed") or "") == current_seed
                and str(row.get("raw_rel") or "") == raw_rel
            ]
            if (
                len(source_rows) != 1
                or str(source_rows[0].get("source_sha256") or stamp.get("sha256") or "") != str(stamp.get("sha256") or "")
                or str(stamp.get("raw") or "") != raw_rel
                or str(stamp.get("id_seed") or "") != current_seed
            ):
                continue
            raw_bytes = _show(project, commit, f"raw/{raw_rel}")
            if raw_bytes is None or hashlib.sha256(raw_bytes).hexdigest() != str(stamp.get("sha256") or ""):
                continue
            body_bytes = _show(project, commit, f"{state_rel}/wiki/{filename}")
            if body_bytes is None:
                continue
            try:
                body = body_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if (
                marker_matches(body)
                or marker_matches(body, "source")
                or footer_span(body) is not None
                or "llm-wiki-links" in body
                or "llm-wiki-bot-ref" in body
            ):
                continue
            sidecar_rows = []
            for sidecar_path in _sidecars(project, commit, f"{state_rel}/state/pages"):
                row = _json(project, commit, sidecar_path)
                if row and row.get("filename") == filename:
                    sidecar_rows.append(row)
            if len(sidecar_rows) != 1:
                continue
            sidecar = sidecar_rows[0]
            if sidecar.get("human_edited") or sidecar.get("content_sha256") != sha256_text(body):
                continue
            key = (edit["edit_id"], sha256_text(body))
            candidates.setdefault(key, {
                "edit_id": edit["edit_id"], "filename": filename, "body": body,
                "sha256": sha256_text(body), "commits": [], "body_blob_ids": [],
                "raw_blob_ids": [], "source_digest": stamp["sha256"],
            })
            candidates[key]["commits"].append(commit)
            candidates[key]["body_blob_ids"].append(_object_id(project, commit, f"{state_rel}/wiki/{filename}"))
            candidates[key]["raw_blob_ids"].append(_object_id(project, commit, f"raw/{raw_rel}"))
    recovered = 0
    evidence: list[dict[str, Any]] = []
    selected: dict[str, dict[str, Any]] = {}
    for edit in legacy:
        choices = [row for (edit_id, _digest), row in candidates.items() if edit_id == edit["edit_id"]]
        if len(choices) != 1:
            return {"status": "legacy_pinned", "reason": "zero_or_multiple_verified_candidates",
                    "recovered": 0, "required": len(legacy)}
        selected[edit["edit_id"]] = choices[0]
    for edit in legacy:
        choice = selected[edit["edit_id"]]
        human = store.get(edit["human_after_blob"])
        document["pages"][choice["filename"]] = {"body_blob": store.put(choice["body"])}
        edit.update({"status": "deleted", "deleted_from_revision": "legacy-recovery",
                     "updated_at": now(), "legacy_recovery_superseded": True})
        evidence.append({
            "algorithm": RECOVERY_ALGORITHM,
            "edit_id": edit["edit_id"],
            "page": choice["filename"],
            "body_sha256": choice["sha256"],
            "source_digest": choice["source_digest"],
            "commits": sorted(choice["commits"]),
            "body_blob_ids": sorted(set(filter(None, choice["body_blob_ids"]))),
            "raw_blob_ids": sorted(set(filter(None, choice["raw_blob_ids"]))),
            "recovered_at": now(),
        })
        store.save(document)
        local_path = str(edit["anchor"]["old_local_path"])
        revision = str(edit.get("last_seen_revision") or "legacy")
        store.capture(
            raw_rel,
            local_path,
            {"marker_id": str(edit["anchor"].get("page_marker_id") or "legacy"),
             "revision_id": "legacy-base", "observed_revision_id": revision},
            editable(choice["body"]),
            editable(human),
            generated_before=choice["body"],
        )
        document = store.document(raw_rel)
        recovered += 1
    document = store.document(raw_rel)
    document["requires_pure_rebuild"] = False
    document.setdefault("legacy_recovery", []).extend(evidence)
    store.save(document)
    store.render(raw_rel)
    store.project_summary()
    return {"status": "recovered", "recovered": recovered, "evidence": evidence}


__all__ = ["RECOVERY_ALGORITHM", "recover_legacy_ancestor"]
