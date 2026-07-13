# mcp_server.py
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# Reuse your existing app.py runtime.
#
# This gives us:
# - DB_DIR
# - _DB_RE
# - STACKS
# - stages
# - errors
# - _ensure_building()
# - _dump()
# - Researcher stack
import app as wiki_runtime


log = logging.getLogger("llm_wiki_mcp")

# Current sqlite/wiki name for this MCP server process.
# Direct FastMCP mode serves one wiki per process.
ACTIVE_DB: str | None = None


# =============================================================================
# Image block cleanup helpers
# =============================================================================

_IMAGE_UNIT_RE = re.compile(
    r"<image-unit\b[^>]*>(?P<body>.*?)</image-unit>",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_DESCRIPTION_RE = re.compile(
    r"<image-description\b[^>]*>(?P<description>.*?)</image-description>",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_MEDIA_RE = re.compile(
    r"<image-media\b[^>]*>.*?</image-media>",
    re.IGNORECASE | re.DOTALL,
)

_IMAGE_SRC_RE = re.compile(
    r"""<img\b[^>]*\bsrc=["'](?P<src>data:image/[^"']+)["'][^>]*>""",
    re.IGNORECASE | re.DOTALL,
)

_DATA_IMAGE_URL_RE = re.compile(
    r"""data:image/[^"' \n\r\t>]+""",
    re.IGNORECASE | re.DOTALL,
)


def has_image_units(text: str) -> bool:
    """Return True if the text contains at least one image-unit block."""
    if not isinstance(text, str) or not text:
        return False

    return _IMAGE_UNIT_RE.search(text) is not None


def count_image_units(text: str) -> int:
    """Return the number of image-unit blocks."""
    if not isinstance(text, str) or not text:
        return 0

    return len(list(_IMAGE_UNIT_RE.finditer(text)))


def strip_image_media(text: str) -> str:
    """
    Remove embedded image media payloads from image-unit blocks.

    If an image-description exists, keep only the description.
    If no description exists, remove only the image-media block and keep
    any remaining non-media content inside the image-unit.
    """
    if not isinstance(text, str) or not text:
        return text

    def replace_image_unit(match: re.Match[str]) -> str:
        body = match.group("body")

        description = _IMAGE_DESCRIPTION_RE.search(body)
        if description:
            return description.group("description").strip()

        return _IMAGE_MEDIA_RE.sub("", body).strip()

    return _IMAGE_UNIT_RE.sub(replace_image_unit, text).strip()


def replace_image_media_with_marker(
    text: str,
    marker: str = "[image omitted]",
) -> str:
    """
    Replace image-media blocks with a marker while preserving image-unit structure.

    This is useful when the caller wants to keep a visible placeholder instead
    of deleting image payloads completely.
    """
    if not isinstance(text, str) or not text:
        return text

    return _IMAGE_MEDIA_RE.sub(marker, text).strip()


def extract_image_descriptions(text: str) -> list[str]:
    """Return non-empty image-description values from image-unit blocks."""
    if not isinstance(text, str) or not text:
        return []

    descriptions: list[str] = []

    for unit_match in _IMAGE_UNIT_RE.finditer(text):
        body = unit_match.group("body")
        description = _IMAGE_DESCRIPTION_RE.search(body)

        if description:
            value = description.group("description").strip()
            if value:
                descriptions.append(value)

    return descriptions


def extract_image_data_urls(text: str) -> list[str]:
    """Return data:image/... URLs from image-media blocks."""
    if not isinstance(text, str) or not text:
        return []

    urls: list[str] = []

    for unit_match in _IMAGE_UNIT_RE.finditer(text):
        body = unit_match.group("body")
        media = _IMAGE_MEDIA_RE.search(body)

        if not media:
            continue

        src = _IMAGE_SRC_RE.search(media.group(0))

        if src:
            urls.append(src.group("src").strip())

    return urls


def extract_image_base64(text: str) -> list[str]:
    """Return raw base64 payloads from image data URLs."""
    values: list[str] = []

    for data_url in extract_image_data_urls(text):
        if "," in data_url:
            values.append(data_url.split(",", 1)[1])

    return values


def sanitize_markdown_for_text_llm(text: str) -> str:
    """
    Remove embedded image payloads before sending Markdown to a text-only LLM.

    Behavior:
    - Removes base64 image data from image-unit/image-media blocks.
    - If image-description exists, keeps that semantic description.
    - If image-description is empty/missing, removes image-media payload and
      keeps any remaining non-media text.
    - Also handles standalone image-media blocks and standalone data:image URLs
      defensively, in case malformed content exists outside image-unit blocks.

    Important:
    - This does NOT truncate normal text.
    - This does NOT impose any character limit.
    - Full node body text is preserved except embedded base64 image payloads.
    """
    if not isinstance(text, str) or not text:
        return text

    cleaned = strip_image_media(text)

    # Defensive cleanup for image-media blocks that might exist outside image-unit.
    cleaned = _IMAGE_MEDIA_RE.sub("[image omitted]", cleaned)

    # Defensive cleanup for standalone data:image URLs.
    cleaned = _DATA_IMAGE_URL_RE.sub("data:image/[base64 payload omitted]", cleaned)

    return cleaned.strip()


# =============================================================================
# Runtime helpers
# =============================================================================

def _validate_db_name(db: str) -> None:
    if not wiki_runtime._DB_RE.fullmatch(db):
        raise ValueError("unknown wiki")


def _set_active_db(db: str) -> None:
    global ACTIVE_DB

    _validate_db_name(db)
    ACTIVE_DB = db


async def _wait_until_ready(db: str, timeout_seconds: float = 90.0) -> dict:
    """
    Lazily bootstrap the requested DB using app.py's existing stack builder.

    app.py already supports one runtime stack per sqlite name:
        STACKS[db]

    This waits up to timeout_seconds so the first MCP call can trigger startup.
    """
    _validate_db_name(db)

    if wiki_runtime.stages.get(db) == "ready" and db in wiki_runtime.STACKS:
        return wiki_runtime.STACKS[db]

    wiki_runtime._ensure_building(db)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    while loop.time() < deadline:
        if wiki_runtime.stages.get(db) == "ready" and db in wiki_runtime.STACKS:
            return wiki_runtime.STACKS[db]

        if wiki_runtime.stages.get(db) == "failed":
            raise RuntimeError(
                f"wiki '{db}' bootstrap failed: {wiki_runtime.errors.get(db)}"
            )

        await asyncio.sleep(0.25)

    stage = wiki_runtime.stages.get(db, "starting")
    err = wiki_runtime.errors.get(db)

    raise RuntimeError(
        f"wiki '{db}' is not ready yet; stage={stage}; detail={err or 'building'}"
    )


async def _researcher_for_current_db():
    db = ACTIVE_DB

    if not db:
        raise RuntimeError("missing db context; start server with --wiki {sqlite_name}")

    stack = await _wait_until_ready(db)
    return stack["researcher"]


async def _search_with_evidence(researcher, query: str, limit: int):
    """
    Prefer public Researcher.search_with_evidence() if it exists.
    Otherwise use the existing Researcher._run(...) bridge.
    """
    if hasattr(researcher, "search_with_evidence"):
        return await researcher.search_with_evidence(query, limit)

    return await researcher._run(
        lambda session: session.search_with_evidence(query, limit)
    )


async def _follow_link_limited(
    researcher,
    node_id: str,
    *,
    label: str | None,
    direction: str,
    limit: int,
):
    """
    Follow graph links from a node.

    Supports both possible Researcher signatures:
    - follow_link(..., limit=N)
    - follow_link(...) then slice result
    """
    try:
        return await researcher.follow_link(
            node_id,
            label=label,
            direction=direction,
            limit=limit,
        )
    except TypeError as exc:
        if "limit" not in str(exc):
            raise

        pairs = await researcher.follow_link(
            node_id,
            label=label,
            direction=direction,
        )
        return pairs[:limit]


# =============================================================================
# JSON serialization helpers
# =============================================================================

def _jsonable(obj: Any) -> Any:
    """
    Convert app objects / dataclasses / pydantic models into JSON-safe values.
    """
    obj = wiki_runtime._dump(obj)

    if obj is None:
        return None

    if isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, Enum):
        return obj.value

    if isinstance(obj, (datetime, date)):
        return obj.isoformat()

    if is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(asdict(obj))

    if isinstance(obj, Path):
        return str(obj)

    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]

    return str(obj)


