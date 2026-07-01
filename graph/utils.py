"""Independent helpers for `Graph`: agent tool schemas, pure formatters, and the
mermaid validate/repair subsystem. Nothing here touches a `Graph` instance."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from . import core
from .models import Node, Settings

_MERMAID_BLOCK_RE = re.compile(
    r"```mermaid[ \t]*\r?\n(?P<code>.*?)```", re.IGNORECASE | re.DOTALL
)


# --- agent tool schemas (lowercase = the tool name the model calls) -----------
class search(BaseModel):
    """Search the wiki for nodes matching a text query."""

    text: str = Field(description="keywords to search for")


class read(BaseModel):
    """Read a node's full body and metadata by id."""

    node_id: str = Field(description="id of the node to read")


class follow_link(BaseModel):
    """Follow edges from a node to its neighboring nodes."""

    node_id: str = Field(description="id of the node to expand")
    direction: str = Field(
        default="both", description="'incoming', 'outgoing', or 'both'"
    )


class explore(BaseModel):
    """Hand distinct starting node ids to a team of exploration subagents."""

    node_ids: list[str] = Field(
        default_factory=list, description="distinct starting node ids"
    )


class finish(BaseModel):
    """Provide the final answer and the node ids used as evidence."""

    answer: str = Field(description="the final answer, grounded in node content")
    cited_node_ids: list[str] = Field(
        default_factory=list, description="ids that support the answer"
    )


LEAD_TOOLS = [search, explore, finish]
SUBAGENT_TOOLS = [search, read, follow_link, finish]


@dataclass
class Subrun:
    """Per-subagent run state. Created once, captured by the dispatch closure —
    never threaded through call signatures."""

    start_id: str
    index: int
    visited: list[str] = field(default_factory=list)
    read_ids: set[str] = field(default_factory=set)
    empty_streak: int = 0


# --- pure formatters ----------------------------------------------------------
def node_ref(node: Node) -> dict[str, str]:
    return {"id": node.id, "title": node.title or node.entity or node.id}


def dedupe(ids: list[str]) -> list[str]:
    seen: list[str] = []
    for node_id in ids:
        if node_id not in seen:
            seen.append(node_id)
    return seen


def clean_node_ref(value: str) -> str:
    """Strip the decoration an LLM tends to add around a node id (bullets,
    backticks, 'id:' prefix, a copied table row, a trailing '(Title)')."""
    text = str(value or "").strip()
    text = re.sub(r"^\s*[-*]\s*", "", text).strip()
    text = text.strip("`'\" \t\r\n")
    text = re.sub(r"^id\s*:\s*", "", text, flags=re.IGNORECASE).strip()
    if "|" in text:
        text = text.split("|", 1)[0].strip()
    text = re.sub(r"\s+\([^)]*\)\s*$", "", text).strip()
    return text.strip("`'\" \t\r\n,;")


def format_node_full(node: Node | None, requested_id: str, cleaned_id: str) -> str:
    if not node:
        return f"node not found\nrequested_id: {requested_id}\ncleaned_id: {cleaned_id}"
    note = ""
    if requested_id.strip() != cleaned_id:
        note = f"requested_id: {requested_id}\ncleaned_id: {cleaned_id}\n"
    return f"{note}id: {node.id}\ntitle: {node.title}\nsummary: {node.summary}\nbody:\n{node.body}"


# --- mermaid validate / repair (a real subsystem; not inlined) ----------------
def validate_mermaid(code: str, settings: Settings) -> tuple[bool, str]:
    """Render the code to SVG with mmdc; True if it parses + renders."""
    mmdc = shutil.which(settings.mermaid_cli_bin)
    if mmdc is None:
        return False, f"mermaid CLI '{settings.mermaid_cli_bin}' not found in PATH"
    config = Path(settings.mermaid_puppeteer_config).expanduser()
    cmd_prefix = [mmdc] + (["-p", str(config)] if config.exists() else [])
    with tempfile.TemporaryDirectory() as tmp:
        in_file = Path(tmp) / "d.mmd"
        out_file = Path(tmp) / "d.svg"
        in_file.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [*cmd_prefix, "-i", str(in_file), "-o", str(out_file)],
                capture_output=True,
                timeout=settings.mermaid_render_timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"mmdc timed out after {settings.mermaid_render_timeout}s"
        except Exception as exc:  # noqa: BLE001 - mmdc failed to start
            return False, f"mmdc failed to start: {exc}"
        if proc.returncode == 0 and out_file.exists() and out_file.stat().st_size > 0:
            return True, ""
        return False, proc.stderr.decode("utf-8", errors="replace").strip()


def repair_answer_mermaid(
    answer: str, llm: core.LlmClient, settings: Settings, emit: Callable[[dict], None]
) -> str:
    """Validate + repair every mermaid block in `answer`, emitting progress events."""
    blocks = list(_MERMAID_BLOCK_RE.finditer(answer))
    if not blocks:
        return answer
    emit({"type": "diagram_pending"})

    new_answer = answer
    fixed_codes: list[str] = []
    all_ok = True
    for match in blocks:
        code = match.group("code").strip()
        ok, error = validate_mermaid(code, settings)
        attempt = 0
        while not ok and attempt < settings.mermaid_repair_attempts:
            attempt += 1
            user = (
                "The following Mermaid diagram does not render. Fix the syntax, preserving "
                "all nodes, edges, directions, and labels. Use simple ASCII node IDs and "
                "quoted labels for any spaces/punctuation/long text.\n\n"
                f"Render error:\n{error}\n\n```mermaid\n{code}\n```"
            )
            try:
                response = llm.complete(core.MERMAID_FIX_SYSTEM, user)
            except Exception:  # noqa: BLE001 - repair is best-effort
                break
            fix = _MERMAID_BLOCK_RE.search(response)
            repaired = (
                fix.group("code").strip()
                if fix
                else response.strip().strip("`").strip()
            )
            if not repaired:
                break
            code = repaired
            ok, error = validate_mermaid(code, settings)
        all_ok = all_ok and ok
        fixed_codes.append(code)
        new_answer = new_answer.replace(match.group(0), f"```mermaid\n{code}\n```", 1)

    emit(
        {
            "type": "diagram_ready" if all_ok else "diagram_failed",
            "answer": new_answer,
            **({"mermaid": fixed_codes} if all_ok else {}),
        }
    )
    return new_answer
