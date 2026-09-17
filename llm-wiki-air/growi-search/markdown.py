"""Pure Markdown parsing: headings/sections and link extraction. No network I/O."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import unquote

_FENCE_RE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>.*?)[ \t]*$")
_INDENT_CODE_RE = re.compile(r"^(?:[ ]{4}|\t)\S")
# [anchor](target) or [anchor](target "title"); guard against images (leading !).
_LINK_RE = re.compile(
    r"(?<!!)\[(?P<anchor>[^\]\n]*)\]\(\s*(?P<target>[^)\s]*)(?:[ \t]+\"[^\")]*\")?\s*\)"
)
_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")


def strip_search_highlights(text: str) -> str:
    """Drop GROWI highlight tags but keep their inner text and any other HTML."""
    text = text or ""
    text = re.sub(
        r"<em class=\"highlighted-keyword\">(.*?)</em>", r"\1", text, flags=re.DOTALL
    )
    text = re.sub(r"</?em>", "", text)
    import html as _html

    return _html.unescape(text)


def page_title(path: str, body: str) -> str:
    for line in (body or "").splitlines():
        match = _HEADING_RE.match(line)
        if match:
            return match.group("text").strip()
    return (path or "").rstrip("/").split("/")[-1] or path or ""


@dataclass
class MarkdownSection:
    heading: str
    level: int
    start_line: int          # 1-based
    end_line: int            # 1-based, inclusive
    body: str
    breadcrumb: str = ""


@dataclass
class ParsedLink:
    anchor: str
    raw_target: str
    fragment: str = ""
    heading: str = ""        # breadcrumb at the link's location
    summary: str = ""        # em-dash trailing text on the same line
    line: int = 0


@dataclass
class ResolvedTarget:
    page_id: str | None = None
    path: str | None = None
    fragment: str = ""
    kind: str = "path"       # "id" | "path"


def _fence_state(line: str, fence: str | None) -> tuple[bool, str | None]:
    """Return (in_fence_line, new_fence) for one raw line."""
    fence_match = _FENCE_RE.match(line)
    if fence is not None:
        if (
            fence_match
            and fence_match.group("fence")[0] == fence[0]
            and len(fence_match.group("fence")) >= len(fence)
            and not fence_match.group(0).strip(fence[0] + " \t")
        ):
            return True, None
        return True, fence
    if fence_match:
        return True, fence_match.group("fence")
    return False, fence


def split_sections(body: str, max_chars: int = 3000) -> list[MarkdownSection]:
    """Split into ATX-heading sections (fence-safe) plus a leading preamble."""
    lines = (body or "").split("\n")
    headings: list[tuple[int, int, str]] = []  # (0-based line, level, text)
    fence: str | None = None
    for index, line in enumerate(lines):
        in_fence, fence = _fence_state(line, fence)
        if in_fence:
            continue
        if line.strip() and _INDENT_CODE_RE.match(line):
            continue
        match = _HEADING_RE.match(line)
        if match:
            headings.append((index, len(match.group("hashes")), match.group("text").strip()))

    sections: list[MarkdownSection] = []

    if not headings:
        preamble = "\n".join(lines)
        if preamble.strip():
            sections.extend(
                _emit(MarkdownSection("", 0, 1, len(lines), preamble, ""), max_chars)
            )
        return sections

    if headings[0][0] > 0:
        pre = "\n".join(lines[: headings[0][0]])
        if pre.strip():
            sections.extend(_emit(MarkdownSection("", 0, 1, headings[0][0], pre, ""), max_chars))

    stack: list[tuple[int, str]] = []  # (level, heading) breadcrumb
    for pos, (line_index, level, text) in enumerate(headings):
        start = line_index
        end = headings[pos + 1][0] if pos + 1 < len(headings) else len(lines)
        text_block = "\n".join(lines[start + 1 : end])
        while stack and stack[-1][0] >= level:
            stack.pop()
        breadcrumb = " > ".join(h for _lvl, h in stack)
        section = MarkdownSection(
            heading=text,
            level=level,
            start_line=start + 1,
            end_line=end,
            body=text_block,
            breadcrumb=breadcrumb,
        )
        stack.append((level, text))
        sections.extend(_emit(section, max_chars))

    return sections


def _emit(section: MarkdownSection, max_chars: int) -> list[MarkdownSection]:
    """Split an oversized section on paragraph boundaries, never inside a fence."""
    if len(section.body) <= max_chars or not section.body.strip():
        return [section]

    paragraphs = _paragraphs_outside_fences(section.body)
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > max_chars:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)

    out: list[MarkdownSection] = []
    line_offset = section.start_line
    for part_index, chunk in enumerate(chunks):
        consumed = chunk.count("\n") + 1
        label = section.heading if part_index == 0 else f"{section.heading} (続き {part_index + 1})"
        out.append(
            MarkdownSection(
                heading=label,
                level=section.level,
                start_line=line_offset,
                end_line=min(line_offset + consumed - 1, section.end_line),
                body=chunk,
                breadcrumb=section.breadcrumb,
            )
        )
        line_offset = min(line_offset + consumed, section.end_line)
    return out


def _paragraphs_outside_fences(text: str) -> list[str]:
    """Blank-line paragraphs, but a fenced block stays glued to its opening line."""
    lines = text.split("\n")
    paragraphs: list[str] = []
    buffer: list[str] = []
    fence: str | None = None
    for line in lines:
        in_fence, fence = _fence_state(line, fence)
        buffer.append(line)
        if not in_fence and line.strip() == "":
            paragraphs.append("\n".join(buffer).strip("\n"))
            buffer = []
    if buffer:
        paragraphs.append("\n".join(buffer).strip("\n"))
    return [p for p in paragraphs if p.strip()]


def extract_links(body: str) -> list[ParsedLink]:
    out: list[ParsedLink] = []
    seen: set[str] = set()
    breadcrumb_parts: list[tuple[int, str]] = []
    fence: str | None = None

    for number, raw_line in enumerate((body or "").split("\n"), start=1):
        in_fence, fence = _fence_state(raw_line, fence)
        if in_fence:
            continue
        if raw_line.strip() and _INDENT_CODE_RE.match(raw_line):
            continue

        heading = _HEADING_RE.match(raw_line)
        if heading:
            level = len(heading.group("hashes"))
            text = heading.group("text").strip()
            while breadcrumb_parts and breadcrumb_parts[-1][0] >= level:
                breadcrumb_parts.pop()
            breadcrumb_parts.append((level, text))
            continue

        breadcrumb = " > ".join(h for _lvl, h in breadcrumb_parts)

        for match in _LINK_RE.finditer(raw_line):
            target = match.group("target").strip()
            anchor = match.group("anchor").strip()
            if not target or target.startswith("#") or target.startswith("/attachment/"):
                continue
            if re.match(r"^(https?|mailto|ftp|data|tel):", target, re.IGNORECASE):
                continue

            path_part, sep, frag = target.partition("#")
            fragment = unquote(frag) if sep else ""
            key = f"{path_part}#{fragment}"
            if key in seen:
                continue
            seen.add(key)

            summary = ""
            tail = raw_line[match.end():].strip()
            for dash in ("—", "–", "―", " - ", " – "):  # em/en dash = relationship summary
                if tail.startswith(dash):
                    summary = tail[len(dash):].strip(" \t-–—―:：").strip()
                    break

            out.append(
                ParsedLink(
                    anchor=anchor,
                    raw_target=path_part,
                    fragment=fragment,
                    heading=breadcrumb,
                    summary=summary,
                    line=number,
                )
            )
    return out


def resolve_target(source_path: str, raw_target: str, root: str | None = None) -> ResolvedTarget | None:
    raw_target = (raw_target or "").strip()
    if not raw_target:
        return None

    path_part, sep, frag = raw_target.partition("#")
    fragment = unquote(frag) if sep else ""

    # Rule 1: permalink to a GROWI page id.
    bare = path_part[1:] if path_part.startswith("/") else path_part
    if _ID_RE.match(bare):
        return ResolvedTarget(page_id=bare, fragment=fragment, kind="id")

    # Rule 2/3: absolute or relative wiki path.
    if path_part.startswith("/"):
        resolved = posixpath.normpath(path_part)
    else:
        base = posixpath.dirname(source_path or "/")
        resolved = posixpath.normpath(posixpath.join(base, path_part))
    if not resolved.startswith("/"):
        resolved = "/" + resolved
    if resolved.lower().endswith(".md"):
        resolved = resolved[:-3]

    if root and root != "/" and not (
        resolved == root.rstrip("/") or resolved.startswith(root.rstrip("/") + "/")
    ):
        return None

    return ResolvedTarget(path=resolved, fragment=fragment, kind="path")


def section_summary(section: MarkdownSection | str, max_chars: int = 500) -> str:
    text = section.body if isinstance(section, MarkdownSection) else str(section or "")
    text = _strip_fences(text)
    for para in _paragraphs_outside_fences(text):
        clean = " ".join(para.split())
        if clean and not clean.startswith("#"):
            return clean[:max_chars]
    return " ".join(text.split())[:max_chars]


def _strip_fences(text: str) -> str:
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        in_fence, fence = _fence_state(line, fence)
        if in_fence:
            continue
        out.append(line)
    return "\n".join(out)
