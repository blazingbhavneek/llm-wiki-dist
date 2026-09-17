"""Pure rendering and parsing of the managed Markdown links footer."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graph.common.markdown import LINKS_FOOTER_END as FOOTER_END, LINKS_FOOTER_START as FOOTER_START
from graph.wiki.page import link_entity_mentions, strip_reader_references
from graph.wiki.storage import write_text_atomic

FOOTER_TITLE = "## 関連リンク"
MAX_FOOTER_ENTRIES = 15
MAX_SIMILAR_ENTRIES = 5


@dataclass
class RenderEdge:
    edge_id: str
    peer_page_rel: str
    peer_title: str
    peer_heading: str
    label: str
    summary: str
    forward: bool = True
    source: str = ""
    via: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FooterLink:
    peer_path: str
    label: str
    summary: str
    reverse: bool = False


def relative_link(from_page_rel: str, to_page_rel: str) -> str:
    return posixpath.relpath(to_page_rel, posixpath.dirname(from_page_rel) or ".")


def ordered(edges: list[RenderEdge]) -> list[RenderEdge]:
    group = {"define": 0, "use": 0, "similar": 1}
    return sorted(edges, key=lambda edge: (group.get(edge.source, 2), edge.label, edge.peer_title, edge.edge_id))


def footer_edges(edges: list[RenderEdge]) -> list[RenderEdge]:
    """Edges are per chunk; a page footer shows one line per peer page and reason."""
    seen: set[tuple[str, ...]] = set()
    kept: list[RenderEdge] = []
    similar = 0
    for edge in ordered(edges):
        _arrow, label, summary = display(edge)
        key = (edge.peer_page_rel, label) if edge.source == "similar" else (edge.peer_page_rel, label, summary)
        if key in seen:
            continue
        if edge.source == "similar":
            if similar >= MAX_SIMILAR_ENTRIES:
                continue
            similar += 1
        seen.add(key)
        kept.append(edge)
    return kept[:MAX_FOOTER_ENTRIES]


def peer_defines(edge: RenderEdge) -> bool:
    """True when, seen from this page, the peer chunk defines the entity in ``via``."""
    return bool(edge.via) and ((edge.source == "use" and edge.forward) or (edge.source == "define" and not edge.forward))


def display(edge: RenderEdge) -> tuple[str, str, str]:
    """(arrow, label, summary) as this page's reader should see the edge."""
    if edge.source in ("use", "define") and edge.via:
        entity = edge.via[0]
        if peer_defines(edge):
            return "", "defines", f"「{entity}」の定義"
        return "", "uses", f"「{entity}」を使用"
    return ("" if edge.forward else "← "), edge.label, edge.summary


def render_page(original: str, *, page_rel: str, edges: list[RenderEdge], mode: str) -> str:
    body = strip_reader_references(original).rstrip("\n") + "\n"
    entity_paths: set[str] = set()
    if mode == "neo":
        seen_entities: set[str] = set()
        entity_targets: list[tuple[str, str]] = []
        for edge in ordered(edges):
            if not peer_defines(edge):
                continue
            entity = edge.via[0].strip()
            key = entity.casefold()
            if not entity or key in seen_entities:
                continue
            entity_targets.append((entity, relative_link(page_rel, edge.peer_page_rel)))
            seen_entities.add(key)
        for entity, path in sorted(entity_targets, key=lambda target: -len(target[0])):
            linked = link_entity_mentions(body, entity, path)
            if linked != body or f"[{entity}]({path})" in body:
                body = linked
                entity_paths.add(path)
    footer = [
        edge for edge in footer_edges([edge for edge in edges if edge.source not in {"use", "define"}])
        if relative_link(page_rel, edge.peer_page_rel) not in entity_paths
    ]
    if not footer:
        return body
    lines = [FOOTER_START, FOOTER_TITLE, ""]
    for edge in footer:
        heading = edge.peer_heading if edge.peer_heading and edge.peer_heading != edge.peer_title else ""
        peer = f"{edge.peer_title} › {heading}" if heading else edge.peer_title
        arrow, label, summary = display(edge)
        lines.append(f"- [{peer}]({relative_link(page_rel, edge.peer_page_rel)}) — {arrow}{label}: {summary}")
    lines.append(FOOTER_END)
    return body + "\n" + "\n".join(lines) + "\n"


_FOOTER_LINE_RE = re.compile(r"^- \[(?P<title>.*?)\]\((?P<path>[^)]+)\) — (?P<reverse>← )?(?P<label>[^:]+): (?P<summary>.*)$")


def parse_footer(text: str) -> list[FooterLink]:
    start = text.find(FOOTER_START)
    end = text.find(FOOTER_END, start + len(FOOTER_START)) if start >= 0 else -1
    if start < 0 or end < 0:
        return []
    result: list[FooterLink] = []
    for line in text[start:end].splitlines():
        match = _FOOTER_LINE_RE.match(line.strip())
        if match:
            result.append(FooterLink(match["path"], match["label"].strip(), match["summary"].strip(), bool(match["reverse"])))
    return result


def write_if_changed(path: Path, text: str) -> bool:
    path = Path(path)
    if path.exists() and path.read_bytes() == text.encode("utf-8"):
        return False
    write_text_atomic(path, text)
    return True


__all__ = ["FOOTER_END", "FOOTER_START", "FOOTER_TITLE", "FooterLink", "MAX_FOOTER_ENTRIES", "MAX_SIMILAR_ENTRIES", "display", "footer_edges", "peer_defines", "RenderEdge", "ordered", "parse_footer", "relative_link", "render_page", "write_if_changed"]
