"""
Clean-slate implementation of the split read/write architecture.

No wrapping of the existing Graph monolith.
Each session class owns its own logic, db connection, and state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import threading
import uuid
from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from db import Database
from embeddings import Embedder, Reranker
from llm.agent import AgentClient

from .core import *
from .enrich import EnrichmentQueue
from .models import (
    AgentAnswer,
    ClaimExtraction,
    ClusterRenamePlan,
    Edge,
    EdgeSuggestions,
    EntityMatch,
    GraphStats,
    Keywords,
    Node,
    NodeStatus,
    NodeType,
    QueryResult,
    Settings,
    now_iso,
)
from .utils import *
from .utils import (
    Subrun,
    clean_node_ref,
    dedupe,
    format_node_full,
    node_ref,
    repair_answer_mermaid,
)

log = logging.getLogger("graph_rw")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_NUMBERED_DOC_RE = re.compile(r"^\d+-(.+\.md)$")
_CASCADE_MATCH_THRESHOLD = 0.45

# Bump when the search_items chunking scheme changes; forces a bootstrap rebuild.
SEARCH_INDEX_VERSION = "chunk512-80-v1"


@dataclass
class EvidenceHit:
    """One retrieval hit, normalized across every pool (node + search_item)."""

    node_id: str
    field: str
    item_id: str | None
    text: str
    rank: int
    weight: float
    start_char: int | None = None

    def contribution(self, rrf_k: int) -> float:
        return self.weight / (rrf_k + self.rank)


def _item_vec_weight(settings: Settings, field: str) -> float:
    return {
        "title": settings.weight_title_vec,
        "claim": settings.weight_claim_vec,
        "small_chunk": settings.weight_small_chunk_vec,
        "summary": settings.weight_summary_vec,
        "big_chunk": settings.weight_big_chunk_vec,
    }.get(field, settings.weight_small_chunk_vec)


def _normalize_scores(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [1.0 for _ in values]
    return [(v - low) / (high - low) for v in values]


def _mmr_order(texts: list[str], rel: list[float], lam: float) -> list[int]:
    """Greedy MMR ordering: lam*rel - (1-lam)*max_sim_to_selected (token overlap)."""
    remaining = set(range(len(texts)))
    order: list[int] = []
    while remaining:
        best_idx, best_score = None, float("-inf")
        for i in remaining:
            sim = max((token_jaccard(texts[i], texts[j]) for j in order), default=0.0)
            score = lam * rel[i] - (1.0 - lam) * sim
            if score > best_score:
                best_score, best_idx = score, i
        order.append(best_idx)
        remaining.discard(best_idx)
    return order


def _node_snippet(node: Node) -> str:
    return (node.summary or node.title or "").strip()


def _evidence_why(node_hits: list[EvidenceHit]) -> list[dict[str, Any]]:
    """Best (lowest) rank per field that matched this node, rank-ascending."""
    best: dict[str, int] = {}
    for hit in node_hits:
        if hit.field not in best or hit.rank < best[hit.field]:
            best[hit.field] = hit.rank
    return [
        {"field": field, "rank": rank}
        for field, rank in sorted(best.items(), key=lambda kv: kv[1])
    ]


def _format_lead_candidate(result: dict[str, Any]) -> str:
    node = result["node"]
    why = result.get("why", [])
    why_str = ", ".join(f"{w['field']}#{w['rank']}" for w in why[:4]) or "n/a"
    lines = (
        f"- node_id: `{node.id}`\n"
        f"  title: {node.title}\n"
        f"  summary: {node.summary}\n"
        f"  why_matched: {why_str}"
    )
    for ev in result.get("evidence", [])[:3]:
        snippet = " ".join((ev.get("text") or "").split())
        if len(snippet) > 240:
            snippet = snippet[:240] + "…"
        if snippet:
            lines += f"\n  evidence[{ev['field']}]: {snippet}"
    lines += (
        "\n  next_action: pass this id to explore(node_ids=[...]) if promising "
        "and distinct"
    )
    return lines


# ============================================================================
# 1. Shared Runtime
# ============================================================================


class SharedGraphRuntime:
    """
    Heavy resources loaded once, shared across all sessions.
    No per-request mutable state.
    """

    def __init__(self, settings: Settings):
        print(
            "[runtime] building SharedGraphRuntime (embedder + reranker + llm)",
            flush=True,
        )
        self.settings = settings
        # Background assimilation queue; installed by WriteGraphService.
        self.enrich: Any = None
        print(
            f"[runtime] embed_backend={settings.embed_backend} rerank_backend={settings.rerank_backend}",
            flush=True,
        )
        self.embedder = Embedder(settings)
        self.reranker = self._build_reranker(settings)
        print("[runtime] SharedGraphRuntime ready", flush=True)
        self.llm = AgentClient(
            model=settings.chat_model,
            base_url=settings.chat_base_url,
            api_key=settings.chat_api_key,
            system_prompt=GRAPH_SYSTEM_PROMPT,
            temperature=settings.chat_temperature,
        )

    def _build_reranker(self, settings: Settings) -> Reranker | None:
        try:
            return Reranker(settings)
        except Exception as exc:
            log.info("reranker unavailable: %s", exc)
            return None

    def close(self) -> None:
        for obj in (self.embedder, self.reranker, self.llm):
            if hasattr(obj, "close"):
                try:
                    obj.close()
                except Exception:
                    pass

    def update_settings(self, settings: Settings) -> None:
        """Replace settings at runtime. Caller must handle client rebuilds."""
        self.settings = settings


# ============================================================================
# 2. Db Session
# ============================================================================


class GraphDbSession:
    """
    One database connection per operation.
    Opens in WAL-friendly mode, commits on success, rolls back on error.
    """

    def __init__(self, settings: Settings, readonly: bool = False):
        self.settings = settings
        self.conn = self._open(settings.database_path, readonly=readonly)

    def _open(self, path: str, readonly: bool) -> Any:
        import sqlite3

        mode = "ro" if readonly else "rw"
        uri = f"file:{path}?mode={mode}"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> GraphDbSession:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.close()


# ============================================================================
# 3. Read Session - All read logic lives here
# ============================================================================


class GraphReadSession:
    """
    Pure read operations. No mutation of the database.
    Each method receives emit explicitly, never stored on self.
    """

    def __init__(self, runtime: SharedGraphRuntime, db: GraphDbSession):
        self.runtime = runtime
        self.db = db
        self.settings = runtime.settings
        # Defaults to the shared client; apply_overrides may swap in a
        # per-request one when the caller overrides chat endpoint/model/key.
        self.llm = runtime.llm

    # Per-request overrides: one /api/ask may carry a subset of tunables;
    # anything omitted falls back to the global runtime defaults. Applied to a
    # throwaway session only, so concurrent requests never share state.
    def apply_overrides(self, overrides: dict[str, Any] | None) -> None:
        if not overrides:
            return
        int_keys = {
            "edge_candidate_k",
            "vector_query_k",
            "cascade_max_hops",
            "cascade_max_nodes",
            "agent_max_steps",
            "agent_patience",
            "search_rrf_k",
            "search_candidate_pool",
            "rerank_top_k",
            "subagent_count",
            "subagent_concurrency",
            "subagent_max_steps",
            "subagent_min_reads",
            "subagent_max_reads",
        }
        float_keys = {"chat_temperature"}
        str_keys = {"chat_base_url", "chat_api_key", "chat_model"}
        bool_keys = {"entity_dedup", "enable_mermaid"}
        clean: dict[str, Any] = {}
        for key, value in overrides.items():
            if value is None:
                continue
            try:
                if key in int_keys:
                    clean[key] = max(0, int(value))
                elif key in float_keys:
                    clean[key] = float(value)
                elif key in bool_keys:
                    clean[key] = bool(value)
                elif key in str_keys:
                    text = str(value).strip()
                    if text:
                        clean[key] = text
            except (TypeError, ValueError):
                continue
        if not clean:
            return
        self.settings = replace(self.settings, **clean)
        if any(k in clean for k in str_keys) or "chat_temperature" in clean:
            self.llm = AgentClient(
                model=self.settings.chat_model,
                base_url=self.settings.chat_base_url,
                api_key=self.settings.chat_api_key,
                system_prompt=GRAPH_SYSTEM_PROMPT,
                temperature=self.settings.chat_temperature,
            )

    # -- Basic CRUD reads -------------------------------------------------

    def get(self) -> tuple[list[Node], list[Edge]]:
        nodes = self.db_get_all_nodes()
        edges = self.db_get_all_edges()
        return nodes, edges

    def read_node(self, node_id: str) -> Node | None:
        node = self._db_get_node(node_id)
        if node:
            return node
        if not node_id.startswith("node:"):
            node = self._db_get_node(f"node:{node_id}")
            if node:
                return node
        # Fuzzy fallback
        matches = self._keyword_search(node_id, 5)
        return matches[0] if matches else None

    def follow_link(
        self,
        node_id: str,
        label: str | None = None,
        direction: str = "both",
        limit: int | None = None,
    ) -> list[tuple[Edge, Node]]:
        normalized = direction.lower().strip()
        if normalized not in {"incoming", "outgoing", "both"}:
            raise ValueError("direction must be 'incoming', 'outgoing', or 'both'")

        pairs: list[tuple[Edge, Node]] = []
        if normalized in {"outgoing", "both"}:
            for edge in self._db_get_outgoing_edges(node_id, label):
                target = self._db_get_node(edge.target_node_id)
                if target and target.status == NodeStatus.active:
                    pairs.append((edge, target))
        if normalized in {"incoming", "both"}:
            for edge in self._db_get_incoming_edges(node_id, label):
                source = self._db_get_node(edge.source_node_id)
                if source and source.status == NodeStatus.active:
                    pairs.append((edge, source))
        return pairs[:limit] if limit is not None else pairs

    def health(self, node_id: str | None = None) -> GraphStats:
        nodes = self.db_get_all_nodes()
        edges = self.db_get_all_edges()
        if node_id:
            nodes = [n for n in nodes if n.id == node_id]
            edges = [
                e for e in edges if node_id in (e.source_node_id, e.target_node_id)
            ]

        node_ids = {n.id for n in nodes}
        neighbors: dict[str, set[str]] = {nid: set() for nid in node_ids}
        for edge in edges:
            if edge.source_node_id in neighbors and edge.target_node_id in node_ids:
                neighbors[edge.source_node_id].add(edge.target_node_id)
            if edge.target_node_id in neighbors and edge.source_node_id in node_ids:
                neighbors[edge.target_node_id].add(edge.source_node_id)

        node_count = len(nodes)
        total_degree = sum(len(v) for v in neighbors.values())
        avg_degree = (total_degree / node_count) if node_count else 0.0
        max_edges = node_count * (node_count - 1) / 2
        density = ((total_degree / 2) / max_edges) if max_edges else 0.0

        overlap_total, overlap_pairs = 0.0, 0
        for nid, nid_neighbors in neighbors.items():
            for other_id in nid_neighbors:
                if other_id <= nid:
                    continue
                union = nid_neighbors | neighbors.get(other_id, set())
                if union:
                    overlap_total += len(
                        nid_neighbors & neighbors.get(other_id, set())
                    ) / len(union)
                    overlap_pairs += 1
        mean_overlap = (overlap_total / overlap_pairs) if overlap_pairs else 0.0

        clusters: dict[str, int] = {}
        for node in nodes:
            key = node.cluster or "Unclustered"
            clusters[key] = clusters.get(key, 0) + 1

        return GraphStats(
            total_nodes=node_count,
            active_nodes=sum(1 for n in nodes if n.status == NodeStatus.active),
            endogenous_nodes=sum(1 for n in nodes if n.type == NodeType.endogenous),
            exogenous_nodes=sum(1 for n in nodes if n.type == NodeType.exogenous),
            total_edges=len(edges),
            isolated_nodes=sum(1 for nid in node_ids if not neighbors[nid]),
            avg_degree=round(avg_degree, 3),
            density=round(density, 5),
            mean_neighbor_overlap=round(mean_overlap, 4),
            clusters=clusters,
            target_node_id=node_id,
        )

    def query(self, query_type: str, value: str) -> QueryResult:
        normalized = query_type.lower().strip()
        if normalized == "id":
            node = self.read_node(value)
            return QueryResult(
                query_type="id",
                value=value,
                nodes=[node] if node else [],
                edges=self._db_get_edges_for_node(value) if node else [],
            )
        if normalized == "keyword":
            nodes = self._keyword_search(value, self.settings.vector_query_k)
            edges: dict[str, Edge] = {}
            for node in nodes:
                for edge in self._db_get_edges_for_node(node.id):
                    edges[edge.id] = edge
            return QueryResult(
                query_type="keyword",
                value=value,
                nodes=nodes,
                edges=list(edges.values()),
            )
        if normalized == "vector":
            vector = self.runtime.embedder.embed_query(value)
            hits = self._vector_search(vector, "vec_body", self.settings.vector_query_k)
            seeds = [n for n in (self._db_get_node(nid) for nid, _ in hits) if n]
            nodes, edges_list = self._expand_neighborhood(seeds, hops=2)
            return QueryResult(
                query_type="vector", value=value, nodes=nodes, edges=edges_list
            )
        raise ValueError("query_type must be 'keyword', 'vector', or 'id'")

    def search(self, text: str, limit: int | None = None) -> list[Node]:
        """Backward-compatible node search. Delegates to the evidence-first
        pipeline and returns just the ranked ``Node`` list."""
        limit = limit or self.settings.vector_query_k
        try:
            results = self.search_with_evidence(text, limit)
        except Exception as exc:
            log.info("evidence search failed; BM25-only fallback: %s", exc)
            try:
                nodes = self._keyword_search(text, limit)
            except Exception as exc2:
                log.info("bm25 fallback failed: %s", exc2)
                return []
            return nodes[:limit]
        return [r["node"] for r in results][:limit]

    def search_with_evidence(
        self, text: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Evidence-first retrieval. Returns ``{node, score, why, evidence}`` dicts
        ranked by cross-encoder relevance (falling back to weighted RRF)."""
        limit = limit or self.settings.vector_query_k
        s = self.settings
        rrf_k = s.search_rrf_k
        hits: list[EvidenceHit] = []

        # --- node BM25 --------------------------------------------------------
        node_bm25: list[Node] = []
        try:
            node_bm25 = self._keyword_search(text, s.pool_node_bm25)
        except Exception as exc:
            log.info("node bm25 failed: %s", exc)
        for rank, node in enumerate(node_bm25, start=1):
            hits.append(
                EvidenceHit(
                    node.id,
                    "node_bm25",
                    None,
                    _node_snippet(node),
                    rank,
                    s.weight_node_bm25,
                )
            )

        # --- vector pools -----------------------------------------------------
        query_vec: list[float] | None = None
        try:
            query_vec = self.runtime.embedder.embed_query(text)
        except Exception as exc:
            log.info("query embed failed; BM25-only: %s", exc)

        if query_vec is not None:
            for table, field, weight, pool in (
                ("vec_body", "body_vec", s.weight_body_vec, s.pool_vec_body),
                (
                    "vec_summary",
                    "summary_vec",
                    s.weight_summary_vec,
                    s.pool_vec_summary,
                ),
            ):
                try:
                    vhits = self._vector_search(query_vec, table, pool)
                except Exception as exc:
                    log.info("%s search failed: %s", table, exc)
                    continue
                for rank, (node_id, _dist) in enumerate(vhits, start=1):
                    hits.append(EvidenceHit(node_id, field, None, "", rank, weight))

            try:
                item_hits = self._vector_search(
                    query_vec, "vec_search_item", s.pool_vec_item
                )
            except Exception as exc:
                log.info("vec_search_item search failed: %s", exc)
                item_hits = []
            rows = self._get_search_items([iid for iid, _ in item_hits])
            for rank, (item_id, _dist) in enumerate(item_hits, start=1):
                row = rows.get(item_id)
                if not row:
                    continue
                hits.append(
                    EvidenceHit(
                        row["node_id"],
                        row["field"],
                        item_id,
                        row["text"] or "",
                        rank,
                        _item_vec_weight(s, row["field"]),
                        row.get("start_char"),
                    )
                )

        # --- item BM25 --------------------------------------------------------
        try:
            item_bm25 = self._search_items_fts(text, s.pool_item_bm25)
        except Exception as exc:
            log.info("item bm25 failed: %s", exc)
            item_bm25 = []
        for rank, row in enumerate(item_bm25, start=1):
            hits.append(
                EvidenceHit(
                    row["node_id"],
                    row["field"],
                    row["item_id"],
                    row["text"] or "",
                    rank,
                    s.weight_item_bm25,
                    row.get("start_char"),
                )
            )

        if not hits:
            return []

        # --- weighted RRF per node + load active nodes ------------------------
        node_scores: dict[str, float] = defaultdict(float)
        for hit in hits:
            node_scores[hit.node_id] += hit.contribution(rrf_k)

        nodes: dict[str, Node] = {}
        for node_id in node_scores:
            node = self._db_get_node(node_id)
            if node and node.status == NodeStatus.active:
                nodes[node_id] = node

        by_node: dict[str, list[EvidenceHit]] = defaultdict(list)
        for hit in hits:
            if hit.node_id not in nodes:
                continue
            if not hit.text.strip():
                hit.text = _node_snippet(nodes[hit.node_id])
            by_node[hit.node_id].append(hit)

        # --- per-node evidence selection with caps ----------------------------
        pool: list[EvidenceHit] = []
        for node_id, node_hits in by_node.items():
            pool.extend(self._select_node_evidence(node_hits))
        pool.sort(key=lambda h: h.contribution(rrf_k), reverse=True)
        pool = [h for h in pool if h.text.strip()][: s.evidence_rerank_pool]

        # --- cross-encoder rerank of snippets (guarded) -----------------------
        rel: list[float] | None = None
        if self.runtime.reranker and pool:
            try:
                ranked = self.runtime.reranker.top_k(
                    text, [(pool[i].text, i) for i in range(len(pool))], len(pool)
                )
                by_idx = {idx: score for idx, score in ranked}
                rel = [float(by_idx.get(i, 0.0)) for i in range(len(pool))]
            except Exception as exc:
                log.info("snippet rerank failed; RRF order: %s", exc)
                rel = None
        if rel is None:
            rel = [pool[i].contribution(rrf_k) for i in range(len(pool))]
        rel = _normalize_scores(rel)

        # --- MMR dedup over the snippet pool ----------------------------------
        order = _mmr_order([h.text for h in pool], rel, s.evidence_mmr_lambda)
        rank_of = {idx: pos for pos, idx in enumerate(order)}

        node_pool_idx: dict[str, list[int]] = defaultdict(list)
        for i, hit in enumerate(pool):
            node_pool_idx[hit.node_id].append(i)

        # --- aggregate evidence back to nodes ---------------------------------
        results: list[dict[str, Any]] = []
        for node_id, node in nodes.items():
            snippet_idxs = node_pool_idx.get(node_id, [])
            if snippet_idxs:
                best_rel = max(rel[i] for i in snippet_idxs)
                chosen = sorted(snippet_idxs, key=lambda i: rank_of[i])[
                    : s.evidence_max_per_node
                ]
                evidence = [
                    {
                        "field": pool[i].field,
                        "text": pool[i].text,
                        "item_id": pool[i].item_id,
                        "rank": pool[i].rank,
                    }
                    for i in chosen
                ]
            else:
                best_rel, evidence = 0.0, []
            results.append(
                {
                    "node": node,
                    "score": best_rel + node_scores[node_id],
                    "why": _evidence_why(by_node[node_id]),
                    "evidence": evidence,
                }
            )
        results.sort(key=lambda d: d["score"], reverse=True)
        return results[:limit]

    def _select_node_evidence(self, node_hits: list[EvidenceHit]) -> list[EvidenceHit]:
        """Caps per node: <=max_per_node snippets, <=max_per_field per field,
        <=1 per overlapping small/big-chunk region (start_char proximity)."""
        s = self.settings
        ordered = sorted(
            node_hits, key=lambda h: h.contribution(s.search_rrf_k), reverse=True
        )
        selected: list[EvidenceHit] = []
        per_field: Counter = Counter()
        regions: list[int] = []
        seen: set[str] = set()
        for hit in ordered:
            if len(selected) >= s.evidence_max_per_node:
                break
            if not hit.text.strip():
                continue
            key = hit.item_id or hit.text[:80]
            if key in seen:
                continue
            if per_field[hit.field] >= s.evidence_max_per_field:
                continue
            if hit.field in ("small_chunk", "big_chunk") and hit.start_char is not None:
                if any(
                    abs(hit.start_char - r) < s.evidence_dedup_char_window
                    for r in regions
                ):
                    continue
                regions.append(hit.start_char)
            selected.append(hit)
            per_field[hit.field] += 1
            seen.add(key)
        return selected

    # -- Agent / Ask ------------------------------------------------------

    def ask(
        self,
        question: str,
        persist: bool = False,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> AgentAnswer:
        if persist:
            raise ValueError("ReadSession.ask() must use persist=False")

        emit = on_event or (lambda event: None)
        emit({"type": "start", "question": question})

        answer = self._run_lead(question, emit)

        if self.settings.enable_mermaid and answer.answer:
            # repair_answer_mermaid expects the llm as second arg
            answer.answer = repair_answer_mermaid(
                answer.answer, self.llm, self.settings, emit
            )

        emit({"type": "done"})
        return answer

    def _run_lead(self, question: str, emit: Callable) -> AgentAnswer:
        evidence: list[str] = []

        def dispatch(name: str, args: dict[str, Any]) -> str:
            if name == "search":
                query = str(args.get("text", ""))
                emit({"type": "search", "phase": "main", "query": query})
                try:
                    results = self.search_with_evidence(
                        query, limit=self.settings.rerank_top_k
                    )
                except Exception as exc:
                    log.info("lead evidence search failed; node-only: %s", exc)
                    results = [
                        {"node": n, "why": [], "evidence": []}
                        for n in self.search(query, limit=self.settings.rerank_top_k)
                    ]
                candidate_nodes = [r["node"] for r in results]
                emit(
                    {
                        "type": "candidates",
                        "count": len(candidate_nodes),
                        "nodes": [node_ref(n) for n in candidate_nodes],
                    }
                )
                if not candidate_nodes:
                    return "no nodes found"
                return "\n".join(_format_lead_candidate(r) for r in results)
            if name == "explore":
                return self._run_subagents(
                    args.get("node_ids", []), question, evidence, emit
                )
            return f"unknown tool: {name}"

        system_prompt = MAIN_AGENT_SYSTEM_PROMPT
        if self.settings.enable_mermaid:
            system_prompt += MERMAID_INSTRUCTION

        result = self.llm.run_tool_loop(
            system_prompt, question, LEAD_TOOLS, dispatch, self.settings.agent_max_steps
        )
        emit({"type": "compiling"})

        if result.finished_args is not None:
            answer_text = str(result.finished_args.get("answer", "")).strip()
            cited = [
                nid for nid in result.finished_args.get("cited_node_ids", []) if nid
            ]
        else:
            answer_text, cited = result.content, []

        return AgentAnswer(
            question=question,
            answer=answer_text,
            cited_node_ids=cited or dedupe(evidence),
            steps=result.steps,
        )

    def _run_subagents(
        self,
        raw_node_ids: list[Any],
        question: str,
        evidence: list[str],
        emit: Callable,
    ) -> str:
        starts = self._resolve_distinct_starts(raw_node_ids)
        if not starts:
            return (
                "no valid starting nodes resolved from those ids. Search again and pass "
                "exact node ids from the search results to explore."
            )
        emit(
            {
                "type": "subagents_spawned",
                "starts": [
                    node_ref(self.read_node(s)) for s in starts if self.read_node(s)
                ],
            }
        )

        assignments = [(start, [o for o in starts if o != start]) for start in starts]
        reports: list[dict[str, Any]] = []

        for index, (start, siblings) in enumerate(assignments, start=1):
            try:
                report = self._run_single_subagent(
                    start, siblings, question, index, emit
                )
                reports.append(report)
            except Exception as exc:
                reports.append(
                    {
                        "start": "?",
                        "answer": f"(subagent failed: {exc})",
                        "cited": [],
                    }
                )

        for report in reports:
            evidence.extend(report.get("cited", []))

        blocks = ["Subagent reports (each explored a different region):"]
        for index, report in enumerate(reports, start=1):
            cited_str = ", ".join(report.get("cited", [])) or "(none)"
            blocks.append(
                f"\n### Subagent {index} — start node: {report.get('start')}\n"
                f"{report.get('answer', '').strip()}\nEvidence node ids: {cited_str}"
            )
        return "\n".join(blocks)

    def _resolve_distinct_starts(self, raw_node_ids: list[Any]) -> list[str]:
        resolved: list[str] = []
        seen: set[str] = set()
        for raw in raw_node_ids or []:
            node = self.read_node(clean_node_ref(str(raw)))
            if node and node.id not in seen:
                seen.add(node.id)
                resolved.append(node.id)
            if len(resolved) >= self.settings.subagent_count:
                break
        return resolved

    def _run_single_subagent(
        self,
        start_id: str,
        sibling_ids: list[str],
        question: str,
        index: int,
        emit: Callable,
    ) -> dict[str, Any]:
        run = Subrun(start_id=start_id, index=index)

        start_node = self.read_node(start_id)
        if start_node:
            emit(
                {"type": "subagent_start", "agent": index, "node": node_ref(start_node)}
            )

        def dispatch(name: str, args: dict[str, Any]) -> str:
            return self._dispatch_subagent(name, args, run, emit)

        def finish_guard(_args: dict[str, Any]) -> str | None:
            if len(run.read_ids) < self.settings.subagent_min_reads:
                return (
                    f"You have read only {len(run.read_ids)} node(s); read at least "
                    f"{self.settings.subagent_min_reads} before finishing. Read another now."
                )
            return None

        siblings_str = ", ".join(sibling_ids) if sibling_ids else "(none)"
        user_prompt = (
            f"Question: {question}\n\n"
            f"Your assigned starting node: {start_id}\n"
            f"Sibling agents are covering (do NOT explore these): {siblings_str}\n\n"
            "Read your starting node first, then follow links / search within your "
            "region. Report what this region says about the question."
        )

        result = self.llm.run_tool_loop(
            SUBAGENT_SYSTEM_PROMPT,
            user_prompt,
            SUBAGENT_TOOLS,
            dispatch,
            self.settings.subagent_max_steps,
            finish_guard=finish_guard,
        )

        if result.finished_args is not None:
            answer = str(result.finished_args.get("answer", "")).strip()
            cited = [
                nid for nid in result.finished_args.get("cited_node_ids", []) if nid
            ]
        else:
            answer, cited = result.content, []

        cited = cited or dedupe(run.visited)
        emit({"type": "subagent_done", "agent": index, "cited": cited})

        return {
            "start": start_id,
            "answer": answer or "(no findings)",
            "cited": cited,
        }

    def _dispatch_subagent(
        self,
        name: str,
        args: dict[str, Any],
        run: Subrun,
        emit: Callable,
    ) -> str:
        if name == "search":
            query = str(args.get("text", ""))
            emit({"type": "search", "phase": "sub", "agent": run.index, "query": query})
            nodes = self.search(query, limit=self.settings.rerank_top_k)
            run.visited.extend(n.id for n in nodes)
            if nodes:
                run.empty_streak = 0
                return "\n".join(
                    f"- node_id: `{n.id}`\n  title: {n.title}\n  summary: {n.summary}\n"
                    f"  next_action: read this node with read(node_id='{n.id}') if relevant"
                    for n in nodes
                )
            run.empty_streak += 1
            if run.empty_streak >= self.settings.agent_patience:
                return (
                    f"no nodes found ({run.empty_streak} consecutive empty searches). Stop "
                    "searching now: call finish with the best answer supported by nodes you read."
                )
            return "no nodes found"

        if name == "read":
            requested_id = str(args.get("node_id", ""))
            cleaned_id = clean_node_ref(requested_id)
            node = self.read_node(cleaned_id)
            if node:
                if node.id in run.read_ids:
                    return f"already read {node.id} ({node.title}). Pick a DIFFERENT node, follow a link, or finish."
                if len(run.read_ids) >= self.settings.subagent_max_reads:
                    return (
                        f"read budget reached ({len(run.read_ids)}/{self.settings.subagent_max_reads} "
                        "nodes). Call finish now with what you have gathered."
                    )
                run.empty_streak = 0
                run.read_ids.add(node.id)
                run.visited.append(node.id)
                emit({"type": "read", "agent": run.index, "node": node_ref(node)})
            return format_node_full(node, requested_id, cleaned_id)

        if name == "follow_link":
            node_id = str(args.get("node_id", ""))
            pairs = self.follow_link(
                node_id, direction=str(args.get("direction", "both"))
            )
            if pairs:
                run.empty_streak = 0
            run.visited.extend(n.id for _edge, n in pairs)
            anchor = self.read_node(node_id)
            emit(
                {
                    "type": "follow_link",
                    "agent": run.index,
                    "node": (
                        node_ref(anchor)
                        if anchor
                        else {"id": node_id, "title": node_id}
                    ),
                    "neighbors": len(pairs),
                }
            )
            if not pairs:
                return "no neighbors"
            return "\n".join(
                f"- [{e.label}] {n.id} | {n.title} | {n.summary}" for e, n in pairs
            )

        return f"unknown tool: {name}"

    # -- Neighbourhood expansion ------------------------------------------

    def _expand_neighborhood(
        self, seeds: list[Node], hops: int = 2
    ) -> tuple[list[Node], list[Edge]]:
        seen_nodes = {node.id: node for node in seeds}
        seen_edges: dict[str, Edge] = {}
        frontier = list(seen_nodes)
        for _hop in range(hops):
            next_frontier: list[str] = []
            for node_id in frontier:
                for edge in self._db_get_edges_for_node(node_id):
                    seen_edges[edge.id] = edge
                    other_id = (
                        edge.target_node_id
                        if edge.source_node_id == node_id
                        else edge.source_node_id
                    )
                    if other_id in seen_nodes:
                        continue
                    other = self._db_get_node(other_id)
                    if other and other.status == NodeStatus.active:
                        seen_nodes[other_id] = other
                        next_frontier.append(other_id)
            frontier = next_frontier
        return list(seen_nodes.values()), list(seen_edges.values())

    # -- DB helpers -------------------------------------------------------

    def db_get_all_nodes(self) -> list[Node]:
        from db import Database

        return Database(self.settings.database_path).get_all_nodes()

    def db_get_all_edges(self) -> list[Edge]:
        from db import Database

        return Database(self.settings.database_path).get_all_edges()

    def _db_get_node(self, node_id: str) -> Node | None:
        from db import Database

        return Database(self.settings.database_path).get_node(node_id)

    def _keyword_search(self, text: str, limit: int) -> list[Node]:
        from db import Database

        return Database(self.settings.database_path).keyword_search(text, limit)

    def _vector_search(
        self, vector: list[float], table: str, k: int
    ) -> list[tuple[str, float]]:
        from db import Database

        return Database(self.settings.database_path).vector_search(vector, table, k)

    def _search_items_fts(self, text: str, limit: int) -> list[dict]:
        from db import Database

        return Database(self.settings.database_path).search_items_fts_query(text, limit)

    def _get_search_items(self, ids: list[str]) -> dict[str, dict]:
        from db import Database

        return Database(self.settings.database_path).get_search_items(ids)

    def _db_get_edges_for_node(self, node_id: str) -> list[Edge]:
        from db import Database

        return Database(self.settings.database_path).get_edges_for_node(node_id)

    def _db_get_outgoing_edges(
        self, node_id: str, label: str | None = None
    ) -> list[Edge]:
        from db import Database

        return Database(self.settings.database_path).get_outgoing_edges(node_id, label)

    def _db_get_incoming_edges(
        self, node_id: str, label: str | None = None
    ) -> list[Edge]:
        from db import Database

        return Database(self.settings.database_path).get_incoming_edges(node_id, label)


# ============================================================================
# 4. Write Session - All write logic lives here
# ============================================================================


class GraphWriteSession:
    """
    Pure write operations. Called only by WriteGraphService worker.
    """

    def __init__(self, runtime: SharedGraphRuntime, db: GraphDbSession):
        self.runtime = runtime
        self.db = db
        self.settings = runtime.settings

    # -- Node operations --------------------------------------------------

    def update_node(self, node_id: str, body: str) -> Node:
        old = self._db_get_node(node_id)
        if not old:
            raise KeyError(f"node not found: {node_id}")

        replacement = Node(
            id=make_node_id(body, old.original_document_name),
            body=body,
            type=old.type,
            title=old.title,
            original_document_name=old.original_document_name,
            source_path=old.source_path,
            source_version=source_hash(body),
            cluster=old.cluster,
        )

        if replacement.id == old.id:
            old.source_version = replacement.source_version
            self._fill_derived_fields(old)
            self._db_upsert_node(old)
            return old

        self._persist_node(replacement)
        self._supersede(old, replacement)
        return replacement

    def delete_node(self, node_id: str) -> None:
        self._db_delete_node(node_id)

    def create_exogenous_node(
        self, body: str, source_node_ids: list[str], origin: str | None = None
    ) -> Node:
        # Fast path for agent-authored notes: keep the markdown verbatim (mermaid
        # and all), fill only the cheap derived fields, link cited sources as
        # `reference` edges, and defer summary generation to the background. No
        # recluster -- the node lands in a fixed "Agent Notes" cluster and is
        # searchable (FTS title/body/keywords + body vector) the moment we return.
        node = Node(
            id=make_exogenous_node_id(origin or body),
            body=body,
            type=NodeType.exogenous,
            title=self._title_from_markdown(body) or self._exo_fallback_title(origin, body),
            # Leave the document name unset so every agent note groups under the
            # single "Agent Notes" library entry instead of spawning its own
            # top-level document per save.
            original_document_name=None,
            cluster="Agent Notes",
        )
        self._fill_cheap_fields(node)
        self._db_upsert_node(node)
        body_vec, _ = self._store_vectors(node)
        # Place the note in a real topic cluster instead of a static bucket.
        node.cluster = self._cluster_for_references(node.id, source_node_ids, body_vec)
        self._db_upsert_node(node)
        self._link_references(node, source_node_ids)
        if self.runtime.enrich is not None:
            self.runtime.enrich.enqueue_summary(node.id)
        return node

    def create_document_node(
        self,
        body: str,
        title: str | None = None,
        document_name: str | None = None,
        source_path: str | None = None,
        source_ranges: list[tuple[int, int]] | None = None,
    ) -> Node:
        body = body.strip()
        if not body:
            raise ValueError("document body is empty")

        inferred_title = title or self._title_from_markdown(body)
        doc_name = self._document_name(
            document_name or inferred_title or f"uploaded-{short_hash(body)}.md"
        )
        line_count = max(1, len(body.splitlines()))
        ranges = source_ranges if source_ranges is not None else [(1, line_count)]
        version = source_hash(body)

        node = Node(
            id=make_node_id(body, doc_name),
            body=body,
            type=NodeType.endogenous,
            title=inferred_title or doc_name,
            original_document_name=doc_name,
            source_path=source_path,
            source_ranges=ranges,
            source_version=version,
            source_material_hash=source_hash(body),
            cluster="Uploaded Documents",
        )

        active_old = [
            n
            for n in self._db_get_nodes_by_document(doc_name, active_only=True)
            if n.type == NodeType.endogenous
        ]

        for old in active_old:
            old_hash = old.source_material_hash or source_hash(old.body)
            if old_hash == node.source_material_hash:
                old.source_version = version
                old.source_material_hash = old_hash
                self._db_upsert_node(old)
                self._ingest_one(old)
                self._db_record_source(doc_name, version)
                return old

        self._fill_cheap_fields(node)
        replacements: dict[str, str] = {}
        stale_sources: set[str] = set()
        actions: list[str] = []

        backfilled_old = [self._backfill_revision_metadata(n) for n in active_old]
        best = max(
            ((old, match_score(old, node)) for old in backfilled_old),
            key=lambda item: item[1],
            default=None,
        )

        self._persist_node(node, cheap=True)

        matched_old_id: str | None = None
        if best is not None and best[1] >= _CASCADE_MATCH_THRESHOLD:
            matched_old_id = best[0].id
            self._supersede(best[0], node)
            replacements[best[0].id] = node.id
            actions.append(f"superseded:{best[0].id}->{node.id}")

        for old in active_old:
            if old.id == matched_old_id:
                continue
            self._db_set_node_status(old.id, NodeStatus.stale)
            stale_sources.add(old.id)
            actions.append(f"stale:{old.id}")

        self._replace_structural_edges(doc_name, [])
        self._db_record_source(doc_name, version)
        # Defer the expensive graph-wide bookkeeping so the UI add returns fast.
        # The node + its semantic edges are already committed and searchable;
        # duplicate-merge, cascade regen, and reclustering catch up in the
        # background (recluster only fires every N endogenous adds).
        if self.runtime.enrich is not None:
            self.runtime.enrich.enqueue_cascade(replacements, list(stale_sources))
            self.runtime.enrich.enqueue_entity_dedup(node.id)
            self.runtime.enrich.note_endogenous_added()
        else:
            self._cascade_dependents(replacements, stale_sources, actions)
            self._refresh_clusters()
        return node

    # -- Structural graph operations --------------------------------------

    def recluster(self, resolution: float = 1.0) -> dict[str, str]:
        return self._recluster(resolution=resolution, persist=True)

    def ensure_japanese_clusters(self) -> dict[str, str]:
        return self._ensure_japanese_clusters()

    # -- Ingest / cascade -------------------------------------------------

    def ingest_md_output(self, md_output_dir: str | Path) -> list[Node]:
        out_path = Path(md_output_dir)
        if not out_path.exists():
            raise FileNotFoundError(f"input directory does not exist: {out_path}")

        nodes, structural_edges = self._load_md_output(out_path)
        if not nodes:
            return []

        document_name = nodes[0].original_document_name or out_path.name
        version = self._source_version_for_nodes(nodes)

        edge_count = 0
        for index, node in enumerate(nodes, start=1):
            node.source_version = version
            edges = self._ingest_one(node)
            edge_count += len(edges)
            log.info(
                "ingest %d/%d | edges so far %d | %s",
                index,
                len(nodes),
                edge_count,
                node.id,
            )

        if nodes:
            self._replace_structural_edges(document_name, structural_edges)
            if document_name:
                self._db_record_source(document_name, version)

        log.info(
            "ingest done: %d nodes, %d semantic/dedup edges, %d structural",
            len(nodes),
            edge_count,
            len(structural_edges),
        )

        try:
            mapping = self.recluster()
            self.ensure_japanese_clusters()
            log.info("reclustered into %d topics", len(set(mapping.values())))
        except Exception as exc:
            log.info("recluster skipped: %s", exc)

        return nodes

    def cascading_update(self, source_file: str | Path) -> list[str]:
        out_path = Path(source_file)
        if not out_path.exists():
            raise FileNotFoundError(f"source does not exist: {out_path}")

        nodes, structural_edges = self._load_md_output(out_path)
        if not nodes:
            return []

        document_name = nodes[0].original_document_name or out_path.name
        version = self._source_version_for_nodes(nodes)

        # Cheap pass: stamp version + body hash
        for node in nodes:
            node.source_version = version
            if not node.source_material_hash:
                node.source_material_hash = source_hash(node.body)

        active_old = [
            n
            for n in self._db_get_nodes_by_document(document_name, active_only=True)
            if n.type == NodeType.endogenous
        ]

        if not active_old:
            for node in nodes:
                self._persist_node(node)
            self._replace_structural_edges(document_name, structural_edges)
            self._db_record_source(document_name, version)
            return [f"ingested-new:{n.id}" for n in nodes]

        actions: list[str] = []
        replacements: dict[str, str] = {}
        stale_sources: set[str] = set()
        matched_old: set[str] = set()
        exact_by_hash: dict[str, Node] = {}
        for old in active_old:
            exact_by_hash.setdefault(
                old.source_material_hash or source_hash(old.body), old
            )

        pending: list[Node] = []
        for node in nodes:
            exact = exact_by_hash.get(node.source_material_hash)
            if exact and exact.id not in matched_old:
                matched_old.add(exact.id)
                actions.append(f"unchanged:{exact.id}")
            else:
                pending.append(node)

        for node in pending:
            self._fill_derived_fields(node)

        unmatched_old = [
            self._backfill_revision_metadata(old)
            for old in active_old
            if old.id not in matched_old
        ]

        for node in pending:
            candidates = [old for old in unmatched_old if old.id not in matched_old]
            best = max(
                ((c, match_score(c, node)) for c in candidates),
                key=lambda item: item[1],
                default=None,
            )
            if best is None or best[1] < _CASCADE_MATCH_THRESHOLD:
                self._persist_node(node)
                actions.append(f"new:{node.id}")
                continue
            old = best[0]
            matched_old.add(old.id)
            if claims_equivalent(old, node):
                actions.append(f"remapped:{old.id}")
                continue
            self._persist_node(node)
            self._supersede(old, node)
            replacements[old.id] = node.id
            actions.append(f"superseded:{old.id}->{node.id}")

        for old in active_old:
            if old.id not in matched_old:
                self._db_set_node_status(old.id, NodeStatus.stale)
                stale_sources.add(old.id)
                actions.append(f"stale:{old.id}")

        self._cascade_dependents(replacements, stale_sources, actions)
        self._replace_structural_edges(document_name, structural_edges)
        self._db_record_source(document_name, version)

        try:
            self.recluster()
            self.ensure_japanese_clusters()
        except Exception as exc:
            log.info("recluster skipped: %s", exc)

        return actions

    def _ingest_one(self, node: Node) -> list[Edge]:
        existing = self._db_get_node(node.id)
        complete = (
            existing is not None
            and existing.status == NodeStatus.active
            and self._db_has_vector(node.id)
        )
        if complete:
            return []

        self._fill_derived_fields(node)
        self._db_upsert_node(node)
        body_vec, summary_vec = self._store_vectors(node)
        edges = self._build_semantic_edges(node, body_vec, summary_vec)

        if self.settings.entity_dedup:
            candidates = self._knn_candidates(node.id, body_vec, summary_vec)
            edges += self._link_entity_duplicates(node, candidates)

        return edges

    # -- DB helpers -------------------------------------------------------

    def _db_conn(self):
        return self.db.conn

    def _db_get_node(self, node_id: str) -> Node | None:
        from db import Database

        return Database(self.settings.database_path).get_node(node_id)

    def _db_upsert_node(self, node: Node) -> None:
        from db import Database

        Database(self.settings.database_path).upsert_node(node)

    def _db_delete_node(self, node_id: str) -> None:
        from db import Database

        Database(self.settings.database_path).delete_node(node_id)

    def _db_set_node_status(self, node_id: str, status: NodeStatus) -> None:
        from db import Database

        Database(self.settings.database_path).set_node_status(node_id, status)

    def _db_get_nodes_by_document(
        self, doc_name: str, active_only: bool = False
    ) -> list[Node]:
        from db import Database

        return Database(self.settings.database_path).get_nodes_by_document(
            doc_name, active_only
        )

    def _db_upsert_edge(self, edge: Edge) -> None:
        from db import Database

        Database(self.settings.database_path).upsert_edge(edge)

    def _db_delete_edges_by_label_for_nodes(
        self, label: str, node_ids: set[str]
    ) -> None:
        from db import Database

        Database(self.settings.database_path).delete_edges_by_label_for_nodes(
            label, node_ids
        )

    def _db_record_source(self, doc_name: str, version: str) -> None:
        from db import Database

        Database(self.settings.database_path).record_source(doc_name, version)

    def _db_get_source(self, doc_name: str) -> tuple[str, str] | None:
        from db import Database

        return Database(self.settings.database_path).get_source(doc_name)

    def _db_ensure_vec_tables(self, dim: int) -> None:
        from db import Database

        Database(self.settings.database_path).ensure_vec_tables(dim)

    def _db_reset_vec_tables(self) -> None:
        from db import Database

        Database(self.settings.database_path).reset_vec_tables()

    def _db_has_vector(self, node_id: str) -> bool:
        from db import Database

        return Database(self.settings.database_path).has_vector(node_id)

    def _db_count_vectors(self, table: str) -> int:
        from db import Database

        return Database(self.settings.database_path).count_vectors(table)

    def _db_set_vector(self, node_id: str, table: str, vector: list[float]) -> None:
        from db import Database

        Database(self.settings.database_path).set_vector(node_id, table, vector)

    def _db_replace_search_items(self, node_id: str, items: list[dict]) -> None:
        from db import Database

        Database(self.settings.database_path).replace_search_items(node_id, items)

    def _db_delete_search_items(self, node_id: str) -> None:
        from db import Database

        Database(self.settings.database_path).delete_search_items(node_id)

    def _db_set_search_item_vector(self, item_id: str, vector: list[float]) -> None:
        from db import Database

        Database(self.settings.database_path).set_search_item_vector(item_id, vector)

    def _db_get_vector(self, node_id: str, table: str) -> list[float] | None:
        from db import Database

        return Database(self.settings.database_path).get_vector(node_id, table)

    def _db_get_meta(self, key: str) -> str | None:
        from db import Database

        return Database(self.settings.database_path).get_meta(key)

    def _db_set_meta(self, key: str, value: str) -> None:
        from db import Database

        Database(self.settings.database_path).set_meta(key, value)

    def _db_get_all_nodes(self) -> list[Node]:
        from db import Database

        return Database(self.settings.database_path).get_all_nodes()

    def _db_get_all_edges(self) -> list[Edge]:
        from db import Database

        return Database(self.settings.database_path).get_all_edges()

    def _db_get_edges_for_node(self, node_id: str) -> list[Edge]:
        from db import Database

        return Database(self.settings.database_path).get_edges_for_node(node_id)

    def _db_get_outgoing_edges(
        self, node_id: str, label: str | None = None
    ) -> list[Edge]:
        from db import Database

        return Database(self.settings.database_path).get_outgoing_edges(node_id, label)

    def _db_get_incoming_edges(
        self, node_id: str, label: str | None = None
    ) -> list[Edge]:
        from db import Database

        return Database(self.settings.database_path).get_incoming_edges(node_id, label)

    # -- Embedding helpers ------------------------------------------------

    def _ensure_vec(self) -> None:
        self._db_ensure_vec_tables(self.runtime.embedder.dim)

    def _store_vectors(self, node: Node) -> tuple[list[float], list[float] | None]:
        self._ensure_vec()
        body_vec = self.runtime.embedder.embed_document(node.body)
        self._db_set_vector(node.id, "vec_body", body_vec)
        summary_vec = None
        if node.summary.strip():
            summary_vec = self.runtime.embedder.embed_document(node.summary)
            self._db_set_vector(node.id, "vec_summary", summary_vec)
        self._store_search_items(node)
        return body_vec, summary_vec

    def _build_search_items(self, node: Node) -> list[dict]:
        """Derive evidence rows for a node: title, summary, each claim, plus
        big (3000/300) and small (512/80) overlapping body chunks. Each row gets
        a deterministic id from ``short_hash(node|field|ordinal)``."""
        s = self.settings
        items: list[dict] = []

        def add(field: str, text: str, ordinal: int, start, end) -> None:
            clean = (text or "").strip()
            if not clean:
                return
            items.append(
                {
                    "id": short_hash(f"{node.id}|{field}|{ordinal}"),
                    "node_id": node.id,
                    "field": field,
                    "text": clean,
                    "ordinal": ordinal,
                    "start_char": start,
                    "end_char": end,
                    "source_path": node.source_path,
                    "source_hash": node.source_material_hash,
                }
            )

        add("title", node.title, 0, None, None)
        add("summary", node.summary, 0, None, None)
        for i, claim in enumerate(node.claims):
            add("claim", claim, i, None, None)
        body = node.body or ""
        for i, (start, end, chunk) in enumerate(
            chunk_text(body, s.search_big_chunk_size, s.search_big_chunk_overlap)
        ):
            add("big_chunk", chunk, i, start, end)
        for i, (start, end, chunk) in enumerate(
            chunk_text(body, s.search_small_chunk_size, s.search_small_chunk_overlap)
        ):
            add("small_chunk", chunk, i, start, end)
        return items

    def _store_search_items(self, node: Node) -> None:
        """Write FTS rows + per-item vectors for a node's evidence items.
        Best-effort: never let search-index maintenance break ingestion."""
        try:
            self._ensure_vec()
            items = self._build_search_items(node)
            self._db_replace_search_items(node.id, items)
            if not items:
                return
            texts = [it["text"] for it in items]
            try:
                vectors = self.runtime.embedder.embed_documents(texts)
            except Exception as exc:
                log.info("batch item embed failed; per-item fallback: %s", exc)
                vectors = []
                for text in texts:
                    try:
                        vectors.append(self.runtime.embedder.embed_document(text))
                    except Exception as exc2:
                        log.info("item embed failed: %s", exc2)
                        vectors.append(None)
            for item, vector in zip(items, vectors):
                if vector is None:
                    continue
                try:
                    self._db_set_search_item_vector(item["id"], vector)
                except Exception as exc:
                    log.info("set item vector failed %s: %s", item["id"], exc)
        except Exception as exc:
            log.info("store search items failed for %s: %s", node.id, exc)

    def _knn_candidates(
        self,
        node_id: str,
        body_vec: list[float],
        summary_vec: list[float] | None,
        k: int | None = None,
    ) -> list[Node]:
        if k is None:
            k = self.settings.edge_candidate_k
        ranked: list[str] = []
        probes = [("vec_body", body_vec)] + (
            [("vec_summary", summary_vec)] if summary_vec else []
        )
        for table, vector in probes:
            for candidate_id, _distance in Database(
                self.settings.database_path
            ).vector_search(vector, table, k + 1):
                if candidate_id != node_id and candidate_id not in ranked:
                    ranked.append(candidate_id)
        candidates = [
            node
            for node in (self._db_get_node(cid) for cid in ranked)
            if node and node.status == NodeStatus.active
        ]
        return self._collapse_same_as(candidates)[:k]

    # -- Entity dedup -----------------------------------------------------

    def _link_entity_duplicates(self, node: Node, candidates: list[Node]) -> list[Edge]:
        if not candidates:
            return []
        payload = {
            "new_node": {
                "id": node.id,
                "title": node.title,
                "entity": node.entity,
                "summary": node.summary,
            },
            "candidates": [
                {"id": c.id, "title": c.title, "entity": c.entity, "summary": c.summary}
                for c in candidates
            ],
        }
        result = self.runtime.llm.complete_structured(
            ENTITY_DEDUP_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            EntityMatch,
        )
        match = (
            result
            if isinstance(result, EntityMatch)
            else EntityMatch.model_validate(result)
        )
        allowed = {c.id for c in candidates}
        if (
            not match.is_same
            or match.target_node_id not in allowed
            or match.target_node_id == node.id
        ):
            return []

        stamp = now_iso()
        episodes = [node.id, match.target_node_id]
        edges: list[Edge] = []
        for src, dst in (
            (node.id, match.target_node_id),
            (match.target_node_id, node.id),
        ):
            edge = Edge(
                id=make_edge_id(src, dst, "same-as"),
                source_node_id=src,
                target_node_id=dst,
                label="same-as",
                summary="Same real-world entity.",
                valid_at=stamp,
                source_episode_ids=episodes,
            )
            self._db_upsert_edge(edge)
            edges.append(edge)
        return edges

    def _collapse_same_as(self, nodes: list[Node]) -> list[Node]:
        kept: list[Node] = []
        seen: set[str] = set()
        for node in nodes:
            if node.id in seen:
                continue
            kept.append(node)
            group = {node.id}
            for edge in self._db_get_edges_for_node(node.id):
                if edge.label == "same-as":
                    other = (
                        edge.target_node_id
                        if edge.source_node_id == node.id
                        else edge.source_node_id
                    )
                    group.add(other)
            seen |= group
        return kept

    # -- Enrichment -------------------------------------------------------

    def _fill_derived_fields(self, node: Node) -> Node:
        if not node.source_material_hash:
            node.source_material_hash = source_hash(node.body)
        if not node.summary.strip() and node.body.strip():
            node.summary = self.runtime.llm.complete(SUMMARY_PROMPT, node.body).strip()
        if not node.keywords:
            node.keywords = self._extract_keywords(node.body)
        if not node.claims:
            extracted = self._extract_claims(node.body)
            node.entity = node.entity or extracted.entity
            node.claims = extracted.claims
        if not node.entity and node.keywords:
            node.entity = node.keywords[0]
        return node

    def _extract_keywords(self, text: str) -> list[str]:
        if not text.strip():
            return []
        result = self.runtime.llm.complete_structured(
            KEYWORD_PROMPT, text[:8000], Keywords
        )
        parsed = (
            result if isinstance(result, Keywords) else Keywords.model_validate(result)
        )
        kept: list[str] = []
        seen: set[str] = set()
        for kw in parsed.keywords:
            kw = kw.strip()
            if kw and kw.lower() not in seen:
                kept.append(kw)
                seen.add(kw.lower())
        return kept[:12]

    def _extract_claims(self, text: str) -> ClaimExtraction:
        if not text.strip():
            return ClaimExtraction()
        result = self.runtime.llm.complete_structured(
            CLAIM_PROMPT, text[:12000], ClaimExtraction
        )
        parsed = (
            result
            if isinstance(result, ClaimExtraction)
            else ClaimExtraction.model_validate(result)
        )
        claims: list[str] = []
        seen: set[str] = set()
        for claim in parsed.claims:
            claim = " ".join(claim.strip().split())
            if claim and claim.lower() not in seen:
                seen.add(claim.lower())
                claims.append(claim)
        return ClaimExtraction(
            entity=" ".join(parsed.entity.strip().split()), claims=claims[:20]
        )

    # -- Semantic edges ---------------------------------------------------

    def _build_semantic_edges(
        self, node: Node, body_vec: list[float], summary_vec: list[float] | None
    ) -> list[Edge]:
        candidates = self._knn_candidates(node.id, body_vec, summary_vec)
        if not candidates:
            return []

        payload = {
            "new_node": {
                "id": node.id,
                "title": node.title,
                "summary": node.summary,
                "keywords": node.keywords,
                "body": node.body[:4000],
            },
            "candidates": [
                {
                    "id": c.id,
                    "title": c.title,
                    "summary": c.summary,
                    "keywords": c.keywords,
                    "body": c.body[:1200],
                }
                for c in candidates
            ],
        }
        result = self.runtime.llm.complete_structured(
            EDGE_PROMPT, json.dumps(payload, ensure_ascii=False), EdgeSuggestions
        )
        parsed = (
            result
            if isinstance(result, EdgeSuggestions)
            else EdgeSuggestions.model_validate(result)
        )
        allowed = {c.id for c in candidates}

        edges: list[Edge] = []
        for suggestion in parsed.edges:
            target_id = suggestion.target_node_id
            if target_id not in allowed or target_id == node.id:
                continue
            label = suggestion.label.strip() or "related"
            stamp = now_iso()
            if label == "contradicts":
                self._invalidate_prior_edges(node.id, target_id, stamp)
            episodes = [node.id, target_id]
            for src, dst in ((node.id, target_id), (target_id, node.id)):
                edge = Edge(
                    id=make_edge_id(src, dst, label),
                    source_node_id=src,
                    target_node_id=dst,
                    label=label,
                    summary=suggestion.summary.strip(),
                    valid_at=stamp,
                    source_episode_ids=episodes,
                )
                self._db_upsert_edge(edge)
                edges.append(edge)
        return edges

    def _invalidate_prior_edges(
        self, source_id: str, target_id: str, stamp: str
    ) -> None:
        for edge in self._db_get_edges_for_node(target_id):
            if {edge.source_node_id, edge.target_node_id} != {source_id, target_id}:
                continue
            if edge.label == "contradicts" or edge.invalid_at:
                continue
            edge.invalid_at = stamp
            edge.expired_at = stamp
            self._db_upsert_edge(edge)

    # -- Supersede / persist / link ---------------------------------------

    def _persist_node(self, node: Node, *, cheap: bool = False) -> Node:
        (self._fill_cheap_fields if cheap else self._fill_derived_fields)(node)
        self._db_upsert_node(node)
        body_vec, summary_vec = self._store_vectors(node)
        self._build_semantic_edges(node, body_vec, summary_vec)
        return node

    def _supersede(self, old: Node, new: Node) -> None:
        self._db_upsert_edge(
            Edge(
                id=make_edge_id(old.id, new.id, "superseded_by"),
                source_node_id=old.id,
                target_node_id=new.id,
                label="superseded_by",
                summary="Newer source material replaces these facts.",
            )
        )
        self._db_upsert_edge(
            Edge(
                id=make_edge_id(new.id, old.id, "supersedes"),
                source_node_id=new.id,
                target_node_id=old.id,
                label="supersedes",
                summary="Older source material replaced by this node.",
            )
        )
        self._db_set_node_status(old.id, NodeStatus.superseded)
        # Superseded facts should no longer surface as evidence.
        try:
            self._db_delete_search_items(old.id)
        except Exception as exc:
            log.info("clear search items on supersede failed %s: %s", old.id, exc)

    def _link_supports(self, node: Node, source_node_ids: list[str]) -> None:
        for source_id in source_node_ids:
            if not self._db_get_node(source_id):
                continue
            self._db_upsert_edge(
                Edge(
                    id=make_edge_id(source_id, node.id, "supports"),
                    source_node_id=source_id,
                    target_node_id=node.id,
                    label="supports",
                    summary="Source node supports this derived node.",
                )
            )

    # -- Fast-add helpers + background enrichment -------------------------

    def _fill_cheap_fields(self, node: Node) -> Node:
        """Derived fields cheap enough for the synchronous fast-add path:
        keywords + claims + entity. Skips the summary (1 LLM call) which is
        deferred to the EnrichmentQueue."""
        if not node.source_material_hash:
            node.source_material_hash = source_hash(node.body)
        if not node.keywords:
            node.keywords = self._extract_keywords(node.body)
        if not node.claims:
            extracted = self._extract_claims(node.body)
            node.entity = node.entity or extracted.entity
            node.claims = extracted.claims
        if not node.entity and node.keywords:
            node.entity = node.keywords[0]
        return node

    def _link_references(self, node: Node, cited_node_ids: list[str]) -> None:
        """Edge from a derived note to each source it cited (label `reference`).
        These render the note's provenance path immediately after add."""
        for source_id in dedupe([cid for cid in cited_node_ids if cid]):
            if not self._db_get_node(source_id):
                continue
            self._db_upsert_edge(
                Edge(
                    id=make_edge_id(node.id, source_id, "reference"),
                    source_node_id=node.id,
                    target_node_id=source_id,
                    label="reference",
                    summary="Derived note references this source node.",
                )
            )

    def _cluster_for_references(
        self, node_id: str, source_node_ids: list[str], body_vec: list[float]
    ) -> str:
        """Cluster for an agent note: the majority cluster among the sources it
        cited; ties (or no cited clusters) broken by the nearest node in
        embedding space. Falls back to 'Agent Notes' when nothing is clustered
        yet. A later recluster (every N endo adds) may refine this."""
        cited_clusters = [
            n.cluster
            for n in (self._db_get_node(sid) for sid in dedupe(source_node_ids))
            if n and n.status == NodeStatus.active and (n.cluster or "").strip()
        ]
        if cited_clusters:
            counts = Counter(cited_clusters)
            top = counts.most_common()
            best = top[0][1]
            tied = {c for c, freq in top if freq == best}
            if len(tied) == 1:
                return next(iter(tied))
            # Tie-break: nearest node (by body vector) whose cluster is tied.
            return (
                self._nearest_cluster(node_id, body_vec, allowed=tied)
                or sorted(tied)[0]
            )
        # No cited clusters: pure embedding neighbour.
        return self._nearest_cluster(node_id, body_vec) or "Agent Notes"

    def _nearest_cluster(
        self, node_id: str, body_vec: list[float], allowed: set[str] | None = None
    ) -> str | None:
        for cid, _dist in Database(self.settings.database_path).vector_search(
            body_vec, "vec_body", 50
        ):
            if cid == node_id:
                continue
            nn = self._db_get_node(cid)
            if not nn or nn.status != NodeStatus.active:
                continue
            cluster = (nn.cluster or "").strip()
            if cluster and (allowed is None or cluster in allowed):
                return cluster
        return None

    # Methods below are driven by the background EnrichmentQueue
    # (graph/enrich.py); each runs in its own short-lived write session.

    def get_meta(self, key: str) -> str | None:
        return self._db_get_meta(key)

    def set_meta(self, key: str, value: str) -> None:
        self._db_set_meta(key, value)

    def refresh_clusters(self) -> None:
        self._refresh_clusters()

    def enrich_summary(self, node_id: str) -> None:
        node = self._db_get_node(node_id)
        if not node or node.status != NodeStatus.active or node.summary.strip():
            return
        body = node.body.strip()
        if not body:
            return
        node.summary = self.runtime.llm.complete(SUMMARY_PROMPT, body).strip()
        node.updated_at = now_iso()
        self._db_upsert_node(node)
        if node.summary.strip():
            self._ensure_vec()
            self._db_set_vector(
                node.id,
                "vec_summary",
                self.runtime.embedder.embed_document(node.summary),
            )
            # Rebuild search items now that a summary field exists.
            self._store_search_items(node)

    def enrich_entity_dedup(self, node_id: str) -> None:
        node = self._db_get_node(node_id)
        if not node or node.status != NodeStatus.active:
            return
        body_vec = self._db_get_vector(node.id, "vec_body")
        if body_vec is None:
            body_vec = self.runtime.embedder.embed_document(node.body)
        summary_vec = self._db_get_vector(node.id, "vec_summary")
        candidates = self._knn_candidates(node.id, body_vec, summary_vec)
        self._link_entity_duplicates(node, candidates)

    def enrich_cascade(
        self, replacements: dict[str, str], stale_sources: list[str]
    ) -> None:
        actions: list[str] = []
        self._cascade_dependents(replacements, set(stale_sources), actions)

    def _backfill_revision_metadata(self, node: Node) -> Node:
        changed = False
        if not node.source_material_hash:
            node.source_material_hash = source_hash(node.body)
            changed = True
        if not node.claims:
            extracted = self._extract_claims(node.body)
            node.entity = node.entity or extracted.entity
            node.claims = extracted.claims
            changed = True
        if not node.entity and node.keywords:
            node.entity = node.keywords[0]
            changed = True
        if changed:
            self._db_upsert_node(node)
        return node

    def _source_version_for_nodes(self, nodes: list[Node]) -> str:
        if not nodes:
            return source_hash("")
        parts: list[str] = []
        for node in nodes:
            if node.source_path and Path(node.source_path).exists():
                parts.append(
                    Path(node.source_path).read_text(encoding="utf-8", errors="ignore")
                )
            else:
                parts.append(node.body)
        return source_hash("\n\n--- NODE BREAK ---\n\n".join(parts))

    def _replace_structural_edges(
        self, document_name: str | None, edges: list[Edge]
    ) -> None:
        if document_name:
            node_ids = {
                n.id
                for n in self._db_get_nodes_by_document(document_name)
                if n.type == NodeType.endogenous
            }
            self._db_delete_edges_by_label_for_nodes("follows", node_ids)
        for edge in edges:
            source = self._db_get_node(edge.source_node_id)
            target = self._db_get_node(edge.target_node_id)
            if (
                source
                and target
                and source.status == NodeStatus.active
                and target.status == NodeStatus.active
            ):
                self._db_upsert_edge(edge)

    # -- Cascade ----------------------------------------------------------

    def _cascade_dependents(
        self, replacements: dict[str, str], stale_sources: set[str], actions: list[str]
    ) -> None:
        max_hops = max(0, self.settings.cascade_max_hops)
        max_nodes = max(0, self.settings.cascade_max_nodes)
        if max_hops == 0 or max_nodes == 0:
            if replacements or stale_sources:
                actions.append("cascade-skipped:disabled")
            return

        frontier: deque[tuple[str, int]] = deque(
            (nid, 0) for nid in sorted(set(replacements) | set(stale_sources))
        )
        visited: set[str] = set()
        processed = 0

        while frontier:
            changed_id, depth = frontier.popleft()
            target_depth = depth + 1
            if target_depth > max_hops:
                continue
            for edge in self._db_get_outgoing_edges(changed_id, "supports"):
                target = self._db_get_node(edge.target_node_id)
                if (
                    not target
                    or target.status != NodeStatus.active
                    or target.type != NodeType.exogenous
                    or target.id in visited
                ):
                    continue
                if processed >= max_nodes:
                    actions.append(
                        f"cascade-cap-hit:max_nodes={max_nodes}:at={target.id}"
                    )
                    return
                visited.add(target.id)
                processed += 1

                support_nodes = self._current_support_nodes(target, replacements)
                replacement = (
                    self._regenerate_exogenous_node(target, support_nodes)
                    if support_nodes
                    else None
                )
                if replacement is None:
                    self._db_set_node_status(target.id, NodeStatus.stale)
                    actions.append(f"stale-exogenous:{target.id}")
                else:
                    replacements[target.id] = replacement.id
                    actions.append(
                        f"regenerated-exogenous:{target.id}->{replacement.id}"
                    )
                if target_depth < max_hops:
                    frontier.append((target.id, target_depth))

    def _current_support_nodes(
        self, node: Node, replacements: dict[str, str]
    ) -> list[Node]:
        support_nodes: dict[str, Node] = {}
        for edge in self._db_get_incoming_edges(node.id, "supports"):
            source_id = replacements.get(edge.source_node_id, edge.source_node_id)
            source = self._db_get_node(source_id)
            if source and source.status == NodeStatus.superseded:
                for swap in self._db_get_outgoing_edges(source.id, "superseded_by"):
                    target = self._db_get_node(swap.target_node_id)
                    if target and target.status == NodeStatus.active:
                        source = target
                        break
            if source and source.status == NodeStatus.active:
                support_nodes[source.id] = source
        return self._collapse_same_as(list(support_nodes.values()))

    def _regenerate_exogenous_node(
        self, old: Node, support_nodes: list[Node]
    ) -> Node | None:
        if not support_nodes:
            return None
        payload = {
            "previous_node": {
                "id": old.id,
                "title": old.title,
                "summary": old.summary,
                "body": old.body[:4000],
            },
            "current_support_material": [
                {
                    "id": n.id,
                    "title": n.title,
                    "summary": n.summary,
                    "body": n.body[:2500],
                }
                for n in support_nodes[:8]
            ],
        }
        body = self.runtime.llm.complete(
            REGENERATE_EXOGENOUS_PROMPT, json.dumps(payload, ensure_ascii=False)
        ).strip()
        if not body:
            return None

        support_ids = sorted(n.id for n in support_nodes)
        version = source_hash(
            "|".join(
                [
                    source_hash(body),
                    *support_ids,
                    *(n.source_version or "" for n in support_nodes),
                ]
            )
        )
        replacement = Node(
            id=make_exogenous_node_id(f"{old.id}|{version}|{body}"),
            body=body,
            type=NodeType.exogenous,
            title=old.title,
            original_document_name=old.original_document_name,
            source_version=version,
            cluster=old.cluster,
        )
        if replacement.id == old.id:
            return old
        self._persist_node(replacement)
        self._link_supports(replacement, [n.id for n in support_nodes])
        self._supersede(old, replacement)
        return replacement

    # -- Clustering -------------------------------------------------------

    def _recluster(
        self, resolution: float = 1.0, seed: int = 42, persist: bool = True
    ) -> dict[str, str]:
        import networkx as nx

        nodes = [n for n in self._db_get_all_nodes() if n.status == NodeStatus.active]
        node_by_id = {n.id: n for n in nodes}
        graph = nx.Graph()
        graph.add_nodes_from(node_by_id)
        for edge in self._db_get_all_edges():
            src, dst = edge.source_node_id, edge.target_node_id
            if src not in node_by_id or dst not in node_by_id or src == dst:
                continue
            if graph.has_edge(src, dst):
                graph[src][dst]["weight"] += 1.0
            else:
                graph.add_edge(src, dst, weight=1.0)

        communities = nx.community.louvain_communities(
            graph, weight="weight", resolution=resolution, seed=seed
        )
        ordered = sorted(communities, key=len, reverse=True)

        per_comm: list[Counter[str]] = []
        titles: list[list[str]] = []
        doc_freq: Counter[str] = Counter()
        for members in ordered:
            counts: Counter[str] = Counter()
            comm_titles: list[str] = []
            for nid in members:
                node = node_by_id.get(nid)
                if node:
                    counts.update(k.lower().strip() for k in node.keywords if k.strip())
                    if node.title:
                        comm_titles.append(node.title)
            per_comm.append(counts)
            titles.append(comm_titles)
            doc_freq.update(counts.keys())

        n_comms = max(len(ordered), 1)
        mapping: dict[str, str] = {}
        used: Counter[str] = Counter()
        used_labels: list[str] = []
        print(
            f"[recluster] {len(ordered)} communities -> naming each via LLM", flush=True
        )
        for index, members in enumerate(ordered):
            keywords = self._tfidf_keywords(per_comm[index], doc_freq, n_comms, k=8)
            print(
                f"[recluster] naming community {index + 1}/{len(ordered)} ({len(members)} nodes)",
                flush=True,
            )
            label = self._name_cluster(keywords, titles[index][:12], used_labels)
            used[label] += 1
            if used[label] > 1:
                label = f"{label} {used[label]}"
            used_labels.append(label)
            for nid in members:
                mapping[nid] = label

        if persist:
            for node in nodes:
                new_label = mapping.get(node.id)
                if new_label and node.cluster != new_label:
                    node.cluster = new_label
                    self._db_upsert_node(node)
        return mapping

    def _tfidf_keywords(
        self, counts: Counter[str], doc_freq: Counter[str], n_comms: int, k: int = 5
    ) -> list[str]:
        if not counts:
            return []
        import math

        scored = sorted(
            counts.items(),
            key=lambda kv: kv[1] * math.log(1 + n_comms / max(doc_freq[kv[0]], 1)),
            reverse=True,
        )
        return [kw for kw, _ in scored[:k]]

    def _name_cluster(
        self, keywords: list[str], titles: list[str], used_names: list[str]
    ) -> str:
        import re

        def has_japanese(text: str) -> bool:
            return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text))

        if not keywords and not titles:
            raise SystemExit(
                "fatal: cluster naming failed: no keywords or titles were available"
            )

        user = (
            f"キーワード: {', '.join(keywords) or '(なし)'}\n"
            f"サンプルタイトル: {'; '.join(titles) or '(なし)'}\n"
            f"避けるべき既存の名前: {', '.join(used_names) or '(なし)'}\n\n"
            "必ず日本語のクラスタ名を1つだけ返してください。\n"
            "英語のみの名前は禁止です。\n"
            "少なくとも1文字以上の日本語文字、つまりひらがな、カタカナ、または漢字を含めてください。\n"
            "API名、関数名、ライブラリ名、頭字語、識別子は必要な場合のみ原文のまま残してかまいません。\n"
            "説明、引用符、句読点、箇条書きは不要です。\n\n"
            "日本語クラスタ名:"
        )

        try:
            raw = self.runtime.llm.complete(CLUSTER_NAMER_SYSTEM, user)
        except Exception as exc:
            raise SystemExit(
                f"fatal: cluster naming failed: LLM call raised {type(exc).__name__}: {exc}"
            ) from exc

        name = " ".join(raw.strip().strip("\"'").split())

        if not name:
            raise SystemExit(
                f"fatal: cluster naming failed: empty LLM response; "
                f"keywords={keywords!r}; titles={titles[:5]!r}"
            )

        if len(name) > 60:
            raise SystemExit(f"fatal: cluster naming failed: name too long: {name!r}")

        if len(name.split()) > 6:
            raise SystemExit(f"fatal: cluster naming failed: too many words: {name!r}")

        if name.lower() in {u.lower() for u in used_names}:
            raise SystemExit(
                f"fatal: cluster naming failed: duplicate cluster name: {name!r}"
            )

        if not has_japanese(name):
            raise SystemExit(
                "fatal: cluster naming failed: LLM returned English-only/non-Japanese name: "
                f"{name!r}; keywords={keywords!r}; titles={titles[:5]!r}"
            )

        return name

    def _ensure_japanese_clusters(self) -> dict[str, str]:
        """Ask LLM to rename clusters for Japanese UI."""
        try:
            nodes = [
                n
                for n in self._db_get_all_nodes()
                if getattr(n, "status", None) == NodeStatus.active
                and getattr(n, "cluster", None)
                and n.cluster.strip()
            ]
            cluster_names = sorted({n.cluster.strip() for n in nodes})
            if not cluster_names:
                return {}

            prompt = """
    You are reviewing knowledge-graph cluster labels for a Japanese user interface.
    You will receive a list of existing cluster names.
    
    Task:
    - Return ONLY the cluster names that should be renamed.
    - If a name is already good, do not include it.
    - If an English abbreviation, acronym, product name, library name, model name, or technical term is better left unchanged, do not include it.
    - If a name is awkward English and should be localized for Japanese users, provide a concise Japanese or natural Japanese-mixed replacement.
    - Preserve technical meaning.
    - Do not over-translate proper nouns.
    - New names should be short, clear, and suitable as UI cluster labels.
    - Maximum 60 characters per new name.
    - If no names need changes, return an empty renames list.
    """

            result = self.runtime.llm.complete_structured(
                prompt,
                json.dumps({"cluster_names": cluster_names}, ensure_ascii=False),
                ClusterRenamePlan,
            )

            if not isinstance(result, ClusterRenamePlan):
                return {}

            allowed_originals = set(cluster_names)
            mapping: dict[str, str] = {}

            for item in result.renames:
                old_name = (item.original_name or "").strip()
                new_name = (item.new_name or "").strip()
                if not old_name or not new_name:
                    continue
                if old_name not in allowed_originals:
                    continue
                if old_name == new_name:
                    continue
                if len(new_name) > 60:
                    continue
                mapping[old_name] = new_name

            if not mapping:
                return {}

            existing_names = set(cluster_names)
            final_mapping: dict[str, str] = {}
            for old_name, new_name in mapping.items():
                would_collide = (
                    new_name in existing_names
                    and new_name != old_name
                    and new_name not in mapping
                )
                if would_collide:
                    continue
                final_mapping[old_name] = new_name

            if not final_mapping:
                return {}

            for node in nodes:
                old_cluster = node.cluster.strip()
                if old_cluster in final_mapping:
                    node.cluster = final_mapping[old_cluster]
                    self._db_upsert_node(node)

            return final_mapping

        except Exception as e:
            log.warning("ensure_japanese_clusters failed: %s", e)
            return {}

    def _refresh_clusters(self) -> None:
        try:
            self.recluster()
            self.ensure_japanese_clusters()
        except Exception as exc:
            log.info("recluster skipped: %s", exc)

    # -- MD output loading ------------------------------------------------

    def _load_md_output(self, out_path: Path) -> tuple[list[Node], list[Edge]]:
        if (out_path / "manifest.json").exists():
            return self._load_old_manifest_output(out_path)
        return self._load_new_planning_docs_output(out_path)

    def _load_old_manifest_output(
        self, out_path: Path
    ) -> tuple[list[Node], list[Edge]]:
        manifest = json.loads((out_path / "manifest.json").read_text(encoding="utf-8"))
        source_path = manifest.get("source")
        document_name = Path(source_path).name if source_path else out_path.name

        by_filename = {
            r["filename"]: r for r in manifest.get("files", []) if r.get("filename")
        }
        nodes: list[Node] = []
        sections: dict[str, list[str]] = {}
        for filename, record in by_filename.items():
            leaf = out_path / filename
            if not leaf.exists():
                continue
            meta, body = self._split_frontmatter(
                leaf.read_text(encoding="utf-8", errors="ignore")
            )
            if not body:
                continue
            node = Node(
                id=make_node_id(body, document_name),
                body=body,
                type=NodeType.endogenous,
                title=record.get("title") or meta.get("title", ""),
                original_document_name=document_name,
                source_path=source_path,
                source_ranges=self._parse_ranges(record.get("source_ranges"))
                or self._parse_ranges(meta.get("source_lines")),
                summary=record.get("summary") or meta.get("summary", ""),
                cluster=self._humanize(Path(filename).parent.name),
            )
            nodes.append(node)
            sections.setdefault(str(Path(filename).parent), []).append(node.id)

        edges: list[Edge] = []
        for node_ids in sections.values():
            edges += self._chain_edges(
                node_ids, "Adjacent page in the same source section."
            )
        return nodes, edges

    def _load_new_planning_docs_output(
        self, out_path: Path
    ) -> tuple[list[Node], list[Edge]]:
        planning_dir = out_path / "_planning"
        docs_dir = out_path / "docs"
        if not docs_dir.exists():
            raise FileNotFoundError(f"no docs directory found in {out_path}")

        metadata = self._read_json(planning_dir / "metadata.json", default={})
        coverage = self._read_json(planning_dir / "coverage.json", default={})
        document_name = (
            metadata.get("inferred_file_name")
            or metadata.get("original_file_name")
            or out_path.name
        )
        metadata_by_name = {
            i.get("name"): i for i in metadata.get("files", []) if i.get("name")
        }
        coverage_by_name = {
            i.get("filename"): i for i in coverage.get("files", []) if i.get("filename")
        }

        nodes: list[Node] = []
        ordered_ids: list[str] = []
        for md_file in sorted(docs_dir.glob("*.md"), key=self._doc_sort_key):
            meta, body = self._split_frontmatter(
                md_file.read_text(encoding="utf-8", errors="ignore")
            )
            body = body.strip()
            if not body:
                continue
            canonical = self._canonical_doc_name(md_file.name)
            meta_rec = metadata_by_name.get(canonical, {})
            cov_rec = coverage_by_name.get(canonical, {})

            ranges: list[tuple[int, int]] = []
            start, end = cov_rec.get("source_start"), cov_rec.get("source_end")
            if start is not None and end is not None:
                try:
                    ranges = [(int(start), int(end))]
                except (TypeError, ValueError):
                    ranges = []
            else:
                ranges = self._parse_ranges(meta.get("source_lines"))

            node = Node(
                id=make_node_id(body, document_name),
                body=body,
                type=NodeType.endogenous,
                title=(
                    cov_rec.get("title")
                    or meta.get("title")
                    or meta_rec.get("header")
                    or self._title_from_markdown(body)
                    or self._humanize(canonical.removesuffix(".md"))
                ),
                original_document_name=document_name,
                source_path=str(md_file),
                source_ranges=ranges,
                summary=cov_rec.get("summary") or meta.get("summary") or "",
                cluster=cov_rec.get("header") or meta_rec.get("header") or "General",
            )
            nodes.append(node)
            ordered_ids.append(node.id)

        edges = self._chain_edges(ordered_ids, "Next page in the source document.")
        return nodes, edges

    def _split_frontmatter(self, text: str) -> tuple[dict[str, str], str]:
        match = _FRONTMATTER_RE.match(text)
        if not match:
            return {}, text
        meta: dict[str, str] = {}
        for line in match.group(1).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip().strip('"')
        return meta, match.group(2).strip()

    def _parse_ranges(self, value: str | list[object] | None) -> list[tuple[int, int]]:
        if not value:
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return []
        ranges: list[tuple[int, int]] = []
        for pair in value or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                try:
                    ranges.append((int(pair[0]), int(pair[1])))
                except (TypeError, ValueError):
                    pass
        return ranges

    def _title_from_markdown(self, body: str) -> str | None:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or None
        return None

    def _exo_fallback_title(self, origin: str | None, body: str) -> str:
        """Title for an agent note whose markdown has no leading heading: the
        originating question (origin minus the `agent:`/`human:` prefix), else
        the first non-empty line of the body."""
        if origin:
            cleaned = re.sub(r"^(agent|human):", "", origin).strip()
            if cleaned:
                return cleaned[:80]
        for line in body.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:80]
        return "Agent note"

    def _humanize(self, dirname: str) -> str:
        name = re.sub(r"^\d+-", "", dirname).replace("-", " ").strip()
        return name.title()[:80] or "General"

    def _document_name(self, value: str) -> str:
        name = " ".join((value or "").strip().split())
        if not name:
            name = "untitled.md"
        return name[:160]

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _canonical_doc_name(self, filename: str) -> str:
        match = _NUMBERED_DOC_RE.match(filename)
        return match.group(1) if match else filename

    def _doc_sort_key(self, path: Path) -> tuple[int, str]:
        first = path.name.split("-", 1)[0]
        return (int(first), path.name) if first.isdigit() else (10**9, path.name)

    def _chain_edges(self, node_ids: list[str], summary: str) -> list[Edge]:
        return [
            Edge(
                id=make_edge_id(prev_id, next_id, "follows"),
                source_node_id=prev_id,
                target_node_id=next_id,
                label="follows",
                summary=summary,
            )
            for prev_id, next_id in zip(node_ids, node_ids[1:])
        ]


