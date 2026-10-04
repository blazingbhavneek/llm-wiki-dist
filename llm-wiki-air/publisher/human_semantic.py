"""Optional, observe-only semantic proposals for durable human overlays.

The deterministic overlay remains authoritative.  This module may produce a
better reviewed proposal, but every failure returns the existing verbatim
human-primary/source-secondary rendering.
"""

from __future__ import annotations

import asyncio
import math
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from graph.wiki.storage import hash_of, read_json, sha256_text, write_json_atomic
from publisher.human_changes import HumanStore, conflict_text, marker_matches, now

SEMANTIC_SCHEMA_VERSION = 1
SCORER_VERSION = "human-shortlist-v1"
PROMPT_VERSION = "human-merge-v1"
VALIDATION_VERSION = "human-mechanical-v1"
MIN_SCORE = 0.72
MIN_MARGIN = 0.08
MAX_CANDIDATES = 8
MAX_WRITER_ATTEMPTS = 2


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1


class CandidateFeatures(StrictRecord):
    candidate_id: str
    source_id: str
    page_path: str
    heading_path: list[str]
    block_kind: str
    base_sha256: str
    human_sha256: str
    candidate_sha256: str
    neighbor_hashes: list[str] = Field(default_factory=list)
    exact_identifiers: list[str] = Field(default_factory=list)
    length_ratio: float = Field(ge=0, le=100)
    token_ratio: float = Field(ge=0, le=100)
    deterministic_similarity: float = Field(ge=0, le=1)
    reasons: list[str]


class CandidateScore(StrictRecord):
    candidate_id: str
    score: float

    @field_validator("score")
    @classmethod
    def finite_score(cls, value: float) -> float:
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("score must be finite and in [0, 1]")
        return value


class ScorerResult(StrictRecord):
    scorer_version: str
    scores: list[CandidateScore]


class ProtectedSpan(StrictRecord):
    span_id: str
    kind: str
    text: str


class WriterRequest(StrictRecord):
    base_g0: str
    human_h: str
    candidate_g1: str
    source_id: str
    page_path: str
    heading_path: list[str]
    protected_spans: list[ProtectedSpan]
    attempt: int = Field(ge=1, le=MAX_WRITER_ATTEMPTS)
    reason_codes: list[str] = Field(default_factory=list)


class SpanDisposition(StrictRecord):
    span_id: str
    disposition: Literal["preserved", "source-equivalent", "tombstoned"]


class WriterResponse(StrictRecord):
    merged_text: str
    dispositions: list[SpanDisposition]


class ValidationResult(StrictRecord):
    passed: bool
    reason_codes: list[str]


class JudgeResult(StrictRecord):
    passed: bool
    reason_codes: list[str]


class FeedbackAttempt(StrictRecord):
    attempt: int
    validation_reasons: list[str]
    judge_reasons: list[str]


class FinalProposal(StrictRecord):
    status: Literal["proposed", "fallback"]
    candidate_id: str = ""
    merged_blob: str
    fallback_blob: str
    scorer_score: float | None = None
    scorer_margin: float | None = None
    attempts: list[FeedbackAttempt] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


class CacheKey(StrictRecord):
    g0_sha256: str
    human_sha256: str
    g1_sha256: str
    anchor_sha256: str
    scorer_version: str = SCORER_VERSION
    prompt_version: str = PROMPT_VERSION
    model_version: str
    policy_mode: str
    validation_version: str = VALIDATION_VERSION


_IDENTIFIER = re.compile(
    r"(?:https?://[^\s)]+|(?:[A-Z][A-Z0-9_-]*-)?\d+(?:\.\d+)?(?:\s?(?:%|°C|℃|ms|s|kg|mm|cm|V|A))?|[A-Z][A-Z0-9_-]{2,})"
)
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})", re.M)
_MARKER = re.compile(r"<!-- llm-wiki-(?:human|source):[^:>]+:(?:start|end) -->")
_IMAGE = re.compile(r"!\[[^\n]*?]\([^\n)]+\)")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HTML_TABLE = re.compile(r"<table\b[^>]*>.*?</table>", re.I | re.S)


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]", text, re.UNICODE)


def _identifiers(text: str) -> set[str]:
    return set(_IDENTIFIER.findall(text))


