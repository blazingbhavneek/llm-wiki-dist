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
from urllib.parse import quote

import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("llm_wiki_mcp")

_DB_RE = re.compile(r"[A-Za-z0-9_-]+")
DEFAULT_PREFIX = os.environ.get("WIKI_PREFIX", "/llm-wiki").rstrip("/")
DEFAULT_BACKEND_ORIGIN = os.environ.get(
    "MCP_BACKEND_ORIGIN", "http://127.0.0.1:8000"
).rstrip("/")
DEFAULT_DB = os.environ.get("WIKI_DEFAULT_DB", "wiki")
DEFAULT_DB_DIR = Path(os.environ.get("WIKI_DB_DIR", ".wiki")).resolve()
BACKEND_TIMEOUT = httpx.Timeout(90.0, connect=5.0)
BACKEND_READY_TIMEOUT_SECONDS = float(
    os.environ.get("MCP_BACKEND_READY_TIMEOUT_SECONDS", "90")
)
BACKEND_READY_POLL_SECONDS = 0.25


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

def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_wiki_allowlist() -> frozenset[str]:
    values = os.environ.get("MCP_ALLOWED_WIKIS", "").split(",")
    return frozenset(value.strip() for value in values if value.strip())


class WikiRoutingApp:
    """Route one FastMCP ASGI app by wiki name without global request state."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        prefix: str = DEFAULT_PREFIX,
        backend_origin: str = DEFAULT_BACKEND_ORIGIN,
        default_db: str | None = DEFAULT_DB,
        db_dir: Path = DEFAULT_DB_DIR,
        allowed_wikis: frozenset[str] | None = None,
        allow_new_wikis: bool = False,
    ) -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")
        self.backend_origin = backend_origin.rstrip("/")
        self.default_db = default_db
        self.db_dir = db_dir.resolve()
        self.allowed_wikis = allowed_wikis or frozenset()
        self.allow_new_wikis = allow_new_wikis

    def _db_from_path(self, path: str) -> str | None:
        if path.rstrip("/") == "/mcp" and self.default_db:
            return self.default_db

        route_prefix = f"{self.prefix}/" if self.prefix else "/"
        if not path.startswith(route_prefix):
            return None

        remainder = path[len(route_prefix) :].strip("/")
        parts = remainder.split("/")
        if len(parts) != 2 or parts[1] != "mcp":
            return None
        return parts[0]

    def _is_allowed(self, db: str) -> bool:
        if not _DB_RE.fullmatch(db):
            return False
        if self.allowed_wikis and db not in self.allowed_wikis:
            return False
        if self.allow_new_wikis:
            return True
        return (self.db_dir / f"{db}.sqlite").is_file()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        db = self._db_from_path(scope.get("path", ""))
        if not db or not self._is_allowed(db):
            response = JSONResponse({"detail": "unknown wiki"}, status_code=404)
            await response(scope, receive, send)
            return

        routed_scope = dict(scope)
        state = dict(scope.get("state") or {})
        state.update(
            {
                "wiki_db": db,
                "wiki_backend_origin": self.backend_origin,
                "wiki_backend_prefix": self.prefix,
            }
        )
        routed_scope["state"] = state
        routed_scope["path"] = "/mcp"
        routed_scope["raw_path"] = b"/mcp"
        routed_scope["root_path"] = f"{self.prefix}/{db}"
        await self.app(routed_scope, receive, send)


class BackendRequestError(RuntimeError):
    def __init__(self, status_code: int, detail: str, code: str = "backend_error"):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code


def _backend_error(response: httpx.Response) -> tuple[str, str]:
    try:
        raw_error = response.json()
    except ValueError:
        raw_error = {}
    error = raw_error if isinstance(raw_error, dict) else {}
    nested_detail = error.get("detail")
    if isinstance(nested_detail, dict):
        detail = str(
            nested_detail.get("message")
            or nested_detail.get("detail")
            or response.reason_phrase
        )
        code = str(nested_detail.get("code") or "backend_error")
    else:
        detail = str(nested_detail or response.text or response.reason_phrase)
        code = str(error.get("code") or "backend_error")
    return detail, code


async def _wait_for_backend_ready(client: httpx.AsyncClient, ready_url: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BACKEND_READY_TIMEOUT_SECONDS

    while loop.time() < deadline:
        response = await client.request("GET", ready_url)
        if response.is_error:
            detail, code = _backend_error(response)
            raise BackendRequestError(response.status_code, detail, code)

        try:
            status = response.json()
        except ValueError as exc:
            raise RuntimeError("LLM-Wiki backend returned invalid readiness JSON") from exc
        if not isinstance(status, dict):
            raise RuntimeError("LLM-Wiki backend returned invalid readiness data")
        if status.get("ready"):
            return
        if status.get("stage") == "failed":
            raise BackendRequestError(
                503,
                str(status.get("error") or "wiki bootstrap failed"),
                "bootstrap_failed",
            )
        await asyncio.sleep(BACKEND_READY_POLL_SECONDS)

    raise BackendRequestError(
        503,
        "wiki backend did not become ready before the MCP timeout",
        "not_ready",
    )


def _request_backend() -> tuple[str, str, str]:
    request = get_http_request()
    db = getattr(request.state, "wiki_db", None)
    origin = getattr(request.state, "wiki_backend_origin", None)
    prefix = getattr(request.state, "wiki_backend_prefix", None)
    if not db or not origin or prefix is None:
        raise RuntimeError("missing wiki route context")
    return db, origin, prefix


async def _backend_json(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    allow_not_found: bool = False,
) -> Any:
    db, origin, prefix = _request_backend()
    url = f"{origin}{prefix}/{db}{path}"
    ready_url = f"{origin}{prefix}/{db}/api/ready"

    try:
        async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
            response = await client.request(method, url, params=params, json=payload)
            if response.status_code == 503:
                _detail, code = _backend_error(response)
                if code == "not_ready":
                    await _wait_for_backend_ready(client, ready_url)
                    response = await client.request(
                        method, url, params=params, json=payload
                    )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"LLM-Wiki backend unavailable: {exc}") from exc

    if allow_not_found and response.status_code == 404:
        return None

    if response.is_error:
        detail, code = _backend_error(response)
        raise BackendRequestError(response.status_code, detail, code)

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("LLM-Wiki backend returned invalid JSON") from exc


async def _backend_links(
    node_id: str,
    *,
    label: str | None,
    direction: str,
    limit: int,
) -> list[tuple[Any, Any]]:
    params: dict[str, Any] = {
        "direction": direction,
        "limit": limit,
        "compact": True,
    }
    if label:
        params["label"] = label
    items = await _backend_json(
        "GET", f"/api/node/{quote(node_id, safe='')}/links", params=params
    )
    if not isinstance(items, list):
        raise RuntimeError("LLM-Wiki backend returned invalid link data")
    return [
        (item.get("edge"), item.get("node"))
        for item in items[:limit]
        if isinstance(item, dict)
    ]


# =============================================================================
# JSON serialization helpers
# =============================================================================

def _jsonable(obj: Any) -> Any:
    """
    Convert dataclasses and pydantic models into JSON-safe values.
    """
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

    if hasattr(obj, "model_dump"):
        return _jsonable(obj.model_dump())

    if hasattr(obj, "dict"):
        return _jsonable(obj.dict())

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
        "Request-scoped LLM-Wiki graph research server. The wiki is selected by the "
        "database name in the MCP endpoint URL and must not be changed with tool arguments. "
        "The main agent orchestrates: use hybrid_search only for bounded initial seeding, "
        "then delegate every read_nodes call, explore_links call, and later gap search to "
        "route subagents. Subagents return compact cited reports; they do not synthesize, "
        "spawn agents, or write. Explore every selected material route before synthesis. "
        "After producing reusable, evidence-grounded knowledge, the main agent calls "
        "queue_agent_note exactly once. "
        "That tool only queues the note; do not poll or wait for assimilation. "
        "Tool responses are Markdown, not raw JSON. "
        "Node bodies are returned in full; embedded base64 image payloads are removed before output. "
        "If image descriptions exist, they are preserved as semantic text. "
        "Do not save raw transcripts, secrets, chain-of-thought, or unsupported speculation."
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
    - After this tool, the main agent delegates the most relevant node IDs to route
      subagents, which call read_nodes and traverse their assigned regions.
    - Use node IDs exactly as returned.
    """
    query = query.strip()

    if not query:
        return (
            "# Hybrid Search Results\n\n"
            "**Error:** empty query.\n"
        )

    limit = max(1, int(limit or 10))

    results = await _backend_json(
        "GET", "/api/search", params={"q": query, "limit": limit, "compact": True}
    )
    if not isinstance(results, list):
        raise RuntimeError("LLM-Wiki backend returned invalid search data")

    lines: list[str] = [
        "# Hybrid Search Results",
        "",
        f"- **Query:** `{query}`",
        f"- **Requested result count:** `{limit}`",
        "",
        "## How to use these results",
        "",
        "The main agent assigns relevant node IDs to route subagents. Each subagent calls `read_nodes` and explores its assigned region.",
        "",
    ]

    output_count = 0

    for rank, item in enumerate(results, start=1):
        dumped = _jsonable(item)

        if isinstance(dumped, dict) and "node" in dumped:
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
            "Delegate one or more Node IDs above to route subagents for `read_nodes` and graph traversal.",
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
    - If more graph context is needed, the route subagent calls explore_links.
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
            node = await _backend_json(
                "GET",
                f"/api/node/{quote(node_id, safe='')}",
                allow_not_found=True,
            )
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
                pairs = await _backend_links(
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
            "The route subagent should read promising Neighbor Node IDs or call `explore_links` until its assigned frontier is closed or bounded.",
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
    - Then the route subagent calls read_nodes on those IDs to inspect full source content.
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
            pairs = await _backend_links(
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
            "The route subagent calls `read_nodes` on the most relevant Neighbor Node IDs.",
        ]
    )

    return "\n".join(lines)


