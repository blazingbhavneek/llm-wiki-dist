# Developer guide

This document maps the implemented system to files and current line ranges. Line
ranges are a snapshot of the current tree; after a large edit, rerun the
commands in the final section to refresh them.

The four implementation plans describe intent and acceptance criteria:

- PLAN_SYNC.md: one data root, mount conversion, raw Git changes, wiki output.
- PLAN_FORMATS.md: DOCX/PPTX/PDF/table-aware structure and Excel records.
- PLAN_GROWI.md: GROWI authority, scopes, registry, publication, reverse sync.
- PLAN_NEO.md: observe -> plan -> section-wise lossless wiki writing.

The code is the final authority when a plan and implementation differ.

## 1. Architecture in one page

~~~text
mount/
  original DOCX/PPTX/PDF/XLSX/CSV files
       |
       | graph/convert.py -> parser/server.py /parse
       v
raw/                         normalized Markdown, one Git repository
       |
       | graph/sync.py
       v
graph/writers.py             chooses format and ingest mode
       |
       +--> graph/formats/   structural/table-aware seed planning
       +--> graph/wiki/      lossless wiki writer
       +--> graph/pages.py   shelf/page writer
       +--> graph/chunk.py   historical chunk writer
       v
wiki/                        local generated/export Markdown
       |
       | graph/growi.py -> GROWI HTTP API
       v
GROWI                       editable/canonical wiki pages
       |
       | graph/growi.py + graph/librarian.py
       v
graph.sqlite                 nodes, edges, FTS5, sqlite-vec vectors
       |
       v
app.py -> researcher.py -> frontend / MCP
~~~

There are two independent directions:

1. Source direction: mount -> parser -> raw -> generated wiki -> GROWI.
2. Index direction: GROWI -> changed document folders -> graph revision.

A sync_raw job normally queues or is followed by a sync_growi job. A
GROWI-only edit needs only sync_growi/connection resync. A graph rebuild needs
neither a new vector database nor a new source conversion: preserve GROWI and
rebuild the derived graph.

All graph writes go through the Librarian queue. Reads use read-only
GraphStore instances. Team scopes are read-only SQLite TEMP views over the
same graph file.

## 2. Data and ownership contracts

### Project paths: graph/project.py

graph/project.py lines 1-113 owns the data-root contract:

- lines 9-15: raw filename <-> original extension mapping.
- lines 23-29: reserved team names and fallback general team extraction.
- lines 31-72: Project paths:
  - root
  - mount
  - raw
  - metadata
  - wiki
  - graph.sqlite
  - engine.sqlite
  - metadata/last_sha
  - metadata/convert.json
- lines 74-102: raw-file, wiki-folder, state-folder, work-folder, directory
  creation, raw-file listing, and team discovery.
- lines 105-113: ZIP export; _planning files are omitted.

Change this file when changing a path contract, extension mapping, scope-folder
discovery, or ZIP contents. Do not put parser, GROWI, or graph logic here.

### Generated data

The persistent data contract is:

~~~text
data/mount/                       external/source files
data/raw/                         parser Markdown + .git
data/metadata/convert.json       mount conversion ledger
data/metadata/last_sha           raw commit consumed by sync
data/metadata/state/              resumable wiki writer state
data/metadata/work/               temporary writer staging
data/wiki/                        generated local wiki/export
data/graph.sqlite                 derived graph/index/vector store
data/engine.sqlite                encrypted GROWI registry/page ledger
~~~

mount/, raw/, and wiki/ are file interfaces. graph.sqlite is not an
authoritative wiki store. engine.sqlite is not the graph; it stores connection
and remote-page synchronization metadata.

### SQLite and sqlite-vec: graph/store.py

graph/store.py lines 95-258 own connection behavior:

- lines 99-110: GraphStore path, readonly, and scope arguments.
- lines 112-168: thread-local SQLite connections.
- lines 170-238: URI opening and connection setup.
- lines 240-258: sqlite-vec loading and scoped TEMP views. A team view filters
  nodes, edges, and search_items by team.

Schema and persistence:

- lines 279-390: core schema, metadata, nodes, edges, source ledger.
- lines 391-486: node/edge migration and indexes.
- lines 487-552: vector tables and dimension metadata.
- lines 553-588: graph metadata and commit helpers.
- lines 589-699: transactions and database snapshots.
- lines 700-1024: node reads/writes/deletion.
- lines 1025-1245: edge operations and graph traversal.
- lines 1246-1382: neighborhood, scope/team, and source access.
- lines 1383-1493: source ledger and FTS5 indexing.
- lines 1495-1625: vector storage, deletion, retrieval, and KNN.
- lines 1661-1845: search-item persistence and FTS5 search-item lookup.

When changing the schema, update migrations, row conversion, deletion paths, and
tests. When changing scope behavior, update the TEMP views and verify that
both node search and edge traversal remain scoped. Do not open a scoped store
as writable.

### Vector seam: graph/vectors.py

- lines 9-29: VectorIndex protocol.
- lines 31-92: supported SqliteVecIndex, backed by GraphStore tables.
- lines 94-455: legacy QdrantIndex compatibility implementation.

