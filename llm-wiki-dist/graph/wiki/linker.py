"""Pre-ingestion cross-document wiki linker (docs/LINKER.md).

Runs after a source document's seed/rewrite/intra-link phases and before its
Markdown is considered finished. It compares compact page maps exhaustively
with older same-team documents, uses FTS5/dense/bridge retrieval only as
discovery accelerators, fully reads both endpoints, and publishes reciprocal,
direction-specific managed link blocks plus related-reading footers into the
Markdown itself.

Hard rules encoded here:

* never imports GraphStore / Librarian / Researcher / GROWI;
* Markdown under ``data/wiki/`` is the product; this database is a cache;
* retrieval score is never the admission gate;
* edits happen only inside ``llm-wiki-link`` / ``llm-wiki-related`` markers;
* a relation commits bilaterally or not at all.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from pydantic import ValidationError

from ..project import Project, team_of
from .config import (
    LINKER_BRIDGE_PROMPT_VERSION,
    LINKER_JUDGE_PROMPT_VERSION,
    LINKER_MAP_PROMPT_VERSION,
    LINKER_RESEARCH_PROMPT_VERSION,
)
from .images import scrub_base64
from .prompts import (
    bridge_probe_prompt,
    deep_link_research_prompt,
    link_pair_judge_prompt,
    map_link_scan_prompt,
    oversized_section_note_prompt,
)
from .storage import (
    canonical_json,
    read_json,
    sha256_text,
    write_json_atomic,
    write_text_atomic,
)
from .wire import (
    BridgeProbeResult,
    DeepLinkProposal,
    DeepLinkResearchResult,
    LinkJudgeResult,
    MapLinkCandidate,
    MapLinkScanResult,
    ObservedRange,
    OversizedSectionNote,
)

# ---------------------------------------------------------------------------
# Constants (docs/LINKER.md section 15). Deliberately not environment knobs.
# ---------------------------------------------------------------------------

MAP_BLOCK_CHARS = 24_000
READ_BUNDLE_CHARS = 60_000
DIRECT_K = 24
RERANK_K = 12
MAX_RESEARCH_CANDIDATES = 12
MAX_HOPS = 2
MAX_HOP1 = 4
MAX_HOP2 = 4
MAX_NEW_LINKS_PER_TARGET = 3
MAX_ACTIVE_LINKS_PER_PAGE = 12
MIN_ACCEPT_SCORE = 70
MODEL_ATTEMPTS = 3
CORRECTION_ATTEMPTS = 2
MODEL_CALL_TIMEOUT_SECONDS = 180

SCHEMA_VERSION = 1
RRF_K = 60
OUTPUT_LANGUAGE_DEFAULT = "Japanese (日本語)"

Progress = Callable[[dict[str, Any]], None] | None
StopCheck = Callable[[], bool] | None

LINK_START = "<!-- llm-wiki-link:{pair_id}:start -->"
LINK_END = "<!-- llm-wiki-link:{pair_id}:end -->"
RELATED_START = "<!-- llm-wiki-related:start -->"
RELATED_END = "<!-- llm-wiki-related:end -->"
RELATED_HEADING = "## 関連資料"

_LINK_BLOCK_RE = re.compile(
    r"\n\n<!-- llm-wiki-link:[a-z0-9-]+:start -->.*?\n<!-- llm-wiki-link:[a-z0-9-]+:end -->",
    re.DOTALL,
)
_RELATED_BLOCK_RE = re.compile(
    r"\n\n<!-- llm-wiki-related:start -->.*?\n<!-- llm-wiki-related:end -->\n",
    re.DOTALL,
)

_GENERIC_PATTERNS = (
    "関連があります",
    "さらに詳しくは",
    "関連情報",
    "同じテーマ",
    "似た用語",
    "類似した用語",
    "関連ページを参照",
    "refer to the related page",
    "for more information, see",
)


class LinkerError(RuntimeError):
    """Fatal linker problem: malformed markers, path escape, broken commit."""


class LinkerIncomplete(LinkerError):
    """Model work could not complete; the phase must resume, not 'no links'."""


class LinkerCancelled(LinkerError):
    """User cancellation detected before commit."""


# ---------------------------------------------------------------------------
# IDs, paths, hashes (section 7)
# ---------------------------------------------------------------------------


def _short(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def make_document_id(raw_rel: str) -> str:
    return "ldoc-" + _short(normalize_rel_path(raw_rel))


def make_page_id(document_id: str, wiki_rel_path: str) -> str:
    return "lpage-" + _short(document_id + "\x00" + normalize_rel_path(wiki_rel_path))


def make_pair_id(page_a_id: str, page_b_id: str) -> str:
    return "llink-" + _short("\x00".join(sorted((page_a_id, page_b_id))))


def make_entry_id(page_id: str, entry: dict[str, Any]) -> str:
    return "lmap-" + _short(page_id + canonical_json(entry))


def normalize_rel_path(rel: str) -> str:
    if not rel or "\x00" in rel or rel.startswith("/"):
        raise LinkerError(f"invalid relative path: {rel!r}")
    path = PurePosixPath(rel)
    if any(part == ".." for part in path.parts):
        raise LinkerError(f"path escapes root: {rel!r}")
    return path.as_posix()


def resolve_within(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root``; symlinks resolved before containment."""

    root = Path(root)
    candidate = (root / rel).resolve()
    if root.resolve() not in candidate.parents and candidate != root.resolve():
        raise LinkerError(f"path escapes wiki root: {rel!r}")
    return candidate


def relative_link(from_rel_path: str, to_rel_path: str) -> str:
    source_dir = PurePosixPath(from_rel_path).parent
    target = PurePosixPath(to_rel_path)
    rel = os.path.relpath(target.as_posix(), source_dir.as_posix())
    return rel.replace(os.sep, "/")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _norm_evidence(text: str) -> str:
    """Evidence matching form: whitespace collapsed, Markdown decoration ignored.

    Models habitually drop ``####`` prefixes and `backticks` when quoting; the
    grounding contract is verbatim *content*, not verbatim decoration. Any
    other character change still fails.
    """

    return _norm_ws(re.sub(r"`{1,3}|(?<!\w)[*_]|[*_](?!\w)", "", text))


