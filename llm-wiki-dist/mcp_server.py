# region Imports

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

# endregion Imports

# region Global vars/Configs

log = logging.getLogger("llm_wiki_mcp")

_DB_RE = re.compile(r"[A-Za-z0-9_-]+")
DEFAULT_PREFIX = os.environ.get("WIKI_PREFIX", "/llm-wiki").rstrip("/")
DEFAULT_BACKEND_ORIGIN = os.environ.get(
    "MCP_BACKEND_ORIGIN", "http://127.0.0.1:8000" # TODO Make this sync with backend auto
).rstrip("/")
DEFAULT_DB = os.environ.get("WIKI_DEFAULT_DB", "wiki")
DEFAULT_DB_DIR = Path(os.environ.get("WIKI_DB_DIR", ".wiki")).resolve()

BACKEND_TIMEOUT = httpx.Timeout(90.0, connect=5.0)
BACKEND_READY_TIMEOUT_SECONDS = float(
    os.environ.get("MCP_BACKEND_READY_TIMEOUT_SECONDS", "90")
)
BACKEND_READY_POLL_SECONDS = 1

# endregion Global vars/Configs

# region Utils

# Image cleaner regexes 
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

# Image cleaner helpers
# TODO: I made a mistake of giving it a prompt which contained a lot of helper, so it made a lot of utils that are prolly
# Not needed, so i need to remove some useless ones

# Return True if the text contains at least one image-unit block.
def has_image_units(text: str) -> bool:
    if not isinstance(text, str) or not text:
        return False

    return _IMAGE_UNIT_RE.search(text) is not None

# Return the number of image-unit blocks
def count_image_units(text: str) -> int:
    if not isinstance(text, str) or not text:
        return 0

    return len(list(_IMAGE_UNIT_RE.finditer(text)))

# Remove embedded image media payloads from image-unit blocks
def strip_image_media(text: str) -> str:

    if not isinstance(text, str) or not text:
        return text

    def replace_image_unit(match: re.Match[str]) -> str:
        body = match.group("body")

        description = _IMAGE_DESCRIPTION_RE.search(body)
        if description:
            return description.group("description").strip()

        return _IMAGE_MEDIA_RE.sub("", body).strip()

    return _IMAGE_UNIT_RE.sub(replace_image_unit, text).strip()

# Replace image-media blocks with a marker while preserving image-unit structure
def replace_image_media_with_marker(
    text: str,
    marker: str = "[image omitted]",
) -> str:

    if not isinstance(text, str) or not text:
        return text

    return _IMAGE_MEDIA_RE.sub(marker, text).strip()

# Return non-empty image-description values from image-unit blocks
def extract_image_descriptions(text: str) -> list[str]:
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

# Return data:image/... URLs from image-media blocks
def extract_image_data_urls(text: str) -> list[str]:
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

# Return raw base64 payloads from image data URLs
def extract_image_base64(text: str) -> list[str]:
    values: list[str] = []

    for data_url in extract_image_data_urls(text):
        if "," in data_url:
            values.append(data_url.split(",", 1)[1])

    return values

# Remove embedded image payloads before sending Markdown to a text-only LLM
# - Removes base64 image data from image-unit/image-media blocks.
# - If image-description exists, keeps that semantic description.
# - If image-description is empty/missing, removes image-media payload and
#     keeps any remaining non-media text.
# - Also handles standalone image-media blocks and standalone data:image URLs
#     defensively, in case malformed content exists outside image-unit blocks.
def sanitize_markdown_for_text_llm(text: str) -> str:

    if not isinstance(text, str) or not text:
        return text

    cleaned = strip_image_media(text)

    # Defensive cleanup for image-media blocks that might exist outside image-unit.
    cleaned = _IMAGE_MEDIA_RE.sub("[image omitted]", cleaned)

    # Defensive cleanup for standalone data:image URLs.
    cleaned = _DATA_IMAGE_URL_RE.sub("data:image/[base64 payload omitted]", cleaned)

    return cleaned.strip()


# Reads an environment variable and returns a boolean (if its activated or not)
def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

