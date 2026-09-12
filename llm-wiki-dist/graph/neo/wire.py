"""Tiny model-facing schemas for observation, planning, and page review."""

from __future__ import annotations

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


class SeedPlan(BaseModel):
    summary: str = ""
    pages: list[SeedRange] = Field(default_factory=list)


class ReferenceSelection(BaseModel):
    """Small shortlist of pages worth comparing with one target page."""

    page_numbers: list[int] = Field(default_factory=list)


class ReferenceFact(BaseModel):
    """One useful fact found outside the target page's owned source range."""

    description: str = ""
    reason: str = ""
    insertion_point: str = ""
    source_start: int = 0
    source_end: int = 0


class ReferenceResearchResult(BaseModel):
    """Useful cross-page evidence, or an explicit reason that none exists."""

    useful_facts: list[ReferenceFact] = Field(default_factory=list)
    no_useful_information_reason: str = ""


class WikiPlanJudgeResult(BaseModel):
    """Whether a plan designs a real standalone Wiki instead of a cleanup."""

    acceptable: bool = False
    issues: list[str] = Field(default_factory=list)


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