def _kind(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith("```") or stripped.startswith("~~~"):
        return "code"
    if "\n|" in text and re.search(r"\|\s*:?-{3,}", text):
        return "table"
    if re.search(r"!\[[^]]*]\([^)]+\)", text):
        return "image"
    return "prose"


def _critical_units(text: str) -> set[str]:
    units = set(_IMAGE.findall(text)) | set(_COMMENT.findall(text)) | set(_HTML_TABLE.findall(text))
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        fence = _FENCE.match(lines[index])
        if fence:
            marker = fence.group(1)
            end = index + 1
            closing = re.compile(
                r"^[ \t]{0,3}" + re.escape(marker[0]) + r"{" + str(len(marker)) + r",}[ \t]*$"
            )
            while end < len(lines):
                if closing.match(lines[end].rstrip("\r\n")):
                    end += 1
                    break
                end += 1
            units.add("".join(lines[index:end]))
            index = end
            continue
        if lines[index].lstrip().startswith("|"):
            end = index + 1
            while end < len(lines) and lines[end].lstrip().startswith("|"):
                end += 1
            units.add("".join(lines[index:end]))
            index = end
            continue
        index += 1
    return {unit for unit in units if unit}


def shortlist(
    *,
    source_id: str,
    base: str,
    human: str,
    anchor: dict[str, Any],
    candidates: list[dict[str, Any]],
    cap: int = MAX_CANDIDATES,
) -> list[CandidateFeatures]:
    """Create a stable, bounded candidate list before any external call."""

    wanted_kind = str(anchor.get("block_kind") or _kind(base or human))
    rows: list[CandidateFeatures] = []
    for item in candidates:
        text = str(item.get("text") or "")
        if str(item.get("source_id") or "") != source_id:
            continue
        kind = str(item.get("block_kind") or _kind(text))
        if kind != wanted_kind:
            continue
        anchor_path = str(anchor.get("page_path") or "")
        page_path = str(item.get("page_path") or "")
        if anchor_path and page_path and Path(anchor_path).parent != Path(page_path).parent:
            continue
        base_tokens, candidate_tokens = _tokens(base), _tokens(text)
        similarity = SequenceMatcher(a=base_tokens, b=candidate_tokens, autojunk=False).ratio()
        reasons = ["same_source_identity", "same_block_kind"]
        if item.get("heading_path") == anchor.get("heading_path"):
            reasons.append("same_heading_path")
        if text == base:
            reasons.append("exact_base")
        rows.append(CandidateFeatures(
            candidate_id=str(item.get("candidate_id") or sha256_text(page_path + "\0" + text)[:20]),
            source_id=source_id,
            page_path=page_path,
            heading_path=[str(value) for value in item.get("heading_path", [])],
            block_kind=kind,
            base_sha256=sha256_text(base),
            human_sha256=sha256_text(human),
            candidate_sha256=sha256_text(text),
            neighbor_hashes=sorted(str(value) for value in item.get("neighbor_hashes", [])),
            exact_identifiers=sorted(_identifiers(base) & _identifiers(text)),
            length_ratio=(len(text) / max(1, len(base))),
            token_ratio=(len(candidate_tokens) / max(1, len(base_tokens))),
            deterministic_similarity=similarity,
            reasons=reasons,
        ))
    return sorted(
        rows,
        key=lambda row: (-row.deterministic_similarity, row.page_path, row.candidate_id),
    )[: max(0, cap)]


def mechanical_validate(
    request: WriterRequest,
    response: WriterResponse,
    *,
    forbidden_candidate_texts: list[str] | None = None,
) -> ValidationResult:
    reasons: list[str] = []
    output = response.merged_text
    dispositions = {row.span_id: row.disposition for row in response.dispositions}
    if len(dispositions) != len(response.dispositions):
        reasons.append("duplicate_disposition")
    for span in request.protected_spans:
        if dispositions.get(span.span_id) != "preserved":
            reasons.append("protected_disposition")
        count = output.count(span.text)
        if count == 0:
            reasons.append("protected_missing")
        elif count != 1:
            reasons.append("protected_duplicated")
    allowed = _identifiers(request.base_g0 + "\n" + request.human_h + "\n" + request.candidate_g1)
    present = _identifiers(output)
    required = _identifiers(request.human_h) | _identifiers(request.candidate_g1)
    if required - present:
        reasons.append("critical_token_missing")
    if present - allowed:
        reasons.append("critical_token_invented")
    output_counts = Counter(_IDENTIFIER.findall(output))
    base_counts = Counter(_IDENTIFIER.findall(request.base_g0))
    human_counts = Counter(_IDENTIFIER.findall(request.human_h))
    source_counts = Counter(_IDENTIFIER.findall(request.candidate_g1))
    if any(
        count > max(base_counts[token], human_counts[token], source_counts[token])
        for token, count in output_counts.items()
    ):
        reasons.append("critical_token_duplicated")
    from graph.common.markdown import scan_markdown_fences

    fence_scan = scan_markdown_fences(output.splitlines(keepends=True))
    if fence_scan.unclosed:
        reasons.append("unbalanced_fence")
    try:
        marker_matches(output)
        marker_matches(output, "source")
    except ValueError:
        reasons.append("unbalanced_marker")
    allowed_units = _critical_units(request.base_g0 + request.human_h + request.candidate_g1)
    required_units = _critical_units(request.human_h + request.candidate_g1)
    output_units = _critical_units(output)
    if any(output.count(unit) != 1 for unit in required_units):
        reasons.append("structured_unit_missing_or_duplicated")
    if output_units - allowed_units:
        reasons.append("structured_unit_invented")
    if len(output) > max(4096, 4 * (len(request.base_g0) + len(request.human_h) + len(request.candidate_g1))):
        reasons.append("oversized_output")
    for text in forbidden_candidate_texts or []:
        if text and text not in (request.base_g0, request.human_h, request.candidate_g1) and text in output:
            reasons.append("unrelated_candidate_leak")
            break
    return ValidationResult(passed=not reasons, reason_codes=sorted(set(reasons)))


@dataclass
class SemanticAssistant:
    """Bounded scorer/writer/judge orchestration used only in observe mode."""

    store: HumanStore
    scorer: Callable[[list[dict[str, Any]]], Any] | None = None
    writer: Callable[[dict[str, Any]], Any] | None = None
    judge: Callable[[dict[str, Any]], Any] | None = None
    model_version: str = "unconfigured"
    max_attempts: int = MAX_WRITER_ATTEMPTS

    def _cache_path(self, key: CacheKey) -> Path:
        return self.store.root / "semantic" / "cache" / f"{hash_of(key)}.json"

    def _fallback(self, human: str, source: str, edit_id: str) -> str:
        return conflict_text(human, source, edit_id)

    def propose(
        self,
        *,
        source_id: str,
        base: str,
        human: str,
        anchor: dict[str, Any],
        candidates: list[dict[str, Any]],
        edit_id: str,
        policy_mode: str = "observe",
    ) -> FinalProposal:
        started = time.monotonic()
        ordered = shortlist(source_id=source_id, base=base, human=human, anchor=anchor, candidates=candidates)
        selected_by_id = {
            str(item.get("candidate_id") or sha256_text(
                str(item.get("page_path") or "") + "\0" + str(item.get("text") or "")
            )[:20]): item
            for item in candidates
        }
        # The deterministic shortlist order selects the complete source variant
        # used by every early fallback. Input list order is not authoritative.
        source = str(selected_by_id.get(ordered[0].candidate_id, {}).get("text") or "") if ordered else ""
        fallback_blob = self.store.put(self._fallback(human, source, edit_id))
        reasons: list[str] = []
        if policy_mode != "observe":
            return self._finish_early(
                base, human, anchor, ordered,
                FinalProposal(status="fallback", merged_blob=fallback_blob, fallback_blob=fallback_blob,
                              reason_codes=["observe_only"]),
                policy_mode=policy_mode, started=started,
            )
        if not ordered or self.scorer is None:
            return self._finish_early(
                base, human, anchor, ordered,
                FinalProposal(status="fallback", merged_blob=fallback_blob, fallback_blob=fallback_blob,
                              reason_codes=["scorer_unavailable" if self.scorer is None else "no_candidate"]),
                policy_mode=policy_mode, started=started,
            )
        request_lookup = hash_of({
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "g0": sha256_text(base),
            "human": sha256_text(human),
            "anchor": hash_of(anchor),
            "candidates": [(row.candidate_id, row.candidate_sha256) for row in ordered],
            "scorer": SCORER_VERSION,
            "prompt": PROMPT_VERSION,
            "validation": VALIDATION_VERSION,
            "model": self.model_version,
            "policy": policy_mode,
        })
        lookup_path = self.store.root / "semantic" / "cache-index" / f"{request_lookup}.json"
        if lookup_path.exists():
            try:
                lookup = read_json(lookup_path)
                cache_path = self.store.root / "semantic" / "cache" / f"{lookup['cache_key']}.json"
                cached = FinalProposal.model_validate(read_json(cache_path))
                self.store.get(cached.merged_blob)
                self.store.get(cached.fallback_blob)
                self._record_early_metrics(
                    base, human, anchor, ordered, cached, policy_mode=policy_mode,
                    cache_hit=True, duration_ms=round((time.monotonic() - started) * 1000),
                )
                return cached
            except (OSError, KeyError, TypeError, ValueError, ValidationError):
                pass
        try:
            scorer_rows = []
            for row in ordered:
                candidate = selected_by_id[row.candidate_id]
                scorer_rows.append({
                    **row.model_dump(mode="json"),
                    "base_g0": base,
                    "human_h": human,
                    "candidate_g1": str(candidate.get("text") or ""),
                })
            scored = ScorerResult.model_validate(self.scorer(scorer_rows))
            known = {row.candidate_id for row in ordered}
            returned = [row.candidate_id for row in scored.scores]
            if set(returned) != known or len(returned) != len(known):
                raise ValueError("scorer must return every shortlisted candidate exactly once")
            ranking = sorted(scored.scores, key=lambda row: (-row.score, row.candidate_id))
        except Exception:  # external scorer/transport/schema failures always use exact fallback
            return self._finish_early(
                base, human, anchor, ordered,
                FinalProposal(status="fallback", merged_blob=fallback_blob, fallback_blob=fallback_blob,
                              reason_codes=["scorer_failure"]),
                policy_mode=policy_mode, started=started,
            )
        if not ranking:
            reasons.append("scorer_empty")
        else:
            top = ranking[0]
            margin = top.score - (ranking[1].score if len(ranking) > 1 else 0.0)
            if top.score < MIN_SCORE:
                reasons.append("low_score")
            if len(ranking) > 1 and margin < MIN_MARGIN:
                reasons.append("low_margin")
            if len(ranking) > 1 and top.score == ranking[1].score:
                reasons.append("tie")
        if reasons:
            return self._finish_early(
                base, human, anchor, ordered,
                FinalProposal(status="fallback", merged_blob=fallback_blob, fallback_blob=fallback_blob,
                              scorer_score=ranking[0].score if ranking else None,
                              scorer_margin=margin if ranking else None, reason_codes=reasons),
                policy_mode=policy_mode, started=started,
            )
        selected_feature = next(row for row in ordered if row.candidate_id == top.candidate_id)
        selected = selected_by_id[top.candidate_id]
        source = str(selected.get("text") or "")
        key = CacheKey(
            g0_sha256=sha256_text(base), human_sha256=sha256_text(human), g1_sha256=sha256_text(source),
            anchor_sha256=hash_of(anchor), model_version=self.model_version, policy_mode=policy_mode,
        )
        cache_path = self._cache_path(key)
        if cache_path.exists():
            try:
                cached = FinalProposal.model_validate(read_json(cache_path))
                self.store.get(cached.merged_blob)
                self.store.get(cached.fallback_blob)
                self._record_metrics(
                    key, ordered, cached, cache_hit=True,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                return cached
            except (OSError, ValueError, ValidationError):
                pass
        if self.writer is None or self.judge is None:
            return self._finish_early(
                base, human, anchor, ordered,
                FinalProposal(status="fallback", candidate_id=top.candidate_id,
                              merged_blob=fallback_blob, fallback_blob=fallback_blob,
                              scorer_score=top.score, scorer_margin=margin,
                              reason_codes=["writer_or_judge_unavailable"]),
                policy_mode=policy_mode, started=started,
            )
        protected = [ProtectedSpan(span_id="human-1", kind="human", text=human)]
        attempts: list[FeedbackAttempt] = []
        feedback: list[str] = []
        for attempt in range(1, min(MAX_WRITER_ATTEMPTS, max(1, self.max_attempts)) + 1):
            request = WriterRequest(
                base_g0=base, human_h=human, candidate_g1=source,
                source_id=source_id, page_path=selected_feature.page_path,
                heading_path=selected_feature.heading_path, protected_spans=protected,
                attempt=attempt, reason_codes=feedback,
            )
            try:
                response = WriterResponse.model_validate(self.writer(request.model_dump()))
                validation = mechanical_validate(
                    request, response,
                    forbidden_candidate_texts=[str(row.get("text") or "") for row in candidates if row is not selected],
                )
            except Exception:  # external writer/transport/schema failures are bounded fallback inputs
                validation = ValidationResult(passed=False, reason_codes=["writer_failure"])
                response = None
            judge_result = JudgeResult(passed=False, reason_codes=["mechanical_rejection"])
            if validation.passed and response is not None:
                try:
                    judge_result = JudgeResult.model_validate(self.judge({
                        "schema_version": SEMANTIC_SCHEMA_VERSION,
                        "base_g0": base,
                        "human_h": human,
                        "candidate_g1": source,
                        "merged_text": response.merged_text,
                        "protected_spans": [span.model_dump(mode="json") for span in protected],
                        "dispositions": [row.model_dump(mode="json") for row in response.dispositions],
                        "mechanical_validation": validation.model_dump(mode="json"),
                    }))
                except Exception:  # the independent judge cannot block deterministic preservation
                    judge_result = JudgeResult(passed=False, reason_codes=["judge_failure"])
            attempts.append(FeedbackAttempt(
                attempt=attempt,
                validation_reasons=validation.reason_codes,
                judge_reasons=judge_result.reason_codes,
            ))
            if validation.passed and judge_result.passed and response is not None:
                proposal = FinalProposal(
                    status="proposed", candidate_id=top.candidate_id,
                    merged_blob=self.store.put(response.merged_text), fallback_blob=fallback_blob,
                    scorer_score=top.score, scorer_margin=margin, attempts=attempts,
                )
                write_json_atomic(cache_path, proposal.model_dump(mode="json"))
                write_json_atomic(lookup_path, {"schema_version": SEMANTIC_SCHEMA_VERSION,
                                                "cache_key": hash_of(key)})
                self._record_metrics(
                    key, ordered, proposal, cache_hit=False,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                return proposal
            feedback = sorted(set(validation.reason_codes + judge_result.reason_codes))
        proposal = FinalProposal(
            status="fallback", candidate_id=top.candidate_id,
            merged_blob=fallback_blob, fallback_blob=fallback_blob,
            scorer_score=top.score, scorer_margin=margin, attempts=attempts,
            reason_codes=["attempts_exhausted"],
        )
        self._record_metrics(
            key, ordered, proposal, cache_hit=False,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return proposal

    def _finish_early(
        self,
        base: str,
        human: str,
        anchor: dict[str, Any],
        candidates: list[CandidateFeatures],
        proposal: FinalProposal,
        *,
        policy_mode: str,
        started: float,
    ) -> FinalProposal:
        self._record_early_metrics(
            base, human, anchor, candidates, proposal, policy_mode=policy_mode,
            cache_hit=False, duration_ms=round((time.monotonic() - started) * 1000),
        )
        return proposal

    def _record_early_metrics(
        self,
        base: str,
        human: str,
        anchor: dict[str, Any],
        candidates: list[CandidateFeatures],
        proposal: FinalProposal,
        *,
        policy_mode: str,
        cache_hit: bool,
        duration_ms: int,
    ) -> None:
        identity = hash_of({
            "g0": sha256_text(base), "human": sha256_text(human), "anchor": hash_of(anchor),
            "candidates": [(row.candidate_id, row.candidate_sha256) for row in candidates],
            "policy": policy_mode, "proposal": proposal.model_dump(mode="json"), "cache_hit": cache_hit,
        })
        path = self.store.root / "semantic" / "observations" / f"{identity}.json"
        if path.exists():
            return
        write_json_atomic(path, {
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "recorded_at": now(),
            "request_sha256": identity,
            "shortlist_size": len(candidates),
            "score": proposal.scorer_score,
            "margin": proposal.scorer_margin,
            "attempts": len(proposal.attempts),
            "reason_codes": proposal.reason_codes,
            "cache_hit": cache_hit,
            "fallback": proposal.status == "fallback",
            "duration_ms": duration_ms,
            "budget": {"max_candidates": MAX_CANDIDATES, "max_writer_attempts": MAX_WRITER_ATTEMPTS},
            "versions": {
                "scorer": SCORER_VERSION, "prompt": PROMPT_VERSION,
                "validation": VALIDATION_VERSION, "model": self.model_version,
            },
        })

    def _record_metrics(
        self, key: CacheKey, candidates: list[CandidateFeatures], proposal: FinalProposal, *,
        cache_hit: bool, duration_ms: int
    ) -> None:
        identity = hash_of({"key": key, "proposal": proposal.model_dump(mode="json"), "cache_hit": cache_hit})
        path = self.store.root / "semantic" / "observations" / f"{identity}.json"
        if path.exists():
            return
        write_json_atomic(path, {
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "recorded_at": now(),
            "cache_key_sha256": hash_of(key),
            "shortlist_size": len(candidates),
            "score": proposal.scorer_score,
            "margin": proposal.scorer_margin,
            "attempts": len(proposal.attempts),
            "reason_codes": proposal.reason_codes,
            "cache_hit": cache_hit,
            "fallback": proposal.status == "fallback",
            "duration_ms": duration_ms,
            "budget": {"max_candidates": MAX_CANDIDATES, "max_writer_attempts": MAX_WRITER_ATTEMPTS},
            "versions": {
                "scorer": SCORER_VERSION,
                "prompt": PROMPT_VERSION,
                "validation": VALIDATION_VERSION,
                "model": self.model_version,
            },
        })


def build_runtime_semantic_assistant(store: HumanStore, settings: Any) -> SemanticAssistant:
    """Build lazy production adapters; construction performs no network/model call."""

    from graph.wiki.model import ChatModelPort
    from graph.workspace.writer import wiki_config
    from langchain_core.messages import HumanMessage, SystemMessage

    prompt_root = Path(__file__).with_name("prompts")
    writer_system = (prompt_root / "human_merge_system.txt").read_text(encoding="utf-8")
    judge_system = (prompt_root / "human_judge_system.txt").read_text(encoding="utf-8")
    writer_config = wiki_config(settings, run_dir=store.project.metadata / "state" / "human-semantic-writer")
    judge_config = wiki_config(settings, run_dir=store.project.metadata / "state" / "human-semantic-judge")
    writer_model = ChatModelPort(writer_config)
    judge_model = ChatModelPort(judge_config)

    def score(rows: list[dict[str, Any]]) -> dict[str, Any]:
        from jev import JevQuestion, JevRequest, get_engine_for

        engine = get_engine_for(settings)
        question = (
            "G1はG0と同じ意味上のブロックの更新先であり、Hの人間編集を安全に再適用する候補ですか。"
            "語が似ているだけ、別の対象、別の見出しならいいえ。"
        )
        requests = [
            JevRequest(
                {
                    "G0": row["base_g0"], "H": row["human_h"], "G1": row["candidate_g1"],
                    "source_id": row["source_id"], "page_path": row["page_path"],
                    "heading_path": row["heading_path"], "block_kind": row["block_kind"],
                },
                JevQuestion(question, key=row["candidate_id"]),
            )
            for row in rows
        ]
        results = engine.decide_batch(requests)
        return ScorerResult(
            scorer_version=SCORER_VERSION,
            scores=[
                CandidateScore(candidate_id=row["candidate_id"], score=float(result.p_yes))
                for row, result in zip(rows, results, strict=True)
            ],
        ).model_dump(mode="json")

    def write(payload: dict[str, Any]) -> dict[str, Any]:
        request = WriterRequest.model_validate(payload)
        messages = [
            SystemMessage(content=writer_system),
            HumanMessage(content="<DATA>\n" + json.dumps(request.model_dump(mode="json"), ensure_ascii=False) + "\n</DATA>"),
        ]
        result = asyncio.run(
            writer_model.structured(WriterResponse, messages, max_output_tokens=8000, temperature=0.0)
        )
        return WriterResponse.model_validate(result).model_dump(mode="json")

    def judge(payload: dict[str, Any]) -> dict[str, Any]:
        messages = [
            SystemMessage(content=judge_system),
            HumanMessage(content="<DATA>\n" + json.dumps(payload, ensure_ascii=False) + "\n</DATA>"),
        ]
        result = asyncio.run(
            judge_model.structured(JudgeResult, messages, max_output_tokens=2000, temperature=0.0)
        )
        return JudgeResult.model_validate(result).model_dump(mode="json")

    return SemanticAssistant(
        store=store,
        scorer=score,
        writer=write,
        judge=judge,
        model_version=f"writer={writer_model.name};judge={judge_model.name}",
    )


__all__ = [
    "CacheKey", "CandidateFeatures", "CandidateScore", "FeedbackAttempt", "FinalProposal",
    "JudgeResult", "ProtectedSpan", "ScorerResult", "SemanticAssistant", "SpanDisposition",
    "ValidationResult", "WriterRequest", "WriterResponse", "build_runtime_semantic_assistant",
    "mechanical_validate", "shortlist",
]
