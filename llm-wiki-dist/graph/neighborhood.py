"""Structural neighbours of a node: the chain, the typed edges, the page.

Ranking is meaning-based and therefore blind to layout. ``pmf_prg.txt`` and
``pmf_procdata.txt`` are two chunks of one printed page; a query about required
files ranks the first at #3 and the second at #19, and no amount of reranking
fixes that because the second chunk does not restate the question. Reading the
rest of the page does fix it.

Two callers share this module:

- background enrichment, which writes the walk into ``node_neighborhood`` so a
  request pays one indexed lookup instead of one query per hop per seed;
- the realtime pipeline, which falls back to walking live when the cache has
  not been built yet.

Chain edges (``follows``) are walked further than typed ones: chunk order is
document order, which is exactly the axis retrieval cannot see.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

log = logging.getLogger("graph_neighborhood")

# Edge label written between consecutive chunks of one document.
CHAIN_LABELS = frozenset({"follows"})

DEFAULT_CHAIN_HOPS = 3
DEFAULT_TYPED_HOPS = 1
DEFAULT_SIBLING_LIMIT = 6
# Ceiling for one walk, shared across every seed. A pathological hub node with
# hundreds of typed edges must not turn a 5 ms lookup into a 2 s graph crawl.
DEFAULT_MAX_NODES = 400


class NeighborStore(Protocol):
    def get_edges_for_nodes(self, node_ids: Iterable[str]) -> list[Any]: ...
    def get_nodes_by_ids(self, node_ids: Iterable[str]) -> list[Any]: ...
    def get_nodes_by_source_path(self, source_path: str, limit: int = 40) -> list[Any]: ...
    def get_node_neighborhoods(self, node_ids: Iterable[str]) -> dict[str, dict]: ...


@dataclass(frozen=True)
class NeighborRef:
    """One neighbour, in the shape a prompt and a client both accept."""

    node_id: str
    title: str = ""
    summary: str = ""
    label: str = "related"
    relation: str = "typed"  # chain | typed | sibling
    distance: int = 1

    def line(self) -> str:
        # The format subagent link-following already emits, so a model that has
        # seen one has seen both.
        return f"- [{self.label}] {self.node_id} | {self.title} | {self.summary}"

    def public(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "summary": self.summary,
            "label": self.label,
            "relation": self.relation,
            "distance": self.distance,
        }

    @classmethod
    def from_public(cls, payload: dict[str, Any]) -> "NeighborRef | None":
        node_id = str(payload.get("node_id") or "").strip()
        if not node_id:
            return None
        return cls(
            node_id=node_id,
            title=str(payload.get("title") or ""),
            summary=str(payload.get("summary") or ""),
            label=str(payload.get("label") or "related"),
            relation=str(payload.get("relation") or "typed"),
            distance=int(payload.get("distance") or 1),
        )


def neighbors_for_seeds(
    store: NeighborStore,
    seed_ids: Iterable[str],
    *,
    chain_hops: int = DEFAULT_CHAIN_HOPS,
    typed_hops: int = DEFAULT_TYPED_HOPS,
    siblings: bool = True,
    limit: int = 8,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> dict[str, list[NeighborRef]]:
    """Neighbours per seed, from the precomputed table where possible."""
    seeds = [str(s) for s in dict.fromkeys(seed_ids) if s]
    if not seeds:
        return {}

    found: dict[str, list[NeighborRef]] = {}
    missing = list(seeds)

    try:
        cached = store.get_node_neighborhoods(seeds)
    except Exception as exc:  # noqa: BLE001 - cache is an optimization only
        log.info("neighborhood cache read failed; walking live: %s", exc)
        cached = {}

    for seed in seeds:
        payload = cached.get(seed)
        if not payload or not _covers(payload, chain_hops, typed_hops, siblings):
            continue
        found[seed] = _select(_from_payload(payload, siblings), chain_hops, typed_hops, limit)
        missing.remove(seed)

    if missing:
        walked = walk(
            store,
            missing,
            chain_hops=chain_hops,
            typed_hops=typed_hops,
            siblings=siblings,
            max_nodes=max_nodes,
        )
        for seed, refs in walked.items():
            found[seed] = _select(refs, chain_hops, typed_hops, limit)

    return {seed: found.get(seed, []) for seed in seeds}


def build_payload(
    store: NeighborStore,
    node_id: str,
    *,
    chain_hops: int = DEFAULT_CHAIN_HOPS,
    typed_hops: int = DEFAULT_TYPED_HOPS,
    sibling_limit: int = DEFAULT_SIBLING_LIMIT,
) -> dict[str, Any]:
    """The stored form of one node's walk, for the enrichment job."""
    refs = walk(
        store,
        [node_id],
        chain_hops=chain_hops,
        typed_hops=typed_hops,
        siblings=True,
        sibling_limit=sibling_limit,
    ).get(node_id, [])

    return {
        "chain_hops": chain_hops,
        "typed_hops": typed_hops,
        "chain": [ref.public() for ref in refs if ref.relation == "chain"],
        "typed": [ref.public() for ref in refs if ref.relation == "typed"],
        "siblings": [ref.public() for ref in refs if ref.relation == "sibling"],
    }


