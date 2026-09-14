# Neo organization, one-phase linker, and minimal GROWI publisher port

Status: implementation plan. It replaces `docs/LINKER.md` (deleted) and the previous
version of this document. The four older plans (`PLAN_SYNC`, `PLAN_FORMATS`, `PLAN_GROWI`,
`PLAN_NEO`) stay as history; where they disagree with this document, this document wins.

The current repository (`llm-wiki-dist`) remains the source of truth until every gate in
this plan passes. The downstream target is a new sibling folder under
`/home/seigyo/c_repo/bhavneek/llm-wiki-air/`; its working name is `wiki-publisher/`.

Written for an implementer working one work package at a time. Where this document gives
code, type that code. Where it says delete, delete. If a snippet cannot fit the real file
(a name differs, a signature changed), stop and report the exact line; do not invent a
workaround.

---

## 0. Decisions already made (do not reopen)

| ID | Decision |
|---|---|
| D-1 | `graph/wiki/linker.py`, `tests/test_wiki_linker.py`, `docs/LINKER.md`, the four `LINKER_*_PROMPT_VERSION` constants, the linker Pydantic contracts in `wiki/wire.py`, and the linker prompt builders in `wiki/prompts.py` are **deleted**. Nothing from them is reused. |
| D-2 | Cross-document linking happens in **exactly one place: wiki creation**, inside the writer, after the local pages are published and before the source stamp. The knowledge engine no longer creates semantic edges with a model; it **parses** the links the writer wrote. |
| D-3 | There are two linker **modes**, selected by `WIKI_LINKER_MODE`: `legacy` (the `graph/librarian.py` chunk-linking logic, moved out of the engine) and `neo` (entities + behaviours from `plan.txt`). One mode runs per data root. Never both. |
| D-4 | Both modes share the same **chunking, chunk metadata call, catalog, embedding, rendering and publication** code. A mode only decides which peer chunks are candidates and which prompt filters them. |
| D-5 | The wiki writer keeps two **ingest modes**: `wiki` (lossless section rewrite, `graph/wiki/pipeline.py`) and `chunks` (the old concept-chunk writer, `graph/chunk.py` moved to `graph/wiki/legacy.py`). The `pages` shelf mode (`graph/pages.py`) is deleted. |
| D-6 | Links are **Markdown** (a managed footer on both endpoints; in `neo` mode also an inline link at the first mention of a used entity). `_planning/` never reaches GROWI, so anything a GROWI-only reader must see lives in the page body. |
| D-7 | The **original page before links** is kept as a file: `wiki/<doc>/_planning/pages/<page>.md`. Rendering is a pure function `render(original, edges)`; the published page is always rewritten from the original, never patched. No marker stripping, no "pending/committing" recovery states. |
| D-8 | Reciprocal edits to older documents are **republished**: the linker returns the touched documents, the full product publishes `changed ∪ touched`, the downstream product publishes by content-hash sweep. |
| D-9 | The footer is published to GROWI as its **own chunk-marked block** so a footer-only change never overwrites human edits inside the generated body block. |
| D-10 | `neo` mode uses a **closed label set**; `legacy` mode keeps the free verb-phrase labels of the old `EDGE_PROMPT`. |
| D-11 | Per-chunk metadata is **one structured call** returning summary, keywords, claims, entity, bridge probe, entities and behaviours. It is cached by chunk text hash in `_planning/chunks.json`; the catalog SQLite is rebuildable from those files. |

---

## 1. Plain-English overview

Two products live in `graph/`:

1. a **wiki factory**: source files → doc-parser → raw Markdown → wiki pages → links →
   GROWI; and
2. a **knowledge engine**: GROWI pages → SQLite graph/FTS/vectors → Librarian/Researcher
   queries.

The full product (`llm-wiki-dist`) has both. The minimal product (`wiki-publisher`) has only
the factory, and a cloud model reads GROWI directly. Because the minimal product has no
engine, everything a reader needs — including links — must be produced by the factory and
must be visible in the page Markdown. That is why linking moves to wiki creation (D-2) and why
links are Markdown (D-6).

```text
mount/<team>/x.docx
   | workspace.convert  (doc-parser /parse)                       PHASE 1
   v
raw/<team>/x_docx.md
   | workspace.writer.write_wiki
   |   wiki.pipeline (mode=wiki)  or  wiki.legacy (mode=chunks)   PHASE 2
   v
wiki/<team>/x.docx/NNN-*.md  +  _planning/                       (pristine pages)
   | linker.link_document                                         PHASE 3
   |   chunks -> meta -> catalog -> candidates(mode) -> edges -> render
   |   may rewrite pages of OTHER documents (reciprocal footer)
   v
wiki/<team>/*/NNN-*.md          (pages with footer [+ inline links])
   | growi.publisher   (changed ∪ touched documents)              PHASE 4
   v
GROWI
   | knowledge.librarian  (full product only)                     PHASE 5
   v
graph.sqlite -> researcher / app / MCP
```

Each folder under `graph/` is one phase. Porting the minimal product means copying the
phase-1..4 folders and writing a small runner around them.

### 1.1 The safety rule

Never combine a file move with an algorithm change. Every commit is either "moved code and
changed imports" or "changed behaviour and has focused tests". If a commit cannot be described
by one of those sentences, split it.

---

## 2. Goals

1. A reader identifies a module's phase from its directory.
2. The wiki writer (both ingest modes) behaves identically after the moves.
3. Cross-document links are created once, at wiki creation, in one of two selectable modes,
   and are visible in the page Markdown on both endpoints.
4. Regenerating, changing or deleting a document cleans its links from every peer page and
   republishes those peers.
5. The knowledge engine keeps working and stops spending model calls on its own semantic
   edges.
6. The downstream service runs without `graph.sqlite`, `engine.sqlite`, Git, Librarian or
   Researcher, and detects add/change/delete of mounted files without Git.
7. A failed parser, model, linker or GROWI call is retried safely and never mistaken for
   success.
8. Shared code is synchronized to the downstream repository by copying whole allowlisted
   folders, not functions.

---

## 3. Non-goals

- a new vector database, ChromaDB, FAISS, Qdrant, LanceDB, Elasticsearch;
- graph ingestion, Librarian, Researcher, realtime query, clustering, MCP, frontend in the
  downstream service;
- GROWI-to-local reverse sync downstream;
- Git downstream;
- a queue/worker framework, watchdog/inotify, Kubernetes, Celery, Redis;
- exhaustive all-pairs map scouting, deep multi-page "research" calls, judge calls, hop
  path validation, evidence quoting — all of that was LINKER.md and is gone;
- a plugin system for one parser or one GROWI;
- automatic merge with hand-edited GROWI pages;
- rename detection (a rename is delete + add).

---

## 4. Verified current state

### 4.1 Source-direction flow (today)

```text
graph.convert.convert_mount -> doc-parser POST /parse -> raw Markdown
  -> graph.sync.sync_raw (Git A/M/D)
  -> graph.writers.write_wiki
       build_wiki_output: formats + wiki.pipeline | pages.py | chunk.py
       publish_output: work/out/docs/*.md + _planning -> wiki/<doc>/
       run_wiki_linker (graph/wiki/linker.py, 3,862 lines)     <- deleted by this plan
       write_source_stamp
  -> graph.growi.GrowiPublisher.publish_document (only the changed source)
```

### 4.2 Knowledge flow (today)

```text
GROWI pages -> graph.growi.sync_growi_pages -> graph.librarian.sync_growi
  -> one Node per GROWI page
  -> _fill_derived_fields: SUMMARY_PROMPT, KEYWORD_PROMPT, CLAIM_PROMPT (3 calls/page)
  -> _store_vectors: body/summary/bridge vectors (+ BRIDGE_PROBE_PROMPT, 1 call/page)
  -> _knn_candidates: 3 dense + 2 BM25 channels, RRF k=60, top 16, + bridge candidates
  -> _build_semantic_edges: EDGE_PROMPT per group of 8 candidates
  -> graph.sqlite -> researcher
```

The candidate/edge part of this flow is the `legacy` linker mode. It moves to phase 3 and
runs on chunks of wiki pages. The engine keeps derived fields and vectors (search needs
them) and stops building semantic edges (D-2).

### 4.3 What already exists that the new linker reuses

- `graph/wiki/page.py::link_titles` — first plain-text mention linking that skips fences,
  headings, tables, HTML, images, inline code and existing links. Reused for `neo` inline
  entity links.
- `graph/wiki/page.py::_fence_flags` — fence detection for line-wise Markdown work.
- `graph/wiki/storage.py` — `write_text_atomic`, `write_json_atomic`, `read_json`,
  `read_text`, `sha256_text`, `canonical_json`, `hash_of`.
- `graph/wiki/model.py::ModelPort` — `structured(schema, messages)` / `text(messages)`.
- `graph/core.py::strip_image_media`, `strip_big_tables` — prompt/embedding sanitizers.
- `graph/gateway.py::Embedder` — `embed_documents(texts)`, `dim`, `model_name`.
- `graph/librarian.py::_knn_candidates`, `_rrf_fuse`, `_bridge_candidate_ids`,
  `_request_edges_for_group` — the legacy algorithm, to be re-typed in
  `graph/linker/legacy.py` against the catalog instead of `GraphStore`.
- `graph/core.py::SUMMARY_PROMPT`, `KEYWORD_PROMPT`, `CLAIM_PROMPT`, `BRIDGE_PROBE_PROMPT`,
  `EDGE_PROMPT` — moved to `graph/linker/prompts.py`; the engine imports them from there.
- `graph/growi.py::wrap_page`, `merge_marked_sections`, `publish_document` — publication
  markers; extended for the footer block (D-9).
- `metadata/state/<doc>/wiki/NNN-*.md` — the pipeline's own pristine output, which is what
  `_planning/pages/` is copied from at publish time.
- `sqlite-vec` is already a dependency and already loaded by `graph/store.py`.

### 4.4 Coupling that must be untangled before moves

- `graph/wiki/model.py` imports `make_llm`, `structured_ainvoke` from `graph/chunk.py`.
- `graph/wiki/markdown_blocks.py`, `graph/formats/tree.py` import fence helpers from
  `graph/chunk.py`.
- `graph/wiki/ids.py` imports `short_hash` from `graph/core.py`.
- `graph/writers.py` dispatches among wiki / pages / chunks and hosts the linker hook.
- `graph/core.py` mixes Settings, Node/Edge models, prompts and text helpers.
- `graph/gateway.py` mixes endpoint clients with engine composition.
- `graph/growi.py` mixes client, paths, publication and reverse sync.

---

## 5. Hard constraints

- **C1 — Preserve wiki quality.** Do not alter wiki prompts, prompt versions, wire schemas,
  retry counts, concurrency, ownership rules, section validation, image restoration, page
  naming, coverage metadata or fallbacks. The only permitted edit in `graph/wiki/page.py`
  is adding the alias `fence_flags = _fence_flags`.
- **C2 — One linker phase.** Cross-document links are created only by `graph/linker/`
  inside `workspace.writer.write_wiki`. The knowledge engine never calls `EDGE_PROMPT`.
- **C3 — Either/or.** A data root runs one linker mode. Switching mode requires
  `python -m graph.linker rebuild --mode <m>`; it is never automatic.
- **C4 — Originals are files.** `_planning/pages/<page>.md` is the pre-link page. A page is
  always rendered from it. The linker never reads the published page to compute anything.
- **C5 — Same team only.** Candidates, links, inline targets and relative paths never leave
  the first path component under `wiki/`.
- **C6 — Bilateral or absent.** An edge is rendered on both endpoint pages or on neither.
  Because rendering is derived from catalog state, this holds by construction; the
  end-to-end test still asserts it.
- **C7 — Markdown is the product.** `wiki-linker.sqlite` is a rebuildable cache of
  `_planning/chunks.json` + `_planning/links.json` across all documents. Deleting it must
  only cost embeddings.
- **C8 — No success before completion.** A source is stamped only after generation, linking
  and (downstream) publication succeed.
- **C9 — No engine imports in factory code.** `common`, `clients`, `workspace`, `formats`,
  `wiki`, `linker`, `growi/{client,paths,publisher}` must not import `knowledge`, `app`, or
  `graph.sqlite`. Enforced by `tests/test_boundaries.py`.