def _normalize_node_ids(node_ids: str | list[str]) -> list[str]:
    """
    Accept a single node ID or a list of node IDs.
    Remove blank values and duplicates while preserving order.
    """
    if isinstance(node_ids, str):
        raw = [node_ids]
    else:
        raw = node_ids

    cleaned: list[str] = []
    seen: set[str] = set()

    for node_id in raw:
        value = str(node_id).strip()

        if not value:
            continue

        if value in seen:
            continue

        seen.add(value)
        cleaned.append(value)

    return cleaned


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _node_id_from_data(data: dict[str, Any]) -> str:
    return _as_text(
        data.get("id")
        or data.get("node_id")
        or data.get("requested_id")
        or ""
    )


def _node_title_from_data(data: dict[str, Any]) -> str:
    return _as_text(data.get("title") or data.get("name") or "(untitled)")


def _node_summary_from_data(data: dict[str, Any]) -> str:
    return _as_text(data.get("summary") or "(no summary)")


def _node_body_from_data(data: dict[str, Any]) -> str:
    return _as_text(data.get("body") or data.get("content") or data.get("text") or "")


def _node_type_from_data(data: dict[str, Any]) -> str:
    return _as_text(data.get("type") or data.get("kind") or "")


def _node_source_from_data(data: dict[str, Any]) -> str:
    source_bits: list[str] = []

    document = data.get("original_document_name")
    path = data.get("source_path")
    ranges = data.get("source_ranges")

    if document:
        source_bits.append(f"document={document}")

    if path:
        source_bits.append(f"path={path}")

    if ranges:
        source_bits.append(f"ranges={ranges}")

    return "; ".join(source_bits)


