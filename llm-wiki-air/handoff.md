# llm-wiki-air handoff

Updated: 2026-09-15 (Asia/Tokyo)

## Operator preference

Do not run build, link, publish, reset, sync, or watch commands on the user's
behalf. Inspect and edit code as needed, then give the exact command for the
user to run. Read-only diagnostics and local tests are fine.

## What this project does

`llm-wiki-air` is the minimal no-Git pipeline:

```text
mount/ -> document parser -> raw Markdown -> generated wiki -> cross-document links -> GROWI
                                                          <- managed page edits <-
```

It also supports a manual/test flow where Markdown is placed directly in
`data/raw`, each expensive phase is run separately, output is inspected or
backed up, and publication happens only when explicitly requested.

## Current runtime snapshot

The working tree was clean when this handoff was written.

Three documents under `data/wiki/rikiseisan/test` currently have linker status
`complete` in `legacy` mode:

- error-management manual: last run added 144 edges
- system-operation-information manual: last run added 227 edges
- configuration-control manual: last run added 109 edges

`data/metadata/pipeline.json` records all three as published at approximately
2026-09-15 04:44 UTC. This is local ledger state; confirm the actual pages in
GROWI when external state matters.

`.env` is ignored by Git. It currently points GROWI at
`http://10.160.152.38:3000`, uses attach mode, and publishes from the GROWI
root so local paths are mirrored as `/rikiseisan/test/...`. Never put a token
in this handoff; the operator supplies `GROWI_TOKEN` directly in `.env`.

## Main commands

Run these from `llm-wiki-air/`. Put options before the phase when paths follow
the phase.

```bash
# Health checks only
python main.py check --data-root ./data

# Manual phased workflow
python main.py -v build --data-root ./data wiki [raw-relative-path ...]
python main.py -v build --data-root ./data link [raw-relative-path ...]
python main.py -v build --data-root ./data all [raw-relative-path ...]

# Equivalent pending-link command
python main.py -v link --data-root ./data

# Re-curate/relink completed documents
python main.py -v link --data-root ./data --force

# Publish only completed local wiki output
python main.py publish --data-root ./data

# Automated pipeline
python main.py -v sync --data-root ./data
python main.py -v watch --data-root ./data --interval 60

# Delete only publisher-marked GROWI pages and clear the local publish ledger
python main.py reset --data-root ./data
```

`build` with no phase means `build all`. `wiki` remains an alias for bare
`build`. `--force` now performs a genuinely fresh wiki run instead of resuming
cached page rewrites.

## Repository map

### Entry point and orchestration

- `main.py`
  - CLI parser and command dispatch.
  - Loads `Settings`, applies CLI overrides, prints progress JSON.
  - Commands: `check`, `build`, `link`, `publish`, `sync`, `watch`, `reset`.
- `publisher/pipeline.py`
  - Product-level orchestration and the process lock.
  - `build_wiki_only`: raw -> pristine wiki, linker marker becomes pending.
  - `link_raw`: links pending wiki documents without regenerating them.
  - `build_raw`: generates the entire selected wiki batch first, then links.
  - `sync_once`: scans mount, parses all changed sources, generates all wikis,
    links the pending batch, then publishes once.
  - `publish_only`: GROWI sweep without touching mount/raw/wiki generation.
  - `reset_growi`: removes publisher-marked GROWI pages and clears publish state.
- `publisher/scanner.py`
  - Detects added, changed, unchanged, and deleted files below `mount/`.
- `publisher/ledger.py`
  - Atomic persistence for source and GROWI publication state.

### Workspace and conversion

- `graph/workspace/project.py`
  - Defines all data-root paths and raw-name/wiki-folder mappings.
  - `Project.wiki_dir(raw_rel)` is the canonical raw -> wiki folder mapping.
- `graph/workspace/writer.py`
  - Shared boundary between wiki generation and linking.
  - `write_wiki_pages` publishes pristine generated pages locally only.
  - `run_linkers` invokes the batch linker.
  - `wiki_up_to_date` and `links_up_to_date` keep phase state independent.
  - Preserves linker artifacts when regenerated pages replace a wiki folder.
- `graph/workspace/parser_client.py`
  - Calls the external document parser for non-Markdown mount files.
- `graph/workspace/convert.py`
  - Older standalone mount conversion helper; the active automated path is
    `publisher.pipeline.sync_once`.

### Wiki generation

- `graph/wiki/pipeline.py`
  - Main wiki pipeline: source observation, semantic planning, parent context,
    cross-page research, section rewrite/judging, manifest and review output.
- `graph/wiki/model.py`
  - `ChatModelPort`, the common structured/text LLM seam.
- `graph/clients/chat.py`
  - OpenAI-compatible client plus structured-output fallback handling.
- `graph/wiki/document_map.py`
  - Observation and page-plan generation/validation.
- `graph/wiki/windows.py`
  - Source windows and observation calls.
- `graph/wiki/page.py`
  - Deterministic Markdown validation, preservation gates, and inline linking.