def _evidence_matches(needle: str, haystack: str) -> bool:
    """Exact excerpt check; also tolerant of pure spacing differences."""

    clean = lambda text: re.sub(r"`{1,3}|(?<!\w)[*_]|[*_](?!\w)", "", text)  # noqa: E731
    if _norm_evidence(needle) in _norm_evidence(haystack):
        return True
    return _norm_ws(clean(needle)).replace(" ", "") in _norm_ws(clean(haystack)).replace(
        " ", ""
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Managed text (section 7.3), anchors, safe link extraction
# ---------------------------------------------------------------------------


def strip_managed_links(text: str, *, path: str = "") -> str:
    stripped = text
    while True:
        new = _LINK_BLOCK_RE.sub("", stripped)
        new = _RELATED_BLOCK_RE.sub("", new)
        if new == stripped:
            break
        stripped = new
    if "llm-wiki-link:" in stripped or "llm-wiki-related:" in stripped:
        raise LinkerError(f"malformed or nested linker marker in {path or '<text>'}")
    return stripped


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_FENCE_RE = re.compile(r"^(```|~~~)")


def extract_heading_anchors(text: str) -> list[dict[str, Any]]:
    """``lead`` plus one anchor per heading outside fences."""

    anchors: list[dict[str, Any]] = [{"anchor_id": "lead", "heading": "(lead)", "line": 1}]
    in_fence = False
    index = 0
    for number, line in enumerate(text.split("\n"), start=1):
        if _FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if match:
            index += 1
            anchors.append(
                {"anchor_id": f"h{index}", "heading": match.group(2), "line": number}
            )
    return anchors


def extract_local_page_links(text: str) -> list[str]:
    """Markdown ``.md`` link targets outside fences/code/images/HTML."""

    targets: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith("<"):
            continue
        line = re.sub(r"`[^`]*`", "", line)  # inline code
        for match in re.finditer(r"(?<!!)\[[^\]]*\]\(\s*([^)\s]+)\s*\)", line):
            target = match.group(1).split("#")[0]
            if target.endswith(".md"):
                targets.append(target)
    return targets


def _ordinary(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return not (
        _FENCE_RE.match(stripped)
        or _HEADING_RE.match(line)
        or stripped.startswith(("|", ">", "<", "![" ))
        or re.match(r"^([-*+]|\d+[.)])\s", stripped)
        or "<!-- llm-wiki-" in stripped
    )


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class MapEntryRecord:
    entry_id: str
    source_start: int
    source_end: int
    title: str
    kind: str
    summary: str
    parent: str
    enumeration_family: str
    flags: dict[str, bool]
    quality: str

    def key(self) -> tuple[Any, ...]:
        return (self.title.strip().lower(), self.kind, self.source_start, self.source_end)

    def card_line(self) -> str:
        flags = "".join(
            "c" if self.flags.get("continues_before") else ""
            for _ in [0]
        ) + ("a" if self.flags.get("continues_after") else "") + (
            "r" if self.flags.get("repeated_format") else ""
        )
        parts = [
            f"- [{self.entry_id}] lines {self.source_start}-{self.source_end}",
            f"| {self.kind or 'note'} | {self.title or '(no title)'}",
        ]
        if self.parent:
            parts.append(f"| parent={self.parent}")
        if self.enumeration_family:
            parts.append(f"| family={self.enumeration_family}")
        if flags:
            parts.append(f"| flags={flags}")
        if self.quality != "observed":
            parts.append(f"| quality={self.quality}")
        if self.summary:
            parts.append(f"| {self.summary}")
        return " ".join(parts)


@dataclass
class WikiPageRecord:
    page_id: str
    document_id: str
    wiki_rel_path: str
    filename: str
    title: str
    chapter: str
    summary: str
    owner_ranges: list[tuple[int, int]]
    reference_ranges: list[tuple[int, int]]
    map_entries: list[MapEntryRecord]
    map_text: str = ""
    map_hash: str = ""
    body_hash: str = ""
    vector_state: str = "pending"
    team: str = ""

    def finalize(self) -> "WikiPageRecord":
        self.map_text = render_page_map(self)
        self.map_hash = sha256_text(self.map_text)
        return self


@dataclass
class WikiDocumentRecord:
    document_id: str
    raw_rel: str
    wiki_rel: str
    team: str
    source_sha256: str
    map_hash: str
    map_quality: str
    pages: list[WikiPageRecord] = field(default_factory=list)

    def finalize(self) -> "WikiDocumentRecord":
        self.map_hash = sha256_text(
            canonical_json(
                sorted((page.wiki_rel_path, page.map_hash) for page in self.pages)
            )
        )
        qualities = {entry.quality for page in self.pages for entry in page.map_entries}
        if "observed" in qualities:
            self.map_quality = "observed"
        elif "mechanical" in qualities or "derived" in qualities:
            self.map_quality = "derived"
        else:
            self.map_quality = "coverage_only"
        return self


@dataclass
class LinkCandidate:
    target_page_id: str
    candidate_page_id: str
    sources: set[str] = field(default_factory=set)
    discovery_path: list[str] = field(default_factory=list)
    relation_type: str = ""
    hypothesis: str = ""
    reader_value: str = ""
    target_map_entry_ids: list[str] = field(default_factory=list)
    candidate_map_entry_ids: list[str] = field(default_factory=list)
    bridge_questions: list[str] = field(default_factory=list)
    priority: int = 0
    overflow: bool = False


@dataclass
class AcceptedLink:
    pair_id: str
    page_a_id: str
    page_b_id: str
    relation_type: str
    discovery_path: list[str]
    evidence: dict[str, Any]
    a_anchor_id: str
    b_anchor_id: str
    a_bridge_template: str
    b_bridge_template: str
    a_footer_reason: str
    b_footer_reason: str
    novelty_score: int
    usefulness_score: int
    confidence_score: int
    target_hashes: dict[str, Any]


# ---------------------------------------------------------------------------
# Page map card (section 8.5) and document blocks (section 9.1)
# ---------------------------------------------------------------------------


def render_page_map(page: WikiPageRecord) -> str:
    lines = [
        f"PAGE ID: {page.page_id}",
        f"DOCUMENT: {page.document_id}",
        f"WIKI PATH: {page.wiki_rel_path}",
        f"TITLE: {page.title}",
        f"CHAPTER: {page.chapter}",
        f"SUMMARY: {page.summary}",
        "OWNED SOURCE: "
        + (", ".join(f"{s}-{e}" for s, e in page.owner_ranges) or "none"),
        "IMPORTED SOURCE: "
        + (", ".join(f"{s}-{e}" for s, e in page.reference_ranges) or "none"),
        "MAP QUALITY: " + (page.map_entries[0].quality if page.map_entries else "none"),
        "OBSERVATIONS:",
    ]
    lines.extend(entry.card_line() for entry in page.map_entries[:40])
    return "\n".join(lines)


def make_document_blocks(pages: Sequence[WikiPageRecord]) -> list[list[WikiPageRecord]]:
    """Pack whole page cards into blocks; a page card is never split."""

    blocks: list[list[WikiPageRecord]] = []
    current: list[WikiPageRecord] = []
    size = 0
    for page in pages:
        card_len = len(page.map_text) + 2
        if current and size + card_len > MAP_BLOCK_CHARS:
            blocks.append(current)
            current, size = [], 0
        current.append(page)
        size += card_len
        if card_len > MAP_BLOCK_CHARS:  # oversized card owns its block
            blocks.append(current)
            current, size = [], 0
    if current:
        blocks.append(current)
    return blocks


def rrf_fuse(rankings: Sequence[Sequence[str]], k: int = RRF_K) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item: (-scores[item], item))


# ---------------------------------------------------------------------------
# Discovery of generated documents (section 8)
# ---------------------------------------------------------------------------


def _canonical_filename(name: str) -> str:
    return re.sub(r"^\d+-", "", name)


def _fallback_title(body: str, filename: str) -> str:
    for line in body.split("\n"):
        match = _HEADING_RE.match(line)
        if match:
            return match.group(2)
    return PurePosixPath(filename).stem


def _fallback_summary(body: str) -> str:
    seen_h1 = False
    for line in body.split("\n"):
        stripped = line.strip()
        if not seen_h1:
            if _HEADING_RE.match(line):
                seen_h1 = True
            continue
        if _ordinary(line) and _HEADING_RE.match(line) is None:
            return stripped[:500]
    return ""


def assign_observations(
    observations: Sequence[ObservedRange],
    pages: list[WikiPageRecord],
    source_line_count: int,
) -> None:
    """Clip every observation into each page it intersects (section 8.3)."""

    for observation in observations:
        if observation.source_start < 1 or observation.source_end < observation.source_start:
            continue
        if source_line_count and observation.source_end > source_line_count:
            continue
        for page in pages:
            for owner_start, owner_end in page.owner_ranges:
                low = max(observation.source_start, owner_start)
                high = min(observation.source_end, owner_end)
                if low > high:
                    continue
                entry = {
                    "source_start": low,
                    "source_end": high,
                    "title": _norm_ws(observation.title),
                    "kind": observation.kind.strip(),
                    "summary": _norm_ws(observation.summary),
                    "parent": observation.parent.strip(),
                    "enumeration_family": observation.enumeration_family.strip(),
                    "flags": {
                        "repeated_format": observation.repeated_format,
                        "continues_before": observation.continues_before,
                        "continues_after": observation.continues_after,
                    },
                    "quality": (
                        "mechanical" if observation.kind == "mechanical" else "observed"
                    ),
                }
                page.map_entries.append(
                    MapEntryRecord(entry_id=make_entry_id(page.page_id, entry), **entry)
                )
    for page in pages:
        page.map_entries = _dedupe_entries(page.map_entries)


def _dedupe_entries(entries: list[MapEntryRecord]) -> list[MapEntryRecord]:
    unique: dict[str, MapEntryRecord] = {}
    for entry in entries:
        key = canonical_json(
            {
                "s": entry.source_start,
                "e": entry.source_end,
                "t": entry.title,
                "k": entry.kind,
                "u": entry.summary,
                "p": entry.parent,
                "f": entry.enumeration_family,
                "g": entry.flags,
                "q": entry.quality,
            }
        )
        unique.setdefault(key, entry)
    entries = list(unique.values())

    by_key: dict[tuple[Any, ...], MapEntryRecord] = {}
    for entry in entries:  # same title/kind/range -> longer summary
        current = by_key.get(entry.key())
        if current is None or len(entry.summary) > len(current.summary):
            by_key[entry.key()] = entry
    entries = list(by_key.values())

    kept: list[MapEntryRecord] = []
    for entry in entries:  # same title/summary, contained range -> container
        contained = None
        contains = None
        for other in kept:
            if (
                other.title == entry.title
                and other.summary == entry.summary
                and other.source_start <= entry.source_start
                and other.source_end >= entry.source_end
            ):
                contained = other  # the new entry is contained: drop it
                break
            if (
                other.title == entry.title
                and other.summary == entry.summary
                and entry.source_start <= other.source_start
                and entry.source_end >= other.source_end
            ):
                contains = other  # the new entry contains an old one
        if contained is not None:
            continue
        if contains is not None:
            kept.remove(contains)
        kept.append(entry)
    kept.sort(key=lambda e: (e.source_start, -e.source_end, e.title, e.kind, e.summary))
    return kept


def _derive_entries(page: WikiPageRecord, body: str) -> None:
    """Degraded map: headings outside fences + first paragraph (section 8.4)."""

    entries: list[MapEntryRecord] = []

    def add(title: str, summary: str) -> None:
        entry = {
            "source_start": page.owner_ranges[0][0] if page.owner_ranges else 1,
            "source_end": page.owner_ranges[-1][1] if page.owner_ranges else 1,
            "title": title,
            "kind": "derived",
            "summary": summary[:300],
            "parent": page.chapter,
            "enumeration_family": "",
            "flags": {},
            "quality": "derived",
        }
        entries.append(MapEntryRecord(entry_id=make_entry_id(page.page_id, entry), **entry))

    add(page.title, page.summary)
    in_fence = False
    lines = body.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        if _FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            index += 1
            continue
        match = _HEADING_RE.match(line) if not in_fence else None
        if match:
            heading = match.group(2)
            summary = ""
            for follow in lines[index + 1 :]:
                if _HEADING_RE.match(follow) or _FENCE_RE.match(follow.strip()):
                    break
                if _ordinary(follow):
                    summary = follow.strip()[:300]
                    break
            if heading != page.title:
                add(heading, summary)
        index += 1
    page.map_entries = _dedupe_entries(entries)


def discover_documents(project: Project) -> list[WikiDocumentRecord]:
    """Reconstruct every generated document's map from writer artifacts."""

    wiki_root = project.wiki.resolve()
    documents: list[WikiDocumentRecord] = []
    if not wiki_root.is_dir():
        return documents
    for manifest_path in sorted(wiki_root.rglob("_planning/manifest.json")):
        document = _discover_one(project, wiki_root, manifest_path)
        if document is not None:
            documents.append(document)
    return documents


def _discover_one(
    project: Project, wiki_root: Path, manifest_path: Path
) -> WikiDocumentRecord | None:
    planning = manifest_path.parent
    folder = planning.parent
    try:
        wiki_rel = normalize_rel_path(folder.resolve().relative_to(wiki_root).as_posix())
    except ValueError:
        return None
    meta = read_json(planning / "metadata.json", {}) or {}
    raw_rel = meta.get("original_file_name") or ""
    if not raw_rel:
        return None
    try:
        raw_rel = normalize_rel_path(raw_rel)
    except LinkerError:
        return None
    team = PurePosixPath(wiki_rel).parts[0] if PurePosixPath(wiki_rel).parts else ""
    if not team or team_of(raw_rel) != team:
        return None  # cross-team or root-escape record: refuse before access

    manifest = read_json(manifest_path, {}) or {}
    coverage = read_json(planning / "coverage.json", {}) or {}
    state_dir = project.state_dir(raw_rel)
    plan = read_json(state_dir / "state" / "plan.json", {}) or {}
    plan_pages = {p.get("filename"): p for p in plan.get("pages", []) if p.get("filename")}
    coverage_pages = {
        _canonical_filename(f.get("filename", "")): f for f in coverage.get("files", [])
    }

    document_id = make_document_id(raw_rel)
    pages: list[WikiPageRecord] = []
    seen: set[str] = set()
    for entry in manifest.get("files", []):
        filename = entry.get("filename", "")
        if (
            not filename.endswith(".md")
            or filename == "index.md"
            or filename.startswith("_")
            or filename.startswith(".")
            or "review" in filename.lower()
        ):
            continue
        wiki_rel_path = normalize_rel_path(f"{wiki_rel}/{filename}")
        if wiki_rel_path in seen:
            raise LinkerError(f"duplicate page path: {wiki_rel_path}")
        seen.add(wiki_rel_path)
        try:
            page_path = resolve_within(wiki_root, wiki_rel_path)
        except LinkerError:
            continue
        body = page_path.read_text(encoding="utf-8") if page_path.exists() else ""

        plan_page = plan_pages.get(filename, {})
        cov = coverage_pages.get(_canonical_filename(filename), {})
        title = plan_page.get("title") or entry.get("title") or cov.get("title") or ""
        summary = plan_page.get("summary") or cov.get("summary") or ""
        chapter = plan_page.get("chapter") or cov.get("header") or ""
        owner = [tuple(r) for r in plan_page.get("owner_ranges", [])] or [
            tuple(r) for r in entry.get("source_ranges", [])
        ]
        if not owner and cov.get("source_start"):
            owner = [(int(cov["source_start"]), int(cov["source_end"]))]
        refs = [tuple(r) for r in plan_page.get("reference_ranges", [])] or [
            tuple(r) for r in entry.get("reference_ranges", [])
        ]
        if not title:
            title = _fallback_title(body, filename)
        if not summary:
            summary = _fallback_summary(strip_managed_links(body, path=str(page_path)))

        page = WikiPageRecord(
            page_id=make_page_id(document_id, wiki_rel_path),
            document_id=document_id,
            wiki_rel_path=wiki_rel_path,
            filename=filename,
            title=title,
            chapter=str(chapter),
            summary=summary,
            owner_ranges=[(int(s), int(e)) for s, e in owner],
            reference_ranges=[(int(s), int(e)) for s, e in refs],
            map_entries=[],
            team=team,
        )
        page = page.finalize()
        pages.append(page)
    if not pages:
        return None

    observations = _load_observations(state_dir, int(plan.get("source_line_count") or 0))
    if observations:
        assign_observations(observations, pages, int(plan.get("source_line_count") or 0))
    else:
        for page in pages:
            body_path = wiki_root / page.wiki_rel_path
            body = (
                strip_managed_links(body_path.read_text(encoding="utf-8"), path=str(body_path))
                if body_path.exists()
                else ""
            )
            if body:
                _derive_entries(page, body)
            else:
                entry = {
                    "source_start": page.owner_ranges[0][0] if page.owner_ranges else 1,
                    "source_end": page.owner_ranges[-1][1] if page.owner_ranges else 1,
                    "title": page.title,
                    "kind": "coverage",
                    "summary": page.summary,
                    "parent": page.chapter,
                    "enumeration_family": "",
                    "flags": {},
                    "quality": "coverage_only",
                }
                page.map_entries = [
                    MapEntryRecord(entry_id=make_entry_id(page.page_id, entry), **entry)
                ]
    for page in pages:
        page.finalize()
        page_path = wiki_root / page.wiki_rel_path
        page.body_hash = sha256_text(
            strip_managed_links(
                page_path.read_text(encoding="utf-8") if page_path.exists() else "",
                path=str(page_path),
            )
        )
    document = WikiDocumentRecord(
        document_id=document_id,
        raw_rel=raw_rel,
        wiki_rel=wiki_rel,
        team=team,
        source_sha256=str(manifest.get("source_sha256", "")),
        map_hash="",
        map_quality="",
        pages=pages,
    ).finalize()
    return document


def _load_observations(state_dir: Path, source_line_count: int) -> list[ObservedRange]:
    live = state_dir / "work" / "observations" / "live"
    if not live.is_dir():
        return []
    observations: list[ObservedRange] = []
    for path in sorted(live.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        window_start = int(data.get("source_start", 0))
        window_end = int(data.get("source_end", 0))
        window_summary = str(data.get("summary", ""))
        for raw in data.get("observations", []):
            try:
                observation = ObservedRange.model_validate(raw)
            except ValidationError:
                continue
            if (
                observation.source_start == window_start
                and observation.source_end == window_end
                and observation.summary == window_summary
            ):
                observation.kind = "mechanical"
            observations.append(observation)
    return observations


# ---------------------------------------------------------------------------
# LinkCatalog (section 6)
# ---------------------------------------------------------------------------


class LinkCatalog:
    """The sole owner of ``metadata/wiki-linker.sqlite``."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self.conn = conn
        self.path = path
        self._vec_ready: bool | None = None

    @classmethod
    def open(cls, path: Path) -> "LinkCatalog":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        catalog = cls(conn, path)
        catalog.migrate()
        return catalog

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                raw_rel TEXT UNIQUE NOT NULL,
                wiki_rel TEXT UNIQUE NOT NULL,
                team TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                map_hash TEXT NOT NULL,
                map_quality TEXT NOT NULL,
                page_count INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pages (
                page_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                wiki_rel_path TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                title TEXT NOT NULL,
                chapter TEXT NOT NULL,
                summary TEXT NOT NULL,
                owner_ranges_json TEXT NOT NULL,
                reference_ranges_json TEXT NOT NULL,
                map_text TEXT NOT NULL,
                map_hash TEXT NOT NULL,
                body_hash TEXT NOT NULL,
                vector_state TEXT NOT NULL DEFAULT 'pending',
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS map_entries (
                entry_id TEXT PRIMARY KEY,
                page_id TEXT NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
                source_start INTEGER NOT NULL,
                source_end INTEGER NOT NULL,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                parent TEXT NOT NULL,
                enumeration_family TEXT NOT NULL,
                flags_json TEXT NOT NULL,
                quality TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS page_edges (
                source_page_id TEXT NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
                target_page_id TEXT NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                pair_id TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(source_page_id, target_page_id, kind, pair_id)
            );
            CREATE TABLE IF NOT EXISTS map_comparisons (
                target_map_hash TEXT NOT NULL,
                document_block_hash TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(target_map_hash, document_block_hash, prompt_version)
            );
            CREATE TABLE IF NOT EXISTS research_cache (
                target_body_hash TEXT NOT NULL,
                bundle_hash TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(target_body_hash, bundle_hash, prompt_version)
            );
            CREATE TABLE IF NOT EXISTS judge_cache (
                target_body_hash TEXT NOT NULL,
                endpoint_body_hash TEXT NOT NULL,
                proposal_hash TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(target_body_hash, endpoint_body_hash, proposal_hash, prompt_version)
            );
            CREATE TABLE IF NOT EXISTS page_discovery_checkpoints (
                page_id TEXT PRIMARY KEY,
                input_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS page_link_checkpoints (
                page_id TEXT PRIMARY KEY,
                input_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS links (
                pair_id TEXT PRIMARY KEY,
                page_a_id TEXT NOT NULL,
                page_b_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                discovery_path_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                a_anchor_id TEXT NOT NULL,
                b_anchor_id TEXT NOT NULL,
                a_bridge_template TEXT NOT NULL,
                b_bridge_template TEXT NOT NULL,
                a_footer_reason TEXT NOT NULL,
                b_footer_reason TEXT NOT NULL,
                novelty_score INTEGER NOT NULL,
                usefulness_score INTEGER NOT NULL,
                confidence_score INTEGER NOT NULL,
                target_hashes_json TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                status TEXT NOT NULL,
                run_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS link_runs (
                run_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                status TEXT NOT NULL,
                affected_pages_json TEXT NOT NULL,
                error TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL
            );
            """
        )
        version = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if version is None:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif version[0] != str(SCHEMA_VERSION):
            raise LinkerError(f"unsupported linker schema version: {version[0]}")
        self.conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5("
            "page_id UNINDEXED, title, chapter, summary, map_text, tokenize='unicode61')"
        )
        self.conn.commit()

    # -- meta ---------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
        self.conn.commit()

    # -- vectors ------------------------------------------------------------

    def _load_vec(self) -> bool:
        if self._vec_ready is not None:
            return self._vec_ready
        try:
            import sqlite_vec

            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            self._vec_ready = True
        except Exception:
            self._vec_ready = False
        return self._vec_ready

    def rebuild_vectors(self, fingerprint: str, dimension: int) -> None:
        stored = self.get_meta("embedding_fingerprint")
        stored_dimension = self.get_meta("embedding_dimension")
        if (stored is not None and stored != fingerprint) or (
            stored_dimension is not None and stored_dimension != str(dimension)
        ):
            self.conn.execute("DROP TABLE IF EXISTS page_vectors")
            self.conn.execute("UPDATE pages SET vector_state='pending'")
            self.conn.execute("DELETE FROM meta WHERE key IN "
                              "('embedding_fingerprint','embedding_dimension')")
        self.set_meta("embedding_fingerprint", fingerprint)
        self.set_meta("embedding_dimension", str(dimension))
        if self._load_vec():
            self.conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS page_vectors USING vec0("
                f"page_id TEXT PRIMARY KEY, embedding float[{int(dimension)}])"
            )
            self.conn.commit()

    def has_vec_table(self) -> bool:
        if not self._load_vec():
            return False
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='page_vectors'"
        ).fetchone()
        return row is not None

    def set_vector(self, page_id: str, vector: list[float]) -> None:
        import sqlite_vec

        self.conn.execute("DELETE FROM page_vectors WHERE page_id=?", (page_id,))
        self.conn.execute(
            "INSERT INTO page_vectors(page_id, embedding) VALUES(?, ?)",
            (page_id, sqlite_vec.serialize_float32(vector)),
        )

    def vector_search(self, query: list[float], limit: int) -> list[str]:
        if not self.has_vec_table():
            return []
        import sqlite_vec

        rows = self.conn.execute(
            "SELECT page_id FROM page_vectors WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (sqlite_vec.serialize_float32(query), max(1, limit * 3)),
        ).fetchall()
        return [row[0] for row in rows]

    # -- sync ---------------------------------------------------------------

    def sync_document(self, document: WikiDocumentRecord) -> dict[str, list[str]]:
        """Upsert by hash. Returns classification against previous active rows."""

        now = _now()
        old = {
            row["wiki_rel_path"]: (row["map_hash"], row["body_hash"], row["page_id"])
            for row in self.conn.execute(
                "SELECT wiki_rel_path, map_hash, body_hash, page_id FROM pages "
                "WHERE document_id=? AND active=1",
                (document.document_id,),
            )
        }
        classification: dict[str, list[str]] = {
            "unchanged": [],
            "changed": [],
            "new": [],
            "removed": [],
        }
        current_paths = {page.wiki_rel_path for page in document.pages}
        for path, (_, _, page_id) in old.items():
            if path not in current_paths:
                classification["removed"].append(page_id)
        row = self.conn.execute(
            "SELECT map_hash, source_sha256 FROM documents WHERE document_id=?",
            (document.document_id,),
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO documents(document_id, raw_rel, wiki_rel, team, source_sha256,"
                " map_hash, map_quality, page_count, active, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,1,?)",
                (
                    document.document_id,
                    document.raw_rel,
                    document.wiki_rel,
                    document.team,
                    document.source_sha256,
                    document.map_hash,
                    document.map_quality,
                    len(document.pages),
                    now,
                ),
            )
        else:
            self.conn.execute(
                "UPDATE documents SET source_sha256=?, map_hash=?, map_quality=?,"
                " page_count=?, active=1, updated_at=? WHERE document_id=?",
                (
                    document.source_sha256,
                    document.map_hash,
                    document.map_quality,
                    len(document.pages),
                    now,
                    document.document_id,
                ),
            )
        for page in document.pages:
            previous = old.get(page.wiki_rel_path)
            if previous is None:
                classification["new"].append(page.page_id)
            elif (previous[0], previous[1]) != (page.map_hash, page.body_hash):
                classification["changed"].append(page.page_id)
            else:
                classification["unchanged"].append(page.page_id)
            self._upsert_page(page, now)
        self.conn.execute(
            "UPDATE pages SET active=0 WHERE document_id=? AND active=1",
            (document.document_id,),
        )
        if document.pages:
            placeholders = ",".join("?" * len(document.pages))
            self.conn.execute(
                f"UPDATE pages SET active=1 WHERE page_id IN ({placeholders})",
                tuple(page.page_id for page in document.pages),
            )
        self.conn.commit()
        return classification

    def _upsert_page(self, page: WikiPageRecord, now: str) -> None:
        row = self.conn.execute(
            "SELECT map_hash, body_hash FROM pages WHERE page_id=?", (page.page_id,)
        ).fetchone()
        if row is not None and row["map_hash"] == page.map_hash and row["body_hash"] == page.body_hash:
            return  # idempotent: nothing changed
        self.conn.execute(
            """
            INSERT INTO pages(page_id, document_id, wiki_rel_path, filename, title, chapter,
                summary, owner_ranges_json, reference_ranges_json, map_text, map_hash,
                body_hash, vector_state, active, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(page_id) DO UPDATE SET
                title=excluded.title, chapter=excluded.chapter, summary=excluded.summary,
                owner_ranges_json=excluded.owner_ranges_json,
                reference_ranges_json=excluded.reference_ranges_json,
                map_text=excluded.map_text, map_hash=excluded.map_hash,
                body_hash=excluded.body_hash, vector_state='pending',
                active=1, updated_at=excluded.updated_at
            """,
            (
                page.page_id,
                page.document_id,
                page.wiki_rel_path,
                page.filename,
                page.title,
                page.chapter,
                page.summary,
                json.dumps(page.owner_ranges),
                json.dumps(page.reference_ranges),
                page.map_text,
                page.map_hash,
                page.body_hash,
                "pending",
                1,
                now,
            ),
        )
        self.conn.execute("DELETE FROM map_entries WHERE page_id=?", (page.page_id,))
        for entry in page.map_entries:
            self.conn.execute(
                "INSERT OR REPLACE INTO map_entries(entry_id, page_id, source_start,"
                " source_end, title, kind, summary, parent, enumeration_family,"
                " flags_json, quality) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.entry_id,
                    page.page_id,
                    entry.source_start,
                    entry.source_end,
                    entry.title,
                    entry.kind,
                    entry.summary,
                    entry.parent,
                    entry.enumeration_family,
                    json.dumps(entry.flags, sort_keys=True),
                    entry.quality,
                ),
            )
        self.conn.execute("DELETE FROM pages_fts WHERE page_id=?", (page.page_id,))
        self.conn.execute(
            "INSERT INTO pages_fts(page_id, title, chapter, summary, map_text)"
            " VALUES(?,?,?,?,?)",
            (page.page_id, page.title, page.chapter, page.summary, page.map_text),
        )

    def rebuild_edges(self, links_by_page: dict[str, list[str]]) -> None:
        """Rebuild intra and reference edges from current active pages."""

        pages = {
            row["page_id"]: row
            for row in self.conn.execute("SELECT * FROM pages WHERE active=1")
        }
        by_path = {row["wiki_rel_path"]: pid for pid, row in pages.items()}
        self.conn.execute("DELETE FROM page_edges WHERE kind IN ('intra','reference')")
        for page_id, row in pages.items():
            path = PurePosixPath(row["wiki_rel_path"])
            for target in links_by_page.get(page_id, []):
                rel = PurePosixPath((path.parent / target).as_posix())
                while ".." in rel.parts:
                    parts = rel.parts
                    i = parts.index("..")
                    rel = PurePosixPath(*parts[: max(0, i - 1)], *parts[i + 2 :])
                rel = rel.as_posix()
                if rel in by_path:
                    other = by_path[rel]
                    if other != page_id:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO page_edges(source_page_id,"
                            " target_page_id, kind, pair_id, summary)"
                            " VALUES(?,?,?,?,?)",
                            (page_id, other, "intra", "", ""),
                        )
            for start, end in json.loads(row["reference_ranges_json"]):
                for other_id, other_row in pages.items():
                    if other_id == page_id or other_row["document_id"] != row["document_id"]:
                        continue
                    for owner_start, owner_end in json.loads(
                        other_row["owner_ranges_json"]
                    ):
                        if start <= owner_end and owner_start <= end:
                            self.conn.execute(
                                "INSERT OR IGNORE INTO page_edges(source_page_id,"
                                " target_page_id, kind, pair_id, summary)"
                                " VALUES(?,?,?,?,?)",
                                (page_id, other_id, "reference", "", ""),
                            )
                            break
        self.conn.commit()

    def active_documents(self, team: str, exclude_document: str = "") -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM documents WHERE active=1 AND team=? AND document_id!=?",
            (team, exclude_document),
        ).fetchall()

    def active_pages(self, document_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pages WHERE active=1 AND document_id=? ORDER BY filename",
            (document_id,),
        ).fetchall()

    def page(self, page_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM pages WHERE page_id=?", (page_id,)).fetchone()

    def entry_ids(self, page_id: str) -> set[str]:
        return {
            row["entry_id"]
            for row in self.conn.execute(
                "SELECT entry_id FROM map_entries WHERE page_id=?", (page_id,)
            )
        }

    def fts_search(self, query: str, limit: int = DIRECT_K) -> list[str]:
        tokens = re.findall(r"[A-Za-z0-9_\-.]{3,}|[぀-ヿ一-龯]{2,12}", query)[:12]
        if not tokens:
            return []
        match = " OR ".join('"{}"*'.format(token.replace('"', '""')) for token in tokens)
        try:
            rows = self.conn.execute(
                "SELECT page_id FROM pages_fts WHERE pages_fts MATCH ?"
                " ORDER BY rank LIMIT ?",
                (match, limit * 3),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [row[0] for row in rows]

    def comparison_get(self, target_map_hash: str, block_hash: str) -> MapLinkScanResult | None:
        row = self.conn.execute(
            "SELECT result_json FROM map_comparisons WHERE target_map_hash=?"
            " AND document_block_hash=? AND prompt_version=?",
            (target_map_hash, block_hash, LINKER_MAP_PROMPT_VERSION),
        ).fetchone()
        if row is None:
            return None
        try:
            return MapLinkScanResult.model_validate(json.loads(row["result_json"]))
        except (ValueError, ValidationError):
            return None

    def comparison_put(self, target_map_hash: str, block_hash: str, result: MapLinkScanResult) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO map_comparisons(target_map_hash, document_block_hash,"
            " prompt_version, result_json, created_at) VALUES(?,?,?,?,?)",
            (
                target_map_hash,
                block_hash,
                LINKER_MAP_PROMPT_VERSION,
                canonical_json(result.model_dump(mode="json")),
                _now(),
            ),
        )
        self.conn.commit()

    def research_get(self, target_body_hash: str, bundle_hash: str) -> DeepLinkResearchResult | None:
        row = self.conn.execute(
            "SELECT result_json FROM research_cache WHERE target_body_hash=?"
            " AND bundle_hash=? AND prompt_version=?",
            (target_body_hash, bundle_hash, LINKER_RESEARCH_PROMPT_VERSION),
        ).fetchone()
        if row is None:
            return None
        try:
            return DeepLinkResearchResult.model_validate(json.loads(row["result_json"]))
        except (ValueError, ValidationError):
            return None

    def research_put(self, target_body_hash: str, bundle_hash: str, result: DeepLinkResearchResult) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO research_cache(target_body_hash, bundle_hash,"
            " prompt_version, result_json, created_at) VALUES(?,?,?,?,?)",
            (
                target_body_hash,
                bundle_hash,
                LINKER_RESEARCH_PROMPT_VERSION,
                canonical_json(result.model_dump(mode="json")),
                _now(),
            ),
        )
        self.conn.commit()

    def judge_get(
        self, target_body_hash: str, endpoint_body_hash: str, proposal_hash: str
    ) -> LinkJudgeResult | None:
        row = self.conn.execute(
            "SELECT result_json FROM judge_cache WHERE target_body_hash=?"
            " AND endpoint_body_hash=? AND proposal_hash=? AND prompt_version=?",
            (
                target_body_hash,
                endpoint_body_hash,
                proposal_hash,
                LINKER_JUDGE_PROMPT_VERSION,
            ),
        ).fetchone()
        if row is None:
            return None
        try:
            return LinkJudgeResult.model_validate(json.loads(row["result_json"]))
        except (ValueError, ValidationError):
            return None

    def judge_put(
        self,
        target_body_hash: str,
        endpoint_body_hash: str,
        proposal_hash: str,
        result: LinkJudgeResult,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO judge_cache(target_body_hash,endpoint_body_hash,"
            " proposal_hash,prompt_version,result_json,created_at) VALUES(?,?,?,?,?,?)",
            (
                target_body_hash,
                endpoint_body_hash,
                proposal_hash,
                LINKER_JUDGE_PROMPT_VERSION,
                canonical_json(result.model_dump(mode="json")),
                _now(),
            ),
        )
        self.conn.commit()

    def page_checkpoint_get(
        self, page_id: str, input_hash: str
    ) -> list[AcceptedLink] | None:
        row = self.conn.execute(
            "SELECT result_json FROM page_link_checkpoints"
            " WHERE page_id=? AND input_hash=?",
            (page_id, input_hash),
        ).fetchone()
        if row is None:
            return None
        try:
            return [AcceptedLink(**item) for item in json.loads(row["result_json"])]
        except (TypeError, ValueError):
            return None

    def discovery_checkpoint_get(
        self, page_id: str, input_hash: str
    ) -> tuple[list[LinkCandidate], bool] | None:
        row = self.conn.execute(
            "SELECT result_json FROM page_discovery_checkpoints"
            " WHERE page_id=? AND input_hash=?",
            (page_id, input_hash),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["result_json"])
            candidates = [
                LinkCandidate(
                    **{
                        **item,
                        "sources": set(item.get("sources", [])),
                    }
                )
                for item in payload["candidates"]
            ]
            return candidates, bool(payload.get("overflow"))
        except (KeyError, TypeError, ValueError):
            return None

    def discovery_checkpoint_put(
        self,
        page_id: str,
        input_hash: str,
        candidates: Sequence[LinkCandidate],
        overflow: bool,
    ) -> None:
        payload = {
            "candidates": [
                {**candidate.__dict__, "sources": sorted(candidate.sources)}
                for candidate in candidates
            ],
            "overflow": overflow,
        }
        self.conn.execute(
            "INSERT OR REPLACE INTO page_discovery_checkpoints"
            "(page_id,input_hash,result_json,created_at) VALUES(?,?,?,?)",
            (page_id, input_hash, canonical_json(payload), _now()),
        )
        self.conn.commit()

    def page_checkpoint_put(
        self, page_id: str, input_hash: str, links: Sequence[AcceptedLink]
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO page_link_checkpoints"
            "(page_id,input_hash,result_json,created_at) VALUES(?,?,?,?)",
            (
                page_id,
                input_hash,
                canonical_json([dict(link.__dict__) for link in links]),
                _now(),
            ),
        )
        self.conn.commit()

    def edges_from(self, page_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM page_edges WHERE source_page_id=?", (page_id,)
        ).fetchall()

    def edge_exists(self, a: str, b: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM page_edges WHERE (source_page_id=? AND target_page_id=?)"
            " OR (source_page_id=? AND target_page_id=?)",
            (a, b, b, a),
        ).fetchone()
        return row is not None

    def links_for_page(self, page_id: str, statuses: Sequence[str] = ("active",)) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM links WHERE status IN ({marks})"
            " AND (page_a_id=? OR page_b_id=?)",
            (*statuses, page_id, page_id),
        ).fetchall()

    # -- relation commit (section 13) ----------------------------------------

    def begin_relation_commit(
        self,
        run_id: str,
        document_id: str,
        new_links: list[AcceptedLink],
        stale_pair_ids: list[str],
        affected: list[dict[str, str]],
    ) -> None:
        now = _now()
        with self.conn:  # one short transaction
            self.conn.execute(
                "INSERT OR REPLACE INTO link_runs(run_id, document_id, status,"
                " affected_pages_json, error, started_at, finished_at)"
                " VALUES(?,?,?,?,?,?,'')",
                (run_id, document_id, "committing", json.dumps(affected), "", now),
            )
            for link in new_links:
                self.conn.execute(
                    """
                    INSERT INTO links(pair_id, page_a_id, page_b_id, relation_type,
                        discovery_path_json, evidence_json, a_anchor_id, b_anchor_id,
                        a_bridge_template, b_bridge_template, a_footer_reason,
                        b_footer_reason, novelty_score, usefulness_score,
                        confidence_score, target_hashes_json, prompt_version, status,
                        run_id, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(pair_id) DO UPDATE SET
                        relation_type=excluded.relation_type,
                        discovery_path_json=excluded.discovery_path_json,
                        evidence_json=excluded.evidence_json,
                        a_anchor_id=excluded.a_anchor_id, b_anchor_id=excluded.b_anchor_id,
                        a_bridge_template=excluded.a_bridge_template,
                        b_bridge_template=excluded.b_bridge_template,
                        a_footer_reason=excluded.a_footer_reason,
                        b_footer_reason=excluded.b_footer_reason,
                        novelty_score=excluded.novelty_score,
                        usefulness_score=excluded.usefulness_score,
                        confidence_score=excluded.confidence_score,
                        target_hashes_json=excluded.target_hashes_json,
                        prompt_version=excluded.prompt_version,
                        status='pending', run_id=excluded.run_id,
                        updated_at=excluded.updated_at
                    """,
                    (
                        link.pair_id,
                        link.page_a_id,
                        link.page_b_id,
                        link.relation_type,
                        json.dumps(link.discovery_path),
                        json.dumps(link.evidence),
                        link.a_anchor_id,
                        link.b_anchor_id,
                        link.a_bridge_template,
                        link.b_bridge_template,
                        link.a_footer_reason,
                        link.b_footer_reason,
                        link.novelty_score,
                        link.usefulness_score,
                        link.confidence_score,
                        json.dumps(link.target_hashes),
                        LINKER_RESEARCH_PROMPT_VERSION,
                        "pending",
                        run_id,
                        now,
                    ),
                )
            for pair_id in stale_pair_ids:
                self.conn.execute(
                    "UPDATE links SET status='deleting', updated_at=? WHERE pair_id=?",
                    (now, pair_id),
                )

    def finish_relation_commit(self, run_id: str) -> None:
        now = _now()
        with self.conn:
            self.conn.execute("DELETE FROM links WHERE status='deleting'")
            self.conn.execute(
                "UPDATE links SET status='active', updated_at=? WHERE status='pending'",
                (now,),
            )
            self.conn.execute(
                "DELETE FROM page_edges WHERE kind='managed'"
            )
            for row in self.conn.execute("SELECT * FROM links WHERE status='active'"):
                for source, target, summary in (
                    (row["page_a_id"], row["page_b_id"], row["b_footer_reason"]),
                    (row["page_b_id"], row["page_a_id"], row["a_footer_reason"]),
                ):
                    self.conn.execute(
                        "INSERT OR IGNORE INTO page_edges(source_page_id, target_page_id,"
                        " kind, pair_id, summary) VALUES(?,?,?,?,?)",
                        (source, target, "managed", row["pair_id"], summary),
                    )
            self.conn.execute(
                "UPDATE link_runs SET status='complete', finished_at=? WHERE run_id=?",
                (now, run_id),
            )

    def fail_relation_commit(self, run_id: str, error: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM links WHERE status='pending'")
            self.conn.execute(
                "UPDATE links SET status='active' WHERE status='deleting'"
            )
            self.conn.execute(
                "UPDATE link_runs SET status='failed', error=?, finished_at=?"
                " WHERE run_id=?",
                (error[:500], _now(), run_id),
            )

    def committing_runs(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM link_runs WHERE status='committing' ORDER BY started_at"
        ).fetchall()

    def page_body_state(self, page_id: str) -> tuple[str, str] | None:
        row = self.conn.execute(
            "SELECT map_hash, body_hash FROM pages WHERE page_id=?", (page_id,)
        ).fetchone()
        return (row["map_hash"], row["body_hash"]) if row else None


# ---------------------------------------------------------------------------
# Rendering (section 12)
# ---------------------------------------------------------------------------


def _insertion_offset(base: str, anchor_id: str, anchors: list[dict[str, Any]]) -> int | None:
    """Character offset (end of an ordinary line) where a block may attach."""

    lines = base.split("\n")
    nonempty_last = max(
        (i for i, line in enumerate(lines) if line.strip()), default=-1
    )

    def end_of_line(index: int) -> int:
        return sum(len(lines[i]) + 1 for i in range(index)) + len(lines[index])

    def before_line(index: int) -> int:
        # end of the last non-empty line strictly before ``index``
        for i in range(index - 1, -1, -1):
            if lines[i].strip():
                return end_of_line(i)
        return end_of_line(max(0, min(nonempty_last, len(lines) - 1))) if nonempty_last >= 0 else len(base.rstrip("\n"))

    if anchor_id == "lead":
        for i, line in enumerate(lines):
            match = _HEADING_RE.match(line)
            if match and len(match.group(1)) <= 2 and i > 0:
                return before_line(i)
        return end_of_line(nonempty_last) if nonempty_last >= 0 else None

    anchor_line = next(
        (a["line"] - 1 for a in anchors if a["anchor_id"] == anchor_id), None
    )
    if anchor_line is None:
        return None
    index = anchor_line + 1
    in_fence = False
    while index < len(lines):
        line = lines[index]
        if _FENCE_RE.match(line.strip()):
            in_fence = True
        if in_fence:
            if _FENCE_RE.match(line.strip()):
                in_fence = False
            index += 1
            continue
        if not line.strip():
            index += 1
            continue
        if _ordinary(line):
            start = index
            while index + 1 < len(lines) and _ordinary(lines[index + 1]):
                index += 1
            return end_of_line(index)
        # non-ordinary content: jump to before next heading (or continue)
        if _HEADING_RE.match(line):
            return before_line(index)
        if start_block := _next_heading(lines, index):
            return before_line(start_block)
        break
    return end_of_line(nonempty_last) if nonempty_last >= 0 else None


def _next_heading(lines: list[str], start: int) -> int | None:
    in_fence = False
    for i in range(start, len(lines)):
        if _FENCE_RE.match(lines[i].strip()):
            in_fence = not in_fence
        if not in_fence and _HEADING_RE.match(lines[i]):
            return i
    return None


def render_inline_block(pair_id: str, text: str) -> str:
    return (
        "\n\n"
        + LINK_START.format(pair_id=pair_id)
        + "\n> 関連: "
        + text
        + "\n"
        + LINK_END.format(pair_id=pair_id)
    )


def render_footer(items: Sequence[tuple[str, str, str]]) -> str:
    body = "\n".join(f"- [{title}]({link}) — {reason}" for title, link, reason in items)
    return f"\n\n{RELATED_START}\n{RELATED_HEADING}\n\n{body}\n{RELATED_END}\n"


def render_managed_page(
    base: str,
    inline: Sequence[tuple[str, str, str]],  # (anchor_id, pair_id, rendered_text)
    footer_items: Sequence[tuple[str, str, str]],  # (peer title, relative link, reason)
    anchors: list[dict[str, Any]],
) -> str:
    """Rebuild one page from its stripped base plus desired relation state."""

    groups: dict[str, list[tuple[str, str]]] = {}
    for anchor_id, pair_id, text in inline:
        groups.setdefault(anchor_id, []).append((pair_id, text))
    insertions: list[tuple[int, str]] = []
    for anchor_id, items in groups.items():
        offset = _insertion_offset(base, anchor_id, anchors)
        if offset is None:
            raise LinkerError(f"anchor missing: {anchor_id}")
        # caller orders blocks (peer title, then pair ID); preserve that order
        insertions.append((offset, "".join(render_inline_block(pair, text) for pair, text in items)))
    for offset, text in sorted(insertions, key=lambda item: -item[0]):
        base = base[:offset] + text + base[offset:]
    if footer_items:
        ordered = sorted(footer_items, key=lambda item: (item[0], item[1]))
        base = base + render_footer(ordered)
    return base


def verify_bilateral_links(
    project: Project,
    relations: Sequence[sqlite3.Row],
    pages_by_id: dict[str, sqlite3.Row],
    base_texts: dict[str, str],
    desired: dict[str, tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]],
) -> None:
    """Abort activation unless both sides hold exactly one block and footer."""

    for row in relations:
        for side, other in (("a", "b"), ("b", "a")):
            page_id = row[f"page_{side}_id"]
            peer_id = row[f"page_{other}_id"]
            page = pages_by_id[page_id]
            peer = pages_by_id[peer_id]
            path = project.wiki / page["wiki_rel_path"]
            if not path.exists():
                raise LinkerError(f"endpoint file missing: {path}")
            text = path.read_text(encoding="utf-8")
            start = LINK_START.format(pair_id=row["pair_id"])
            if text.count(start) != 1:
                raise LinkerError(f"expected exactly one inline block {row['pair_id']} in {path}")
            link = relative_link(page["wiki_rel_path"], peer["wiki_rel_path"])
            footer = _RELATED_BLOCK_RE.search(text)
            if footer is None or f"]({link})" not in footer.group(0):
                raise LinkerError(f"footer peer link missing in {path}")
            if PurePosixPath(page["wiki_rel_path"]).parts[0] != PurePosixPath(
                peer["wiki_rel_path"]
            ).parts[0]:
                raise LinkerError("bilateral link crossed teams")
            template = row[f"{side}_bridge_template"]
            rendered = template.replace("{link}", f"[{peer['title']}]({link})")
            if f"> 関連: {rendered}" not in text:
                raise LinkerError(f"direction prose missing in {path}")
            stripped = strip_managed_links(text, path=str(path))
            if stripped != base_texts.get(page_id):
                raise LinkerError(f"outside-marker bytes changed: {path}")
            inline, footer_items = desired.get(page_id, ([], []))
            again = render_managed_page(
                stripped, inline, footer_items, extract_heading_anchors(stripped)
            )
            if again != text:
                raise LinkerError(f"rerender not byte-identical: {path}")



# ---------------------------------------------------------------------------
# Artifacts and bounded model calls (section 16)
# ---------------------------------------------------------------------------


class _Artifacts:
    """Per-run prompt/response artifacts under work/linker/<run-id>/."""

    def __init__(self, root: Path | None, progress: Progress = None) -> None:
        self.root = Path(root) if root else None
        self.progress = progress
        self.cache_hits: list[str] = []

    def _write(self, name: str, content: str) -> None:
        if self.root is None:
            return
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def prompt(self, name: str, rendered: str) -> None:
        self._write(name, rendered)

    def response(self, name: str, payload: Any) -> None:
        self._write(name, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def error(self, name: str, message: str) -> None:
        existing = ""
        if self.root is not None and (self.root / name).exists():
            existing = (self.root / name).read_text(encoding="utf-8")
        self._write(name, existing + message + "\n")


def _emit(progress: Progress, step: str, **details: Any) -> None:
    if progress:
        progress({"stage": "linker", "step": step, **details})


async def _call_structured(
    model: Any,
    schema: Any,
    build_prompt: Callable[[str | None], Any],
    validate: Callable[[Any], list[str]] | None,
    *,
    attempts: int = MODEL_ATTEMPTS,
    artifacts: _Artifacts | None = None,
    name: str = "call",
    semaphore: asyncio.Semaphore | None = None,
) -> Any:
    """Bounded structured call with exact-error correction retries.

    Failure raises :class:`LinkerIncomplete`; a failure is never recorded as
    an empty (valid) result.
    """

    last_error: str | None = None
    for attempt in range(attempts):
        prompt = build_prompt(last_error)
        started = time.monotonic()
        try:
            if semaphore is None:
                _emit(
                    artifacts.progress if artifacts else None,
                    "request_start",
                    request=name,
                    attempt=attempt + 1,
                    attempts=attempts,
                )
                result = await asyncio.wait_for(
                    model.structured(schema, prompt.messages()),
                    timeout=MODEL_CALL_TIMEOUT_SECONDS,
                )
            else:
                async with semaphore:
                    _emit(
                        artifacts.progress if artifacts else None,
                        "request_start",
                        request=name,
                        attempt=attempt + 1,
                        attempts=attempts,
                    )
                    result = await asyncio.wait_for(
                        model.structured(schema, prompt.messages()),
                        timeout=MODEL_CALL_TIMEOUT_SECONDS,
                    )
        except LinkerIncomplete:
            raise
        except Exception as exc:  # transport/schema failure: retry, never cache
            last_error = f"{type(exc).__name__}: {exc}"
            if artifacts:
                artifacts.error(f"{name}-error.txt", f"attempt {attempt + 1}: {last_error}")
            _emit(
                artifacts.progress if artifacts else None,
                "request_retry",
                request=name,
                attempt=attempt + 1,
                error=last_error[:200],
                seconds=round(time.monotonic() - started, 2),
            )
            continue
        errors = validate(result) if validate else []
        if errors:
            last_error = "\n".join(errors)
            if artifacts:
                artifacts.error(f"{name}-error.txt", f"attempt {attempt + 1}:\n{last_error}")
            _emit(
                artifacts.progress if artifacts else None,
                "request_retry",
                request=name,
                attempt=attempt + 1,
                error=last_error[:200],
                seconds=round(time.monotonic() - started, 2),
            )
            continue
        if artifacts:
            artifacts.prompt(f"{name}-prompt.md", prompt.render())
            artifacts.response(f"{name}-response.json", result.model_dump(mode="json"))
        _emit(
            artifacts.progress if artifacts else None,
            "request_done",
            request=name,
            attempt=attempt + 1,
            seconds=round(time.monotonic() - started, 2),
        )
        return result
    raise LinkerIncomplete(f"{name}: no valid model result after {attempts} attempts ({last_error})")


# ---------------------------------------------------------------------------
# Validation helpers (sections 9.1, 11.5)
# ---------------------------------------------------------------------------


def validate_map_scan(
    result: MapLinkScanResult,
    *,
    block_page_ids: set[str],
    entries_by_page: dict[str, set[str]],
    target_page_id: str,
) -> list[str]:
    errors: list[str] = []
    for index, candidate in enumerate(result.candidates):
        if candidate.candidate_page_id not in block_page_ids:
            errors.append(f"candidates[{index}]: unknown candidate_page_id")
        if candidate.candidate_page_id == target_page_id:
            errors.append(f"candidates[{index}]: candidate is the target itself")
        own = entries_by_page.get(candidate.candidate_page_id, set())
        for entry_id in candidate.candidate_map_entry_ids:
            if entry_id not in own:
                errors.append(f"candidates[{index}]: entry {entry_id} not owned by candidate")
        for entry_id in candidate.target_map_entry_ids:
            if entry_id not in entries_by_page.get(target_page_id, set()):
                errors.append(f"candidates[{index}]: entry {entry_id} not owned by target")
    return errors


def filter_valid_map_candidates(
    result: MapLinkScanResult,
    *,
    block_page_ids: set[str],
    entries_by_page: dict[str, set[str]],
    target_page_id: str,
) -> tuple[MapLinkScanResult, int]:
    """Discard malformed scout hints; final links still require full-page evidence."""

    valid = [
        candidate
        for candidate in result.candidates
        if not validate_map_scan(
            MapLinkScanResult(candidates=[candidate]),
            block_page_ids=block_page_ids,
            entries_by_page=entries_by_page,
            target_page_id=target_page_id,
        )
    ]
    return result.model_copy(update={"candidates": valid}), len(result.candidates) - len(valid)


def validate_bridge_probes(result: BridgeProbeResult) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for probe in result.probes:
        normalized = _norm_ws(probe)
        if len(normalized) < 20:
            errors.append(f"probe too short (need 20 chars): {probe!r}")
        if not ("?" in probe or "？" in probe):
            errors.append(f"probe is not a question: {probe!r}")
        if normalized in seen:
            errors.append(f"duplicate probe: {probe!r}")
        seen.add(normalized)
    if len(seen) < 3:
        errors.append("fewer than 3 unique probes remain")
    return errors


_TEMPLATE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}|\d[\d.]*[a-zA-Z]?")


def _closest_fragments(
    needle: str, haystack: str, count: int = 3, max_len: int = 160
) -> list[str]:
    """Verbatim fragments the model probably meant, for self-correcting errors."""

    import difflib

    wanted = _norm_ws(needle)
    candidates: list[str] = []
    for chunk in re.split(r"\n+|。", haystack):
        chunk = chunk.strip()
        if len(chunk) >= 8:
            candidates.append(chunk)
    scored = sorted(
        candidates,
        key=lambda chunk: difflib.SequenceMatcher(
            None, wanted, _norm_ws(chunk)
        ).ratio(),
        reverse=True,
    )
    return [chunk[:max_len] for chunk in scored[:count] if chunk]


def _repair_evidence(
    proposal: DeepLinkProposal, target_body: str, endpoint_body: str
) -> bool:
    """Replace a near-miss quote with the verbatim region it was reaching for.

    Near-identical text is repaired. A longer model quote that joins adjacent
    source fragments may also be reduced to one substantially copied verbatim
    fragment; a real content difference remains rejected.
    """

    import difflib

    changed = False
    for side, body in (
        ("target_evidence", target_body),
        ("endpoint_evidence", endpoint_body),
    ):
        needle = getattr(proposal, side)
        if _evidence_matches(needle, body):
            continue
        best, best_score = "", (False, 0.0, 0)
        normalized_needle = _norm_evidence(needle)
        for chunk in _closest_fragments(needle, body, 3, max_len=500):
            normalized_chunk = _norm_evidence(chunk)
            matcher = difflib.SequenceMatcher(None, normalized_needle, normalized_chunk)
            ratio = matcher.ratio()
            overlap = matcher.find_longest_match().size
            copied = len(normalized_chunk) >= 24 and overlap / len(normalized_chunk) >= 0.8
            score = (ratio >= 0.93 or copied, ratio, overlap)
            if score > best_score:
                best, best_score = chunk, score
        if best and best_score[0]:
            setattr(proposal, side, best)
            changed = True
    return changed


def _validate_template(template: str, *, side: str) -> list[str]:
    errors: list[str] = []
    if template.count("{link}") != 1:
        errors.append(f"{side}: template must contain {{link}} exactly once")
    if any(ch in template for ch in "\n\r"):
        errors.append(f"{side}: template must be one line")
    if re.search(r"(^|\s)(#{1,6}\s|[-*+]\s|\|>|<\w+|```)", template):
        errors.append(f"{side}: template contains heading/list/table/HTML/fence")
    sentences = [s for s in re.split(r"[。.|!?!?]", template) if s.strip()]
    if len(sentences) > 2:
        errors.append(f"{side}: template exceeds two sentences")
    if len(template) > 400:
        errors.append(f"{side}: template exceeds 400 characters")
    if ".md" in template or "://" in template or re.search(r"(?<!\w)/\w+/\w+", template):
        errors.append(f"{side}: template contains a path or URL")
    for pattern in _GENERIC_PATTERNS:
        if pattern in template:
            errors.append(f"{side}: template is a generic shared-topic phrase: {pattern}")
    return errors


def validate_proposal(
    proposal: DeepLinkProposal,
    *,
    target: sqlite3.Row,
    endpoint: sqlite3.Row | None,
    target_body: str,
    endpoint_body: str,
    allowed_endpoint_ids: set[str],
    seeds: set[str],
    edge_exists: Callable[[str, str], bool],
    entries_by_page: dict[str, set[str]],
    anchors_by_page: dict[str, set[str]],
) -> list[str]:
    """Every mechanical check of docs/LINKER.md section 11.5."""

    errors: list[str] = []
    if endpoint is None or proposal.endpoint_page_id not in allowed_endpoint_ids:
        errors.append("endpoint page was not fully supplied")
        return errors
    if endpoint["document_id"] == target["document_id"]:
        errors.append("endpoint belongs to the same document")
    if PurePosixPath(target["wiki_rel_path"]).parts[0] != PurePosixPath(
        endpoint["wiki_rel_path"]
    ).parts[0]:
        errors.append("endpoint is in another team")

    path = proposal.discovery_path
    if len(path) < 2 or len(path) > 4 or path[0] != target["page_id"] or path[-1] != endpoint["page_id"]:
        errors.append("discovery_path must start at target, end at endpoint, hold 2..4 IDs")
    else:
        for previous, current in zip(path, path[1:]):
            direct_seed = previous == target["page_id"] and current in seeds
            if not direct_seed and not edge_exists(previous, current):
                errors.append(f"discovery_path step {previous}->{current} is not a seed or catalog edge")
                break

    if any(pattern in proposal.relationship_explanation for pattern in _GENERIC_PATTERNS):
        errors.append("relationship_explanation uses a generic shared-topic pattern")
    if not _evidence_matches(proposal.target_evidence, target_body):
        hints = _closest_fragments(proposal.target_evidence, target_body)
        errors.append(
            f"target_evidence {proposal.target_evidence[:80]!r} is not an exact "
            "excerpt of the target page; copy one of these verbatim instead: "
            + " | ".join(f"{h!r}" for h in hints)
        )
    if not _evidence_matches(proposal.endpoint_evidence, endpoint_body):
        hints = _closest_fragments(proposal.endpoint_evidence, endpoint_body)
        errors.append(
            f"endpoint_evidence {proposal.endpoint_evidence[:80]!r} is not an exact "
            "excerpt of the endpoint page; copy one of these verbatim instead: "
            + " | ".join(f"{h!r}" for h in hints)
        )
    for entry_id in proposal.target_map_entry_ids:
        if entry_id not in entries_by_page.get(target["page_id"], set()):
            errors.append(f"target map entry {entry_id} unknown")
    for entry_id in proposal.endpoint_map_entry_ids:
        if entry_id not in entries_by_page.get(endpoint["page_id"], set()):
            errors.append(f"endpoint map entry {entry_id} unknown")
    if proposal.target_anchor_id not in anchors_by_page.get(target["page_id"], set()):
        errors.append(f"target anchor {proposal.target_anchor_id} not in allowlist")
    if proposal.endpoint_anchor_id not in anchors_by_page.get(endpoint["page_id"], set()):
        errors.append(f"endpoint anchor {proposal.endpoint_anchor_id} not in allowlist")

    errors.extend(_validate_template(proposal.target_bridge_template, side="target_bridge_template"))
    errors.extend(
        _validate_template(proposal.endpoint_bridge_template, side="endpoint_bridge_template")
    )

    corpus = _norm_ws(
        target_body + "\x00" + endpoint_body
        + proposal.target_evidence + proposal.endpoint_evidence
        + target["title"] + target["summary"] + endpoint["title"] + endpoint["summary"]
    )
    for template, side in (
        (proposal.target_bridge_template, "target_bridge_template"),
        (proposal.endpoint_bridge_template, "endpoint_bridge_template"),
    ):
        for token in _TEMPLATE_TOKEN_RE.findall(template.replace("{link}", " ")):
            if token.lower() == "link":
                continue
            if token not in corpus:
                errors.append(f"{side}: identifier {token!r} appears in neither endpoint")

    for reason, side in (
        (proposal.target_footer_reason, "target_footer_reason"),
        (proposal.endpoint_footer_reason, "endpoint_footer_reason"),
    ):
        if ".md" in reason or "://" in reason:
            errors.append(f"{side} contains a path or URL")
        if not 20 <= len(reason) <= 240:
            errors.append(f"{side} length out of bounds (20..240)")

    for name, value in (
        ("novelty_score", proposal.novelty_score),
        ("usefulness_score", proposal.usefulness_score),
        ("confidence_score", proposal.confidence_score),
    ):
        if value < MIN_ACCEPT_SCORE:
            errors.append(f"{name} {value} below minimum {MIN_ACCEPT_SCORE}")
    return errors


# ---------------------------------------------------------------------------
# Page views and bundles (section 11)
# ---------------------------------------------------------------------------


def build_page_view(
    page: sqlite3.Row, body: str, anchors: list[dict[str, Any]]
) -> str:
    anchor_lines = "\n".join(
        f"{a['anchor_id']} | {a['heading']} | {a['line']}" for a in anchors
    )
    return (
        f"PAGE ID: {page['page_id']}\n"
        f"TITLE: {page['title']}\n"
        f"DOCUMENT: {page['document_id']}\n"
        f"WIKI PATH: {page['wiki_rel_path']}\n"
        f"ANCHORS:\n{anchor_lines}\n"
        "--- 本文(管理リンク除去済み) ---\n"
        + scrub_base64(body)
        + "\n--- MAP ---\n"
        + page["map_text"]
    )


def build_judge_view(
    page: sqlite3.Row,
    body: str,
    anchors: list[dict[str, Any]],
    evidence: str,
) -> str:
    """Compact final-check view; deep research already read the full page."""

    index = body.find(evidence)
    if index >= 0:
        start = max(0, index - 800)
        end = min(len(body), index + len(evidence) + 800)
        context = body[start:end]
    else:
        matches = _closest_fragments(evidence, body, count=1, max_len=1800)
        context = matches[0] if matches else evidence
    anchor_lines = "\n".join(
        f"{anchor['anchor_id']} | {anchor['heading']} | {anchor['line']}"
        for anchor in anchors
    )
    return (
        f"PAGE ID: {page['page_id']}\n"
        f"TITLE: {page['title']}\n"
        f"SUMMARY: {page['summary']}\n"
        f"ANCHORS:\n{anchor_lines}\n"
        f"VERIFIED EVIDENCE:\n{evidence}\n"
        "--- EVIDENCE CONTEXT ---\n"
        + scrub_base64(context)
    )


def _split_sections(body: str) -> list[tuple[str, str]]:
    """Split only at Markdown heading boundaries; returns (heading, text)."""

    sections: list[tuple[str, str]] = []
    current_heading = "(lead)"
    current: list[str] = []
    in_fence = False
    for line in body.split("\n"):
        if _FENCE_RE.match(line.strip()):
            in_fence = not in_fence
        if not in_fence and _HEADING_RE.match(line):
            if current:
                sections.append((current_heading, "\n".join(current)))
            current_heading = line
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append((current_heading, "\n".join(current)))
    return sections


# ---------------------------------------------------------------------------
# Candidate discovery (section 9)
# ---------------------------------------------------------------------------


async def discover_candidates(
    catalog: LinkCatalog,
    model: Any,
    target: WikiPageRecord,
    target_row: sqlite3.Row,
    old_documents: list[WikiDocumentRecord],
    *,
    embedder: Any,
    reranker: Any,
    output_language: str,
    concurrency: int,
    artifacts: _Artifacts,
    progress: Progress,
    stop_check: StopCheck,
    target_number: int = 0,
) -> tuple[list[LinkCandidate], bool]:
    checkpoint_hash = page_link_input_hash(
        catalog, target, old_documents, output_language
    )
    checkpoint = catalog.discovery_checkpoint_get(target.page_id, checkpoint_hash)
    if checkpoint is not None:
        cached_candidates, cached_overflow = checkpoint
        artifacts.cache_hits.append(f"discovery {target.filename}")
        _emit(
            progress,
            "discovery_resumed",
            page=target.filename,
            candidates=len(cached_candidates),
        )
        return cached_candidates, cached_overflow

    sem = asyncio.Semaphore(max(1, concurrency))
    candidates: dict[str, LinkCandidate] = {}

    def add(
        candidate_id: str,
        source: str,
        path: list[str],
        *,
        relation_type: str = "",
        hypothesis: str = "",
        reader_value: str = "",
        target_entries: list[str] | None = None,
        candidate_entries: list[str] | None = None,
        questions: list[str] | None = None,
        priority: int = 0,
    ) -> None:
        existing = candidates.get(candidate_id)
        if existing is None:
            candidates[candidate_id] = LinkCandidate(
                target_page_id=target.page_id,
                candidate_page_id=candidate_id,
                sources={source},
                discovery_path=path,
                relation_type=relation_type,
                hypothesis=hypothesis,
                reader_value=reader_value,
                target_map_entry_ids=target_entries or [],
                candidate_map_entry_ids=candidate_entries or [],
                bridge_questions=questions or [],
                priority=priority,
            )
            return
        existing.sources.add(source)
        if priority > existing.priority:
            existing.priority = priority
        if not existing.relation_type and relation_type:
            existing.relation_type = relation_type
            existing.hypothesis = hypothesis
            existing.reader_value = reader_value
            existing.target_map_entry_ids = target_entries or []
            existing.candidate_map_entry_ids = candidate_entries or []
        if len(path) < len(existing.discovery_path):
            existing.discovery_path = path
        if questions:
            merged = list(dict.fromkeys(existing.bridge_questions + questions))
            existing.bridge_questions = merged[:4]

    def eligible(page_id: str) -> bool:
        row = catalog.page(page_id)
        return (
            row is not None
            and bool(row["active"])
            and row["document_id"] != target.document_id
            and PurePosixPath(row["wiki_rel_path"]).parts[0] == target.team
        )

    # -- Lane A: exhaustive map scout ---------------------------------------
    scout_calls: list[tuple[WikiDocumentRecord, list[WikiPageRecord], str]] = []
    for document in old_documents:
        if document.team != target.team or document.document_id == target.document_id:
            continue
        for block in make_document_blocks(document.pages):
            block_hash = sha256_text(
                canonical_json(sorted(p.map_hash for p in block))
            )
            scout_calls.append((document, block, block_hash))

    cache_hits = 0
    pending_calls = []
    for document, block, block_hash in scout_calls:
        cached = catalog.comparison_get(target.map_hash, block_hash)
        if cached is not None:
            cache_hits += 1
            artifacts.cache_hits.append(f"map_scout {target.filename} {block_hash}")
            _apply_scan(candidates, add, cached, target, block)
        else:
            pending_calls.append((document, block, block_hash))

    async def one_scout(document: WikiDocumentRecord, block: list[WikiPageRecord], block_hash: str, index: int) -> MapLinkScanResult:
        if stop_check and stop_check():
            raise LinkerCancelled("cancelled during map scout")
        async with sem:
            block_page_ids = {p.page_id for p in block}
            entries_by_page = {p.page_id: {e.entry_id for e in p.map_entries} for p in block}
            entries_by_page[target.page_id] = {e.entry_id for e in target.map_entries}
            cards = "\n\n".join(p.map_text for p in block)
            document_label = document.wiki_rel

            def build(last_error: str | None):
                return map_link_scan_prompt(
                    target_card=target.map_text,
                    block_cards=cards,
                    document_label=document_label,
                    output_language=output_language,
                    last_error=last_error,
                )

            result = await _call_structured(
                model,
                MapLinkScanResult,
                build,
                None,
                artifacts=artifacts,
                name=f"target-{target_number}/map-scout-{block_hash[:12]}",
            )
            result, dropped = filter_valid_map_candidates(
                result,
                block_page_ids=block_page_ids,
                entries_by_page=entries_by_page,
                target_page_id=target.page_id,
            )
            if dropped:
                _emit(
                    progress,
                    "map_scout",
                    page=target.filename,
                    degraded=f"discarded {dropped} invalid candidate(s)",
                )
            else:
                catalog.comparison_put(target.map_hash, block_hash, result)
            return result

    if pending_calls:
        _emit(
            progress,
            "map_scout",
            page=target.filename,
            blocks=len(scout_calls),
            cached=cache_hits,
            pending=len(pending_calls),
        )
        results = await asyncio.gather(
            *(
                one_scout(document, block, block_hash, number)
                for number, (document, block, block_hash) in enumerate(pending_calls, start=1)
            )
        )
        for (document, block, _hash), result in zip(pending_calls, results):
            _apply_scan(candidates, add, result, target, block)
    else:
        _emit(progress, "map_scout", page=target.filename, blocks=len(scout_calls), cached=cache_hits, pending=0)

    # -- Lane B: hybrid accelerator ------------------------------------------
    fts_ids = [pid for pid in catalog.fts_search(target.map_text, DIRECT_K) if eligible(pid)]
    dense_ids: list[str] = []
    if embedder is not None and catalog.has_vec_table():
        try:
            query_vector = embedder.embed_query(target.map_text)
            dense_ids = [
                pid
                for pid in catalog.vector_search(query_vector, DIRECT_K)
                if eligible(pid)
            ]
        except Exception as exc:
            _emit(progress, "retrieve", page=target.filename, degraded=f"embed: {type(exc).__name__}")
    fused = rrf_fuse([fts_ids, dense_ids])[:DIRECT_K]
    _emit(progress, "retrieve", page=target.filename, fts=len(fts_ids), dense=len(dense_ids), fused=len(fused))
    for pid in fused:
        add(pid, "hybrid", [target.page_id, pid])

    # -- Lane C: bridge probes ------------------------------------------------
    def build_probe(last_error: str | None):
        return bridge_probe_prompt(
            target_card=target.map_text,
            output_language=output_language,
            last_error=last_error,
        )

    try:
        probe_result = await _call_structured(
            model,
            BridgeProbeResult,
            build_probe,
            validate_bridge_probes,
            artifacts=artifacts,
            name=f"target-{target_number}/bridge",
        )
        probes = probe_result.probes
    except LinkerIncomplete as exc:
        # Bridge probes are an accelerator. The exhaustive map scout above is
        # the required discovery path, so a probe-only failure may degrade.
        probes = []
        _emit(
            progress,
            "bridge_probe",
            page=target.filename,
            degraded=f"{type(exc).__name__}: {exc}",
        )
    _emit(progress, "bridge_probe", page=target.filename, probes=len(probes))
    for probe in probes:
        hit_ids: list[str] = []
        hit_ids += [pid for pid in catalog.fts_search(probe, 8) if eligible(pid)]
        if embedder is not None and catalog.has_vec_table():
            try:
                hit_ids += [
                    pid
                    for pid in catalog.vector_search(embedder.embed_query(probe), 8)
                    if eligible(pid)
                ]
            except Exception:
                pass
        for pid in dict.fromkeys(hit_ids):
            add(
                pid,
                "bridge_probe",
                [target.page_id, pid],
                questions=[probe],
                priority=30,
            )

    # -- Lane D: optional reranker -------------------------------------------
    map_candidate_ids = {
        pid for pid, cand in candidates.items() if "map_scout" in cand.sources
    }
    rerank_ids = [
        pid for pid, cand in candidates.items() if pid not in map_candidate_ids
    ]
    if reranker is not None and rerank_ids:
        try:
            items = []
            for pid in rerank_ids:
                row = catalog.page(pid)
                if row is not None:
                    items.append((row["map_text"], pid))
            ranked = reranker.top_k(target.map_text + "\n" + "\n".join(probes), items, RERANK_K)
            kept = {pid for pid, _score in ranked}
            for pid in rerank_ids:
                if pid not in kept:
                    candidates.pop(pid, None)
            _emit(progress, "retrieve", page=target.filename, reranked=len(kept))
        except Exception as exc:
            # failure keeps the stable pre-rerank (RRF) order; nothing is dropped
            _emit(progress, "retrieve", page=target.filename, degraded=f"rerank: {type(exc).__name__}")
    # map-scout candidates can never be removed by retrieval or rerank score

    # -- Lane E: one/two-hop expansion ----------------------------------------
    seeds = list(candidates)
    hop_candidates = _expand_hops(catalog, target, seeds)
    for pid, path in hop_candidates:
        add(pid, "hop", path, priority=40)
    _emit(progress, "hops", page=target.filename, added=len(hop_candidates))

    ordered = sorted(candidates.values(), key=lambda c: (-c.priority, c.candidate_page_id))
    overflow = False
    if len(ordered) > MAX_RESEARCH_CANDIDATES:
        survivors = _schedule(ordered)[:MAX_RESEARCH_CANDIDATES]
        if sum(1 for c in ordered if "map_scout" in c.sources) > MAX_RESEARCH_CANDIDATES:
            overflow = True
        ordered = survivors
    catalog.discovery_checkpoint_put(
        target.page_id, checkpoint_hash, ordered, overflow
    )
    return ordered, overflow


def _apply_scan(
    candidates: dict[str, LinkCandidate],
    add: Callable[..., None],
    result: MapLinkScanResult,
    target: WikiPageRecord,
    block: list[WikiPageRecord],
) -> None:
    for candidate in result.candidates:
        add(
            candidate.candidate_page_id,
            "map_scout",
            [target.page_id, candidate.candidate_page_id],
            relation_type=candidate.relation_type,
            hypothesis=candidate.hypothesis,
            reader_value=candidate.reader_value,
            target_entries=list(candidate.target_map_entry_ids),
            candidate_entries=list(candidate.candidate_map_entry_ids),
            questions=list(candidate.bridge_questions),
            priority=candidate.priority,
        )


_HOP_KIND_ORDER = {"managed": 0, "reference": 1, "intra": 2}


def _expand_hops(
    catalog: LinkCatalog, target: WikiPageRecord, seeds: Sequence[str]
) -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []
    seen = {target.page_id} | set(seeds)
    hop2_used = 0
    for seed in seeds:
        row = catalog.page(seed)
        if row is None or not row["active"]:
            continue
        edges = sorted(
            catalog.edges_from(seed), key=lambda e: (_HOP_KIND_ORDER.get(e["kind"], 9), e["target_page_id"])
        )
        hop1 = []
        for edge in edges:
            other = edge["target_page_id"]
            if other == seed or other in seen:
                continue
            other_row = catalog.page(other)
            if (
                other_row is None
                or not other_row["active"]
                or other_row["document_id"] == target.document_id
                or PurePosixPath(other_row["wiki_rel_path"]).parts[0] != target.team
            ):
                continue
            hop1.append((other, edge["kind"]))
            if len(hop1) >= MAX_HOP1:
                break
        for other, _kind in hop1:
            seen.add(other)
            found.append((other, [target.page_id, other]))
        for other, _kind in hop1:
            for edge in catalog.edges_from(other):
                if hop2_used >= MAX_HOP2:
                    break
                far = edge["target_page_id"]
                if far in seen or far == target.page_id:
                    continue
                far_row = catalog.page(far)
                if (
                    far_row is None
                    or not far_row["active"]
                    or far_row["document_id"] == target.document_id
                    or PurePosixPath(far_row["wiki_rel_path"]).parts[0] != target.team
                ):
                    continue
                seen.add(far)
                hop2_used += 1
                found.append((far, [target.page_id, other, far]))
    return found


_LANE_ORDER = {"map_scout": 0, "hop": 1, "bridge_probe": 2, "hybrid": 3, "rerank_dropped": 4}


def _schedule(candidates: list[LinkCandidate]) -> list[LinkCandidate]:
    def rank(candidate: LinkCandidate) -> tuple[int, int, str]:
        lane = min((_LANE_ORDER.get(source, 9) for source in candidate.sources), default=9)
        return (lane, -candidate.priority, candidate.candidate_page_id)

    return sorted(candidates, key=rank)


def page_link_input_hash(
    catalog: LinkCatalog,
    target: WikiPageRecord,
    old_documents: Sequence[WikiDocumentRecord],
    output_language: str,
) -> str:
    """Fingerprint everything that can change a page's final link proposals."""

    old_pages = {
        page.page_id: (page.map_hash, page.body_hash)
        for document in old_documents
        for page in document.pages
    }
    edges = sorted(
        (
            row["source_page_id"],
            row["target_page_id"],
            row["kind"],
            row["pair_id"],
        )
        for row in catalog.conn.execute(
            "SELECT source_page_id,target_page_id,kind,pair_id FROM page_edges"
        )
        if row["source_page_id"] in old_pages
    )
    return sha256_text(
        canonical_json(
            {
                "version": 1,
                "target": [target.page_id, target.map_hash, target.body_hash],
                "old_pages": sorted((page_id, *hashes) for page_id, hashes in old_pages.items()),
                "edges": edges,
                "prompts": [
                    LINKER_MAP_PROMPT_VERSION,
                    LINKER_BRIDGE_PROMPT_VERSION,
                    LINKER_RESEARCH_PROMPT_VERSION,
                    LINKER_JUDGE_PROMPT_VERSION,
                ],
                "output_language": output_language,
                "limits": [
                    DIRECT_K,
                    RERANK_K,
                    MAX_RESEARCH_CANDIDATES,
                    MAX_HOPS,
                    MAX_NEW_LINKS_PER_TARGET,
                    MAX_ACTIVE_LINKS_PER_PAGE,
                    MIN_ACCEPT_SCORE,
                ],
            }
        )
    )


# ---------------------------------------------------------------------------
# Deep research (section 11)
# ---------------------------------------------------------------------------


async def research_target_page(
    catalog: LinkCatalog,
    model: Any,
    project: Project,
    target: WikiPageRecord,
    target_row: sqlite3.Row,
    candidates: list[LinkCandidate],
    *,
    output_language: str,
    concurrency: int,
    artifacts: _Artifacts,
    progress: Progress,
    stop_check: StopCheck,
    target_number: int = 0,
    request_sem: asyncio.Semaphore | None = None,
) -> list[AcceptedLink]:
    request_sem = request_sem or asyncio.Semaphore(max(1, concurrency))

    async def one(candidate: LinkCandidate, index: int) -> list[AcceptedLink]:
        if stop_check and stop_check():
            raise LinkerCancelled("cancelled during research")
        try:
            return await _research_candidate(
                catalog,
                model,
                project,
                target,
                target_row,
                candidate,
                output_language=output_language,
                artifacts=artifacts,
                index=index,
                target_number=target_number,
                request_sem=request_sem,
            )
        except LinkerIncomplete as exc:
            _emit(
                progress,
                "research_skipped",
                page=target.filename,
                candidate=candidate.candidate_page_id,
                current=index,
                total=len(candidates),
                error=str(exc)[-300:],
            )
            return []

    results = await asyncio.gather(
        *(one(candidate, index) for index, candidate in enumerate(candidates, start=1))
    )
    accepted: list[AcceptedLink] = []
    for links in results:
        accepted.extend(links)
    return accepted


def _load_page_body(project: Project, row: sqlite3.Row) -> str:
    path = project.wiki / row["wiki_rel_path"]
    return strip_managed_links(path.read_text(encoding="utf-8"), path=str(path))


async def _research_candidate(
    catalog: LinkCatalog,
    model: Any,
    project: Project,
    target: WikiPageRecord,
    target_row: sqlite3.Row,
    candidate: LinkCandidate,
    *,
    output_language: str,
    artifacts: _Artifacts,
    index: int,
    target_number: int = 0,
    request_sem: asyncio.Semaphore | None = None,
) -> list[AcceptedLink]:
    endpoint_row = catalog.page(candidate.candidate_page_id)
    if endpoint_row is None or not endpoint_row["active"]:
        return []
    target_body = _load_page_body(project, target_row)
    endpoint_body = _load_page_body(project, endpoint_row)

    target_anchors = extract_heading_anchors(target_body)
    endpoint_anchors = extract_heading_anchors(endpoint_body)
    target_view = build_page_view(target_row, target_body, target_anchors)
    endpoint_view = build_page_view(endpoint_row, endpoint_body, endpoint_anchors)

    discovery_notes = " / ".join(candidate.discovery_path) + (
        f"\n出典: {','.join(sorted(candidate.sources))}"
        + (f"\n仮説: {candidate.hypothesis}" if candidate.hypothesis else "")
    )

    oversized = len(target_view) + len(endpoint_view) + 2000 > READ_BUNDLE_CHARS

    if oversized:
        candidate_notes, nominated_sections = await _oversized_notes(
            catalog, model, project, target_row, target_view, endpoint_row,
            endpoint_body, output_language, artifacts, index, request_sem,
        )
        endpoint_input = (
            f"PAGE ID: {endpoint_row['page_id']}\nTITLE: {endpoint_row['title']}\n"
            f"DOCUMENT: {endpoint_row['document_id']}\n"
            f"WIKI PATH: {endpoint_row['wiki_rel_path']}\n"
            "ANCHORS:\n"
            + "\n".join(f"{a['anchor_id']} | {a['heading']} | {a['line']}" for a in endpoint_anchors)
            + "\n--- 注記( oversized procedure ) ---\n"
            + candidate_notes
            + "\n--- 提名セクション全文 ---\n"
            + nominated_sections
            + "\n--- MAP ---\n"
            + endpoint_row["map_text"]
        )
    else:
        endpoint_input = endpoint_view

    # Hop pages are discovery clues. Each discovered endpoint is researched in
    # its own task, so repeating intermediate full bodies only bloats prefill.
    allowed_endpoint_ids = {endpoint_row["page_id"]}
    entries_by_page = {
        target_row["page_id"]: catalog.entry_ids(target_row["page_id"]),
        endpoint_row["page_id"]: catalog.entry_ids(endpoint_row["page_id"]),
    }
    anchors_by_page = {
        target_row["page_id"]: {a["anchor_id"] for a in target_anchors},
        endpoint_row["page_id"]: {a["anchor_id"] for a in endpoint_anchors},
    }
    seeds = {candidate.candidate_page_id} | set(candidate.discovery_path)

    def validate_research(result: DeepLinkResearchResult) -> list[str]:
        errors: list[str] = []
        for number, proposal in enumerate(result.proposals):
            _repair_evidence(proposal, target_body, endpoint_body)
            for message in validate_proposal(
                proposal,
                target=target_row,
                endpoint=catalog.page(proposal.endpoint_page_id),
                target_body=target_body,
                endpoint_body=endpoint_body,
                allowed_endpoint_ids=allowed_endpoint_ids,
                seeds=seeds,
                edge_exists=catalog.edge_exists,
                entries_by_page=entries_by_page,
                anchors_by_page=anchors_by_page,
            ):
                errors.append(f"proposals[{number}]: {message}")
        return errors

    bundle_hash = sha256_text(
        canonical_json(
            {
                "target": target_row["body_hash"],
                "endpoint": endpoint_row["body_hash"],
                "candidate": {
                    "sources": sorted(candidate.sources),
                    "path": candidate.discovery_path,
                    "type": candidate.relation_type,
                    "questions": candidate.bridge_questions,
                },
                "views": [
                    sha256_text(target_view),
                    sha256_text(endpoint_input),
                ],
            }
        )
    )
    cached = catalog.research_get(target_row["body_hash"], bundle_hash)
    request_name = f"target-{target_number}/research-{candidate.candidate_page_id}"
    if cached is not None:
        artifacts.cache_hits.append(f"research {target.filename} {candidate.candidate_page_id}")
        _emit(artifacts.progress, "request_cached", request=request_name)
        research = cached
    else:
        candidate_views = endpoint_input
        anchors_map = (
            f"target: {','.join(a['anchor_id'] for a in target_anchors)}; "
            f"endpoint: {','.join(a['anchor_id'] for a in endpoint_anchors)}"
        )

        def build(last_error: str | None):
            return deep_link_research_prompt(
                target_view=target_view,
                candidate_views=candidate_views,
                discovery_notes=discovery_notes,
                anchor_allowlist=anchors_map,
                output_language=output_language,
                last_error=last_error,
            )

        research = await _call_structured(
            model,
            DeepLinkResearchResult,
            build,
            validate_research,
            attempts=CORRECTION_ATTEMPTS + 1,
            artifacts=artifacts,
            name=request_name,
            semaphore=request_sem,
        )
        catalog.research_put(target_row["body_hash"], bundle_hash, research)

    accepted: list[AcceptedLink] = []
    for proposal in research.proposals:
        proposal_errors = validate_proposal(
            proposal,
            target=target_row,
            endpoint=catalog.page(proposal.endpoint_page_id),
            target_body=target_body,
            endpoint_body=endpoint_body,
            allowed_endpoint_ids=allowed_endpoint_ids,
            seeds=seeds,
            edge_exists=catalog.edge_exists,
            entries_by_page=entries_by_page,
            anchors_by_page=anchors_by_page,
        )
        if proposal_errors:
            continue  # cached rows may predate a stricter validator
        judge = await _judge_proposal(
            catalog,
            model,
            target_row,
            target_body,
            target_anchors,
            catalog.page(proposal.endpoint_page_id),
            endpoint_body,
            endpoint_anchors,
            proposal,
            output_language,
            anchors_by_page,
            artifacts,
            target_number,
            request_sem,
        )
        if judge is None:
            continue
        final = proposal.model_copy(
            update={
                k.removeprefix("corrected_"): v
                for k, v in {
                    "corrected_target_anchor_id": judge.corrected_target_anchor_id,
                    "corrected_endpoint_anchor_id": judge.corrected_endpoint_anchor_id,
                    "corrected_target_bridge_template": judge.corrected_target_bridge_template,
                    "corrected_endpoint_bridge_template": judge.corrected_endpoint_bridge_template,
                }.items()
                if v
            }
        )
        if validate_proposal(
            final,
            target=target_row,
            endpoint=catalog.page(final.endpoint_page_id),
            target_body=target_body,
            endpoint_body=endpoint_body,
            allowed_endpoint_ids=allowed_endpoint_ids,
            seeds=seeds,
            edge_exists=catalog.edge_exists,
            entries_by_page=entries_by_page,
            anchors_by_page=anchors_by_page,
        ):
            continue
        accepted.append(_make_accepted(final, target_row, catalog.page(final.endpoint_page_id)))
    return accepted


async def _oversized_notes(
    catalog: LinkCatalog,
    model: Any,
    project: Project,
    target_row: sqlite3.Row,
    target_view: str,
    endpoint_row: sqlite3.Row,
    endpoint_body: str,
    output_language: str,
    artifacts: _Artifacts,
    index: int,
    request_sem: asyncio.Semaphore | None = None,
) -> tuple[str, str]:
    """Section-by-section evidence notes for an oversized endpoint (11.3)."""

    sections = _split_sections(endpoint_body)
    notes: list[str] = []
    relevant_ids: set[int] = set()
    for number, (heading, text) in enumerate(sections):
        section_id = f"s{number:02d}"

        def build(last_error: str | None, section_id=section_id, text=text):
            return oversized_section_note_prompt(
                target_view=target_view,
                section_id=section_id,
                section_text=text,
                output_language=output_language,
            )

        note = await _call_structured(
            model,
            OversizedSectionNote,
            build,
            None,
            artifacts=artifacts,
            name=f"target-{index}/section-{section_id}",
            semaphore=request_sem,
        )
        note = note.model_copy(update={"section_id": section_id})
        notes.append(canonical_json(note.model_dump(mode="json")))
        if note.relevant:
            relevant_ids.add(number)
    nominated: set[int] = set()
    for number in sorted(relevant_ids):
        nominated.update({max(0, number - 1), number, min(len(sections) - 1, number + 1)})
    full = "\n\n".join(
        f"[{f's{n:02d}'}]\n{sections[n][1]}" for n in sorted(nominated)
    )
    return "\n".join(notes), full


async def _judge_proposal(
    catalog: LinkCatalog,
    model: Any,
    target_row: sqlite3.Row,
    target_body: str,
    target_anchors: list[dict[str, Any]],
    endpoint_row: sqlite3.Row,
    endpoint_body: str,
    endpoint_anchors: list[dict[str, Any]],
    proposal: DeepLinkProposal,
    output_language: str,
    anchors_by_page: dict[str, set[str]],
    artifacts: _Artifacts,
    target_number: int = 0,
    request_sem: asyncio.Semaphore | None = None,
) -> LinkJudgeResult | None:
    proposal_hash = sha256_text(canonical_json(proposal.model_dump(mode="json")))
    request_name = (
        f"target-{target_number}/judge-"
        + make_pair_id(target_row["page_id"], endpoint_row["page_id"])
    )
    judge = catalog.judge_get(
        target_row["body_hash"], endpoint_row["body_hash"], proposal_hash
    )
    if judge is not None:
        artifacts.cache_hits.append(f"judge {request_name}")
        _emit(artifacts.progress, "request_cached", request=request_name)

    anchors_map = (
        f"target: {','.join(sorted(anchors_by_page.get(target_row['page_id'], ())))}; "
        f"endpoint: {','.join(sorted(anchors_by_page.get(endpoint_row['page_id'], ())))}"
    )
    evidence = (
        f"target: {proposal.target_evidence}\nendpoint: {proposal.endpoint_evidence}"
    )
    target_view = build_judge_view(
        target_row, target_body, target_anchors, proposal.target_evidence
    )
    endpoint_view = build_judge_view(
        endpoint_row, endpoint_body, endpoint_anchors, proposal.endpoint_evidence
    )

    def build(_last_error: str | None):
        return link_pair_judge_prompt(
            target_view=target_view,
            endpoint_view=endpoint_view,
            proposal_json=json.dumps(
                proposal.model_dump(mode="json"), ensure_ascii=False, indent=2
            ),
            evidence=evidence,
            anchor_allowlist=anchors_map,
            output_language=output_language,
        )

    if judge is None:
        judge = await _call_structured(
            model,
            LinkJudgeResult,
            build,
            None,
            artifacts=artifacts,
            name=request_name,
            semaphore=request_sem,
        )
        catalog.judge_put(
            target_row["body_hash"], endpoint_row["body_hash"], proposal_hash, judge
        )
    if not all(
        (
            judge.is_grounded_in_both_pages,
            judge.is_specific_relationship,
            judge.is_more_than_shared_topic,
            judge.is_useful_to_target_reader,
            judge.is_useful_to_endpoint_reader,
            judge.bridge_text_adds_no_unsupported_claim,
            judge.recommended,
        )
    ):
        return None
    return judge


def _make_accepted(
    proposal: DeepLinkProposal, target_row: sqlite3.Row, endpoint_row: sqlite3.Row
) -> AcceptedLink:
    return AcceptedLink(
        pair_id=make_pair_id(target_row["page_id"], endpoint_row["page_id"]),
        page_a_id=target_row["page_id"],
        page_b_id=endpoint_row["page_id"],
        relation_type=proposal.relation_type,
        discovery_path=list(proposal.discovery_path),
        evidence={
            "target_evidence": proposal.target_evidence,
            "endpoint_evidence": proposal.endpoint_evidence,
            "relationship_explanation": proposal.relationship_explanation,
            "why_reader_needs_link": proposal.why_reader_needs_link,
            "why_not_shared_topic_only": proposal.why_not_shared_topic_only,
            "target_map_entry_ids": proposal.target_map_entry_ids,
            "endpoint_map_entry_ids": proposal.endpoint_map_entry_ids,
        },
        a_anchor_id=proposal.target_anchor_id,
        b_anchor_id=proposal.endpoint_anchor_id,
        a_bridge_template=proposal.target_bridge_template,
        b_bridge_template=proposal.endpoint_bridge_template,
        a_footer_reason=proposal.target_footer_reason,
        b_footer_reason=proposal.endpoint_footer_reason,
        novelty_score=proposal.novelty_score,
        usefulness_score=proposal.usefulness_score,
        confidence_score=proposal.confidence_score,
        target_hashes={
            "target_map_hash": target_row["map_hash"],
            "target_body_hash": target_row["body_hash"],
            "endpoint_body_hash": endpoint_row["body_hash"],
        },
    )


def select_final_links(
    catalog: LinkCatalog, target_page_id: str, accepted: list[AcceptedLink]
) -> tuple[list[AcceptedLink], list[str]]:
    def sort_key(link: AcceptedLink) -> tuple[int, int, int, int, str]:
        minimum = min(link.novelty_score, link.usefulness_score, link.confidence_score)
        return (
            -minimum,
            -link.novelty_score,
            -link.usefulness_score,
            len(link.discovery_path),
            link.pair_id,
        )

    selected: list[AcceptedLink] = []
    skipped: list[str] = []
    seen_pairs: set[str] = set()
    for link in sorted(accepted, key=sort_key):
        if link.pair_id in seen_pairs:
            continue  # same pair can be re-proposed through another lane
        seen_pairs.add(link.pair_id)
        if len(selected) >= MAX_NEW_LINKS_PER_TARGET:
            skipped.append(f"{link.pair_id}: target quota")
            continue
        for page_id in (link.page_a_id, link.page_b_id):
            active = len(catalog.links_for_page(page_id))
            already = sum(1 for s in selected if page_id in (s.page_a_id, s.page_b_id))
            if active + already >= MAX_ACTIVE_LINKS_PER_PAGE:
                skipped.append(f"{link.pair_id}: endpoint_capacity")
                break
        else:
            selected.append(link)
    return selected, skipped


# ---------------------------------------------------------------------------
# Desired-state rendering and bilateral commit (sections 12-13)
# ---------------------------------------------------------------------------


def _desired_for_page(
    project: Project,
    catalog: LinkCatalog,
    page_id: str,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]], list[sqlite3.Row]]:
    """Inline + footer items for one page from active+pending-deleting state."""

    rows = catalog.conn.execute(
        "SELECT * FROM links WHERE status IN ('active','pending')"
        " AND (page_a_id=? OR page_b_id=?) ORDER BY pair_id",
        (page_id, page_id),
    ).fetchall()
    inline: list[tuple[str, str, str]] = []
    keyed: list[tuple[tuple[str, str], tuple[str, str, str]]] = []
    footer: list[tuple[str, str, str]] = []
    used: list[sqlite3.Row] = []
    for row in rows:
        side, other = ("a", "b") if row["page_a_id"] == page_id else ("b", "a")
        peer = catalog.page(row[f"page_{other}_id"])
        own = catalog.page(page_id)
        if peer is None or not peer["active"] or own is None:
            continue
        link = relative_link(own["wiki_rel_path"], peer["wiki_rel_path"])
        rendered = row[f"{side}_bridge_template"].replace(
            "{link}", f"[{peer['title']}]({link})"
        )
        anchor_id = row[f"{side}_anchor_id"]
        item = (anchor_id, row["pair_id"], rendered)
        keyed.append(((peer["title"], row["pair_id"]), item))
        inline.append(item)
        footer.append((peer["title"], link, row[f"{side}_footer_reason"]))
        used.append(row)
    # multiple blocks at one anchor sort by peer title then pair ID
    inline = [item for _key, item in sorted(keyed, key=lambda pair: pair[0])]
    return inline, footer, used