def _edge_label(edge: Any) -> str:
    """
    Extract a useful relation label from an edge object.
    """
    data = _jsonable(edge)

    if isinstance(data, dict):
        for key in (
            "label",
            "type",
            "kind",
            "name",
            "predicate",
            "relation",
            "edge_type",
        ):
            if data.get(key):
                return str(data[key])

        compact = {
            k: v
            for k, v in data.items()
            if k in {"source", "target", "from", "to", "weight", "score"}
        }
        if compact:
            return str(compact)

        return "related"

    if data:
        return str(data)

    return "related"


def _format_ranked_search_result(rank: int, node: Any, score: Any = None) -> str:
    """
    Format one compact search result.

    Important:
    - No evidence chunks.
    - No body text.
    - No embedded media.
    - No raw giant JSON.
    """
    data = _jsonable(node)

    if not isinstance(data, dict):
        return (
            f"### {rank}. Unknown node\n\n"
            f"- **Raw:** `{data}`\n"
        )

    node_id = _node_id_from_data(data)
    title = _node_title_from_data(data)
    summary = _node_summary_from_data(data)
    node_type = _node_type_from_data(data)

    lines = [
        f"### {rank}. {title}",
        "",
        f"- **Node ID:** `{node_id}`",
    ]

    if score is not None:
        lines.append(f"- **Score:** `{score}`")

    if node_type:
        lines.append(f"- **Type:** `{node_type}`")

    lines.append(f"- **Summary:** {summary}")
    lines.append("")

    return "\n".join(lines)


def _format_neighbor(
    index: int,
    seed_node_id: str,
    edge: Any,
    node: Any,
) -> str:
    """
    Format a neighboring graph node in a compact next-read style.
    """
    data = _jsonable(node)
    label = _edge_label(edge)

    if not isinstance(data, dict):
        return (
            f"{index}. Unknown neighboring node\n"
            f"   - **Seed Node ID:** `{seed_node_id}`\n"
            f"   - **Relation:** `{label}`\n"
            f"   - **Raw:** `{data}`\n"
        )

    node_id = _node_id_from_data(data)
    title = _node_title_from_data(data)
    summary = _node_summary_from_data(data)
    node_type = _node_type_from_data(data)

    lines = [
        f"{index}. **{title}**",
        f"   - **Neighbor Node ID:** `{node_id}`",
        f"   - **Relation:** `{label}`",
    ]

    if node_type:
        lines.append(f"   - **Type:** `{node_type}`")

    lines.append(f"   - **Summary:** {summary}")

    return "\n".join(lines) + "\n"


def _compact_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


# =============================================================================
# MCP server + tools
# =============================================================================

mcp = FastMCP(
    name="llm-wiki-graph",
    instructions=(
        "Read-only LLM-Wiki graph exploration server. "
        "Recommended workflow: "
        "1) Use hybrid_search to find relevant node IDs. "
        "2) Use read_nodes on the best node IDs to read the full source body and see neighboring nodes. "
        "3) Use explore_links if wider graph traversal is needed. "
        "Tool responses are Markdown, not raw JSON. "
        "Node bodies are returned in full; embedded base64 image payloads are removed before output. "
        "If image descriptions exist, they are preserved as semantic text."
    ),
)


