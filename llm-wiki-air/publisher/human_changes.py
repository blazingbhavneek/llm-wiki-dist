"""Durable human overlays. Generation owns the base; only remote edits own intent.

The first implementation deliberately uses exact/structural matches and checked
diffs. Uncertain matches retain the complete human block instead of asking a
model to decide whether it may disappear.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from graph.linker.chunks import split_page
from graph.wiki.incremental import line_hunks
from graph.wiki.storage import read_json, sha256_text, write_json_atomic, write_text_atomic

log = logging.getLogger(__name__)
VERSION = 1
RETAINED = "99-Retained-Human-Notes.md"
DASHBOARD = "98-Human-Conflicts.md"
_HEX = re.compile(r"[0-9a-f]{64}")
_REGION = re.compile(
    r"^<!-- llm-wiki-human:(hedit-[0-9a-f]+):start -->\n(.*?)"
    r"^<!-- llm-wiki-human:\1:end -->[ \t]*\n?", re.M | re.S,
)
_SOURCE = re.compile(
    r"\n?<!-- llm-wiki-source:(hedit-[0-9a-f]+):start -->\n(.*?)"
    r"^<!-- llm-wiki-source:\1:end -->[ \t]*\n?", re.M | re.S,
)


class LegacyBaseUnavailable(ValueError):
    """Old reverse-sync state contains human text and cannot be a pure base."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def editable(body: str) -> str:
    """Separate only explicitly managed derived regions; keep unknown content."""
    body = body.replace("\r\n", "\n")
    span = footer_span(body)
    if span is not None:
        start, end = span
        body = body[:start] + body[end:]
    return body.rstrip("\n") + "\n" if body else ""


@dataclass
class Block:
    path: str
    heading: str
    ordinal: int
    text: str
    start: int
    end: int


def blocks(pages: dict[str, str]) -> list[Block]:
    result = []
    for path, body in sorted(pages.items()):
        lines = body.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        chunks = split_page(body)
        atoms = _atomic_ranges(body)
        chunks = [chunk for chunk in chunks
                  if not any(s < offsets[chunk.line_start - 1] < e for s, e in atoms)]
        for ordinal, chunk in enumerate(chunks):
            start = offsets[chunk.line_start - 1]
            end = offsets[chunks[ordinal + 1].line_start - 1] if ordinal + 1 < len(chunks) else len(body)
            result.append(Block(path, chunk.heading, ordinal, body[start:end], start, end))
    return result


def _changes(before: str, after: str) -> list[tuple[int, int, str]]:
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    result = []
    offsets = [0]
    atoms = _atomic_ranges(before)
    for line in old:
        offsets.append(offsets[-1] + len(line))
    for start, count, new_start, new_count in line_hunks(
        before.splitlines(), after.splitlines()
    ):
        first, last = offsets[start - 1], offsets[start - 1 + count]
        replacement = "".join(new[new_start - 1:new_start - 1 + new_count])
        if count == new_count and not any(_overlaps((first, last, ""), (s, e, "")) for s, e in atoms):
            # Line for line, as for a one-line hunk: a hunk of N rewritten lines (two
            # adjacent lines differing only in publication spelling, e.g. stripped
            # inline code) must not become one span that swallows a human token edit.
            token_re = re.compile(r"\d+(?:[.,]\d+)*(?:[ ]?[%°℃\w/]+)?|\w+|[^\w\s]|\s+")
            for line in range(count):
                line_first, line_last = offsets[start - 1 + line], offsets[start + line]
                line_new = new[new_start - 1 + line]
                old_tokens = list(token_re.finditer(before[line_first:line_last]))
                new_tokens = list(token_re.finditer(line_new))
                matcher = SequenceMatcher(a=[t.group() for t in old_tokens], b=[t.group() for t in new_tokens], autojunk=False)
                for tag, i, j, k, l in matcher.get_opcodes():
                    if tag != "equal":
                        s = old_tokens[i].start() if i < len(old_tokens) else line_last - line_first
                        e = old_tokens[j - 1].end() if j > i else s
                        result.append((line_first + s, line_first + e, "".join(t.group() for t in new_tokens[k:l])))
        else:
            result.append((first, last, replacement))
    # git's line comparison omits final-newline-only differences.
    if not result and before != after:
        return [(0, len(before), after)]
    return result


def _overlaps(a: tuple, b: tuple) -> bool:
    s, e, _ = a
    t, u, _ = b
    if s == e and t == u:
        return s == t
    if s == e:
        return t <= s <= u
    if t == u:
        return s <= t <= e
    return max(s, t) < min(e, u)


def _atomic_ranges(text: str) -> list[tuple[int, int]]:
    """Keep code, tables, and image units atomic even when line edits differ."""
    from graph.common.markdown import scan_markdown_fences
    from graph.common.images import find_images

    lines = text.splitlines(keepends=True)
    scan = scan_markdown_fences(lines)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    ranges = []
    for opening, closing in zip(scan.openings, scan.closings):
        ranges.append((offsets[opening.line_number - 1], offsets[closing.line_number]))
    if scan.unclosed:
        ranges.append((offsets[scan.unclosed.line_number - 1], len(text)))
    start = None
    for i, line in enumerate(lines + [""]):
        if line.lstrip().startswith("|") and start is None:
            start = offsets[i]
        elif not line.lstrip().startswith("|") and start is not None:
            ranges.append((start, offsets[i]))
            start = None
    for image in find_images(text):
        ranges.append((image.start, image.end))
    for table in re.finditer(r"<table\b[^>]*>.*?</table>", text, re.I | re.S):
        ranges.append((table.start(), table.end()))
    divider = re.compile(r"^\s*\|?\s*:?-{3,}:?(?:\s*\|\s*:?-{3,}:?)+\s*\|?\s*$")
    for i, line in enumerate(lines):
        if i and divider.fullmatch(line.rstrip("\n")) and "|" in lines[i - 1]:
            end = i + 1
            while end < len(lines) and "|" in lines[end]:
                end += 1
            ranges.append((offsets[i - 1], offsets[end]))
    return ranges


def merge(base: str, human: str, source: str) -> tuple[str, str]:
    """Three-way merge with exact conflict fallback. No side is paraphrased."""
    if human == base:
        return source, "deleted"
    if source == human:
        return source, "absorbed"
    if source == base:
        return human, "active"
    h, g = _changes(base, human), _changes(base, source)
    if set(h) <= set(g):
        return source, "absorbed"
    atoms = _atomic_ranges(base)
    for left in h:
        for right in g:
            if left == right:
                continue
            same_atom = any(_overlaps(left, (s, e, "")) and _overlaps(right, (s, e, "")) for s, e in atoms)
            if _overlaps(left, right) or same_atom:
                return human, "conflict"
    combined = sorted(set(h + g), key=lambda item: (item[0], item[1]), reverse=True)
    output = base
    for start, end, replacement in combined:
        output = output[:start] + replacement + output[end:]
    # Every changed human/source span is protected verbatim by this diff path.
    return output, "active"