def _affected_page_ids(
    catalog: LinkCatalog,
    new_links: list[AcceptedLink],
    stale_pair_ids: list[str],
    refresh_page_ids: Sequence[str] = (),
) -> list[str]:
    affected: set[str] = set(refresh_page_ids)
    for link in new_links:
        affected.update((link.page_a_id, link.page_b_id))
    for pair_id in stale_pair_ids:
        row = catalog.conn.execute(
            "SELECT page_a_id, page_b_id FROM links WHERE pair_id=?", (pair_id,)
        ).fetchone()
        if row is not None:
            for page_id in (row["page_a_id"], row["page_b_id"]):
                page = catalog.page(page_id)
                if page is not None and page["active"]:
                    affected.add(page_id)
    return sorted(affected)


def commit_relations(
    project: Project,
    catalog: LinkCatalog,
    run_id: str,
    document_id: str,
    new_links: list[AcceptedLink],
    stale_pair_ids: list[str],
    *,
    refresh_page_ids: Sequence[str] = (),
    progress: Progress = None,
) -> list[Path]:
    """Section 13.2: stage in DB, render all affected, verify, activate."""

    affected = _affected_page_ids(
        catalog, new_links, stale_pair_ids, refresh_page_ids
    )
    if not affected:
        return []
    # close over the graph: every endpoint of every relation touching an
    # affected page must be rendered/verified too (bilateral guarantee).
    while True:
        extra: list[str] = []
        for page_id in affected:
            for row in catalog.links_for_page(page_id, ("active", "pending", "deleting")):
                for endpoint in (row["page_a_id"], row["page_b_id"]):
                    endpoint_row = catalog.page(endpoint)
                    if (
                        endpoint_row is not None
                        and endpoint_row["active"]
                        and endpoint not in affected
                        and endpoint not in extra
                    ):
                        extra.append(endpoint)
        if not extra:
            break
        affected = sorted(set(affected) | set(extra))
    originals: dict[str, str] = {}
    bases: dict[str, str] = {}
    affected_rows: dict[str, sqlite3.Row] = {}
    for page_id in affected:
        row = catalog.page(page_id)
        if row is None:
            raise LinkerError(f"endpoint page vanished from catalog: {page_id}")
        path = project.wiki / row["wiki_rel_path"]
        if not path.exists():
            raise LinkerError(f"endpoint file missing: {path}")
        text = path.read_text(encoding="utf-8")
        originals[page_id] = text
        bases[page_id] = strip_managed_links(text, path=str(path))
        affected_rows[page_id] = row

    catalog.begin_relation_commit(
        run_id,
        document_id,
        new_links,
        stale_pair_ids,
        [{"page_id": pid, "base_hash": sha256_text(bases[pid])} for pid in affected],
    )
    _emit(progress, "commit", pages=len(affected))
    written: list[Path] = []
    try:
        desired: dict[str, tuple[list, list]] = {}
        for page_id in affected:
            inline, footer, _used = _desired_for_page(project, catalog, page_id)
            anchors = extract_heading_anchors(bases[page_id])
            anchor_ids = {a["anchor_id"] for a in anchors}
            for anchor_id, pair_id, _text in inline:
                if anchor_id not in anchor_ids:
                    raise LinkerError(
                        f"anchor {anchor_id} missing on {affected_rows[page_id]['wiki_rel_path']}"
                        " (relation needs re-research; not moved silently)"
                    )
            rendered = render_managed_page(bases[page_id], inline, footer, anchors)
            desired[page_id] = (inline, footer)
            path = project.wiki / affected_rows[page_id]["wiki_rel_path"]
            if rendered != originals[page_id]:
                write_text_atomic(path, rendered)
                written.append(path)
        relations = catalog.conn.execute(
            "SELECT * FROM links WHERE status IN ('active','pending')"
        ).fetchall()
        relevant = [
            row
            for row in relations
            if row["page_a_id"] in affected or row["page_b_id"] in affected
        ]
        verify_bilateral_links(project, relevant, affected_rows, bases, desired)
        catalog.finish_relation_commit(run_id)
    except BaseException:
        for page_id, text in originals.items():
            write_text_atomic(project.wiki / affected_rows[page_id]["wiki_rel_path"], text)
        catalog.fail_relation_commit(run_id, "commit failed; rolled back")
        raise
    return [project.wiki / affected_rows[pid]["wiki_rel_path"] for pid in affected]