# Get a list of allowed wiki names and make it a set
def _env_wiki_allowlist() -> frozenset[str]:
    values = os.environ.get("MCP_ALLOWED_WIKIS", "").split(",")
    return frozenset(value.strip() for value in values if value.strip())

# endregion Utils

# region Routing

# ASGI = Asynchronous Server Gateway Interface.

class WikiRoutingApp:

    # Stores the wrapped ASGI app and routing/security config, such as URL prefix,
    # DB directory, allowed wiki names, and whether unknown/new wikis are accepted.
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

    # Extracts the wiki/database name from the request URL path.
    # Example: "/wiki/cats/mcp" -> "cats", "/mcp" -> default_db, invalid paths -> None.
    def _db_from_path(self, path: str) -> str | None:
        if path.rstrip("/") == "/mcp" and self.default_db:
            return self.default_db

        route_prefix = f"{self.prefix}/" if self.prefix else "/"
        if not path.startswith(route_prefix):
            return None

        # Remove prefix, then expect "{db}/mcp".
        remainder = path[len(route_prefix) :].strip("/")
        parts = remainder.split("/")

        if len(parts) != 2 or parts[1] != "mcp":
            return None

        return parts[0]

    # Validates whether the extracted DB/wiki name is safe and permitted.
    # It checks name format, allow-list, allow_new_wikis, or whether "db.sqlite" exists.
    def _is_allowed(self, db: str) -> bool:
        # Reject unsafe DB names before using them in a file path.
        if not _DB_RE.fullmatch(db):
            return False

        if self.allowed_wikis and db not in self.allowed_wikis:
            return False

        if self.allow_new_wikis:
            return True

        return (self.db_dir / f"{db}.sqlite").is_file()

    # Main ASGI entry point: receives the request, finds the wiki DB, rejects invalid ones,
    # rewrites the path to "/mcp", adds wiki info to scope["state"], then forwards to self.app.
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        db = self._db_from_path(scope.get("path", ""))
        if not db or not self._is_allowed(db):
            response = JSONResponse({"detail": "unknown wiki"}, status_code=404)
            await response(scope, receive, send)
            return

        # Copy scope so we can modify routing info without changing the original.
        routed_scope = dict(scope)

        # Add wiki-specific data for the inner app to read.
        state = dict(scope.get("state") or {})
        state.update(
            {
                "wiki_db": db,
                "wiki_backend_origin": self.backend_origin,
                "wiki_backend_prefix": self.prefix,
            }
        )
        routed_scope["state"] = state

        # Make the inner app always handle "/mcp".
        routed_scope["path"] = "/mcp"
        routed_scope["raw_path"] = b"/mcp"

        # Preserve the public mount path, e.g. "/wiki/cats".
        routed_scope["root_path"] = f"{self.prefix}/{db}"

        await self.app(routed_scope, receive, send)

# Custom error for backend failures, since this is an MCP server, not main backend, we are defining a "backend error"
# meaning the error is related to interaction with HTTP Backend conncetion, not within this server
class BackendRequestError(RuntimeError):
    def __init__(self, status_code: int, detail: str, code: str = "backend_error"):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code

# Extracts a clean error message and error code from a backend HTTP response.
# This function's return will be used to make above BackendRequestError
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

# Polls the backend readiness endpoint until the wiki is ready, the backend has a wiki initializing logic
# if the wiki didnt exist already, it will wait for the backend to create one
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

# This connects to above WikiRoutingApp, which inserted the 3 params this is extracting
def _request_backend() -> tuple[str, str, str]:
    request = get_http_request() # Gets routing info from the current request state
    db = getattr(request.state, "wiki_db", None)
    origin = getattr(request.state, "wiki_backend_origin", None)
    prefix = getattr(request.state, "wiki_backend_prefix", None)
    if not db or not origin or prefix is None:
        raise RuntimeError("missing wiki route context")
    return db, origin, prefix

# This is the main helper that calls the backend API and returns JSON
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

# Fetches links for a node from backend
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


# endregion Routing

# region Graph utils

# Convert dataclasses and pydantic models into JSON-safe values
def _jsonable(obj: Any) -> Any:

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