- `graph/wiki/prompts.py`, `graph/wiki/wire.py`, `graph/wiki/schemas.py`
  - Prompt definitions and Pydantic contracts.
- `graph/wiki/export.py`
  - Converts the run directory to the local ingest/wiki layout.
- `graph/wiki/storage.py`
  - Atomic JSON/text writes and stable hashing.
- `graph/wiki/images.py`, `markdown_blocks.py`, `ids.py`, `incremental.py`
  - Image placeholders, Markdown block boundaries, stable IDs, incremental helpers.
- `graph/wiki/legacy.py`
  - Legacy chunk-mode wiki writer used by `--mode chunks`.

### Input formats

- `graph/formats/__init__.py`
  - Detects source kind and routes format behavior.
- `pdf.py`, `docx.py`, `pptx.py`
  - Structural seeding rules for parsed document Markdown.
- `csv.py`, `xlsx.py`, `tabular.py`
  - Table-specific wiki generation.
- `tree.py`, `context.py`
  - Structural tree and parent/sibling context generation.

### Cross-document linker

- `graph/linker/service.py`
  - End-to-end linker orchestration.
  - `link_document` prepares chunks/metadata and discovers edges for one document.
  - `link_documents` links a whole batch, then curates/renders affected pages once.
  - Batch markers use `render_pending` until final page rendering succeeds.
  - `remove_document` deletes catalog rows and removes stale rendered references.
- `graph/linker/catalog.py`
  - SQLite catalog for documents, pages, chunks, entities, behaviours, vectors,
    edge decisions, and accepted edges.
- `graph/linker/chunks.py`
  - Splits pristine pages at H2 boundaries and extracts/reuses chunk metadata.
  - Entity and behaviour extraction has no item-count cap. Chunks are described in
    document order with a normalized rolling entity registry, so later chunks reuse
    known names instead of duplicating them.
  - An entity can declare earlier names in `replaces`; later evidence can therefore
    split a conflated entity such as `A-B` into `A` and `B` and revise earlier chunk
    metadata before links are rebuilt.
- `graph/linker/legacy.py`
  - FTS/vector RRF candidate discovery.
- `graph/linker/neo.py`
  - Entity define/use and behaviour-hop candidate discovery.
- `graph/linker/prompts.py`, `graph/linker/wire.py`
  - Edge-evaluation and page-reference-curation prompts/contracts.
- `graph/linker/render.py`
  - Deterministic rendering and final safety caps.
- `graph/linker/__main__.py`
  - Maintenance CLI for status, one-document relink, and catalog rebuild.

Link rendering is intentionally split by link type:

1. Neo entity define/use edges are deterministic and bypass page curation. A use
   points to its definition by wrapping existing entity text at 1-3 well-spaced
   occurrences; entity links have no page-wide count limit.
2. Behaviour edges use graph-hop discovery plus LLM acceptance. A page-level LLM
   sees current and new behaviour references together and decides what remains,
   what is replaced, and whether each link is inline or in `関連資料`.

The page curator is instructed to retain a few concretely useful references
even when none is critical, while rejecting pure similarity and internal graph
jargon. Its result is stored in `_planning/navigation.json`.

Hard safety limits are:

- at most 15 footer references per page
- at most 3 Neo behaviour links inline per page
- at most 8 legacy inline references per page, or 12 when the document has at
  least 150 combined lines across its pristine wiki pages

These are ceilings, not target counts or proportional quotas. Entity metadata is
extracted sequentially inside a document because each chunk receives the rolling
registry; edge calls and page curation still use configured concurrency.

Reader-facing pages never include `（参照元: ... 原文 N-N行）` annotations. Source
ranges remain in planning/coverage metadata, and old annotations are stripped both
when pages are linked and at the GROWI publication boundary.

### GROWI

- `graph/growi/client.py`
  - REST v3 client, path sanitization, marker merge, publication, deletion.
- `graph/growi/publisher.py`, `graph/growi/paths.py`
  - Compatibility re-exports.

Publication behavior:

- Only wiki folders whose linker marker is `complete` or `disabled` are published.
- Local `data/wiki` folder structure is appended to `GROWI_WRITE_PATH`.
- With the current `GROWI_WRITE_PATH=/`, paths become `/rikiseisan/test/...`.
- Local page suffix `.md` is stripped because GROWI rejects page paths ending
  in `.md` with `could_not_create_page`.
- Page body and managed link footer use separate chunk markers.
- Publishing resolves or creates every eligible page first, then converts local
  `.md` links to stable GROWI `/{pageId}` permalinks in the outgoing body only.
  Local Markdown therefore remains usable in VS Code and Obsidian.
- `pipeline.json` keeps a wiki-relative path -> GROWI page/revision ID map. Every
  sweep validates the map, so a remotely deleted page is recreated and all links
  receive its new ID even when local content did not change.
- Updating a page replaces only publisher-marked sections and preserves human
  text outside those markers.
