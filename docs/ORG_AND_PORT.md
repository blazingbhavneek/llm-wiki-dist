# Backend organization and minimal GROWI publisher port

Status: implementation plan only. This document does not authorize mixing the
reorganization, linker correctness fixes, and downstream port into one change.

The current repository remains the source of truth until every acceptance gate in this
plan passes. The downstream target is a new sibling folder under
`/home/seigyo/c_repo/bhavneek/llm-wiki-air/`; its working name in this plan is
`wiki-publisher/`.

The user referred to `PLAN_GROW.md`; the file currently present in this repository is
`docs/PLAN_GROWI.md`.

---

## 1. Plain-English overview

There are really two different products mixed together in the current `graph/` package:

1. a **wiki factory**, which takes source files, converts them to raw Markdown, writes
   high-quality wiki pages, adds cross-document links, and publishes those pages; and
2. a **knowledge engine**, which ingests wiki pages into SQLite, creates embeddings and a
   graph, and serves Librarian/Researcher queries.

Both are useful, but they do not need to live in one pile of modules.

The work should therefore happen in two deliberately separate stages:

1. **Organize the existing repository without changing behavior.** Move files into folders
   that say what they own: workspace files, wiki generation, format-specific planning,
   GROWI access, and the local knowledge engine. Preserve the existing wiki code and its
   prompts exactly. Run the same tests before and after every move.
2. **Copy only the wiki-factory side into `llm-wiki-air/wiki-publisher`.** This smaller
   service watches `mount/`, calls the separate `doc-parser` HTTP service, writes `raw/`,
   runs the complete existing wiki pipeline and linker, and pushes the generated Markdown
   directly to GROWI. It does not contain Librarian, Researcher, GraphStore, graph
   ingestion, graph.sqlite, query APIs, MCP, or the existing raw Git history machinery.

“Minimal” applies to the orchestration around the wiki factory. It does **not** mean a
smaller or weaker wiki generator. The downstream copy must retain the complete format
planning, observation, seed planning, rewrite, validation, image handling, incremental
state, and cross-document linker logic.

The new pipeline is intentionally simple:

```text
mounted source files
        |
        | scan for add/change/delete
        v
separate doc-parser HTTP service
        |
        v
raw Markdown
        |
        v
existing format-aware wiki generator
        |
        v
existing cross-document linker
        |                    \
        |                     \ may update older wiki pages too
        v                      v
local generated wiki tree -- hash reconciliation --> GROWI
```

There is one subtle but essential point: after generating document X, the linker may edit
documents A, B, or C to add reciprocal links. Therefore “publish X” is insufficient. The
publisher must compare the whole local generated-wiki tree with its small publication
ledger and publish every document whose generated Markdown changed. This preserves
bilateral links in GROWI without adding graph ingestion or Git-diff machinery.

### 1.1 What remains in the current repository

The current `llm-wiki-dist` application keeps both products:

- the complete wiki factory;
- GROWI publishing and reverse synchronization;
- GraphStore/sqlite-vec ingestion;
- Librarian and Researcher;
- graph query APIs, frontend integration, and MCP;
- Git-backed raw change tracking, if the full product still wants it.

### 1.2 What the downstream repository contains

`llm-wiki-air/wiki-publisher` contains only:

- the data-folder contract;
- a mount scanner and small local ledger;
- the doc-parser HTTP client;
- the complete wiki generator and format logic;
- the complete pre-ingestion linker and its standalone SQLite catalog;
- the GROWI client/publisher;
- a `sync-once` command and a simple polling runner;
- focused tests and operational documentation.

It does not contain the local query engine.

### 1.3 Why the folder should be named `workspace`, not `files`

Use `workspace/` for the package that owns `data/mount`, `data/raw`, `data/metadata`, and
`data/wiki`.

`files/` is too broad: almost every module handles files. `workspace/` communicates that
the package owns the complete on-disk workspace contract, source discovery, conversion
state, and synchronization orchestration. It must not own wiki-writing algorithms or
GROWI HTTP details.

### 1.4 The safety rule

Never combine a file move with an algorithm change. A reviewer must be able to look at a
commit and say either:

- “this commit only moved code and changed imports”, or
- “this commit changed behavior and contains focused tests for that behavior.”

If those statements cannot be made separately, the change is too large.

---

## 2. Goals

The plan has the following concrete outcomes.

1. A reader can identify a module's owner from its directory.
2. The existing application behaves identically after organization.
3. The wiki pipeline and linker remain fully functional through `wiki_one.py`.
4. The full local knowledge engine remains functional through the existing application.
5. The downstream service can be deployed without graph.sqlite or engine.sqlite.
6. The downstream service detects new, changed, and deleted mounted documents without Git.
7. Changed sources are regenerated locally before destructive remote replacement.
8. Reciprocal linker edits to older documents are also published to GROWI.
9. A failed parser, model, linker, or GROWI call is retried safely and is never mistaken for
   success or “no changes”.
10. Shared wiki behavior can be synchronized from the current repository to the downstream
    repository without hand-copying individual functions.

---

## 3. Non-goals

Do not add any of the following to the first organization/port effort:

- a new graph database;
- graph ingestion in the downstream service;
- Librarian, Researcher, realtime query, graph search, claims, clustering, or notes;
- GROWI-to-local reverse synchronization in the downstream service;
- MCP or the existing frontend;
- raw Git repositories, Git commits, Git hunks, or rename detection downstream;
- Kubernetes operators, Celery, Redis, Kafka, or a distributed job queue;
- watchdog/inotify as a required dependency;
- a second wiki implementation;
- simplified prompts, reduced validation, reduced page context, or a lighter linker;
- a plugin system for one parser or one GROWI target;
- automatic conflict merging with hand-edited GROWI pages;
- a generalized workflow engine;
- a database merely to replace a small atomic JSON ledger.

Rename detection is deliberately not required. A rename is processed as deletion of the
old path plus addition of the new path.

---

## 4. Verified current state

### 4.1 Current source-direction flow

The implemented source flow is:

```text
graph.convert.convert_mount
  -> doc-parser POST /parse
  -> raw Markdown
  -> graph.sync.sync_raw (Git change plan)
  -> graph.writers.write_wiki
  -> graph.formats + graph.wiki
  -> graph.wiki.linker
  -> graph.growi.GrowiPublisher
```

`write_wiki(mode="wiki")` publishes generated pages locally, runs the linker, then writes
the source completion stamp. This ordering must remain unchanged.

### 4.2 Current knowledge-engine flow

The implemented knowledge flow is:

```text
GROWI pages
  -> graph.growi.sync_growi_pages
  -> graph.librarian
  -> graph.store.GraphStore
  -> graph.sqlite / FTS5 / sqlite-vec
  -> graph.researcher / graph.realtime
```

This flow remains in `llm-wiki-dist`; it is omitted from `wiki-publisher`.

### 4.3 Current doc-parser contract

The sibling parser is an independent FastAPI application at
`llm-wiki-air/doc-parser`.

The relevant API is:

```text
GET  /health
POST /parse?images=true&describe_images=true
```