# Accept a single node ID or a list of node IDs, Remove blank values and duplicates while preserving order
def _normalize_node_ids(node_ids: str | list[str]) -> list[str]:
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

# stringify anything
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

# Extract a useful relation label from an edge object
def _edge_label(edge: Any) -> str:
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

# Format one compact search result, dont dump node metadata to llm context
def _format_ranked_search_result(rank: int, node: Any, score: Any = None) -> str:

    data = _jsonable(node)

    if not isinstance(data, dict):
        return (
            f"### {rank}. 不明なノード\n\n"
            f"- **生データ:** `{data}`\n"
        )

    node_id = _node_id_from_data(data)
    title = _node_title_from_data(data)
    summary = _node_summary_from_data(data)
    node_type = _node_type_from_data(data)

    lines = [
        f"### {rank}. {title}",
        "",
        f"- **ノードID:** `{node_id}`",
    ]

    if score is not None:
        lines.append(f"- **スコア:** `{score}`")

    if node_type:
        lines.append(f"- **種別:** `{node_type}`")

    lines.append(f"- **概要:** {summary}")
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
            f"{index}. 不明な隣接ノード\n"
            f"   - **起点ノードID:** `{seed_node_id}`\n"
            f"   - **関係:** `{label}`\n"
            f"   - **生データ:** `{data}`\n"
        )

    node_id = _node_id_from_data(data)
    title = _node_title_from_data(data)
    summary = _node_summary_from_data(data)
    node_type = _node_type_from_data(data)

    lines = [
        f"{index}. **{title}**",
        f"   - **隣接ノードID:** `{node_id}`",
        f"   - **関係:** `{label}`",
    ]

    if node_type:
        lines.append(f"   - **種別:** `{node_type}`")

    lines.append(f"   - **概要:** {summary}")

    return "\n".join(lines) + "\n"


def _compact_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"

# endregion Graph utils

# region MCP and its tools

mcp = FastMCP(
    name="llm-wiki-graph",
    instructions=(
        "リクエスト単位で動作する LLM-Wiki グラフ調査サーバーです。Wiki は "
        "MCP エンドポイントURL内のデータベース名によって選択され、ツール引数で変更してはなりません。"
        "メインエージェントが全体を統括します。hybrid_search は、範囲を限定した初期シード取得にのみ使用し、"
        "その後の read_nodes 呼び出し、explore_links 呼び出し、および後続の不足情報検索は、"
        "すべてルート担当サブエージェントに委任してください。"
        "サブエージェントは、引用付きのコンパクトなレポートを返します。サブエージェントは統合を行わず、"
        "エージェントを生成せず、書き込みも行いません。"
        "統合前に、選択されたすべての資料ルートを探索してください。"
        "再利用可能で、証拠に基づいた知識を生成した後、メインエージェントは "
        "queue_agent_note を正確に1回呼び出します。"
        "このツールはノートをキューに入れるだけです。同化処理をポーリングしたり待機したりしないでください。"
        "ツール応答は Markdown であり、生の JSON ではありません。"
        "ノード本文は全文返されます。ただし、埋め込まれた base64 画像ペイロードは出力前に削除されます。"
        "画像説明が存在する場合、それらは意味的テキストとして保持されます。"
        "生のトランスクリプト、秘密情報、思考過程、または根拠のない推測を保存しないでください。"
    ),
)