@mcp.tool()
async def hybrid_search(query: str, limit: int = 10) -> str:
    """
    Search the active wiki graph and return compact ranked results.

    Use this tool FIRST when the user asks about an unknown topic, API,
    error code, design rule, behavior, Japanese manual concept, subsystem,
    or implementation detail.

    Input:
    - query:
        Natural-language search query. Japanese and English are both okay.
        Examples:
        - "ファイルのデッドロック"
        - "E_MFS_ETIMEOUT"
        - "category lock deadlock"
    - limit:
        Number of search results to return.
        Default: 10.
        This controls result count only. It is not a body/text character limit.

    Response format:
    - Markdown, not raw JSON.
    - Results are sorted by best relevance/ranking.
    - Each result contains:
        - Rank number
        - Node title
        - Node ID
        - Score, if available
        - Node type, if available
        - Node summary

    Important:
    - This tool intentionally does NOT return evidence chunks, full bodies,
      image payloads, or raw large JSON.
    - The purpose is to identify the right node IDs.
    - After this tool, call read_nodes with the most relevant node IDs.
    - Use node IDs exactly as returned.
    """
    query = query.strip()

    if not query:
        return (
            "# Hybrid Search Results\n\n"
            "**Error:** empty query.\n"
        )

    limit = max(1, int(limit or 10))

    researcher = await _researcher_for_current_db()
    results = await _search_with_evidence(researcher, query, limit)

    lines: list[str] = [
        "# Hybrid Search Results",
        "",
        f"- **Query:** `{query}`",
        f"- **Requested result count:** `{limit}`",
        "",
        "## How to use these results",
        "",
        "Pick the most relevant node IDs below, then call `read_nodes` to inspect the full source body and neighboring nodes.",
        "",
    ]

    output_count = 0

    for rank, item in enumerate(results, start=1):
        dumped = _jsonable(item)

        if isinstance(dumped, dict):
            node = dumped.get("node")
            score = dumped.get("score")
        else:
            node = dumped
            score = None

        lines.append(_format_ranked_search_result(rank, node, score))
        output_count += 1

    if output_count == 0:
        lines.append("No matching nodes found.")

    lines.extend(
        [
            "",
            "## Next step",
            "",
            "Call `read_nodes` with one or more Node IDs from above.",
        ]
    )

    return "\n".join(lines)


