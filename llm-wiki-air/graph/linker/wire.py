from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChunkEntity(BaseModel):
    name: str = ""
    kind: str = ""
    role: Literal["defines", "uses"] = "uses"


class ChunkBehaviour(BaseModel):
    subject: str = ""
    action: str = ""
    object: str = ""


class ChunkMeta(BaseModel):
    summary: str = ""
    keywords: list[str] = Field(default_factory=list)
    entity: str = ""
    claims: list[str] = Field(default_factory=list)
    bridge_probe: str = ""
    entities: list[ChunkEntity] = Field(default_factory=list)
    behaviours: list[ChunkBehaviour] = Field(default_factory=list)


class EdgeSuggestion(BaseModel):
    target_node_id: str
    label: str = "related"
    summary: str = ""


class EdgeSuggestions(BaseModel):
    edges: list[EdgeSuggestion] = Field(default_factory=list)


NeoLabel = Literal[
    "defines", "uses", "prerequisite", "consequence", "constraint",
    "alternative", "contradicts", "interacts",
]


class NeoEdgeSuggestion(BaseModel):
    target_chunk_id: str
    label: NeoLabel = "interacts"
    summary: str = ""


class NeoEdgeSuggestions(BaseModel):
    edges: list[NeoEdgeSuggestion] = Field(default_factory=list)


__all__ = [
    "ChunkBehaviour", "ChunkEntity", "ChunkMeta", "EdgeSuggestion", "EdgeSuggestions",
    "NeoEdgeSuggestion", "NeoEdgeSuggestions", "NeoLabel",
]