`POST /parse` accepts multipart field `file` and optional headers:

- `X-LLM-Base-URL`
- `X-LLM-API-Key`
- `X-LLM-Model`

Successful JSON contains at least:

- `markdown`
- `parser`
- `image_count`
- `duration_s`

The parser currently supports DOCX, PDF, PPTX, XLSX, and CSV through its registry. Plain
Markdown should bypass the parser and be copied as normalized raw Markdown.

Some parser implementations stream leading JSON whitespace as a heartbeat. The client
must read the complete JSON response and use a read timeout suitable for long GPU jobs.

### 4.4 Current high-risk coupling

The following coupling explains why a blind directory move would fail:

- `graph/wiki/model.py` imports model helpers from the 2,800-line `graph/chunk.py`.
- `graph/wiki/markdown_blocks.py` and `graph/formats/tree.py` import Markdown fence helpers
  from `graph/chunk.py`.
- `graph/wiki/ids.py` imports `short_hash` from `graph/core.py`.
- `graph/wiki/linker.py` imports `Project` and creates Embedder/Reranker clients through
  root modules.
- `graph/writers.py` selects among wiki, page, and legacy chunk generation.
- `graph/core.py` mixes Settings, knowledge models, prompts, and shared text helpers.
- `graph/gateway.py` mixes generic model clients with knowledge-engine composition.
- `graph/growi.py` mixes client, path mapping, publication, and reverse synchronization.

These seams must be untangled with characterization tests before the downstream copy.

---

## 5. Hard constraints

### C1 — Preserve wiki quality

Do not alter prompts, prompt versions, Pydantic wire schemas, retry counts, concurrency
semantics, source ownership rules, section validation, image restoration, page naming,
coverage metadata, or fallback behavior merely to make imports prettier.

### C2 — Preserve linker behavior

The complete linker described by `docs/LINKER.md` remains part of wiki generation. It is
not part of the local knowledge engine and must be present downstream.

The porting baseline is the current linker behavior: at most 12 research candidates per
page, one proposal per candidate call, a shared request-level research semaphore across
pages, full target/candidate reading during research, and compact verified-evidence/local
context during judging. Preserve prompt versions `wiki-link-research-2` and
`wiki-link-judge-2`; do not restore redundant intermediate-hop page bodies or full-page
judge inputs during extraction.

### C3 — Separate correctness fixes from moves

Any linker defect discovered during this work receives its own test and behavior commit
before package moves begin. Move commits do not contain fixes.

### C4 — One canonical implementation during organization

Do not duplicate a helper inside the current repository. Extract it once, update callers,
and leave a temporary re-export only when an existing public import needs compatibility.

### C5 — Minimal downstream control plane

The downstream service has one global process lock and one sync loop. Do not add a queue or
worker framework until measured throughput requires it.

### C6 — Local Markdown remains authoritative

The downstream service treats `data/wiki` as the generated product and GROWI as the remote
publication target. GROWI edits are not pulled back.

### C7 — Safe ownership on deletion

Only GROWI pages bearing this publisher's ownership marker and located under its configured
root may be deleted. Never recursively delete an arbitrary path or an unmarked human page.

### C8 — Team isolation

The first path component below `mount/`, `raw/`, and `wiki/` is the team. Parser output,
linker candidates, local paths, and GROWI paths may not escape that team.

### C9 — No success marker before completion

A source is complete only after parsing, local wiki generation, linker completion, and
remote publication reconciliation succeed. Intermediate state must remain retryable.

### C10 — Bilateral publication

If the linker changes two local pages, both corresponding GROWI documents must eventually
be republished. Publication is reconciled from content hashes, not inferred only from the
new source path.

### C11 — No hidden dependency on the full engine

The downstream package must not import `knowledge`, GraphStore, Librarian, Researcher,
graph.sqlite, engine.sqlite, or the GROWI connection registry.

### C12 — Linux process model

The existing linker uses `fcntl`; the first downstream deployment is therefore Linux-only.
Do not introduce a cross-platform lock abstraction until another platform is required.

---

## 6. Target organization in `llm-wiki-dist`

Use this directory structure:

```text
llm-wiki-dist/
  graph/
    __init__.py
    config.py

    common/
      __init__.py
      async_tools.py
      hashing.py
      markdown.py

    clients/
      __init__.py
      chat.py
      embeddings.py
      reranker.py

    workspace/
      __init__.py
      project.py
      parser_client.py
      convert.py
      git_sync.py

    formats/
      __init__.py
      context.py
      csv.py
      docx.py
      pdf.py
      pptx.py
      tabular.py
      tree.py
      xlsx.py

    wiki/
      __init__.py
      __main__.py
      config.py
      document_map.py
      export.py
      ids.py
      images.py
      incremental.py
      linker.py
      markdown_blocks.py
      model.py
      page.py
      pipeline.py
      prompts.py
      schemas.py
      storage.py
      windows.py
      writer.py

    growi/
      __init__.py
      client.py
      paths.py
      publisher.py
      reverse_sync.py
      registry.py

    knowledge/
      __init__.py
      core.py
      gateway.py
      librarian.py
      neighborhood.py
      realtime.py
      researcher.py
      store.py
      vectors.py
      vocab.py

    legacy/
      __init__.py
      chunk_writer.py
      page_writer.py

    cli.py
```

`app.py`, `mcp_server.py`, and `wiki_one.py` remain top-level executable adapters. They
must import organized packages and contain no domain algorithms.

### 6.1 Import direction

The allowed dependency direction is:

```text
common       config       clients
   ^            ^            ^
   |            |            |
formats <---- wiki -----> workspace
                  \           |
                   \          v
                    -------> growi

knowledge may use config, clients, formats, workspace, wiki artifacts, and growi.
wiki must never import knowledge.
workspace must never import knowledge.
growi must never import knowledge except reverse_sync callbacks supplied by callers.
```

Enforce this with an import-boundary test; do not rely only on review.

### 6.2 Package responsibilities

| Package | Owns | Must not own |
| --- | --- | --- |
| `common` | pure hashing, async bridge, Markdown fence primitives | settings, paths, HTTP, graph models |
| `config` | environment-backed settings | runtime clients or persistence |
| `clients` | chat, embedding, reranking endpoint adapters | wiki policy or graph policy |
| `workspace` | data-root layout, mount/raw conversion, full-product Git sync | page-writing algorithms or GROWI request code |
| `formats` | source-kind detection and format-specific structural planning | mount scanning, GROWI, graph ingestion |
| `wiki` | raw Markdown to generated wiki, linker, writer stamps/index | Librarian, Researcher, GraphStore |
| `growi` | GROWI HTTP, paths, owned publication, optional reverse sync | wiki generation or graph reasoning |
| `knowledge` | local graph storage, enrichment, search, Librarian/Researcher | source conversion or wiki generation internals |
| `legacy` | old chunk/page generation modes still needed by the full product | new wiki behavior |

### 6.3 Exact current-file disposition