def walk(
    store: NeighborStore,
    seed_ids: Iterable[str],
    *,
    chain_hops: int = DEFAULT_CHAIN_HOPS,
    typed_hops: int = DEFAULT_TYPED_HOPS,
    siblings: bool = True,
    sibling_limit: int = DEFAULT_SIBLING_LIMIT,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> dict[str, list[NeighborRef]]:
    """Breadth-first walk from every seed at once, batched per hop.

    All seeds advance together so one hop costs one bulk edge query and one
    bulk node query, instead of two per seed per hop.
    """
    seeds = [str(s) for s in dict.fromkeys(seed_ids) if s]
    if not seeds:
        return {}

    results: dict[str, list[NeighborRef]] = {seed: [] for seed in seeds}
    visited: dict[str, set[str]] = {seed: {seed} for seed in seeds}
    frontier: dict[str, list[str]] = {seed: [seed] for seed in seeds}
    budget = max(0, int(max_nodes))
    hops = max(int(chain_hops), int(typed_hops))

    for hop in range(1, hops + 1):
        active = [node_id for ids in frontier.values() for node_id in ids]
        if not active or budget <= 0:
            break

        adjacency = _adjacency(store, active)
        pending: list[tuple[str, str, str, str]] = []  # seed, other, label, relation
        next_frontier: dict[str, list[str]] = {}

        for seed, ids in frontier.items():
            for node_id in ids:
                for other, label in adjacency.get(node_id, ()):
                    chain = label in CHAIN_LABELS
                    if hop > (chain_hops if chain else typed_hops):
                        continue
                    if other in visited[seed]:
                        continue
                    visited[seed].add(other)
                    pending.append((seed, other, label, "chain" if chain else "typed"))
                    if chain:
                        next_frontier.setdefault(seed, []).append(other)
                    budget -= 1
                    if budget <= 0:
                        break
                if budget <= 0:
                    break
            if budget <= 0:
                break

        if not pending:
            break

        nodes = _nodes_by_id(store, {other for _seed, other, _label, _rel in pending})
        for seed, other, label, relation in pending:
            node = nodes.get(other)
            if node is None:
                continue
            results[seed].append(
                NeighborRef(
                    node_id=other,
                    title=_text(getattr(node, "title", "")),
                    summary=_text(getattr(node, "summary", "")),
                    label=label,
                    relation=relation,
                    distance=hop,
                )
            )

        frontier = next_frontier

    if siblings:
        _add_siblings(store, seeds, results, visited, sibling_limit)

    return results


# region internals


def _adjacency(store: NeighborStore, node_ids: list[str]) -> dict[str, list[tuple[str, str]]]:
    try:
        edges = store.get_edges_for_nodes(node_ids)
    except Exception as exc:  # noqa: BLE001 - degrade to no neighbours
        log.info("neighborhood edge fetch failed: %s", exc)
        return {}

    adjacency: dict[str, list[tuple[str, str]]] = {}
    for edge in edges:
        if getattr(edge, "invalid_at", None) or getattr(edge, "expired_at", None):
            continue
        source = str(getattr(edge, "source_node_id", "") or "")
        target = str(getattr(edge, "target_node_id", "") or "")
        label = str(getattr(edge, "label", "") or "related")
        if not source or not target:
            continue
        adjacency.setdefault(source, []).append((target, label))
        adjacency.setdefault(target, []).append((source, label))
    return adjacency


def _nodes_by_id(store: NeighborStore, node_ids: Iterable[str]) -> dict[str, Any]:
    ids = [node_id for node_id in dict.fromkeys(node_ids) if node_id]
    if not ids:
        return {}
    try:
        nodes = store.get_nodes_by_ids(ids)
    except Exception as exc:  # noqa: BLE001
        log.info("neighborhood node fetch failed: %s", exc)
        return {}
    return {str(node.id): node for node in nodes if _is_active(node)}


def _is_active(node: Any) -> bool:
    # Node.status is a str-Enum; test its value so both the enum and a plain
    # string (test doubles, cached rows) answer the same way.
    status = getattr(node, "status", "active")
    return str(getattr(status, "value", status)) == "active"


def _add_siblings(
    store: NeighborStore,
    seeds: list[str],
    results: dict[str, list[NeighborRef]],
    visited: dict[str, set[str]],
    sibling_limit: int,
) -> None:
    """Same page, near the same offset: the rest of the list being asked about."""
    seed_nodes = _nodes_by_id(store, seeds)
    for seed in seeds:
        node = seed_nodes.get(seed)
        source_path = str(getattr(node, "source_path", "") or "") if node else ""
        if not source_path:
            continue
        try:
            page = store.get_nodes_by_source_path(source_path, limit=40)
        except Exception as exc:  # noqa: BLE001
            log.info("sibling fetch failed: %s", exc)
            continue

        anchor = _range_start(node)
        ordered = sorted(page, key=lambda other: abs(_range_start(other) - anchor))
        for other in ordered:
            other_id = str(getattr(other, "id", "") or "")
            if not other_id or other_id in visited[seed]:
                continue
            visited[seed].add(other_id)
            results[seed].append(
                NeighborRef(
                    node_id=other_id,
                    title=_text(getattr(other, "title", "")),
                    summary=_text(getattr(other, "summary", "")),
                    label="same-page",
                    relation="sibling",
                    distance=1,
                )
            )
            if sum(1 for ref in results[seed] if ref.relation == "sibling") >= sibling_limit:
                break


def _range_start(node: Any) -> int:
    ranges = getattr(node, "source_ranges", None) or []
    for item in ranges:
        if isinstance(item, (tuple, list)) and item:
            try:
                return int(item[0])
            except (TypeError, ValueError):
                continue
    return 0


def _from_payload(payload: dict[str, Any], siblings: bool) -> list[NeighborRef]:
    refs: list[NeighborRef] = []
    buckets = ["chain", "typed"] + (["siblings"] if siblings else [])
    for bucket in buckets:
        for item in payload.get(bucket) or []:
            if not isinstance(item, dict):
                continue
            ref = NeighborRef.from_public(item)
            if ref is not None:
                refs.append(ref)
    return refs


def _covers(payload: dict[str, Any], chain_hops: int, typed_hops: int, siblings: bool) -> bool:
    # A stored walk answers a request only when it went at least as far.
    if int(payload.get("chain_hops") or 0) < chain_hops:
        return False
    if int(payload.get("typed_hops") or 0) < typed_hops:
        return False
    return not siblings or "siblings" in payload


def _select(
    refs: list[NeighborRef], chain_hops: int, typed_hops: int, limit: int
) -> list[NeighborRef]:
    """Nearest first, chain before typed: document order carries the most."""
    kept = [
        ref
        for ref in refs
        if ref.distance
        <= (chain_hops if ref.relation == "chain" else max(1, typed_hops))
    ]
    order = {"chain": 0, "sibling": 1, "typed": 2}
    kept.sort(key=lambda ref: (ref.distance, order.get(ref.relation, 3)))
    seen: set[str] = set()
    unique: list[NeighborRef] = []
    for ref in kept:
        if ref.node_id in seen:
            continue
        seen.add(ref.node_id)
        unique.append(ref)
    return unique[: max(0, int(limit))] if limit else unique


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


# endregion internals