# ============================================================================
# 5. ReadGraphService
# ============================================================================


class ReadGraphService:
    """
    Concurrent read access with bounded concurrency.
    Each method builds a fresh DbSession + ReadSession and runs the operation.
    """

    def __init__(
        self, runtime: SharedGraphRuntime, max_reads: int = 8, max_agents: int = 2
    ):
        self.runtime = runtime
        self.read_sem = asyncio.Semaphore(max_reads)
        self.agent_sem = asyncio.Semaphore(max_agents)

    async def _run(
        self, fn: Callable[[GraphReadSession], Any], use_agent_sem: bool = False
    ) -> Any:
        def work():
            db = GraphDbSession(self.runtime.settings, readonly=True)
            try:
                session = GraphReadSession(self.runtime, db)
                return fn(session)
            finally:
                db.close()

        sem = self.agent_sem if use_agent_sem else self.read_sem
        async with sem:
            return await asyncio.to_thread(work)

    async def get(self) -> tuple[list[Node], list[Edge]]:
        return await self._run(lambda s: s.get())

    async def health(self, node_id: str | None = None) -> GraphStats:
        return await self._run(lambda s: s.health(node_id))

    async def read_node(self, node_id: str) -> Node | None:
        return await self._run(lambda s: s.read_node(node_id))

    async def follow_link(
        self, node_id: str, label=None, direction: str = "both", limit=None
    ):
        return await self._run(
            lambda s: s.follow_link(
                node_id, label=label, direction=direction, limit=limit
            )
        )

    async def query(self, query_type: str, value: str) -> QueryResult:
        return await self._run(lambda s: s.query(query_type, value))

    async def search(self, q: str, limit: int | None = None) -> list[Node]:
        return await self._run(lambda s: s.search(q, limit))

    async def ask(
        self,
        question: str,
        on_event: Callable[[dict], None] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> AgentAnswer:
        def work():
            db = GraphDbSession(self.runtime.settings, readonly=True)
            try:
                session = GraphReadSession(self.runtime, db)
                session.apply_overrides(overrides)
                return session.ask(question, persist=False, on_event=on_event)
            finally:
                db.close()

        async with self.agent_sem:
            return await asyncio.to_thread(work)


# ============================================================================
# 6. WriteJob
# ============================================================================

WriteStatus = Literal["queued", "running", "done", "failed", "cancelled"]


@dataclass
class WriteJob:
    id: str
    type: str
    payload: dict[str, Any]
    user_id: str | None = None
    status: WriteStatus = "queued"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: Any = None
    error: str | None = None


def job_to_dict(job: WriteJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "type": job.type,
        "payload": job.payload,
        "user_id": job.user_id,
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "result": job.result,
        "error": job.error,
    }


# ============================================================================
# 7. WriteGraphService
# ============================================================================


ASSIMILATING_MESSAGE = (
    "Added to the graph and searchable now. Ranking is still assimilating in "
    "the background — results improve shortly."
)


def _assimilating_result(node: Node) -> dict[str, Any]:
    """Wrap a fast-add node result with the UI 'still assimilating' notice."""
    return {"node": node, "assimilating": True, "message": ASSIMILATING_MESSAGE}


class WriteGraphService:
    """
    Serialized write queue. One worker applies jobs one at a time.
    """

    def __init__(self, runtime: SharedGraphRuntime, max_queue_size: int = 100):
        self.runtime = runtime
        self.queue: asyncio.Queue[WriteJob] = asyncio.Queue(maxsize=max_queue_size)
        self.jobs: dict[str, WriteJob] = {}
        self.worker_task: asyncio.Task | None = None
        # Slow background assimilation (summary, dedup, cascade, recluster).
        self.enrich = EnrichmentQueue(
            self._enrich_worker,
            drip_seconds=float(getattr(runtime.settings, "enrich_drip_seconds", 3.0)),
            recluster_every=int(getattr(runtime.settings, "recluster_every", 10)),
        )
        runtime.enrich = self.enrich

    @contextmanager
    def _enrich_worker(self):
        """Yield a write session on its own connection for one enrichment job."""
        db = GraphDbSession(self.runtime.settings, readonly=False)
        try:
            yield GraphWriteSession(self.runtime, db)
            db.conn.commit()
        except Exception:
            db.conn.rollback()
            raise
        finally:
            db.close()

    async def start(self) -> None:
        self.worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        self.enrich.close()

    async def enqueue(
        self, type_: str, payload: dict[str, Any], user_id: str | None = None
    ) -> WriteJob:
        job = WriteJob(
            id=str(uuid.uuid4()), type=type_, payload=payload, user_id=user_id
        )
        self.jobs[job.id] = job
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            job.status = "failed"
            job.error = "write queue is full"
            job.finished_at = datetime.now(timezone.utc)
            raise RuntimeError("write queue is full")
        return job

    async def _worker_loop(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self._run_job(job)
            finally:
                self.queue.task_done()

    async def _run_job(self, job: WriteJob) -> None:
        if job.status == "cancelled":
            return
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        try:
            result = await asyncio.to_thread(self._apply_job_sync, job)
            job.result = result
            job.status = "done"
        except Exception as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = datetime.now(timezone.utc)

    def _apply_job_sync(self, job: WriteJob) -> Any:
        db = GraphDbSession(self.runtime.settings, readonly=False)
        try:
            session = GraphWriteSession(self.runtime, db)

            if job.type == "update_node":
                return session.update_node(job.payload["node_id"], job.payload["body"])

            if job.type == "delete_node":
                session.delete_node(job.payload["node_id"])
                return {"deleted": job.payload["node_id"]}

            if job.type == "create_exogenous":
                node = session.create_exogenous_node(
                    body=job.payload["body"],
                    source_node_ids=job.payload.get("source_node_ids", []),
                    origin=job.payload.get("origin"),
                )
                return _assimilating_result(node)

            if job.type == "create_document":
                node = session.create_document_node(
                    body=job.payload["body"],
                    title=job.payload.get("title"),
                    document_name=job.payload.get("document_name"),
                    source_path=job.payload.get("source_path"),
                    source_ranges=job.payload.get("source_ranges"),
                )
                return _assimilating_result(node)

            if job.type == "recluster":
                mapping = session.recluster(
                    resolution=job.payload.get("resolution", 1.0)
                )
                return {"clusters": mapping}

            if job.type == "cascading_update":
                actions = session.cascading_update(job.payload["source_file"])
                return {"actions": actions}

            if job.type == "ingest_md_output":
                nodes = session.ingest_md_output(job.payload["path"])
                return {"ingested": len(nodes)}

            if job.type == "ensure_japanese_clusters":
                mapping = session.ensure_japanese_clusters()
                return {"renamed": mapping}

            raise ValueError(f"unknown write job type: {job.type}")
        finally:
            db.close()

    # -- Introspection ----------------------------------------------------

    def list_jobs(self, status: str | None = None, limit: int = 100) -> list[WriteJob]:
        jobs = list(self.jobs.values())
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def get_job(self, job_id: str) -> WriteJob | None:
        return self.jobs.get(job_id)

    def queue_size(self) -> int:
        return self.queue.qsize()

    def queue_position(self, job_id: str) -> int | None:
        queued = sorted(
            [j for j in self.jobs.values() if j.status == "queued"],
            key=lambda j: j.created_at,
        )
        for index, job in enumerate(queued, start=1):
            if job.id == job_id:
                return index
        return None

    def cancel_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status != "queued":
            return False
        job.status = "cancelled"
        job.finished_at = datetime.now(timezone.utc)
        return True


# ============================================================================
# 8. Bootstrap
# ============================================================================


def bootstrap_database(settings: Settings) -> None:
    """
    One-time startup: create vector tables, embed existing active nodes,
    set embed model metadata, and run cluster naming.

    Call this in the lifespan before accepting requests.
    """
    print(f"[bootstrap] START db={settings.database_path}", flush=True)
    db = GraphDbSession(settings, readonly=False)
    try:
        print(
            "[bootstrap] building runtime (NOTE: app.py already built one -- this is a 2nd load)",
            flush=True,
        )
        write_session = GraphWriteSession(SharedGraphRuntime(settings), db)

        # Ensure vector tables exist
        write_session._db_ensure_vec_tables(write_session.runtime.embedder.dim)

        # Check if we need to re-embed
        current_model = write_session.runtime.embedder.model_name
        current_dim = write_session.runtime.embedder.dim
        stored_model = write_session._db_get_meta("embed_model")
        stored_dim_raw = write_session._db_get_meta("embed_dim")
        stored_dim = int(stored_dim_raw) if stored_dim_raw else None

        active_nodes = [
            n
            for n in write_session._db_get_all_nodes()
            if n.status == NodeStatus.active
        ]

        dim_changed = stored_dim is not None and stored_dim != current_dim
        model_changed = stored_model is not None and stored_model != current_model
        coverage_incomplete = (
            write_session._db_count_vectors("vec_body") < len(active_nodes)
            if not dim_changed
            else False
        )

        print(
            f"[bootstrap] active_nodes={len(active_nodes)} "
            f"stored_model={stored_model} current_model={current_model} "
            f"dim_changed={dim_changed} model_changed={model_changed} "
            f"coverage_incomplete={coverage_incomplete}",
            flush=True,
        )
        if dim_changed or model_changed or coverage_incomplete:
            print(
                f"[bootstrap] RE-EMBEDDING all {len(active_nodes)} active nodes",
                flush=True,
            )
            log.info(
                "rebuilding ALL vectors (model %s->%s dim %s->%s)",
                stored_model,
                current_model,
                stored_dim,
                current_dim,
            )
            write_session._db_reset_vec_tables()
            write_session._db_ensure_vec_tables(current_dim)

            for index, node in enumerate(active_nodes, start=1):
                try:
                    write_session._db_set_vector(
                        node.id,
                        "vec_body",
                        write_session.runtime.embedder.embed_document(node.body),
                    )
                    if node.summary.strip():
                        write_session._db_set_vector(
                            node.id,
                            "vec_summary",
                            write_session.runtime.embedder.embed_document(node.summary),
                        )
                except Exception as exc:
                    log.warning("reembed node %s failed: %s", node.id, exc)
                if index % 25 == 0 or index == len(active_nodes):
                    print(
                        f"[bootstrap] reembed {index}/{len(active_nodes)} nodes",
                        flush=True,
                    )
                    log.info("reembed %d/%d nodes", index, len(active_nodes))

            write_session._db_set_meta("embed_model", current_model)
            write_session._db_set_meta("embed_dim", str(current_dim))
            print("[bootstrap] re-embedding done", flush=True)
        else:
            print("[bootstrap] vectors up to date -- skipping re-embed", flush=True)

        # --- evidence-first search index gate --------------------------------
        # Rebuild search_items + vec_search_item when the chunking scheme
        # changed, when the vectors were just rebuilt, or when coverage is
        # incomplete (fewer item vectors than active nodes -> at least one item
        # each is missing).
        reembedded = dim_changed or model_changed or coverage_incomplete
        stored_search_version = write_session._db_get_meta("search_index_version")
        item_vec_count = write_session._db_count_vectors("vec_search_item")
        search_version_changed = stored_search_version != SEARCH_INDEX_VERSION
        search_coverage_incomplete = item_vec_count < len(active_nodes)
        rebuild_search = bool(active_nodes) and (
            reembedded or search_version_changed or search_coverage_incomplete
        )
        print(
            f"[bootstrap] search_index stored={stored_search_version} "
            f"current={SEARCH_INDEX_VERSION} item_vecs={item_vec_count} "
            f"version_changed={search_version_changed} "
            f"coverage_incomplete={search_coverage_incomplete} "
            f"rebuild={rebuild_search}",
            flush=True,
        )
        if rebuild_search:
            print(
                f"[bootstrap] REBUILDING search_items for {len(active_nodes)} nodes",
                flush=True,
            )
            for index, node in enumerate(active_nodes, start=1):
                try:
                    write_session._store_search_items(node)
                except Exception as exc:
                    log.warning("rebuild search items node %s failed: %s", node.id, exc)
                if index % 25 == 0 or index == len(active_nodes):
                    print(
                        f"[bootstrap] search_items {index}/{len(active_nodes)} nodes",
                        flush=True,
                    )
            write_session._db_set_meta("search_index_version", SEARCH_INDEX_VERSION)
            print("[bootstrap] search_items rebuild done", flush=True)
        else:
            print("[bootstrap] search_items up to date -- skipping", flush=True)

        # Attempt cluster naming (best-effort). Only recluster when the graph
        # topology actually changed since last time, or some active node still
        # lacks a cluster -- otherwise a plain restart is a no-op.
        edges = write_session._db_get_all_edges()
        signature = source_hash(
            "|".join(
                [
                    str(len(active_nodes)),
                    *sorted(n.id for n in active_nodes),
                    str(len(edges)),
                    *sorted(e.id for e in edges),
                ]
            )
        )
        stored_signature = write_session._db_get_meta("cluster_signature")
        unclustered = any(not (n.cluster or "").strip() for n in active_nodes)
        if signature == stored_signature and not unclustered:
            print(
                "[bootstrap] graph unchanged -- skipping recluster/naming", flush=True
            )
        else:
            try:
                print(
                    f"[bootstrap] RECLUSTER start (graph changed={signature != stored_signature} "
                    f"unclustered={unclustered}; Louvain + 1 LLM naming call PER cluster = slow)",
                    flush=True,
                )
                write_session._recluster(persist=True)
                print(
                    "[bootstrap] recluster done; ENSURE_JAPANESE_CLUSTERS start (more LLM translate calls)",
                    flush=True,
                )
                write_session._ensure_japanese_clusters()
                write_session._db_set_meta("cluster_signature", signature)
                print("[bootstrap] japanese cluster naming done", flush=True)
            except Exception as exc:
                print(f"[bootstrap] cluster naming skipped: {exc}", flush=True)
                log.info("cluster naming skipped: %s", exc)

    finally:
        print("[bootstrap] DONE", flush=True)
        db.close()
