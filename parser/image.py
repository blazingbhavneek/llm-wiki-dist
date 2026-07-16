#!/usr/bin/env python3
"""Image description pipelines.

Replaces images in MinerU Markdown output (process_markdown_folder) or in a wiki
SQLite database (process_sqlite_database / CLI) with staged reconstructions:

    classify -> transcribe -> [entity diagram] nodes -> edges -> verify -> Mermaid
                           -> [everything else] grounded plain description

Images are processed sequentially in document order; each image receives the
preceding document blocks as context (CONTEXT_BLOCKS blocks, trimmed oldest-first
to CONTEXT_MAX_TOKENS; 0 disables context).

parser/server.py mutates the module globals below and calls
process_markdown_folder(); keep their names stable.
"""

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

from image_core import mermaid, stages
from image_core.config import ImageConfig
# get_response_text / normalize_invoke_url are re-exported for server.py; the
# "as" form marks them as public re-exports for linters.
from image_core.llm import Llm
from image_core.llm import get_response_text as get_response_text
from image_core.llm import normalize_invoke_url as normalize_invoke_url

load_dotenv()

# =========================
# Config globals (server.py sets these directly)
# =========================

MARKDOWN_FOLDER = os.environ.get("MARKDOWN_FOLDER", "./mineru")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://10.160.144.101:51029/v1")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "local")
OPENAI_MODEL = os.environ.get("WIKI_MODEL", "gemma-4-31B")

TEMPERATURE = 0.7
TOP_P = 0.95
LLM_THINKING_TIMEOUT_SECONDS = int(os.environ.get("LLM_THINKING_TIMEOUT_SECONDS", "600"))
LLM_FALLBACK_TIMEOUT_SECONDS = int(os.environ.get("LLM_FALLBACK_TIMEOUT_SECONDS", "600"))

FILE_CONCURRENCY = 5

ENABLE_MERMAID_DIAGRAMS = True
VALIDATE_MERMAID = True
MERMAID_CLI_BIN = "mmdc"
MERMAID_PUPPETEER_CONFIG_FILE = os.environ.get(
    "PUPPETEER_CONFIG_PATH", "./puppeteer-config.json"
)
MERMAID_PARSE_TIMEOUT_SECONDS = 30

# Preceding-document context per image. CONTEXT_MAX_TOKENS = 0 sends the image only.
CONTEXT_MAX_TOKENS = int(os.environ.get("IMAGE_CONTEXT_MAX_TOKENS", "32000"))
CONTEXT_BLOCKS = int(os.environ.get("IMAGE_CONTEXT_BLOCKS", "100"))

# Accepted so existing callers (server.py) keep working; images are always
# processed sequentially now and the visual-match loop no longer exists.
CONCURRENCY = 1
OUTPUT_FILE = None
ENABLE_MERMAID_VISUAL_MATCH_LOOP = False


def _config() -> ImageConfig:
    return ImageConfig(
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY,
        model=OPENAI_MODEL,
        temperature=TEMPERATURE,
        top_p=TOP_P,
        thinking_timeout=LLM_THINKING_TIMEOUT_SECONDS,
        fallback_timeout=LLM_FALLBACK_TIMEOUT_SECONDS,
        mermaid_enabled=ENABLE_MERMAID_DIAGRAMS,
        validate_mermaid=VALIDATE_MERMAID,
        mmdc_bin=MERMAID_CLI_BIN,
        puppeteer_config=MERMAID_PUPPETEER_CONFIG_FILE,
        mermaid_timeout=MERMAID_PARSE_TIMEOUT_SECONDS,
        context_blocks=CONTEXT_BLOCKS,
        context_max_tokens=CONTEXT_MAX_TOKENS,
    )


# =========================
# Markdown image helpers (imported by server.py)
# =========================

IMAGE_MARKDOWN_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)")
FENCE_START_RE = re.compile(r"^\s*(```|~~~)")


def is_remote_url(path: str) -> bool:
    return urlparse(path).scheme in {"http", "https"}