The supported runtime vector store is sqlite-vec inside graph.sqlite. Do not
make Qdrant or LanceDB a new dependency for this phase. If vector retrieval
needs improvement, first edit the GraphStore/sqlite-vec path and its tests.
Keep the seam only because it keeps retrieval code independent of storage.

## 3. HTTP application and scopes

### Startup and stack: app.py

app.py is transport, lifecycle, routing, and API composition. It should not
contain document-format algorithms.

- lines 1-90: imports, app-level models, serialization/error helpers.
- lines 94-126: runtime globals, DATA_ROOT, current scope context, and
  compatibility flags.
- lines 143-179: _build_stack; creates Settings, ModelGateway, one writable
  GraphStore, Librarian, and read-only Researcher.
- lines 181-215: _bootstrap_engine; validates the registered GROWI connection,
  builds the stack, starts the queue, and enqueues initial sync.
- lines 217-265: stack closure and scoped-store cleanup.
- lines 268-326: FastAPI lifespan and periodic sync timer.
- lines 327-340: _ready_stack; lazily creates a read-only scoped GraphStore.
- lines 342-866: helper models, serialization, admin auth, and legacy
  compatibility helpers.
- lines 868-955: prefix/scope URL middleware.
- lines 958-980: HTTP and exception formatting.
- lines 982-1010: readiness and scope APIs.
- lines 1013-1025: current scope and write-scope guard.
- lines 1028-1040: public GROWI connection summary.
- lines 1043-1191: admin connection registration, patch, test, resync, and
  delete endpoints.
- lines 1194-1216: admin status and admin sync.
- lines 1219-1590: legacy database-management endpoints. They remain for older
  callers; do not use them as the new project/GROWI design.
- lines 1805-1879: runtime settings read/replace/patch/reset and admin resync.
- lines 1893-1992: graph, health, node, link, search, and query reads.
- lines 2015-2205: ask, streaming ask, realtime ask, and agent stop.
- lines 2213-2330: write queue adapter, node/document writes, uploads,
  recluster, cascade, ingest, sync, and ZIP export.
- lines 2333-2366: write-job list/status/cancel.
- lines 2373-end: admin/static frontend serving and application mount.

When adding an API endpoint, route it through _ready_stack() and the current
scope, use _enqueue() for writes, validate user paths at the boundary, and add
a route test. Do not mutate GraphStore directly in a FastAPI handler.

### URL routing

The middleware at app.py lines 868-955 owns:

~~~text
<PREFIX>/<scope>/             SPA and scoped API
<PREFIX>/<scope>/api/...      current team/all GraphStore
<PREFIX>/all/                 aggregate read scope
<PREFIX>/admin/api/...        prefix-level administration
~~~

all maps to scope=None in read operations but is not a writable team.
_require_team() at lines 1018-1025 rejects scoped writes from /all/.

If changing URL layout, update all of:

- app.py middleware and _db_url helpers;
- frontend/src/api.js base URL detection;
- frontend scope switcher/useWorkspace behavior;
- mcp_server.py routing;
- app and routing tests.

### Settings: graph/core.py

graph/core.py is the shared configuration and domain model module:

- lines 25-89: Settings defaults for chat, embeddings, reranking, database,
  edges, and search.
- lines 91-138: ingest modes, data root, parser, legacy vector fields,
  GROWI/sync fields, format settings, concurrency, and retrieval settings.
- lines 140-185: retrieval weights, evidence, agent, service, and Mermaid
  settings.
- lines 187-348: Settings.from_env mapping.
- lines 361-367: NodeType values: endogenous, exogenous, page, table.
- lines 369-375: NodeStatus values.
- lines 377-401: Node model and source/team/version fields.
- lines 404 onward: Edge and domain helpers.
- approximately lines 1067 onward: hashing, node IDs, chunk sizing, and
  image/table search-text helpers.

If adding a setting, add the typed default, environment mapping, runtime
serialization behavior, UI control if user-facing, and a focused test. Keep
secrets out of settings responses; app.py redacts them around lines 1805-1879.

## 4. Conversion, sync, and writer dispatch

### Conversion: graph/convert.py

- lines 1-16: imports and parser skip names.
- lines 19-20: unsupported-document error.
- lines 23-50: parser /parse HTTP request, multipart upload, parser headers,
  timeout, and Markdown response validation.
- lines 53-60: file stat key and raw target naming.
- lines 62-127: mount scan, incremental conversion ledger, raw writes,
  unsupported/failure handling, vanished-file cleanup, and raw Git commit.

Change this file for parser request shape, mount exclusions, conversion caching,
or conversion failure policy. Add/update tests/test_convert.py.

### Git source tracking: graph/sync.py

- lines 1-27: Git helper types and identity.
- lines 29-43: subprocess Git wrapper and repository initialization.
- lines 45-67: HEAD/last_sha and raw commit helpers.
- lines 70-90: A/M/D raw change planning.
- lines 93-109: changed-hunk extraction.
- lines 112-206: sync_raw orchestration:
  - optional mount conversion;
  - changed raw-file selection;
  - deletion;
  - wiki invalidation;
  - wiki generation;
  - GROWI publication;
  - index regeneration;
  - last_sha update.
- lines 209 onward: status/progress and compatibility adapter behavior.

