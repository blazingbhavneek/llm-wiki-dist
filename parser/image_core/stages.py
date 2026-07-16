"""Staged image understanding. Each stage is one narrow LLM call:

    classify -> transcribe -> [entity diagram] extract_nodes -> extract_edges -> verify_graph
                           -> [everything else] describe_plain

Rationale (see IMAGE_HANDOFF.md for sources): VLMs read node labels well but get
edges/relations wrong when asked for everything at once, so edges get their own
call anchored on the confirmed node list, and the transcript grounds all labels.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, List, Literal, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from image_core.llm import Llm

# =========================
# Schemas
# =========================

Kind = Literal[
    "entity_diagram", "table", "chart", "screenshot", "photo", "text", "other"
]


class Classification(BaseModel):
    kind: Kind


class Transcript(BaseModel):
    strings: List[str] = Field(default_factory=list)


class Node(BaseModel):
    id: str
    label: str
    group: Optional[str] = None


class Group(BaseModel):
    id: str
    label: str
    parent: Optional[str] = None


class NodeExtraction(BaseModel):
    nodes: List[Node] = Field(default_factory=list)
    groups: List[Group] = Field(default_factory=list)


Cue = Literal[
    "arrow",
    "line",
    "touching",
    "proximity",
    "shared_region",
    "color_or_legend",
    "number_or_label_reference",
]


class Edge(BaseModel):
    src: str
    dst: str
    label: Optional[str] = None
    directed: bool = True
    bidirectional: bool = False
    style: Literal["solid", "dashed", "dotted"] = "solid"
    cue: Cue = "arrow"


class EdgeExtraction(BaseModel):
    direction: Literal["TD", "LR"] = "TD"
    edges: List[Edge] = Field(default_factory=list)


class DiagramGraph(BaseModel):
    direction: str = "TD"
    nodes: List[Node] = Field(default_factory=list)
    groups: List[Group] = Field(default_factory=list)
    edges: List[Edge] = Field(default_factory=list)


class EdgeVerdict(BaseModel):
    edge_index: int
    verdict: Literal["correct", "reversed", "wrong", "not_visible"]
    corrected: Optional[Edge] = None


class GraphAudit(BaseModel):
    verdicts: List[EdgeVerdict] = Field(default_factory=list)
    missed_edges: List[Edge] = Field(default_factory=list)


# =========================
# Prompts
# =========================

CONTEXT_HEADER = (
    "Document context: the text below precedes this image in the source document. "
    "Use it ONLY to resolve terminology, abbreviations, and hard-to-read characters. "
    "Never copy facts from it into your answer; report only what is visible in the image.\n"
    "----- document context -----\n{context}\n----- end of document context -----"
)

CLASSIFY_PROMPT = """Classify this image for a technical document pipeline.

Kinds:
- entity_diagram: discrete labeled entities (boxes, components, systems, actors, databases,
  computers, processes, files) connected by ANY visible relation cues: arrows, plain lines,
  touching boxes, containment, shared labeled regions, color/legend coding, numbered references.
  Covers flowcharts, block/architecture diagrams, network layouts, ER diagrams, org charts,
  data-flow and sequence-like diagrams.
- table: a grid of rows and columns.
- chart: a data plot (bar, line, pie, scatter, gantt, ...).
- screenshot: software UI.
- photo: photograph or realistic scene.
- text: mostly plain printed text.
- other: none of the above (memory/bit layouts, timelines, equations, maps, mixed figures).

Pick the dominant structure of the image."""

TRANSCRIBE_PROMPT = """List EVERY visible text string in the image(s): titles, captions, node and
box labels, edge labels, axis labels and tick values, legend entries, table cells, UI text,
numbers, units, footnotes.

Rules:
- One string per visible text element, in reading order (top-left to bottom-right).
- Copy characters exactly. Keep the original language (Japanese stays Japanese). Copy numbers
  exactly as printed.
- If a string is unreadable, output "[unclear]" for it. Never guess.
- Do not add, translate, summarize, or interpret anything."""

NODES_PROMPT = """Identify the entities and containers in this diagram.

