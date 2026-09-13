# Plan v2 — GROWI is the wiki, sqlite-vec keeps the vectors, one mount with teams

**Status: hard plan (2026-09-13), written against branch `neo-hardcoded` at `361fdbb`.**
Every file, function and line number below was checked against the working tree. The
whole unit-test suite (28 modules) is green at that commit; that is the baseline.

This replaces the earlier `PLAN_GROWI.md`. Its Parts 1–2 (chunks are material, pages are
shelves; verbatim by line number; stitch = operations) are **built**: `graph/pages.py`,
`graph/wiki/` (the neo writer, see `PLAN_NEO.md`), `graph/writers.py` (the one switch).
Its Parts 3–5 (GROWI + Qdrant) were half-built as WP-8…WP-14 and are superseded by this
document. `PLAN_SYNC.md` (data folder + git sync) is built and is the starting point here.

Written for an implementer working one work package at a time. Where it gives code,
**type that code**. Where it says delete, delete. If a snippet does not fit the real file
(a name differs, a signature moved), stop and report the exact line — do not patch around it.

---

## What you asked for, in one paragraph

1. **sqlite stops being ground truth.** GROWI holds every page's text and history. sqlite
   keeps only bookkeeping (registry, sync ledger) and the *derived* index (node cache, FTS,
   sqlite-vec vectors, edges, clusters, neighbourhoods) that is rebuilt from GROWI whenever
   it is lost. **Vectors stay in sqlite-vec** (decided 2026-09-13: 6,967 vectors = 7 MB,
   KNN 5–70 ms; the Qdrant adapter built as WP-14 is deleted, the `VectorIndex` seam stays).
2. **One mount, many teams.** The file server is bind-mounted once. Its top-level folders
   are teams/projects. `mount/team-a/x.docx → raw/team-a/x_docx.md → wiki/team-a/x.docx/…
   → GROWI /inbox/team-a/x.docx/…`. The URL segment that used to pick a `.sqlite` now
   picks a **scope**: `/prefix/team-a/` sees one team, `/prefix/all/` sees everything.
3. **The git checker is actually wired.** `raw/` is initialised and committed by the app,
   the diff runs on a timer and at startup, and what it finds flows through to GROWI and
   the index — today it stops at a button.

---

## Part 0 — What exists today (verified)

### 0.1 Module map

| File | What it does now | Key symbols (line) |
|---|---|---|
| `app.py` (2408) | FastAPI transport. One stack per URL segment (`STACKS`), lazy bootstrap, admin `.sqlite` management, GROWI connection endpoints, `/api/sync`, `/api/wiki.zip`. | `DB_DIR`/`DATA_ROOT` 94–99, `STACKS` 127, `_build_stack` 148, `_bootstrap_db` 186, `_close_stack` 226, `lifespan` 254, `_ready_stack` 405, `_db_path` 566, `db_routing` 930, connections 1103–1207, dbs admin 1210–1655, `create_document` 2266, `/api/sync` 2321, `/api/wiki.zip` 2336 |
| `graph/core.py` | `Settings` (env + runtime-patchable), `Node`/`Edge` models. | `Settings` 33, `data_root` 109, `parser_base_url` 110, `vector_backend`/`qdrant_*` 113–116, `from_env` 198, `Node` 347 |
| `graph/store.py` (1839) | `GraphStore`: sqlite schema, nodes/edges/FTS/search-items/neighbourhood, **sqlite-vec tables**. | `_open_connection` 166, `_create_core_tables` 259, `_ensure_node_columns` 391, `upsert_node` 676, `delete_node` 791, `delete_nodes` 868, `keyword_search` 1436, vec methods 452–528 and 1462–1625, `search_items_fts_query` 1774 |
| `graph/librarian.py` (4111) | All writes: job queue, ingest, enrichment, revision flow, clusters. Owns `self.vector_index`. | vector index choice 214–223, `_SNAPSHOT_JOB_TYPES` 431, `_dispatch_job` 526, `bootstrap` 883, `_bootstrap_vectors` 937, `_bootstrap_qdrant_vectors` 1038, `_bootstrap_search_items` 1077, `delete_document` 1241, `ingest_md_output` 1629, `chunk_and_ingest` 1700, `sync_project` 1817, `publish_to_growi` 1850, `sync_growi` 1914, `_revise_document` 2017, `_ingest_one` 2170, `_store_vectors` 2215, `_store_search_items` 2355, `_knn_candidates` 2416, `_supersede` 2870, `_load_new_planning_docs_output` 3888 |
| `graph/researcher.py` (2189) | All reads: hybrid search, `ask`, evidence. **Reads vectors straight from sqlite-vec.** | `Researcher.__init__` 941, `query` 1129, `search_with_evidence` 1183, `store.vector_search` calls 1153 / 1236 / 1247 |
| `graph/vectors.py` (449) | `VectorIndex` seam: `SqliteVecIndex` (default, 31–83) and `QdrantIndex` (opt-in, 85–449, **deleted by WP-G1**). | `SqliteVecIndex` 31 |
| `graph/growi.py` (393) | GROWI REST v3 client, marker merge, publish, incremental sync. | `list_pages` 148, `create_page` 178, `update_page` 186, `assert_publish_path` 200, `merge_marked_sections` 233, `publish_pages` 255, `sync_growi_pages` 290 |
| `graph/registry.py` (263) | `engine.sqlite`: `growi_connections` (Fernet-encrypted token) + `growi_pages` ledger. | `ConnectionRegistry` 39 |
| `graph/project.py` (90) | One project folder: `mount/ raw/ metadata/ wiki/ graph.sqlite`. | `Project`, `wiki_folder_name`, `raw_name_for`, `zip_wiki` |
| `graph/sync.py` (163) | git diff of `raw/` → A/M/D → write wiki → ingest. | `git` 28, `head_sha` 38, `plan_changes` 49, `changed_hunks` 67, `sync_project` 82 |
| `graph/convert.py` (128) | `mount/ → raw/` through doc-parser `POST /parse`, mtime+size ledger, git commit. | `convert_mount` 62 |
| `graph/writers.py` (175) | `build_wiki_output(mode)` → `chunks | pages | wiki`; `write_wiki`, `publish_output`, `write_index`. | `write_wiki` 128 |
| `graph/pages.py`, `graph/chunk.py`, `graph/wiki/*` | The three writers. Only `pages` emits `<!-- chunk: … -->` markers (`pages.py:614`). | |
| `mcp_server.py` | Stateless MCP proxy per `{prefix}/{db}/mcp`. **Allows a db only if `DB_DIR/<db>.sqlite` exists** (254–265). | |
| `frontend/src/api.js`, `data/growi.js`, `UploadView.jsx`, `pages/admin/AdminApp.jsx` | `syncProject`, `wikiZipUrl`; `growiLinkFor` expects `GET /api/growi` (**does not exist**); admin manages `.sqlite` files only. | |
| `growi-stack/docker-compose.yml` | mongo + growi. No Elasticsearch (fine: FTS5 stays ours). | |
| `data/` (your current folder) | `mount/ raw/(git) metadata/ wiki/ graph.sqlite(400 MB) engine.sqlite` — **one project laid out at the root**, mount subfolders `csv docx pdf pptx xlsx`. `engine.sqlite` has one connection `local → http://localhost:3000`, mode `attach`, write_path `/inbox`, 0 pages synced. | |

### 0.2 The two flows today

```
 FLOW A  (built, PLAN_SYNC)                        FLOW B  (half-built, WP-11..14)
 ─────────────────────────                          ───────────────────────────────
 mount/<x>.docx                                     GROWI pages
   │ convert_mount (doc-parser)                       │ sync_growi (only when the URL segment
   ▼                                                  │  is a *registered connection name*)
 raw/<x>_docx.md  (git commit)                        ▼
   │ plan_changes = git diff last_sha..HEAD         nodes with original_document_name="growi:<name>"
   ▼                                                  in a *separate* cache sqlite
 write_wiki → wiki/<x>.docx/NNN-*.md                  (growi-cache/<name>.sqlite)
   │ ingest_md_output
   ▼
 graph.sqlite: nodes + FTS + vec_* + edges + clusters   ← ground truth, served at /prefix/<db>/
   │ publish_to_growi (job exists, NO endpoint calls it)
   ▼
 GROWI (mirror, never read back)
```

Two indexes, two truths, and neither GROWI direction is reachable from the UI.

### 0.3 Audit — what is not wired (the fix list)

Each item is fixed by the work package in the last column.

| # | Finding | Where | WP |
|---|---|---|---|
| F1 | **The Qdrant path is half-wired and broken.** `Researcher` still reads sqlite-vec (`store.vector_search`), so `vector_backend=qdrant` finds nothing for body/summary; `QdrantIndex` uses raw node ids as point ids (Qdrant needs UUIDs) and one id across channels (each upsert overwrites the last); nothing deletes points. Decision: **delete it**, keep sqlite-vec. | `vectors.py:85–449`, `librarian.py:214–223, 1038–1075, 2400–2404` | G1 |
| F2 | **Scoped vector search returns too few hits.** `vector_search` applies `LIMIT k` inside the `matches` CTE *before* joining `nodes`; under a team scope (TEMP view, WP-G6) other teams' hits are discarded after the limit. | `store.py:1557–1624` | G2 |
| F3 | `graph.sqlite` is 400 MB for 132 nodes: `_reindex_fts` indexes 60 MB of inline base64 images (see `PLAN_FORMATS.md` WP-F0, a one-liner). Vectors are 7 MB. | `store.py:1404` | F0 (PLAN_FORMATS) |
| F4 | `sqlite-vec` KNN is brute force: ~70 ms over 7k search-item vectors today; ceiling ≈ 100k vectors before realtime notices. The `VectorIndex` seam stays so a server (Qdrant) or an embedded store (LanceDB) can replace it later without touching callers. | `vectors.py:31` | — (ceiling) |
| F5 | **GROWI listing stops at 100 pages, then deletes the rest.** GROWI's `GET /_api/v3/pages/list` paginates with `page=N` and returns `{pages,totalCount,offset,limit}` (verified in `apps/app/src/server/routes/apiv3/pages/index.js`). The client looks for `nextCursor` → only the first 100 (newest) pages are ever listed, and `sync_growi_pages` treats "absent from listing" as deleted. | `growi.py:148–176, 302–316` | G4 |
| F6 | `updatedAfter` is not a GROWI parameter. Harmless (unused) but misleading. | `growi.py:154` | G4 |
| F7 | **Publish breaks on the second run.** `merge_marked_sections` raises when the body has no `<!-- chunk: … -->` marker; only `pages` mode writes markers, `chunks` and `wiki` (your current data) do not. | `growi.py:236` | G4 |
| F8 | **Publish paths are rejected by GROWI** for titles with `+ # % ? * ^ $ < >` (e.g. `028-MPI+OpenMP…`) and any path ending in `.md`. GROWI's `isCreatablePage` (`packages/core/src/utils/page-path-utils/index.ts`) forbids them. Nothing sanitises. | `librarian.py:1896` | G4 |
| F9 | `publish_to_growi` job has no HTTP trigger; `GET /api/growi` used by `frontend/src/data/growi.js` does not exist. | `app.py` | G4, G11 |
| F10 | **`raw/` git is never initialised by the app.** `Project.ensure()` makes dirs only; `convert_mount` runs `git add/commit` → fails on a fresh project; `git commit` needs `user.name/email` (absent in the container); `head_sha` raises on a repo with no commits. Your `data/raw` was seeded by hand. | `project.py:70`, `convert.py:117`, `sync.py:28–39` | G5 |
| F11 | **Nothing runs the sync.** `POST /api/sync` (the UI button) is the only trigger. No timer, no startup catch-up. Conversion is inert unless `WIKI_PARSER_BASE_URL` is set (`.env` does not set it). | `app.py:2321`, `sync.py:105` | G8 |
| F12 | `sync_project` is in `_SNAPSHOT_JOB_TYPES`: every sync copies the whole `graph.sqlite` (400 MB today) first. | `librarian.py:431` | G3 |
| F13 | **Layout mismatch.** `.env` sets `WIKI_DATA_ROOT=…/data`, so the app expects `data/<db>/mount…`, but `data/` *is* the project. Startup preloads nothing; every segment 404s until an admin creates a db. One project = one mount; teams are not a concept. | `app.py:285–300, 560–571` | G5, G6 |
| F14 | **Two indexes for one wiki.** A registered GROWI gets its own `growi-cache/<name>.sqlite` and `document_name="growi:<name>"` nodes, unrelated to the project's `graph.sqlite`. `sync_growi.index_page` bypasses `_revise_document`, so an edited page loses its edges and agent-note links, and the node id changes on every revision. | `app.py:196–216`, `librarian.py:1938–1958` | G6, G8 |
| F15 | MCP allow-check needs `DB_DIR/<db>.sqlite` → broken for `WIKI_DATA_ROOT` and for GROWI names. | `mcp_server.py:254–265` | G6 |
| F16 | Admin panel (1447 lines) manages `.sqlite` files; no UI for the GROWI connection or for teams. | `AdminApp.jsx` | G10, G11 |
| F17 | `pyproject.toml` carries an unused `qdrant` optional group; `tests/test_qdrant_vectors.py` and `tests/test_page_settings.py` assert the dead backend switch. | | G1 |