def image_file_to_data_url(image_path: str) -> str:
    mime_type, _ = mimetypes.guess_type(image_path)
    with open(image_path, "rb") as f:
        payload = b64encode(f.read()).decode()
    return f"data:{mime_type or 'image/png'};base64,{payload}"


def strip_markdown_title(target: str) -> str:
    """Handles image targets like: path.png, path.png "title", <path with spaces>."""
    target = target.strip()
    if target.startswith("<") and ">" in target:
        return target[1 : target.index(">")].strip()
    for quote in (' "', " '"):
        if quote in target:
            target = target.split(quote, 1)[0]
            break
    return target.strip()


def resolve_image_path(markdown_file: Path, target: str) -> str:
    """Local relative targets resolve against the Markdown file dir; URLs pass through."""
    target = unquote(strip_markdown_title(target))
    if is_remote_url(target):
        return target
    target = target.split("#", 1)[0].split("?", 1)[0]
    image_path = Path(target)
    if not image_path.is_absolute():
        image_path = markdown_file.parent / image_path
    return str(image_path.resolve())


def extract_images_from_line(line: str, markdown_file: Path) -> list[dict]:
    return [
        {
            "alt": m.group("alt"),
            "original_target": m.group("target"),
            "resolved": resolve_image_path(markdown_file, m.group("target")),
            "original_markdown": m.group(0),
        }
        for m in IMAGE_MARKDOWN_RE.finditer(line)
    ]


def find_image_line_indices(lines) -> list[int]:
    """Image lines outside fenced code blocks (literal ![...] examples stay untouched)."""
    indices = []
    inside_fence = False
    for i, line in enumerate(lines):
        if FENCE_START_RE.match(line):
            inside_fence = not inside_fence
            continue
        if not inside_fence and IMAGE_MARKDOWN_RE.search(line):
            indices.append(i)
    return indices


# =========================
# Document context
# =========================

DATA_URL_RE = re.compile(r"data:[a-zA-Z0-9/+.\-]+;base64,[A-Za-z0-9+/=]+")

try:
    import tiktoken

    _ENCODING = tiktoken.get_encoding("o200k_base")

    def count_tokens(text: str) -> int:
        return len(_ENCODING.encode(text))