| Current file | Final owner | Treatment |
| --- | --- | --- |
| `graph/project.py` | `graph/workspace/project.py` | move unchanged; temporary old-path re-export |
| `graph/convert.py` | `graph/workspace/convert.py` | move orchestration; parser HTTP function first extracted |
| `graph/sync.py` | `graph/workspace/git_sync.py` | move unchanged; name advertises Git dependency |
| `graph/writers.py` | `graph/wiki/writer.py` plus legacy dispatch facade | extract wiki-only path without behavior changes |
| `graph/formats/*` | `graph/formats/*` | already correctly placed; imports only |
| `graph/wiki/*` | `graph/wiki/*` | keep filenames and behavior |
| `graph/growi.py` | `graph/growi/{client,paths,publisher,reverse_sync}.py` | mechanical symbol extraction, one commit at a time |
| `graph/registry.py` | `graph/growi/registry.py` | move unchanged |
| `graph/librarian.py` | `graph/knowledge/librarian.py` | move unchanged |
| `graph/researcher.py` | `graph/knowledge/researcher.py` | move unchanged |
| `graph/realtime.py` | `graph/knowledge/realtime.py` | move unchanged |
| `graph/store.py` | `graph/knowledge/store.py` | move unchanged |
| `graph/vectors.py` | `graph/knowledge/vectors.py` | move unchanged |
| `graph/vocab.py` | `graph/knowledge/vocab.py` | move unchanged |
| `graph/neighborhood.py` | `graph/knowledge/neighborhood.py` | move unchanged |
| `graph/core.py` | `graph/config.py` + `graph/knowledge/core.py` | extract Settings first; move remaining knowledge content |
| `graph/gateway.py` | `graph/clients/*` + `graph/knowledge/gateway.py` | extract endpoint clients first; keep composition in knowledge |
| `graph/chunk.py` | `graph/legacy/chunk_writer.py` | first extract shared Markdown/async/chat helpers |
| `graph/pages.py` | `graph/legacy/page_writer.py` | move unchanged after imports point to legacy chunk writer |
| `graph/cli.py` | `graph/cli.py` | keep; update imports to knowledge package |

### 6.4 Compatibility policy

Existing top-level imports such as `from graph.project import Project` may be retained for
one transition release through tiny re-export modules. A compatibility module may contain
only imports and `__all__`; no logic.

Package conversions such as `graph.growi.py` to `graph/growi/` preserve
`from graph.growi import GrowiClient` through `graph/growi/__init__.py`.

Tests must primarily use the new paths. Compatibility tests cover only explicitly supported
old imports. Do not keep duplicate implementations.

---

## 7. Organization method: behavior-preserving moves

### 7.1 Commit discipline

Use this commit sequence. Do not squash until review is complete.

1. Baseline/characterization tests.
2. Linker correctness fixes, if needed, with no moves.
3. Shared helper extraction, one seam per commit.
4. Workspace moves and import edits.
5. GROWI moves and import edits.
6. Knowledge moves and import edits.
7. Wiki writer ownership and legacy isolation.
8. Entry-point/test/documentation imports.
9. Removal of temporary compatibility paths, only if approved.

### 7.2 Rules for a move commit

For every move commit:

1. Use `git mv`.
2. Change imports required by that move.
3. Do not run a whole-tree formatter.
4. Do not rename functions, arguments, classes, environment variables, progress events, or
   persisted fields.
5. Do not change exception types or retry behavior.
6. Do not alter prompt text, even for spelling.
7. Run focused tests and import smoke checks.
8. Inspect `git diff --find-renames` and confirm the moved module is recognized as a rename.
9. Search the old import path with `rg`.
10. Commit before starting the next domain.

### 7.3 Characterization before extraction

Before moving a helper out of a large module, add a test against the current behavior.
After extraction, run the same test unchanged.

Required characterization targets:

- Markdown fence parsing and unclosed-fence behavior;
- table-line detection;
- synchronous execution of async wiki calls;
- structured model fallback and timeout behavior;
- `short_hash` output;
- Embedder and Reranker public methods with mocked HTTP;
- Settings environment names and defaults;
- GROWI page path generation;
- marker wrapping/merging;
- publisher delete ownership checks;
- parser request fields, headers, and error mapping.

---

## 8. Linker correctness gate before organization

The linker test suite currently exercises many happy paths, but the organization must not
freeze known-risk behavior as a trusted baseline. Add focused failing tests for the cases
below, then fix them in a separate commit before any moves.

### 8.1 Republished unchanged pages must regain managed links

`publish_output` replaces the current document directory. The linker catalog hashes bodies
after stripping managed blocks. Consequently, a regenerated page with identical base text
may classify as unchanged even though publication just removed all its managed blocks.

Required test:

1. create two linked documents;
2. republish one document from clean staged output with identical base text;
3. rerun the linker;
4. assert that both inline blocks and both footer entries exist exactly once.

Required behavior: active catalog relations touching the republished document are included
in desired-state rendering even when no page requires new research.

### 8.2 Removing a generated page must not require writing that missing page

When a page disappears from a regenerated document, stale-link cleanup must remove the
peer's backlink even though the deleted endpoint file no longer exists.

Required test:

1. create a bilateral relation;
2. regenerate one document without one linked page;
3. run reconciliation;
4. assert the surviving page loses its managed block/footer entry;
5. assert the absent page is not opened for writing;
6. assert the catalog relation is removed.

### 8.3 Hop paths must retain every actual hop

For a target `A`, retrieved seed `B`, and edge `B -> C`, the discovery path must be
`[A, B, C]`, not `[A, C]`. For `C -> D`, it must be `[A, B, C, D]` within the configured
bound.

Required tests must prove:

- the original seed is retained;
- every non-seed step corresponds to a real catalog edge;
- a model cannot validate an invented direct step merely because its ID appeared elsewhere
  in the supplied path;
- a final hop endpoint receives its own body, map entries, and anchor allowlist during
  proposal validation.

### 8.4 Updating an existing pair must roll back to the previous row

If an active pair is re-earned with new prose and a file write fails, rollback must restore
the previous database row as well as the previous Markdown. It must not delete the old
active link merely because the replacement row temporarily had status `pending`.

Required test: inject failure while updating an existing pair and compare the complete
pre/post row and both endpoint files byte-for-byte.

### 8.5 Commit and verification must use the pre-write base

The bilateral verification input must be the base captured before rendering, not a newly
stripped copy read after the write. Add a failure-injection test that changes non-managed
text between staging and verification and assert activation is refused.

### 8.6 Reranker behavior must match its documentation

Either make the reranker actually remove non-map candidates below its retained set, or
document that it only annotates/reorders. A `rerank_dropped` flag that never drops anything
is not acceptable. Map-scout candidates must remain protected.

### 8.7 Gate

Do not begin module moves until all focused linker tests and the deterministic smoke corpus
pass. This gate protects both the full and downstream pipelines.

---

## 9. Work packages for organizing the current repository

### WP-O0 — Record the baseline

**Files changed:** tests and a baseline note only.

**Steps:**

1. Record `git status`, current commit, Python version, and dependency lock hash.
2. Run linker tests separately.
3. Run wiki chunking, page, export, incremental, format, writer, project, conversion,
   GROWI, sync, GraphStore, Librarian, and Researcher tests in named groups.