@mcp.tool()
async def read_nodes(
    node_ids: str | list[str],
    neighbor_limit: int = 10,
    direction: str = "both",
) -> str:
    """
    Read one or more wiki nodes by exact node ID.

    Use this tool AFTER hybrid_search when you need actual source/manual
    content from selected nodes.

    Input:
    - node_ids:
        A single node ID string or a list of node ID strings.
        Examples:
        - "node:moove-file-management-us:2fa49de66669"
        - ["node:a", "node:b"]
    - neighbor_limit:
        Number of neighboring linked nodes to show per requested node.
        Default: 10.
        This controls only how many related nodes are listed.
        It does NOT truncate or limit the requested node body.
        Use 0 if you only want the requested full node body.
    - direction:
        Which graph links to show around the node.
        Allowed values:
        - "incoming": nodes that point to this node
        - "outgoing": nodes this node points to
        - "both": both incoming and outgoing links
        Default: "both".

    Response format:
    - Markdown, not raw JSON.
    - For each requested node:
        - Node ID
        - Title
        - Type, if available
        - Summary
        - Source information, if available
        - Full body text
        - Image cleanup stats
        - Neighboring nodes with:
            - Neighbor node ID
            - Relation label
            - Neighbor title
            - Neighbor summary

    Image/base64 behavior:
    - The body is returned in full except embedded image payloads.
    - image-media base64 data is removed before output.
    - If image-description exists, the description is preserved and used as
      the semantic image text.
    - Base64 data URLs are never sent to the text-only LLM.

    How the LLM should use the response:
    - Use the Body section as the primary source for answering the user.
    - Use Neighboring Nodes to decide what node to read next.
    - If more graph context is needed, call explore_links.
    """
    ids = _normalize_node_ids(node_ids)

    if not ids:
        return (
            "# Read Nodes\n\n"
            "**Error:** no node IDs supplied.\n"
        )

    normalized_direction = direction.lower().strip()
    if normalized_direction not in {"incoming", "outgoing", "both"}:
        return (
            "# Read Nodes\n\n"
            "**Error:** direction must be `incoming`, `outgoing`, or `both`.\n"
        )

    neighbor_limit = max(0, int(neighbor_limit or 0))

    researcher = await _researcher_for_current_db()

    lines: list[str] = [
        "# Read Nodes",
        "",
        f"- **Requested node count:** `{len(ids)}`",
        f"- **Neighbor direction:** `{normalized_direction}`",
        f"- **Neighbor count per node:** `{neighbor_limit}`",
        "- **Body behavior:** full body returned; no character truncation",
        "- **Image behavior:** embedded base64 image payloads removed; image descriptions preserved",
        "",
    ]

    missing: list[str] = []

    for node_index, node_id in enumerate(ids, start=1):
        try:
            node = await researcher.read_node(node_id)
        except Exception as exc:
            lines.extend(
                [
                    f"## {node_index}. `{node_id}`",
                    "",
                    f"**Error reading node:** `{_compact_error(exc)}`",
                    "",
                ]
            )
            continue

        if node is None:
            missing.append(node_id)
            continue

        data = _jsonable(node)

        if not isinstance(data, dict):
            raw_text = sanitize_markdown_for_text_llm(str(data))
            lines.extend(
                [
                    f"## {node_index}. `{node_id}`",
                    "",
                    raw_text,
                    "",
                ]
            )
            continue

        actual_id = _node_id_from_data(data) or node_id
        title = _node_title_from_data(data)
        summary = _node_summary_from_data(data)
        node_type = _node_type_from_data(data)
        source = _node_source_from_data(data)

        raw_body = _node_body_from_data(data)
        image_unit_count = count_image_units(raw_body)
        image_description_count = len(extract_image_descriptions(raw_body))
        image_payload_count = len(extract_image_data_urls(raw_body))
        body = sanitize_markdown_for_text_llm(raw_body)

        lines.extend(
            [
                f"## {node_index}. {title}",
                "",
                f"- **Node ID:** `{actual_id}`",
            ]
        )

        if node_type:
            lines.append(f"- **Type:** `{node_type}`")

        lines.append(f"- **Summary:** {summary}")

        if source:
            lines.append(f"- **Source:** {source}")

        lines.extend(
            [
                "- **Body returned:** full body, no character truncation",
                f"- **Image units detected:** `{image_unit_count}`",
                f"- **Image descriptions preserved:** `{image_description_count}`",
                f"- **Embedded image payloads removed:** `{image_payload_count}`",
                "",
                "### Body",
                "",
                body if body else "(no body text)",
                "",
            ]
        )

        if neighbor_limit > 0:
            lines.extend(
                [
                    "### Neighboring Nodes",
                    "",
                    "These are useful next reads connected to this node.",
                    "",
                ]
            )

            try:
                pairs = await _follow_link_limited(
                    researcher,
                    actual_id,
                    label=None,
                    direction=normalized_direction,
                    limit=neighbor_limit,
                )

                if not pairs:
                    lines.extend(
                        [
                            "(no neighboring nodes found)",
                            "",
                        ]
                    )
                else:
                    for neighbor_index, pair in enumerate(pairs, start=1):
                        try:
                            edge, neighbor_node = pair
                        except Exception:
                            lines.append(
                                f"{neighbor_index}. Could not parse neighbor pair: `{pair}`"
                            )
                            continue

                        lines.append(
                            _format_neighbor(
                                neighbor_index,
                                actual_id,
                                edge,
                                neighbor_node,
                            )
                        )

            except Exception as exc:
                lines.extend(
                    [
                        f"Could not fetch neighboring nodes for `{actual_id}`.",
                        f"- **Error:** `{_compact_error(exc)}`",
                        "",
                    ]
                )

    if missing:
        lines.extend(
            [
                "## Missing Nodes",
                "",
                *[f"- `{node_id}`" for node_id in missing],
                "",
            ]
        )

    lines.extend(
        [
            "## Next step",
            "",
            "If more context is needed, call `read_nodes` on one of the Neighbor Node IDs or call `explore_links` for wider graph traversal.",
        ]
    )

    return "\n".join(lines)