- **C10 — Bounded model calls.** Per chunk: one metadata call; per group of ≤4 candidates:
  one edge call. Nothing else.
- **C11 — Linux only.** `fcntl.flock` for the linker lock and the downstream global lock.
- **C12 — Moves are moves.** `git mv`, imports only, no formatter, no renames of functions,
  env vars, progress events or persisted fields, no prompt edits.

---

## 6. Target organization of `llm-wiki-dist`

```text
llm-wiki-dist/
  wiki_one.py            CLI: one raw file -> wiki -> links               shared
  app.py                 FastAPI                                          engine only
  mcp_server.py          MCP proxy                                        engine only

  graph/
    __init__.py
    config.py            Settings + from_env                              shared

    common/                                                               shared
      __init__.py
      hashing.py         short_hash, sha256 helpers            (from core.py)
      markdown.py        fence scan, table lines, strip_image_media,
                         strip_big_tables, chunk_text         (from chunk.py, core.py)
      async_tools.py     run_async_blocking                    (from chunk.py)

    clients/                                                              shared
      __init__.py
      chat.py            make_llm, structured_ainvoke          (from chunk.py)
      embeddings.py      Embedder                              (from gateway.py)
      reranker.py        Reranker                              (from gateway.py; engine use only)

    workspace/           PHASE 1  source -> raw, and the orchestrator
      __init__.py
      project.py         data-root paths, teams                 (from project.py)   shared
      parser_client.py   doc-parser POST /parse                 (from convert.py)   shared
      convert.py         mount scan -> raw + convert ledger     (from convert.py)   shared
      writer.py          write_wiki: build -> publish local -> link -> stamp; up_to_date;
                         write_index                            (from writers.py)   shared
      git_sync.py        raw Git A/M/D -> write_wiki -> publish changed ∪ touched
                                                                (from sync.py)      engine only

    formats/             PHASE 2a format-specific structure (unchanged files)
      __init__.py tree.py context.py docx.py pptx.py pdf.py xlsx.py csv.py tabular.py

    wiki/                PHASE 2  raw -> pages
      __init__.py __main__.py
      config.py          WikiConfig + versions (linker versions removed)
      wire.py            observation/plan/page schemas (linker contracts removed)
      prompts.py         wiki prompt builders (linker builders removed)
      model.py           ModelPort / ChatModelPort (imports clients.chat)
      storage.py ids.py images.py markdown_blocks.py schemas.py
      windows.py         observe
      document_map.py    seed plan
      page.py            sections, lossless checks, link_titles (+ fence_flags alias)
      pipeline.py        rewrite orchestration
      incremental.py     changed-hunk invalidation
      export.py          run dir -> docs/ + _planning/
      legacy.py          ingest_mode=chunks: concept-chunk writer   (chunk.py moved)

    linker/              PHASE 3  pages -> links                      NEW
      __init__.py        link_document(project, rel, ...) -> LinkResult; remove_document
      __main__.py        python -m graph.linker rebuild [--mode m] [--no-edges] | relink <doc> | status
      chunks.py          split page on H2, chunk ids, ChunkMeta call, chunks.json cache
      catalog.py         wiki-linker.sqlite: chunks, fts5, vec, entities, behaviours,
                         edges, edge_decisions; rebuild from _planning/
      legacy.py          mode=legacy: RRF over 3 dense + 2 FTS channels, bridge
                         candidates, EDGE_PROMPT in groups of 4
      neo.py             mode=neo: define/use edges, similarity footer, behaviour
                         hops 1-3, NEO_EDGE_PROMPT in groups of 4
      render.py          footer block, inline entity links, render(original, edges)
      prompts.py         CHUNK_META_PROMPT, EDGE_PROMPT (moved), NEO_EDGE_PROMPT
      wire.py            ChunkMeta, ChunkEntity, ChunkBehaviour, EdgeSuggestion(s),
                         NeoEdgeSuggestion(s)

    growi/               PHASE 4  pages -> GROWI
      __init__.py        re-exports
      client.py          GrowiAPIError, GrowiPage, GrowiClient                     shared
      paths.py           growi_segment, growi_path, team_of_path, assert_publish_path shared
      publisher.py       wrap_page, merge_marked_sections, publish_pages,
                         GrowiPublisher (body block + links block)                  shared
      reverse_sync.py    sync_growi_pages, registry_page                            engine only
      registry.py        encrypted connection registry                              engine only

    knowledge/           PHASE 5  GROWI -> graph -> query                            engine only
      core.py            Node/Edge models, engine prompts (linker prompts removed)
      gateway.py         LlmClient, ModelGateway
      store.py vectors.py vocab.py neighborhood.py
      librarian.py       ingest: derived fields + vectors as today; edges parsed from
                         the links footer; no EDGE_PROMPT / bridge / dedup calls
      researcher.py realtime.py

    cli.py               graph CLI (engine)

  tests/
  docs/                  DEV.md RUNNING.md ORG_AND_PORT.md (this file) + historical plans
```

`app.py`, `mcp_server.py`, `wiki_one.py` are adapters: imports and argument handling only.

### 6.1 Import direction

```text
common   config   clients                 (leaves; import nothing from graph/*)
   ^        ^        ^
formats  <-- wiki <-- linker              linker may import wiki (model, storage, page)
   ^          ^         ^                 wiki must NOT import linker
   +----- workspace ----+                 workspace.writer imports wiki + linker + formats
              |
              v
            growi                         growi imports config, common, workspace.project

knowledge  may import everything above.  Nothing above may import knowledge or app.
```

`tests/test_boundaries.py` walks every module under `graph/` except `knowledge/`, imports
it in a subprocess, and asserts `graph.knowledge`, `app`, `sqlite3`-backed `GraphStore`, and
`torch` are absent from `sys.modules`.

### 6.2 File disposition

| Current file | Final owner | Treatment |
|---|---|---|
| `graph/project.py` | `graph/workspace/project.py` | `git mv`; keep `linker_database` property |
| `graph/convert.py` | `graph/workspace/convert.py` + `parser_client.py` | extract `parse_document` first, then move |
| `graph/sync.py` | `graph/workspace/git_sync.py` | `git mv`; then WP-8 adds touched-doc publication |
| `graph/writers.py` | `graph/workspace/writer.py` | `git mv`; drop `pages` branch; linker hook rewritten in WP-8 |
| `graph/formats/*` | unchanged | imports only |
| `graph/wiki/*` | unchanged | imports only, except deletions listed in WP-1 |
| `graph/wiki/linker.py` | — | **delete** (WP-1) |
| `graph/chunk.py` | `graph/wiki/legacy.py` | extract shared helpers (WP-2), then `git mv` |
| `graph/pages.py` | — | **delete** with `tests/test_pages_*.py`, `tests/test_page_settings.py` (WP-1) |
| `graph/growi.py` | `graph/growi/{client,paths,publisher,reverse_sync}.py` | one extraction per commit |
| `graph/registry.py` | `graph/growi/registry.py` | `git mv` |
| `graph/core.py` | `graph/config.py` + `graph/common/*` + `graph/knowledge/core.py` | extract Settings, helpers, linker prompts; move remainder |
| `graph/gateway.py` | `graph/clients/*` + `graph/knowledge/gateway.py` | extract Embedder/Reranker; move remainder |
| `graph/librarian.py` | `graph/knowledge/librarian.py` | `git mv`; WP-10 removes semantic-edge calls |
| `graph/{researcher,realtime,store,vectors,vocab,neighborhood}.py` | `graph/knowledge/…` | `git mv` |
| `graph/cli.py` | unchanged | imports |
| new | `graph/linker/*` | WP-8, WP-9 |

### 6.3 Compatibility policy

Old top-level imports (`from graph.project import Project`, `from graph.core import
Settings`, `from graph.growi import GrowiClient`, `from graph.chunk import
_run_async_blocking`) are kept for one transition through re-export modules containing only
imports and `__all__`. They are deleted in WP-11 after `rg` shows no remaining internal user.

---

## 7. Data contract

### 7.1 Data root (both products)

```text
data/
  mount/<team>/<path>/<original-file>
  raw/<team>/<path>/<name>_<ext>.md            (+ .git in the full product only)
  metadata/
    convert.json                                mount conversion ledger
    last_sha                                    full product only
    pipeline.json  pipeline.lock                downstream only
    wiki-linker.sqlite (+ -wal, -shm)           linker catalog (cache)
    wiki-linker.lock                            linker process lock
    state/<team>/<document>/                    writer resume state (wiki mode)
      source/original.md  state/  work/  wiki/  (wiki/ = pipeline's own pristine pages)
    work/<team>/<document>/                     transient staging, removed after publish
  wiki/<team>/<document-folder>/
    NNN-<slug>.md                               PUBLISHED page = render(original, edges)
    _planning/
      metadata.json coverage.json manifest.json (writer, both ingest modes)
      source.json                               source stamp (written last)
      pages/NNN-<slug>.md                       ORIGINAL page, pre-link (D-7)
      chunks.json                               chunk metadata for this document (D-11)
      links.json                                every edge touching this document
      linker.json                               linker status for this document
```

`_planning/` is local-only: `zip_wiki` skips it, `GrowiPublisher.publish_document` globs
`*.md` in the folder root only. Nothing in `_planning/` is ever published.

### 7.2 `_planning/chunks.json`

```json
{
  "schema_version": 1,
  "meta_version": "wiki-chunk-meta-1",
  "document": "test/docx/x.docx",
  "team": "test",
  "pages": [
    {
      "filename": "003-第3章A-初期審査.md",
      "title": "第3章A 初期審査",
      "original_sha256": "…",
      "chunks": [
        {
          "chunk_id": "lchunk-…",
          "ordinal": 0,
          "heading": "",
          "line_start": 1,
          "line_end": 4,
          "text_sha256": "…",
          "summary": "…",
          "keywords": ["…"],
          "entity": "…",
          "claims": ["…"],
          "bridge_probe": "…",
          "entities": [{"name": "常任受託者", "kind": "role", "role": "uses"}],
          "behaviours": [{"subject": "常任受託者", "action": "利益相反を確認する", "object": "ケース"}]
        }
      ]
    }
  ]
}
```

`line_start`/`line_end` are 1-based line numbers inside the **original** page.

### 7.3 `_planning/links.json`

```json
{
  "schema_version": 1,
  "mode": "legacy",
  "edges": [
    {
      "edge_id": "ledge-…",
      "a": {"document": "test/docx/x.docx", "filename": "003-….md", "chunk_id": "lchunk-…"},
      "b": {"document": "test/docx/y.docx", "filename": "001-….md", "chunk_id": "lchunk-…"},
      "label": "prerequisite-for",
      "summary": "…",
      "source": "legacy_rrf",
      "via": [],
      "created_at": "…"
    }
  ]
}
```

`a` is the chunk that was being linked when the edge was created, `b` the peer;
`label` describes how `b` relates to `a` (the legacy `EDGE_PROMPT` convention). Every edge
touching a document appears in that document's `links.json`, so each edge exists in exactly
two files. The catalog rebuild dedupes by `edge_id`.

### 7.4 `_planning/linker.json`

```json
{
  "schema_version": 2,
  "status": "complete",
  "mode": "legacy",
  "meta_version": "wiki-chunk-meta-1",
  "edge_version": "wiki-link-edge-legacy-1",
  "run_id": "…",
  "chunks_total": 212,
  "chunks_new": 212,
  "meta_calls": 212,
  "edge_calls": 318,
  "edges_added": 87,
  "edges_removed": 0,
  "touched_documents": ["test/docx/y.docx", "test/docx/z.docx"],
  "finished_at": "…"
}
```

`status` is `pending | complete | failed | disabled`. `workspace.writer.up_to_date` returns
`False` while the marker is `pending` or `failed`.

### 7.5 Catalog: `metadata/wiki-linker.sqlite`

Owned only by `graph/linker/catalog.py::Catalog`. `sqlite3.connect(path, timeout=30)`,
`PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `sqlite3.Row`. Load `sqlite_vec`
exactly as `graph/store.py` does today.

```sql
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- keys: schema_version=1, mode, meta_version, edge_version, embed_model, embed_dim