def recover_pending_runs(project: Project, catalog: LinkCatalog, progress: Progress = None) -> int:
    """Section 13.4: finish interrupted commits before doing new work."""

    recovered = 0
    for run in catalog.committing_runs():
        affected = json.loads(run["affected_pages_json"])
        rows: dict[str, sqlite3.Row] = {}
        bases: dict[str, str] = {}
        for item in affected:
            page_id = item["page_id"]
            row = catalog.page(page_id)
            if row is None:
                raise LinkerError(f"recovery: page {page_id} missing from catalog")
            path = project.wiki / row["wiki_rel_path"]
            text = (
                path.read_text(encoding="utf-8")
                if path.exists()
                else raise_missing(path)
            )
            base = strip_managed_links(text, path=str(path))
            if sha256_text(base) != item["base_hash"]:
                raise LinkerError(
                    f"recovery: base text changed outside markers: {path}"
                )
            rows[page_id] = row
            bases[page_id] = base
        desired: dict[str, tuple[list, list]] = {}
        for page_id in rows:
            inline, footer, _used = _desired_for_page(project, catalog, page_id)
            rendered = render_managed_page(
                bases[page_id], inline, footer, extract_heading_anchors(bases[page_id])
            )
            desired[page_id] = (inline, footer)
            path = project.wiki / rows[page_id]["wiki_rel_path"]
            write_text_atomic(path, rendered)
        affected_ids = set(rows)
        relations = [
            row
            for row in catalog.conn.execute(
                "SELECT * FROM links WHERE status IN ('active','pending')"
            ).fetchall()
            if row["page_a_id"] in affected_ids and row["page_b_id"] in affected_ids
        ]
        verify_bilateral_links(project, relations, rows, bases, desired)
        catalog.finish_relation_commit(run["run_id"])
        recovered += 1
        _emit(progress, "commit", recovered=run["run_id"])
    return recovered