except Exception:  # tiktoken unavailable: rough chars/3 covers CJK-heavy text

    def count_tokens(text: str) -> int:
        return max(1, len(text) // 3)


def split_context_blocks(text: str) -> list[str]:
    """Splits document text into context units: a block is one non-empty line, one
    whole Markdown table, or one whole <image-unit> element (media payload stripped)."""
    text = DATA_URL_RE.sub("data:[embedded-image]", text)
    blocks: list[str] = []
    current: list[str] = []
    mode = None  # None | "table" | "unit"

    def flush():
        nonlocal current, mode
        if current:
            blocks.append("\n".join(current))
        current = []
        mode = None

    for line in text.splitlines():
        stripped = line.strip()
        if mode == "unit":
            current.append(line)
            if "</image-unit>" in stripped:
                flush()
            continue
        if "<image-unit" in stripped:
            flush()
            current.append(line)
            mode = "unit"
            if "</image-unit>" in stripped:
                flush()
            continue
        if not stripped:
            flush()
            continue
        if stripped.startswith("|"):
            if mode != "table":
                flush()
                mode = "table"
            current.append(line)
            continue
        flush()
        blocks.append(line)

    flush()
    return blocks


def context_enabled(cfg: ImageConfig) -> bool:
    return cfg.context_max_tokens > 0 and cfg.context_blocks > 0


def trim_context_blocks(blocks: list[str], cfg: ImageConfig) -> list[str]:
    """Keeps the last context_blocks blocks, then drops the oldest block until the
    total fits context_max_tokens."""
    blocks = blocks[-cfg.context_blocks :]
    counts = [count_tokens(b) + 1 for b in blocks]
    total = sum(counts)
    while blocks and total > cfg.context_max_tokens:
        total -= counts.pop(0)
        blocks.pop(0)
    return blocks


def build_context(text: str, cfg: ImageConfig) -> str | None:
    """Context string from the document text preceding an image. Returns None when
    context is disabled or empty."""
    if not context_enabled(cfg) or not text.strip():
        return None
    blocks = trim_context_blocks(split_context_blocks(text), cfg)
    return "\n".join(blocks) if blocks else None


# =========================
# Per-image description (shared by both pipelines)
# =========================


def wrap_description(kind: str, text: str) -> str:
    text = text.strip()
    if text.startswith("[Image reconstruction:") or text.startswith("[Image description:"):
        return text
    return f"[Image reconstruction:\nType: {kind}\n{text}\n]"


async def render_entity_description(
    graph: stages.DiagramGraph, transcript: list[str], cfg: ImageConfig
) -> str:
    if cfg.mermaid_enabled:
        code = mermaid.graph_to_mermaid(graph)
        ok, error = await mermaid.validate_mermaid(code, cfg)
        if ok:
            content = f"```mermaid\n{code}\n```"
        else:
            print(f"[WARN] emitted Mermaid failed validation (unexpected):\n{error}")
            content = mermaid.graph_to_text(graph)
    else:
        content = mermaid.graph_to_text(graph)

    labels = {n.id: n.label for n in graph.nodes}
    notes = [
        f"- {labels[e.src]} / {labels[e.dst]}: relation shown by {e.cue.replace('_', ' ')}"
        for e in graph.edges
        if e.cue != "arrow" and e.src in labels and e.dst in labels
    ]
    covered = " ".join(
        [n.label for n in graph.nodes]
        + [g.label for g in graph.groups]
        + [e.label or "" for e in graph.edges]
    )
    leftover = stages.missing_strings(transcript, covered)

    parts = ["[Image reconstruction:", "Type: entity diagram", "Reconstructed content:", content]
    if notes:
        parts.append("Detailed notes:")
        parts.extend(notes)
    if leftover:
        parts.append("Other visible text:")
        parts.extend(f"- {s}" for s in leftover)
    parts.append("]")
    return "\n".join(parts)


async def describe_images(
    llm: Llm, image_blocks: list, cfg: ImageConfig, context: str | None = None
) -> str:
    kind = await stages.classify(llm, image_blocks, context)
    transcript = await stages.transcribe(llm, image_blocks, context)
    print(f"  kind: {kind}, transcript: {len(transcript)} string(s)")

    if kind == "entity_diagram":
        try:
            graph = await stages.extract_graph(llm, image_blocks, transcript, context)
            if graph.nodes:
                graph = await stages.verify_graph(llm, image_blocks, graph)
                print(f"  graph: {len(graph.nodes)} node(s), {len(graph.edges)} edge(s)")
                return await render_entity_description(graph, transcript, cfg)
            print("  no entities extracted; falling back to plain description.")
        except Exception as exc:
            print(f"[WARN] entity-diagram route failed ({exc}); falling back to plain description.")

    text = await stages.describe_plain(llm, image_blocks, kind, transcript, context)
    missing = stages.missing_strings(transcript, text)
    if missing:
        print(f"  coverage retry for {len(missing)} missing string(s)")
        text = await stages.describe_plain(
            llm, image_blocks, kind, transcript, context, must_include=missing
        )
    return wrap_description(kind, text)


def render_image_unit(media_html: str, description: str) -> str:
    safe_description = description.replace("\n\n", "\n") if description else ""
    return (
        f"<image-unit>\n"
        f"  <image-media>\n"
        f"    {media_html.strip()}\n"
        f"  </image-media>\n"
        f"  <image-description>\n"
        f"{safe_description}\n"
        f"  </image-description>\n"
        f"</image-unit>"
    )


# =========================
# Markdown folder pipeline (MinerU output; used by parser/server.py)
# =========================


def build_image_blocks(images: list[dict]) -> list:
    blocks = []
    for number, img in enumerate(images, 1):
        if is_remote_url(img["resolved"]):
            url = img["resolved"]
        else:
            url = image_file_to_data_url(img["resolved"])
        blocks.append({"type": "image_url", "image_url": {"url": url, "detail": "high"}})
        if img.get("alt"):
            blocks.append({"type": "text", "text": f"Image {number} alt text: {img['alt']}"})
    return blocks


def render_markdown_image_unit(images: list[dict], description: str) -> str:
    tags = []
    for img in images:
        src = img["resolved"] if is_remote_url(img["resolved"]) else image_file_to_data_url(img["resolved"])
        alt = img["alt"].replace('"', "&quot;")
        tags.append(f'<img src="{src}" alt="{alt}">')
    return render_image_unit("\n    ".join(tags), description)


def get_described_output_path(input_path: Path) -> Path:
    return input_path.with_name(input_path.stem + ".described" + input_path.suffix)


def find_markdown_files_to_process(folder_path: Path):
    files_to_process = []
    skipped = []
    for md_file in sorted(folder_path.rglob("*.md")):
        if md_file.name.endswith(".described.md"):
            skipped.append((md_file, "already a described output file"))
        elif get_described_output_path(md_file).exists():
            skipped.append((md_file, "described output already exists"))
        else:
            files_to_process.append(md_file)
    return files_to_process, skipped


async def process_one_markdown_file(llm: Llm, input_path: Path, cfg: ImageConfig) -> dict:
    input_path = input_path.resolve()
    output_path = get_described_output_path(input_path)

    try:
        if output_path.exists():  # another concurrent run finished it first
            return {"status": "skipped", "input": str(input_path), "output": str(output_path),
                    "reason": "described output already exists", "replaced_image_lines": 0}

        lines = input_path.read_text(encoding="utf-8").splitlines(keepends=True)
        indices = find_image_line_indices(lines)

        print("")
        print("=" * 80)
        print(f"Processing file: {input_path}")
        print(f"Found {len(indices)} image line(s).")
        print("=" * 80)

        new_lines = list(lines)
        replaced = 0

        # Sequential, in document order: earlier replacements become context for
        # later images.
        for index in indices:
            original_line = new_lines[index].rstrip("\n")
            images = [
                img
                for img in extract_images_from_line(original_line, input_path)
                if is_remote_url(img["resolved"]) or os.path.exists(img["resolved"])
            ]
            if not images:
                print(f"[WARN] No resolvable image on line {index + 1}; leaving line as is.")
                continue

            print(f"Image line {index + 1}: {original_line[:120]}")
            context = build_context("".join(new_lines[:index]), cfg)

            try:
                description = await describe_images(llm, build_image_blocks(images), cfg, context)
            except Exception as exc:
                print(f"[WARN] Image line {index + 1} failed: {exc}")
                description = ""

            new_lines[index] = render_markdown_image_unit(images, description) + "\n"
            replaced += 1

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("".join(new_lines), encoding="utf-8")

        print(f"Done: {input_path} -> {output_path} ({replaced} image line(s) replaced)")
        return {"status": "done", "input": str(input_path), "output": str(output_path),
                "reason": "", "replaced_image_lines": replaced}

    except Exception as exc:
        print(f"ERROR while processing {input_path}: {exc}")
        return {"status": "error", "input": str(input_path), "output": str(output_path),
                "reason": str(exc), "replaced_image_lines": 0}


async def process_markdown_folder():
    cfg = _config()
    folder_path = Path(MARKDOWN_FOLDER).resolve()

    if not folder_path.is_dir():
        raise NotADirectoryError(f"MARKDOWN_FOLDER is not a directory: {folder_path}")

    files_to_process, skipped_files = find_markdown_files_to_process(folder_path)

    print(f"Input folder: {folder_path}")
    print(f"Markdown files to process: {len(files_to_process)} (skipped: {len(skipped_files)})")
    print(f"Model: {cfg.model}")
    print(f"Mermaid diagrams: {cfg.mermaid_enabled}")
    print(f"Context: last {cfg.context_blocks} blocks, max {cfg.context_max_tokens} tokens")
    print("")

    if not files_to_process:
        print("No Markdown files to process.")
        return

    llm = Llm(cfg)
    file_semaphore = asyncio.Semaphore(FILE_CONCURRENCY)

    async def run_one(path: Path):
        async with file_semaphore:
            return await process_one_markdown_file(llm, path, cfg)

    results = await asyncio.gather(*(run_one(p) for p in files_to_process))

    errors = [r for r in results if r["status"] == "error"]
    total = sum(r["replaced_image_lines"] for r in results)
    print("")
    print("=" * 80)
    print(f"Batch complete. Files: {len(results)}, errors: {len(errors)}, "
          f"image lines replaced: {total}")
    for r in errors:
        print(f"  ERROR {r['input']}: {r['reason']}")


# =========================
# SQLite <image-unit> pipeline (CLI)
# =========================

IMAGE_UNIT_RE = re.compile(r"<image-unit\b[^>]*>(?P<inner>.*?)</image-unit>", re.IGNORECASE | re.DOTALL)
IMAGE_MEDIA_RE = re.compile(r"<image-media\b[^>]*>(?P<media>.*?)</image-media>", re.IGNORECASE | re.DOTALL)
IMAGE_DESCRIPTION_RE = re.compile(r"<image-description\b[^>]*>(?P<description>.*?)</image-description>", re.IGNORECASE | re.DOTALL)
IMG_SRC_RE = re.compile(r"<img\b[^>]*\bsrc=[\"'](?P<src>[^\"']+)[\"']", re.IGNORECASE)

# Older runs appended judge notes to descriptions; strip them when reading.
JUDGE_NOTE_RE = re.compile(
    r"\n*\[(?:Image description coverage judge|Mermaid visual match judge|"
    r"Mermaid validation warning):.*?\n\]",
    re.DOTALL,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_judge_notes(description: str) -> str:
    return JUDGE_NOTE_RE.sub("", description or "").strip()


def extract_image_units(body: str) -> list[dict]:
    units = []
    for match in IMAGE_UNIT_RE.finditer(body or ""):
        inner = match.group("inner")
        media_match = IMAGE_MEDIA_RE.search(inner)
        description_match = IMAGE_DESCRIPTION_RE.search(inner)
        media_html = media_match.group("media").strip() if media_match else ""
        units.append(
            {
                "span": match.span(),
                "media_html": media_html,
                "sources": IMG_SRC_RE.findall(media_html) if media_html else [],
                "description": strip_judge_notes(
                    description_match.group("description") if description_match else ""
                ),
            }
        )
    return units


def build_image_blocks_from_sources(sources: list[str]) -> list:
    blocks = []
    for src in sources:
        if not is_remote_url(src) and not src.startswith("data:"):
            if os.path.exists(src):
                src = image_file_to_data_url(src)
            else:
                print(f"[WARN] Image source not found: {src}")
                continue
        blocks.append({"type": "image_url", "image_url": {"url": src, "detail": "high"}})
    return blocks


def make_working_copy(source_path: Path, dest_path: Path) -> None:
    """Consistent whole-database copy via SQLite's online backup API (a plain file
    copy can tear committed data still living in the -wal side file)."""
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(dest_path) + suffix)
        if stale.exists():
            stale.unlink()

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(str(dest_path))
        try:
            with dest:
                source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def reindex_node_fts(conn: sqlite3.Connection, node_id: str) -> None:
    """Rebuilds the nodes_fts row for one node, mirroring GraphStore._reindex_fts;
    no librarian bootstrap path covers nodes_fts."""
    row = conn.execute(
        "SELECT title, summary, body, keywords_json, status FROM nodes WHERE id=?",
        (node_id,),
    ).fetchone()
    if row is None:
        return

    conn.execute("DELETE FROM nodes_fts WHERE node_id=?", (node_id,))
    if row["status"] == "deleted":
        return

    try:
        keywords = json.loads(row["keywords_json"] or "[]")
    except Exception:
        keywords = []

    text = " ".join(
        part
        for part in [
            row["title"] or "",
            row["summary"] or "",
            row["body"] or "",
            " ".join(k for k in keywords if isinstance(k, str)),
        ]
        if part
    )
    conn.execute("INSERT INTO nodes_fts(node_id, text) VALUES(?, ?)", (node_id, text))


def invalidate_search_index(conn: sqlite3.Connection, node_ids) -> None:
    """nodes_fts rebuilt here; vec_body rows dropped so bootstrap re-embeds;
    search_index_version cleared so search_items rebuild. Pure SQL, no embedder."""
    if not node_ids:
        return

    for node_id in node_ids:
        try:
            reindex_node_fts(conn, node_id)
        except sqlite3.OperationalError as exc:
            print(f"[WARN] nodes_fts reindex skipped for {node_id}: {exc}")
        try:
            conn.execute("DELETE FROM vec_body WHERE node_id=?", (node_id,))
        except sqlite3.OperationalError:
            pass  # vector tables may not exist yet; bootstrap will build them

    try:
        conn.execute("DELETE FROM meta WHERE key='search_index_version'")
    except sqlite3.OperationalError as exc:
        print(f"[WARN] could not clear search_index_version: {exc}")

    print(f"Search index invalidated for {len(node_ids)} node(s).")


def load_follows_chains(conn: sqlite3.Connection) -> list[list[str]]:
    """Orders nodes into document chains via `follows` edges (the librarian creates
    them between consecutive nodes of a document). A chain starts at a node with no
    incoming `follows` edge and walks to the end."""
    try:
        rows = conn.execute(
            "SELECT source_node_id, target_node_id FROM edges WHERE label='follows'"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    next_of = {}
    has_prev = set()
    for row in rows:
        next_of.setdefault(row["source_node_id"], row["target_node_id"])
        has_prev.add(row["target_node_id"])

    chains = []
    for start in next_of:
        if start in has_prev:
            continue
        chain, seen = [], set()
        node_id = start
        while node_id and node_id not in seen:
            chain.append(node_id)
            seen.add(node_id)
            node_id = next_of.get(node_id)
        chains.append(chain)
    return chains


async def process_sqlite_database(database_path: str, output_path: str | None = None):
    """Fills empty <image-unit> descriptions in a wiki SQLite database. The source
    database is never modified; all work lands in a working copy.

    Nodes are walked in document order (follows-edge chains, start node first);
    each description is spliced into the body immediately, so later images in the
    same document see earlier images' fresh descriptions as context. The rolling
    context window crosses node boundaries within a chain."""
    cfg = _config()
    source_path = Path(database_path).resolve()

    if not source_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {source_path}")

    if output_path:
        working_path = Path(output_path).resolve()
    else:
        working_path = source_path.with_name(source_path.stem + ".described" + source_path.suffix)
    if working_path == source_path:
        raise ValueError("Working copy path must differ from the source database.")

    print("=" * 80)
    print(f"Source database (read-only): {source_path}")
    print(f"Working copy:                {working_path}")
    print(f"Model: {cfg.model}")
    print(f"Mermaid diagrams: {cfg.mermaid_enabled}")
    print(f"Context: last {cfg.context_blocks} blocks, max {cfg.context_max_tokens} tokens")
    print("=" * 80)

    make_working_copy(source_path, working_path)
    print("Working copy created.\n")

    conn = sqlite3.connect(str(working_path))
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute("SELECT id, body, status FROM nodes").fetchall()
        bodies = {row["id"]: row["body"] or "" for row in rows}
        statuses = {row["id"]: row["status"] for row in rows}

        # Document order: follows-edge chains first, then any remaining node as
        # its own standalone sequence, in table order.
        chains = [
            [nid for nid in chain if nid in bodies]
            for chain in load_follows_chains(conn)
        ]
        chained = {nid for chain in chains for nid in chain}
        sequences = [c for c in chains if c] + [
            [row["id"]] for row in rows if row["id"] not in chained
        ]

        total_units = 0
        pending_units = 0
        for body in bodies.values():
            for unit in extract_image_units(body):
                total_units += 1
                if not unit["description"] and unit["sources"]:
                    pending_units += 1

        print(f"Document chains: {len([c for c in chains if c])} "
              f"(standalone nodes: {len(sequences) - len([c for c in chains if c])})")
        print(f"<image-unit> blocks: {total_units} (to describe: {pending_units})\n")

        if not pending_units:
            print("Nothing to describe.")
            return

        llm = Llm(cfg)
        described_cache = {}  # media hash -> description (same image in many nodes)
        touched_nodes = []
        rewritten_units = 0
        failed = 0
        position = 0

        for sequence in sequences:
            rolling: list[str] = []  # context blocks carried across the chain

            for node_id in sequence:
                if statuses.get(node_id) == "deleted":
                    continue
                body = bodies[node_id]
                units = extract_image_units(body)

                shift = 0  # span offset drift from earlier splices in this body
                changed = False

                for unit in units:
                    if unit["description"] or not unit["sources"]:
                        continue

                    start, end = unit["span"][0] + shift, unit["span"][1] + shift
                    key = hashlib.sha256("|".join(unit["sources"]).encode("utf-8")).hexdigest()

                    description = described_cache.get(key)
                    if description is None:
                        image_blocks = build_image_blocks_from_sources(unit["sources"])
                        if not image_blocks:
                            print("[WARN] No usable image sources; skipping unit.")
                            continue

                        position += 1
                        print(f"Image {position}/{pending_units} (node {node_id})")

                        context = None
                        if context_enabled(cfg):
                            blocks = rolling + split_context_blocks(body[:start])
                            blocks = trim_context_blocks(blocks, cfg)
                            context = "\n".join(blocks) if blocks else None

                        try:
                            description = await describe_images(llm, image_blocks, cfg, context)
                            described_cache[key] = description
                        except Exception as exc:
                            print(f"[WARN] Image failed: {exc}")
                            failed += 1
                            continue

                    replacement = render_image_unit(unit["media_html"], description)
                    body = body[:start] + replacement + body[end:]
                    shift += len(replacement) - (end - start)
                    changed = True
                    rewritten_units += 1

                if changed:
                    conn.execute(
                        "UPDATE nodes SET body=?, updated_at=? WHERE id=?",
                        (body, now_iso(), node_id),
                    )
                    bodies[node_id] = body
                    touched_nodes.append(node_id)

                if context_enabled(cfg):
                    rolling = trim_context_blocks(
                        rolling + split_context_blocks(body), cfg
                    )

        invalidate_search_index(conn, touched_nodes)
        conn.commit()

        print("")
        print("=" * 80)
        print(f"Images described: {len(described_cache)} unique (failed: {failed})")
        print(f"<image-unit> blocks rewritten: {rewritten_units}/{total_units}")
        print(f"Nodes updated: {len(touched_nodes)}")
        print(f"Original left untouched: {source_path}")
        print(f"Result written to:       {working_path}")

    finally:
        conn.close()


# =========================
# CLI
# =========================


def main():
    global CONTEXT_MAX_TOKENS, CONTEXT_BLOCKS

    parser = argparse.ArgumentParser(
        description=(
            "Fill in <image-unit> descriptions inside a wiki SQLite database. "
            "The source database is never modified: all work lands in a working copy."
        )
    )
    parser.add_argument("database", help="Path to the source .sqlite file (opened read-only).")
    parser.add_argument("-o", "--output", default=None,
                        help="Working copy path. Defaults to <name>.described.sqlite.")
    parser.add_argument("--context-tokens", type=int, default=None,
                        help=f"Max context tokens per image (0 = no context). Default {CONTEXT_MAX_TOKENS}.")
    parser.add_argument("--context-blocks", type=int, default=None,
                        help=f"How many preceding blocks to consider. Default {CONTEXT_BLOCKS}.")
    args = parser.parse_args()

    if args.context_tokens is not None:
        CONTEXT_MAX_TOKENS = args.context_tokens
    if args.context_blocks is not None:
        CONTEXT_BLOCKS = args.context_blocks

    asyncio.run(process_sqlite_database(args.database, args.output))


if __name__ == "__main__":
    main()