An entity is any distinct labeled box, component, actor, system, database, file, process, or shape.
A container (group) is a larger region that visually encloses entities: a computer, server,
network zone, subsystem, layer, or dashed boundary.

Rules:
- Take labels from the transcribed strings below; do not invent labels.
- Give every entity a short ASCII id (n1, n2, ...) and its exact visible label.
- Give every container an id (g1, g2, ...), its label, and its parent container id if nested.
- Set an entity's group to the id of the container that visually encloses it, else null.
- Do not invent entities that are not visible.

Transcribed strings:
{transcript}"""

EDGES_PROMPT = """Identify every relation between the confirmed entities below.

Relation cues to look for (ALL count, not only arrows):
- arrow: a line with an arrowhead (directed=true; src is the tail, dst is the head)
- line: a plain connecting line without arrowheads (directed=false)
- touching: boxes sharing a border
- proximity: deliberate adjacency implying association
- shared_region: entities interacting within the same labeled region (containment alone is
  already captured by groups; only report an edge if the image implies interaction)
- color_or_legend: entities linked by matching color/hatching explained by a legend
- number_or_label_reference: numbered or lettered markers that refer to each other

Rules:
- Use ONLY these entity ids: {node_ids}
- Copy edge labels exactly as printed; omit the label if none is printed.
- Preserve arrow direction exactly. Arrowheads on both ends: bidirectional=true.
- style is solid/dashed/dotted as drawn.
- Report every visible connection. Do not invent connections.
- direction is the overall reading direction of the diagram: TD (top-down) or LR (left-right).

Confirmed entities:
{nodes}"""

VERIFY_PROMPT = """Audit this extracted graph against the image.

Numbered edges:
{edges}

For each edge index, give a verdict:
- correct: connection and direction are visible exactly as stated
- reversed: connection exists but points the opposite way
- wrong: connection exists but a node or label is wrong; put the fixed edge in corrected
- not_visible: no such connection appears in the image

Then list clearly visible connections that are missing from the list (missed_edges), using only
the existing entity ids. Judge only what is visible; when unsure, prefer correct."""

PLAIN_PROMPT = """Transcribe this image into Markdown that will replace it in a technical document.

Absolute rules:
- Report ONLY what is visible. Do not summarize, caption, or add background knowledge.
- Every transcribed string listed below must appear in your output, character for character.
- Keep the original language (Japanese stays Japanese; you may add an English gloss in
  parentheses). Copy numbers exactly. Write "[unclear]" for unreadable text; never guess.
- Do not invent any text, value, row, or element you cannot see.

Representation patterns — pick what fits:
- Ordered or positional content (array cells, bit fields, memory slots, timelines, packet
  layouts): a Markdown table preserving order; an empty box stays an empty cell. Put pointers
  and markers under it as a list, e.g. `head` -> position 0.
- Tables: recreate as a Markdown table with all headers, cells, units, and footnotes; explain
  merged cells after the table.
- Charts: name the chart type; transcribe axes, ticks, units, legend, series; recreate readable
  data as a table; state only visible trends.
- Screenshots: recreate the UI state in text: window titles, menus, buttons, fields, values,
  errors; note what is selected or highlighted.
- Photos / no text: state concretely what is depicted, without interpretation.
- Anything else spatial: structured prose describing layout and relationships.

