"""Terminal progress rendering for the foreground sync command."""

from __future__ import annotations

import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - tqdm is an optional terminal aid
    tqdm = None


NAME_KEEP = 5
NAME_THRESHOLD = 25
NAME_JOIN = "....."
LEVEL_WIDTH = 8
STAGE_WIDTH = 14
# 5 + 5 + 5 characters are at most 25 columns when CJK counts as two.
DOC_WIDTH = 25

# Long source paths (CJK folder + file names) eat whole terminal lines, so hide
# the middle of anything that looks like a path or a filename.  URLs stay whole
# so error lines stay diagnosable.
_TOKEN = re.compile(r"""[^\s"',\[\]{}()|=]+""")
_EXTENSION = re.compile(r"\.\w{1,5}$")
_LINE = re.compile(r"^(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?P<rest>.*)$", re.DOTALL)
_TAG = re.compile(r"^\[(?P<tag>[^\]]{1,%d})\]\s*" % STAGE_WIDTH)
# The document column is hoisted from whichever key the sync path happens to log.
_NAMED = (
    (re.compile(r'"document": "([^"]+)"'), '"document": "{}"'),
    (re.compile(r'"file": "([^"]+)"'), '"file": "{}"'),
    # Source paths contain spaces, so a field runs until the next `key=`.
    (re.compile(r"path=(.+?)(?=\s+\w+=|$)"), "path={}"),
)


def _cells(value: str) -> int:
    """Terminal columns a string occupies, counting CJK as two."""
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in value)


def _pad(value: str, width: int) -> str:
    return value + " " * max(0, width - _cells(value))


def shorten_name(value: str, threshold: int = NAME_THRESHOLD) -> str:
    """Keep the first and last NAME_KEEP characters of a name wider than threshold."""
    if _cells(value) <= threshold:
        return value
    return f"{value[:NAME_KEEP]}{NAME_JOIN}{value[-NAME_KEEP:]}"


def _looks_like_name(token: str) -> bool:
    if "://" in token:
        return False
    return "/" in token or bool(_EXTENSION.search(token))


def mask_names(message: str) -> str:
    return _TOKEN.sub(
        lambda match: shorten_name(match.group()) if _looks_like_name(match.group()) else match.group(),
        message,
    )


def _hoist(rest: str) -> tuple[str, str]:
    """Pull the document name out of the message so it is not printed twice."""
    for pattern, _ in _NAMED:
        found = pattern.search(rest)
        if not found:
            continue
        value = found.group(1)
        for _, template in _NAMED:
            pair = template.format(value)
            for needle in (f"{pair}, ", f", {pair}", pair):
                rest = rest.replace(needle, "")
        return value, rest
    return "", rest


class ShortNameFormatter(logging.Formatter):
    """Render `LEVEL STAGE DOCUMENT : info` in fixed left columns.

    The stage is the leading ``[stage]`` of the message or the module logger name;
    the document is the first ``document``/``file``/``path`` the line carries, with
    its middle hidden and the key dropped from the info so nothing repeats.
    """

    def format(self, record: logging.LogRecord) -> str:
        line = mask_names(super().format(record))
        match = _LINE.match(line)
        if not match:
            return line
        rest = match.group("rest").lstrip()
        tag_match = _TAG.match(rest)
        if tag_match:
            stage, rest = tag_match.group("tag"), rest[tag_match.end():]
        else:
            stage = record.name.rsplit(".", 1)[-1]
        document, rest = _hoist(rest)
        info = re.sub(r"\s{2,}", " ", rest).strip()
        columns = (
            _pad(match.group("level"), LEVEL_WIDTH),
            _pad(stage[:STAGE_WIDTH], STAGE_WIDTH),
            _pad(shorten_name(document, DOC_WIDTH), DOC_WIDTH),
        )
        return f"{''.join(columns)}  : {info}".rstrip()