def raise_missing(path: Path) -> str:
    raise LinkerError(f"recovery: endpoint file missing: {path}")


def _acquire_lock(project: Project) -> Any:
    lock_path = project.metadata / "wiki-linker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise LinkerError("another linker run holds metadata/wiki-linker.lock")
    return handle


# ---------------------------------------------------------------------------
# Vector sync (WP-3)
# ---------------------------------------------------------------------------


def sync_vectors(
    catalog: LinkCatalog,
    records: dict[str, WikiDocumentRecord],
    embedder: Any,
    progress: Progress = None,
) -> str | None:
    """Embed changed map cards only. Failure leaves vectors pending."""

    if embedder is None:
        return None
    pending = catalog.conn.execute(
        "SELECT page_id, map_text FROM pages WHERE active=1 AND vector_state='pending'"
    ).fetchall()
    if not pending:
        return None
    try:
        fingerprint = (
            f"{getattr(embedder, 'model_name', 'embed')}:"
            f"{getattr(embedder, 'dim', 0)}"
        )
        stored = catalog.get_meta("embedding_fingerprint")
        if stored is not None and stored != fingerprint:
            catalog.conn.execute("DROP TABLE IF EXISTS page_vectors")
            catalog.conn.execute("UPDATE pages SET vector_state='pending'")
            catalog.conn.execute(
                "DELETE FROM meta WHERE key IN ('embedding_fingerprint','embedding_dimension')"
            )
            catalog.conn.commit()
            catalog.set_meta("embedding_fingerprint", fingerprint)
            pending = catalog.conn.execute(
                "SELECT page_id, map_text FROM pages"
                " WHERE active=1 AND vector_state='pending'"
            ).fetchall()
        embedded = 0
        while pending:
            batch, pending = pending[:16], pending[16:]
            vectors = embedder.embed_documents([row["map_text"] for row in batch])
            if len(vectors) != len(batch):
                raise RuntimeError(
                    f"embedding count mismatch: expected {len(batch)}, got {len(vectors)}"
                )
            dimension = len(vectors[0])
            if dimension <= 0:
                raise RuntimeError("embedding endpoint returned an empty vector")
            if embedded == 0:
                catalog.rebuild_vectors(fingerprint, dimension)
            if not catalog.has_vec_table():
                raise RuntimeError("sqlite-vec unavailable")
            for row, vector in zip(batch, vectors):
                if len(vector) != dimension:
                    raise RuntimeError("embedding dimension drift")
                catalog.set_vector(row["page_id"], list(vector))
                catalog.conn.execute(
                    "UPDATE pages SET vector_state='ready' WHERE page_id=?",
                    (row["page_id"],),
                )
            catalog.conn.commit()
            embedded += len(batch)
        _emit(progress, "embed", embedded=embedded)
        return None
    except Exception as exc:
        catalog.conn.execute(
            "UPDATE pages SET vector_state='pending'"
            " WHERE page_id IN (SELECT page_id FROM pages WHERE active=1)"
        )
        catalog.conn.commit()
        _emit(progress, "embed", degraded=f"{type(exc).__name__}: {exc}")
        return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Orchestration (sections 5, 14)