{kind_hint}Transcribed strings (must all appear in your output):
{transcript}
{must_include}
Return only the Markdown replacement, no preface."""

KIND_HINTS = {
    "table": "This image is a table; the table pattern applies.\n\n",
    "chart": "This image is a chart; the chart pattern applies.\n\n",
    "screenshot": "This image is a screenshot; the screenshot pattern applies.\n\n",
    "photo": "This image is a photo; the photo pattern applies.\n\n",
    "text": "This image is mostly plain text; transcribe it as Markdown text.\n\n",
}


# =========================
# Stage calls
# =========================


def _content(images: list, prompt: str, context: str | None = None) -> list:
    blocks = []
    if context:
        blocks.append({"type": "text", "text": CONTEXT_HEADER.format(context=context)})
    blocks.extend(images)
    blocks.append({"type": "text", "text": prompt})
    return blocks


def _numbered(strings: list[str]) -> str:
    return "\n".join(f"{i}. {s}" for i, s in enumerate(strings, 1)) or "(none)"


async def classify(llm: Llm, images: list, context: str | None = None) -> str:
    result = await llm.ask_json(_content(images, CLASSIFY_PROMPT, context), Classification)
    return result.kind


async def transcribe(llm: Llm, images: list, context: str | None = None) -> list[str]:
    result = await llm.ask_json(_content(images, TRANSCRIBE_PROMPT, context), Transcript)
    return [s for s in result.strings if s.strip()]


async def extract_graph(
    llm: Llm, images: list, transcript: list[str], context: str | None = None
) -> DiagramGraph:
    prompt = NODES_PROMPT.format(transcript=_numbered(transcript))
    nodes = await llm.ask_json(_content(images, prompt, context), NodeExtraction)

    if not nodes.nodes:
        return DiagramGraph()

    node_lines = "\n".join(f"- {n.id}: {n.label}" for n in nodes.nodes)
    prompt = EDGES_PROMPT.format(
        node_ids=", ".join(n.id for n in nodes.nodes), nodes=node_lines
    )
    edges = await llm.ask_json(_content(images, prompt, context), EdgeExtraction)

    known = {n.id for n in nodes.nodes}
    return DiagramGraph(
        direction=edges.direction,
        nodes=nodes.nodes,
        groups=nodes.groups,
        edges=[e for e in edges.edges if e.src in known and e.dst in known],
    )


async def verify_graph(llm: Llm, images: list, graph: DiagramGraph) -> DiagramGraph:
    """One audit call; verdicts are applied in Python. No context: the judge must
    compare graph against image only."""
    if not graph.edges:
        return graph

    edge_lines = "\n".join(
        f"{i}. {e.src} -> {e.dst}"
        + (f' label "{e.label}"' if e.label else "")
        + f" ({'directed' if e.directed else 'undirected'}, {e.style}, cue: {e.cue})"
        for i, e in enumerate(graph.edges)
    )
    audit = await llm.ask_json(
        _content(images, VERIFY_PROMPT.format(edges=edge_lines)), GraphAudit
    )

    known = {n.id for n in graph.nodes}
    edges = list(graph.edges)
    for v in audit.verdicts:
        if not 0 <= v.edge_index < len(edges) or edges[v.edge_index] is None:
            continue
        if v.verdict == "reversed":
            e = edges[v.edge_index]
            edges[v.edge_index] = e.model_copy(update={"src": e.dst, "dst": e.src})
        elif v.verdict == "wrong":
            ok = v.corrected and v.corrected.src in known and v.corrected.dst in known
            edges[v.edge_index] = v.corrected if ok else None
        elif v.verdict == "not_visible":
            edges[v.edge_index] = None

    kept = [e for e in edges if e is not None]
    kept.extend(e for e in audit.missed_edges if e.src in known and e.dst in known)
    return graph.model_copy(update={"edges": kept})


async def describe_plain(
    llm: Llm,
    images: list,
    kind: str,
    transcript: list[str],
    context: str | None = None,
    must_include: list[str] | None = None,
) -> str:
    must = ""
    if must_include:
        must = (
            "\nYour previous answer omitted these visible strings; include every one:\n"
            + _numbered(must_include)
            + "\n"
        )
    prompt = PLAIN_PROMPT.format(
        kind_hint=KIND_HINTS.get(kind, ""),
        transcript=_numbered(transcript),
        must_include=must,
    )
    return await llm.ask_text(_content(images, prompt, context))


# =========================
# Deterministic coverage check (replaces the old LLM coverage judge)
# =========================

_WS_RE = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS_RE.sub("", unicodedata.normalize("NFKC", s)).casefold()


def missing_strings(transcript: list[str], text: str) -> list[str]:
    """Transcript strings (len >= 2 after normalization) not present in text."""
    haystack = _norm(text)
    missing = []
    for s in transcript:
        needle = _norm(s)
        if needle == "[unclear]" or len(needle) < 2:
            continue
        if needle not in haystack:
            missing.append(s)
    return missing