class TqdmStreamHandler(logging.StreamHandler):
    """Log handler that clears the bars first, so every line starts at column 0.

    Plain ``StreamHandler`` output lands in the middle of a redrawn tqdm row,
    which is what makes sync -v look like it is indented with tabs.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if tqdm is None:  # pragma: no cover - tqdm is an optional terminal aid
            super().emit(record)
            return
        with tqdm.external_write_mode(file=self.stream):
            super().emit(record)


@dataclass
class _Phase:
    label: str
    started: float | None = None
    total: int | None = None
    current: int = 0
    completed: int = 0
    reported: bool = False


class SyncProgress:
    """Render one document bar and one transient bar for the active phase.

    Progress is intentionally kept outside the pipeline.  The pipeline emits
    structured events; this class owns terminal redraws and timing only.
    """

    _LABELS = {
        "capture": "capture",
        "parse": "parser",
        "planner": "planner",
        "research": "wiki research",
        "writer": "wiki writer + judge",
        "fast-repair": "wiki repair + judge",
        "linker-entities": "linker entities",
        "linker-edges": "linker main",
        "growi-preflight": "publish preflight",
        "growi-publish": "publish",
        "reset": "reset remote pages",
        "reset-index": "reset index pages",
        "index": "index",
    }

    def __init__(self, *, stream: Any = None) -> None:
        self.stream = stream or sys.stderr
        self._tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._bars_enabled = tqdm is not None and self._tty
        self._documents: set[str] = set()
        self._completed: set[str] = set()
        self._phases: dict[tuple[str, str], _Phase] = {}
        self._active_key: tuple[str, str] | None = None
        self._stage_bar: Any = None
        self._doc_bar: Any = None
        if tqdm is not None:
            self._doc_bar = tqdm(
                total=0,
                desc="Documents",
                unit="doc",
                position=0,
                leave=True,
                dynamic_ncols=True,
                disable=not self._bars_enabled,
                file=self.stream,
            )

    def add_documents(self, paths: Iterable[Any]) -> None:
        for path in paths:
            if isinstance(path, dict):
                path = path.get("to") or path.get("path") or ""
            value = str(path or "")
            if not value or value in self._documents:
                continue
            self._documents.add(value)
        if self._doc_bar is not None:
            self._doc_bar.total = len(self._documents)
            self._doc_bar.refresh()

    @property
    def has_documents(self) -> bool:
        return bool(self._documents)

    def add_scan_result(self, result: dict[str, Any]) -> None:
        moved = result.get("moved") or []
        self.add_documents([
            *result.get("added", []),
            *result.get("updated", []),
            *result.get("deleted", []),
            *(item.get("to", "") if isinstance(item, dict) else item for item in moved),
        ])

    def mark_completed(self, paths: Iterable[Any]) -> None:
        for path in paths:
            value = str(path or "")
            if not value:
                continue
            self.add_documents([value])
            if value in self._completed:
                continue
            self._completed.add(value)
            if self._doc_bar is not None:
                self._doc_bar.update(1)

    def _phase_name(self, event: dict[str, Any]) -> str | None:
        stage = str(event.get("stage") or "")
        step = str(event.get("step") or "")
        if stage in self._LABELS:
            return stage
        if stage == "linker":
            if step in {"chunks", "entities"}:
                return "linker-entities"
            if step in {"edge_target_done", "incremental_scope", "page_curated"}:
                return "linker-edges"
        return None

    @staticmethod
    def _document(event: dict[str, Any]) -> str:
        if str(event.get("stage") or "") in {
            "capture", "growi-publish", "reset", "reset-index", "index"
        }:
            return "<batch>"
        value = event.get("document") or event.get("file") or "<batch>"
        value = str(value)
        if value.startswith("/"):
            value = Path(value).name
        return shorten_name(value)

    def _write(self, message: str) -> None:
        if tqdm is not None:
            tqdm.write(message, file=self.stream)
        else:
            print(message, file=self.stream, flush=True)

    def _close_visible(self) -> None:
        if self._stage_bar is not None:
            self._stage_bar.close()
            self._stage_bar = None
        self._active_key = None

    def _ensure_phase(
        self,
        key: tuple[str, str],
        *,
        total: int | None = None,
    ) -> _Phase:
        phase = self._phases.get(key)
        if phase is None or phase.reported:
            phase = _Phase(self._LABELS[key[1]])
            self._phases[key] = phase
        if phase.started is None:
            phase.started = time.monotonic()
        if total is not None and total > 0:
            phase.total = total
        if self._active_key != key:
            self._close_visible()
            self._active_key = key
            if tqdm is not None:
                self._stage_bar = tqdm(
                    total=phase.total,
                    desc=f"{key[0]} | {phase.label}",
                    unit="item",
                    position=1,
                    leave=False,
                    dynamic_ncols=True,
                    disable=not self._bars_enabled,
                    file=self.stream,
                )
        elif self._stage_bar is not None and phase.total != self._stage_bar.total:
            self._stage_bar.total = phase.total
            self._stage_bar.refresh()
        return phase

    def _update_bar(self, phase: _Phase, *, step: str = "") -> None:
        if self._stage_bar is None:
            return
        desired = max(self._stage_bar.n, phase.current)
        if desired > self._stage_bar.n:
            self._stage_bar.update(desired - self._stage_bar.n)
        if step:
            self._stage_bar.set_postfix_str(step[:80], refresh=False)
        self._stage_bar.refresh()

    def _finish(self, key: tuple[str, str], event: dict[str, Any], *, status: str = "done") -> None:
        phase = self._phases.get(key)
        if phase is None or phase.reported:
            return
        if phase.started is None:
            phase.started = time.monotonic()
        if status == "done" and phase.total is not None:
            phase.current = max(phase.current, phase.total)
        elapsed = time.monotonic() - phase.started
        phase.reported = True
        if self._active_key == key:
            self._update_bar(phase, step=status)
            self._close_visible()
        suffix = "" if status == "done" else f" ({status})"
        self._write(f"[{key[0]}] {phase.label}: {elapsed:.1f}s{suffix}")

    def on_event(self, event: dict[str, Any]) -> None:
        event = dict(event)
        if str(event.get("stage") or "") == "queue-claim":
            self.add_documents(event.get("paths", []))
            return
        phase_name = self._phase_name(event)
        if phase_name is None:
            return
        if phase_name == "fast-repair":
            self._on_repair_event(event)
            return
        document = self._document(event)
        key = (document, phase_name)
        step = str(event.get("step") or "")
        previous = self._phases.get(key)
        if previous is not None and previous.reported and step in {"done", "complete", "batch_done"}:
            return
        total = event.get("total")
        total = total if isinstance(total, int) and total > 0 else None

        if step in {"start", "page_start"}:
            phase = self._ensure_phase(key, total=total)
            if step == "page_start" and phase.total is not None:
                phase.total = max(phase.total, total or phase.total)
            self._update_bar(phase, step=step)
            return

        phase = self._ensure_phase(key, total=total)
        if step in {"page_done", "document_done"}:
            phase.completed += 1
            phase.current = max(phase.current, phase.completed)
            if total is not None:
                phase.total = total
            detail = step
            if event.get("page"):
                detail += f": {shorten_name(str(event['page']), DOC_WIDTH)}"
            if event.get("status"):
                detail += f" ({event['status']})"
            self._update_bar(phase, step=detail)
            if phase.total is not None and phase.completed >= phase.total:
                self._finish(key, event)
            return

        current = event.get("current")
        if isinstance(current, int):
            phase.current = max(phase.current, current)
        self._update_bar(phase, step=step)
        if step in {"done", "complete", "batch_done"}:
            self._finish(key, event)
        elif step in {"failed", "page_failed"}:
            self._finish(key, event, status="failed")

    def _on_repair_event(self, event: dict[str, Any]) -> None:
        """Map concurrent page repair events onto one completion bar per document."""

        document = self._document(event)
        key = (document, "fast-repair")
        step = str(event.get("step") or "")
        if step == "document_started":
            pages = event.get("pages")
            total = pages if isinstance(pages, int) and pages > 0 else None
            phase = self._ensure_phase(key, total=total)
            self._update_bar(phase, step=f"starting {pages or 0} pages")
            return
        if step == "document_done":
            status = str(event.get("status") or "done")
            self._finish(key, event, status="done" if status == "repaired" else status)
            return

        phase = self._ensure_phase(key)
        page = shorten_name(str(event.get("page") or ""), DOC_WIDTH)
        if step in {"page_clean", "page_repaired", "page_review"}:
            phase.completed += 1
            phase.current = max(phase.current, phase.completed)
            label = step.removeprefix("page_")
            if step == "page_review" and int(event.get("selected_attempt") or 0) > 0:
                label += (
                    f" (best attempt {int(event['selected_attempt'])}, "
                    f"score {int(event.get('selected_score') or 0)})"
                )
            self._update_bar(phase, step=f"{label}: {page}")
            if phase.total is not None and phase.completed >= phase.total:
                self._finish(key, event)
            return

        attempt = event.get("attempt")
        detail = step
        if step == "candidate_judged":
            detail = (
                f"judge score {int(event.get('score') or 0)}"
                f", issues {int(event.get('issues') or 0)}"
            )
        elif step == "candidate_rejected":
            detail = f"rejected ({str(event.get('reason') or 'unknown')})"
        if page:
            detail += f": {page}"
        if isinstance(attempt, int):
            detail += f" (attempt {attempt})"
        self._update_bar(phase, step=detail)

    def close(self) -> None:
        self._close_visible()
        if self._doc_bar is not None:
            self._doc_bar.close()
        if self._documents:
            self._write(
                f"Documents: {len(self._completed)}/{len(self._documents)} processed"
            )
