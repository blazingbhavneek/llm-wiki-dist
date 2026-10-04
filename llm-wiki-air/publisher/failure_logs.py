"""Failure evidence lives outside disposable candidate worktrees."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from graph import config
from graph.wiki.storage import write_json_atomic
from graph.workspace.parser_client import safe_endpoint


def _redact(text: str, settings: Any) -> str:
    for name in ("growi_token", "chat_api_key", "embed_api_key"):
        secret = str(getattr(settings, name, "") or "")
        if secret:
            text = text.replace(secret, "[redacted]")
    return re.sub(r"https?://[^\s\"'<>]+", lambda m: safe_endpoint(m.group()), text)


def save_failure(settings: Any, project: Any, run_id: str, attempt: int,
                 rel: str, details: dict[str, Any], transcript: str = "") -> str:
    root = Path(getattr(settings, "failure_log_root", "") or ((config.PROJECT_ROOT or Path.cwd()) / "logs"))
    identity = str(details.get("source_id") or rel)
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    directory = root / "sync" / project.root.name / run_id / key
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"attempt-{attempt}-{uuid.uuid4().hex[:8]}"
    # Redact both exception text and structured evidence, including URLs.
    import json

    body = json.dumps({"path": rel, "attempt": attempt, "recorded_at": time.time(), **details},
                      ensure_ascii=False, default=str)
    write_json_atomic(directory / f"{stem}.json", json.loads(_redact(body, settings)))
    (directory / f"{stem}.log").write_text(_redact(transcript, settings), encoding="utf-8")
    return str(directory / f"{stem}.json")


class AttemptLog:
    def __init__(self, settings: Any, project: Any, run_id: str, attempt: int):
        self.settings, self.project = settings, project
        self.run_id = run_id or ("sync-" + uuid.uuid4().hex)
        self.attempt = attempt
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        self.events: list[dict[str, Any]] = []
        self.started = time.monotonic()

    def __enter__(self):
        logging.getLogger().addHandler(self.handler)
        return self

    def event(self, row: dict[str, Any]) -> None:
        self.events.append(dict(row))

    def save(self, result: dict[str, Any]) -> list[str]:
        jobs = result.get("job_versions") or [{"rel": rel} for rel in result.get("paths", ["<shared>"])]
        return [save_failure(
            self.settings, self.project, self.run_id, self.attempt, job["rel"],
            {**job, "result": result, "events": self.events,
             "elapsed_seconds": round(time.monotonic() - self.started, 2),
             "accepted_base": job.get("base_commit", ""),
             "recovery": "required" if result.get("recovery_required") else "settled; accepted main retained"},
            self.stream.getvalue(),
        ) for job in jobs]

    def __exit__(self, kind, value, tb):
        try:
            if value is not None:
                self.save({"failures": ["".join(traceback.format_exception(kind, value, tb))],
                           "recovery_required": True})
        finally:
            logging.getLogger().removeHandler(self.handler)
            self.handler.close()

