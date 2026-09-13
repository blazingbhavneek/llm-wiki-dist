"""pdf: headings are opt-in because MinerU headings are not reliable."""

from __future__ import annotations

from typing import Any, Sequence

from graph.wiki.schemas import CompiledSeedPlan


def plan(lines: Sequence[str], *, config: Any) -> CompiledSeedPlan | None:
    if not getattr(config, "pdf_use_headings", False):
        return None
    from .tree import heading_tree

    try:
        tree = heading_tree(lines)
    except ValueError:
        return None
    if _count(tree) < len(lines) / 300 or _depth(tree) > 3 or any(c.size > 0.6 * len(lines) for c in tree.children):
        return None
    from . import docx

    return docx.plan(lines, config=config)


def _count(node) -> int:
    return len(node.children) + sum(_count(c) for c in node.children)


def _depth(node) -> int:
    return 1 + max((_depth(c) for c in node.children), default=0) if node.children else 0