4. Do not use a live LLM for the automated baseline.
5. Record unrelated pre-existing collection/import failures separately.
6. Save a deterministic fixture's generated `wiki/`, `_planning/`, and relevant state-file
   hashes for later parity comparison.

**Acceptance:** the baseline is reproducible and failures are classified before edits.

### WP-O1 — Pass the linker correctness gate

**Files changed:** `graph/wiki/linker.py`, its direct writer hook only if necessary, and
`tests/test_wiki_linker.py`.

Implement section 8 as independent fixes. Do not move files in this work package.

**Acceptance:** all new regressions fail on the current implementation and pass after the
fixes; the current 51 linker tests remain green.

### WP-O2 — Extract stable shared primitives

**Goal:** let wiki/format code stop importing the legacy chunk writer or knowledge core.

**Steps:**

1. Create `graph/common/markdown.py` and move, without semantic edits:
   - `MarkdownFenceInfo`;
   - `MarkdownFenceScan`;
   - `parse_markdown_fence_marker`;
   - `scan_markdown_fences`;
   - `is_tableish_line` and only its direct constants/helpers.
2. Update `wiki/markdown_blocks.py`, `formats/tree.py`, and legacy callers.
3. Keep re-exports in the old chunk module during transition.
4. Create `graph/common/hashing.py` and move `short_hash`; re-export it from the old core
   path until imports are migrated.
5. Create `graph/common/async_tools.py` for `_run_async_blocking`; preserve its running-loop
   behavior exactly.
6. Move `make_llm`, structured invocation, and only their direct JSON/message helpers into
   `graph/clients/chat.py`.
7. Update `wiki/model.py` to use `clients.chat`.
8. Do not modify prompt bodies or model-call counts.

**Acceptance:** `graph/wiki` and `graph/formats` contain no import from `graph.chunk` or
`graph.core`; output parity hashes remain unchanged.

### WP-O3 — Separate Settings and endpoint clients

**Goal:** downstream wiki code can configure itself without importing knowledge schemas.

**Steps:**

1. Move `Settings` and its unchanged `from_env` implementation to `graph/config.py`.
2. Re-export `Settings` from `graph.core` temporarily.
3. Split Embedder and Reranker into `graph/clients/embeddings.py` and
   `graph/clients/reranker.py` with their existing public APIs.
4. Keep the knowledge-only `LlmClient` and `ModelGateway` composition for later placement
   under `knowledge/gateway.py`.
5. Update the linker writer hook to import endpoint clients from `clients`.
6. Verify all existing environment variable names and defaults byte-for-byte.
7. Do not add a new configuration framework.

**Acceptance:** importing Settings or wiki writer code does not import Librarian,
Researcher, GraphStore, Torch, sentence-transformers, or the FastAPI app.

### WP-O4 — Move the workspace package

**Steps:**

1. `git mv graph/project.py graph/workspace/project.py`.
2. Extract `parse_document` into `workspace/parser_client.py`; keep the same request
   contract and exception mapping.
3. `git mv graph/convert.py graph/workspace/convert.py`, importing the parser client.
4. `git mv graph/sync.py graph/workspace/git_sync.py`.
5. Update internal imports in app, Librarian, wiki linker, writer, tests, and entry points.
6. Add temporary `graph/project.py`, `graph/convert.py`, and `graph/sync.py` re-exports if
   compatibility is required.
7. Add an import-boundary test asserting workspace does not import knowledge modules.

**Acceptance:** full-product Git sync behavior, paths, stamps, changed hunks, and conversion
ledger are unchanged.

### WP-O5 — Move and split GROWI by responsibility

Perform one extraction per commit:

1. `growi/client.py`: `GrowiAPIError`, `GrowiPage`, `GrowiClient`.
2. `growi/paths.py`: segment/path validation, source-range and ownership-marker helpers.
3. `growi/publisher.py`: marked merge, `publish_pages`, coverage lookup,
   `GrowiPublisher`.
4. `growi/reverse_sync.py`: GROWI listing/revision diff and callback-driven reverse sync.
5. `growi/registry.py`: move the current registry unchanged.
6. `growi/__init__.py`: re-export the currently public names needed by existing callers.

Do not change request URLs, JSON payloads, conflict retry, attach/own boundaries, marker
syntax, or deletion ownership during these moves.

**Acceptance:** all GROWI client/publish/sync/registry tests pass with no live server.

### WP-O6 — Move the knowledge engine

Move the following modules with import-only edits:

- core remainder to `knowledge/core.py`;
- gateway composition to `knowledge/gateway.py`;
- librarian, researcher, realtime, store, vectors, vocabulary, and neighborhood to the
  corresponding `knowledge/` modules.

Rules:

1. Do not split Librarian or Researcher in this work package.
2. Do not change graph schema, SQL, prompt strings, tool names, or job payloads.
3. Update app/CLI imports only after all knowledge modules move.
4. Preserve public compatibility imports if external callers require them.
5. Add a boundary test asserting wiki does not import knowledge.

**Acceptance:** graph bootstrap, ingestion, revision, search, realtime, scope, and vector
tests match the baseline.

### WP-O7 — Isolate legacy writers and make wiki ownership explicit

**Steps:**

1. Move `chunk.py` to `legacy/chunk_writer.py` after all shared callers have left it.
2. Move `pages.py` to `legacy/page_writer.py`.
3. Create `wiki/writer.py` containing the existing wiki-only configuration, run, export,
   publish-local, linker, source-stamp, up-to-date, and index behavior.
4. Keep full-product legacy mode selection in a small compatibility dispatcher if it is
   still used by Librarian.
5. Point `wiki_one.py` directly at `wiki.writer`.
6. Do not alter the order:

   ```text
   generate staged output
   -> publish local wiki folder
   -> run cross-document linker
   -> write source stamp
   -> update index
   ```

**Acceptance:** `wiki_one.py` imports no legacy or knowledge module; all writer tests pass.

### WP-O8 — Update executable adapters and documentation

Update:

- `app.py` to import config/workspace/growi/knowledge packages;
- `wiki_one.py` to import config/workspace/wiki/formats only;
- `mcp_server.py` only where backend import paths changed;
- tests to patch the module where a symbol is looked up after the move;
- `docs/DEV.md` and `docs/RUNNING.md` file paths;
- all four existing plan documents with a short “paths moved” note, not a rewrite of their
  historical instructions.

Run `rg` for every old import path. Remaining hits must be compatibility shims or historical
documentation explicitly labeled as such.

### WP-O9 — Organization acceptance gate

The organization is complete only when:

1. all automated unit tests that passed at baseline still pass;
2. deterministic wiki output and planning/state hashes match the baseline;
3. deterministic linker output matches after normalizing timestamps/run IDs;
4. import-boundary tests pass;
5. `wiki_one.py` imports no knowledge module;
6. app startup still builds the full knowledge engine;
7. a mocked GROWI publication behaves identically;
8. a real two-document wiki run produces bilateral links;
9. `git diff --find-renames` shows moves rather than rewritten modules; and
10. no downstream folder has been created yet.

