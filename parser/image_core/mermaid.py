"""Deterministic DiagramGraph -> Mermaid emitter, plus one mmdc sanity check.

The emitter owns all syntax (ASCII ids, quoted labels, nested subgraphs), so the
LLM never writes Mermaid and the old repair/visual-match loops have nothing to fix.
"""

import asyncio
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

from image_core.config import ImageConfig
from image_core.stages import DiagramGraph, Edge


def _esc(label: str) -> str:
    label = " ".join((label or "").replace('"', "'").split())
    return label or " "


def _arrow(e: Edge) -> str:
    dashed = e.style in ("dashed", "dotted")
    if e.bidirectional:
        return "<-.->" if dashed else "<-->"
    if e.directed:
        return "-.->" if dashed else "-->"
    return "-.-" if dashed else "---"


def graph_to_mermaid(graph: DiagramGraph) -> str:
    node_ids = {n.id: f"n{i}" for i, n in enumerate(graph.nodes, 1)}
    group_ids = {g.id: f"g{i}" for i, g in enumerate(graph.groups, 1)}

    direction = graph.direction if graph.direction in ("TD", "LR", "BT", "RL") else "TD"
    lines = [f"flowchart {direction}"]

    child_groups = defaultdict(list)
    top_groups = []
    for g in graph.groups:
        if g.parent and g.parent in group_ids and g.parent != g.id:
            child_groups[g.parent].append(g)
        else:
            top_groups.append(g)

    grouped_nodes = defaultdict(list)
    loose_nodes = []
    for n in graph.nodes:
        if n.group and n.group in group_ids:
            grouped_nodes[n.group].append(n)
        else:
            loose_nodes.append(n)

    emitted = set()

    def emit_group(g, indent: int) -> None:
        if g.id in emitted:  # guards against parent cycles from the model
            return
        emitted.add(g.id)
        pad = "    " * indent
        lines.append(f'{pad}subgraph {group_ids[g.id]}["{_esc(g.label)}"]')
        for n in grouped_nodes[g.id]:
            lines.append(f'{pad}    {node_ids[n.id]}["{_esc(n.label)}"]')
        for child in child_groups[g.id]:
            emit_group(child, indent + 1)
        lines.append(f"{pad}end")

    for n in loose_nodes:
        lines.append(f'    {node_ids[n.id]}["{_esc(n.label)}"]')
    for g in top_groups:
        emit_group(g, 1)

    for e in graph.edges:
        if e.src not in node_ids or e.dst not in node_ids:
            continue
        label = f'|"{_esc(e.label)}"|' if e.label else ""
        lines.append(f"    {node_ids[e.src]} {_arrow(e)}{label} {node_ids[e.dst]}")

    return "\n".join(lines)


def graph_to_text(graph: DiagramGraph) -> str:
    """Plain node/edge list for when Mermaid output is disabled."""
    labels = {n.id: n.label for n in graph.nodes}
    lines = ["Entities:"]
    lines += [f"- {n.label}" + (f" (in {g})" if (g := _group_label(graph, n.group)) else "")
              for n in graph.nodes]
    if graph.edges:
        lines.append("Relations:")
        for e in graph.edges:
            if e.src not in labels or e.dst not in labels:
                continue
            joint = "<->" if e.bidirectional else ("->" if e.directed else "--")
            line = f"- {labels[e.src]} {joint} {labels[e.dst]}"
            if e.label:
                line += f" [{e.label}]"
            lines.append(line)
    return "\n".join(lines)


def _group_label(graph: DiagramGraph, group_id: str | None) -> str | None:
    if not group_id:
        return None
    return next((g.label for g in graph.groups if g.id == group_id), None)


async def validate_mermaid(code: str, cfg: ImageConfig) -> tuple[bool, str]:
    """Renders to SVG with mmdc. Missing mmdc/puppeteer config: warn and pass,
    matching the old non-required behavior."""
    if not cfg.validate_mermaid:
        return True, ""

    mmdc_path = shutil.which(cfg.mmdc_bin)
    if mmdc_path is None:
        print(f"[WARN] '{cfg.mmdc_bin}' not in PATH; skipping Mermaid validation.")
        return True, ""

    puppeteer_config = Path(cfg.puppeteer_config).expanduser()
    if not puppeteer_config.exists():
        print(f"[WARN] Puppeteer config missing: {puppeteer_config}; skipping Mermaid validation.")
        return True, ""

    with tempfile.TemporaryDirectory() as tmpdir:
        input_file = Path(tmpdir) / "diagram.mmd"
        output_file = Path(tmpdir) / "diagram.svg"
        input_file.write_text(code, encoding="utf-8")

        cmd = [mmdc_path, "-p", str(puppeteer_config), "-i", str(input_file), "-o", str(output_file)]
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=cfg.mermaid_timeout
            )
        except asyncio.TimeoutError:
            if process is not None:
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
            return False, f"mmdc timed out after {cfg.mermaid_timeout}s"
        except Exception as exc:
            return False, f"mmdc failed to start: {exc}"

        if process.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
            return True, ""

        return False, (
            f"mmdc exit {process.returncode}\n"
            f"{stdout.decode('utf-8', errors='replace').strip()}\n"
            f"{stderr.decode('utf-8', errors='replace').strip()}"
        ).strip()