@mcp.tool()
async def explore_links(
    node_ids: str | list[str],
    direction: str = "both",
    label: str | None = None,
    limit: int = 30,
) -> str:
    """
    Explore graph links from one or more seed nodes.

    Use this tool when:
    - read_nodes shows a useful node and you want related concepts.
    - You need to understand what is upstream or downstream of a node.
    - You want to choose the next node to read.
    - You need graph context without reading full bodies.

    Input:
    - node_ids:
        A single seed node ID string or a list of seed node IDs.
    - direction:
        Allowed values:
        - "incoming": nodes that point to the seed node
        - "outgoing": nodes the seed node points to
        - "both": both incoming and outgoing links
        Default: "both".
    - label:
        Optional relation/edge label filter.
        Use null or omit this parameter to show all relation labels.
    - limit:
        Total number of links returned across all seed nodes.
        Default: 30.
        This controls graph fanout only. It is not a text/body character limit.

    Response format:
    - Markdown, not raw JSON.
    - For each seed node, the response lists linked neighbor nodes.
    - Each linked neighbor shows:
        - Seed node ID
        - Relation/edge label
        - Neighbor node ID
        - Neighbor title
        - Neighbor type, if available
        - Neighbor summary

    How the LLM should use the response:
    - Use this as a navigation map.
    - Pick promising Neighbor Node IDs.
    - Then call read_nodes on those IDs to inspect full source content.
    """
    ids = _normalize_node_ids(node_ids)

    if not ids:
        return (
            "# Explore Links\n\n"
            "**Error:** no node IDs supplied.\n"
        )

    normalized_direction = direction.lower().strip()

    if normalized_direction not in {"incoming", "outgoing", "both"}:
        return (
            "# Explore Links\n\n"
            "**Error:** direction must be `incoming`, `outgoing`, or `both`.\n"
        )

    limit = max(1, int(limit or 30))

    researcher = await _researcher_for_current_db()

    lines: list[str] = [
        "# Explore Links",
        "",
        f"- **Seed node count:** `{len(ids)}`",
        f"- **Direction:** `{normalized_direction}`",
        f"- **Label filter:** `{label if label else 'none'}`",
        f"- **Total link count requested:** `{limit}`",
        "",
        "## How to use this output",
        "",
        "Use `read_nodes` on any promising Neighbor Node ID below to inspect full source content.",
        "",
    ]

    total_count = 0
    failed: list[dict[str, str]] = []

    for seed_index, node_id in enumerate(ids, start=1):
        remaining = limit - total_count

        if remaining <= 0:
            break

        lines.extend(
            [
                f"## Seed {seed_index}: `{node_id}`",
                "",
            ]
        )

        try:
            pairs = await _follow_link_limited(
                researcher,
                node_id,
                label=label,
                direction=normalized_direction,
                limit=remaining,
            )
        except Exception as exc:
            failed.append(
                {
                    "node_id": node_id,
                    "error": _compact_error(exc),
                }
            )
            lines.extend(
                [
                    f"Could not explore links for `{node_id}`.",
                    f"- **Error:** `{_compact_error(exc)}`",
                    "",
                ]
            )
            continue

        if not pairs:
            lines.extend(
                [
                    "(no links found)",
                    "",
                ]
            )
            continue

        for local_index, pair in enumerate(pairs, start=1):
            try:
                edge, neighbor_node = pair
            except Exception:
                lines.append(
                    f"{local_index}. Could not parse link pair: `{pair}`"
                )
                continue

            total_count += 1

            lines.append(
                _format_neighbor(
                    local_index,
                    node_id,
                    edge,
                    neighbor_node,
                )
            )

            if total_count >= limit:
                break

        lines.append("")

    lines.extend(
        [
            "## Summary",
            "",
            f"- **Returned links:** `{total_count}`",
            f"- **Failed seeds:** `{len(failed)}`",
            "",
        ]
    )

    if failed:
        lines.append("### Failed Seeds")
        lines.append("")
        for item in failed:
            lines.append(f"- `{item['node_id']}`: {item['error']}")
        lines.append("")

    lines.extend(
        [
            "## Next step",
            "",
            "Call `read_nodes` on the most relevant Neighbor Node IDs.",
        ]
    )

    return "\n".join(lines)


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-Wiki MCP server")

    parser.add_argument(
        "--wiki",
        default=os.environ.get("MCP_WIKI", "wiki"),
        help="SQLite/wiki name to serve. Example: wiki",
    )

    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_BIND_HOST", "0.0.0.0"),
        help="Bind host. Use 0.0.0.0 to listen on all interfaces.",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PORT", "8001")),
        help="HTTP port.",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

    logging.getLogger("graph_librarian").setLevel(logging.INFO)

    _set_active_db(args.wiki)

    log.info("Starting LLM-Wiki MCP server")
    log.info("wiki=%s", args.wiki)
    log.info("bind=%s:%s", args.host, args.port)

    mcp.run(
        transport="http",
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