---

## 10. Downstream target structure

Create this only after WP-O9 passes:

```text
/home/seigyo/c_repo/bhavneek/llm-wiki-air/
  doc-parser/                       existing separate service
  wiki-publisher/
    README.md
    pyproject.toml
    uv.lock
    .env.example

    graph/                          retained namespace for byte-identical porting
      __init__.py
      config.py
      common/
      clients/
      formats/
      wiki/
      workspace/
        __init__.py
        project.py
        parser_client.py
      growi/
        __init__.py
        client.py
        paths.py
        publisher.py

    publisher/
      __init__.py
      cli.py
      ledger.py
      pipeline.py
      scanner.py

    tests/
      test_boundaries.py
      test_ledger.py
      test_parser_client.py
      test_pipeline.py
      test_publish.py
      test_sync_parity.py
      test_wiki_*.py                copied relevant upstream suites

    data/                           runtime, ignored by Git
      mount/
      raw/
      metadata/
      wiki/
```

Keep the `graph` Python namespace in the first port. Renaming it would touch every internal
import while providing no runtime value. A namespace rename can happen later as a standalone
mechanical change.

### 10.1 Exact port allowlist

Copy these organized modules without behavior edits:

- `graph/config.py` with only fields required by copied code plus identical defaults;
- `graph/common/`;
- `graph/clients/chat.py`;
- `graph/clients/embeddings.py`;
- `graph/clients/reranker.py`;
- all of `graph/formats/`;
- all of `graph/wiki/`;
- `graph/workspace/project.py`;
- `graph/workspace/parser_client.py`;
- `graph/growi/client.py`;
- `graph/growi/paths.py`;
- `graph/growi/publisher.py`.

Do not copy:

- `graph/knowledge/`;
- `graph/legacy/`;
- `graph/workspace/git_sync.py`;
- `graph/workspace/convert.py` if it still commits Git;
- `graph/growi/reverse_sync.py`;
- `graph/growi/registry.py`;
- `app.py`, `mcp_server.py`, the existing frontend, or graph query CLI.

### 10.2 Downstream settings

Keep only settings consumed by the allowlisted code and publisher:

- data root;
- doc-parser base URL and timeout;
- chat base URL, API key, model, temperature, and request timeout;
- wiki output language, section target, write attempts, and rewrite concurrency;
- structure and format planning settings;
- linker enabled/map concurrency/research concurrency;
- embedding endpoint/backend/model/dimension fields required by the existing Embedder;
- reranker endpoint/backend/model fields required by the existing Reranker;
- GROWI URL, token, root/write path, and request timeout;
- polling interval.

Do not copy agent, graph search, clustering, evidence, MCP, admin, database routing, or
GROWI reverse-sync settings.

Settings removal must be proved by `rg` over the copied package. Never delete a field merely
because it appears unused in one entry point.

`linker research concurrency` is the size of one shared request-level pool across all page
research jobs; it must not become a per-candidate or per-page semaphore during porting.

---

## 11. Downstream data contract

Use:

```text
data/
  mount/<team>/<path>/<original-file>
  raw/<team>/<path>/<name>_<ext>.md
  metadata/
    pipeline.json
    pipeline.lock
    wiki-linker.sqlite
    wiki-linker.sqlite-wal          runtime only, when present
    wiki-linker.sqlite-shm          runtime only, when present
    wiki-linker.lock
    state/<team>/<document>/...
    work/<team>/<document>/...
  wiki/<team>/<document-folder>/
    NNN-page.md
    _planning/
      metadata.json
      coverage.json
      manifest.json
      linker.json
      source.json
```

Do not create:

- `raw/.git`;
- `metadata/last_sha`;
- `graph.sqlite`;
- `engine.sqlite`.

### 11.1 Source identity

The stable input key is the normalized path relative to `mount/`. It must:

- use `/` separators;
- be non-empty;
- reject absolute paths, `..`, NUL, and symlink escape;
- contain a non-reserved first component used as team;
- preserve Unicode filenames.

The raw output name continues to use the existing rule:

```text
manual.docx -> manual_docx.md
slides.pptx -> slides_pptx.md
table.xlsx  -> table_xlsx.md
notes.md    -> notes_md.md or a documented direct-Markdown identity
```

Choose one Markdown rule once and test it. Prefer reusing existing `raw_name_for` exactly so
the wiki folder mapping and linker IDs remain stable.

### 11.2 Pipeline ledger

Use one atomically replaced JSON file at `metadata/pipeline.json`. A single-process lock
makes a database unnecessary initially.

Schema version 1:

```json
{
  "schema_version": 1,
  "sources": {
    "team/path/manual.docx": {
      "source_sha256": "...",
      "size": 123,
      "mtime_ns": 123456789,
      "raw_rel": "team/path/manual_docx.md",
      "wiki_rel": "team/path/manual.docx",
      "parser": "docx",
      "completed_at": "...",
      "last_error": ""
    }
  },
  "published_documents": {
    "team/path/manual.docx": {
      "content_sha256": "...",
      "growi_path": "/configured-root/team/path/manual.docx",
      "published_at": "..."
    }
  }
}
```

Rules:

1. Validate `schema_version`; unknown versions fail loudly.
2. Read missing ledger as an empty version-1 ledger.
3. Write to a sibling temporary file, flush, fsync when available, then `replace`.
4. Never write a source's new `source_sha256` until its local wiki generation and linker
   complete.
5. Never write a publication hash until the complete remote document publish succeeds.
6. Keep `last_error` short and non-secret.
7. Never store API tokens in the ledger.
8. On corrupt JSON, stop and preserve the corrupt file; do not silently reset it.

### 11.3 Hashes

Use SHA-256 from the standard library.

- `source_sha256`: bytes of the mounted source.
- `raw_sha256`: UTF-8 bytes written to raw Markdown, if useful for diagnosis.
- `document content_sha256`: canonical JSON of sorted `(relative page path, page bytes
  SHA-256)` for every publishable `.md` page in the document folder.

Exclude `_planning`, temporary files, and directory mtimes from the publication digest.
Include linker-managed Markdown because link changes must trigger publication.

For the first implementation, hash every mounted file on every scan. This is intentionally
simple and correct. If scan cost becomes measurable, use size/mtime as a prefilter while
retaining content hashes as the decision value.

---

## 12. Mount scanner without Git

### 12.1 Supported entries

Scan regular files below `mount/` recursively. Ignore:

- `.DS_Store`, `Thumbs.db`, `desktop.ini`;
- names beginning `~$`;
- hidden VCS directories;
- temporary upload suffixes documented by deployment;
- symlinks that resolve outside `mount/`.

Use the parser's supported extensions plus Markdown passthrough. Unsupported files are
reported and left unprocessed; they are not treated as deleted.

### 12.2 Change plan

Given current scan `C` and completed ledger sources `L`:

```text
added   = C paths not in L
changed = paths in both where current SHA-256 != completed source_sha256
deleted = L paths not in C
```

Sort each set by normalized path for deterministic behavior. A rename naturally appears in
`deleted` and `added`.