Change this file when changing what counts as a source change or the order of
conversion/writer/publication. The raw repository is a deliberate incremental
ledger; do not bypass it with mtime-only graph ingestion.

### Writer dispatch and publication: graph/writers.py

- lines 16-39: Settings -> WikiConfig conversion.
- lines 42-67: wiki pipeline invocation.
- lines 70-141: build_wiki_output; tabular format dispatch, wiki mode,
  page mode, and chunk mode selection.
- lines 144-158: staged docs/_planning publication.
- lines 161-197: write one document, clean work output, publish, and stamp.
- lines 200-224: source hash stamps and up-to-date checks.
- lines 227-239: generated wiki index.

If a new format or mode is needed, start here only to select it; put its
algorithm in graph/formats or its own pipeline. Update tests/test_writers.py
and the format-specific tests.

## 5. Format-aware wiki generation

### Shared format dispatch: graph/formats/__init__.py

- lines 1-11: format module setup.
- lines 12-21: kind detection and tabular classification.
- lines 23-41: structural seed-plan dispatch.

Source names are significant. A raw file ending in _docx.md, _pptx.md,
_xlsx.md, _csv.md, or _pdf.md selects the corresponding format. Unknown
Markdown uses md behavior.

### Boundary-safe structure: graph/formats/tree.py

- lines 1-17: section/tree models.
- lines 18-60: heading-tree construction and safe section boundaries.
- lines 61-109: lead/divider and page splitting.
- lines 110-156: greedy packing and SeedRange conversion.

This is the right file for deterministic structure boundaries and target-size
packing. It is not the right file for prose generation.

### Parent and sibling context: graph/formats/context.py

- lines 1-23: page-line and ParentSummary models.
- lines 25-38: hierarchy-summary prompt.
- lines 40-70: one LLM summary call per parent, checkpointing, and fallback.
- lines 72-89: context block containing hierarchy, parent summary, sibling
  summaries, and previous/next navigation.

If writer context is missing or too large, change this file and its tests; do
not add a summary call inside the per-page loop.

### DOCX: graph/formats/docx.py

- lines 1-29: heading-tree structural plan and fallback when headings are
  unusable.

### PPTX: graph/formats/pptx.py

- lines 1-31: slide block splitting and delimiter handling.
- lines 32-80: divider candidates and cheap structural observations.
- lines 81-142: one deck-level structured judge, atomic slide grouping, and
  target-size splitting.

The deck-level decision is intentionally not one LLM call per slide/page.

### PDF: graph/formats/pdf.py

- lines 1-33: optional heading-aware PDF structural planning.
- Default PDF behavior remains the ordinary observed-window path unless
  WIKI_PDF_USE_HEADINGS is enabled.

### XLSX: graph/formats/xlsx.py

- lines 1-14: sheet marker splitting and source region extraction.
- lines 15-27: workbook runner and tabular runner integration.

### CSV: graph/formats/csv.py

- lines 1-14: one-table CSV runner.

### Excel/table pipeline: graph/formats/tabular.py

This is the main table implementation:

- lines 1-38: models, parsing constants, and normalized table types.
- lines 39-130: HTML/GFM table parsing and source-region discovery.
- lines 131-218: table region previews, column/row normalization, and
  deterministic range helpers.
- lines 219-300: TableSpec and SheetStructure validation.
- lines 301-365: per-sheet LLM structure decision and retry/fallback logic.
- lines 366-420: record extraction, numeric/statistical summaries, and metadata.
- lines 421-463: verbatim table rendering, analysis-page rendering, and
  citation/range markers.
- lines 464-505: in-memory records, safe SELECT query, result limits, and
  table output assembly.

The contract is:

1. Python creates a bounded region preview.
2. One structured LLM call decides each sheet/table structure.
3. Python validates ranges.
4. Invalid structure is retried; after two failures, deterministic heuristic
   structure is used.
5. The original table remains available in generated Markdown.
6. Records are embedded in table pages for graph/research querying.

For changes to Excel row queries, update tabular.py, researcher.py's
query_table path, and table tests together.

## 6. Lossless wiki writer

### Wiki configuration and models

- graph/wiki/config.py lines 12-65: WikiConfig fields and defaults.
- graph/wiki/wire.py lines 8-104: observed ranges, regional plans, seed
  ranges, reference facts, and page-judge schemas.
- graph/wiki/model.py lines 20-101: ModelPort and ChatModelPort structured/text
  model calls.
- graph/wiki/storage.py lines 21-128: atomic writes, hashes, line splitting,
  normalization, and source slicing.
- graph/wiki/ids.py lines 17-29: stable document/window/image IDs.
- graph/wiki/images.py lines 39-267: protected image extraction, sanitization,
  placeholder restoration, and image integrity checks.
- graph/wiki/page.py lines 31-263: section splitting, code/table boundary
  checks, coverage assignment, and title-link insertion.

### Observe and plan

- graph/wiki/windows.py:
  - lines 33-64: observation errors and overlapping windows.
  - lines 66-176: inventory cleanup/validation and prompts.
  - lines 177-301: one-window observation call and result handling.
  - lines 302 onward: document observation orchestration.