# ---------------------------------------------------------------------------


def _marker(
    project: Project,
    raw_rel: str,
    status: str,
    *,
    source_sha256: str = "",
    document_map_hash: str = "",
    run_id: str = "",
    pages_considered: int = 0,
    links_added: int = 0,
    links_removed: int = 0,
    degraded_maps: int = 0,
    overflow_review_required: bool = False,
) -> dict[str, Any]:
    marker = {
        "schema_version": SCHEMA_VERSION,
        "prompt_versions": {
            "map": LINKER_MAP_PROMPT_VERSION,
            "bridge": LINKER_BRIDGE_PROMPT_VERSION,
            "research": LINKER_RESEARCH_PROMPT_VERSION,
            "judge": LINKER_JUDGE_PROMPT_VERSION,
        },
        "source_sha256": source_sha256,
        "document_map_hash": document_map_hash,
        "run_id": run_id,
        "status": status,
        "pages_considered": pages_considered,
        "links_added": links_added,
        "links_removed": links_removed,
        "degraded_maps": degraded_maps,
        "overflow_review_required": overflow_review_required,
    }
    try:
        raw = normalize_rel_path(raw_rel)
    except LinkerError:
        return marker
    path = project.wiki_dir(raw) / "_planning" / "linker.json"
    if path.parent.is_dir() or status == "disabled":
        write_json_atomic(path, marker)
    return marker