CREATE TABLE IF NOT EXISTS documents (
  document   TEXT PRIMARY KEY,      -- wiki folder rel path, e.g. test/docx/x.docx
  team       TEXT NOT NULL,
  raw_rel    TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
  page_rel        TEXT PRIMARY KEY,  -- document + "/" + filename
  document        TEXT NOT NULL REFERENCES documents(document) ON DELETE CASCADE,
  filename        TEXT NOT NULL,
  title           TEXT NOT NULL,
  original_sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
  chunk_id        TEXT PRIMARY KEY,
  page_rel        TEXT NOT NULL REFERENCES pages(page_rel) ON DELETE CASCADE,
  document        TEXT NOT NULL,
  team            TEXT NOT NULL,
  ordinal         INTEGER NOT NULL,
  heading         TEXT NOT NULL,
  line_start      INTEGER NOT NULL,
  line_end        INTEGER NOT NULL,
  text_sha256     TEXT NOT NULL,
  summary         TEXT NOT NULL,
  keywords_json   TEXT NOT NULL,
  entity          TEXT NOT NULL,
  claims_json     TEXT NOT NULL,
  bridge_probe    TEXT NOT NULL,
  entities_json   TEXT NOT NULL,
  behaviours_json TEXT NOT NULL,
  vectors_ready   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chunks_page ON chunks(page_rel);
CREATE INDEX IF NOT EXISTS chunks_team ON chunks(team);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED, title, heading, summary, keywords, claims, body,
  tokenize='unicode61'
);

-- one vec0 table per channel; created after the first embedding reveals the dimension
-- CREATE VIRTUAL TABLE vec_body    USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[D]);
-- CREATE VIRTUAL TABLE vec_summary USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[D]);
-- CREATE VIRTUAL TABLE vec_bridge  USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[D]);

CREATE TABLE IF NOT EXISTS entities (            -- neo mode
  name_norm TEXT NOT NULL,
  name      TEXT NOT NULL,
  kind      TEXT NOT NULL,
  role      TEXT NOT NULL,                       -- defines | uses
  chunk_id  TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  team      TEXT NOT NULL,
  PRIMARY KEY (name_norm, chunk_id, role)
);
CREATE INDEX IF NOT EXISTS entities_lookup ON entities(team, name_norm, role);