Already correct, keep: `WIKI_STARTUP_REQUIRE_ALL_DBS` defaults to `false`; `assert_publish_path`
boundary check; Fernet registry; Bearer auth (GROWI's `extractAccessToken` reads `Authorization:
Bearer` first, then `access_token` — verified); `_revise_document` per-node exact-hash matching;
`invalidate_pages` for `wiki`-mode `M` changes; git `--no-renames` D+A semantics.

---

## Part 1 — Target design (read once, then do not re-argue)

### 1.1 Folder — one root, teams are folders

```
$WIKI_DATA_ROOT/                       (= your data/ today)
  mount/                               ONE read-only bind mount. Top-level dirs = teams.
    team-a/spec.docx  team-b/x/y.pdf   (files at mount root → team "general")
  raw/                                 ONE git repo (app-initialised, app-committed)
    .git/  team-a/spec_docx.md  team-b/x/y_pdf.md  team-a/uploads/notes.md
  metadata/                            never served, never git-tracked
    last_sha  convert.json  state/<team>/<doc>/  work/<team>/<doc>/
  wiki/                                staging + export (Obsidian zip). NOT the index source.
    index.md  team-a/spec.docx/001-….md  team-a/spec.docx/_planning/{coverage,manifest,metadata,source}.json
  graph.sqlite                         the DERIVED index: nodes cache, FTS, edges, clusters, neighbourhoods
  engine.sqlite                        bookkeeping: growi_connections, growi_pages ledger
```

Team = **first path segment under `mount/`** (and therefore under `raw/`, `wiki/`, and the
GROWI write path). No per-team sqlite. No per-team mount.

### 1.2 URL — the segment is a scope

```
{PREFIX}/                 → 307 → {PREFIX}/all/
{PREFIX}/all/…            everything (no filter)
{PREFIX}/team-a/…         only team-a's nodes, edges between team-a nodes, team-a vectors
{PREFIX}/admin/…          admin, as today
{PREFIX}/{scope}/mcp      MCP, same scopes (mcp_server.py)
```

A scope is **a read-only filtered view of one engine**, not a separate database:

- sqlite: a scoped `GraphStore(readonly=True, scope="team-a")` creates three `TEMP VIEW`s
  (`nodes`, `edges`, `search_items`) that shadow the real tables for that connection only.
  Every existing query in `store.py` becomes team-filtered with **zero edits** to those
  queries (verified: temp objects shadow `main` on a `mode=ro` connection; FTS joins,
  `get_node`, edge lookups all filter correctly; writes stay blocked).
- sqlite-vec: `vector_search` already joins `nodes`, so the same TEMP view filters vector
  hits; WP-G2 only makes it over-fetch so a team still gets `k` results.
- Writes never go through a scope. There is **one** `Librarian`, one write queue, one
  write store. Uploads and agent notes made *inside* a team scope are tagged with that
  team; made in `all` they get `team=NULL` and appear only in `all`.

### 1.3 Who owns what

| Thing | Owner | Lost it? |
|---|---|---|
| Page text, titles, paths, history, permissions, editor, attachments | **GROWI** (Mongo) | it is the wiki; back it up like one |
| Node cache (body copy, summary, keywords, claims, team), FTS5, **sqlite-vec vectors** (`vec_body`, `vec_summary`, `vec_bridge`, `vec_search_item`), edges, clusters, neighbourhood cache, job queue | `graph.sqlite` | rebuilt from GROWI + one enrichment pass (`bootstrap` re-embeds) |
| GROWI connection (encrypted token), `growi_pages` ledger (page_id → revision_id, path) | `engine.sqlite` | retype the token; ledger rebuilds on next sync |
| `raw/` (converted markdown, git), `wiki/` (writer output), `metadata/` (writer state, convert ledger, `last_sha`) | files under `WIKI_DATA_ROOT` | `raw/` regenerates from `mount/`; `wiki/` regenerates by the writers (LLM cost) |

FTS5 and sqlite-vec **stay in `graph.sqlite`** as derived indexes: one file under
`WIKI_DATA_ROOT`, copyable, rebuildable, no second service. Your GROWI stack has no
Elasticsearch, and nothing else gives Japanese BM25 for free.

### 1.4 Data flow after this plan

```
                 every WIKI_SYNC_INTERVAL_SECONDS, and once at startup
                 ─────────────────────────────────────────────────────
 mount/<team>/… ──convert_mount──► raw/<team>/…_ext.md ──git commit──┐
                                                                       │ plan_changes = git diff last_sha..HEAD
 POST /api/document (in a team scope) ──► raw/<team>/uploads/x.md ─────┘
                                                                       ▼
                                          A/M: write_wiki (skipped when wiki/…/_planning says the raw sha is unchanged)
                                               publish_document → GROWI /inbox/<team>/<doc>/<NNN-title>   (create / marker-merge / trash)
                                          D:   trash our pages under /inbox/<team>/<doc>/
                                                                       │
                                          then sync_growi is enqueued  ▼
 GROWI (humans edit here too) ──list all pages──► changed docs ──get bodies──► _revise_document per doc folder
                                                                       │
                                                                       ▼
                                          graph.sqlite (nodes.team, FTS, sqlite-vec, edges, clusters)
                                                                       │
                              /prefix/all/  /prefix/<team>/  ◄─────────┘  scoped read stores (TEMP views filter nodes, edges, vectors)
```

**One indexer.** Only `sync_growi` writes wiki pages into the index. `ingest_md_output` from
`wiki/` folders, `chunk_and_ingest`, `create_document` (as an ingest) go away.

### 1.5 Identity rules

| What | Rule |
|---|---|
| Raw document | `team-a/spec_docx.md` (raw-relative path). Unchanged from PLAN_SYNC D6. |
| Wiki folder | `wiki/team-a/spec.docx/` (PLAN_SYNC D7). |
| GROWI page path | `<write_path>/team-a/spec.docx/<NNN-title>` with every segment passed through `growi_segment()` (replace `^ $ * + # < > % ? \` with `-`, strip `.md`). |
| GROWI "document" | the **parent path** of a page, e.g. `/inbox/team-a/spec.docx`. This is `Node.original_document_name`. Human pages outside the write path use the same rule (`/Sandbox/foo` → document `/Sandbox`). |
| Team of a GROWI page | segment right after `write_path` (`/inbox/team-a/…` → `team-a`); a page not under `write_path` → `team=NULL` (visible in `all` only). In `own` mode set `write_path` to the tree you own (e.g. `/wiki`), not `/`. |
| Node id | `make_node_id(body, document)` exactly as `_load_new_planning_docs_output` does today — same body ⇒ same id ⇒ `_revise_document` sees "unchanged". |
| Page marker | first line of every page we publish: `<!-- chunk: page<write_path>/<team>/<doc>/<name> lines <a>-<b> hash:<sha12> -->` (spaces in the id replaced by `_`). `pages` mode already carries per-chunk markers; those are kept and no page-level marker is added. `source_ranges` on the node = every `lines a-b` in the body. |

### 1.6 Decisions

| # | Decision | Why |
|---|---|---|
| D1 | One engine ↔ one GROWI. The registry keeps its multi-row shape, but the engine uses the connection named `WIKI_GROWI_NAME` (default: the only row). Many GROWIs = many llm-wiki stacks. | You have one GROWI. "Many GROWIs behind one engine" from the old plan is exactly what teams-inside-one-GROWI now gives you, without tenant plumbing in every query. |
| D2 | `all` is a reserved scope name; teams are `[A-Za-z0-9_-]+` folder names. A mount folder named `all` is ignored by `Project.teams()` (`RESERVED_TEAMS`). | |
| D3 | GROWI is **required**. No connection registered ⇒ engine stage `needs_growi`, admin works, wiki routes answer 503 with that stage. | You asked for GROWI as the truth. A second "files only" index path is the thing we are deleting. |
| D4 | The write side is one `Librarian`; scopes are read-only. | One queue, one snapshot policy, one vector table set. |
| D5 | Scope filtering = sqlite TEMP views; vectors are filtered by the same views because `vector_search` joins `nodes`. | Zero edits to ~40 read queries. Verified on a read-only connection. |
| D11 | Vectors stay in sqlite-vec behind the `VectorIndex` seam; the Qdrant adapter is deleted. | 7 MB of vectors, 5–70 ms KNN, one file on the mount. Revisit at ~100k vectors (Qdrant if a service is fine, LanceDB if files on a share matter). |
| D6 | Sync cadence: timer + startup + button. Both `sync_raw` and `sync_growi` are write jobs; the timer only enqueues when neither is queued/running. | Serialised by the existing queue; no new locking. |
| D7 | `sync_raw` is **not** snapshotted. Each file is its own unit; `last_sha` only advances when every change succeeded; a re-run is idempotent because the writer is skipped when the raw sha is unchanged. | 400 MB copies per sync (F12). |
| D8 | Deleting from the UI (`/api/document/delete` with a GROWI document path) trashes our pages in GROWI (never completely-deletes) and `git rm`s the raw file if it was an upload; a mount-derived file comes back only when the mount file changes. | GROWI trash is the reviewer's undo. |
| D9 | Agent notes (`exogenous`) stay index-only, tagged with the scope's team. | They are Q&A products, not wiki pages. Publishing them is a later feature. |
| D10 | Legacy `WIKI_DB_DIR` / `.sqlite`-per-wiki mode, sqlite-vec, `vector_backend`, admin upload/copy/rename, `chunk_and_ingest`, `create_document`-as-ingest, `/api/ingest`, `/api/recon`, `/api/cascading-update` are **deleted** (WP-G10). | Deletion over addition; two ingest paths is how F14 happened. |

---

## Part 2 — Hard constraints

- **C1 — Phase order.** WP-G1 → G12 in order. G1–G4 are pure fixes and land on today's
  layout; G5–G9 change the layout and flows; G10 deletes; G11–G12 are UI and ops.
- **C2 — GROWI is never written except through `publish_pages` / `delete_pages`**, and
  `publish_pages` only ever creates pages or replaces *marked* sections. Human text outside
  our markers is never touched. Never write to Mongo.
- **C3 — Writers untouched.** `graph/chunk.py`, `graph/pages.py`, `graph/wiki/*` change by
  zero lines. `graph/realtime.py`, `graph/vocab.py`, `graph/neighborhood.py`,
  `graph/gateway.py`: zero lines.
- **C4 — Never lose a sync.** `last_sha` moves only after every change in the batch is
  published; the `growi_pages` ledger row for a page moves only after its document was
  revised in the index.
- **C5 — Nothing writes into `mount/`.**
- **C6 — Tests green before every commit**, whole suite:

  ```bash
  cd llm-wiki-dist
  for f in tests/test_*.py; do .venv/bin/python -m unittest "tests.$(basename ${f%.py})" || echo "FAILED $f"; done
  ```

- **C7 — Type the code as given.** Mismatch with the real file ⇒ stop and report.
- **C8 — Commit per package**: `growi2 WP-GN: <goal line>`.

---

## Part 3 — Work packages

### WP-G0 — Baseline

**Steps**

1. `git status` clean on `neo-hardcoded`. `git checkout -b growi-truth`.
2. Run the suite (C6). Expected: no `FAILED` line.
3. `llm-wiki-dist/pyproject.toml`: delete the `[project.optional-dependencies]` `qdrant`
   group (nothing else changes; `sqlite-vec` stays). `uv sync`.

**Verify:** suite green; `.venv/bin/python -c "import sqlite_vec; print('ok')"`.

---

### WP-G1 — Delete the Qdrant adapter; sqlite-vec is the only backend (F1, F17)

**Goal:** one vector backend, one code path, no dead switch.

**Files:** `graph/vectors.py`, `graph/core.py`, `graph/librarian.py`, `tests/test_qdrant_vectors.py`,
`tests/test_page_settings.py`, `tests/test_vectors.py`.

**Steps**

1. `graph/vectors.py`: delete `class QdrantIndex` (85–449). Keep `VectorIndex` and
   `SqliteVecIndex`. Add to `SqliteVecIndex`, after `delete`:

   ```python
       def get(self, channel: str, item_id: str) -> list[float] | None:
           return self.store.get_vector(item_id, self._table(channel))

       def has(self, channel: str, item_id: str) -> bool:
           return self.store.has_vector(item_id, self._table(channel))

       def count(self, channel: str) -> int:
           return self.store.count_vectors(self._table(channel))
   ```

   (`store.get_vector` (1534), `has_vector` (1521), `count_vectors` (1497) exist; read their
   signatures — `get_vector(node_id, table)` / `has_vector(node_id, table)` — and match.)

2. `graph/core.py` `Settings` (113–116): delete the four lines `vector_backend`, `qdrant_url`,
   `qdrant_collection`, `qdrant_growi_id`; in `from_env` (231–234) delete their four lines.
   `tests/test_page_settings.py`: delete the two `vector_backend` assertions (lines 14, 30).

3. `graph/librarian.py`:
   - line 57: `from .vectors import SqliteVecIndex`.
   - `__init__` (214–223): the `if/else` becomes `self.vector_index = SqliteVecIndex(store)`.
   - `_bootstrap_vectors` (942–943): delete the `if isinstance(self.vector_index, QdrantIndex)` branch.
   - delete `_bootstrap_qdrant_vectors` (1038–1075).
   - `_store_search_items` (2399–2404): delete the "compatibility mirror" block
     (`if isinstance(self.vector_index, QdrantIndex): …`).
   - `_get_vector` (2204–2213): body becomes `return self.vector_index.get(channel, node_id)`.
   - `_ingest_one` (2176): `self.store.has_vector(node.id)` → `self.vector_index.has("body", node.id)`.
   - grep: `grep -n "Qdrant\|qdrant" graph/*.py app.py` must print nothing.

4. Delete `tests/test_qdrant_vectors.py`. `tests/test_vectors.py` stays (it tests `SqliteVecIndex`);
   add one assertion each for `get`, `has`, `count` against its `FakeStore`.

**Verify:** suite green; the app starts on `data/graph.sqlite` unchanged (`bootstrap.vectors_up_to_date`).

**Revert:** `git checkout -- graph tests pyproject.toml`.

---

### WP-G2 — Scoped vector search that still returns `k` hits (F2)

**Goal:** under a team scope (WP-G6 TEMP views) `vector_search` filters *after* the KNN, so
it must over-fetch.

**Files:** `graph/store.py`, `tests/test_scoped_store.py` (created in WP-G6).

**Steps**

1. `graph/store.py` `vector_search` (1557–1624): keep the `LIMIT ?` inside the `matches`
   CTE, add a second `LIMIT ?` at the very end of each of the two outer queries, and pass
   `(blob, limit * 4 if self.scope else limit, limit)` as the parameters. The `search_item`
   branch already joins `search_items s` and `nodes n` (both shadowed by TEMP views under a
   scope); the node branch joins `nodes n`. No other change.

   `self.scope` is added in WP-G6 step 2; until then add `self.scope = None` in
   `GraphStore.__init__` (WP-G6 replaces that line).

2. Test (in WP-G6's `tests/test_scoped_store.py`): 8 nodes, 4 per team, body vectors set so
   the four team-b nodes are closest to the query; the team-a scoped store with `limit=3`
   still returns 3 team-a hits.

**Verify:** suite green.

---

### WP-G3 — Job atomicity: long syncs commit per document (F12)

**Goal:** a sync never copies the whole database and never holds one transaction for hours.
(Vector rows need no lifecycle work under sqlite-vec: `store.delete_node(s)` removes them
and `vector_search` joins `nodes … status='active'`, so retired nodes are invisible.)

**Files:** `graph/librarian.py`.

**Steps**

1. Job atomicity. `_apply_job` (439–449) runs every non-snapshot job inside **one**
   `store.transaction()` held for the whole job — fine for a node edit, wrong for a sync
   that interleaves hours of LLM calls (the snapshot path exists for that reason, but it
   copies the whole 400 MB file). Add a third class, incremental jobs: lock, no
   transaction, no snapshot; they commit per document and are idempotent per document.

   ```python
       _SNAPSHOT_JOB_TYPES = {"ingest_md_output", "chunk_and_ingest"}
       # Long syncs commit per document (store._commit auto-commits outside a
       # transaction); a crash repeats the unfinished document on the next run.
       _INCREMENTAL_JOB_TYPES = {"sync_raw", "sync_growi", "sync_project"}

       def _apply_job(self, job: WriteJob) -> Any:
           if job.type in self._SNAPSHOT_JOB_TYPES:
               return self._apply_job_snapshotted(job)
           if job.type in self._INCREMENTAL_JOB_TYPES:
               with self._write_lock:
                   return self._dispatch_job(job)
           with self._write_lock, self.store.transaction():
               return self._dispatch_job(job)
   ```

   (`sync_project` is renamed `sync_raw` in WP-G7; drop the old name from the set then.
   The two snapshot types are deleted in WP-G10; the snapshot set becomes empty.)

**Verify:** suite green; `POST /api/sync` no longer writes a database snapshot copy
(read `_snapshot_db` for the file name) next to `graph.sqlite`.

---

### WP-G4 — GROWI client that matches GROWI (F5–F9)

**Goal:** full listing, legal paths, every published page mergeable, trash, `GET /api/growi`.

**Files:** `graph/growi.py`, `app.py`, `tests/test_growi_client.py`, `tests/test_growi_publish.py`.

**Steps**

1. `graph/growi.py` — replace `list_pages` (148–176) with two methods:

   ```python
       async def list_pages(
           self, root_path: str = "/", *, page: int = 1, limit: int = 100
       ) -> tuple[list[GrowiPage], int]:
           """One page of GROWI's listing: pages sorted by updatedAt desc + totalCount."""
           response = await self._request(
               "GET", "/pages/list", params={"path": root_path, "limit": limit, "page": page}
           )
           payload = response.json()
           if not isinstance(payload, dict):
               return [], 0
           raw_pages = payload.get("pages") or []
           pages = [self._page_from_payload(item) for item in raw_pages if isinstance(item, dict)]
           return pages, int(payload.get("totalCount") or len(pages))

       async def list_all_pages(self, root_path: str = "/") -> list[GrowiPage]:
           """Every non-empty page under root_path. GROWI paginates with page=N."""
           seen: dict[str, GrowiPage] = {}
           page = 1
           while True:
               batch, total = await self.list_pages(root_path, page=page)
               for item in batch:
                   if item.page_id:
                       seen.setdefault(item.page_id, item)
               # The listing is sorted by updatedAt desc; an edit while paging can
               # shift an item across a boundary. Dedupe by id and let the next poll
               # catch anything that slipped.
               if not batch or len(seen) >= total or page * 100 >= total:
                   return list(seen.values())
               page += 1
   ```

   `_page_from_payload` already handles `revision` as a bare id string (that is what the
   listing returns) and as a populated `{_id, body}` (what `GET /page` returns).

2. Add to `GrowiClient` after `update_page`:

   ```python
       async def delete_pages(self, page_ids_to_revisions: dict[str, str]) -> None:
           """Move pages to GROWI's trash (never `isCompletely`)."""
           if not page_ids_to_revisions:
               return
           await self._request(
               "POST",
               "/pages/delete",
               json_body={"pageIdToRevisionIdMap": page_ids_to_revisions},
           )
   ```

3. Add module-level helpers above `assert_publish_path`:

   ```python
   _GROWI_BAD = re.compile(r"[\^$*+#<>%?\\]")
   _LINES_RE = re.compile(r"^<!-- chunk: [^ ]+ lines (\d+)-(\d+)", re.MULTILINE)


   def growi_segment(name: str) -> str:
       """A GROWI-legal path segment (isCreatablePage rejects ^ $ * + # < > % ? \\ and *.md)."""
       cleaned = _GROWI_BAD.sub("-", name.strip()).strip("/")
       if cleaned.lower().endswith(".md"):
           cleaned = cleaned[:-3]
       return cleaned or "-"


   def growi_path(*parts: str) -> str:
       segments = [growi_segment(seg) for part in parts for seg in part.split("/") if seg.strip()]
       return "/" + "/".join(segments)


   def team_of_path(path: str, write_path: str) -> str | None:
       """`/inbox/team-a/spec.docx/001-x` with write_path `/inbox` → `team-a`; not under write_path → None."""
       base = "/" + write_path.strip("/")
       rest = path
       if base != "/":
           if not path.startswith(base.rstrip("/") + "/"):
               return None
           rest = path[len(base):]
       head, sep, _tail = rest.strip("/").partition("/")
       return head if sep and head else None


   def wrap_page(body: str, *, page_id: str, ranges: list[tuple[int, int]]) -> str:
       """Make a whole page one marked section unless it already carries chunk markers."""
       if _CHUNK_MARKER_RE.search(body):
           return body
       start = min((a for a, _ in ranges), default=0)
       end = max((b for _, b in ranges), default=0)
       digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
       return f"<!-- chunk: {page_id} lines {start}-{end} hash:{digest} -->\n{body}"


   def source_ranges(body: str) -> list[tuple[int, int]]:
       return [(int(a), int(b)) for a, b in _LINES_RE.findall(body) if int(b) >= int(a) > 0]
   ```

   Add `import hashlib` at the top.

4. `app.py`: add next to `/api/ready`:

   ```python
   @app.get("/api/growi")
   async def growi_info() -> dict[str, Any]:
       connection = _growi_connection()
       if connection is None:
           return {"enabled": False}
       return {
           "enabled": True,
           "name": connection.name,
           "url": connection.url,
           "mode": connection.mode,
           "root_path": connection.root_path,
           "write_path": connection.write_path,
       }
   ```

   `_growi_connection()` is defined in WP-G6; until then use
   `_registered_growi(os.environ.get("WIKI_GROWI_NAME", "local"))`.

5. Tests:
   - `test_growi_client.py`: a fake transport that answers `/pages/list` with
     `totalCount=250` and three pages of 100/100/50 → `list_all_pages` returns 250 ids and
     requested `page=1,2,3`.
   - `test_growi_publish.py`: `growi_path("/inbox", "team-a/x.docx", "028-MPI+OpenMP.md")`
     → `/inbox/team-a/x.docx/028-MPI-OpenMP`; `wrap_page` on a body without markers starts
     with `<!-- chunk: page/… lines 1-44`; `wrap_page` on a `pages`-mode body is unchanged;
     `merge_marked_sections(existing_wrapped, new_wrapped)` replaces the whole section and
     keeps text a human appended after it; `team_of_path("/inbox/team-a/x/y", "/inbox") == "team-a"`,
     `team_of_path("/Sandbox/a", "/inbox") is None`.

**Verify:** suite green; against your GROWI:
```bash
cd llm-wiki-dist && WIKI_SECRET_KEY=… .venv/bin/python - <<'EOF'
import asyncio
from graph.growi import GrowiClient
from graph.registry import ConnectionRegistry
c = ConnectionRegistry("../data/engine.sqlite").get("local")
cl = GrowiClient(c.url, c.api_token)
print(len(asyncio.run(cl.list_all_pages("/"))))
EOF
```

---

### WP-G5 — One project root, teams, and a git repo the app owns (F10, F13)

**Goal:** `Project(WIKI_DATA_ROOT)` is the only layout; teams come from folders; `raw/` is
initialised, identified and committed by the app; a repo with no commits is not an error.

**Files:** `graph/project.py`, `graph/sync.py`, `graph/convert.py`, `tests/test_project.py`,
`tests/test_sync.py`.

**Steps**

1. `graph/project.py` — add after `raw_name_for`:

   ```python
   RESERVED_TEAMS = {"all", "admin", "assets"}


   def team_of(rel: str) -> str:
       """`team-a/x/y_docx.md` → `team-a`; a file at the root → `general`."""
       head, sep, _ = rel.partition("/")
       return head if sep and head else "general"
   ```

   add to `Project`, after `raw_files`:

   ```python
       @property
       def engine_db(self) -> Path:
           return self.root / "engine.sqlite"

       def teams(self) -> list[str]:
           """Top-level folders of mount/ and raw/ (minus .git and reserved names)."""
           names: set[str] = set()
           for base in (self.mount, self.raw):
               if not base.is_dir():
                   continue
               for path in base.iterdir():
                   if path.is_dir() and not path.name.startswith(".") and path.name not in RESERVED_TEAMS:
                       names.add(path.name)
           return sorted(names)
   ```

   and change `zip_wiki` to take an optional team:

   ```python
   def zip_wiki(project: Project, team: str | None = None) -> bytes:
       base = project.wiki / team if team else project.wiki
       buffer = io.BytesIO()
       with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
           for path in sorted(base.rglob("*")):
               if not path.is_file() or "_planning" in path.parts:
                   continue
               archive.write(path, path.relative_to(project.wiki).as_posix())
       return buffer.getvalue()
   ```

2. `graph/sync.py` — replace `git` and `head_sha` (28–39) with:

   ```python
   _GIT_IDENTITY = ("-c", "user.name=llm-wiki", "-c", "user.email=llm-wiki@localhost")


   def git(project: Project, *args: str) -> str:
       result = subprocess.run(
           ["git", *_GIT_IDENTITY, "-C", str(project.raw), *args],
           check=True,
           capture_output=True,
           text=True,
       )
       return result.stdout


   def ensure_repo(project: Project) -> None:
       """raw/ is a git repo the app owns. Idempotent."""
       project.raw.mkdir(parents=True, exist_ok=True)
       if not (project.raw / ".git").exists():
           git(project, "init", "-q")


   def head_sha(project: Project) -> str | None:
       """None while the repo has no commit yet."""
       result = subprocess.run(
           ["git", "-C", str(project.raw), "rev-parse", "-q", "--verify", "HEAD"],
           capture_output=True,
           text=True,
       )
       return result.stdout.strip() or None


   def commit_raw(project: Project, message: str) -> bool:
       """Stage everything and commit if anything changed. Returns True when a commit was made."""
       ensure_repo(project)
       git(project, "add", "-A", ".")
       if not git(project, "status", "--porcelain").strip():
           return False
       git(project, "commit", "-qm", message)
       return True
   ```

   In `plan_changes` (49–64): the second line becomes

   ```python
       head = head_sha(project)
       if head is None:
           return []
       if previous is None:
           return [Change("A", rel) for rel in project.raw_files()]
       if previous == head:
           return []
       try:
           listing = git(project, "diff", "--name-status", "--no-renames", f"{previous}..HEAD", "--", ".")
       except subprocess.CalledProcessError:
           # last_sha points at a commit that no longer exists (history rewritten):
           # treat everything as new; the writer skip in WP-G7 keeps this cheap.
           return [Change("A", rel) for rel in project.raw_files()]
       for line in listing.splitlines():
   ```

   (the rest of the loop body is unchanged). `changed_hunks`: wrap its `git(...)` call the
   same way and return `[]` on `CalledProcessError`.

3. `graph/convert.py` (117–119): replace the two `git(...)` lines with
   `commit_raw(project, f"convert: +{len(converted)} -{len(removed)}")` and change the
   import to `from .sync import commit_raw`.

4. `tests/test_project.py`: `team_of("a/b/c.md") == "a"`, `team_of("c.md") == "general"`,
   `teams()` lists `mount/t1`, `raw/t2`, skips `raw/.git` and `mount/all`.
   `tests/test_sync.py`: replace the `git(...)` helper's identity env with a call to
   `sync.ensure_repo(self.project)` + `sync.commit_raw(self.project, "one")`; add
   `test_no_commits_is_not_an_error`: fresh `Project`, `ensure_repo`, `plan_changes == []`,
   `head_sha is None`.

**Verify:** suite green; in a temp dir with no git identity (`HOME=/tmp/empty`),
`python -c "from graph.project import Project; from graph.sync import *; p=Project(Path('/tmp/x')).ensure(); ensure_repo(p); (p.raw/'t/a.md').parent.mkdir(parents=True); (p.raw/'t/a.md').write_text('x'); print(commit_raw(p,'m'), head_sha(p), plan_changes(p))"` → `True <sha> [Change('A','t/a.md')]`.

---

### WP-G6 — Scopes: `team` on nodes, scoped read stores, routing (F13, F14, F15)

**Goal:** one engine stack; `/prefix/<team>/` and `/prefix/all/` are filtered views of it.

**Files:** `graph/core.py`, `graph/store.py`, `graph/librarian.py`, `app.py`, `mcp_server.py`,
`tests/test_scoped_store.py` (new), `tests/test_growi_routing.py`.

**Steps**

1. `graph/core.py` `Node` (347): add after `cluster: str | None = None`:

   ```python
       # Scope tag: first folder under mount/ (raw sync) or the segment after the GROWI
       # write path (GROWI sync). None = only visible in the `all` scope.
       team: str | None = None
   ```

   `Settings`: add after `parser_base_url`:

   ```python
       # engine.sqlite (registry + GROWI ledger); default is <data_root>/engine.sqlite
       engine_db: str = ""
       growi_name: str = ""
       sync_interval_seconds: int = 300
   ```

   and in `from_env`: `engine_db=env("WIKI_ENGINE_DB", cls.engine_db)`,
   `growi_name=env("WIKI_GROWI_NAME", cls.growi_name)`,
   `sync_interval_seconds=int(env("WIKI_SYNC_INTERVAL_SECONDS", cls.sync_interval_seconds))`.

2. `graph/store.py`:
   - `_ensure_node_columns` additions dict: add `"team": "TEXT"`; index script: add
     `CREATE INDEX IF NOT EXISTS idx_nodes_team ON nodes(team);`.
   - `_row_to_node`: add `team=row["team"] if "team" in row.keys() else None,`.
   - `upsert_node`: add `team` to the column list, the `VALUES (…)` gets one more `?`
     (19 total), `team=excluded.team` in the `ON CONFLICT` set, and `node.team` in the
     parameter tuple right after `node.bridge_probe`.
   - `__init__` signature: `def __init__(self, path=..., readonly: bool = False, scope: str | None = None)`; after `self.readonly = readonly`:

     ```python
             # A scope is a read-only, team-filtered view of the same file (see _open_connection).
             if scope and not readonly:
                 raise ValueError("a scoped GraphStore must be readonly")
             self.scope = scope
     ```

   - `_open_connection`: after `conn.execute("PRAGMA foreign_keys=ON")` (and after the
     deleted `_load_vec_extension` call) add:

     ```python
             if self.scope:
                 # TEMP objects shadow main-schema names on this connection only, so every
                 # existing query below sees just this team. Verified on mode=ro.
                 team = self.scope.replace("'", "''")
                 conn.executescript(
                     f"""
                     CREATE TEMP VIEW nodes AS SELECT * FROM main.nodes WHERE team = '{team}';
                     CREATE TEMP VIEW edges AS
                       SELECT e.* FROM main.edges e
                       JOIN main.nodes s ON s.id = e.source_node_id AND s.team = '{team}'
                       JOIN main.nodes t ON t.id = e.target_node_id AND t.team = '{team}';
                     CREATE TEMP VIEW search_items AS
                       SELECT s.* FROM main.search_items s
                       JOIN main.nodes n ON n.id = s.node_id AND n.team = '{team}';
                     """
                 )
     ```

   - add a method next to `nodes_fingerprint`:

     ```python
         def teams(self) -> list[str]:
             rows = self.connection.execute(
                 "SELECT DISTINCT team FROM main.nodes WHERE team IS NOT NULL AND status='active' ORDER BY team"
             ).fetchall()
             return [row["team"] for row in rows]
     ```

3. `graph/librarian.py`:
   - (vectors need no team tag: `vector_search` joins `nodes`, and the scoped TEMP view carries `team`.)
   - `create_exogenous_node` (1355): add parameter `team: str | None = None` and set
     `team=team` in the `Node(...)` it builds (line ~1385, next to `cluster="Agent Notes"`).
     `_dispatch_job` `create_exogenous` branch: pass `team=job.payload.get("team")`.
   - `_regenerate_exogenous_node` (3315) copies `cluster=old.cluster` — add `team=old.team`
     beside it. Same in the superseding copy at 1214 (`update_node`).
   - add a helper near `settings`:

     ```python
         def registry(self):
             from .registry import ConnectionRegistry

             path = self.settings.engine_db or str(Path(self.settings.data_root) / "engine.sqlite")
             return ConnectionRegistry(path)

         def growi_connection(self):
             """The one GROWI this engine mirrors (WIKI_GROWI_NAME, or the only registered row)."""
             registry = self.registry()
             name = self.settings.growi_name
             if name:
                 return registry.get(name)
             rows = registry.list()
             return rows[0] if len(rows) == 1 else None
     ```

     `publish_to_growi` and `sync_growi` (1850–1985) now use `self.registry()` and
     `self.growi_connection()` instead of reading `WIKI_ENGINE_DB` from `os.environ` and
     `job.payload["name"]` — delete those lookups (both methods are rewritten in WP-G7/G8 anyway).

4. `app.py` — the stack becomes one engine plus scoped researchers.
   - Replace lines 94–100 (`DB_DIR … DEFAULT_DB`) with:

     ```python
     from graph.project import RESERVED_TEAMS, Project

     DATA_ROOT = Path(os.environ.get("WIKI_DATA_ROOT", "data")).resolve()
     PROJECT = Project(DATA_ROOT)
     ALL_SCOPE = "all"
     ```

     Replace `GROWI_ENABLED` (111–116) and `GROWI_ENGINE_DB` (117–119) with
     `GROWI_ENGINE_DB = Path(os.environ.get("WIKI_ENGINE_DB", str(PROJECT.engine_db)))`.
     `_RESERVED_DB_NAMES = RESERVED_TEAMS`. `current_db` default → `ALL_SCOPE` (it now holds
     the scope; keep the variable name to keep the diff small).
   - `_build_stack(db_path, data_root)` → `_build_stack()`; inside: `settings.database_path = str(PROJECT.database)`,
     `settings.data_root = str(DATA_ROOT)`, `settings.engine_db = str(GROWI_ENGINE_DB)`;
     the rest unchanged (it builds gateway, write store, librarian, unscoped read store,
     researcher, vocabulary).
   - Replace `_bootstrap_db` (186–224) with:

     ```python
     async def _bootstrap_engine() -> None:
         """Build the one engine stack; cached under STACKS[ALL_SCOPE]."""
         if ALL_SCOPE in building or ALL_SCOPE in STACKS:
             return
         building.add(ALL_SCOPE)
         errors[ALL_SCOPE] = None
         stages[ALL_SCOPE] = "starting"
         try:
             connection = _growi_connection()
             if connection is None:
                 stages[ALL_SCOPE] = "needs_growi"
                 errors[ALL_SCOPE] = "register a GROWI connection in /admin first"
                 return
             from graph.growi import GrowiClient

             stages[ALL_SCOPE] = "checking_growi"
             if not await GrowiClient(connection.url, connection.api_token).health():
                 raise RuntimeError(f"GROWI healthcheck failed: {connection.url}")
             PROJECT.ensure()
             stack = await asyncio.to_thread(_build_stack)
             await stack["librarian"].start()
             STACKS[ALL_SCOPE] = stack
             stages[ALL_SCOPE] = "ready"
             await stack["librarian"].enqueue("sync_raw", {})
             await stack["librarian"].enqueue("sync_growi", {})
             log.info("startup: engine ready, serving requests")
         except Exception as exc:
             stages[ALL_SCOPE] = "failed"
             errors[ALL_SCOPE] = f"{type(exc).__name__}: {exc}"
             log.exception("startup/bootstrap failed")
         finally:
             building.discard(ALL_SCOPE)


     def _scope_stack(team: str) -> dict:
         """A read-only, team-filtered view over the engine; cheap, built on first use."""
         engine = STACKS[ALL_SCOPE]
         store = GraphStore(engine["write_store"].path, readonly=True, scope=team)
         return {**engine, "read_store": store, "researcher": Researcher(engine["gateway"], store)}


     def _growi_connection():
         try:
             registry = _growi_registry()
             name = os.environ.get("WIKI_GROWI_NAME", "")
             if name:
                 return registry.get(name)
             rows = registry.list()
             return rows[0] if len(rows) == 1 else None
         except Exception as exc:
             log.warning("GROWI registry lookup failed: %s", exc)
             return None


     def _known_scopes() -> set[str]:
         scopes = {ALL_SCOPE, *PROJECT.teams()}
         engine = STACKS.get(ALL_SCOPE)
         if engine is not None:
             with suppress(Exception):
                 scopes.update(engine["read_store"].teams())
         return scopes
     ```

     `_ensure_building(db)` → `_ensure_building()` → `asyncio.create_task(_bootstrap_engine())`
     when `ALL_SCOPE not in STACKS and ALL_SCOPE not in building`.
   - `_close_stack(db)` → `_close_all()`: close every scope's `read_store`/`researcher`
     (they are distinct objects), then the engine's librarian/write_store/read_store/gateway
     once; clear `STACKS`, `stages`, `errors`.
   - `_ready_stack()` becomes:

     ```python
     def _ready_stack() -> dict:
         if stages.get(ALL_SCOPE) != "ready" or ALL_SCOPE not in STACKS:
             _ensure_building()
             raise HTTPException(status_code=503, detail=_not_ready_detail(ALL_SCOPE))
         scope = current_db.get()
         if scope not in STACKS:
             STACKS[scope] = _scope_stack(scope)
         return STACKS[scope]
     ```

     `_not_ready_detail(db)` keeps its signature; `/api/ready` and `restart_bootstrap` call
     `_ensure_building()` / `_bootstrap_engine()` and report `stages[ALL_SCOPE]`.
   - `lifespan` (254–403): delete the whole candidates/preload block (285–384) and replace
     with `await _bootstrap_engine()` followed by the sync timer from WP-G8. Keep the
     `finally` → `await _close_all()`.
   - `db_routing` (930–1018): replace the "unknown wiki" check (1002–1004) with

     ```python
         if seg != ALL_SCOPE and seg not in _known_scopes():
             return PlainTextResponse("unknown scope", status_code=404)
     ```

     and the bare-prefix redirect (997) with `RedirectResponse(f"{PREFIX}/{ALL_SCOPE}/", 307)`.
   - Delete `_registered_growi`, `_growi_cache_path`, `_data_root`, `_db_path`,
     `_db_sidecar_paths`, `_unlink_db_files` (546–594). `_db_url` stays (`_validate_name` too).
   - Add:

     ```python
     @app.get("/api/scopes")
     async def scopes() -> dict[str, Any]:
         return {
             "current": current_db.get(),
             "all": ALL_SCOPE,
             "teams": sorted(_known_scopes() - {ALL_SCOPE}),
             "urls": {name: _db_url(name) for name in sorted(_known_scopes())},
         }


     def _scope_team() -> str | None:
         scope = current_db.get()
         return None if scope == ALL_SCOPE else scope


     def _require_team() -> str:
         team = _scope_team()
         if team is None:
             raise HTTPException(
                 status_code=400,
                 detail=api_error("open a team scope to write (e.g. /prefix/<team>/)", False, "no_team"),
             )
         return team
     ```

   - `create_exogenous` (2253): add `"team": _scope_team()` to the payload.
   - `/api/wiki.zip` (2336): `zip_wiki(PROJECT, _scope_team())`, filename `f"{current_db.get()}-wiki.zip"`; drop the `DATA_ROOT is None` guard (it is never None now). Same for `/api/sync`.
   - `_growi_public_summary`: `stage`/`error` come from `stages.get(ALL_SCOPE)` /
     `errors.get(ALL_SCOPE)`. `_require_growi_enabled()` → delete the function and its calls.
     `admin_resync_connection` → `_enqueue("sync_growi", {})`.

5. `mcp_server.py` `_is_allowed` (254–265): delete the `.sqlite` check; the method returns
   `True` when the name matches `_DB_RE`, is not reserved (`admin`, `assets`) and passes the
   allowlist. The backend answers 404 for unknown scopes and the proxy already surfaces
   that. Delete the `--db-dir`/`db_dir` plumbing (lines ~1344, 265) and `MCP_ALLOW_NEW_WIKIS`.

6. Tests:
   - new `tests/test_scoped_store.py`: write two nodes (`team="a"`, `team="b"`), an edge
     between them, one search item each, into a temp `GraphStore`; open
     `GraphStore(path, readonly=True, scope="a")`; assert `get_all_nodes()` → 1,
     `keyword_search(<word in both bodies>)` → 1, `get_all_edges()` → 0, `get_node(<b id>)`
     is None, `search_items_fts_query(<word>)` → 1, `teams()` → `["a","b"]` (unscoped
     method reads `main.nodes`), and `GraphStore(path, scope="a")` (not readonly) raises.
   - `tests/test_growi_routing.py`: `/wiki/team-a/api/ready` routes with `current_db == "team-a"`
     when `_known_scopes` is patched to `{"all","team-a"}`; `/wiki/nope/` → 404; `/wiki/` → 307 to `/wiki/all/`.

**Verify:** suite green. Run the app with `WIKI_DATA_ROOT=…/data`; `curl …/all/api/scopes`
lists your mount folders; `curl …/pdf/api/graph` returns only `pdf` nodes (after WP-G8 has
indexed; before that it is empty, which is fine).

---

### WP-G7 — `sync_raw`: convert → write → publish to GROWI (F7, F8, F11)

**Goal:** raw changes become GROWI pages. The index is **not** touched here.

**Files:** `graph/sync.py`, `graph/writers.py`, `graph/growi.py`, `graph/librarian.py`,
`app.py`, `tests/test_sync.py`, `tests/test_growi_publish.py`.

**Steps**

1. `graph/writers.py` `write_wiki` (128–161): after `publish_output(result.out_dir, target)` add

   ```python
           write_source_stamp(target, project.raw_file(rel), rel)
   ```

   and add the helper + `up_to_date` at module level:

   ```python
   import hashlib
   import json


   def _sha256_file(path: Path) -> str:
       return hashlib.sha256(Path(path).read_bytes()).hexdigest()


   def write_source_stamp(target: Path, raw_file: Path, rel: str) -> None:
       """wiki/<…>/_planning/source.json = which raw bytes this folder was written from."""
       planning = Path(target) / "_planning"
       planning.mkdir(exist_ok=True)
       tmp = planning / "source.json.tmp"
       tmp.write_text(json.dumps({"raw": rel, "sha256": _sha256_file(raw_file)}), encoding="utf-8")
       tmp.replace(planning / "source.json")


   def up_to_date(project: Any, rel: str) -> bool:
       """True when wiki/<…> was written from the raw bytes that exist now."""
       planning = project.wiki_dir(rel) / "_planning"
       current = _sha256_file(project.raw_file(rel)) if project.raw_file(rel).exists() else ""
       for name, key in (("source.json", "sha256"), ("manifest.json", "source_sha256")):
           path = planning / name
           if path.exists():
               try:
                   return json.loads(path.read_text(encoding="utf-8")).get(key) == current
               except ValueError:
                   return False
       return False
   ```

   (`manifest.json.source_sha256` is what your existing `wiki`-mode folders already carry —
   this is what stops the first run from re-writing every book.)

2. `graph/growi.py` — add the publisher (sync wrappers; every caller runs on a worker thread):

   ```python
   class GrowiPublisher:
       """Publish one document's wiki folder into GROWI, or trash it. Append-only by construction."""

       def __init__(self, client: GrowiClient, connection: Any) -> None:
           self.client = client
           self.connection = connection

       def doc_path(self, project: Any, rel: str) -> str:
           folder = project.wiki_dir(rel).relative_to(project.wiki).as_posix()  # team-a/spec.docx
           return growi_path(self.connection.write_path, folder)

       def publish_document(self, project: Any, rel: str) -> list[GrowiPage]:
           folder = project.wiki_dir(rel)
           doc_path = self.doc_path(project, rel)
           ranges = _coverage_ranges(folder)
           pages: list[dict[str, str]] = []
           for md in sorted(folder.glob("*.md")):
               name = growi_segment(md.name)
               body = md.read_text(encoding="utf-8")
               pages.append(
                   {
                       "path": f"{doc_path}/{name}",
                       "body": wrap_page(
                           body,
                           # marker ids may not contain spaces (_CHUNK_MARKER_RE: [^ ]+)
                           page_id="page" + f"{doc_path}/{name}".replace(" ", "_"),
                           ranges=ranges.get(_canonical(md.name), []),
                       ),
                   }
               )
           results = asyncio.run(
               publish_pages(
                   self.client,
                   pages,
                   mode=self.connection.mode,
                   write_path=self.connection.write_path,
                   root_path=self.connection.root_path,
               )
           )
           asyncio.run(self._trash_under(doc_path, keep={p["path"] for p in pages}))
           return results

       def delete_document(self, project: Any, rel: str) -> int:
           return asyncio.run(self._trash_under(self.doc_path(project, rel), keep=set()))

       async def _trash_under(self, doc_path: str, *, keep: set[str]) -> int:
           """Trash our marked pages under doc_path that are not in `keep`. Human pages stay."""
           doomed: dict[str, str] = {}
           for listed in await self.client.list_all_pages(doc_path):
               if listed.path == doc_path or listed.path in keep:
                   continue
               full = await self.client.get_page(page_id=listed.page_id)
               if full and _CHUNK_MARKER_RE.search(full.body):
                   doomed[full.page_id] = full.revision_id
           await self.client.delete_pages(doomed)
           return len(doomed)


   _NUMBERED_RE = re.compile(r"^\d+-(.+)$")


   def _canonical(filename: str) -> str:
       match = _NUMBERED_RE.match(filename)
       return match.group(1) if match else filename


   def _coverage_ranges(folder: Path) -> dict[str, list[tuple[int, int]]]:
       path = Path(folder) / "_planning" / "coverage.json"
       if not path.exists():
           return {}
       try:
           files = json.loads(path.read_text(encoding="utf-8")).get("files", [])
       except ValueError:
           return {}
       out: dict[str, list[tuple[int, int]]] = {}
       for item in files:
           start, end = item.get("source_start"), item.get("source_end")
           if item.get("filename") and start is not None and end is not None:
               out[str(item["filename"])] = [(int(start), int(end))]
       return out
   ```

   Add `import asyncio`, `import json` and `from pathlib import Path` at the top.
   (`coverage.files[].filename` is the prefix-stripped name — see `PLAN_SYNC.md` folder contract.)

3. `graph/sync.py` — replace `sync_project` (82–163) with `sync_raw`:

   ```python
   def sync_raw(
       project: Project,
       publisher: Any,
       *,
       mode: str,
       settings: Any,
       llm: Any,
       embedder: Any,
       on_progress: Progress = None,
       stop_check: StopCheck = None,
   ) -> dict[str, Any]:
       """mount → raw (git) → wiki/ → GROWI. Never touches the index; sync_growi does."""
       from .writers import up_to_date, write_index, write_wiki

       def emit(**event: Any) -> None:
           if on_progress:
               on_progress(event)

       def stop() -> bool:
           return bool(stop_check and stop_check())

       ensure_repo(project)
       parser_base_url = str(getattr(settings, "parser_base_url", "") or "")
       if parser_base_url and project.mount.exists():
           from .convert import convert_mount

           emit(stage="convert", step="start")
           report = convert_mount(project, parser_base_url=parser_base_url, settings=settings, on_progress=on_progress)
           emit(stage="convert", step="done", **{key: len(value) for key, value in report.items()})

       changes = plan_changes(project)
       head = head_sha(project)
       done: list[dict[str, Any]] = []
       for index, change in enumerate(changes, start=1):
           if stop():
               raise RuntimeError("sync cancelled")
           emit(stage="sync", step=change.status, current=index, total=len(changes), file=change.rel)
           if change.status == "D":
               shutil.rmtree(project.wiki_dir(change.rel), ignore_errors=True)
               shutil.rmtree(project.state_dir(change.rel), ignore_errors=True)
               trashed = publisher.delete_document(project, change.rel)
               done.append({"file": change.rel, "status": "D", "trashed": trashed})
               continue
           if change.status == "M" and mode == "wiki" and not up_to_date(project, change.rel):
               from .wiki.incremental import invalidate_pages

               result = invalidate_pages(
                   project.state_dir(change.rel),
                   changed_hunks(project, change.rel),
                   project.raw_file(change.rel).read_bytes().decode("utf-8"),
               )
               emit(stage="sync", step="invalidate", file=change.rel, result=result)
           if not up_to_date(project, change.rel):
               write_wiki(
                   project,
                   change.rel,
                   mode=mode,
                   settings=settings,
                   llm=llm,
                   embedder=embedder,
                   on_progress=lambda event, rel=change.rel: emit(stage="write", file=rel, **event),
                   stop_check=stop_check,
               )
           emit(stage="publish", file=change.rel)
           published = publisher.publish_document(project, change.rel)
           done.append({"file": change.rel, "status": change.status, "pages": len(published)})
       write_index(project)
       if head is not None:
           project.metadata.mkdir(parents=True, exist_ok=True)
           project.last_sha_path.write_text(head + "\n", encoding="utf-8")
       return {"head": head, "changes": done}
   ```

4. `graph/librarian.py` — replace `sync_project` (1817–1848) and `publish_to_growi`
   (1850–1894) and `growi_pages_from_wiki` (1896–1912) with one method:

   ```python
       def sync_raw(self, job: WriteJob) -> dict[str, Any]:
           """mount → raw → wiki/ → GROWI for this engine's project folder."""
           from .chunk import make_llm
           from .growi import GrowiClient, GrowiPublisher
           from .project import Project
           from .sync import sync_raw

           connection = self.growi_connection()
           if connection is None:
               raise RuntimeError("no GROWI connection registered")
           project = Project(Path(self.settings.data_root)).ensure()
           settings = self.settings
           llm = make_llm(
               model=settings.chat_model,
               base_url=settings.chat_base_url,
               api_key=settings.chat_api_key,
               temperature=settings.chat_temperature,
           )
           publisher = GrowiPublisher(GrowiClient(connection.url, connection.api_token), connection)

           def on_progress(update: dict[str, Any]) -> None:
               job.progress = update

           try:
               result = sync_raw(
                   project,
                   publisher,
                   mode=str(job.payload.get("ingest_mode") or getattr(settings, "ingest_mode", "chunks")),
                   settings=settings,
                   llm=llm,
                   embedder=self.gateway.embedder,
                   on_progress=on_progress,
                   stop_check=lambda: job.stop_event.is_set(),
               )
           except Exception as exc:
               self.registry().record_sync(connection.name, error=f"{type(exc).__name__}: {exc}")
               raise
           return result
   ```

   `_dispatch_job`: `sync_project` → `sync_raw`; delete the `publish_to_growi` branch.

5. `app.py` `/api/sync` (2321): enqueue `sync_raw` **then** `sync_growi`; return the first job:

   ```python
   @app.post("/api/sync")
   async def sync_now(payload: SyncBody | None = None) -> dict:
       first = await _enqueue("sync_raw", {"ingest_mode": payload.ingest_mode if payload else None})
       await _enqueue("sync_growi", {})
       return first
   ```

6. Tests — `tests/test_sync.py`: the `FakeLibrarian` becomes `FakePublisher` with
   `publish_document(project, rel) -> [1]` and `delete_document(project, rel) -> 1`;
   assert first run publishes `f1/a_docx.md`, a `D` calls `delete_document`, a second run
   with unchanged raw and a `_planning/source.json` written by `fake_write` **does not** call
   `write_wiki` (patch it with `side_effect=AssertionError`). `tests/test_growi_publish.py`:
   `GrowiPublisher.doc_path` for `team-a/x_docx.md` with write_path `/inbox` is
   `/inbox/team-a/x.docx`; `_trash_under` trashes only marked pages (fake client with one
   marked and one human page). `tests/test_wiki_zip.py::test_growi_pages_from_wiki` tests
   the deleted helper — delete that test. `tests/test_writers.py::test_write_wiki_publishes_flat_pages_and_planning`:
   add `self.assertTrue(writers.up_to_date(project, "f1/a_docx.md"))` at the end.

**Verify:** suite green. Real run: put one small `.docx` under `data/mount/<team>/`, set
`WIKI_PARSER_BASE_URL`, `POST …/all/api/sync`, watch `/api/write-jobs`, then open GROWI
`/inbox/<team>/<name>.docx/` — pages exist; edit one by hand in GROWI, add a paragraph
*below* the marked section, re-run sync: your paragraph survives.

---

### WP-G8 — `sync_growi` is the only indexer, on a timer (F11, F14)

**Goal:** the index is built from GROWI pages only, per document folder, through
`_revise_document`; a timer and startup keep it moving.

**Files:** `graph/growi.py`, `graph/librarian.py`, `app.py`, `tests/test_growi_sync.py`.

**Steps**

1. `graph/growi.py` — replace `sync_growi_pages` (290–375) with:

   ```python
   def _parent(path: str) -> str:
       return posixpath.dirname(path.rstrip("/")) or "/"


   async def sync_growi_pages(
       client: GrowiClient,
       registry: Any,
       connection: Any,
       *,
       on_document: Any,
       on_delete_document: Any,
   ) -> dict[str, int]:
       """Diff GROWI against the ledger; revise every touched document folder.

       A document is a parent path. The ledger rows of a document move only after
       its callback succeeded, so a crash repeats work instead of skipping it.
       """
       remote = {p.page_id: p for p in await client.list_all_pages(connection.root_path)}
       local = {p.page_id: p for p in registry.pages(connection.name)}

       touched: set[str] = set()
       for page_id, page in remote.items():
           previous = local.get(page_id)
           if previous is None or previous.revision_id != page.revision_id or previous.path != page.path:
               touched.add(_parent(page.path))
               if previous is not None:
                   touched.add(_parent(previous.path))
       for page_id, previous in local.items():
           if page_id not in remote:
               touched.add(_parent(previous.path))

       by_document: dict[str, list[GrowiPage]] = {}
       for page in remote.values():
           by_document.setdefault(_parent(page.path), []).append(page)

       revised = deleted = 0
       for document in sorted(touched):
           listed = sorted(by_document.get(document, []), key=lambda p: p.path)
           if not listed:
               await _maybe_await(on_delete_document, document)
               deleted += 1
           else:
               full = [await client.get_page(page_id=p.page_id) for p in listed]
               await _maybe_await(on_document, document, [p for p in full if p is not None])
               revised += 1
           for page in listed:
               registry.upsert_page(registry_page(connection.name, page))
           for page_id, previous in local.items():
               if page_id not in remote and _parent(previous.path) == document:
                   registry.delete_page(connection.name, page_id)

       registry.record_sync(
           connection.name, cursor=None, synced_at=datetime.now(timezone.utc).isoformat(), error=None
       )
       return {"pages": len(remote), "documents_revised": revised, "documents_deleted": deleted}
   ```

2. `graph/librarian.py` — replace `sync_growi` (1914–1985) with:

   ```python
       def sync_growi(self, job: WriteJob) -> dict[str, Any]:
           """Bring the index in line with GROWI. This is the only wiki-page indexer."""
           from .growi import GrowiClient, source_ranges, sync_growi_pages, team_of_path

           connection = self.growi_connection()
           if connection is None:
               raise RuntimeError("no GROWI connection registered")
           client = GrowiClient(connection.url, connection.api_token)
           registry = self.registry()
           changed = False

           def node_from_page(page: Any, document: str) -> Node:
               body = page.body.strip()
               name = re.sub(r"^\d+-", "", page.path.rstrip("/").split("/")[-1])
               return Node(
                   id=make_node_id(body, document),
                   body=body,
                   type=NodeType.page,
                   title=self._title_from_markdown(body) or name,
                   original_document_name=document,
                   source_path=page.path,
                   source_ranges=source_ranges(body),
                   source_version=page.revision_id,
                   source_material_hash=source_hash(body),
                   cluster=document.strip("/").split("/")[-1] or "GROWI",
                   team=team_of_path(page.path, connection.write_path),
               )

           def revise(document: str, pages: list[Any]) -> None:
               nonlocal changed
               if job.stop_event.is_set():
                   raise JobCancelled("GROWI sync cancelled")
               nodes = [node_from_page(page, document) for page in pages]
               nodes = [node for node in nodes if node.body]
               edges = self._chain_edges([node.id for node in nodes], "Next page in the source document.")
               version = source_hash("|".join(page.revision_id for page in pages))
               stop = lambda: job.stop_event.is_set()  # noqa: E731
               if self.store.get_nodes_by_document(document, active_only=True):
                   actions = self._revise_document(nodes, edges, document, version, stop_check=stop)
               else:
                   # First time we see this folder: the parallel prepare/link path
                   # (same as ingest_md_output's first-ingest branch used to do).
                   for node in nodes:
                       node.source_version = version
                   self._prepare_and_link_nodes(nodes, stop_check=stop, label=document)
                   self._replace_structural_edges(document, edges)
                   self.store.record_source(document, version)
                   actions = [f"ingested-new:{node.id}" for node in nodes]
               changed = changed or any(not a.startswith("unchanged") for a in actions)
               job.progress = {"stage": "growi_sync", "document": document, "actions": len(actions)}

           def drop(document: str) -> None:
               nonlocal changed
               self.delete_document(document)
               changed = True

           try:
               result = asyncio.run(
                   sync_growi_pages(client, registry, connection, on_document=revise, on_delete_document=drop)
               )
           except Exception as exc:
               registry.record_sync(connection.name, error=f"{type(exc).__name__}: {exc}")
               raise
           if changed and self._recluster_every > 0:
               try:
                   self.recluster()
                   self.ensure_japanese_clusters()
               except Exception as exc:  # noqa: BLE001 - clustering is best-effort
                   log.info("recluster skipped: %s", exc)
           return result
   ```

   `_apply_job` already holds `_write_lock` for the whole job (WP-G3 step 5), which is why
   the callbacks above take no lock: `threading.Lock` is not re-entrant. `re`, `make_node_id`,
   `source_hash`, `NodeType`, `JobCancelled` are already imported in `librarian.py`.

3. `app.py` — the timer. In `lifespan`, right after `await _bootstrap_engine()`:

   ```python
       async def sync_timer() -> None:
           interval = int(os.environ.get("WIKI_SYNC_INTERVAL_SECONDS", "300"))
           if interval <= 0:
               return
           while True:
               await asyncio.sleep(interval)
               stack = STACKS.get(ALL_SCOPE)
               if stack is None or stages.get(ALL_SCOPE) != "ready":
                   continue
               librarian = stack["librarian"]
               busy = any(
                   job.type in {"sync_raw", "sync_growi"} and job.status in {"queued", "running"}
                   for job in librarian.list_jobs(limit=500)
               )
               if busy:
                   continue
               with suppress(RuntimeError):  # queue full: try again next tick
                   await librarian.enqueue("sync_raw", {})
                   await librarian.enqueue("sync_growi", {})

       timer = asyncio.create_task(sync_timer())
   ```

   and in the `finally`: `timer.cancel()` before `_close_all()`.

4. `tests/test_growi_sync.py`: rewrite `FakeClient` with `list_all_pages` and `get_page`;
   tests: (a) unchanged revisions → no `on_document` call and no `get_page`; (b) one edited
   page in a 3-page folder → `on_document(document, 3 pages)` and three `get_page`s;
   (c) a page moved to another folder → both folders touched; (d) a folder whose pages all
   vanished → `on_delete_document` and ledger rows removed; (e) a callback that raises →
   ledger rows for that document unchanged, `record_sync` not called.

**Verify:** suite green. Real run: after WP-G7 published pages, `POST …/all/api/sync`;
`GET …/all/api/graph` shows `page` nodes with `source_path` under `/inbox/…` and
`team` set; edit a page in GROWI, wait one interval (or `POST /admin/api/connections/<name>/resync`),
the node body updates and its id changes while the old node reads `superseded`.
`GET …/<team>/api/graph` shows only that team.

---

### WP-G9 — Uploads and notes under a scope

**Goal:** the UI upload becomes a raw file; agent notes carry the team; deletes go to GROWI.

**Files:** `app.py`, `graph/librarian.py`, `graph/sync.py`.

**Steps**

1. `app.py` `create_document` (2266–2289) becomes:

   ```python
   _UPLOAD_NAME = re.compile(r"[^A-Za-z0-9_.\-぀-ヿ一-鿿]+")


   @app.post("/api/document")
   async def create_document(payload: DocumentBody) -> dict:
       """Drop the markdown into raw/<team>/uploads/ and let sync_raw carry it to GROWI."""
       from graph.sync import commit_raw

       team = _require_team()
       stem = Path(payload.document_name or payload.title or "upload").stem
       stem = _UPLOAD_NAME.sub("-", stem).strip("-") or f"upload-{uuid.uuid4().hex[:8]}"
       rel = f"{team}/uploads/{stem}.md"
       target = PROJECT.raw_file(rel)
       target.parent.mkdir(parents=True, exist_ok=True)
       await asyncio.to_thread(target.write_text, payload.body, "utf-8")
       await asyncio.to_thread(commit_raw, PROJECT, f"upload: {rel}")
       job = await _enqueue("sync_raw", {"ingest_mode": (payload.chunk_options or {}).get("ingest_mode")})
       await _enqueue("sync_growi", {})
       return {**job, "raw": rel}
   ```

   `DocumentBody` keeps `body`, `title`, `document_name`, `chunk_options`; delete
   `source_path` and `source_ranges` (accepted-and-ignored fields are a lie). Delete
   `CHUNK_LINE_THRESHOLD`.

2. `delete_document` endpoint (2236) — unchanged shape; the librarian job changes:
   `Librarian.delete_document(document_name, node_ids)` (1241): when `document_name`
   starts with `/` (a GROWI document path) trash our pages there before deleting nodes:

   ```python
           if document_name and document_name.startswith("/"):
               from .growi import GrowiClient, GrowiPublisher

               connection = self.growi_connection()
               if connection is not None:
                   publisher = GrowiPublisher(GrowiClient(connection.url, connection.api_token), connection)
                   asyncio.run(publisher._trash_under(document_name, keep=set()))
                   self._git_rm_upload(document_name, connection)
   ```

   with

   ```python
       def _git_rm_upload(self, document: str, connection: Any) -> None:
           """A document under <write_path>/<team>/uploads/ maps 1:1 to raw/<team>/uploads/<name>.md."""
           from .project import Project
           from .sync import commit_raw

           base = "/" + connection.write_path.strip("/")
           rel = document[len(base):].strip("/") if document.startswith(base + "/") else ""
           parts = rel.split("/")
           if len(parts) != 3 or parts[1] != "uploads":
               return
           project = Project(Path(self.settings.data_root))
           raw = project.raw / parts[0] / "uploads" / f"{parts[2]}.md"
           if raw.exists():
               raw.unlink()
               commit_raw(project, f"delete: {parts[0]}/uploads/{parts[2]}.md")
   ```

   (A mount-derived document is trashed in GROWI and dropped from the index; it comes back
   the next time the mount file changes. Say so in the UI tooltip.)

3. `create_exogenous` already carries `team` (WP-G6). Nothing else.

**Verify:** in `/prefix/<team>/`, upload a markdown from the UI → `raw/<team>/uploads/…`
committed, pages appear in GROWI under `/inbox/<team>/uploads/<stem>/`, nodes appear in
that scope. Delete it from the UI → GROWI trash has the pages, raw file gone, nodes gone.
In `/prefix/all/` the upload button answers `no_team`.

---

### WP-G10 — Delete the legacy sqlite-file world (D10, F16)

**Goal:** one layout, one ingest path, no dead code. Pure deletion; nothing new.

**Files:** `app.py`, `graph/librarian.py`, `graph/core.py`, `Dockerfile`, `README`s,
`frontend/src/pages/admin/AdminApp.jsx`, `frontend/src/components/UploadView.jsx`.

**Delete in `app.py`** (line numbers from `361fdbb`; use `grep -n` after the earlier WPs):
`MAX_SQLITE_UPLOAD_BYTES`, `ADMIN_DB_LOCK`, `_open_sqlite_ro`, `_table_exists`, `_columns`,
`_count_sql`, `_validate_sqlite_file`, `_migrate_sqlite_file`, `_db_summary_from_path`,
`_db_summary` (615–914); `admin_list_dbs`, `admin_get_db`, `admin_create_db`,
`admin_upload_db`, `admin_delete_db`, `AdminDbCopyRequest`, `AdminDbRenameRequest`,
`_next_copy_name`, `admin_copy_db`, `admin_rename_db` (1210–1655); `IngestBody`,
`RecondBody`, `CascadingUpdateBody`, `/api/recon`, `/api/cascading-update`, `/api/ingest`,
`INGEST_ROOT`, `_path_within_ingest_root`; the `File, UploadFile, Query, sqlite3, tempfile`
imports if now unused. Add instead:

```python
@app.get("/admin/api/status")
async def admin_status(_: str | None = Header(default=None, alias="X-Admin-Password")):
    _require_admin(_)
    connection = _growi_connection()
    return {
        "stage": stages.get(ALL_SCOPE, "not_started"),
        "error": errors.get(ALL_SCOPE),
        "data_root": str(DATA_ROOT),
        "teams": sorted(_known_scopes() - {ALL_SCOPE}),
        "connection": _growi_public_summary(connection) if connection else None,
        "urls": {name: _db_url(name) for name in sorted(_known_scopes())},
    }


@app.post("/admin/api/sync")
async def admin_sync(_: str | None = Header(default=None, alias="X-Admin-Password")):
    _require_admin(_)
    stack = STACKS.get(ALL_SCOPE)
    if stack is None:
        raise HTTPException(status_code=503, detail=_not_ready_detail(ALL_SCOPE))
    first = await stack["librarian"].enqueue("sync_raw", {})
    await stack["librarian"].enqueue("sync_growi", {})
    return _job_response(first)
```

**Delete in `graph/librarian.py`** — entry points first, then whatever they leave dead:

1. `_dispatch_job` branches `create_document`, `cascading_update`, `ingest_md_output`,
   `chunk_and_ingest`, and the methods behind them: `chunk_and_ingest` (1700–1815),
   `ingest_md_output` (1629–1698), `cascading_update` (1987–2015), `recon` (2136–2168),
   `create_document_node` (1401; its only caller was the `create_document` branch at 560).
2. Now dead by construction — confirm each with `grep -n "<name>(" graph/*.py app.py mcp_server.py`
   showing only the `def` line, then delete: `create_document_nodes`, `_prepare_document`,
   `_revise_document_node`, `_link_document`, `_DocumentIngest`, `_load_md_output`,
   `_load_old_manifest_output`,
   `_load_new_planning_docs_output`, `_parse_ranges`, `_split_frontmatter`, `_doc_sort_key`,
   `_canonical_doc_name`, `_humanize`, `_document_name`, `_read_json`, `_source_version_for_nodes`. Keep
   `_title_from_markdown`, `_chain_edges`, `_prepare_and_link_nodes`, `_run_ingest_phase`
   (used by `sync_growi`).
3. Snapshots: `_SNAPSHOT_JOB_TYPES = set()`; delete `_apply_job_snapshotted`, `_snapshot_db`,
   `_restore_db`, `_discard_snapshot`, `_recover_incomplete_ingest_if_needed` and its call in
   `__init__` (~line 260); `_apply_job` keeps the incremental branch from WP-G3.
4. Tests: `tests/test_pages_wire.py` and `tests/test_ingestion_concurrency.py` exercise the
   deleted entry points — delete both modules (the writers they cover keep `test_pages_*`,
   `test_wiki_*`, `test_writers`). Run the suite.

**`graph/core.py`:** delete `Settings.database_path`'s stale default comment; `WIKI_DB` env
stays as an override only if `graph/cli.py` still uses it (it does: leave it).

**`Dockerfile`:** the start script exports `WIKI_DB_DIR`/`WIKI_DEFAULT_DB` — replace with
`WIKI_DATA_ROOT=${WIKI_DATA_ROOT:-/data}` for both tmux windows; `EXPOSE` unchanged; add
`VOLUME /data`. `mkdir -p ${APP_HOME}/.wiki` lines → `mkdir -p /data`.

**Frontend:** `UploadView.jsx` — remove the "sqlite" wording, keep the markdown upload
(it now writes to `raw/`), keep Sync + zip buttons, disable upload with tooltip when
`GET /api/scopes` says `current === all`. `AdminApp.jsx` — see WP-G11.

**Verify:** `grep -rn "sqlite" app.py graph/librarian.py | grep -v "graph.sqlite\|engine.sqlite\|sqlite3"` → nothing;
suite green; `npm run build` clean.

---

### WP-G11 — Frontend: scope switcher, GROWI links, admin (F9, F16)

**Goal:** a user can pick a scope, jump to GROWI for any page, trigger a sync; an admin can
register/test the GROWI connection and see the engine state.

**Files:** `frontend/src/api.js`, `frontend/src/data/growi.js`,
`frontend/src/components/layout/TopBar.jsx`, `frontend/src/components/DocSidebar.jsx` (or
wherever a node is rendered with its source), `frontend/src/components/QueueView.jsx`,
`frontend/src/pages/admin/AdminApp.jsx`.

**Steps**

1. `api.js`: add `scopes: () => req('/api/scopes')`, `growi: () => req('/api/growi')`;
   `syncProject` stays (`POST /api/sync`).
2. `data/growi.js` `growiLinkFor`: drop the `growi:` check — the condition becomes
   `path.startsWith('/')` (every wiki node is a GROWI page now; agent notes have no `source_path`).
3. `TopBar.jsx`: a `<select>` fed by `api.scopes()`; on change
   `window.location.assign(urls[value])`. Show `current` as the selected value.
4. Node rendering (`DocSidebar.jsx` / `MarkdownView.jsx`): where a node's source is shown,
   render `growiLinkFor(node, connection)` as "GROWIで開く / 編集" links; `connection` from
   `api.growi()` loaded once in `useWorkspace.js`.
5. `QueueView.jsx`: stage labels for `sync_raw` (`convert`, `sync`, `write`, `publish`) and
   `sync_growi` (`growi_sync`). The `chunk_and_ingest` labels are dead — delete.
6. `AdminApp.jsx`: delete the database list/create/upload/copy/rename/delete UI. Keep the
   login and the shell. New content, top to bottom:
   - **Engine**: `GET /admin/api/status` → stage badge, error, data root, team list with
     links (`urls`), a **Sync now** button (`POST /admin/api/sync`).
   - **GROWI connection**: the single connection from `status.connection` — form with
     `url`, `api_token` (write-only field; `has_token` shows a dot), `mode`, `root_path`,
     `write_path`; **Save** → `POST /admin/api/connections/{name}` (name fixed to
     `local` unless `WIKI_GROWI_NAME` differs — read it from `status.connection.name` or
     default `local`), **Test** → `POST …/test`, **Resync** → `POST …/resync`,
     **Detach** → `DELETE …` with a confirm ("your GROWI is untouched; the index is dropped").
   - Existing strings live in `AdminApp.jsx`'s `t` object; add Japanese/English pairs the
     same way the current keys are done.

**Verify:** `npm run build`; open `/prefix/all/`, switch to a team, the graph shrinks;
click a page's GROWI link → opens the page; admin page registers a token and shows
`stage: ready`.

---

### WP-G12 — Compose, env, README, and moving your current `data/`

**Goal:** the stack runs from one `docker compose up`; your existing folder keeps its wiki
output and needs no LLM re-run.

**Files:** `growi-stack/docker-compose.yml`, `llm-wiki-dist/.env`, `docs/README.md`.

**Steps**

1. `growi-stack/docker-compose.yml`: unchanged (mongo + growi). No vector service:
   vectors live in `graph.sqlite`. Elasticsearch stays out: search stays on our FTS5.

2. `.env` (llm-wiki-dist):

   ```
   WIKI_DATA_ROOT=/mnt/common/Code/llm-wiki-dist/data
   WIKI_PARSER_BASE_URL=http://localhost:8000        # doc-parser; leave empty if raw/ is delivered by hand
   WIKI_GROWI_NAME=local
   WIKI_SYNC_INTERVAL_SECONDS=300
   WIKI_SECRET_KEY=…                                  # unchanged
   WIKI_EMBED_* / WIKI_RERANK_*                       # unchanged
   ```

   Delete `WIKI_GROWI_ENABLED`, `WIKI_VECTOR_BACKEND`, `WIKI_DB_DIR`, `WIKI_DEFAULT_DB` wherever they appear.

3. Migrate `data/` (once, app stopped):

   ```bash
   cd /mnt/common/Code/llm-wiki-dist/data
   rm -f graph.sqlite graph.sqlite-wal graph.sqlite-shm       # derived; rebuilt from GROWI
   rm -rf raw/data                                            # stray empty folder
   rm -f metadata/last_sha                                    # first sync republishes everything…
   # …but does NOT re-run the writers: every wiki/<x>/<y>/_planning/manifest.json carries
   # source_sha256 of the raw file it was written from (up_to_date() reads it).
   ```

   Your mount folders are `csv docx pdf pptx xlsx` — those become the teams. Rename them
   to real team names **before** the first run if you want (rename in `mount/` and `raw/`
   together, `git -C raw add -A && git -C raw commit -m rename`, then delete the matching
   `wiki/<old>` folders so they are rewritten under the new team… or keep the format names).

4. First run:

   ```bash
   cd growi-stack && docker compose up -d                     # mongo, growi
   cd ../llm-wiki-dist && uv run uvicorn app:app --port 51023
   # register the connection if engine.sqlite was recreated:
   curl -X POST -H 'X-Admin-Password: …' -H 'Content-Type: application/json' \
     localhost:51023/agent/llm-wiki/admin/api/connections/local \
     -d '{"url":"http://localhost:3000","api_token":"…","mode":"attach","write_path":"/inbox"}'
   curl localhost:51023/agent/llm-wiki/all/api/ready          # stage: ready
   ```

   Startup enqueues `sync_raw` (publishes every wiki folder into `/inbox/<team>/…`) then
   `sync_growi` (indexes them). Watch `…/all/api/write-jobs`.

5. `docs/README.md`: replace the "Any URL segment picks/creates a db" paragraph and the env
   table with: `WIKI_DATA_ROOT` layout (1.1), scopes (1.2), the env list above, the
   compose command, and "the wiki is GROWI at `<url>/inbox/<team>/…`; llm-wiki is the brain".

**Verify (done when):** delete `data/graph.sqlite*` + restart → the engine rebuilds from
GROWI (nodes, FTS, vectors via `bootstrap`) with no LLM writer run, and
`ask` answers as before. Drop a `.docx` into `mount/<team>/`, wait one interval: it is in
GROWI and answerable in `/prefix/<team>/`. Edit a page in GROWI: the answer changes.

---

## Appendix A — Environment after this plan

| Variable | Meaning | Default |
|---|---|---|
| `WIKI_DATA_ROOT` | the project root (1.1) | `data` |
| `WIKI_ENGINE_DB` | registry sqlite | `<root>/engine.sqlite` |
| `WIKI_GROWI_NAME` | which registry row this engine mirrors | the only row |
| `WIKI_PARSER_BASE_URL` | doc-parser; empty = `mount/` is not converted | empty |
| `WIKI_SYNC_INTERVAL_SECONDS` | timer for `sync_raw`+`sync_growi`; 0 = button only | 300 |
| `WIKI_PREFIX`, `WIKI_ADMIN_PASSWORD`, `WIKI_SECRET_KEY`, model/embed/rerank vars | unchanged | |
| ~~`WIKI_DB_DIR`, `WIKI_DEFAULT_DB`, `WIKI_GROWI_ENABLED`, `WIKI_VECTOR_BACKEND`, `QDRANT_URL`, `WIKI_QDRANT_*`, `WIKI_INGEST_ROOT`, `WIKI_CHUNK_THRESHOLD_LINES`, `MCP_ALLOW_NEW_WIKIS`~~ | deleted | |

## Appendix B — HTTP surface after this plan

| Route | Change |
|---|---|
| `{P}/` → `{P}/all/` | redirect target |
| `{P}/{scope}/api/scopes` | new |
| `{P}/{scope}/api/growi` | new |
| `{P}/{scope}/api/document` | writes `raw/<team>/uploads/`, 400 `no_team` in `all` |
| `{P}/{scope}/api/document/delete` | trashes GROWI pages + raw upload |
| `{P}/{scope}/api/sync` | enqueues `sync_raw` then `sync_growi` |
| `{P}/{scope}/api/wiki.zip` | team-filtered zip |
| `{P}/admin/api/status`, `{P}/admin/api/sync` | new |
| `{P}/admin/api/connections/*` | kept |
| ~~`{P}/admin/api/dbs/*`, `api/ingest`, `api/recon`, `api/cascading-update`~~ | deleted |
| everything else (`ask`, `ask/stream`, `ask/realtime/stream`, `search`, `graph`, `node/*`, `exogenous`, `write-jobs`, `settings`) | unchanged, now scope-filtered |

## Appendix C — Deliberate ceilings (`ponytail:` comments to leave in the code)

- **`sync_growi` refetches every page of a touched document** (`get_page` per sibling). A
  40-page folder with one edit costs 40 GETs against a local GROWI. Fetch only changed
  pages and reuse cached bodies for the rest when this shows up in the job time.
- **Scope = TEMP views.** Every scoped connection re-creates three views; a scope cannot be
  changed on a live connection. Fine for a handful of teams; move to a `WHERE team IN (...)`
  parameter if you ever need "these three teams".
- **Listing drift.** GROWI's `pages/list` is sorted by `updatedAt desc`; an edit during
  paging can hide one page until the next poll. Acceptable at a 5-minute cadence.
- **One GROWI per engine.** Second GROWI = second stack.
- **sqlite-vec is brute force.** ~70 ms over 7k search-item vectors; budget ~100k vectors
  before realtime feels it. The `VectorIndex` seam is the swap point (Qdrant or LanceDB).
- **`team=NULL` for pages outside the write path.** Human pages under `/Sandbox` show only
  in `all`. If teams want their hand-written trees scoped, give each team a folder under
  `write_path` and put the pages there.
- **FTS5 default tokenizer.** `unicode61` treats a Japanese sentence as one token; BM25
  works on `search_items` text but is weak on long CJK runs. Try `tokenize='trigram'` on
  the two FTS tables (a schema version bump + rebuild) when keyword search feels blind.
- **Timer, not webhook.** GROWI has no generic outbound webhook; 5-minute polling is the
  ceiling on freshness. `POST /api/sync` is the manual override.

## Appendix D — Questions only you can answer

1. Team folder names: rename `csv/docx/pdf/pptx/xlsx` under `mount/` before the first run,
   or accept format-named teams for now?
2. `attach` + `/inbox` keeps every machine page in one tree that humans may move. Do you
   want `own` mode with `write_path=/wiki` on this GROWI instead (teams then live at
   `/wiki/<team>`), since the instance is yours?
3. Who holds `WIKI_SECRET_KEY` and the GROWI admin token in the deployed container?
4. Should agent notes ever be published to GROWI (D9 says no for now)?