def _resume_key(document_id: str) -> str:
    return f"resume:{document_id}"


def _load_resume(catalog: LinkCatalog, document_id: str) -> list[str]:
    """Targets of the last incomplete run: C11 forbids silently forgetting them."""

    raw = catalog.get_meta(_resume_key(document_id))
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if data.get("status") == "complete":
        return []
    return [
        page_id
        for page_id in data.get("targets", [])
        if catalog.page(page_id) is not None and catalog.page(page_id)["active"]
    ]


def _save_resume(
    catalog: LinkCatalog,
    document_id: str,
    run_id: str,
    targets: list[str],
    status: str,
) -> None:
    catalog.set_meta(
        _resume_key(document_id),
        canonical_json({"run_id": run_id, "targets": targets, "status": status}),
    )


async def link_generated_document(
    project: Project,
    raw_rel: str,
    *,
    model: Any,
    settings: Any,
    embedder: Any = None,
    reranker: Any = None,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
) -> dict[str, Any]:
    """Sole writer entry point: lock, recover, sync, discover, research, commit."""

    started = time.monotonic()
    run_id = "lrun-" + uuid.uuid4().hex[:16]
    lock = _acquire_lock(project)
    catalog = LinkCatalog.open(project.linker_database)
    output_language = getattr(settings, "wiki_output_language", OUTPUT_LANGUAGE_DEFAULT)
    map_concurrency = int(getattr(settings, "wiki_linker_map_concurrency", 4))
    research_concurrency = int(getattr(settings, "wiki_linker_research_concurrency", 2))
    artifacts = _Artifacts(
        project.state_dir(raw_rel) / "work" / "linker" / run_id,
        on_progress,
    )
    try:
        _emit(on_progress, "bootstrap", document=raw_rel, run=run_id)
        recover_pending_runs(project, catalog, on_progress)

        discovered = discover_documents(project)
        records: dict[str, WikiDocumentRecord] = {}
        current: WikiDocumentRecord | None = None
        for document in discovered:
            records[document.document_id] = document
            if document.raw_rel == normalize_rel_path(raw_rel):
                current = document
        if current is None:
            raise LinkerError(f"generated document not discoverable: {raw_rel}")

        classification: dict[str, list[str]] = {
            "unchanged": [],
            "changed": [],
            "new": [],
            "removed": [],
        }
        for document in discovered:
            result = catalog.sync_document(document)
            if document is current:
                classification = result
        links_by_page: dict[str, list[str]] = {}
        for document in discovered:
            for page in document.pages:
                path = project.wiki / page.wiki_rel_path
                links_by_page[page.page_id] = extract_local_page_links(
                    strip_managed_links(path.read_text(encoding="utf-8"), path=str(path))
                )
        catalog.rebuild_edges(links_by_page)
        degraded_maps = sum(
            1 for row in catalog.conn.execute("SELECT map_quality FROM documents WHERE active=1")
            if row[0] != "observed"
        )
        _emit(
            on_progress,
            "maps",
            documents=len(discovered),
            new=len(classification["new"]),
            changed=len(classification["changed"]),
            removed=len(classification["removed"]),
        )

        degraded_service = sync_vectors(catalog, records, embedder, on_progress)

        target_ids = classification["new"] + classification["changed"]
        # C11: a previous failed run's targets are retried even when the maps
        # look unchanged; failed work must never decay into "no links".
        resumed = [pid for pid in _load_resume(catalog, current.document_id) if pid not in target_ids]
        target_ids = target_ids + resumed
        _save_resume(catalog, current.document_id, run_id, target_ids, "running")
        team = current.team
        old_documents = [
            records[row["document_id"]]
            for row in catalog.active_documents(team, exclude_document=current.document_id)
            if row["document_id"] in records
        ]

        accepted: list[AcceptedLink] = []
        skipped: list[str] = []
        overflow = False
        completed_pages: list[tuple[str, list[AcceptedLink]]] = []
        research_jobs: list[
            tuple[int, str, WikiPageRecord, sqlite3.Row, list[LinkCandidate], str]
        ] = []
        for target_number, page_id in enumerate(target_ids, start=1):
            if stop_check and stop_check():
                raise LinkerCancelled("cancelled before research")
            row = catalog.page(page_id)
            record = next(p for p in current.pages if p.page_id == page_id)
            if not old_documents:
                continue  # first document: catalog only, no scout/research calls
            checkpoint_hash = page_link_input_hash(
                catalog, record, old_documents, output_language
            )
            checkpoint = catalog.page_checkpoint_get(page_id, checkpoint_hash)
            if checkpoint is not None:
                completed_pages.append((page_id, checkpoint))
                artifacts.cache_hits.append(f"page {record.filename}")
                _emit(
                    on_progress,
                    "page_resumed",
                    page=record.filename,
                    links=len(checkpoint),
                )
                continue
            candidates, page_overflow = await discover_candidates(
                catalog,
                model,
                record,
                row,
                old_documents,
                embedder=embedder,
                reranker=reranker,
                output_language=output_language,
                concurrency=map_concurrency,
                artifacts=artifacts,
                progress=on_progress,
                stop_check=stop_check,
                target_number=target_number,
            )
            overflow = overflow or page_overflow
            _emit(
                on_progress,
                "research",
                page=record.filename,
                candidates=len(candidates),
                slots=research_concurrency,
            )
            if not candidates:
                catalog.page_checkpoint_put(page_id, checkpoint_hash, [])
                continue
            research_jobs.append(
                (target_number, page_id, record, row, candidates, checkpoint_hash)
            )

        # One request pool spans every changed page. A short page can therefore
        # fill slots released by a long page instead of waiting for its tail.
        request_sem = asyncio.Semaphore(max(1, research_concurrency))

        async def run_research(
            job: tuple[
                int, str, WikiPageRecord, sqlite3.Row, list[LinkCandidate], str
            ],
        ) -> list[AcceptedLink]:
            target_number, page_id, record, row, candidates, checkpoint_hash = job
            links = await research_target_page(
                catalog,
                model,
                project,
                record,
                row,
                candidates,
                output_language=output_language,
                concurrency=research_concurrency,
                artifacts=artifacts,
                progress=on_progress,
                stop_check=stop_check,
                target_number=target_number,
                request_sem=request_sem,
            )
            catalog.page_checkpoint_put(page_id, checkpoint_hash, links)
            _emit(
                on_progress,
                "page_checkpointed",
                page=record.filename,
                links=len(links),
            )
            return links

        page_results = await asyncio.gather(
            *(run_research(job) for job in research_jobs)
        )
        completed_pages.extend(
            (job[1], page_links) for job, page_links in zip(research_jobs, page_results)
        )
        for page_id, page_links in completed_pages:
            selected, page_skipped = select_final_links(catalog, page_id, page_links)
            accepted.extend(selected)
            skipped.extend(page_skipped)

        stale = _stale_pair_ids(catalog, current, classification, accepted)
        refresh = [
            page.page_id
            for page in current.pages
            if catalog.links_for_page(page.page_id)
        ]
        written = commit_relations(
            project, catalog, run_id, current.document_id, accepted, stale,
            refresh_page_ids=refresh,
            progress=on_progress,
        )
        if accepted or stale:
            catalog.set_meta("last_complete_run_id", run_id)
        _save_resume(catalog, current.document_id, run_id, target_ids, "complete")
        marker = _marker(
            project,
            raw_rel,
            "complete",
            source_sha256=current.source_sha256,
            document_map_hash=current.map_hash,
            run_id=run_id,
            pages_considered=len(target_ids),
            links_added=len(accepted),
            links_removed=len(stale),
            degraded_maps=degraded_maps,
            overflow_review_required=overflow,
        )
        artifacts.response(
            "run.json",
            {
                "run_id": run_id,
                "document": current.raw_rel,
                "status": "complete",
                "maps_scanned": sum(
                    1 for row in catalog.conn.execute("SELECT 1 FROM documents WHERE active=1")
                ),
                "cache_hits": artifacts.cache_hits,
                "targets": len(target_ids),
                "resumed_targets": resumed,
                "accepted": [link.pair_id for link in accepted],
                "stale": stale,
                "skipped": skipped,
                "overflow_review_required": overflow,
                "degraded_maps": degraded_maps,
                "degraded_service": degraded_service,
                "pages_written": [str(p.relative_to(project.wiki)) for p in written],
                "seconds": round(time.monotonic() - started, 3),
            },
        )
        _emit(
            on_progress,
            "done",
            added=len(accepted),
            removed=len(stale),
            seconds=round(time.monotonic() - started, 3),
        )
        return marker
    except Exception as exc:
        fail_open_run(catalog, run_id)
        try:
            document_id = make_document_id(raw_rel)
            # keep the saved targets; only flip the status so the next run resumes
            _save_resume(
                catalog, document_id, run_id, _load_resume(catalog, document_id), "failed"
            )
        except LinkerError:
            pass
        _marker(project, raw_rel, "failed")
        raise
    finally:
        catalog.close()
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        except OSError:
            pass