- A newer GROWI revision is pulled back into the top-level wiki page and its
  pristine planning page when that local document is unchanged. Simultaneous
  local and remote edits are reported as conflicts and neither side is overwritten.
  Pulled pages mark linking pending so the next `sync` reindexes and re-renders them.
- Only publisher-marked content is pulled. Unmarked GROWI text remains preserved
  remotely but is not copied into generated local source pages.
- Stale pages are deleted only when they carry this publisher's markers.
- `reset` scans the configured write path and trashes publisher-marked pages.
  Since the current write path is `/`, its scan scope is the entire GROWI tree,
  though it still deletes only marked pages.

Required environment keys:

```text
GROWI_URL
GROWI_TOKEN
GROWI_WRITE_PATH
GROWI_ROOT_PATH
GROWI_MODE
GROWI_TIMEOUT
```

The current mode is `attach`; write-boundary validation happens before every
create/update. Bearer-token authentication is used for REST v3.

### Configuration and shared utilities

- `graph/config.py`
  - Environment-backed settings for models, parser, ingest, linker, and timeouts.
- `graph/clients/embeddings.py`
  - OpenAI-compatible embedding client.
- `graph/common/async_tools.py`
  - Async-to-sync bridge used by CLI orchestration.
- `graph/common/hashing.py`, `markdown.py`, `prompts.py`
  - Stable hashes, managed footer markers, shared prompt text.
- `.env.example`
  - Safe configuration template. `.env` is local and ignored.
- `README.md`, `../commands.md`
  - Operator-facing quick start and broader command sheet.

## Data-root layout

```text
data/
  mount/                         watched source files
  raw/                           parser output or manually supplied Markdown
  wiki/                          final local wiki pages
    <team>/<path>/<document>/
      *.md                       reader-visible pages
      _planning/
        pages/*.md               pristine pages, never link-rendered
        source.json              raw path and source hash
        manifest.json            generation plan/result metadata
        coverage.json            source-line ownership
        chunks.json              chunk metadata cache
        links.json               durable accepted edges
        navigation.json          LLM-selected rendered references
        linker.json              pending/render_pending/complete/failed marker
  metadata/
    state/<document>/            resumable wiki prompts/results/work artifacts
    wiki-linker.sqlite           cross-document catalog and vectors
    pipeline.json                mount, document publication, and page-ID ledger (schema 2)
    pipeline.lock                single-writer lock
    work/                        temporary per-document staging
```

Always render linked output from `_planning/pages/*.md`; those are the pristine
source of truth. Do not use already-linked top-level pages as renderer input.
The ledger loader accepts schema 1 and writes schema 2 on the next successful
save, so existing runtime state migrates without a separate command.

## Important fixes already made

- Multiple selected documents generate all wiki pages before the link phase.
- Batch linking renders affected pages once after all batch edges are available.
- Link filtering no longer renders `similar-to`/internal relationship jargon.
- Page-level LLM curation compares current and new links and writes natural
  Japanese reader-facing summaries.
- Deleted documents cascade out of the catalog and stale rendered links disappear.
- `chat_template_kwargs` is sent through OpenAI `extra_body`; passing it as a
  top-level argument caused rewrite calls to fall back to verbatim source.
- `--force` sets wiki resume off, so cached failed/verbatim sections are rebuilt.
- GROWI path generation strips `.md`; GROWI otherwise returns HTTP 400
  `could_not_create_page`. This fix exists in both `llm-wiki-air` and
  `llm-wiki-dist`.

## Validation without external writes

```bash
python -m compileall -q .
git diff --check
python main.py -h
python main.py build --help
```

The closest existing automated GROWI tests live in `../llm-wiki-dist/tests`:

```bash
cd ../llm-wiki-dist
.venv/bin/python -m unittest tests.test_growi_client tests.test_growi_publish
```

`llm-wiki-air` currently has no dedicated test suite. Prefer focused temporary
assertions or the corresponding `llm-wiki-dist` tests; do not create test files
unless the change warrants a permanent regression test.

## Caveats for the next agent

- Do not expose `.env` values or the GROWI token in logs, commits, or handoffs.
- Do not publish/reset merely to test code. Use mock transport tests first and
  give the operator the final external command.
- A browser-visible GROWI UI proves reachability, not write authorization.
- `publish` does not run models or linking. It reconciles every completed page so
  deleted pages and changed IDs are detected, while byte-identical remote bodies
  avoid unnecessary update requests.
- Bare `link` processes every pending wiki. Use `build ... link <raw paths>` to
  restrict linking to selected documents.
- `--linker legacy` and `--linker neo` share one catalog mode. Switching modes
  requires `link rebuild --mode ...`.
- `graph/` began as a copy of selected `llm-wiki-dist/graph` modules, but air now
  contains pipeline/linker-specific changes. Do not blindly overwrite either
  tree from the other; compare the exact files first. Keep truly shared fixes,
  such as GROWI path legality, synchronized deliberately.
- Preserve the user's runtime `data/` and unrelated worktree changes. Never
  delete state or rerun expensive phases without explicit instruction.