- graph/wiki/document_map.py:
  - lines 33-120: seed-plan models and regional/seed validation.
  - lines 120-252: source coverage, overlap, block, and boundary checks.
  - lines 253-324: deterministic repairs/fallback.
  - lines 325-535: regional and semantic plan construction.
  - lines 536-711: plan compilation and retries.
  - lines 712 onward: complete seed-plan build.

### Rewrite, references, and final artifacts: graph/wiki/pipeline.py

- lines 73-101: pipeline errors and rewrite/seed result models.
- lines 120-212: reference and navigation helpers.
- lines 214-459: page-plan assembly and section allocation.
- lines 460-684: parent/reference research and link planning.
- lines 707-755: seed loading and page sidecar state.
- lines 756-885: resume logic and section-page preparation.
- lines 886-1074: section and introduction writers.
- lines 1075-1243: page rewrite and concurrent rewrite orchestration.
- lines 1244-1318: index, mechanical verification, manifest, and metadata.
- lines 1319-end: run_pipeline lifecycle, source snapshot, resumable state,
  structural format integration, staged wiki output, and progress emission.

The lossless rule belongs here and in graph/wiki/page.py: the source is copied
or covered by ranges, generated text is checked, protected blocks are
restored, and invalid pages are retried/fallbacked. Do not weaken validation
to make a model response look better.

### Legacy page/chunk modes

- graph/pages.py lines 40-119: shelf/page/route/stitch models.
- lines 128-382: chunk summaries and shelf planning.
- lines 396-521: semantic routing and parked-chunk handling.
- lines 537-613: page sizing and marker helpers.
- lines 619-743: page assembly and output.
- lines 744-856: chunk preservation and stitch operations.
- lines 857-986: async/synchronous page pipeline entry points.

- graph/chunk.py lines 57-153: historical chunk models.
- lines 155-365: source/file/chunk utility functions.
- lines 370-976: safe boundaries, tables, fences, and source windows.
- lines 976-1075: LLM construction and structured invocation.
- lines 1076-1204: concept split schemas.
- lines 1231-1671: partition validation and repair.
- lines 1721-2051: split prompts and concept planning.
- lines 2052-2290: output/manifest rendering.
- lines 2291-2551: enrichment and coverage metadata.
- lines 2551-2625: rendered/source assertions.
- lines 2626-2860: chunk pipeline entry points and async blocking bridge.

Do not delete these modes merely because wiki mode is the current default; tests
and existing callers still use them. Do not put new format logic into the
legacy chunker.

## 7. GROWI authority and synchronization

### HTTP client and publication: graph/growi.py

- lines 19-38: GROWI API error and page models.
- lines 40-128: HTTP client, authentication, request timeout, and retries.
- lines 129-230: health, page fetch, pagination, create/update/delete.
- lines 231-257: safe path segments, path construction, and team extraction.
- lines 258-293: generated-page markers, source ranges, and publish-boundary
  validation.
- lines 294-325: generated-section merge and marker handling.
- lines 326-363: page publication helpers.
- lines 364-383: generated file canonical names and coverage.
- lines 385-437: GrowiPublisher; local wiki document -> GROWI pages and
  generated-page cleanup.
- lines 438-524: legacy page sync compatibility.
- lines 525-586: current folder-grouped GROWI diff sync.
- lines 587-606: async callback and registry-page adapters.

Change this file for GROWI API shape, path boundaries, publication markers,
remote deletion, or remote page grouping. Test client behavior in
tests/test_growi_client.py, publication in tests/test_growi_publish.py,
sync in tests/test_growi_sync.py, and routing separately.

### Registry: graph/registry.py

- lines 18-39: GrowiConnection and GrowiPageIndex models.
- lines 41-80: engine.sqlite schema and connection.
- lines 82-115: WIKI_SECRET_KEY-derived token encryption/decryption.
- lines 117-168: register/upsert validation.
- lines 170-217: get/list/update/delete/public redacted summaries.
- lines 219-263: remote-page ledger and sync cursor/error tracking.

The registry is the durable connection/page ledger. Never log or return the
decrypted API token. If changing fields, update the schema migration path and
tests/test_registry.py plus tests/test_admin_connections.py.

### Librarian: graph/librarian.py

This is the write coordinator and graph ingestion implementation:

- lines 113-156: errors, document-ingest, and WriteJob models.
- lines 157-200: job serialization and worker support types.
- lines 211-318: Librarian initialization, sqlite-vec seam, locks, bounded
  write queue, enrichment queue, and runtime settings.
- lines 320-345: start/stop worker and enrichment lifecycle.
- lines 346-440: enqueue, single worker loop, job completion/pruning.
- lines 440-531: transaction/snapshot policy and interrupted-ingest recovery.
- lines 542-612: job dispatch table.
- lines 614-664: list/get/cancel job API.
- lines 668-895: background enrichment and cascade queue.
- lines 898-1088: bootstrap, migration, vector setup, and search initialization.
- lines 1089-1190: search-item/vector bootstrap and cluster preparation.
- lines 1195-1382: node update/delete, document delete, and exogenous node
  creation.
- lines 1506-1657: ingest preparation, concurrent node processing, and linking.
- lines 1658-1737: legacy Markdown-output ingestion.
- lines 1738-1854: chunk-and-ingest compatibility path.
- lines 1855-1894: sync_raw job adapter; calls graph.sync.sync_raw and publishes
  through GrowiPublisher.
