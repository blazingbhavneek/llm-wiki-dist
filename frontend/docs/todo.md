# TODO — cascading effects on node change (deferred)

## Ingress inventory and endpoint tracker

Goal: every way a page/node enters the graph must land in the write queue, store
searchable text + embeddings, and create enough graph structure that a similar
future query can find/reuse the node.

| Ingress path | Node type | Frontend path | API/orchestrator | Graph write method | Status |
| --- | --- | --- | --- | --- | --- |
| Parser output directory (`docs/*.md` + planning metadata) | Endogenous | Not wired in current UI | `POST /api/ingest` -> `ingest_md_output` job | `GraphWriteSession.ingest_md_output()` | Available |
| Revised parser output directory for a known source | Endogenous replacement | Not wired in current UI | `POST /api/cascading-update` -> `cascading_update` job | `GraphWriteSession.cascading_update()` | Available, policy still below |
| Direct pasted/uploaded markdown | Endogenous | Upload tab -> editable draft -> Add to graph | `POST /api/document` -> `create_document` job | `GraphWriteSession.create_document_node()` | Added |
| PDF converted to markdown | Endogenous | PDF parser -> editable draft -> Add to graph | `POST /api/document` -> `create_document` job | `GraphWriteSession.create_document_node()` | Added |
| AI-agent answer saved as wiki note | Exogenous | Answer tab -> Add to wiki | `POST /api/exogenous` -> `create_exogenous` job | `GraphWriteSession.create_exogenous_node()` | Available |
| Human edit of an existing node | Preserves existing type | Markdown tab -> Save | `PUT /api/node/{node_id}` -> `update_node` job | `GraphWriteSession.update_node()` | Available, cascade policy still below |
| CLI add/cascade | Endogenous | CLI only | `graph add`, `graph cascade` | Intended to call graph ingest/cascade | Broken: `graph/cli.py` imports missing `Graph` facade |

Notes:
- Frontend write calls must poll `/api/write-jobs/{id}` before treating the
  response as a node; otherwise the UI races the write queue.
- Endogenous markdown uploads should never go through `/api/exogenous`; they are
  source material and need source version/hash tracking.
- Exogenous notes should receive the same "smart graph" treatment as source nodes:
  summaries, keywords, claims, FTS, vectors, semantic edges, support edges, and
  reclustering.

When a markdown node is edited in the UI and re-added to the graph, downstream
nodes/edges may need to be recomputed. Backend already exposes
`POST /api/cascading-update` and `update_node` enqueue. Decide policy:

## Endogenous node changed (source material)
- Source-of-truth changed. Options:
  - Re-embed node, re-suggest edges, mark neighbors that cited it as `stale`.
  - Recompute chain (`follows`) if the doc was split differently.
  - Trigger `cascading-update` for the whole `source_file` so derived exo notes
    that cited it get flagged/regenerated.
- Question: auto-supersede old version (status=`superseded`) vs in-place edit?

## Exogenous node changed (agent note)
- Derived artifact. Options:
  - Re-embed + re-suggest edges only for this node.
  - Do NOT cascade to sources (exo does not feed endo).
  - If exo cites endo nodes that since changed, offer "refresh from sources".
- Question: keep edit history / versions of agent notes?

## Shared open questions
- Recluster after edit? (cheap incremental vs full `recluster`).
- Conflict detection: edited node may now contradict a neighbor -> flag.
- UI: show a "graph is updating" indicator while write-job runs.
- Direct-markdown-as-source now has `POST /api/document`; follow-up is deciding
  whether large markdown bodies should be split into multiple chain-linked pages
  instead of one source node.