def conflict_text(human: str, source: str, edit_id: str) -> str:
    h, g = human.rstrip("\n"), source.rstrip("\n")
    # Human text, including spacing, code and media, is kept verbatim. Source
    # markers also make accepting either variant unambiguous on the next pull.
    if "\n" not in h and "\n" not in g and max(len(h), len(g)) < 200 and not re.search(r"[|`!<>]|^[-*#]", h + g):
        return (h + "\n" + f"<!-- llm-wiki-source:{edit_id}:start -->\n"
                + f"(Updated source document says: {g})\n"
                + f"<!-- llm-wiki-source:{edit_id}:end -->\n")
    return (h + "\n\n" + f"<!-- llm-wiki-source:{edit_id}:start -->\n"
            + "> **Updated source document says:**\n>\n"
            + "\n".join("> " + line for line in g.split("\n")) + "\n"
            + f"<!-- llm-wiki-source:{edit_id}:end -->\n")


def wrap_edit(text: str, edit_id: str) -> str:
    return (f"<!-- llm-wiki-human:{edit_id}:start -->\n" + text + ("" if text.endswith("\n") else "\n")
            + f"<!-- llm-wiki-human:{edit_id}:end -->\n")


def marker_matches(text: str, kind: str = "human") -> list[re.Match]:
    """Parse balanced managed regions, treating fenced examples as content."""
    from graph.common.markdown import scan_markdown_fences

    pattern = _REGION if kind == "human" else _SOURCE
    marker = re.compile(r"<!-- llm-wiki-" + kind + r":(hedit-[0-9a-f]+):(start|end) -->[ \t]*")
    lines = text.splitlines(keepends=True)
    scan = scan_markdown_fences(lines).inside_after_line
    flags = [inside or (i > 0 and scan[i - 1]) for i, inside in enumerate(scan)]
    offset, opened = 0, None
    result, seen = [], set()
    for line, fenced in zip(lines, flags):
        token = None if fenced else marker.fullmatch(line.rstrip("\n"))
        if token:
            edit_id, edge = token.groups()
            if edge == "start":
                if opened is not None or edit_id in seen:
                    raise ValueError(f"duplicate or nested {kind} edit region")
                start = offset - 1 if kind == "source" and offset and text[offset - 1] == "\n" else offset
                opened = (edit_id, start)
            else:
                if opened is None or opened[0] != edit_id:
                    raise ValueError(f"unbalanced {kind} edit markers")
                match = pattern.fullmatch(text, opened[1], offset + len(line))
                if match is None:
                    raise ValueError(f"malformed {kind} edit region")
                result.append(match)
                seen.add(edit_id)
                opened = None
        offset += len(line)
    if opened is not None:
        raise ValueError(f"unbalanced {kind} edit markers")
    return result


def regions(text: str) -> dict[str, str]:
    return {match.group(1): match.group(2) for match in marker_matches(text)}


def substitute_markers(text: str, replace: Any, *, kind: str = "human") -> str:
    parts, cursor = [], 0
    for match in marker_matches(text, kind):
        parts.extend((text[cursor:match.start()], replace(match)))
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts)


def source_candidate(text: str) -> re.Match | None:
    matches = marker_matches(text, "source")
    if len(matches) > 1:
        raise ValueError("multiple source candidates in one human region")
    return matches[0] if matches else None


def strip_sources(text: str) -> str:
    return substitute_markers(text, lambda _match: "", kind="source")


def footer_span(text: str) -> tuple[int, int] | None:
    from graph.common.markdown import LINKS_FOOTER_END, LINKS_FOOTER_START, scan_markdown_fences

    protected = [(match.start(), match.end()) for match in marker_matches(text)]
    lines = text.splitlines(keepends=True)
    scan = scan_markdown_fences(lines).inside_after_line
    flags = [inside or (i > 0 and scan[i - 1]) for i, inside in enumerate(scan)]
    markers, offset = [], 0
    for line, fenced in zip(lines, flags):
        token = line.rstrip("\n")
        if not fenced and token in {LINKS_FOOTER_START, LINKS_FOOTER_END} and not any(s <= offset < e for s, e in protected):
            markers.append((token, offset, offset + len(line)))
        offset += len(line)
    if not markers:
        return None
    if len(markers) != 2 or [item[0] for item in markers] != [LINKS_FOOTER_START, LINKS_FOOTER_END]:
        raise ValueError("damaged managed link footer")
    return markers[0][1], markers[1][2]


def unquote_source(text: str) -> str:
    if text.startswith("(Updated source document says: ") and text.rstrip().endswith(")"):
        return text.rstrip()[len("(Updated source document says: "):-1] + "\n"
    rows = text.splitlines()
    if rows and rows[0] == "> **Updated source document says:**":
        rows = rows[2:]
    return "\n".join(row[2:] if row.startswith("> ") else row for row in rows).rstrip("\n") + "\n"


def strip_regions(text: str) -> str:
    def replace(match: re.Match) -> str:
        body = match.group(2)
        source = source_candidate(body)
        primary = strip_sources(body)
        if source and not primary.strip():
            return unquote_source(source.group(2))
        return primary
    return substitute_markers(text, replace)


def map_generated(text: str, transform: Any) -> str:
    """Apply a publisher/linker transform without touching protected human text."""
    parts, cursor = [], 0
    for region in marker_matches(text):
        parts.extend((transform(text[cursor:region.start()]), region.group(0)))
        cursor = region.end()
    parts.append(transform(text[cursor:]))
    return "".join(parts)


@dataclass
class OverlayResult:
    changed_pages: set[str] = field(default_factory=set)
    conflicts: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)


