"""Service-local Pydantic models. GROWI page IDs are node IDs."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field


class WikiPage(BaseModel):
    id: str
    revision_id: str = ""
    path: str = ""
    title: str = ""
    body: str = ""
    summary: str = ""
    snippet: str = ""
    parent_id: str | None = None
    descendant_count: int = 0
    is_empty: bool = False
    updated_at: str = ""
    source_url: str = ""
    document: str = ""
    cluster: str = ""
    type: Literal["page"] = "page"
    status: Literal["active"] = "active"

    # Frontend compatibility aliases for the old graph Node shape.
    @property
    def original_document_name(self) -> str:
        return self.document

    @property
    def source_path(self) -> str:
        return self.path

    @property
    def source_version(self) -> str:
        return self.revision_id

    @property
    def entity(self) -> str:
        return self.title

    def public_dict(self) -> dict[str, Any]:
        data = self.model_dump()
        data.update(
            {
                "original_document_name": self.document,
                "source_path": self.path,
                "source_version": self.revision_id,
                "entity": self.title,
                "keywords": [],
                "claims": [],
                "source_ranges": [],
            }
        )
        return data


class WikiLink(BaseModel):
    id: str
    source_node_id: str
    target_node_id: str
    label: str = ""
    summary: str = ""
    source_heading: str = ""
    fragment: str = ""
    target_path: str = ""
    kind: Literal["markdown", "parent", "child", "nav"] = "markdown"


class Evidence(BaseModel):
    page_id: str
    field: Literal["growi_es", "title_path", "section", "linked_context", "index_map", "jev"]
    text: str
    heading: str = ""
    start_line: int | None = None
    end_line: int | None = None
    source_rank: int = 0
    score: float = 0.0


class AgentAnswer(BaseModel):
    question: str
    answer: str = ""
    cited_node_ids: list[str] = Field(default_factory=list)
    cited_nodes: list[dict[str, str]] = Field(default_factory=list)
    steps: int = 0


def make_link_id(source_id: str, target_id: str, anchor: str, fragment: str) -> str:
    seed = "\x00".join([source_id, target_id, anchor, fragment])
    return "edge:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