def fail_open_run(catalog: LinkCatalog, run_id: str) -> None:
    catalog.conn.execute(
        "INSERT OR IGNORE INTO link_runs(run_id, document_id, status,"
        " affected_pages_json, error, started_at, finished_at)"
        " VALUES(?, '', 'failed', '[]', 'incomplete before commit', ?, '')",
        (run_id, _now()),
    )
    catalog.conn.execute("DELETE FROM links WHERE status='pending' AND run_id=?", (run_id,))
    catalog.conn.commit()


def _stale_pair_ids(
    catalog: LinkCatalog,
    current: WikiDocumentRecord,
    classification: dict[str, list[str]],
    accepted: list[AcceptedLink],
) -> list[str]:
    """Relations invalidated by removal/change, excluding re-earned pairs."""

    re_earned = {link.pair_id for link in accepted}
    invalidated_pages = set(classification["removed"]) | set(classification["changed"])
    stale: list[str] = []
    for row in catalog.conn.execute("SELECT * FROM links WHERE status='active'"):
        endpoints = (row["page_a_id"], row["page_b_id"])
        if row["pair_id"] in re_earned:
            continue
        if any(page_id in invalidated_pages for page_id in endpoints):
            stale.append(row["pair_id"])
            continue
        for page_id in endpoints:
            page = catalog.page(page_id)
            if page is None or not page["active"]:
                stale.append(row["pair_id"])
                break
    return stale


def remove_document(project: Project, raw_rel: str) -> list[Path]:
    """Mark a removed document's relations deleting, rerender survivors.

    Exposed for callers that delete raw files; not wired into sync yet.
    """

    lock = _acquire_lock(project)
    catalog = LinkCatalog.open(project.linker_database)
    try:
        document_id = make_document_id(raw_rel)
        rows = catalog.conn.execute(
            "SELECT pair_id FROM links WHERE status='active' AND (page_a_id IN"
            " (SELECT page_id FROM pages WHERE document_id=?) OR page_b_id IN"
            " (SELECT page_id FROM pages WHERE document_id=?))",
            (document_id, document_id),
        ).fetchall()
        stale = [row["pair_id"] for row in rows]
        run_id = "lrun-" + uuid.uuid4().hex[:16]
        written = commit_relations(project, catalog, run_id, document_id, [], stale)
        catalog.conn.execute(
            "UPDATE documents SET active=0 WHERE document_id=?", (document_id,)
        )
        catalog.conn.execute(
            "UPDATE pages SET active=0 WHERE document_id=?", (document_id,)
        )
        catalog.conn.commit()
        return written
    finally:
        catalog.close()
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        except OSError:
            pass
