"""Tiny model-facing schemas for observation, planning, and page review."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ObservedRange(BaseModel):
    """One piece of evidence noticed inside an overlapping source window."""

    title: str = ""
    kind: str = ""
    summary: str = ""
    source_start: int = 0
    source_end: int = 0
    parent: str = ""
    enumeration_family: str = ""
    repeated_format: bool = False
    continues_before: bool = False
    continues_after: bool = False


class WindowInventory(BaseModel):
    """A descriptive inventory; its ranges may overlap or be nested."""

    summary: str = ""
    observations: list[ObservedRange] = Field(default_factory=list)


class RegionalPage(BaseModel):
    """A provisional page candidate produced from at most ten windows."""

    title: str = ""
    scope: str = ""
    chapter: str = ""
    source_start: int = 0
    source_end: int = 0
    entity_kind: str = ""
    enumeration_family: str = ""
    continues_before: bool = False
    continues_after: bool = False


class RegionalPlan(BaseModel):
    """Compact provisional plan for one batch of overlapping windows."""

    summary: str = ""
    pages: list[RegionalPage] = Field(default_factory=list)


class SemanticPlan(BaseModel):
    """Free-form semantic advice, deliberately separated from range syntax."""

    plan: str = ""


class SeedRange(BaseModel):
    """One final seed. Final validation requires these to tile the source."""

    title: str = ""
    summary: str = ""
    chapter: str = ""
    source_start: int = 0
    source_end: int = 0
    path: list[str] = Field(default_factory=list)


class SeedPlan(BaseModel):
    summary: str = ""
    pages: list[SeedRange] = Field(default_factory=list)


class ReferenceFact(BaseModel):
    """One useful fact found outside the target page's owned source range."""

    description: str = ""
    reason: str = ""
    insertion_point: str = ""
    source_start: int = 0
    source_end: int = 0
    target_line: int = 0


class ReferenceResearchResult(BaseModel):
    """Useful cross-page evidence, or an explicit reason that none exists."""

    useful_facts: list[ReferenceFact] = Field(default_factory=list)
    no_useful_information_reason: str = ""


class ImportantOmission(BaseModel):
    """One meaningful source fact absent from a rewritten page."""

    description: str = ""
    source_start: int = 0
    source_end: int = 0


class PageJudgeResult(BaseModel):
    """Coverage judgment used to select between at most two page versions."""

    coverage_score: int = Field(default=0, ge=0, le=100)
    missing_important_information: list[ImportantOmission] = Field(default_factory=list)
    notes: str = ""


# --------------------------------------------------------------------------
# Cross-document linker contracts (docs/LINKER.md section 10).
# --------------------------------------------------------------------------

LinkRelationType = Literal[
    "prerequisite",
    "upstream_input",
    "downstream_effect",
    "mechanism",
    "shared_constraint",
    "failure_cause",
    "diagnostic_evidence",
    "recovery_action",
    "workflow_next_step",
    "implementation_of",
    "validation_of",
    "alternative_to",
    "tradeoff_with",
    "contradicts",
    "counterexample_to",
    "useful_analogy",
]


class MapLinkCandidate(BaseModel):
    """One page nominated as worth full reading during exhaustive map scout."""

    candidate_page_id: str = ""
    relation_type: LinkRelationType = "mechanism"
    hypothesis: str = Field(default="", min_length=20, max_length=500)
    reader_value: str = Field(default="", min_length=20, max_length=500)
    target_map_entry_ids: list[str] = Field(default_factory=list, min_length=1, max_length=6)
    candidate_map_entry_ids: list[str] = Field(default_factory=list, min_length=1, max_length=6)
    bridge_questions: list[str] = Field(default_factory=list, min_length=1, max_length=4)
    priority: int = Field(default=50, ge=0, le=100)


class MapLinkScanResult(BaseModel):
    """At most three nominations per map block, or an explicit no-candidate reason."""

    candidates: list[MapLinkCandidate] = Field(default_factory=list, max_length=3)
    no_candidate_reason: str = ""


class BridgeProbeResult(BaseModel):
    """3..6 question-shaped bridge probes; summaries are rejected at validation."""

    probes: list[str] = Field(default_factory=list, min_length=3, max_length=6)


class DeepLinkProposal(BaseModel):
    """One bidirectional link proposal produced from fully read pages."""

    endpoint_page_id: str = ""
    relation_type: LinkRelationType = "mechanism"
    discovery_path: list[str] = Field(default_factory=list, min_length=2, max_length=4)
    target_evidence: str = Field(default="", min_length=10, max_length=500)
    endpoint_evidence: str = Field(default="", min_length=10, max_length=500)
    target_map_entry_ids: list[str] = Field(default_factory=list, min_length=1, max_length=6)
    endpoint_map_entry_ids: list[str] = Field(default_factory=list, min_length=1, max_length=6)
    relationship_explanation: str = Field(default="", min_length=30, max_length=800)
    why_reader_needs_link: str = Field(default="", min_length=20, max_length=500)
    why_not_shared_topic_only: str = Field(default="", min_length=20, max_length=500)
    target_anchor_id: str = ""
    endpoint_anchor_id: str = ""
    target_bridge_template: str = ""
    endpoint_bridge_template: str = ""
    target_footer_reason: str = Field(default="", min_length=20, max_length=240)
    endpoint_footer_reason: str = Field(default="", min_length=20, max_length=240)
    novelty_score: int = Field(default=0, ge=0, le=100)
    usefulness_score: int = Field(default=0, ge=0, le=100)
    confidence_score: int = Field(default=0, ge=0, le=100)


class DeepLinkResearchResult(BaseModel):
    """Full-page research outcome; only fully supplied pages may be proposed."""

    proposals: list[DeepLinkProposal] = Field(default_factory=list, max_length=1)
    rejected_page_ids: list[str] = Field(default_factory=list, max_length=1)
    notes: str = ""


class LinkJudgeResult(BaseModel):
    """Final endpoint-only judge. It may reject or tighten prose, nothing else."""

    is_grounded_in_both_pages: bool = False
    is_specific_relationship: bool = False
    is_more_than_shared_topic: bool = False
    is_useful_to_target_reader: bool = False
    is_useful_to_endpoint_reader: bool = False
    bridge_text_adds_no_unsupported_claim: bool = False
    recommended: bool = False
    problems: list[str] = Field(default_factory=list)
    corrected_target_anchor_id: str = ""
    corrected_endpoint_anchor_id: str = ""
    corrected_target_bridge_template: str = ""
    corrected_endpoint_bridge_template: str = ""
    corrected_target_footer_reason: str = ""
    corrected_endpoint_footer_reason: str = ""


class OversizedSectionNote(BaseModel):
    """Evidence note for one heading-delimited section of an oversized endpoint.

    Required by the oversized-endpoint procedure (docs/LINKER.md 11.3); kept
    here because it is model-facing.
    """

    section_id: str = ""
    relevant: bool = False
    excerpts: list[str] = Field(default_factory=list, max_length=3)
    note: str = ""