async def _queue_agent_note(
    body: str,
    source_node_ids: list[str] | None = None,
    question: str | None = None,
) -> str:
    clean_body = sanitize_markdown_for_text_llm(str(body or "")).strip()
    if not clean_body:
        raise ValueError("agent note body must not be empty")

    cited_ids = _normalize_node_ids(source_node_ids or [])
    clean_question = " ".join(str(question or "").split()) or None
    result = await _backend_json(
        "POST",
        "/api/exogenous",
        payload={
            "body": clean_body,
            "source_node_ids": cited_ids,
            "origin": "agent:mcp",
            "question": clean_question,
        },
    )
    if not isinstance(result, dict) or not result.get("id"):
        raise RuntimeError("LLM-Wiki backend returned invalid write-job data")

    db, _origin, _prefix = _request_backend()
    lines = [
        "# Agent Note Queued",
        "",
        f"- **Wiki:** `{db}`",
        f"- **Job ID:** `{result['id']}`",
        f"- **Status:** `{result.get('status', 'queued')}`",
        f"- **Cited node count:** `{len(cited_ids)}`",
    ]
    if result.get("position") is not None:
        lines.append(f"- **Queue position:** `{result['position']}`")
    lines.extend(
        [
            "",
            "The backend accepted the write. Continue immediately; do not poll or wait for assimilation.",
        ]
    )
    return "\n".join(lines)


