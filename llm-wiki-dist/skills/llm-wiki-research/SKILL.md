---
name: llm-wiki-research
description: Research questions rigorously against an LLM-Wiki knowledge graph with a lead orchestrator and bounded exploration subagents, then queue durable cited knowledge back into the selected wiki. Use when answering from an LLM-Wiki MCP endpoint, traversing wiki nodes or relationships, comparing graph regions or conflicting sources, or preserving a reusable evidence-grounded result as an agent note.
---

# LLM-Wiki Research

Use the database selected by the MCP endpoint URL. Never accept, infer, or change a
wiki database name through tool arguments.

## Enforce Actor Boundaries

Keep the main agent as lead researcher and librarian. Let it analyze the question,
run compact seeding searches, select distinct routes, dispatch subagents, assess
coverage, synthesize the answer, and queue one final note.

Never let the main agent call `read_nodes` or `explore_links`. Full node bodies and
graph traversal belong exclusively to subagents.

After initial seeding, never let the main agent call `hybrid_search` again. Any gap
search needed during a second wave belongs to the subagent assigned that gap.

Never let subagents spawn more agents, queue notes, or produce the final user answer.
Make each subagent explore one assigned graph region and return a compact report.

## Seed the Graph

1. Identify the main question and its independent dimensions: entities, APIs,
   document names, rules, conditions, dates, values, procedures, exceptions, and
   plausible competing interpretations.
2. Call `hybrid_search` from the main agent for the full question and at most three
   dimension-specific variants. Set `limit` to at most 8 on every main-agent search.
3. Treat search results as routing previews, not evidence. Do not answer from titles
   or summaries alone.
4. Select non-duplicate starting nodes that cover different dimensions. Avoid near
   duplicates from the same document section or cluster when broader coverage exists.
5. Select at most six distinct routes total. Dispatch up to three in the first wave
   and preserve at most three for the second wave. Do not select a route that will
   remain undispatched.

## Dispatch Route Subagents

Give every subagent the original question, one starting node ID, the other assigned
starting IDs to avoid, and the following contract:

```text
Explore only the graph region rooted at START_ID.
Read START_ID first with read_nodes.
Follow useful edges with explore_links and run targeted hybrid_search queries when
the route reveals a missing term or named concept.
Do not read sibling start regions assigned to other agents.
Read at least 5 distinct relevant nodes before finishing; stop at 10.
Do not count repeated or missing nodes toward the read target.
Maintain a route-local frontier of every material edge and search lead discovered.
After 5 reads, finish only when each frontier item was read or explicitly closed as
irrelevant, duplicate, assigned to a sibling route, or blocked by the 10-read budget.
If material frontier items remain at 10 reads, report status incomplete and list them.
Do not finish before 5 reads. If fewer than 5 reachable nodes exist, follow every
available edge from the nodes read and run 2 targeted searches that yield no new
route-local IDs before reporting exhausted. State the read count, followed edges,
and empty searches that prove exhaustion.
Report findings, exact supporting node IDs, contradictions, uncertainty, and open leads.
Do not return full node bodies, raw tool output, transcripts, or chain-of-thought.
```

Dispatch independent routes concurrently when capacity permits. Dispatch every route
selected during seeding, including preserved second-wave routes. Wait for each one to
finish, fail, or prove exhaustion before synthesizing. Never silently drop a selected
route because another route already appears sufficient.

Treat an exogenous or agent-note seed as a lead, not automatically as truth. Require
its subagent to inspect the note and follow its `reference` or `supports` provenance
to source nodes before relying on it.

## Require Compact Reports

Require each subagent to return this shape:

```markdown
Route: <start node and topic>
Status: supported | contradicted | exhausted | incomplete
Findings: <concise facts relevant to the question>
Evidence node IDs: <exact IDs actually read>
Contradictions or uncertainty: <concise statement or none>
Open leads: <unexplored node IDs or queries, or none>
```

Keep each report under 400 words and keep at most six reports, bounding route-report
context to 2,400 words. Cite only nodes the subagent actually read. Do not send full
node text into the main context. Maintain one compact coverage ledger rather than
repeating reports during orchestration.

## Check Coverage Before Finishing

Map the returned reports back to every question dimension and initial seed. Do not
finish merely because one route produced a plausible answer.

Run at most one second wave. Dispatch every preserved route plus any unresolved
replacement, without exceeding six total routes. When an unresolved dimension has no
starting node ID, dispatch a subagent for that dimension; that subagent may run one
targeted `hybrid_search` with `limit` at most 8, choose one new route, and then follow
the same read and exhaustion contract. Across the second wave, permit at most two such
gap-search subagents.
Use the second wave when a material dimension is uncovered, two routes conflict,
provenance is missing, a condition or exception remains unclear, or a report exposes
a promising open lead.

Interpret "all routes" as all materially distinct routes surfaced by bounded seeding,
not every path in a cyclic graph. Before synthesis, verify that every selected route
has a report or an explicit failed/unavailable status. Record uncertainty when the
configured read or six-route budget prevents closure.

## Synthesize on the Main Agent

Answer only from the compact subagent reports and their evidence IDs. Resolve
agreement and disagreement explicitly. Separate established facts from inference and
missing information. Never invent a document, value, procedure, condition, or citation.

Keep orchestration, route comparison, citation selection, and final composition on the
main agent. Do not delegate the final synthesis.

## Queue Durable Knowledge

After producing a reusable evidence-grounded answer, call `queue_agent_note` exactly
once with the original question, the durable synthesized answer as `body`, and only
the node IDs that directly support it as `source_node_ids`.

Treat a returned `queued` job as success. Continue immediately and never poll the job,
wait for assimilation, or delay the user response. The backend librarian serializes
the job with other graph writes and performs enrichment later.

Do not queue partial route reports, duplicate notes, unsupported speculation, secrets,
raw conversations, or chain-of-thought. If submission fails or the queue is full,
deliver the researched answer anyway and state briefly that persistence did not occur.