- lines 1896-1973: sync_growi job adapter; turns changed GROWI folders into
  page/table nodes and revises the graph.
- lines 2019-2171: document revision matching, unchanged/superseded/stale
  decisions, and source versioning.
- lines 2172-2420: one-node ingestion, vector storage, KNN candidates, and
  semantic edges.
- lines 2420-2635: search-item/KNN retrieval.
- lines 2636-3221: derived fields, semantic edges, supersession, support,
  references, clusters, neighborhoods, and structural edges.
- lines 3222-end: cascades, reclustering, legacy loaders, planning-document
  loaders, and chain edges.

When changing ingestion concurrency, keep the single SQLite write lock and
snapshot/recovery behavior. When changing GROWI sync node typing, preserve the
table marker check around lines 1908-1926 and source/team/version fields.

The important revision behavior is in lines 2019 onward:

- exact material hashes remain unchanged;
- changed pages get new/current nodes;
- old pages become stale/superseded;
- removed pages are deleted/staled through document deletion;
- vectors and graph links are rebuilt for changed nodes;
- dependent generated work is cascaded within configured limits.

## 8. Search, research, and table questions

### Researcher: graph/researcher.py

- lines 94-147: image mode, usage logging, and model helpers.
- lines 148-335: stop handling, model compilation, message cleanup, and
  search-result formatting.
- lines 336-363: lead-agent tool argument models.
- lines 364-519: lead context, search, explore, finish, and tool definitions.
- lines 520-623: lead agent execution.
- lines 624-642: subagent context and QueryTableArgs.
- lines 643-657: _query_table; validates table node/records and executes safe
  read-only SQL over an in-memory table.
- lines 658-880: subagent search/read/follow/query_table tools.
- lines 881-969: subagent execution.
- lines 970-1213: ResearchSession setup, overrides, read graph, health, and
  search.
- lines 1214-1486: evidence retrieval, reranking, context construction, and
  answer preparation.
- lines 1487-1889: ask flow, early exit/shallow/deep routing, lead/subagents,
  source citations, and answer result.
- lines 1891-2032: Researcher facade and synchronous ask.
- lines 2032-end: realtime streaming research and event emission.

For numeric table questions, update graph/formats/tabular.py and the
researcher query-table contract together. The query tool must remain bounded,
SELECT-only, and isolated from graph.sqlite.

### API read paths: app.py

- lines 1893-1992 expose graph/health/node/link/search/query.
- lines 2015-2205 expose ordinary, SSE, and realtime ask.
- app.py serializes/restricts the result; researcher owns retrieval and agent
  logic.

### MCP proxy: mcp_server.py

- lines 1-40: imports, route defaults, backend origin, timeout, and scope
  allowlist.
- lines 42-188: image/data sanitization helpers for text-only tools.
- lines 191-285: WikiRoutingApp; maps <PREFIX>/<scope>/mcp to /mcp.
- lines 287-348: backend error and readiness handling.
- lines 350-405: scoped backend HTTP JSON helper.
- lines 406 onward: MCP tool definitions and proxy calls.
- tool definitions include search/read/follow/ask/table query and queued
  agent-note behavior; inspect their names in this range before changing a
  tool contract.
- final lines: uvicorn/FastMCP startup and host/port arguments.

MCP is a stateless proxy. It does not own graph state or a second index. When
changing a backend API path, update app.py, frontend/src/api.js, and the
corresponding MCP proxy call.

## 9. Frontend ownership map

The frontend is under llm-wiki-dist/frontend/src. It is built into
frontend/dist and served by app.py.

### App composition

- App.jsx lines 1-120: imports, translations, state, and service hooks.
- lines 120-250: graph/workspace/search/ask/write hook composition.
- lines 250-540: center view, chat/search/settings/upload routing, and state
  transitions.
- lines 550-620: shell layout and RightDocumentRail integration.

### API and data transforms

- api.js lines 1-90: base URL, error parsing, write-job polling, and queue
  helper.
- api.js lines 92-219: all frontend API methods, streaming ask parser, and
  document/sync/settings requests.
- data/docs.js lines 1-89: document/chain/topic sorting helpers.
- lines 90-179: flat graph -> document library/topic model.
- lines 180-212: chain neighbors and full-document reconstruction.
- data/layout.js lines 8-318: deterministic graph layout and node-to-workspace
  conversion.
- data/growi.js lines 1-75: browser GROWI preference and page-link generation.
- data/download.js lines 1-90: Markdown download/export formatting.
- data/utils.js lines 1-20: application prefix/favicon detection.

### Hooks

- hooks/useGraphData.js lines 1-61: document/time sorting helpers.
- lines 62-170: readiness, graph/health loading, polling, memoized layout and
  library construction.
- hooks/useWorkspace.js lines 1-260: center workspace tabs, document drafts,
  full-document reconstruction, node opening, edits, saves, and history.
- hooks/useSearch.js lines 1-66: search request state and search-result center.
- hooks/useAskStream.js lines 1-205: SSE ask lifecycle, activity events,
  citations, answer messages, and stop/reset.