@mcp.tool()
async def queue_agent_note(
    body: str,
    source_node_ids: list[str] | None = None,
    question: str | None = None,
) -> str:
    """Queue one durable, evidence-grounded agent note in the active wiki.

    Call this once after completing useful research. The backend serializes the
    addition with every other graph write and assimilates it in the background.
    This tool returns when the job is accepted; do not poll or wait for completion.

    Input:
    - body: The concise synthesized knowledge to preserve as Markdown.
    - source_node_ids: Exact node IDs that support the note.
    - question: The original question, used as the note title when provided.

    Do not submit raw transcripts, secrets, chain-of-thought, unsupported claims,
    or partial subagent reports.
    """
    return await _queue_agent_note(body, source_node_ids, question)


# =============================================================================
# CLI
# =============================================================================

def create_app(
    *,
    prefix: str = DEFAULT_PREFIX,
    backend_origin: str = DEFAULT_BACKEND_ORIGIN,
    default_db: str | None = DEFAULT_DB,
    db_dir: Path = DEFAULT_DB_DIR,
    allowed_wikis: frozenset[str] | None = None,
    allow_new_wikis: bool | None = None,
) -> ASGIApp:
    """Build the multi-wiki ASGI wrapper around FastMCP's static route."""
    inner = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    return WikiRoutingApp(
        inner,
        prefix=prefix,
        backend_origin=backend_origin,
        default_db=default_db,
        db_dir=db_dir,
        allowed_wikis=(
            _env_wiki_allowlist() if allowed_wikis is None else allowed_wikis
        ),
        allow_new_wikis=(
            _env_flag("MCP_ALLOW_NEW_WIKIS")
            if allow_new_wikis is None
            else allow_new_wikis
        ),
    )


# ASGI entrypoint for `uvicorn mcp_server:app`.
app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM-Wiki MCP server")

    parser.add_argument(
        "--wiki",
        default=os.environ.get("MCP_WIKI", DEFAULT_DB),
        help="Default wiki for the legacy /mcp route. Named routes ignore this.",
    )

    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help="Route prefix shared with app.py. Default: /llm-wiki",
    )

    parser.add_argument(
        "--backend-origin",
        default=DEFAULT_BACKEND_ORIGIN,
        help="Trusted app.py origin. Default: http://127.0.0.1:8000",
    )

    parser.add_argument(
        "--db-dir",
        default=str(DEFAULT_DB_DIR),
        help="Directory containing <wiki>.sqlite files.",
    )

    parser.add_argument(
        "--allow-new-wikis",
        action="store_true",
        default=_env_flag("MCP_ALLOW_NEW_WIKIS"),
        help="Allow routes for sqlite names that do not exist yet.",
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

    log.info("Starting LLM-Wiki MCP server")
    log.info("route=%s/{wiki}/mcp", args.prefix)
    log.info("legacy_default_wiki=%s", args.wiki)
    log.info("backend=%s", args.backend_origin)
    log.info("bind=%s:%s", args.host, args.port)

    uvicorn.run(
        create_app(
            prefix=args.prefix,
            backend_origin=args.backend_origin,
            default_db=args.wiki,
            db_dir=Path(args.db_dir),
            allow_new_wikis=args.allow_new_wikis,
        ),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