class HumanStore:
    def __init__(self, project: Any):
        self.project = project
        self.root = Path(project.metadata) / "human-sync"
        schema = self.root / "schema.json"
        if self.root.exists() and not schema.exists():
            raise ValueError("human store schema is missing")
        if schema.exists() and read_json(schema) != {"schema_version": VERSION}:
            raise ValueError("unsupported human store schema")

    def _initialize(self) -> None:
        if not (self.root / "schema.json").exists():
            write_json_atomic(self.root / "schema.json", {"schema_version": VERSION})

    def put(self, text: str) -> str:
        self._initialize()
        digest = sha256_text(text)
        path = self.root / "snapshots" / f"{digest}.md"
        if path.exists():
            self.get(digest)
        else:
            write_text_atomic(path, text)
        return digest

    def get(self, digest: str) -> str:
        if not _HEX.fullmatch(digest):
            raise ValueError("invalid human snapshot hash")
        with (self.root / "snapshots" / f"{digest}.md").open(encoding="utf-8", newline="") as stream:
            text = stream.read()
        if sha256_text(text) != digest:
            raise ValueError(f"human snapshot checksum mismatch: {digest}")
        return text

    def validate(self, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("_blob") and item:
                    self.get(item)
                else:
                    self.validate(item)
        elif isinstance(value, list):
            for item in value:
                self.validate(item)

    def audit(self) -> None:
        """Check every referenced blob before remote mutation, including Tier 0."""
        for directory in ("documents", "pages"):
            for path in (self.root / directory).glob("*.json"):
                data = read_json(path)
                if not isinstance(data, dict) or data.get("schema_version") != VERSION:
                    raise ValueError(f"invalid human store record: {path.name}")
                self.validate(data)

    def identity(self, raw_rel: str) -> tuple[str, dict]:
        stamp = read_json(self.project.wiki_dir(raw_rel) / "_planning" / "source.json", default={})
        seed = str(stamp.get("id_seed") or raw_rel)
        ledger = read_json(self.project.metadata / "pipeline.json", default={})
        row = next((item for item in ledger.get("sources", {}).values()
                    if item.get("raw_rel") == raw_rel or item.get("id_seed") == seed), {})
        stamp["has_source_identity"] = bool(row.get("source_id") or stamp.get("source_id"))
        stamp["source_id"] = str(row.get("source_id") or stamp.get("source_id") or seed)
        return sha256_text(stamp["source_id"]), stamp

    def document(self, raw_rel: str) -> dict:
        key, stamp = self.identity(raw_rel)
        data = read_json(self.root / "documents" / f"{key}.json", default={})
        legacy_key = sha256_text(str(stamp.get("id_seed") or raw_rel))
        migrated = False
        if not data and legacy_key != key:
            data = read_json(self.root / "documents" / f"{legacy_key}.json", default={})
            if data and not data.get("alias_of"):
                if data.get("schema_version") != VERSION or data.get("key") != legacy_key:
                    raise ValueError("invalid legacy human journal")
                self.validate(data)
                old_source_id = str(data.get("source_id") or "")
                if old_source_id not in {str(stamp.get("id_seed") or raw_rel), stamp["source_id"]}:
                    data = {}
                else:
                    migrated = True
                    data.setdefault("legacy_source_ids", []).append(old_source_id)
                    data["key"] = key
                    data["source_id"] = stamp["source_id"]
                    data.setdefault("document_id_seed", str(stamp.get("id_seed") or raw_rel))
                    for edit in data.get("edits", []):
                        edit["source_id"] = stamp["source_id"]
        requested_key = key
        aliases = set()
        while data.get("alias_of"):
            alias = str(data["alias_of"])
            if not _HEX.fullmatch(alias) or alias in aliases:
                raise ValueError("invalid human journal alias")
            aliases.add(alias)
            key = alias
            data = read_json(self.root / "documents" / f"{key}.json")
        if data and stamp["has_source_identity"] and data.get("source_id") != stamp["source_id"]:
            # A reused mount path with a different source identity must not
            # inherit an archived journal from the previous document.
            data, key, migrated = {}, requested_key, False
        if data:
            if data.get("schema_version") != VERSION or data.get("key") != key:
                raise ValueError("invalid human document record")
            self.validate(data)
            if not isinstance(data.get("pages"), dict) or not isinstance(data.get("edits"), list):
                raise ValueError("invalid human journal maps")
            if any(Path(name).name != name or name in {".", ".."} for name in data["pages"]):
                raise ValueError("invalid generated snapshot page path")
            for edit in data["edits"]:
                if edit.get("status") not in {"active", "absorbed", "conflict", "orphaned", "deleted", "resolved", "legacy_pinned"}:
                    raise ValueError("invalid human edit status")
                if edit.get("operation") not in {"add", "replace", "delete"}:
                    raise ValueError("invalid human operation")
            if migrated:
                self.save(data)
                write_json_atomic(self.root / "documents" / f"{legacy_key}.json",
                                  {"schema_version": VERSION, "key": legacy_key, "alias_of": key})
            return data
        return {"schema_version": VERSION, "key": key,
                "source_id": stamp["source_id"], "document_id_seed": str(stamp.get("id_seed") or raw_rel), "raw_rel": raw_rel,
                "pages": {}, "edits": [], "captured_revisions": [], "overlay_pages": []}

    def save(self, document: dict) -> None:
        self._initialize()
        self.validate(document)
        write_json_atomic(self.root / "documents" / f"{document['key']}.json", document)

    def page(self, marker_id: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", marker_id):
            raise ValueError("invalid human page marker")
        data = read_json(self.root / "pages" / f"{marker_id}.json", default={})
        if data:
            if data.get("schema_version") != VERSION or data.get("marker_id") != marker_id:
                raise ValueError("invalid human page baseline")
            self.validate(data)
        return data

    def save_page(self, data: dict) -> None:
        self._initialize()
        self.validate(data)
        write_json_atomic(self.root / "pages" / f"{data['marker_id']}.json", data)

    def record_observation(
        self,
        *,
        mode: str,
        decision: str,
        local_path: str,
        page_id: str,
        revision_id: str,
        before: str,
        after: str,
        proposed_operation: str,
        proposed_status: str,
        match_reason: str,
        algorithm_version: str = "deterministic-observer-v1",
    ) -> dict:
        """Persist a text-redacted, idempotent rollout observation."""

        self._initialize()
        identity = sha256_text(
            "\0".join((mode, local_path, page_id, revision_id, algorithm_version))
        )
        record = {
            "schema_version": VERSION,
            "observation_id": "hobs-" + identity[:24],
            "mode": mode,
            "decision": decision,
            "local_path": local_path,
            "page_id": page_id,
            "revision_id": revision_id,
            "before_sha256": sha256_text(before),
            "after_sha256": sha256_text(after),
            "proposed_operation": proposed_operation,
            "proposed_status": proposed_status,
            "match_reason": match_reason,
            "algorithm_version": algorithm_version,
            "observed_at": now(),
        }
        path = self.root / "observations" / f"{identity}.json"
        if path.exists():
            existing = read_json(path)
            comparable = dict(record)
            comparable["observed_at"] = existing.get("observed_at")
            if existing != comparable:
                raise ValueError("observation identity collision")
            return existing
        write_json_atomic(path, record)
        return record

    def record_event(
        self,
        *,
        mode: str,
        decision: str,
        local_path: str,
        page_id: str = "",
        revision_id: str = "",
        reason: str = "",
    ) -> dict:
        """Persist a redacted safety/audit event without page contents."""

        self._initialize()
        identity = sha256_text("\0".join((mode, decision, local_path, page_id, revision_id, reason)))
        path = self.root / "events" / f"{identity}.json"
        if path.exists():
            return read_json(path)
        record = {
            "schema_version": VERSION,
            "event_id": "hevt-" + identity[:24],
            "mode": mode,
            "decision": decision,
            "local_path": local_path,
            "page_id": page_id,
            "revision_id": revision_id,
            "reason": reason,
            "time": now(),
        }
        write_json_atomic(path, record)
        return record

    def observations(self) -> list[dict]:
        result = []
        for path in sorted((self.root / "observations").glob("*.json")):
            row = read_json(path)
            if not isinstance(row, dict) or row.get("schema_version") != VERSION:
                raise ValueError(f"invalid human observation: {path.name}")
            result.append(row)
        return result

    def events(self) -> list[dict]:
        result = []
        for path in sorted((self.root / "events").glob("*.json")):
            row = read_json(path)
            if not isinstance(row, dict) or row.get("schema_version") != VERSION:
                raise ValueError(f"invalid human event: {path.name}")
            result.append(row)
        return result

    def project_summary(self, *, write: bool = True) -> dict:
        """Return the project-wide operator index without copying protected text."""

        self._initialize()
        rows: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for path in sorted((self.root / "documents").glob("*.json")):
            document = read_json(path)
            if document.get("alias_of"):
                continue
            if document.get("schema_version") != VERSION:
                raise ValueError(f"invalid human document record: {path.name}")
            self.validate(document)
            raw_rel = str(document.get("raw_rel") or "")
            prefix = self.project.wiki_dir(raw_rel).relative_to(self.project.wiki).as_posix() if raw_rel else ""
            dashboard = str(document.get("dashboard_filename") or DASHBOARD)
            retained = str(document.get("retained_filename") or RETAINED)
            for edit in document.get("edits", []):
                status = str(edit.get("status") or "blocked")
                counts[status] = counts.get(status, 0) + 1
                target = edit.get("current_target") or {}
                anchor = edit.get("anchor") or {}
                page = str(target.get("path") or anchor.get("old_local_path") or "")
                rows.append({
                    "edit_id": str(edit.get("edit_id") or ""),
                    "project": self.project.root.name,
                    "document": raw_rel,
                    "page": page,
                    "status": status,
                    "source_id": str(edit.get("source_id") or document.get("source_id") or ""),
                    "first_source_sha256": str(edit.get("first_source_sha256") or document.get("source_sha256") or ""),
                    "first_seen_at": str(edit.get("created_at") or ""),
                    "last_remote_revision": str(edit.get("last_seen_revision") or ""),
                    "last_remote_at": str(edit.get("last_seen_at") or edit.get("updated_at") or ""),
                    "last_applied_source_sha256": str(edit.get("last_applied_source_sha256") or ""),
                    "last_applied_at": str(edit.get("last_applied_at") or ""),
                    "reason": str(edit.get("match_reason") or edit.get("fallback_reason") or ""),
                    "action": "none" if status == "deleted" else "keep-human|accept-source|combine|suppress|retry-match",
                    "dashboard": f"{prefix}/{dashboard}" if prefix else dashboard,
                    "retained": f"{prefix}/{retained}" if prefix else retained,
                })
        for path in sorted((self.root / "pages").glob("*.json")):
            page = read_json(path)
            if not page.get("blocked"):
                continue
            local_path = str(page.get("local_path") or "")
            planning = self.project.wiki / Path(local_path).parent / "_planning" / "source.json"
            source = read_json(planning, default={})
            raw_rel = str(source.get("raw") or "")
            counts["blocked"] = counts.get("blocked", 0) + 1
            rows.append({
                "edit_id": "page-" + str(page.get("marker_id") or path.stem),
                "project": self.project.root.name,
                "document": raw_rel,
                "page": local_path,
                "status": "blocked",
                "source_id": str(page.get("source_id") or ""),
                "first_source_sha256": str(page.get("source_sha256") or ""),
                "first_seen_at": str(page.get("updated_at") or ""),
                "last_remote_revision": str(page.get("observed_revision") or ""),
                "last_remote_at": str(page.get("updated_at") or ""),
                "last_applied_source_sha256": "",
                "last_applied_at": "",
                "reason": str(page.get("blocked") or ""),
                "action": "repair-remote-and-retry",
                "dashboard": str(Path(local_path).parent / DASHBOARD),
                "retained": str(Path(local_path).parent / RETAINED),
            })
        proposal_count = len(self.observations())
        counts["observe_only_proposal"] = proposal_count
        rows.sort(key=lambda row: (row["project"], row["document"], row["page"], row["edit_id"]))
        prior_summary = read_json(self.root / "operator-summary.json", default={})
        stable_payload = {"counts": dict(sorted(counts.items())), "rows": rows}
        prior_stable = {"counts": prior_summary.get("counts", {}), "rows": prior_summary.get("rows", [])}
        result = {
            "schema_version": VERSION,
            "generated_at": (
                prior_summary.get("generated_at")
                if prior_summary.get("schema_version") == VERSION and prior_stable == stable_payload
                else now()
            ),
            "counts": stable_payload["counts"],
            "unresolved": sum(value for key, value in counts.items()
                              if key in {"active", "conflict", "orphaned", "legacy_pinned", "blocked"}),
            "blocked": sum(value for key, value in counts.items() if key in {"legacy_pinned", "blocked"}),
            "rows": rows,
        }
        if write:
            write_json_atomic(self.root / "operator-summary.json", result)
            lines = ["# Human sync operator summary", "", "## Counts", ""]
            lines.extend(f"- {key}: {value}" for key, value in result["counts"].items())
            lines.extend(["", "## Records", ""])
            for row in rows:
                dashboard_link = "../../wiki/" + row["dashboard"]
                lines.append(
                    f"- `{row['edit_id']}` [{row['document']}]({dashboard_link}) "
                    f"status={row['status']} page=`{row['page']}` revision=`{row['last_remote_revision']}` "
                    f"action={row['action']}"
                )
            write_text_atomic(self.root / "operator-summary.md", "\n".join(lines).rstrip() + "\n")
        return result

    def resolve(
        self,
        edit_id: str,
        *,
        action: str,
        expected_revision: str,
        combined_text: str = "",
        document: str = "",
    ) -> dict:
        """Apply one revision-checked operator decision by stable edit ID."""

        allowed = {"keep-human", "accept-source", "combine", "suppress", "delete", "retry-match"}
        if action not in allowed:
            raise ValueError(f"unknown human resolution action: {action}")
        matches: list[tuple[dict, dict]] = []
        for path in sorted((self.root / "documents").glob("*.json")):
            journal = read_json(path)
            if journal.get("alias_of") or (document and journal.get("raw_rel") != document):
                continue
            for edit in journal.get("edits", []):
                if edit.get("edit_id") == edit_id:
                    matches.append((journal, edit))
        if len(matches) != 1:
            raise ValueError("human edit ID is unknown or duplicated")
        journal, edit = matches[0]
        resolution_id = "hresolve-" + sha256_text(
            "\0".join((edit_id, action, expected_revision, sha256_text(combined_text)))
        )[:24]
        for resolution in edit.get("resolution_history", []):
            if resolution.get("resolution_id") == resolution_id:
                return resolution
        if not expected_revision or str(edit.get("last_seen_revision") or "") != expected_revision:
            raise ValueError("stale human resolution revision")
        page_marker = str((edit.get("anchor") or {}).get("page_marker_id") or "")
        baseline = self.page(page_marker) if page_marker and re.fullmatch(r"[A-Za-z0-9_-]+", page_marker) else {}
        if baseline and baseline.get("observed_revision") not in {"", expected_revision}:
            raise ValueError("page changed while resolving human edit")
        raw_rel = str(journal.get("raw_rel") or "")
        folder = self.project.wiki_dir(raw_rel)
        rendered = "\n".join(
            page.read_text(encoding="utf-8") for page in sorted(folder.glob("*.md"))
        )
        if rendered:
            found = sum(1 for match in marker_matches(rendered) if match.group(1) == edit_id)
            if edit.get("status") in {"active", "conflict"} and found != 1:
                raise ValueError("human edit marker is missing or duplicated")
        previous = str(edit.get("status") or "")
        if action == "keep-human":
            if edit.get("conflict", {}).get("source_blob"):
                edit["keep_human_source_blob"] = edit["conflict"]["source_blob"]
            edit["status"] = "active"
        elif action == "accept-source":
            edit.update({"status": "deleted", "deleted_from_revision": expected_revision})
        elif action == "combine":
            if not combined_text:
                raise ValueError("combine requires non-empty combined_text")
            edit["human_after_blob"] = self.put(combined_text)
            base = self.get(edit["base_before_blob"])
            edit["human_delta"] = [
                {"start": start, "end": end, "replacement_blob": self.put(replacement)}
                for start, end, replacement in _changes(base, combined_text)
            ]
            edit["status"] = "active"
        elif action in {"suppress", "delete"}:
            edit.update({"status": "deleted", "deleted_from_revision": expected_revision})
        else:
            edit["status"] = "orphaned"
            edit["current_target"] = {}
        resolution = {
            "resolution_id": resolution_id,
            "action": action,
            "expected_revision": expected_revision,
            "previous_status": previous,
            "result_status": edit["status"],
            "time": now(),
        }
        edit.setdefault("resolution_history", []).append(resolution)
        edit["updated_at"] = resolution["time"]
        self.save(journal)
        self.render(raw_rel)
        self.project_summary()
        return resolution

    def generated(self, raw_rel: str, pages: dict[str, str]) -> None:
        """Called with freshly exported source pages before any overlay/linking."""
        document = self.document(raw_rel)
        document["pages"] = {name: {"body_blob": self.put(text)} for name, text in pages.items()}
        document["raw_rel"] = raw_rel
        document["source_sha256"] = sha256_text(self.project.raw_file(raw_rel).read_text(encoding="utf-8"))
        document["archived"] = False
        document["requires_pure_rebuild"] = False
        self.save(document)

    def ensure_generated(self, raw_rel: str) -> dict:
        document = self.document(raw_rel)
        if document["pages"]:
            return document
        state = self.project.state_dir(raw_rel)
        sidecars = [read_json(path) for path in (state / "state" / "pages").glob("*.json")]
        if any(row.get("human_edited") for row in sidecars):
            raise LegacyBaseUnavailable("legacy generator state contains human edits; rebuild required")
        base = state / "wiki"
        files = [page for page in base.glob("*.md") if page.name != "_review.md"]
        by_name = {row.get("filename"): row for row in sidecars}
        if not files or any(by_name.get(page.name, {}).get("content_sha256") != sha256_text(page.read_text(encoding="utf-8")) for page in files):
            raise LegacyBaseUnavailable("no verified pure generated ancestor; rebuild required")
        document["pages"] = {page.name: {"body_blob": self.put(editable(page.read_text(encoding="utf-8")))}
                             for page in files}
        self.save(document)
        return document

    def prepared_pending(self, marker_id: str, page: Any) -> dict:
        """Return the record of a prepared write that no successful publish recorded.

        Prepared keys are retired once a write is recorded, so their presence means the
        attempt died before the ledger saw it. The record must address this page, and the
        page must have moved on from the revision we inspected, otherwise the write never
        landed and the old baseline stays authoritative.
        """
        data = self.page(marker_id) if marker_id else {}
        if not str(data.get("prepared_remote_blob") or "") or str(data.get("prepared_path") or "") != page.path:
            return {}
        if str(data.get("prepared_page_id") or "") not in ("", page.page_id):
            return {}
        if str(data.get("prepared_revision") or "") == page.revision_id:
            return {}
        attempt_id = str(data.get("prepared_attempt_id") or "")
        # Only an error for this attempt vetoes it.  Stale metadata from an
        # earlier attempt cannot approve or reject a later write.
        if (str(data.get("publication_error") or "")
                and str(data.get("publication_error_attempt_id") or "") == attempt_id):
            return {}
        return data

    def retire_unlanded(self, marker_id: str, page: Any) -> bool:
        """Retire a prepared write proven not to have changed the inspected page."""

        data = self.page(marker_id) if marker_id else {}
        if not data or str(data.get("prepared_revision") or "") != page.revision_id:
            return False
        confirmation = data.get("publication_confirmation") or {}
        if confirmation.get("attempt_id") == data.get("prepared_attempt_id"):
            return False
        self.settle_prepared(data, status="not_landed")
        return True

    def settle_prepared(self, data: dict, *, status: str) -> None:
        attempt_id = str(data.get("prepared_attempt_id") or "")
        for attempt in data.get("attempt_history", []):
            if attempt.get("attempt_id") == attempt_id:
                attempt["status"] = status
                attempt["settled_at"] = now()
        for key in (
            "prepared_attempt_id", "prepared_path", "prepared_page_id",
            "prepared_revision", "prepared_remote_blob", "prepared_local_blob",
            "prepared_generated_blob", "publication_confirmation",
            "publication_error", "publication_error_attempt_id",
        ):
            data.pop(key, None)
        self.save_page(data)

    def prepared_match(self, marker_id: str, page: Any) -> dict:
        """Return the pending prepared record whose exact body the page already holds."""
        data = self.prepared_pending(marker_id, page)
        if not data or self.get(data["prepared_remote_blob"]) != page.body:
            return {}
        return data

    def account_prepared(self, row: dict, page: Any, *, remote: str = "") -> dict:
        """Account for our own write whose response never reached the ledger.

        Returns ``{"exact": True}`` when the page holds exactly that write, otherwise the
        prepared remote/local texts and the writer's pure generated ancestor that a
        later revision builds on. The effective local page is never promoted to pure
        state because it can already contain protected human overlays.
        """
        marker = str(row.get("marker_id") or "")
        data = self.prepared_pending(marker, page)
        if not data:
            return {}
        local_path = str(data.get("local_path") or row.get("local_path") or "")
        body = self.get(str(data["prepared_remote_blob"]))
        local = self.get(str(data["prepared_local_blob"])) if data.get("prepared_local_blob") else ""
        generated_blob = str(data.get("prepared_generated_blob") or "")
        generated = self.get(generated_blob) if generated_blob else ""
        if body == page.body:
            for attempt in data.get("attempt_history", []):
                if attempt.get("attempt_id") == data.get("prepared_attempt_id"):
                    attempt.update({"status": "recovered_exact", "settled_at": now()})
            self.save_page(data)
            row.update({"page_id": page.page_id, "growi_path": page.path, "revision_id": page.revision_id})
            self.remember_page(local_path, row, remote or page.body, local, published=True)
            if generated_blob:
                # The accepted remote revision belongs to the generation saved with
                # this attempt, even if a newer source build now exists locally.
                current = self.page(marker)
                current["generated_blob"] = generated_blob
                self.save_page(current)
            log.debug("human_sync event=adopt_exact_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
            return {"exact": True}
        confirmation = data.get("publication_confirmation") or {}
        # Records written by the accepted TODO-1 implementation predate
        # attempt IDs. They retain the conservative legacy rebase behavior;
        # all newly prepared writes require an exact confirmation chain.
        legacy_prepared = not data.get("prepared_attempt_id")
        if not legacy_prepared and not (
            confirmation.get("attempt_id") == data.get("prepared_attempt_id")
            and confirmation.get("page_id") == page.page_id
            and confirmation.get("path") == page.path
            and confirmation.get("remote_blob") == data.get("prepared_remote_blob")
        ):
            raise ValueError("ambiguous prepared publication outcome")
        log.debug("human_sync event=rebase_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
        return {"exact": False, "remote": body, "local": local, "generated": generated,
                "attempt_id": data.get("prepared_attempt_id")}

    def remember_page(self, local_path: str, row: dict, remote: str, local: str, *, published: bool) -> None:
        marker = row["marker_id"]
        data = self.page(marker)
        data.update({"schema_version": VERSION, "marker_id": marker, "local_path": local_path,
                     "page_id": row["page_id"], "remote_path": row["growi_path"],
                     "accepted_revision": row["revision_id"], "observed_revision": row["revision_id"],
                     "remote_blob": self.put(remote), "local_blob": self.put(local), "blocked": ""})
        if published:
            for key in ("deleted_page_id", "deleted_revision", "deleted_path", "deleted_remote_blob", "deleted_at", "legacy_pinned"):
                data.pop(key, None)
            # A recorded publication settles any prepared write for this page.
            active_attempt = str(data.get("prepared_attempt_id") or "")
            for attempt in data.get("attempt_history", []):
                if attempt.get("attempt_id") == active_attempt and attempt.get("status") in {"prepared", "confirmed"}:
                    attempt.update({"status": "published", "settled_at": now()})
            for key in ("prepared_path", "prepared_page_id", "prepared_revision",
                        "prepared_remote_blob", "prepared_local_blob", "prepared_generated_blob",
                        "prepared_attempt_id", "publication_confirmation",
                        "publication_error", "publication_error_attempt_id"):
                data.pop(key, None)
            data["published_revision"] = row["revision_id"]
            data["published_remote_blob"] = data["remote_blob"]
            data["published_local_blob"] = data["local_blob"]
        if published or not data.get("generated_blob"):
            stamp = read_json(self.project.wiki / Path(local_path).parent / "_planning" / "source.json", default={})
            if stamp.get("raw"):
                document = self.document(str(stamp["raw"]))
                pure = document["pages"].get(Path(local_path).name)
                if pure:
                    data["generated_blob"] = pure["body_blob"]
                    data["source_id"] = document["source_id"]
                    data["source_sha256"] = document.get("source_sha256", "")
        self.save_page(data)
        row.update({"remote_snapshot_blob": data["remote_blob"], "effective_snapshot_blob": data["local_blob"],
                    "observed_revision_id": row["revision_id"], "published_revision_id": data.get("published_revision", "")})

    def block_page(self, local_path: str, row: dict, reason: str, page: Any = None) -> None:
        data = self.page(row["marker_id"])
        data.update({"schema_version": VERSION, "marker_id": row["marker_id"],
                     "local_path": local_path, "blocked": reason, "updated_at": now()})
        if page is not None:
            data.update({"observed_revision": page.revision_id, "observed_remote_blob": self.put(page.body)})
        self.save_page(data)
        row["human_sync_blocked"] = reason
        if page is not None:
            row["observed_revision_id"] = page.revision_id

    def _record(self, document: dict, block: Block, human: str, marker: str, before_revision: str,
                revision: str, ordinal: int, *, legacy: bool = False) -> dict:
        edit_id = "hedit-" + sha256_text(f"{marker}\0{before_revision}\0{revision}\0{ordinal}")[:24]
        page_data = document["pages"].get(Path(block.path).name)
        page_body = self.get(page_data["body_blob"]) if page_data else ""
        page_blocks = blocks({block.path: page_body})
        index = next((i for i, item in enumerate(page_blocks) if item.ordinal == block.ordinal), -1)
        title = next((line[2:] for line in page_body.splitlines() if line.startswith("# ")), "")
        manifest = read_json(self.project.wiki / Path(block.path).parent / "_planning" / "manifest.json", default={})
        ranges = next((item.get("source_ranges", []) for item in manifest.get("files", []) if item.get("filename") == Path(block.path).name), [])
        record = {"schema_version": VERSION, "edit_id": edit_id,
                  "source_id": document["source_id"], "document_id_seed": document.get("document_id_seed", document["source_id"]),
                  "operation": "delete" if not human.strip() else ("add" if not block.text.strip() else "replace"),
                  "status": "legacy_pinned" if legacy else "active",
                  "anchor": {"old_local_path": block.path, "page_marker_id": marker,
                             "heading_path": [block.heading], "ordinal": block.ordinal, "page_title": title,
                             "before_neighbor_hash": sha256_text(page_blocks[index - 1].text) if index > 0 else "",
                             "after_neighbor_hash": sha256_text(page_blocks[index + 1].text) if 0 <= index < len(page_blocks) - 1 else "",
                             "old_source_ranges": ranges},
                  "base_before_blob": self.put(block.text), "human_after_blob": self.put(human),
                  "human_delta": [{"start": s, "end": e, "replacement_blob": self.put(t)}
                                  for s, e, t in _changes(block.text, human)],
                  "created_from_revision": before_revision, "last_seen_revision": revision,
                  "first_source_sha256": str(document.get("source_sha256") or ""),
                  "created_at": now(), "last_seen_at": now(), "updated_at": now(),
                  "current_target": {}, "conflict": {}, "match_reason": "captured_remote_delta"}
        document["edits"].append(record)
        return record

    def pin_legacy(self, raw_rel: str, local_path: str, text: str, *, revision: str = "", replace: bool = False) -> dict:
        document = self.document(raw_rel)
        existing = [e for e in document["edits"] if e["status"] != "deleted" and e["anchor"]["old_local_path"] == local_path]
        if existing and not replace:
            return existing[0]
        for edit in existing:
            edit.update({"status": "deleted", "deleted_from_revision": revision, "updated_at": now()})
        record = self._record(document, Block(local_path, "", 0, "", 0, 0), text,
                     "legacy-" + sha256_text(local_path)[:12], "legacy", revision or sha256_text(text), 0, legacy=True)
        if not document["pages"]:
            document["requires_pure_rebuild"] = True
        self.save(document)
        return record

    def import_captured(self, candidate_project: Any, raw_rels: list[str]) -> None:
        """Rollback generation, retaining the human revisions captured by it.

        Snapshot files are immutable. Only the human journal is imported: the
        candidate's new generated base must never become the rollback base.
        """
        candidate = HumanStore(candidate_project)
        if not candidate.root.exists():
            return
        for path in (candidate.root / "snapshots").glob("*.md"):
            self.put(candidate.get(path.stem))
        for raw_rel in raw_rels:
            current = self.ensure_generated(raw_rel)
            # Moves keep the document seed, even when its raw path changed.
            path = candidate.root / "documents" / f"{current['key']}.json"
            if not path.exists():
                continue
            captured = read_json(path)
            candidate.validate(captured)
            current["edits"] = captured["edits"]
            current["capture_history"] = captured.get("capture_history", [])
            current["captured_revisions"] = captured["captured_revisions"]
            self.save(current)
            self.render(raw_rel)

    def archive(self, raw_rel: str) -> None:
        document = self.document(raw_rel)
        if document["pages"] or document["edits"]:
            document.update({"archived": True, "archived_at": now()})
            self.save(document)

    def move(self, raw_rel: str, old_document: str, new_document: str) -> None:
        document = self.document(raw_rel)
        if not document["pages"] and not document["edits"]:
            return
        document["raw_rel"] = raw_rel
        for edit in document["edits"]:
            target = edit.get("current_target", {})
            if str(target.get("path") or "").startswith(old_document + "/"):
                target["path"] = new_document + target["path"][len(old_document):]
        self.save(document)

    def capture(self, raw_rel: str, local_path: str, row: dict, before: str, remote: str, *,
                generated_before: str | None = None, unlinked: str | None = None) -> None:
        """Capture P -> R; store changes against the inspected generated ancestor.

        ``unlinked`` is the published page before the linker added inline links
        (``_planning/pages``); with it only the human's own change is stored, not the
        linker's links around it.
        """
        document = self.ensure_generated(raw_rel)
        revision_key = f"{row['marker_id']}:{row['revision_id']}:{row['observed_revision_id']}"
        if revision_key in document["captured_revisions"]:
            return
        before, remote = editable(before), editable(remote)
        previous_regions, new_regions = regions(before), regions(remote)
        old_records = {e["edit_id"]: e for e in document["edits"] if e["status"] != "deleted"}
        old_statuses = {edit_id: edit["status"] for edit_id, edit in old_records.items()}
        for record in old_records.values():
            record["last_seen_revision"] = row["observed_revision_id"]
            record["last_seen_at"] = now()

        def commit(count: int) -> None:
            document["captured_revisions"].append(revision_key)
            document.setdefault("capture_history", []).append({
                "revision": row["observed_revision_id"], "page": local_path,
                "before_blob": self.put(before), "after_blob": self.put(remote), "time": now(),
            })
            self.save(document)
            log.debug("human_sync event=capture page=%s operations=%d revision=%s", local_path, count, row["observed_revision_id"])
        # An explicit removal of a stored human region is a tombstone. Edits to
        # its body become a new replacement below, with the original in history.
        for edit_id, body in previous_regions.items():
            record = old_records.get(edit_id)
            if record is None:
                raise ValueError("unknown human edit marker")
            old_source = source_candidate(body)
            new_source = source_candidate(new_regions[edit_id]) if edit_id in new_regions else None
            if new_source and new_source.group(1) != edit_id:
                raise ValueError("source candidate belongs to another edit")
            if edit_id not in new_regions:
                # Missing markers alone are not permission to lose a human fact.
                human = self.get(record["human_after_blob"]).strip()
                if human and human in remote:
                    continue
                if record["status"] == "conflict" and human and remote.strip():
                    source = self.get(record["conflict"]["source_blob"]).strip()
                    if source not in remote:
                        raise ValueError("ambiguous conflict marker removal")
            elif new_regions[edit_id] == body:
                continue
            elif old_source and new_source:
                if old_source.groups() != new_source.groups():
                    raise ValueError("ambiguous edit to the source conflict candidate")
            elif old_source and not new_source:
                human_part = strip_sources(body).rstrip("\n")
                if new_regions[edit_id].rstrip("\n") == human_part:
                    record["keep_human_source_blob"] = record["conflict"]["source_blob"]
                    record["status"] = "active"
                    record["updated_at"] = now()
                    continue
                if new_regions[edit_id].strip() == self.get(record["conflict"]["source_blob"]).strip():
                    record.update({"status": "deleted", "deleted_from_revision": row["observed_revision_id"], "updated_at": now()})
                    continue
            record.update({"status": "deleted", "deleted_from_revision": row["observed_revision_id"],
                           "updated_at": now()})
        if generated_before is None and Path(local_path).name.startswith(Path(RETAINED).stem):
            count = 0
            for edit_id, body in new_regions.items():
                prior = old_records.get(edit_id)
                if prior is None:
                    raise ValueError("unknown retained human edit marker")
                if prior["status"] == "deleted" and body != previous_regions.get(edit_id):
                    anchor = prior["anchor"]
                    block = Block(anchor["old_local_path"], anchor["heading_path"][0], anchor["ordinal"],
                                  self.get(prior["base_before_blob"]), 0, 0)
                    self._record(document, block, strip_sources(body), row["marker_id"], row["revision_id"],
                                 row["observed_revision_id"], count, legacy=old_statuses[edit_id] == "legacy_pinned")
                    count += 1
            old_scaffold = substitute_markers(before, lambda _match: "")
            new_scaffold = substitute_markers(remote, lambda _match: "")
            for edit_id in set(previous_regions) - set(new_regions):
                prior = old_records[edit_id]
                if prior["status"] != "deleted":
                    text = self.get(prior["human_after_blob"])
                    new_scaffold = new_scaffold.replace(text, "", 1)
            for _start, _end, addition in _changes(old_scaffold, new_scaffold):
                if addition.strip():
                    self._record(document, Block(local_path, "Human note", count, "", 0, 0), addition,
                                 row["marker_id"], row["revision_id"], row["observed_revision_id"], count)
                    count += 1
            commit(count)
            return
        old_body, new_body = strip_regions(before), strip_regions(remote)
        # A source-only region left after accepting the source becomes ordinary
        # content; its exact text is compared to the generated base below.
        new_body = substitute_markers(new_body, lambda m: unquote_source(m.group(2)), kind="source")
        old_blocks, new_blocks = blocks({local_path: old_body}), blocks({local_path: new_body})
        generated = {name: self.get(value["body_blob"]) for name, value in document["pages"].items()}
        current_pure = generated.get(Path(local_path).name)
        if generated_before is not None:
            generated[Path(local_path).name] = generated_before
        if generated_before is None or current_pure != generated_before:
            # The local pre-link page belongs to the published page only while the
            # local generation is still the published one (not after a build whose
            # publication failed), so otherwise the whole human block is stored.
            unlinked = None
        prefix = Path(local_path).parent.as_posix()
        pure_blocks = blocks({f"{prefix}/{name}": text for name, text in generated.items()})
        pairs = SequenceMatcher(a=[b.heading for b in old_blocks], b=[b.heading for b in new_blocks], autojunk=False)
        changed_pairs: list[tuple[Block, str]] = []
        for tag, i, j, k, l in pairs.get_opcodes():
            if tag == "equal":
                changed_pairs.extend((old, new.text) for old, new in zip(old_blocks[i:j], new_blocks[k:l])
                                     if old.text != new.text)
            else:
                changed_pairs.extend((old, "") for old in old_blocks[i:j])
                changed_pairs.extend((Block(local_path, new.heading, new.ordinal, "", new.start, new.end), new.text)
                                     for new in new_blocks[k:l])
        plain_blocks = (
            {(b.heading, b.ordinal): b.text for b in blocks({local_path: strip_regions(editable(unlinked))})}
            if unlinked is not None else {}
        )
        for ordinal, (old, human) in enumerate(changed_pairs):
            plain = plain_blocks.get((old.heading, old.ordinal))
            if plain is not None and old.text and human.strip() and plain != old.text:
                # Inline links in the published block are linker output, not human text:
                # apply only the human's own change to the block as it was before linking.
                rebased, status = merge(old.text, human, plain)
                if status != "conflict":
                    human = rebased
            candidates = [b for b in pure_blocks if b.path == local_path and b.heading == old.heading]
            base = candidates[0] if len(candidates) == 1 else Block(old.path, old.heading, old.ordinal, "", 0, 0)
            # Replace the prior intent for this block only as a result of this
            # explicit remote revision, retaining complete historical records.
            for record in document["edits"]:
                target = record.get("current_target") or record["anchor"]
                if record["status"] != "deleted" and target.get("path", target.get("old_local_path")) == old.path and target.get("heading", record["anchor"]["heading_path"][0]) == old.heading:
                    record.update({"status": "deleted", "deleted_from_revision": row["observed_revision_id"], "updated_at": now()})
            if human.rstrip("\n") != base.text.rstrip("\n"):
                self._record(document, base, human, row["marker_id"], row["revision_id"], row["observed_revision_id"], ordinal)
        commit(len(changed_pairs))

    def _match(self, record: dict, candidates: list[Block]) -> Block | None:
        anchor = record["anchor"]
        base = self.get(record["base_before_blob"])
        human = self.get(record["human_after_blob"])
        heading = anchor["heading_path"][0]
        exact = [b for b in candidates if b.text == base or (human and b.text == human)]
        if len(exact) == 1:
            return exact[0]
        headed = [b for b in candidates if b.heading == heading]
        def related(block: Block) -> bool:
            # Heading equality is an anchor hint. Check neighboring text or
            # lexical continuity before attaching old facts to a rewritten topic.
            at = candidates.index(block)
            same_page = [item for item in candidates if item.path == block.path]
            intro = same_page[0].text if same_page else ""
            title = next((line[2:] for line in intro.splitlines() if line.startswith("# ")), "")
            if title and title == anchor.get("page_title") and Path(block.path).name == Path(anchor["old_local_path"]).name:
                return True
            for direction, key in ((-1, "before_neighbor_hash"), (1, "after_neighbor_hash")):
                if 0 <= at + direction < len(candidates) and anchor.get(key) and sha256_text(candidates[at + direction].text) == anchor[key]:
                    return True
            old = "\n".join(line for line in base.splitlines() if not line.startswith("#"))
            new = "\n".join(line for line in block.text.splitlines() if not line.startswith("#"))
            return bool(old.strip() and new.strip() and SequenceMatcher(a=old, b=new, autojunk=False).ratio() >= 0.5)
        if heading and len(headed) == 1 and related(headed[0]):
            return headed[0]
        same_page = [b for b in headed if b.path == anchor["old_local_path"]
                     or b.path == record.get("current_target", {}).get("path")]
        if len(same_page) == 1 and related(same_page[0]):
            return same_page[0]
        return None

    def render(self, raw_rel: str) -> OverlayResult:
        document = self.document(raw_rel)
        result = OverlayResult()
        if not document["pages"]:
            return result
        folder = self.project.wiki_dir(raw_rel)
        prefix = folder.relative_to(self.project.wiki).as_posix()
        pure = {f"{prefix}/{name}": self.get(value["body_blob"]) for name, value in document["pages"].items()}
        candidates = blocks(pure)
        replacements: dict[str, list[tuple[int, int, str]]] = {}
        notes, dashboard = [], []
        def auxiliary_name(default: str, key: str) -> str:
            name = document.get(key, default)
            number = 2
            while f"{prefix}/{name}" in pure:
                name = f"{Path(default).stem}-{number}.md"
                number += 1
            document[key] = name
            return name

        retained_name = auxiliary_name(RETAINED, "retained_filename")
        dashboard_name = auxiliary_name(DASHBOARD, "dashboard_filename")
        occupied = set()
        for record in document["edits"]:
            if record["status"] == "deleted":
                continue
            previous_application = (
                record.get("status"), dict(record.get("current_target") or {}),
                record.get("last_applied_source_sha256", ""),
            )
            base, human = self.get(record["base_before_blob"]), self.get(record["human_after_blob"])
            target = self._match(record, candidates)
            if record["status"] == "legacy_pinned":
                target = None
            if target is None and record["operation"] == "add" and record["status"] != "legacy_pinned":
                original_name = Path(record["anchor"]["old_local_path"]).name
                page_path = f"{prefix}/{original_name}"
                if page_path in pure:
                    target = Block(page_path, record["anchor"]["heading_path"][0], -1,
                                   "", len(pure[page_path]), len(pure[page_path]))
            if target is not None and target.start != target.end and (target.path, target.ordinal) in occupied:
                target = None
            if target is None:
                record["status"] = "orphaned" if record["status"] != "legacy_pinned" else "legacy_pinned"
                record["current_target"] = {}
                record["match_reason"] = "no_unambiguous_deterministic_target"
                result.orphaned.append(record["edit_id"])
                anchor = record["anchor"]
                text = human or ("Human requested deletion of this source block:\n\n" + base)
                notes.append("### " + (anchor["heading_path"][0] or Path(anchor["old_local_path"]).name)
                             + "\n\nFormer page: " + anchor["old_local_path"] + "\n\n"
                             + "No unambiguous source block was found.\n\n" + wrap_edit(text, record["edit_id"]))
                destination = f"{prefix}/{retained_name}"
            else:
                if target.start != target.end:
                    occupied.add((target.path, target.ordinal))
                text, status = merge(base, human, target.text)
                # 'deleted' here means the operation is a no-op, not authority
                # to tombstone it. Only capture creates a deleted record.
                record["status"] = "absorbed" if status == "deleted" else status
                record["current_target"] = {"path": target.path, "heading": target.heading, "ordinal": target.ordinal}
                record["match_reason"] = "deterministic_anchor_match"
                record["conflict"] = {}
                if status == "conflict":
                    if record.get("keep_human_source_blob") == sha256_text(target.text):
                        record["status"] = "active"
                        status = "active"
                    else:
                        text = conflict_text(human, target.text, record["edit_id"])
                if status == "conflict":
                    record["conflict"] = {"source_blob": self.put(target.text), "source_sha256": document.get("source_sha256", ""),
                                          "revision": record["last_seen_revision"], "strategy": "verbatim", "version": VERSION}
                    result.conflicts.append(record["edit_id"])
                if record["status"] != "absorbed":
                    text = wrap_edit(text, record["edit_id"])
                if target.start == target.end and target.start:
                    text = "\n" + text
                replacements.setdefault(target.path, []).append((target.start, target.end, text))
                destination = target.path
            source_sha256 = document.get("source_sha256", "")
            current_application = (record.get("status"), dict(record.get("current_target") or {}), source_sha256)
            record["last_applied_source_sha256"] = source_sha256
            if current_application != previous_application:
                record["last_applied_at"] = now()
            if record["status"] in {"conflict", "orphaned", "legacy_pinned"}:
                dashboard.append(
                    f"- {record['status']}: [{record['anchor']['heading_path'][0] or 'Human note'}]"
                    f"({Path(destination).name}) — `{record['edit_id']}`; "
                    f"source=`{record.get('source_id', '')}`; "
                    f"first_seen=`{record.get('created_at', '')}`; "
                    f"last_revision=`{record.get('last_seen_revision', '')}`; "
                    f"last_applied=`{record.get('last_applied_source_sha256', '')}`; "
                    f"reason={record.get('match_reason', '')}; "
                    "actions=keep-human|accept-source|combine|suppress|retry-match\n"
                )
        effective = dict(pure)
        for path, edits in replacements.items():
            for start, end, text in sorted(edits, reverse=True):
                effective[path] = effective[path][:start] + text + effective[path][end:]
        if notes:
            effective[f"{prefix}/{retained_name}"] = "# Retained Human Notes\n\n" + "\n\n".join(notes)
        if dashboard:
            effective[f"{prefix}/{dashboard_name}"] = "# Human edit conflicts\n\n" + "".join(dashboard)
        applied = set()
        for text in effective.values():
            for edit_id in regions(text):
                if edit_id in applied:
                    raise ValueError("human edit rendered more than once")
                applied.add(edit_id)
        required = {record["edit_id"] for record in document["edits"] if record["status"] not in {"deleted", "absorbed"}}
        if required != applied:
            raise ValueError("rendered human edit coverage mismatch")
        originals = folder / "_planning" / "pages"
        for path, text in effective.items():
            target = self.project.wiki / path
            original = originals / target.name
            prior = original.read_text(encoding="utf-8") if original.exists() else (target.read_text(encoding="utf-8") if target.exists() else None)
            if prior != text:
                write_text_atomic(target, text)
                result.changed_pages.add(path)
            write_text_atomic(original, text)
        for name in set(document.get("overlay_pages", [])) - {Path(path).name for path in effective}:
            for path in (folder / name, originals / name):
                path.unlink(missing_ok=True)
            result.changed_pages.add(f"{prefix}/{name}")
        document["overlay_pages"] = [Path(path).name for path in effective if path not in pure]
        self.save(document)
        if result.changed_pages:
            marker = folder / "_planning" / "linker.json"
            state = read_json(marker, default={})
            if state.get("status") != "disabled":
                state["status"] = "pending"
                write_json_atomic(marker, state)
        log.debug("human_sync event=render source=%s changed=%d conflict=%d orphaned=%d",
                 document["source_id"], len(result.changed_pages), len(result.conflicts), len(result.orphaned))
        return result


def apply_generated(project: Any, raw_rel: str) -> OverlayResult:
    store = HumanStore(project)
    folder = project.wiki_dir(raw_rel)
    pages = {page.name: page.read_text(encoding="utf-8") for page in folder.glob("*.md")}
    store.generated(raw_rel, pages)
    return store.render(raw_rel)