### 12.3 Concurrency

Start with one global `fcntl` lock and sequential document orchestration. The wiki pipeline
already has bounded internal concurrency. Concurrent top-level document runs would compete
for the linker catalog and make remote replacement harder to reason about.

If a second process cannot acquire the lock, it exits with a distinct “already running”
status rather than waiting forever.

---

## 13. Parser integration

### 13.1 Request

For non-Markdown supported files:

1. open the source in binary mode;
2. send multipart field `file` to `<PARSER_BASE_URL>/parse`;
3. set `images=true` and `describe_images=true`;
4. pass the configured chat headers used for image descriptions;
5. accept leading JSON whitespace/heartbeat data;
6. parse the complete response;
7. require non-empty string field `markdown`;
8. record parser name and diagnostic metadata.

### 13.2 Errors

- HTTP 400: permanent input error until source changes.
- HTTP 415: unsupported; record and retry only after source changes or parser deployment
  changes.
- HTTP 408/429/5xx, connect/read timeout: transient; retain previous raw/wiki/GROWI output
  and retry later.
- Invalid JSON or missing Markdown: failure; never replace raw.

Do not log API keys or the full response body.

### 13.3 Atomic raw replacement

Write parsed Markdown to a temporary sibling path, fsync, and replace the raw target only
after validation. A parser failure must leave the last good raw file intact.

For `.md`, read and atomically copy the source without calling doc-parser.

### 13.4 Source race

Hash the mounted bytes that are actually sent. After parsing, stat/hash the source again.
If it changed during parsing, discard the result and retry on the next pass.

---

## 14. Wiki generation in the downstream service

Call the same organized `graph.wiki.writer.write_wiki` used by `wiki_one.py`.

Do not create a new wrapper that reimplements the phase order. The port may provide a thin
call site, but the writer owns:

- format detection;
- format-specific planning;
- observation windows;
- semantic/structural seed plan;
- reference selection;
- section writing and judging;
- image placeholder preservation/restoration;
- intra-document links/navigation;
- export layout;
- local publication;
- cross-document linker;
- completion stamp.

### 14.1 State reuse

Keep `metadata/state/<document>` so interrupted and changed runs use the existing resume and
incremental behavior. Do not erase state before every run.

For a changed source, call the existing invalidation logic if it does not depend on Git
hunks. If it requires hunks, the downstream first version may invalidate the complete
document state and regenerate it, but this is a deliberate performance trade-off, not a
rewrite-quality reduction. The full source remains available and all normal validation
still runs.

Mark this ceiling in code:

```python
# ponytail: no-Git port invalidates the whole changed document; add a Markdown diff only
# when regeneration cost is measured as unacceptable.
```

### 14.2 Failure

If generation or linking fails:

- do not update the completed source hash;
- do not delete the last good GROWI document;
- retain resumable state and failure diagnostics;
- continue with independent source files if safe;
- return a non-zero overall result when any source failed.

---

## 15. Linker requirements in the downstream service

Copy the linker and its direct schemas/prompts/config exactly after WP-O1.

The downstream pipeline must preserve:

- standalone `metadata/wiki-linker.sqlite`;
- same-team exhaustive map scouting;
- FTS/dense/bridge/reranker discovery lanes;
- full endpoint reading;
- anti-no-op relationship rules;
- exact evidence validation;
- bounded hops;
- the 12-candidate cap and one-proposal-per-candidate contract;
- shared request-level research concurrency across pages;
- compact judge views built from verified evidence and bounded local context;
- durable per-page discovery, research, judge, and completed-link checkpoints keyed by
  content/corpus hashes;
- managed inline blocks and related-reading footers;
- bilateral local writes;
- lock/recovery behavior;
- stale-link removal.

### 15.1 Important publication consequence

Do not trust the changed source list as the GROWI publish list.

After any successful generation or deletion reconciliation:

1. enumerate every generated document folder under `wiki/`;
2. compute its document content hash;
3. compare it with `published_documents` in the ledger;
4. publish every mismatch;
5. remove remote documents present in the ledger but absent locally;
6. update each ledger row only after that document succeeds.

This detects old documents changed by reciprocal links and crash recovery.

### 15.2 Deletion order with links

For a deleted mounted source:

1. resolve its ledger `raw_rel` and local wiki folder;
2. call the linker's `remove_document` while the old endpoint files still exist;
3. verify surviving peers no longer contain the removed pair;
4. delete the owned remote GROWI document;
5. remove local wiki/state/raw artifacts;
6. remove the source ledger entry;
7. run the publication hash sweep so modified surviving peers reach GROWI.

If step 2 fails, stop deletion and retain local/remote content for retry.

---

## 16. GROWI publication contract

### 16.1 Authority model

The downstream service is one-way:

```text
local generated wiki -> GROWI
```

No GROWI edit is ingested back. Document this prominently. If preserving manual GROWI
edits becomes a requirement, that is a separate synchronization product, not a small flag.

### 16.2 Remote ownership

Every published page must include a stable publisher marker containing at least:

- publisher identity;
- local page identity;
- local content hash.

Reuse the existing marker/wrapper if it meets these needs. Deletion enumerates only beneath
the configured document path and trashes only owned pages.

### 16.3 Remote path mapping

Use a deterministic one-to-one mapping from local wiki page path to GROWI path.

For the downstream service, preserve the `.md` page suffix in the GROWI path so the wiki
generator's existing relative intra- and inter-document Markdown links resolve without
rewriting page bodies:

```text
local:  wiki/team-a/manual.docx/001-start.md
remote: /<write-root>/team-a/manual.docx/001-start.md
```

Before implementation, verify with a two-page GROWI integration test that relative links
ending in `.md` resolve as expected. If GROWI rejects or rewrites such paths, stop and add a
publish-boundary link translation with dedicated Markdown tests; do not modify the wiki
generator's stored Markdown.

Do not silently keep the current suffix-stripping behavior while publishing unmodified
`.md` links; that creates broken remote links.

### 16.4 Add/change/delete behavior

- **Add:** create every generated Markdown page in the remote document subtree.
- **Change:** finish local generation first, then trash owned pages in that remote document
  subtree and publish the complete new set.
- **Delete:** trash owned pages in the remote document subtree.
- **Link-only change:** publication hash sweep treats the affected existing document as
  changed and republishes it.

This intentionally chooses replacement over merge for the minimal pipeline. It matches the
stated requirement and avoids a second conflict-resolution system.

### 16.5 Partial failure

GROWI does not provide a multi-page transaction. Therefore:

1. keep local wiki output authoritative;
2. leave the ledger publication hash unchanged until the full document finishes;
3. retry the entire document on the next run;
4. make create/update/delete idempotent by path and ownership marker;
5. report partial remote state clearly.

Do not report success merely because some pages were created.

---

## 17. Downstream sync algorithm

Implement one function, conceptually `sync_once`, with this order:

```text
acquire global lock
load and validate ledger
scan mount and calculate content hashes
plan added / changed / deleted paths

for each deleted path:
    remove bilateral local linker relations
    delete owned remote document
    remove local raw/wiki/state artifacts
    update source ledger atomically

for each added or changed path:
    parse/copy source to raw atomically
    generate complete local wiki
    run linker as part of writer
    only now record completed source hash

scan all local wiki document folders
for each document whose content hash differs from publication ledger:
    replace owned remote document
    record publication hash only after complete success

for each published document absent locally:
    delete its owned remote subtree
    remove publication ledger row after success

write run summary
release lock
```

### 17.1 Per-source isolation

A failure for one added/changed source should not erase other successful work. Record the
error and continue when the failure has no shared-state corruption. A linker catalog or
bilateral commit failure is shared-state-sensitive; stop before remote publication and
retry after repair.

### 17.2 Ordering

Use sorted normalized paths. Process deletion reconciliation before additions so removed
pages cannot remain as hop/link candidates. Run one final global publication sweep after
all local work.

### 17.3 Cancellation

Check cancellation between documents and before remote replacement, not in the middle of
an atomic local file write. Pass the existing stop callback into the wiki writer/linker.

### 17.4 Result object

Return/write a structured summary:

```json
{
  "status": "complete|partial|failed|no_change",
  "added": [],
  "changed": [],
  "deleted": [],
  "published": [],
  "failed": [{"path": "...", "stage": "...", "error": "..."}],
  "started_at": "...",
  "finished_at": "..."
}
```

Never include secrets or whole document bodies.

---

## 18. Runner and operational surface

### 18.1 Commands

Provide two commands:

```text
python -m publisher.cli sync-once
python -m publisher.cli run --interval 60
```

`sync-once` performs exactly one reconciliation and exits non-zero on partial/failed work.

`run` performs one reconciliation immediately, then waits the configured interval and
repeats. It handles SIGTERM/SIGINT between documents and exits cleanly.

### 18.2 Why no FastAPI initially

The minimal publisher does not need inbound HTTP to do its job. The parser and GROWI are
already HTTP services. A CLI plus polling loop is smaller and can be run by systemd, Docker,
or cron.

Add `/health`, `/status`, and `/sync` only when an actual deployment supervisor needs HTTP.
Do not port the full current FastAPI app merely to obtain a health endpoint.

### 18.3 Logs

Log one line per stage/document with:

- run ID;
- normalized path;
- stage;
- elapsed time;
- result/counts;
- concise exception class/message.

Never log API tokens, parser authorization headers, base64 image media, complete prompts,
or complete pages.

---

## 19. Keeping both repositories in sync

The first synchronization mechanism should be boring and explicit.

### 19.1 Canonical ownership

Until a separate shared package is justified:

- `llm-wiki-neo/llm-wiki-dist/graph/wiki`, `formats`, `common`, and shared clients are the
  canonical implementation;
- behavior changes are made and tested there first;
- downstream receives the complete allowlisted files, not manually selected functions.

### 19.2 Sync manifest/tool

Add one downstream development command after the initial port:

```text
python tools/sync_upstream.py --source /path/to/llm-wiki-neo/llm-wiki-dist --check
python tools/sync_upstream.py --source /path/to/llm-wiki-neo/llm-wiki-dist --apply
```

It must:

1. contain an explicit allowlist of files/directories;
2. compare bytes and report drift in `--check` mode;
3. copy only allowlisted shared files in `--apply` mode;
4. never delete downstream-only orchestration/tests/config without an explicit allowlist;
5. record the upstream commit SHA in a small manifest;
6. run formatter-free so byte equality remains meaningful.

Do not introduce Git submodules or publish a shared package in the first port. Reconsider a
shared package only after repeated synchronized changes make the copy tool burdensome.

### 19.3 Downstream-only adapters

The following are allowed to differ downstream:

- minimal Settings fields/environment loading;
- mount scanner and JSON ledger;
- no-Git whole-document invalidation;
- CLI/polling runner;
- one-way GROWI replacement policy;
- deployment configuration.

Wiki prompts, schemas, algorithms, renderers, format planners, and linker logic are not
downstream adapters and must not drift.

---

## 20. Work packages for the downstream port

### WP-P0 — Create the skeleton

Create only the directories/files shown in section 10. Add `.gitignore` entries for
`data/`, caches, virtual environments, and secrets. Do not add Docker/Kubernetes files yet.

**Acceptance:** package imports with an empty runtime data folder.

### WP-P1 — Port shared code byte-for-byte

Copy the allowlist from section 10.1. Run copied upstream tests before writing downstream
orchestration. Fix import packaging only; do not change logic.

**Acceptance:** the same deterministic fake-model wiki fixture produces the same Markdown,
planning JSON, and normalized linker result as upstream.

### WP-P2 — Implement the ledger and scanner

Implement sections 11 and 12 using pathlib, hashlib, json, tempfile/atomic replace, and
fcntl. Add tests for paths, hashing, adds, changes, deletes, unsupported files, corrupt
ledger, and concurrent lock rejection.

**Acceptance:** repeated unchanged scans are no-ops; rename is delete+add; no Git executable
is invoked.

### WP-P3 — Integrate doc-parser

Port the organized parser client. Add mocked tests for request fields/headers, Markdown
passthrough, 415, 500, timeout, invalid JSON, heartbeat whitespace, source-race rejection,
and atomic raw replacement.

**Acceptance:** parser failure leaves last good raw and wiki output intact.

### WP-P4 — Invoke the complete wiki writer

Wire `sync_once` to `wiki.writer.write_wiki`. Preserve state, progress, stop callbacks, and
completion marker ordering. Add a fake-model integration test.

**Acceptance:** one input creates the same local wiki layout as upstream `wiki_one.py`.

### WP-P5 — Preserve and exercise the linker

Run the deterministic A/B/C/D linker corpus downstream. Add an integration test where the
second source causes both its page and a first-source page to change.

**Acceptance:** both files contain exactly one reciprocal inline block and footer entry;
the linker has no knowledge-engine imports.

### WP-P6 — Implement one-way GROWI replacement

Port the client/path/publisher subset. Add ownership markers, `.md` path compatibility test,
document replacement, stale-page removal, 409 retry, and partial failure behavior.

**Acceptance:** new/change/delete operations affect only owned pages under configured root.

### WP-P7 — Add global publication reconciliation

Implement the wiki-tree hash sweep. The test must generate X, have the linker edit older Y,
and prove both remote document publishers are called even though only X was newly ingested.

Also test a crash after Y publishes but before X publishes: unchanged ledger rows retry only
the unfinished documents safely.

### WP-P8 — Implement deletion end to end

Test:

1. source A and B produce a bilateral link;
2. remove A from mount;
3. run sync;
4. A's owned GROWI pages are removed;
5. A's raw/wiki/state entries are removed;
6. B's backlink is removed locally;
7. B is republished;
8. unrelated and human GROWI pages survive.

### WP-P9 — Add CLI and polling loop

Implement section 18 with signal handling and one global lock. Test `sync-once` exit codes
and ensure the polling loop does not overlap runs.

### WP-P10 — Documentation and deployment example

Document:

- required environment variables;
- doc-parser health check;
- GROWI credentials/root ownership;
- data volume mounts;
- `sync-once` dry operational check;
- polling invocation;
- backup/restore of `data/wiki`, state, linker DB, and ledger;
- the one-way authority warning.

Add Docker/systemd only when the actual deployment method is known.

---

## 21. Required automated test matrix

### 21.1 Organization tests

- new import paths work;
- approved old compatibility paths work;
- wiki has no knowledge import;
- workspace has no knowledge import;
- growi client/publisher has no knowledge import;
- app full stack still imports;
- CLI still imports;
- `wiki_one.py` only reaches allowed packages.

### 21.2 Wiki parity tests

- Markdown fixture output byte parity;
- planning manifest/coverage/metadata parity;
- state schema parity;
- prompt version parity;
- model-call count parity;
- retry/fallback parity;
- image-unit preservation parity;
- DOCX/PPTX/PDF structural planning parity;
- XLSX/CSV table output parity;
- intra-document link parity;
- linker catalog/schema parity;
- bilateral link rendering parity.

Normalize only known nondeterministic fields such as timestamps, UUID run IDs, and elapsed
seconds. Do not normalize page content or hashes.

### 21.3 Scanner/ledger tests

- empty mount;
- first add;
- unchanged rerun;
- same path changed bytes;
- delete;
- rename as delete+add;
- Unicode and nested paths;
- unsupported extension;
- symlink escape;
- corrupt ledger;
- interrupted atomic write;
- lock contention.

### 21.4 Parser tests

- each supported extension request;
- Markdown passthrough;
- headers and query parameters;
- long streaming response heartbeat;
- HTTP 415;
- transient HTTP failure;
- invalid JSON;
- empty Markdown;
- source changed during request;
- previous raw preserved on failure.

### 21.5 Pipeline tests

- add -> raw -> wiki -> GROWI;
- change -> local regeneration -> remote replacement;
- delete -> unlink -> remote delete -> local cleanup;
- parser failure stops only that source;
- wiki failure never deletes last good remote;
- linker failure never writes completion hash;
- GROWI partial failure leaves publication dirty;
- rerun completes dirty publication;
- cancellation between documents;
- no Git subprocess invocation.

### 21.6 GROWI tests

- write-root boundary;
- owned marker required for deletion;
- human page survives;
- `.md` relative link resolution/path mapping;
- stale generated page removed;
- 409 revision retry;
- page create failure;
- document delete partial failure;
- publication ledger updates only after full success.

### 21.7 Link publication tests

- new X links to old A and both are published;
- old A links to X with direction-specific prose;
- zero accepted links produces no managed blocks;
- changed X removes stale links from A and republishes A;
- deleted X removes backlinks and republishes peers;
- rerun is idempotent;
- interrupted bilateral local write recovers before publishing;
- malformed managed marker prevents remote publication.

---

## 22. Manual acceptance scenario

Use one team and three small documents.

1. Start doc-parser and verify `/health`.
2. Configure real chat, embedding, reranker, and GROWI endpoints.
3. Place document A under `mount/team-smoke/`.
4. Run `sync-once`.
5. Verify raw Markdown, generated wiki, linker complete marker, and GROWI pages.
6. Add document B containing a non-obvious relationship to A.
7. Run `sync-once`.
8. Verify local A and B both contain managed link blocks/footer entries.
9. Verify remote A and B both contain the same direction-specific relationship.
10. Add lexical distractor C and verify it is not linked merely for shared vocabulary.
11. Change B and verify it is regenerated locally before its remote subtree is replaced.
12. Remove B and verify A's backlink is removed and A is republished.
13. Run again unchanged and verify zero parser, wiki-model, linker-research, and GROWI write
    calls.
14. Stop GROWI, change A, and run: local generation may complete, but publication remains
    dirty and the command reports failure.
15. Restore GROWI and rerun: only dirty publication is repaired.

Capture generated paths, link snippets, marker status, and run summary. Do not commit real
generated corpus data or credentials.

---

## 23. Rollback strategy

### 23.1 Organization rollback

Because moves are isolated commits, revert the failing domain move rather than manually
moving files back. Compatibility shims keep entry points usable during the transition.

### 23.2 Downstream rollback

The current repository remains deployable until downstream acceptance completes. The new
service writes under its own GROWI root during testing. Rollback is stopping it and restoring
the previous publisher; no graph database migration is involved.

### 23.3 Data recovery

- raw Markdown can be recreated from mount through doc-parser;
- wiki output can be regenerated from raw plus state;
- linker DB is rebuildable, while generated Markdown is the product;
- pipeline ledger is rebuildable by scanning local sources/wiki, although remote ownership
  should be reconciled carefully;
- do not automatically delete an unknown remote page during ledger rebuild.

---

## 24. Definition of done

### Current repository

- [ ] linker correctness gate passes;
- [ ] target package structure exists;
- [ ] moves contain no hidden behavior changes;
- [ ] wiki and formats no longer depend on legacy or knowledge internals;
- [ ] knowledge engine remains complete;
- [ ] current application and `wiki_one.py` run;
- [ ] focused and full non-live test suites match baseline;
- [ ] real two/three-document linker smoke succeeds;
- [ ] docs describe new paths.

### Downstream repository

- [ ] only allowlisted wiki-factory modules are ported;
- [ ] no GraphStore/Librarian/Researcher/local graph database exists;
- [ ] mount add/change/delete works without Git;
- [ ] doc-parser service integration is retry-safe;
- [ ] wiki output matches upstream behavior;
- [ ] linker remains complete and bilateral;
- [ ] publication sweep catches old pages edited by the linker;
- [ ] GROWI add/change/delete affects only owned paths;
- [ ] `.md` links work remotely or publish-boundary translation is proven;
- [ ] interrupted work resumes without false success;
- [ ] unchanged runs perform no expensive work;
- [ ] sync drift check against upstream passes;
- [ ] manual acceptance scenario passes.

---

## 25. Final instructions to a weak implementer

1. Do WP-O0 first.
2. Fix and test the linker issues in section 8 before moving anything.
3. Make one mechanical move/extraction at a time.
4. Run focused tests after every commit.
5. Never edit prompt text during organization.
6. Never copy only “the important-looking half” of the wiki pipeline.
7. Finish WP-O9 before creating `wiki-publisher`.
8. Port only the explicit allowlist.
9. Use the JSON ledger, not Git, downstream.
10. Generate locally before deleting a changed remote document.
11. Always run the whole-wiki publication hash sweep after linker work.
12. On deletion, unlink before deleting local files.
13. Never delete an unmarked GROWI page.
14. Treat failed map-scout/linker-wide calls as incomplete; retry then visibly skip only an
    isolated failed research candidate so it cannot stop unrelated linking work.
15. Do not mark a source or publication complete until its final step succeeds.
16. If output parity changes unexpectedly, stop and find the cause; do not update the
    expected fixture to make the test green.

The intended result is two clear products sharing the same high-quality wiki factory:

- `llm-wiki-dist`: full wiki + GROWI + local graph/query engine;
- `llm-wiki-air/wiki-publisher`: mount + parser + wiki/linker + GROWI only.