CREATE TABLE IF NOT EXISTS behaviours (          -- neo mode
  chunk_id     TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  subject_norm TEXT NOT NULL,
  action       TEXT NOT NULL,
  object_norm  TEXT NOT NULL,                    -- '' when none
  team         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS behaviours_subject ON behaviours(team, subject_norm);
CREATE INDEX IF NOT EXISTS behaviours_object  ON behaviours(team, object_norm);

CREATE TABLE IF NOT EXISTS edges (
  edge_id    TEXT PRIMARY KEY,
  chunk_a    TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  chunk_b    TEXT NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
  label      TEXT NOT NULL,
  summary    TEXT NOT NULL,
  source     TEXT NOT NULL,                      -- legacy_rrf | legacy_bridge | define | use | similar | hop1 | hop2 | hop3
  via_json   TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS edges_a ON edges(chunk_a);
CREATE INDEX IF NOT EXISTS edges_b ON edges(chunk_b);

CREATE TABLE IF NOT EXISTS edge_decisions (      -- model answers, keyed by content
  hash_a       TEXT NOT NULL,                    -- text_sha256 of the linked chunk
  hash_b       TEXT NOT NULL,                    -- text_sha256 of the candidate
  mode         TEXT NOT NULL,
  edge_version TEXT NOT NULL,
  accepted     INTEGER NOT NULL,
  label        TEXT NOT NULL,
  summary      TEXT NOT NULL,
  PRIMARY KEY (hash_a, hash_b, mode, edge_version)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  document    TEXT NOT NULL,
  status      TEXT NOT NULL,                     -- running | complete | failed
  started_at  TEXT NOT NULL,
  finished_at TEXT NOT NULL DEFAULT '',
  error       TEXT NOT NULL DEFAULT ''
);
```

`ON DELETE CASCADE` means deleting a page row removes its chunks, entities, behaviours and
edges. The linker collects the peers of those edges **before** deleting so it can re-render
them.

IDs (SHA-256 hex, `[:20]`):

```text
chunk_id = "lchunk-" + sha256(document + "\0" + filename + "\0" + str(ordinal))
edge_id  = "ledge-"  + sha256("\0".join(sorted([chunk_a, chunk_b])))
run_id   = "lrun-"   + sha256(document + "\0" + started_at)
```

---

## 8. Linker design

### 8.1 Vocabulary

- **document**: one generated wiki folder, keyed by its rel path under `wiki/`.
- **page**: one `NNN-*.md` in that folder.
- **original**: `_planning/pages/<page>.md`, the pre-link page.
- **chunk**: one H2 section of an original page (see 8.3). Chunk 0 is the H1 + intro.
- **peer**: a chunk in another page of the same team.
- **edge**: an undirected relation between two chunks with a label and one summary.
- **touched document**: any document whose published page bytes changed in this run.

### 8.2 `link_document` — the whole algorithm

`graph/linker/__init__.py`:

```python
async def link_document(
    project: Project,
    rel: str,                        # raw rel path, e.g. test/docx/x_docx.md
    *,
    model: ModelPort,
    embedder: Embedder | None,
    settings: Any,
    on_progress: Progress = None,
    stop_check: StopCheck = None,
) -> LinkResult:
    """Chunk, describe, catalog, link and render one freshly published document."""
```

Steps, in this exact order. Each step is one function in the named module.

1. **Marker pending.** Write `_planning/linker.json` with `status: pending`, `mode`.
   (`render.write_marker`)
2. **Lock.** `fcntl.flock(metadata/wiki-linker.lock, LOCK_EX)` for the rest of the call.
   (`catalog.locked(project)` context manager)
3. **Open catalog.** `Catalog.open(project.linker_database, mode=settings.wiki_linker_mode,
   meta_version, edge_version)`. If `meta.mode` exists and differs from the requested mode,
   raise `LinkerModeMismatch("catalog is <m>; run: python -m graph.linker rebuild --mode
   <requested>")`. (C3)
4. **Bootstrap.** `catalog.sync_from_planning(project)`: for every
   `wiki/**/_planning/chunks.json` whose document is missing from the catalog or whose
   page `original_sha256` values differ, upsert pages/chunks/entities/behaviours and, from
   `links.json`, edges. Vectors are left `vectors_ready=0` and filled in step 8. This makes a
   deleted catalog file cost only embeddings (C7). Skip the document being linked.
5. **Originals.** `chunks.snapshot_originals(doc_dir)`: `rmtree(_planning/pages)`; copy every
   `doc_dir/*.md` to `_planning/pages/`. This is correct because `publish_output` just
   replaced the folder with pristine pages. Compute `original_sha256` per page.
6. **Chunk.** For every original page: `chunks.split_page(text) -> list[RawChunk]` (8.3).
   Assign `chunk_id`, `text_sha256`.
7. **Diff.** Compare with the catalog's rows for this document:
   - `unchanged`: same `chunk_id` and same `text_sha256`;
   - `changed`: same `chunk_id`, different hash;
   - `new`: id not in catalog;
   - `removed`: catalog id not in the new set.
   Collect `peers_before = {other endpoint of every edge touching changed ∪ removed}`.
   Delete edges touching changed ∪ removed; delete removed chunks. (`catalog.reconcile`)
8. **Metadata.** For every `new ∪ changed` chunk whose `text_sha256` is not already in this
   document's previous `chunks.json` cache (regeneration that moved a section keeps its
   meta): one `ChunkMeta` structured call (8.4), under `asyncio.Semaphore(concurrency)`.
   Mechanical validation (8.4). Write `_planning/chunks.json`. Upsert chunks, fts,
   entities, behaviours. (`chunks.describe_all`)
9. **Embed.** For every chunk in the team with `vectors_ready=0` (this document's new/changed
   ones plus any bootstrapped ones): `embedder.embed_documents` on body, summary,
   bridge_probe (three batches, ≤64 texts per request, run in `asyncio.to_thread`). Create
   the `vec_*` tables on first use with the observed dimension; if `meta.embed_model` or
   dimension differs from the embedder's, drop the three vec tables and set every chunk
   `vectors_ready=0` first. If `embedder is None` or embedding raises, log, leave
   `vectors_ready=0`, and continue: legacy mode then runs on the two FTS channels only; neo
   mode skips the similarity group. (`catalog.embed_pending`)
10. **Candidates.** For every `new ∪ changed` chunk: `mode.candidates(catalog, chunk) ->
    list[Candidate]` (8.7 legacy, 8.9 neo). A candidate is
    `Candidate(chunk_id, source, via: list[str], programmatic: bool, label: str = "",
    summary: str = "")`. Programmatic candidates are accepted as edges without a model call.
11. **Edge filter.** Non-programmatic candidates, in groups of `EDGE_GROUP_SIZE = 4`, one
    structured call per group with the mode's prompt (8.8 / 8.9). Before calling, look up
    `edge_decisions` by `(hash_a, hash_b, mode, edge_version)` and skip candidates already
    decided (both accepted and rejected). Store every decision. Accepted suggestions become
    edges. Retry a call once on validation error; after that, log the group and continue
    (an unrelated group must still be linked).
12. **Store edges.** Insert edges (ignore duplicates by `edge_id`). Write
    `_planning/links.json` for this document and for every document that gained or lost an
    edge in this run (`peers_before ∪ new peers`).
13. **Render.** `touched_pages = pages of this document ∪ pages of every chunk in
    peers_before ∪ pages of every new edge's peer`. For each: `render.render_page(original,
    edges_for_page, mode)` (8.10); write with `write_text_atomic` only if bytes differ.
    `touched_documents = {document of every page whose bytes changed} − {this document}`.
14. **Marker complete.** Write `linker.json` (7.4) for this document; for each touched
    document rewrite its `linker.json` `touched_by = run_id` (status unchanged).
15. **Unlock, return** `LinkResult(touched_documents, edges_added, edges_removed, …)`.

Any exception: write `linker.json` `status: failed, error: <class>: <message>[:500]`, mark
the run failed, re-raise. Files already rendered stay — they are derived from catalog state
that was committed, so a rerun converges. The catalog writes of steps 7, 8, 12 are each one
short transaction; no transaction is held across a model or embedding call.

### 8.3 Chunking rule (`chunks.split_page`)

```python
def split_page(text: str) -> list[RawChunk]:
    """Cut one original page into H2 sections; chunk 0 is the H1 + intro."""
    lines = text.splitlines()
    flags = fence_flags(lines)                  # graph.wiki.page.fence_flags
    starts = [i for i, line in enumerate(lines)
              if not flags[i] and line.startswith("## ")]
    bounds = [0] + starts + [len(lines)]
    chunks: list[RawChunk] = []
    for ordinal, (s, e) in enumerate(zip(bounds, bounds[1:])):
        body = "\n".join(lines[s:e]).strip("\n")
        if not body.strip():
            continue
        heading = lines[s][3:].strip() if ordinal > 0 else ""
        chunks.append(RawChunk(ordinal=len(chunks), heading=heading,
                               line_start=s + 1, line_end=e, text=body))
    return chunks
```

Rules:

- The navigation footer the pipeline appends (`前のページ: … ｜ 次のページ: …` after a
  `---`) belongs to the last chunk; it is harmless for metadata and is stripped from the
  model text by `chunks.model_text` (drop everything after the last `\n---\n` if it
  contains `前のページ` or `次のページ`).
- `model_text(chunk) = strip_big_tables(strip_image_media(text))[:12000]`.
- A page with no H2 is one chunk. Chunk-mode pages (`wiki/legacy.py`) usually are.
- `text_sha256 = sha256_text(text)` on the raw chunk text (not the model text).
- No character-window chunking. `chunk_text` stays in `common/markdown.py` for the engine.

### 8.4 Chunk metadata (`chunks.describe`)

`graph/linker/wire.py`:

```python
class ChunkEntity(BaseModel):
    name: str = ""                      # exact surface form as written in the chunk
    kind: str = ""                      # person|organization|role|product|api|function|
                                        # parameter|error_code|document|rule|procedure|
                                        # concept|place|other
    role: Literal["defines", "uses"] = "uses"

class ChunkBehaviour(BaseModel):
    subject: str = ""                   # an entity name from `entities`
    action: str = ""                    # short verb phrase, <= 60 chars
    object: str = ""                    # an entity name from `entities`, or ""

class ChunkMeta(BaseModel):
    summary: str = ""                   # 1-3 sentences (legacy SUMMARY_PROMPT rules)
    keywords: list[str] = Field(default_factory=list)   # <= 12 (legacy KEYWORD_PROMPT)
    entity: str = ""                    # main topic (legacy CLAIM_PROMPT)
    claims: list[str] = Field(default_factory=list)     # <= 20 (legacy CLAIM_PROMPT)
    bridge_probe: str = ""              # 1-2 sentences (legacy BRIDGE_PROBE_PROMPT)
    entities: list[ChunkEntity] = Field(default_factory=list)      # <= 20
    behaviours: list[ChunkBehaviour] = Field(default_factory=list) # <= 20
```

`graph/linker/prompts.py::CHUNK_META_VERSION = "wiki-chunk-meta-1"` and
`chunk_meta_prompt(*, page_title, heading, document, text, output_language) -> Prompt`
(reuse `graph.wiki.prompts.Prompt`, `COMMON_RULES`, `_schema_hint`, `_language_rule`).
System text:

```text
あなたはWiki横断リンク用のチャンク記述者である。与えられたWikiページの一節を読み、
検索とリンク判定に必要な情報だけを構造化して返す。
<COMMON_RULES>

JSON形式:
<schema hint for ChunkMeta>
```

Body text (one string; `{…}` are Python format fields):

```text
# 対象
- 文書: {document}
- ページ: {page_title}
- 節: {heading or "(導入)"}
- 出力は{output_language}で書く。

# summary
<SUMMARY_PROMPT text, verbatim from graph/core.py>

# keywords
<KEYWORD_PROMPT text, verbatim>

# entity / claims
<CLAIM_PROMPT text, verbatim>

# bridge_probe
<BRIDGE_PROBE_PROMPT text, verbatim>

# entities
この節に登場する固有のエンティティ（人物、組織、役割、製品、API、関数、パラメータ、
エラーコード、文書名、規則名、手順名、概念、場所）を最大20件。
- name は本文に書かれている表記をそのまま写す（言い換え・翻訳・要約は禁止）。
- role は、この節がそのエンティティを定義・宣言・仕様説明・初出解説している場合は
  "defines"、単に使用・言及している場合は "uses"。
- 一般名詞や、この節にしか出てこないと思われる些末な語は含めない。

# behaviours
この節で「誰が／何が、何をしているか」を最大20件。subject と object は entities の
name のいずれかと一致させる。object が無い場合は空文字。action は短い動詞句。

--- 本文 ---
{text}
```

Mechanical validation after the call (`chunks.validate_meta(meta, text) -> ChunkMeta`):

1. `summary`, `entity`, `bridge_probe`: collapse whitespace; `summary[:1000]`.
2. `keywords`: strip, dedupe case-insensitively, keep first 12 (as `_extract_keywords`).
3. `claims`: collapse whitespace, dedupe case-insensitively, keep first 20 (as
   `_extract_claims`).
4. `entities`: drop entries whose `name` (after `strip`) does not occur verbatim in the raw
   chunk text, whose length < 2, or whose `kind` is empty; dedupe by `normalize_name`;
   keep first 20.
5. `behaviours`: drop entries whose `subject` is not a surviving entity name or whose
   `action` is empty; set `object = ""` when it is not a surviving entity name; keep first
   20.
6. `normalize_name(s) = " ".join(unicodedata.normalize("NFKC", s).casefold().split())`.

Never retry because of dropped entities; only a Pydantic/JSON failure triggers the one
retry that `_structured_with_artifacts`-style callers already do. On a second failure,
store an empty `ChunkMeta` for that chunk (summary = first 300 characters of the model
text) and count it in `linker.json.meta_fallbacks`. The chunk still gets FTS rows and
body vectors, so legacy mode can still link it.

Artifacts: `metadata/state/<doc>/work/linker/<run-id>/meta-<ordinal>-<page>.prompt.md`
and `.json`, same layout the wiki pipeline uses for section prompts.

### 8.5 FTS rows

For every chunk: `INSERT INTO chunks_fts VALUES (chunk_id, title, heading, summary,
" ".join(keywords), " ".join(claims), model_text[:8000])`. Delete + reinsert on change.
Query construction (`catalog.fts_search(text, team, limit, exclude_page)`): tokenize on
whitespace and punctuation, keep tokens of length ≥ 2, quote each token, join with `OR`,
cap at 40 tokens. Always filter by `team` and exclude the target page via a join on
`chunks`.

### 8.6 Embedding

Three channels per chunk, mirroring `_store_vectors`: `body` (= model text), `summary`,
`bridge` (skipped when `bridge_probe` is empty). `vec_search(channel, vector, team, k,
exclude_page)` returns `[chunk_id, …]` ordered by distance, filtered by team and page via
a join on `chunks`. Use the same `sqlite_vec` `MATCH … LIMIT k` query shape as
`graph/store.py`.

### 8.7 `legacy` mode candidates (`legacy.candidates`)

A transcription of `Librarian._knn_candidates` + `_bridge_candidate_ids` onto the catalog:

```python
LEGACY_K = 50                   # settings.edge_candidate_k today
MAX_FUSED_CANDIDATES = 16       # librarian._MAX_FUSED_CANDIDATES
BRIDGE_CANDIDATE_CAP = 5        # librarian._BRIDGE_CANDIDATE_CAP
RRF_K = 60

def candidates(catalog, chunk, *, team) -> list[Candidate]:
    channels: list[list[str]] = []
    for channel in ("body", "summary", "bridge"):
        vector = catalog.vector(channel, chunk.chunk_id)      # None when vectors_ready=0
        if vector:
            ids = catalog.vec_search(channel, vector, team, LEGACY_K + 1, exclude_page=chunk.page_rel)
            if ids: channels.append(ids)
    lexical = " ".join(filter(None, [chunk.title, chunk.model_text[:1500]]))
    ids = catalog.fts_search(lexical, team, LEGACY_K + 1, exclude_page=chunk.page_rel)
    if ids: channels.append(ids)
    if chunk.keywords:
        ids = catalog.fts_search(" ".join(chunk.keywords), team, LEGACY_K + 1, exclude_page=chunk.page_rel)
        if ids: channels.append(ids)
    ranked = rrf_fuse(channels, RRF_K)[:MAX_FUSED_CANDIDATES]
    bridge: list[str] = []
    # same main entity, even if embeddings sit far apart
    if chunk.entity.strip():
        for cid in catalog.chunks_with_entity(chunk.entity, team, exclude_page=chunk.page_rel):
            if cid not in ranked and cid not in bridge:
                bridge.append(cid)
            if len(bridge) >= BRIDGE_CANDIDATE_CAP: break
    # 2-hop through existing edges of the top-5 fused candidates
    for neighbour in ranked[:5]:
        if len(bridge) >= BRIDGE_CANDIDATE_CAP: break
        for other in catalog.edge_peers(neighbour):
            if other != chunk.chunk_id and other not in ranked and other not in bridge \
               and catalog.page_of(other) != chunk.page_rel:
                bridge.append(other)
                if len(bridge) >= BRIDGE_CANDIDATE_CAP: break
    return ([Candidate(cid, source="legacy_rrf", programmatic=False) for cid in ranked]
            + [Candidate(cid, source="legacy_bridge", programmatic=False) for cid in bridge])
```

`rrf_fuse` is the four-line function from `Librarian._rrf_fuse`. `chunks_with_entity`
matches `chunks.entity` case-insensitively (the engine's `get_nodes_by_entity` behaviour).
Candidates from the same **page** are excluded; other pages of the same document are
allowed, as the engine allowed them.

### 8.8 `legacy` edge filter

`EDGE_PROMPT` moves verbatim from `graph/core.py` to `graph/linker/prompts.py`;
`EDGE_VERSION_LEGACY = "wiki-link-edge-legacy-1"`. `EdgeSuggestion`/`EdgeSuggestions` move
verbatim from `graph/core.py` to `graph/linker/wire.py` (`knowledge/core.py` re-imports
them for `_request_edges_for_group`'s type until WP-10 deletes that function).

Payload per group (transcribed from `_request_edges_for_group`, chunk fields instead of
node fields):

```python
payload = {
  "new_node": {"id": chunk.chunk_id, "title": f"{chunk.title} › {chunk.heading}".rstrip(" ›"),
               "summary": chunk.summary, "keywords": chunk.keywords,
               "header": chunk.document, "body": chunk.model_text[:4000]},
  "candidates": [{"id": c.chunk_id, "title": …, "summary": c.summary, "keywords": c.keywords,
                  "header": c.document, "body": c.model_text[:1200]} for c in group],
}
messages = [SystemMessage(EDGE_PROMPT), HumanMessage(json.dumps(payload, ensure_ascii=False))]
result = await model.structured(EdgeSuggestions, messages, max_output_tokens=2000)
```

Accept a suggestion only if `target_node_id` is in the group and not the chunk itself;
`label = suggestion.label.strip() or "related"`; `summary = suggestion.summary.strip()`.
Groups of `EDGE_GROUP_SIZE = 4` (the engine used 8; 4 is the size the old chunk corpus was
run with and keeps weak models focused).

### 8.9 `neo` mode (`neo.candidates`, `neo.render_groups`)

Three candidate groups, produced in this order:

**(a) Definition / usage — programmatic.**

```python
MAX_USERS_PER_DEFINITION = 12

for ent in chunk.entities:
    if ent.role == "uses":
        definers = catalog.entity_chunks(team, normalize_name(ent.name), role="defines",
                                         exclude_page=chunk.page_rel)
        if len(definers) == 1:
            yield Candidate(definers[0], source="use", via=[ent.name], programmatic=True,
                            label="defines", summary=f"「{ent.name}」の定義")
        elif 2 <= len(definers) <= EDGE_GROUP_SIZE:
            for d in definers:              # ambiguous: let the model pick at most one
                yield Candidate(d, source="use", via=[ent.name], programmatic=False)
    else:  # defines
        users = catalog.entity_chunks(team, normalize_name(ent.name), role="uses",
                                      exclude_page=chunk.page_rel)[:MAX_USERS_PER_DEFINITION]
        for u in users:
            yield Candidate(u, source="define", via=[ent.name], programmatic=True,
                            label="uses", summary=f"「{ent.name}」を使用")
```

`label` follows the convention "how the peer relates to this chunk": from a using chunk the
peer `defines`; from a defining chunk the peer `uses`. Both edges are the same undirected
`edge_id`, so the second insert is ignored.

**(b) Similar — programmatic, no model.** The RRF ranking of 8.7 (same channels, no bridge
step), top `NEO_SIMILAR_K = 5`, `source="similar"`, `label="similar"`, `summary =
peer.summary[:120]`. Skipped entirely when vectors are unavailable. These are the pages
query-time RAG would find anyway; they are listed so a GROWI-only reader gets them too.

**(c) Behaviour hops — model-filtered.** Let `S = {normalize_name(e.name) for e in
chunk.entities}` and `obvious = set(top-16 RRF ids)`.

```python
HOP1_MAX, HOP2_MAX, HOP3_MAX = 8, 8, 4

hop1 = chunks (other pages, same team) that have a behaviour whose subject or object ∈ S,
       ordered by |shared entities| desc, then chunk_id; take HOP1_MAX
hop2 = for e1 ∈ S: for (e1, e2) in behaviours anywhere: chunks whose behaviours mention e2
       and that contain no entity of S; via=[e1, e2]; take HOP2_MAX unique
hop3 = one more step (e2 -> e3), via=[e1, e2, e3]; take HOP3_MAX unique
candidates = (hop1 + hop2 + hop3) − obvious − already selected in (a)/(b) − same page
```

Each is `Candidate(cid, source="hop1|hop2|hop3", via=[…], programmatic=False)`.

**Neo edge filter.** `EDGE_VERSION_NEO = "wiki-link-edge-neo-1"`, groups of 4,
`NeoEdgeSuggestions`:

```python
NeoLabel = Literal["defines", "uses", "prerequisite", "consequence", "constraint",
                   "alternative", "contradicts", "interacts"]

class NeoEdgeSuggestion(BaseModel):
    target_chunk_id: str
    label: NeoLabel = "interacts"
    summary: str = ""                  # 1 sentence naming the shared entity/behaviour

class NeoEdgeSuggestions(BaseModel):
    edges: list[NeoEdgeSuggestion] = Field(default_factory=list)
```

`NEO_EDGE_PROMPT` (system):

```text
あなたはWiki横断リンクの判定者である。対象チャンクと、エンティティの振る舞いグラフを
1〜3ホップたどって見つかった候補チャンクが渡される。各候補には、どのエンティティを
経由して到達したか（via）と、候補側の entities / behaviours が付いている。
ルール:
- 与えられた候補IDのみを使う。
- リンクを提案するのは、読者が対象チャンクを理解・実行する上で候補が具体的に役立つ
  場合だけ（前提、結果、制約、代替、矛盾、同じエンティティの別の振る舞い）。
- 単に同じ語が出てくるだけの候補は提案しない。候補が少なくても無理につなげない。
- label は列挙値から選ぶ。summary は、どのエンティティのどの振る舞いでつながるかを
  1文で書く。
```

Payload per group: the target chunk (`title`, `heading`, `summary`, `entities`,
`behaviours`, `body[:3000]`) and for each candidate `chunk_id`, `title`, `heading`,
`summary`, `entities`, `behaviours`, `via`, `body[:1000]`. Validation: id in group,
label in the literal, summary non-empty. For the ambiguous-definer case in (a) accept at
most one suggestion per entity name (first in model order).

**Inline links (neo only).** For every programmatic `use` edge whose peer is in another
page, `render.inline_targets(page)` yields `(entity_surface_name, relative path to definer
page)`; `render_page` applies `graph.wiki.page.link_titles(markdown, targets)` once per page,
which links the first plain mention only, skipping fences/headings/tables/images/existing
links. Longest names first (already how `link_titles` sorts). No inline links in legacy mode.

### 8.10 Rendering (`render.py`)

```python
FOOTER_START = "<!-- llm-wiki-links:start -->"
FOOTER_END   = "<!-- llm-wiki-links:end -->"
FOOTER_TITLE = "## 関連リンク"
MAX_FOOTER_ENTRIES = 30
```

```python
def render_page(original: str, *, page_rel: str, edges: list[RenderEdge], mode: str) -> str:
    """Pure: the published page for this original and these edges."""
    body = original.rstrip("\n") + "\n"
    if mode == "neo":
        body = link_titles(body, inline_targets(page_rel, edges))   # graph.wiki.page.link_titles
    if not edges:
        return body
    lines = [FOOTER_START, FOOTER_TITLE, ""]
    for e in ordered(edges)[:MAX_FOOTER_ENTRIES]:
        peer = f"{e.peer_title} › {e.peer_heading}" if e.peer_heading else e.peer_title
        arrow = "" if e.forward else "← "
        lines.append(f"- [{peer}]({relative_link(page_rel, e.peer_page_rel)}) — {arrow}{e.label}: {e.summary}")
    lines.append(FOOTER_END)
    return body + "\n" + "\n".join(lines) + "\n"
```

- `RenderEdge` is built from an `edges` row plus both chunks' page/title/heading; `forward`
  is `True` when this page holds `chunk_a` (the label reads "peer *label* this"), else the
  arrow marks the reverse reading.
- `ordered(edges)`: by group (`define`/`use` first, then `similar`, then everything else),
  then `label`, then `peer_title`, then `edge_id`. Deterministic.
- `relative_link(from_page_rel, to_page_rel)`: `PurePosixPath` relative path with
  `os.path.relpath` semantics, POSIX separators, `.md` preserved.
- The footer goes after the pipeline's navigation footer, i.e. at the very end.
- Idempotent by construction: same original + same edges → same bytes.
- `write_if_changed(path, text)` compares bytes before `write_text_atomic`.

`render.parse_footer(text) -> list[FooterLink(peer_path, label, summary, reverse: bool)]`
is the exact inverse for one page and is what `knowledge/librarian.py` uses (§10).

### 8.11 Regeneration, change, and removal

- **Regenerated document, same pages:** step 7 classifies chunks. Unchanged chunks keep
  edges. Changed/new chunks are re-described (cache by text hash) and re-linked; their old
  edges are dropped and their peers re-rendered (edges may reappear via `edge_decisions`
  without a model call).
- **Page renamed / renumbered:** its chunk ids change → old chunks `removed`, new ones
  `new`. `chunks.json` cache by text hash keeps meta; `edge_decisions` keeps decisions; so
  the cost is FTS/vector upserts and rendering.
- **Removed document:** `remove_document(project, rel)`: lock; collect peers of all its
  edges; delete its `documents` row (cascade); rewrite peers' `links.json`; re-render peer
  pages; return touched documents. Called by `git_sync` before `rmtree(wiki_dir)` and by
  the downstream `sync_once` deletion step.
- **Mode switch / prompt version bump:** `python -m graph.linker rebuild --mode <m>`:
  delete the catalog file, delete every `links.json`, set every `linker.json` to `pending`,
  then run `link_document` for every document in `wiki/` in sorted order (metadata reused
  from `chunks.json` only if `meta_version` matches). `relink <document>` does it for one
  document; `status` prints documents, chunk counts, edge counts and pending markers;
  `rebuild --no-edges` rebuilds the catalog and renders every page from its original with
  zero edges (the link-free state used for rollback, §15).

### 8.12 Locking and failure

- One `fcntl.flock` on `metadata/wiki-linker.lock` around steps 3–14. A second process
  blocks (the writer runs documents sequentially anyway).
- The catalog is never written while a model/embedding call is in flight.
- `stop_check` is consulted between chunks in steps 8, 10, 11 and between pages in 13;
  cancellation raises `LinkerCancelled` → `status: failed` → the writer re-raises.
- A crash between step 12 and 14 leaves published pages partially rendered but the
  catalog complete; the marker is `pending`, so the document is not up to date and the next
  writer run re-executes `link_document`, which re-renders everything from state.

### 8.13 Settings and constants

`graph/config.py` (`Settings`), replacing the three `wiki_linker_*` fields:

```python
    ingest_mode: Literal["chunks", "wiki"] = "wiki"
    # cross-document linker (graph/linker), runs inside the wiki writer
    wiki_linker_enabled: bool = True
    wiki_linker_mode: Literal["legacy", "neo"] = "legacy"
    wiki_linker_concurrency: int = 0          # 0 = wiki_rewrite_concurrency
    # knowledge engine: parse links from pages; never build them with a model
    engine_semantic_edges: bool = False
```

`from_env` additions (same style as the existing lines):

```python
            ingest_mode=env("WIKI_INGEST_MODE", cls.ingest_mode),
            wiki_linker_enabled=env("WIKI_LINKER_ENABLED", "1" if cls.wiki_linker_enabled else "0") == "1",
            wiki_linker_mode=env("WIKI_LINKER_MODE", cls.wiki_linker_mode),
            wiki_linker_concurrency=int(env("WIKI_LINKER_CONCURRENCY", cls.wiki_linker_concurrency)),
            engine_semantic_edges=env("WIKI_ENGINE_SEMANTIC_EDGES", "0") == "1",
```

Delete `WIKI_LINKER_MAP_CONCURRENCY` / `WIKI_LINKER_RESEARCH_CONCURRENCY`. Delete the
`pages` literal and every `page_*` setting; delete the `pages` branch in the frontend
settings form in WP-11.

Named constants in `graph/linker/`: `EDGE_GROUP_SIZE = 4`, `LEGACY_K = 50`,
`MAX_FUSED_CANDIDATES = 16`, `BRIDGE_CANDIDATE_CAP = 5`, `RRF_K = 60`,
`NEO_SIMILAR_K = 5`, `HOP1_MAX = 8`, `HOP2_MAX = 8`, `HOP3_MAX = 4`,
`MAX_USERS_PER_DEFINITION = 12`, `MAX_FOOTER_ENTRIES = 30`, `EMBED_BATCH = 64`,
`META_MAX_OUTPUT_TOKENS = 4000`, `EDGE_MAX_OUTPUT_TOKENS = 2000`. No environment variable
per constant.

### 8.14 Progress and artifacts

Progress events through the writer's callback, `stage="linker"`:

```text
linker/pending  linker/bootstrap  linker/chunks  linker/meta  linker/embed
linker/candidates  linker/edges  linker/render  linker/done  linker/failed
```

Each carries `document`, and where relevant `current`, `total`, `cache_hits`, `edges`.
Never log bodies, keys or base64.

Artifacts under `metadata/state/<document>/work/linker/<run-id>/`:

```text
run.json                                  counts per step, cache hits, touched documents
meta-<page>-<ordinal>.prompt.md / .json
edge-<page>-<ordinal>-<group>.prompt.md / .json
*-error.txt
```

### 8.15 Cost

Let `C` = new/changed chunks in the run, `G` = candidate groups per chunk (legacy ≤ 6,
neo usually ≤ 5). Model calls = `C` metadata calls + `≤ C·G` edge calls, minus
`edge_decisions` hits. Embedding requests = `3·C / 64`. A 53-page, ~210-chunk handbook is
about 210 + ~900 short calls on first run and zero on an unchanged rerun. This matches the
old per-chunk engine cost the manager accepted (350 chunks × 4 calls + edge groups).

---

## 9. GROWI publication changes

### 9.1 Footer as its own block (D-9) — `growi/publisher.py`

```python
def split_footer(body: str) -> tuple[str, str]:
    """(page body without the links footer, the footer block or '')."""
    start = body.find(FOOTER_START)
    if start < 0:
        return body, ""
    end = body.find(FOOTER_END, start)
    if end < 0:
        raise ValueError("unterminated llm-wiki-links footer")
    end += len(FOOTER_END)
    return body[:start].rstrip("\n") + "\n", body[start:end]

def wrap_links(footer: str, *, page_id: str) -> str:
    digest = hashlib.sha256(footer.encode("utf-8")).hexdigest()[:12]
    return f"<!-- chunk: {page_id}-links hash:{digest} -->\n{footer.rstrip()}\n<!-- chunk-end: {page_id}-links -->"
```

In `GrowiPublisher.publish_document`, replace the `"body": wrap_page(...)` line with:

```python
                main, footer = split_footer(body)
                page_id = "page" + page_path.replace(" ", "_")
                pages.append({
                    "path": page_path,
                    "body": wrap_page(main, page_id=page_id, ranges=ranges.get(_canonical(md.name), []))
                            + "\n\n" + wrap_links(footer, page_id=page_id),
                })
```

Always emit the links block, even when `footer == ""`, so a footer that disappears is
emptied remotely by `merge_marked_sections`. `growi` must not depend on `linker`, so the two
marker strings are defined once in `graph/common/markdown.py` (`LINKS_FOOTER_START`,
`LINKS_FOOTER_END`) and imported by both `growi/publisher.py` and `linker/render.py`.

`graph/growi.py::source_ranges` keeps working: the links marker carries no `lines a-b`.

### 9.2 Publish touched documents (D-8) — `workspace/git_sync.py`

In `sync_raw`, `write_wiki` returns `WriteResult(target: Path, touched: list[str])`
(touched = raw rel paths of documents whose pages changed because of reciprocal edges).
After the change loop, before `write_index`:

```python
    touched = sorted({rel for result in results for rel in result.touched} - {c.rel for c in changes})
    for rel in touched:
        if legacy_librarian:
            publisher.ingest_md_output(project.wiki_dir(rel), stop_check=stop_check,
                                       raw_source_path=project.raw_file(rel))
        else:
            emit(stage="publish", file=rel, reason="touched-by-linker")
            publisher.publish_document(project, rel)
        done.append({"file": rel, "status": "L", "pages": ...})
```

On deletion (`change.status == "D"`), call `linker.remove_document(project, change.rel)`
**before** the two `rmtree` calls and add its touched documents to the same set.

Downstream: the content-hash sweep (§12.6) already republishes anything that changed;
`touched` is informational there.

### 9.3 Human edits

`merge_marked_sections` replaces the whole generated body block on republish (existing
contract). Footer-only changes now touch only the `-links` block. Inline entity links live in
the body block, so a neo-mode run that adds a new inline link to an old page republishes that
body block. Document this in RUNNING.md; do not add a merge system.

---

## 10. Knowledge engine changes (`knowledge/librarian.py`, WP-10)

1. Delete `_build_semantic_edges`, `_request_edges_for_group`, `_invalidate_prior_edges`,
   `_link_entity_duplicates`, `_bridge_candidate_ids`, `_generate_bridge_probe`,
   `_bridge_probe_context`, the `bridge` vector channel in `_store_vectors`, and the
   `EDGE_PROMPT` / `BRIDGE_PROBE_PROMPT` / `ENTITY_MATCH` imports. Keep `_knn_candidates`
   and `_rrf_fuse` only if `researcher.py` or `neighborhood.py` still call them (check with
   `rg`); otherwise delete them too. Keep `entity_dedup` setting parsing but make it a no-op
   with a deprecation log line; remove it from the frontend form.
2. `_link_node` becomes: `if self.settings.engine_semantic_edges: raise RuntimeError("engine
   semantic edges were removed; links come from the wiki linker")` else return `[]`. (The
   flag exists only so an operator gets an explicit error instead of silent nothing.)
3. In `sync_growi.revise` and `ingest_md_output`, after nodes are built and before
   `_replace_structural_edges`, add footer edges:

```python
    def _footer_edges(self, nodes: list[Node]) -> list[Edge]:
        from graph.linker.render import parse_footer       # pure, no catalog
        by_path = {node.source_path: node for node in nodes if node.source_path}
        edges: list[Edge] = []
        for node in nodes:
            for link in parse_footer(node.body):
                peer_path = posixpath.normpath(posixpath.join(posixpath.dirname(node.source_path or ""), link.peer_path))
                peer = by_path.get(peer_path) or self.store.get_node_by_source_path(peer_path)
                if peer is None or peer.status != NodeStatus.active:
                    continue
                src, dst = (node, peer) if not link.reverse else (peer, node)
                for a, b in ((src, dst), (dst, src)):
                    edges.append(Edge(id=make_edge_id(a.id, b.id, link.label), source_node_id=a.id,
                                      target_node_id=b.id, label=link.label, summary=link.summary,
                                      valid_at=now_iso(), source_episode_ids=[node.id, peer.id]))
        return edges
```

   `get_node_by_source_path` is a new one-line `GraphStore` query (`SELECT … WHERE
   source_path = ? AND status = 'active' LIMIT 1`). Footer edges are appended to the
   structural edge list passed to `_replace_structural_edges`, so a republish replaces them
   together with the chain edges. For GROWI pages the peer path resolution uses GROWI paths
   (`growi_path` strips `.md`): strip a trailing `.md` from `link.peer_path` before joining
   when `node.source_path` has no `.md` suffix.
4. `_fill_derived_fields` / `_fill_cheap_fields` are unchanged in this plan (search needs
   summary/keywords/claims). A later, separate change may read them from
   `_planning/chunks.json` when the engine runs beside the data root.
5. Tests: `tests/test_ingestion_concurrency.py` and `tests/test_growi_sync.py` gain one test
   each proving that ingesting a page with a footer creates labelled edges both ways and
   that no `EDGE_PROMPT` call happens.

---

## 11. Work packages — current repository

Run tests with `unittest` (no pytest):

```bash
cd llm-wiki-dist
.venv/bin/python -m unittest tests.test_writers tests.test_wiki_chunking tests.test_wiki_page tests.test_wiki_incremental
for f in tests/test_*.py; do .venv/bin/python -m unittest "tests.$(basename ${f%.py})" || echo "FAILED $f"; done
```

Commit after every work package: `neo-org WP-N: <goal line>`.

### WP-0 — Baseline

1. `git status` clean, on branch `neo-hardcoded`.
2. Run the whole suite; record failures (expected none besides linker tests that will be
   deleted).
3. Generate one small fixture through `wiki_one.py` with the fake model used by
   `tests/test_writers.py`; save SHA-256 of every file under its `wiki/<doc>/` and
   `_planning/` to `tests/fixtures/baseline-hashes.json`. These must not change through
   WP-2..WP-7.

### WP-1 — Delete what is gone (D-1, D-5)

Files: deletions plus the minimum edits that keep imports valid.

1. `git rm graph/wiki/linker.py tests/test_wiki_linker.py docs/LINKER.md`.
2. `git rm graph/pages.py tests/test_pages_shelf.py tests/test_pages_router.py
   tests/test_pages_assemble.py tests/test_pages_stitch.py tests/test_pages_wire.py
   tests/test_page_settings.py`.
3. `graph/wiki/config.py`: delete the four `LINKER_*_PROMPT_VERSION` lines and their comment.
4. `graph/wiki/wire.py`: delete everything from the "Cross-document linker contracts" banner
   to the end of the file.
5. `graph/wiki/prompts.py`: delete `map_link_scan_prompt`, `bridge_probe_prompt`,
   `deep_link_research_prompt`, `link_pair_judge_prompt`, `oversized_section_note_prompt`
   and any import that only they used.
6. `graph/writers.py`: delete `run_wiki_linker`; in `write_wiki` replace the `if mode ==
   "wiki": run_wiki_linker(...)` block with writing `_planning/linker.json`
   `{"schema_version": 2, "status": "disabled"}` (temporary until WP-8); delete the
   `mode == "pages"` branch in `build_wiki_output`.
7. `graph/core.py`: change `ingest_mode` literal to `["chunks", "wiki"]`; delete the
   `page_*` fields and their `from_env` lines; delete `wiki_linker_map_concurrency`,
   `wiki_linker_research_concurrency` and their env lines.
8. `graph/librarian.py`: delete the `pages` branch around line 3912 and the
   `ingest_mode == "pages"` dispatch in `chunk_and_ingest`; delete
   `from .pages import …` if present.
9. `app.py` / frontend `SettingsView.jsx`: remove the `pages` option and `page_*` controls.
10. `docs/DEV.md` §15 and `docs/RUNNING.md` §20: replace with one line "cross-document
    linker: see ORG_AND_PORT.md §8; being rebuilt".
11. `rg -n "linker|LINKER|pages_pipeline|page_min_chunks" graph app.py wiki_one.py tests`
    must show only this document's references and the temporary `disabled` marker.

**Verify:** whole suite green; `wiki_one.py` runs end to end and writes a `disabled` marker;
baseline hashes unchanged for `wiki/<doc>/*.md`.

### WP-2 — Extract shared primitives (moves only)

1. `graph/common/markdown.py`: move from `graph/chunk.py` — `MarkdownFenceInfo`,
   `MarkdownFenceScan`, `parse_markdown_fence_marker`, `scan_markdown_fences`,
   `MARKDOWN_FENCE_RE`, `is_fence_line`, `is_tableish_line`; move from `graph/core.py` —
   `strip_image_media`, `strip_big_tables`, `chunk_text`, `_IMAGE_UNIT_RE`,
   `_IMAGE_DESCRIPTION_RE`, `_IMAGE_MEDIA_RE`, `_BIG_TABLE_RE`. Add
   `LINKS_FOOTER_START = "<!-- llm-wiki-links:start -->"`, `LINKS_FOOTER_END = "<!--
   llm-wiki-links:end -->"`.
2. `graph/common/hashing.py`: move `short_hash` and `source_hash` only. `make_node_id` and
   `make_edge_id` are engine concepts and stay in `knowledge/core.py`.
3. `graph/common/async_tools.py`: move `_run_async_blocking` as `run_async_blocking`; keep
   `_run_async_blocking = run_async_blocking` in `graph/chunk.py` for the transition.
4. `graph/clients/chat.py`: move `make_llm`, `structured_ainvoke`, `extract_json_from_text`
   and only the helpers they call.
5. `graph/clients/embeddings.py`: move `Embedder` (and `_ChunkableEmbeddings` or whatever
   private helpers it uses) from `graph/gateway.py`. `graph/clients/reranker.py`: move
   `Reranker`.
6. Update `graph/wiki/model.py`, `graph/wiki/markdown_blocks.py`, `graph/formats/tree.py`,
   `graph/librarian.py`, `graph/chunk.py`, `graph/gateway.py` imports. Leave re-exports in
   `graph/chunk.py` and `graph/core.py` and `graph/gateway.py`.
7. `tests/test_common.py`: characterization tests written **before** the move against the
   old paths, unchanged after: fence scan with an unclosed fence, `is_tableish_line`,
   `strip_image_media` keeps descriptions, `strip_big_tables` threshold, `chunk_text`
   overlap clamp, `short_hash("abc")` value, `run_async_blocking` inside and outside a
   running loop.

**Verify:** `rg "from .chunk import|from .core import" graph/wiki graph/formats` → no hits;
baseline hashes unchanged.

### WP-3 — Settings and config

1. `graph/config.py`: move `Settings` and `from_env` verbatim; apply the field changes of
   §8.13 (this is the one behaviour change in this WP; it has its own test).
2. `graph/core.py`: `from .config import Settings` re-export.
3. `tests/test_config.py`: every env name and default in a table; `WIKI_LINKER_MODE=neo`
   parses; `WIKI_INGEST_MODE=pages` raises.
4. `wiki_one.py`, `app.py`: import `Settings` from `graph.config`.

**Verify:** importing `graph.config` does not import `langchain`, `torch`, `fastapi`.

### WP-4 — Workspace package

1. `git mv graph/project.py graph/workspace/project.py`.
2. Extract `parse_document` (HTTP call, headers, timeout, response validation) from
   `graph/convert.py` into `graph/workspace/parser_client.py`; `git mv graph/convert.py
   graph/workspace/convert.py`.
3. `git mv graph/sync.py graph/workspace/git_sync.py`.
4. `git mv graph/writers.py graph/workspace/writer.py`.
5. Re-export shims: `graph/project.py`, `graph/convert.py`, `graph/sync.py`,
   `graph/writers.py` (imports + `__all__` only).
6. Update imports in `app.py`, `wiki_one.py`, `graph/librarian.py`, `graph/cli.py`, tests.

**Verify:** `tests.test_project tests.test_convert tests.test_sync tests.test_writers`
green; baseline hashes unchanged.

### WP-5 — GROWI package

One commit per extraction: `client.py` (`GrowiAPIError`, `GrowiPage`, `GrowiClient`),
`paths.py` (`growi_segment`, `growi_path`, `team_of_path`, `assert_publish_path`,
`source_ranges`, `_LINES_RE`, `_GROWI_BAD`), `publisher.py` (`wrap_page`,
`_marked_sections`, `merge_marked_sections`, `publish_pages`, `_canonical`,
`_coverage_ranges`, `GrowiPublisher`), `reverse_sync.py` (`_sync_growi_pages_legacy`,
`_parent`, `sync_growi_pages`, `_maybe_await`, `registry_page`), `git mv graph/registry.py
graph/growi/registry.py`, then `graph/growi/__init__.py` re-exporting every name
`rg "from .growi import|from graph.growi import"` finds.

**Verify:** `tests.test_growi_client tests.test_growi_publish tests.test_growi_sync
tests.test_growi_routing tests.test_registry tests.test_admin_connections` green.

### WP-6 — Knowledge package

`git mv` `librarian.py researcher.py realtime.py store.py vectors.py vocab.py
neighborhood.py` to `graph/knowledge/`; move the remainder of `graph/core.py` (models,
prompts, text helpers) to `graph/knowledge/core.py` and the remainder of `graph/gateway.py`
(`LlmClient`, `ModelGateway`) to `graph/knowledge/gateway.py`. Shims at the old paths.
Update `app.py`, `mcp_server.py`, `graph/cli.py`, tests.

**Verify:** `tests.test_vectors tests.test_neighborhood tests.test_vocab
tests.test_realtime tests.test_ask_realtime tests.test_ingestion_concurrency` green.

### WP-7 — Wiki ownership and legacy chunk writer

1. `git mv graph/chunk.py graph/wiki/legacy.py`. Delete the module-level env constants
   `SOURCE_PATH`, `OUTPUT_ROOT`, `PHASE`, `BASE_URL`, `API_KEY`, `GEN_MODEL`,
   `VERIFY_MODEL` and the `if llm is None: llm = make_llm(...)` fallback in
   `arun_chunk_pipeline`: the writer always passes `llm`. Keep everything else. Keep the
   public names `run_chunk_pipeline`, `arun_chunk_pipeline`.
2. `graph/workspace/writer.py::build_wiki_output`: `mode == "chunks"` imports
   `run_chunk_pipeline` from `graph.wiki.legacy`; `llm` for chunk mode is
   `make_llm(model=settings.chat_model, base_url=settings.chat_base_url,
   api_key=settings.chat_api_key, temperature=0.7, timeout=300)` built in the writer when
   the caller passed a `ModelPort` (chunk mode wants the raw langchain LLM; `ModelPort`
   exposes it as `.llm`).
3. Chunk-mode output must satisfy the same folder contract: `docs/*.md` + `_planning/
   {manifest,coverage,metadata}.json` — it already does. `publish_output` copies both.
4. `graph/chunk.py` shim re-exporting `run_chunk_pipeline`, `_run_async_blocking`,
   `make_llm` for one transition.
5. `tests/test_writers.py`: add `test_chunks_mode_publishes_flat_pages_and_planning`. Patch
   `graph.wiki.legacy.split_window_until_valid` and `enrich_concept_plan` the way
   `tests/test_ingestion_concurrency.py` patches `chunk.split_window_until_valid`, so no
   model is needed; assert `wiki/<doc>/*.md` and `_planning/{manifest,coverage,metadata}.json`
   exist and `publish_output` copied them flat.
6. `tests/test_ingestion_concurrency.py`: change `from graph import chunk` to
   `from graph.wiki import legacy as chunk`; nothing else.

**Verify:** `wiki_one.py` with `WIKI_INGEST_MODE=chunks` produces `wiki/<doc>/NNN-*.md` and
`_planning/`; `WIKI_INGEST_MODE=wiki` output hashes match baseline.

### WP-8 — Linker package, legacy mode, writer hook, publication

Order inside the package (one commit each, tests first):

1. `linker/wire.py` + `linker/prompts.py`: §8.4, §8.8 (move `EDGE_PROMPT`,
   `EdgeSuggestion(s)` from `knowledge/core.py`; re-import there). Test: schema bounds,
   prompt contains the four legacy prompt texts verbatim, versions.
2. `linker/chunks.py`: `split_page`, `model_text`, ids, `describe`, `validate_meta`,
   `snapshot_originals`, `chunks.json` read/write. Tests: H2 split with fenced `## ` inside
   code, page without H2, intro chunk, nav footer stripped from model text, entity not in
   text dropped, behaviour with unknown subject dropped, cache hit by text hash makes zero
   model calls.
3. `linker/catalog.py`: schema, `open`, `locked`, `sync_from_planning`, `reconcile`,
   `upsert_chunks`, `fts_search`, `vec_search`, `embed_pending`, `entity_chunks`,
   `chunks_with_entity`, `edge_peers`, `page_of`, `insert_edges`, `edges_for_page`,
   `edge_decision_get/put`, `write_links_json`, `delete_document`. Tests: second sync is a
   no-op, deleting the file and re-syncing reproduces rows and edges from `_planning/`,
   dimension change drops only vec tables, team filter, page exclusion, cascade delete
   returns peers.
4. `linker/legacy.py`: §8.7 candidates + §8.8 filter. Tests with a fake embedder (fixed
   vectors) and fake model: five channels fused, top 16, bridge by entity, bridge by 2-hop,
   same-page excluded, group size 4, unknown id rejected, decision cache prevents a second
   call, one failed group does not stop others.
5. `linker/render.py`: §8.10. Tests: footer on both endpoints, reverse arrow on the peer,
   ordering, cap, relative paths across nested folders, byte-idempotent, `parse_footer`
   inverse, no footer when no edges, original bytes untouched above the footer.
6. `linker/__init__.py`: `link_document`, `remove_document`, `LinkResult`,
   `LinkerCancelled`, `LinkerModeMismatch`. `linker/__main__.py`: `rebuild`, `relink`,
   `status`.
7. `workspace/writer.py`: replace the temporary marker with the hook:

```python
def write_wiki(project, rel, *, mode, settings, llm, embedder, on_progress=None, stop_check=None) -> WriteResult:
    ...
        target = project.wiki_dir(rel)
        publish_output(result.out_dir, target)
        touched = run_linker(project, rel, settings=settings, llm=llm, embedder=embedder,
                             on_progress=on_progress, stop_check=stop_check)
        write_source_stamp(target, project.raw_file(rel), rel)
        return WriteResult(target=target, touched=touched)
```

```python
def run_linker(project, rel, *, settings, llm, embedder, on_progress, stop_check) -> list[str]:
    from graph.linker import link_document
    from graph.wiki.storage import write_json_atomic
    marker = project.wiki_dir(rel) / "_planning" / "linker.json"
    if not getattr(settings, "wiki_linker_enabled", True):
        write_json_atomic(marker, {"schema_version": 2, "status": "disabled"})
        return []
    model = llm if hasattr(llm, "structured") and hasattr(llm, "text") \
        else ChatModelPort(wiki_config(settings, run_dir=project.state_dir(rel)), llm=llm)
    if embedder is None:
        try:
            from graph.clients.embeddings import Embedder
            embedder = Embedder(settings)
        except Exception as exc:
            embedder = None
            if on_progress: on_progress({"stage": "linker", "step": "embedder_unavailable", "error": str(exc)[:200]})
    result = run_async_blocking(link_document(project, rel, model=model, embedder=embedder,
                                              settings=settings, on_progress=on_progress, stop_check=stop_check))
    return result.touched_documents
```

   `up_to_date` unchanged except it reads `schema_version` 2 markers (`disabled` and
   `complete` are up to date; `pending`/`failed` are not; no marker = legacy output, up to
   date). `write_wiki` now returns `WriteResult` instead of `Path`; update its three callers
   (`workspace/git_sync.py`, `wiki_one.py`, `tests/test_writers.py`) in the same commit.
8. `workspace/git_sync.py`: §9.2. `growi/publisher.py`: §9.1. Tests: `test_sync` gains
   "touched document is published"; `test_growi_publish` gains "footer becomes its own
   block", "empty footer emits an empty links block", "body block unchanged when only the
   footer changes".
9. `tests/test_linker_end_to_end.py` with fake model + fake embedder:
   - document A alone → zero edge calls, `chunks.json` written, marker complete;
   - document B → edges A↔B, footer on both, `links.json` in both, `touched == [A]`;
   - rerun B unchanged → zero model calls, zero byte changes;
   - regenerate B with one changed section → only that chunk's meta call, A's footer
     updated, stale edge removed;
   - `remove_document(B)` → A's footer gone, catalog has no B rows;
   - delete `wiki-linker.sqlite`, rerun A → rows and edges rebuilt from `_planning/`,
     only embedding calls happen;
   - `WIKI_LINKER_MODE=neo` against a `legacy` catalog → `LinkerModeMismatch`;
   - cancellation mid-meta → marker `failed`, `up_to_date` false, rerun completes.

**Verify:** all of the above; `wiki_one.py` on two real small documents produces reciprocal
footers (record the run in `docs/RUNNING.md` §20).

### WP-9 — Neo mode

1. `linker/neo.py`: §8.9 (a), (b), (c), prompt, validation, `inline_targets`.
2. `render.py`: apply `link_titles` in neo mode.
3. Tests (`tests/test_linker_neo.py`, fake model/embedder):
   - unique definer → programmatic `defines`/`uses` edge, zero model calls, inline link at
     first plain mention only, not inside a fence or heading;
   - two definers → one model call, at most one accepted;
   - definer with 20 users → 12 edges;
   - similarity group present only when vectors exist;
   - hop2 candidate reached through `via=[e1, e2]`, not in the RRF top-16, reaches the
     model with its path; obvious candidate excluded;
   - label outside the literal rejected;
   - footer groups ordered define/use → similar → hops;
   - the deterministic corpus: A (uses `X`), B (defines `X`), C (shares vocabulary only),
     D (B's entity interacts with D's entity): A→B inline + footer, C never linked, D reached
     via hop and accepted by the fake model.
4. `python -m graph.linker rebuild --mode neo` on the fixture data root converts a legacy
   catalog.

### WP-10 — Knowledge engine parses links (§10)

Delete the semantic-edge code paths, add `_footer_edges`, `get_node_by_source_path`, the
`engine_semantic_edges` guard, tests. `rg "EDGE_PROMPT|BRIDGE_PROBE|_link_entity_duplicates"
graph/knowledge` → no hits.

### WP-11 — Adapters, shims, docs

1. Delete every transition shim (`graph/project.py`, `graph/convert.py`, `graph/sync.py`,
   `graph/writers.py`, `graph/chunk.py`, `graph/core.py`, `graph/gateway.py`,
   `graph/growi.py`, `graph/registry.py`, `graph/librarian.py` …) after `rg` shows no
   internal importer.
2. `wiki_one.py`: imports `graph.config`, `graph.workspace.project`, `graph.workspace.writer`,
   `graph.formats`, `graph.wiki.model` only; prints `touched` documents.
3. `docs/DEV.md`: rewrite the file map for the new tree; §15 becomes "Linker" pointing to
   this document's §7–§9. `docs/RUNNING.md`: env table (§8.13), `python -m graph.linker`
   commands, the human-edit note (§9.3), footer example.
4. Frontend `SettingsView.jsx`: `WIKI_LINKER_MODE` select, remove `entity_dedup`.

### WP-12 — Organization acceptance gate

1. Whole suite green.
2. `tests/test_boundaries.py` green.
3. Baseline hashes for wiki-mode page bodies **above the footer** unchanged.
4. `wiki_one.py` on three real documents (A, unrelated C, B related to A) in `legacy` mode:
   reciprocal footers, rerun is a no-op, `rebuild --mode neo` converts and produces inline
   links; record calls and wall time in `docs/RUNNING.md`.
5. App startup builds the engine; `sync_growi` ingests footer edges; no `EDGE_PROMPT`
   call in logs.
6. `git log --stat` shows moves as renames.

---

## 12. Downstream port — `llm-wiki-air/wiki-publisher`

Create only after WP-12 passes.

### 12.1 Structure

```text
/home/seigyo/c_repo/bhavneek/llm-wiki-air/
  doc-parser/                       existing separate service
  wiki-publisher/
    README.md  pyproject.toml  uv.lock  .env.example
    graph/                          copied folders, byte-identical
      __init__.py  config.py
      common/  clients/  formats/  wiki/  linker/
      workspace/   __init__.py  project.py  parser_client.py  convert.py  writer.py
      growi/       __init__.py  client.py  paths.py  publisher.py
    publisher/
      __init__.py  cli.py  ledger.py  scanner.py  pipeline.py
    tools/sync_upstream.py
    tests/
      test_boundaries.py  test_ledger.py  test_scanner.py  test_parser_client.py
      test_pipeline.py  test_publish.py  test_sync_parity.py
      test_wiki_*.py  test_linker_*.py  test_common.py  test_config.py   (copied)
    data/                           runtime, ignored by Git
```

Keep the `graph` namespace; renaming it buys nothing.

### 12.2 Port allowlist (copy whole folders/files, no edits)

`graph/config.py`, `graph/common/`, `graph/clients/chat.py`, `graph/clients/embeddings.py`,
`graph/formats/`, `graph/wiki/` (including `legacy.py`), `graph/linker/`,
`graph/workspace/{project,parser_client,convert,writer}.py`,
`graph/growi/{client,paths,publisher}.py`, `graph/growi/__init__.py` trimmed to those
names.

Do not copy: `graph/knowledge/`, `graph/workspace/git_sync.py`, `graph/growi/reverse_sync.py`,
`graph/growi/registry.py`, `graph/clients/reranker.py`, `graph/cli.py`, `app.py`,
`mcp_server.py`, frontend.

`config.py` is copied whole; unused engine fields are harmless and keep the file
byte-identical for the sync tool. `.env.example` lists only the variables the publisher
reads (§12.3).

### 12.3 Settings the publisher reads

`WIKI_DATA_ROOT`, `WIKI_PARSER_BASE_URL`, `WIKI_PARSER_TIMEOUT`, `WIKI_CHAT_BASE_URL`,
`WIKI_CHAT_API_KEY`, `WIKI_CHAT_MODEL`, `WIKI_INGEST_MODE`, `WIKI_OUTPUT_LANGUAGE`,
`WIKI_SECTION_TARGET_LINES`, `WIKI_WRITE_ATTEMPTS`, `WIKI_REWRITE_CONCURRENCY`, the
`structure_*`/`slide_*`/`pdf_*`/`tabular_*` variables, `WIKI_LINKER_ENABLED`,
`WIKI_LINKER_MODE`, `WIKI_LINKER_CONCURRENCY`, `WIKI_EMBED_BASE_URL`, `WIKI_EMBED_MODEL`,
`WIKI_EMBED_DIM`, `GROWI_URL`, `GROWI_TOKEN`, `GROWI_WRITE_PATH`, `GROWI_ROOT_PATH`,
`GROWI_MODE`, `GROWI_TIMEOUT`, `PUBLISHER_INTERVAL_SECONDS`. Use the exact names
`Settings.from_env` already reads for the shared ones; add the `GROWI_*`/`PUBLISHER_*`
ones in `publisher/cli.py`.

### 12.4 Data contract

§7.1 without `raw/.git`, `metadata/last_sha`, `graph.sqlite`, `engine.sqlite`; plus
`metadata/pipeline.json` and `metadata/pipeline.lock`.

`pipeline.json` schema 1:

```json
{
  "schema_version": 1,
  "sources": {
    "team/path/manual.docx": {
      "source_sha256": "…", "size": 123, "mtime_ns": 1,
      "raw_rel": "team/path/manual_docx.md", "wiki_rel": "team/path/manual.docx",
      "parser": "docx", "completed_at": "…", "last_error": ""
    }
  },
  "published_documents": {
    "team/path/manual.docx": {"content_sha256": "…", "growi_path": "/root/team/path/manual.docx", "published_at": "…"}
  }
}
```

Rules: validate `schema_version`; missing file = empty ledger; write temp + fsync +
`replace`; never write `source_sha256` before generation **and** linking complete; never
write a publication hash before the whole document publishes; corrupt JSON stops the run
and keeps the file.

`document content_sha256` = SHA-256 of canonical JSON of sorted `(page filename, page bytes
SHA-256)` over `*.md` in the document folder root. `_planning/` excluded; the linker footer
included, so link changes trigger publication.

### 12.5 Scanner (no Git)

Recursive regular files under `mount/`; ignore `.DS_Store`, `Thumbs.db`, `desktop.ini`,
names starting `~$`, hidden VCS folders, symlinks resolving outside `mount/`. Supported =
parser extensions + `.md` passthrough. `added = C − L`, `changed = sha differs`,
`deleted = L − C`, each sorted. Hash every file every scan (size/mtime prefilter only when
measured necessary). One global `fcntl` lock; a second process exits "already running".

### 12.6 `sync_once`

```text
acquire global lock; load ledger; scan; plan added/changed/deleted

for each deleted path (sorted):
    touched = graph.linker.remove_document(project, raw_rel)      # peers re-rendered
    trash owned remote document subtree
    rmtree wiki/<doc>, metadata/state/<doc>, raw file
    drop ledger source row (atomic write)

for each added or changed path (sorted):
    parse (or copy .md) -> temp -> validate -> replace raw atomically
    workspace.writer.write_wiki(project, raw_rel, mode=settings.ingest_mode, ...)   # includes linker
    write ledger source row with the new source_sha256 (atomic)

sweep: for every wiki/<team>/<doc>/ folder:
    h = content_sha256(folder)
    if published_documents[doc].content_sha256 != h:
        growi.publisher.GrowiPublisher.publish_document(project, raw_rel)
        write ledger publication row (atomic)
for every published_documents row absent locally:
    trash owned remote subtree; drop row

write run summary; release lock
```

Per-source isolation: a parser/model failure for one source records `last_error` and
continues; a linker catalog failure stops before the sweep. Cancellation is checked between
documents. Changed sources use the whole-document invalidation
(`# ponytail: no-Git port invalidates the whole changed document; add a Markdown diff only
when regeneration cost is measured as unacceptable.`).

### 12.7 GROWI

One-way (`local -> GROWI`). Ownership marker = the existing `<!-- chunk: … -->` wrapper
(page id + hash) plus the `-links` block. Deletion enumerates beneath the configured
document path and trashes only marked pages. Preserve the `.md` suffix in GROWI paths so the
generator's relative links resolve — verify with a two-page integration test first; if
GROWI rewrites such paths, add a publish-boundary link translation with tests and do not
touch stored Markdown. Add/change/delete = replace; partial failure leaves the publication
row unchanged and the whole document retries next run.

### 12.8 Runner

```text
python -m publisher.cli sync-once            # one reconciliation, non-zero on partial/failed
python -m publisher.cli run --interval 60    # loop; SIGTERM/SIGINT between documents
python -m graph.linker rebuild --mode neo    # same command as upstream
```

No FastAPI. One log line per stage/document: run id, path, stage, elapsed, counts,
exception class/message. Never tokens, prompts, pages, base64.

### 12.9 Keeping both repositories in sync

`tools/sync_upstream.py --source <upstream>/llm-wiki-dist --check|--apply`: explicit
allowlist (§12.2), byte comparison, copies only allowlisted paths, records the upstream
commit SHA in `tools/upstream.json`, never deletes downstream-only files. No submodules, no
shared package yet.

### 12.10 Downstream work packages

| WP | Goal | Acceptance |
|---|---|---|
| P0 | skeleton + `.gitignore` | package imports with empty `data/` |
| P1 | copy allowlist + upstream tests | copied `test_wiki_*`, `test_linker_*`, `test_common`, `test_config` green unchanged |
| P2 | ledger + scanner | unchanged scan is a no-op; rename = delete + add; corrupt ledger stops; lock contention exits |
| P3 | parser client integration | 415/5xx/timeout/invalid JSON/heartbeat/source race covered; last good raw kept |
| P4 | `write_wiki` call site (both ingest modes) | same local layout as upstream `wiki_one.py` |
| P5 | linker exercised | two-document run yields reciprocal footers; `remove_document` cleans the peer; no `knowledge` import |
| P6 | one-way GROWI replacement | only owned pages touched; `.md` path test; 409 retry; partial failure dirty |
| P7 | publication sweep | X new, Y touched → both published; crash after Y publishes retries only X |
| P8 | deletion end to end | A removed → A remote trashed, B footer cleaned, B republished, human pages survive |
| P9 | CLI + loop | exit codes; no overlapping runs |
| P10 | docs + deployment example | env table, health checks, backup/restore, one-way warning |

---

## 13. Test matrix (new and changed tests only)

- `test_boundaries.py`: factory packages import no `knowledge`/`app`/`torch`.
- `test_common.py`: §WP-2 characterizations.
- `test_config.py`: env names/defaults; mode literals.
- `test_writers.py`: chunks mode publishes; wiki mode unchanged; `run_linker` disabled writes
  marker and constructs no embedder; `up_to_date` marker semantics.
- `test_linker_chunks.py`, `test_linker_catalog.py`, `test_linker_legacy.py`,
  `test_linker_render.py`, `test_linker_neo.py`, `test_linker_end_to_end.py`: §WP-8/9.
- `test_sync.py`: touched documents published; deletion calls `remove_document` first.
- `test_growi_publish.py`: links block; empty links block; body untouched by footer change.
- `test_ingestion_concurrency.py` / `test_growi_sync.py`: footer edges parsed; no
  `EDGE_PROMPT`.

Normalize only timestamps and run ids in parity comparisons; never page bytes.

---

## 14. Manual acceptance

1. Start doc-parser; `GET /health`.
2. Configure chat + embedding endpoints (reranker not needed).
3. `mount/team-smoke/A.docx` → `sync-once` (downstream) or `wiki_one.py` (upstream):
   `_planning/pages/`, `chunks.json`, `linker.json complete`, no footer (first document).
4. Add `B.docx` that uses a term `A` defines → run: A and B both have a footer, B's footer
   entry points at A's page; in neo mode B's first mention is an inline link.
5. Add lexical distractor `C.docx` → run: in legacy mode it may get `similar`-style edges
   only if the model accepts them; in neo mode it appears at most under 類似.
6. Rerun unchanged → zero model calls, zero writes, zero GROWI writes.
7. Change one section of B → only that chunk's meta call; A's footer updated if the edge
   moved; B republished; A republished only if its bytes changed.
8. Remove B → A's footer entry gone; A republished; B's remote pages trashed.
9. `python -m graph.linker rebuild --mode neo` → catalog rebuilt, inline links appear.
10. Stop GROWI, change A, run → local complete, publication dirty, non-zero exit; restore,
    rerun → only the dirty document is published.

---

## 15. Rollback

- Organization: revert the failing move commit; shims keep entry points alive.
- Linker: `WIKI_LINKER_ENABLED=0` writes `disabled` markers and pages stay pristine;
  deleting `wiki-linker.sqlite` and every `_planning/links.json` and rendering from
  `_planning/pages/` restores link-free pages (`python -m graph.linker rebuild` with
  `--no-edges` does exactly that).
- Downstream: stop the service; the full product is untouched.
- Data: raw from mount via parser; wiki from raw + state; links from `_planning/`; catalog
  from `_planning/`; ledger from a rescan (never auto-delete unknown remote pages).

---

## 16. Definition of done

Current repository:

- [ ] `graph/wiki/linker.py`, `pages.py`, `LINKER.md` gone; no reference remains.
- [ ] tree matches §6; boundary test green; moves are renames.
- [ ] wiki-mode pages above the footer byte-identical to baseline.
- [ ] chunks ingest mode works through the writer.
- [ ] `legacy` linker mode: two-document real run yields reciprocal footers; unchanged rerun
      is free; regeneration and removal clean peers; catalog rebuild from `_planning/`.
- [ ] `neo` mode: define/use inline + footer, similar group, hop candidates, closed labels.
- [ ] full product publishes touched documents; footer is its own GROWI block.
- [ ] engine parses footer edges; no `EDGE_PROMPT` anywhere in `knowledge/`.
- [ ] DEV.md / RUNNING.md describe the new tree and commands.

Downstream:

- [ ] only the allowlist is copied and byte-identical (`sync_upstream --check` clean).
- [ ] no Git, no `graph.sqlite`, no `knowledge` import.
- [ ] add/change/delete without Git; parser failures retry-safe.
- [ ] linker complete and bilateral; publication sweep republishes touched documents.
- [ ] GROWI add/change/delete touch only owned pages; `.md` links resolve.
- [ ] unchanged runs perform no expensive work; manual acceptance passes.

---

## 17. Final instructions to a weak implementer

1. Do WP-0, then WP-1. Deleting first makes every later move smaller.
2. One work package per commit; never mix a move with a behaviour change.
3. Never edit wiki prompt text, versions or schemas (C1). The only allowed `page.py` edit
   is the `fence_flags` alias.
4. The linker never reads a published page. Originals live in `_planning/pages/`.
5. The linker never calls a model except `ChunkMeta` per chunk and one edge prompt per
   group of four candidates. If you find yourself adding a third kind of call, stop.
6. One mode per data root. A mismatch is an error with the rebuild command in its message.
7. Every edge is rendered on both pages or on neither; write `links.json` for both
   documents.
8. Publish `changed ∪ touched` (full product) or by hash sweep (downstream). Never trust the
   changed source list alone.
9. On deletion, `remove_document` runs before any `rmtree`.
10. Never mark a source or a publication complete before its last step succeeds.
11. If a baseline hash changes unexpectedly, find out why; do not update the fixture.
12. Port whole folders. If a downstream file differs from upstream, upstream is wrong or
    the file is a downstream adapter listed in §12.1 — nothing else.