- hooks/useGraphWrites.js lines 6-100: write-job submission/status and labels.
- hooks/useAssimilation.js lines 10-80: background enrichment polling.
- hooks/useOverrides.js lines 4-40: browser-only request overrides.

### Layout and document navigation

- components/layout/Shell.jsx lines 1-66: generic shell/placeholder/header.
- components/layout/LeftSidebar.jsx lines 18-139: left navigation/sidebar
  collapse and main navigation.
- components/layout/RightDocumentRail.jsx lines 31-229: right knowledge rail,
  width state, drag/keyboard resize, viewport clamping, and maximum width.
- RightDocumentRail.jsx lines 230-467: cited/mentioned source document list,
  source grouping, and document actions.
- RightDocumentRail.jsx lines 468-end: node ID normalization and source
  lookup helpers.
- components/DocSidebar.jsx lines 55-193: document/topic tabs, filtering, and
  folder-tree entry point.
- DocSidebar.jsx lines 194-270: recursive folder/document tree rendering.
- DocSidebar.jsx lines 271-339: path parsing, scope stripping, labels, and tree
  construction.
- DocSidebar.jsx lines 340-477: knowledge cards, download/delete actions, and
  node listing.
- DocSidebar.jsx lines 478-544: document/node IDs, labels, and active state.
- components/layout/SearchResults.jsx lines 7-106: search result center.
- components/layout/MarkdownWorkspaceFrame.jsx: workspace document frame.
- components/layout/TopBar.jsx lines 1-150: scope/header controls and right-rail
  toggle.
- components/layout/SettingsCenter.jsx and UploadCenter.jsx: center overlays.

### Main feature components

- ChatPanel.jsx lines 98-228: chat input, streaming request controls, and
  prompt actions.
- ChatPanel.jsx lines 229-620: chat summary, messages, references, related
  concepts, activity, and answer actions.
- ChatPanel.jsx lines 621-811: Markdown answer rendering and table handling.
- ChatPanel.jsx lines 812-end: citation/node-link parsing and source ID
  normalization.
- MarkdownView.jsx lines 18-350: node/document preview, edit state, references,
  and Markdown display.
- MarkdownRenderer.jsx lines 1-530: sanitized Markdown, links, tables, images,
  Mermaid, and node links.
- RichMarkdownEditor.jsx lines 1-end: editable Markdown/table/image blocks.
- UploadView.jsx and UploadCenter.jsx: document upload/draft flow.
- PdfParserView.jsx lines 132-end: parser upload queue, task polling, result
  retrieval, Mermaid/image options, and UI queue.
- QueueView.jsx lines 139-260: graph write-job/assimilation queue display and
  cancellation.
- SettingsView.jsx lines 67-1800: runtime settings form, persisted client
  overrides, validation, and PATCH /api/settings.
- GraphCanvas.jsx lines 47-386: graph visualization and selection.
- MermaidDiagram.jsx lines 20-176: Mermaid rendering/repair UI.
- index.css lines 1-330: app layout styling, Markdown/table rules, graph
  styling, and responsive behavior.

When changing folder display, edit DocSidebar path/tree helpers first. When
changing the resizable knowledge rail, edit RightDocumentRail width/clamp
logic and its layout classes; do not add a separate global layout manager.

## 10. Parser and containers

### parser/server.py

- lines 1-180: parser configuration, limits, task state, and utility helpers.
- lines 182-331: queue/task lifecycle and API error handlers.
- lines 341-682: PDF hashing, image/vision smoke test, worker process, and
  parser result construction.
- lines 682-838: task queue/status helpers and scheduler.
- lines 839-1017: POST /upload, deduplication, task creation, and queueing.
- lines 1018-1159: GET /status, GET /queue, and DELETE /queue/task.
- lines 1160-1190: GET /result/task.
- remaining lines: static/service startup and runtime configuration.

The parser's /parse endpoint and its PDF queue endpoints are different
interfaces. graph/convert.py calls /parse; the PDF parser UI uses the PDF queue.

### parser/Dockerfile

The parser image owns MinerU/vLLM/system libraries, GPU settings, model
installation, and parser port exposure. Change it only for parser image/runtime
dependencies. Keep LLM-Wiki application image changes in llm-wiki-dist/Dockerfile.

### llm-wiki-dist/Dockerfile

- early lines: Ubuntu/proxy/base environment.
- approximately lines 211-240: application work directory and browser config.
- lines 330-348: Python lockfile copy and locked uv environment setup.
- lines 351-400: frontend dependency/build layers and source copy.
- lines 402-501: startup script, backend/MCP tmux processes, ports, data volume,
  and container command.

The app image exposes container ports 8000 (backend), 8001 (MCP), and 22
(SSH). Host port mappings are deployment choices.

### growi-stack/docker-compose.yml

- lines 1-15: MongoDB 6 service, persistent volume, healthcheck.
- lines 17-35: GROWI service, Mongo connection, local file upload, port 3000,
  and persistent volume.
- lines 37-40: named volumes.

This stack is intentionally separate from the app container and contains no
Qdrant/LanceDB/Elasticsearch service.

## 11. Tests by responsibility

All tests use unittest. The smallest useful commands are:

~~~bash
cd /mnt/common/Code/llm-wiki-dist/llm-wiki-dist
.venv/bin/python -m unittest tests.test_project tests.test_convert tests.test_sync
.venv/bin/python -m unittest tests.test_writers tests.test_wiki_chunking tests.test_wiki_page tests.test_wiki_incremental
.venv/bin/python -m unittest tests.test_growi_client tests.test_growi_publish tests.test_growi_sync tests.test_growi_routing tests.test_registry tests.test_admin_connections
.venv/bin/python -m unittest tests.test_vectors tests.test_neighborhood tests.test_vocab
.venv/bin/python -m unittest tests.test_pages_shelf tests.test_pages_router tests.test_pages_assemble tests.test_pages_stitch tests.test_pages_wire
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
~~~
Test module map:

- test_project.py: Project path/name/team/ZIP contracts.
- test_convert.py: mount conversion cache, naming, failures, vanished files.
- test_sync.py: raw Git change planning and sync orchestration.
- test_writers.py: format/mode dispatch and writer output.
- test_wiki_chunking.py: observation, planning, lossless rewrite, references,
  hierarchy, images, and wiki publication behavior.
- test_wiki_page.py: safe Markdown sections, coverage, lossless checks, title
  links.
- test_wiki_incremental.py: same-line-count incremental invalidation and
  full-rewrite fallback.
- test_page_settings.py: page-mode settings.
- test_pages_shelf.py: shelf creation/finalization/size behavior.
- test_pages_router.py: page routing decisions.
- test_pages_assemble.py: page assembly and source markers.
- test_pages_stitch.py: page stitch operations.
- test_pages_wire.py: page schema serialization.
- test_growi_client.py: HTTP client request/auth/pagination/path operations.
- test_growi_publish.py: generated page publication and cleanup markers.
- test_growi_sync.py: remote page ledger, touched document grouping, revision,
  deletion, and callbacks.
- test_growi_routing.py: app prefix/scope routing and scope boundaries.
- test_registry.py: encrypted registry, connection lifecycle, and page ledger.
- test_admin_connections.py: admin endpoint auth/register/test/resync/delete.
- test_vectors.py: SqliteVecIndex seam.
- test_qdrant_vectors.py: legacy Qdrant adapter compatibility only; it does not
  make Qdrant a supported runtime dependency.
- test_neighborhood.py: graph neighborhood/structural neighborhood.
- test_vocab.py: vocabulary building, matching, names, and transliteration.
- test_ingestion_concurrency.py: chunk/graph concurrency and ingest settings.
- test_realtime.py: research routing, budgets, evidence, rerank, grounding,
  deep-stage, and anticipation behavior.
- test_ask_realtime.py: realtime API/session behavior and profile settings.
- test_wiki_export.py and test_wiki_zip.py: local/staged wiki export.
- test_realtime.py: agent/research behavior without requiring a live model.

For any live-service test, record the chat, embedding, reranker, parser, and
GROWI endpoint assumptions separately. Do not make unit tests depend on the
developer's current data/ directory.

## 12. Edit recipes

### Change the mount-to-raw contract

Edit:

1. graph/project.py, lines 9-20 for naming.
2. graph/convert.py, lines 53-127 for conversion targets/ledger.
3. graph/sync.py, lines 70-206 for Git change selection.
4. tests/test_project.py and tests/test_convert.py.
5. docs/RUNNING.md data-layout section.

Do not edit GROWI code unless the resulting document path also changes.

### Change wiki page structure

Edit:

1. graph/formats/tree.py for deterministic headings/boundaries.
2. graph/formats/<format>.py for format-specific structural rules.
3. graph/formats/context.py for parent/sibling context.
4. graph/wiki/document_map.py for plan validation/repair.
5. graph/wiki/pipeline.py for writing/verification.
6. matching writer tests.

For Excel, use graph/formats/tabular.py rather than graph/wiki/tree.py.

### Change Excel row query behavior

Edit:

1. graph/formats/tabular.py lines 301-505 for structure, records, marker,
   rendering, and safe SQL.
2. graph/librarian.py lines 1908-1926 for table-node recognition.
3. graph/researcher.py lines 636-657 and 828-880 for the query_table tool.
4. graph/researcher.py lines 1852-1853 for table-question instruction.
5. tests covering tabular output/research/table behavior.

Keep the query in-memory, SELECT-only, and bounded.

### Change GROWI paths or ownership

Edit:

1. graph/registry.py connection fields and validation.
2. graph/growi.py path boundary/publication/sync helpers.
3. graph/project.py only if local document paths change.
4. graph/librarian.py lines 1855-1973 for job integration.
5. app.py lines 1028-1191 for admin API.
6. tests/test_growi_*.py, test_registry.py, and routing tests.

Never change the graph database to become the wiki source of truth.

### Change search ranking or retrieval

Edit:

1. graph/store.py search/vector/FTS operations.
2. graph/librarian.py vector/search-item creation.
3. graph/researcher.py candidate pool, reranking, evidence, and agent reads.
4. graph/core.py settings/weights.
5. app.py only for API parameters/serialization.
6. frontend search/answer files only for display.

Do not add a second vector backend to solve a ranking issue.

### Change scope/folder UI

Edit:

1. app.py lines 868-1010 for route/scope truth.
2. graph/store.py lines 243-258 for scoped views.
3. frontend/src/api.js for scope-aware requests.
4. frontend/src/hooks/useGraphData.js for library refresh.
5. frontend/src/components/DocSidebar.jsx lines 271-339 for path/tree rules.
6. frontend/src/components/layout/RightDocumentRail.jsx lines 31-229
   for the adjustable source rail.
7. tests/test_growi_routing.py and frontend build/lint checks.

### Change runtime settings

Edit:

1. graph/core.py typed field and from_env mapping.
2. app.py settings endpoints/redaction.
3. frontend/src/components/SettingsView.jsx form and patch builder.
4. relevant writer/researcher consumer.
5. the focused unit test.
6. RUNNING.md if the setting is operator-facing.

### Change agent tools or answer grounding

Edit:

1. graph/researcher.py tool schemas and tool implementations.
2. graph/researcher.py ResearchSession orchestration.
3. app.py ask/SSE endpoint only if the wire format changes.
4. mcp_server.py if the tool is exposed over MCP.
5. frontend/src/hooks/useAskStream.js and ChatPanel.jsx if events/display
   change.
6. realtime/ask tests.

Keep explicit citation IDs and source-node normalization intact.

## 13. What not to do

- Do not treat graph.sqlite as the editable wiki database.
- Do not write directly to graph.sqlite while the server is running.
- Do not use all as a writable team; it is an aggregate read scope.
- Do not edit generated data/wiki files as the normal source workflow.
- Do not bypass raw Git commits when changing parser output.
- Do not delete GROWI when rebuilding graph.sqlite.
- Do not delete engine.sqlite unless intentionally removing the encrypted
  connection registry and page ledger.
- Do not enable Qdrant/LanceDB for the current architecture.
- Do not add one LLM call per wiki page for parent summaries; context.py owns
  one call per parent.
- Do not let Python invent Excel structure after a valid LLM sheet decision;
  tabular.py validates the decision and falls back only after two failures.
- Do not expose API keys/tokens to frontend settings responses or logs.
- Do not add an unbounded write endpoint outside the Librarian queue.
- Do not make tests depend on the current real data/ or live services.

## 14. Refreshing this map

From the application directory:

~~~bash
rg -n '^class |^def |^async def |^@app\\.' app.py graph parser/server.py
rg -n '^class |^def |^async def ' graph/growi.py graph/registry.py graph/store.py graph/writers.py graph/formats/*.py graph/wiki/*.py graph/pages.py graph/chunk.py graph/researcher.py
rg -n '^function |^const |^export |export default' frontend/src --glob '*.js' --glob '*.jsx'
nl -ba path/to/file.py | sed -n 'start,endp'
~~~

After line-range changes, run the focused tests first, then:

~~~bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
~~~

## 15. Pre-ingestion cross-document linker (`graph/wiki/linker.py`)

Runs inside `writers.write_wiki` (mode `wiki`) after `publish_output` and before
`write_source_stamp`, so `wiki_one.py` exercises it with no extra options.
Full design and invariants live in `docs/LINKER.md`; this is the implemented reality.

* One standalone SQLite catalog at `project.linker_database`
  (`metadata/wiki-linker.sqlite`, plus WAL/SHM and `wiki-linker.lock`).
  `LinkCatalog` is its only owner. It never opens `graph.sqlite`/`engine.sqlite`
  and never imports Librarian/Researcher/GROWI.
* Maps are reconstructed from writer artifacts only:
  `_planning/manifest.json` (+`metadata.json`, `coverage.json`) joined with
  `state/<doc>/state/plan.json` and `work/observations/live/*.json`; documents
  without observation state get deterministic `derived`/`coverage_only` maps.
* Candidate discovery: exhaustive per-document map scout (Lane A, required,
  hash-cached in `map_comparisons`; a failed call is retried, never cached as
  empty), FTS5+sqlite-vec RRF fusion, bridge-probe questions, optional rerank,
  and one/two-hop traversal of `page_edges`. Retrieval only prioritizes; a
  map-scout candidate is never dropped for a low score.
* Research sends fully stripped page bodies; proposals must quote exact
  evidence from both endpoints and pass the mechanical checks in
  `validate_proposal` before the endpoint-only judge. Relation types are the
  closed `LinkRelationType` literal; generic `related/similar` is impossible.
* Edits are additive and marker-owned:
  `<!-- llm-wiki-link:<pair>:start|end -->` blockquotes plus a
  `<!-- llm-wiki-related:start|end -->` footer. Outside the markers files are
  byte-for-byte unchanged; `strip_managed_links` is the exact inverse of the
  renderer and raises on malformed markers.
* Commit is bilateral: `link_runs` moves `researching → committing → complete`;
  a crash mid-write is finished by `recover_pending_runs` on the next
  invocation, and a caught error restores in-memory originals. The source
  stamp is written only after linker success, and `writers.up_to_date` refuses
  a `pending`/`failed` `wiki/<doc>/_planning/linker.json` marker.
* `remove_document(project, raw_rel)` exists for raw-file deletion but is not
  wired into `graph/sync.py` yet.

Tests: `tests/test_wiki_linker.py` (maps, lanes, validation, renderer,
commit/recovery, writer integration, and the deterministic A/B/C/D smoke
corpus proving a retrieval-missed link is still found).