@mcp.tool()
async def hybrid_search(query: str, limit: int = 10) -> str:
    """
    Wikiを検索し、関連度順にランク付けされた結果を返します。

    ユーザーが未知のトピック、API、エラーコード、設計ルール、挙動、
    日本語マニュアル上の概念、サブシステム、または実装詳細について質問した場合は、
    最初にこのツールを使用してください。

    既に答えを知っていると思う場合でも、回答前の確認としてこのツールを使用してください。
    ユーザーが使った語句は、このWiki固有のドメイン知識における特別な意味を持つ可能性があります。
    意味が不明確な場合は、誤った文脈で回答しないよう、まず短く検索してください。

    入力:
    - query:
        自然言語の検索クエリです。日本語と英語のどちらも使用できます。
        例:
        - "ファイルのデッドロック"
        - "E_MFS_ETIMEOUT"
        - "category lock deadlock"

    - limit:
        返す検索結果の件数です。
        デフォルト: 10。
        これは結果件数のみを制御します。本文やテキストの文字数制限ではありません。

    応答形式:
    - 結果は関連度またはランキングが高い順に並びます。
    - 各結果には以下が含まれます:
        - 順位
        - ノードタイトル
        - ノードID
        - スコア、利用可能な場合
        - ノード種別、利用可能な場合
        - ノード概要
    """
    query = query.strip()

    if not query:
        return (
            "# ハイブリッド検索結果\n\n"
            "**エラー:** クエリが空です。\n"
        )

    limit = max(1, int(limit or 10))

    results = await _backend_json(
        "GET", "/api/search", params={"q": query, "limit": limit, "compact": True}
    )
    if not isinstance(results, list):
        raise RuntimeError("LLM-Wikiバックエンドが無効な検索データを返しました")

    lines: list[str] = [
        "# ハイブリッド検索結果",
        "",
        f"- **検索クエリ:** `{query}`",
        f"- **要求された結果件数:** `{limit}`",
        "",
        "## この検索結果の使い方",
        "",
        "メインエージェントは、関連するノードIDをルート担当サブエージェントに割り当てます。各サブエージェントは `read_nodes` を呼び出し、割り当てられた領域を探索します。",
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
        lines.append("一致するノードは見つかりませんでした。")

    lines.extend(
        [
            "",
            "## 次のステップ",
            "",
            "上記の1つ以上のノードIDをルート担当サブエージェントに委任し、`read_nodes` とグラフ探索を実行してください。",
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
    正確なノードIDを指定して、1つ以上のWikiノードを読み取ります。

    hybrid_search で候補ノードを見つけた後、選択したノードの実際の
    ソース内容、マニュアル本文、または詳細情報を確認するために使用してください。

    入力:
    - node_ids:
        単一のノードID文字列、またはノードID文字列のリストです。
        例:
        - "node:moove-file-management-us:2fa49de66669"
        - ["node:a", "node:b"]

    - neighbor_limit:
        要求した各ノードごとに表示する隣接リンクノードの件数です。
        デフォルト: 10。
        これは関連ノードの表示件数のみを制御します。
        要求したノード本文を切り詰めたり制限したりするものではありません。
        要求ノードの全文本文だけが必要な場合は 0 を指定してください。

    - direction:
        対象ノードの周辺で表示するグラフリンクの方向です。
        指定可能な値:
        - "incoming": このノードを指しているノード
        - "outgoing": このノードが指しているノード
        - "both": incoming と outgoing の両方
        デフォルト: "both"。

    応答形式:
    - 生のJSONではなく Markdown を返します。
    - 要求された各ノードについて、以下を含みます:
        - ノードID
        - タイトル
        - 種別、利用可能な場合
        - 概要
        - ソース情報、利用可能な場合
        - 本文全文
        - 画像クリーンアップ統計
        - 隣接ノード:
            - 隣接ノードID
            - 関係ラベル
            - 隣接ノードのタイトル
            - 隣接ノードの概要

    LLMでの利用方法:
    - ユーザーへの回答では、Body セクションを主要な根拠として使用してください。
    - Neighboring Nodes は、次に読むべきノードを判断するために使用してください。
    - さらにグラフ文脈が必要な場合、ルート担当サブエージェントが explore_links を呼び出します。
    """
    ids = _normalize_node_ids(node_ids)

    if not ids:
        return (
            "# ノード読み取り\n\n"
            "**エラー:** ノードIDが指定されていません。\n"
        )

    normalized_direction = direction.lower().strip()
    if normalized_direction not in {"incoming", "outgoing", "both"}:
        return (
            "# ノード読み取り\n\n"
            "**エラー:** direction は `incoming`、`outgoing`、または `both` のいずれかである必要があります。\n"
        )

    neighbor_limit = max(0, int(neighbor_limit or 0))

    lines: list[str] = [
        "# ノード読み取り",
        "",
        f"- **要求されたノード数:** `{len(ids)}`",
        f"- **隣接ノードの方向:** `{normalized_direction}`",
        f"- **ノードごとの隣接ノード数:** `{neighbor_limit}`",
        "- **本文の扱い:** 全文を返します。文字数による切り詰めはありません",
        "- **画像の扱い:** 埋め込まれた base64 画像ペイロードは削除し、画像説明は保持します",
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
                    f"**ノード読み取りエラー:** `{_compact_error(exc)}`",
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
                f"- **ノードID:** `{actual_id}`",
            ]
        )

        if node_type:
            lines.append(f"- **種別:** `{node_type}`")

        lines.append(f"- **概要:** {summary}")

        if source:
            lines.append(f"- **ソース:** {source}")

        lines.extend(
            [
                "- **本文の返却:** 全文を返します。文字数による切り詰めはありません",
                f"- **検出された画像ユニット数:** `{image_unit_count}`",
                f"- **保持された画像説明数:** `{image_description_count}`",
                f"- **削除された埋め込み画像ペイロード数:** `{image_payload_count}`",
                "",
                "### 本文",
                "",
                body if body else "（本文テキストなし）",
                "",
            ]
        )

        if neighbor_limit > 0:
            lines.extend(
                [
                    "### 隣接ノード",
                    "",
                    "このノードに接続されている、次に読む候補となるノードです。",
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
                            "（隣接ノードは見つかりませんでした）",
                            "",
                        ]
                    )
                else:
                    for neighbor_index, pair in enumerate(pairs, start=1):
                        try:
                            edge, neighbor_node = pair
                        except Exception:
                            lines.append(
                                f"{neighbor_index}. 隣接ノードのペアを解析できませんでした: `{pair}`"
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
                        f"`{actual_id}` の隣接ノードを取得できませんでした。",
                        f"- **エラー:** `{_compact_error(exc)}`",
                        "",
                    ]
                )

    if missing:
        lines.extend(
            [
                "## 見つからなかったノード",
                "",
                *[f"- `{node_id}`" for node_id in missing],
                "",
            ]
        )

    lines.extend(
        [
            "## 次のステップ",
            "",
            "ルート担当サブエージェントは、有望な隣接ノードIDを読み取るか、割り当てられた探索範囲が閉じる、または境界付けられるまで `explore_links` を呼び出してください。",
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
    1つ以上の起点ノードからグラフリンクを探索します。

    次の場合にこのツールを使用してください:
    - read_nodes の結果に有用なノードがあり、関連概念を確認したい場合。
    - ノードの上流または下流にある情報を理解したい場合。
    - 次に読むノードを選びたい場合。
    - 本文全文を読まずに、グラフ上の文脈だけを確認したい場合。

    入力:
    - node_ids:
        単一の起点ノードID文字列、または起点ノードID文字列のリストです。

    - direction:
        表示するリンク方向です。
        指定可能な値:
        - "incoming": 起点ノードを指しているノード
        - "outgoing": 起点ノードが指しているノード
        - "both": incoming と outgoing の両方
        デフォルト: "both"。

    - label:
        任意の関係ラベルまたはエッジラベルのフィルタです。
        すべての関係ラベルを表示する場合は、null を指定するか、この引数を省略してください。

    - limit:
        すべての起点ノードを合計して返すリンク数です。
        デフォルト: 30。
        これはグラフの展開数のみを制御します。
        本文やテキストの文字数制限ではありません。

    応答形式:
    - 生のJSONではなく Markdown を返します。
    - 各起点ノードごとに、リンクされた隣接ノードを一覧表示します。
    - 各隣接ノードには以下が含まれます:
        - 起点ノードID
        - 関係ラベルまたはエッジラベル
        - 隣接ノードID
        - 隣接ノードのタイトル
        - 隣接ノードの種別、利用可能な場合
        - 隣接ノードの概要

    LLMでの利用方法:
    - この結果をナビゲーション用のマップとして使用してください。
    - 有望な隣接ノードIDを選んでください。
    - その後、ルート担当サブエージェントがそれらのIDに対して read_nodes を呼び出し、
      ソース本文を詳しく確認します。
    """
    ids = _normalize_node_ids(node_ids)

    if not ids:
        return (
            "# リンク探索\n\n"
            "**エラー:** ノードIDが指定されていません。\n"
        )

    normalized_direction = direction.lower().strip()

    if normalized_direction not in {"incoming", "outgoing", "both"}:
        return (
            "# リンク探索\n\n"
            "**エラー:** direction は `incoming`、`outgoing`、または `both` のいずれかである必要があります。\n"
        )

    limit = max(1, int(limit or 30))

    lines: list[str] = [
        "# リンク探索",
        "",
        f"- **起点ノード数:** `{len(ids)}`",
        f"- **方向:** `{normalized_direction}`",
        f"- **ラベルフィルタ:** `{label if label else 'なし'}`",
        f"- **要求された合計リンク数:** `{limit}`",
        "",
        "## この出力の使い方",
        "",
        "以下の有望な隣接ノードIDに対して `read_nodes` を使用し、ソース本文を詳しく確認してください。",
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
                f"## 起点 {seed_index}: `{node_id}`",
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
                    f"`{node_id}` のリンクを探索できませんでした。",
                    f"- **エラー:** `{_compact_error(exc)}`",
                    "",
                ]
            )
            continue

        if not pairs:
            lines.extend(
                [
                    "（リンクは見つかりませんでした）",
                    "",
                ]
            )
            continue

        for local_index, pair in enumerate(pairs, start=1):
            try:
                edge, neighbor_node = pair
            except Exception:
                lines.append(
                    f"{local_index}. リンクペアを解析できませんでした: `{pair}`"
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
            "## 概要",
            "",
            f"- **返されたリンク数:** `{total_count}`",
            f"- **失敗した起点ノード数:** `{len(failed)}`",
            "",
        ]
    )

    if failed:
        lines.append("### 失敗した起点ノード")
        lines.append("")
        for item in failed:
            lines.append(f"- `{item['node_id']}`: {item['error']}")
        lines.append("")

    lines.extend(
        [
            "## 次のステップ",
            "",
            "ルート担当サブエージェントは、最も関連性の高い隣接ノードIDに対して `read_nodes` を呼び出してください。",
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
        raise ValueError("エージェントノートの本文は空にできません")

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
        raise RuntimeError("LLM-Wikiバックエンドが無効な書き込みジョブデータを返しました")

    db, _origin, _prefix = _request_backend()
    lines = [
        "# エージェントノートをキューに追加しました",
        "",
        f"- **Wiki:** `{db}`",
        f"- **ジョブID:** `{result['id']}`",
        f"- **ステータス:** `{result.get('status', 'queued')}`",
        f"- **引用ノード数:** `{len(cited_ids)}`",
    ]
    if result.get("position") is not None:
        lines.append(f"- **キュー内の位置:** `{result['position']}`")
    lines.extend(
        [
            "",
            "バックエンドは書き込みを受け付けました。すぐに続行してください。同化処理をポーリングしたり待機したりしないでください。",
        ]
    )
    return "\n".join(lines)


@mcp.tool()
async def queue_agent_note(
    body: str,
    source_node_ids: list[str] | None = None,
    question: str | None = None,
) -> str:
    """現在のWikiに、永続的で証拠に基づいたエージェントノートを1件キューに追加します。

    有用な調査が完了した後に、このツールを1回だけ呼び出してください。
    バックエンドは、この追加処理を他のすべてのグラフ書き込みと直列化し、
    バックグラウンドで同化します。
    このツールはジョブが受理された時点で戻ります。
    完了をポーリングしたり待機したりしないでください。

    入力:
    - body:
        Markdown形式で保存する、簡潔に統合された知識です。
        調査結果に基づき、再利用可能な形で記述してください。

    - source_node_ids:
        ノートの根拠となる正確なノードIDのリストです。
        read_nodes などで確認した出典ノードを指定してください。

    - question:
        元の質問です。
        指定された場合、ノートのタイトルとして使用されます。

    以下は送信しないでください:
    - 生の会話ログ
    - 秘密情報
    - 思考過程
    - 根拠のない主張
    - 未完成の部分的なサブエージェント報告
    """
    return await _queue_agent_note(body, source_node_ids, question)

# endregion MCP and its tools

# region App runner

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

# endregion App runner
