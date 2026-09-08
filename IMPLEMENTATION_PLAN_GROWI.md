# Implementation plan — GROWI integration + page assembly

Companion to `PLAN_GROWI.md`. That document says **what** and **why**. This one says
**exactly what to edit**, in what order, and how to prove each step works.

Written for an implementer agent working one work package at a time. A human should be
able to read it too, so it explains itself rather than assuming context.

---

## How to use this document

1. Read **Hard constraints** first. They override anything else, including the design in `PLAN_GROWI.md`. If a constraint and a design idea conflict, the constraint wins and you stop and say so.
2. Do **one work package at a time**, in order.
3. Each work package has: **Goal → Files → Steps → Verify → Revert**. Do not start the next one until Verify passes.
4. Every work package must leave the app working. If it does not, you have gone too far in one step.
5. **Commit after each work package**, once its Verify passes. See **Checkpoints and committing**.

---

## Reference material — read the source, do not guess

You will need to look things up. These are the places to look. **When this plan and the
official documentation disagree, the documentation wins** — this plan was written on
2026-09-08 and GROWI moves.

### GROWI — official documentation

| Link | Use it for |
|---|---|
| <https://docs.growi.org/en/> | Documentation root. Start here when lost. |
| <https://docs.growi.org/en/api/> | API overview: which versions exist (v1 and v3). |
| <https://docs.growi.org/en/api/rest-v3.html> | **REST API v3** — the API this plan uses for all reads and writes. |
| <https://docs.growi.org/en/api/rest-v1.html> | v1, older. Only if v3 lacks something you need. |
| <https://docs.growi.org/en/dev/plugin/overview.html> | **Plugin types.** Confirms script plugins may inject UI and call external APIs — that is how the chat panel gets into GROWI (WP-15 territory). |
| <https://docs.growi.org/en/admin-guide/management-cookbook/external-notification.html> | Global notification / outgoing webhook, the faster alternative to polling in WP-13. |
| <https://docs.growi.org/en/admin-guide/management-cookbook/setup-ai.html> | GROWI's own AI feature. Read it to understand what we are *replacing* — it requires OpenAI's hosted Vector Store and cannot run on an internal network. |
| <https://docs.growi.org/en/guide/features/mcp-server.html> | Official GROWI MCP server. Recommended v7.3+. |
| <https://growi.cloud/help/en/guide/features/lsx.html> | `$lsx()` — renders a child-page list. Used on chapter pages so navigation builds itself. |

### GROWI — source code (the authority when docs are thin)

The docs are incomplete in places. The source is not. Use `gh` to read it without cloning:

```bash
# list the API v3 route modules
gh api repos/growilabs/growi/contents/apps/app/src/server/routes/apiv3 --jq '.[].name'

# read one route file
gh api repos/growilabs/growi/contents/apps/app/src/server/routes/apiv3/page/create-page.ts \
   --jq '.content' | base64 -d
```

| Path in `growilabs/growi` | What is there |
|---|---|
| `apps/app/src/server/routes/apiv3/page/` | **single-page** operations: `create-page.ts`, `update-page.ts`, `check-page-existence.ts`, `get-page-info.ts`, `index.ts` (route registrations) |
| `apps/app/src/server/routes/apiv3/pages/index.js` | **collection** operations: list, recent, rename, duplicate, delete |
| `apps/app/src/server/routes/apiv3/healthcheck.ts` | health endpoint used in WP-8 |
| `apps/app/src/server/models/page.ts` | the Mongo `Page` model — read before touching Mongo in WP-13 |

Other repositories:

| Repo | Use |
|---|---|
| <https://github.com/growilabs/growi> | main monorepo |
| <https://github.com/growilabs/growi-docker-compose> | the official compose stack; copy its Elasticsearch Dockerfile |
| <https://github.com/growilabs/growi-mcp-server> | official MCP server; useful prior art for the API client in WP-8 |
| <https://github.com/growilabs/growi-plugin-lsx> | the `$lsx` plugin, and a worked example of a GROWI plugin |

### GROWI REST API v3 cheat sheet — verified against source

Base path is `/_api/v3`. Note **`/page` is singular** for single-page operations and
**`/pages` is plural** for collection operations. This trips people up.

| Method | Path | Key body / query fields |
|---|---|---|
| `POST` | `/_api/v3/page/` | `path` **or** `parentPath`, `body`, `grant` (int 0–5), `pageTags[]`, `wip`, `origin` (`"view"`\|`"editor"`) |
| `PUT` | `/_api/v3/page/` | **`pageId`**, **`revisionId`**, `body`, `grant`, `origin`, `wip` |
| `GET` | `/_api/v3/page/exist` | existence check |
| `GET` | `/_api/v3/page/info` | page metadata |
| `PUT` | `/_api/v3/page/:pageId/publish` · `/unpublish` | publish state |
| `GET` | `/_api/v3/pages/list` · `/recent` | list pages |
| `GET` | `/_api/v3/pages/subordinated-list` | descendants of a path |
| `POST` | `/_api/v3/pages/rename` · `/duplicate` · `/delete` | collection mutations |
| `GET` | `/_api/v3/healthcheck` | liveness |

> **The single most important detail on this page.** `PUT /_api/v3/page/` requires
> **`revisionId`** alongside `pageId`. That is optimistic concurrency: if somebody edited
> the page since you read it, your stored `revisionId` is stale and the update **fails**.
> That is a feature — it stops us silently overwriting a human's edit.
>
> Handle it explicitly: on a revision conflict, **re-fetch the page, re-apply our marked
> sections onto the current body, and retry once.** Never force. If the retry also
> conflicts, record the error on the connection and move on.
>
> This is exactly why `growi_pages.revision_id` exists in the WP-9 schema.

Authentication is an API token from the user settings screen. Confirm the exact
header-versus-query form against the v3 docs before writing WP-8 — do not assume.

### Qdrant (only needed for WP-14)

| Link | Use |
|---|---|
| <https://qdrant.tech/documentation/manage-data/multitenancy/> | **Read this before writing any Qdrant code.** The one-collection + `is_tenant` pattern this plan mandates. |
| <https://qdrant.tech/documentation/concepts/indexing/> | payload indexes and the `hnsw.m` / `payload_m` settings |
| <https://qdrant.tech/documentation/concepts/filtering/> | filter syntax for the tenant filter |

### This repository

| File | Why you care |
|---|---|
| `PLAN_GROWI.md` | the design and the reasoning. Read Part 2 and Part 3 before Track A and Track B. |
| `llm-wiki-dist/HANDOFF.md` | what the realtime pipeline expects from the data. Explains why C4 exists. |
| `llm-wiki-dist/SSE_SPEC.md` | the streaming event contract, if you ever touch `app.py` streaming |
| `llm-wiki-dist/tests/test_ingestion_concurrency.py` | **copy this test style.** |

---

## Orientation — what exists today

If you have never opened this codebase, read this before WP-1. Ten minutes here saves a day.

### The four actors

`app.py` is transport only — HTTP in, actor method out. The real work is four objects,
built per wiki in `_build_stack()` (`app.py:132`):

| Actor | File | Owns |
|---|---|---|
| `ModelGateway` | `graph/gateway.py` | the chat LLM, the embedder, the reranker, `Settings` |
| `GraphStore` | `graph/store.py` | SQLite. Two instances: one writable, one read-only |
| `Librarian` | `graph/librarian.py` | **all writes.** A job queue plus background enrichment |
| `Researcher` | `graph/researcher.py` | **all reads.** Search and the `ask()` agent |

**Everything you write in Track A hangs off `Librarian`. Everything in Track B hangs off
`app.py` and a new module.** `Researcher` is never edited (constraint C4).

### How a write happens

Every mutation goes through one queue. There is no second path.

```
HTTP POST /api/document
   -> app.py  _enqueue("chunk_and_ingest", payload)
   -> Librarian.enqueue()            bounded queue, returns a WriteJob immediately
   -> Librarian._worker_loop()       one job at a time
   -> Librarian._run_job()
   -> Librarian._dispatch_job()      big if/elif on job.type   <-- you add branches here
   -> the actual method
```

Client polls `GET /api/write-jobs/{id}` for status. `job.progress` is a free-form dict the
job updates in place — use it, the UI already renders it.

`job.stop_event` is how cancellation works. Long loops must call `stop_check()` and raise
`JobCancelled`. Copy how `chunk_and_ingest` already does it.

### How one wiki is selected

`db_routing` middleware (`app.py:824`) peels `{PREFIX}/{db}/...` off the URL, sets the
`current_db` context variable, and every handler resolves its actor stack through
`_ready_stack()`. Stacks live in `STACKS: dict[str, dict]`, keyed by wiki name, built
lazily by `_bootstrap_db()`.

**This is the multi-tenant machinery Track B reuses.** In WP-11 the segment resolves to a
GROWI connection instead of a `.sqlite` path. Everything else is untouched.

### The ingest path you are extending

```
app.py create_document
   |  body > 300 lines?  (CHUNK_LINE_THRESHOLD)
   |
   +-- yes -> job "chunk_and_ingest"       librarian.py:1493
   |            run_chunk_pipeline()        chunk.py:2589 -> arun_chunk_pipeline:2610
   |              plan_concept_files_streaming()   chunk.py:1903   <- pass 1, KEEP
   |              render_concept_files()           chunk.py:2158   writes docs/*.md
   |              enrich_concept_plan()            chunk.py:2369   titles + headers
   |              write_{coverage,metadata}_json() chunk.py:2451/2478
   |            ingest_md_output(out_dir)   librarian.py:1421
   |              _load_md_output -> _load_new_planning_docs_output   librarian.py:3427
   |              per node: _ingest_one     librarian.py:1767
   |
   +-- no  -> job "create_document" -> create_document_node()  librarian.py:1307
```

`_ingest_one` is where the cost is: summary call, keywords call, claims call, bridge-probe
call, three embeddings, kNN, then LLM edge calls. **Roughly ten model calls per node.**
That is why assembling ~120 pages instead of ~600 chunk-nodes is most of the speedup.

### Vocabulary used throughout this plan

| Term | Means |
|---|---|
| **chunk** | a verbatim slice of the source, cut by line number. Never rewritten. |
| **page** | an assembled wiki article: several chunks pasted verbatim under headings |
| **shelf** | the list of empty pages, decided before any routing |
| **routing** | deciding which page a chunk belongs on |
| **stitching** | adding small generated connective text, via typed operations only |
| **parked** | a chunk the router could not confidently place |
| **connection** | one registered GROWI instance |
| **channel** | a vector namespace: `body`, `summary`, `bridge`, `search_item` |
| **attach mode** | pointed at somebody's existing GROWI. Append only. |
| **own mode** | we created this GROWI, may write anywhere under `root_path` |

---

## Hard constraints

These are the rules of the job. Breaking one is worse than not finishing.

### C1 — Additive, not a rewrite

New behaviour goes in **new files**. Existing files get **small insertions** only.

The budget for edits to existing files, across this entire plan:

| File | Allowed change |
|---|---|
| `graph/core.py` | add settings fields, add one enum value. **No edits to existing fields.** |
| `graph/librarian.py` | add job-type branches, one `if` in `chunk_and_ingest`. **No rewrites of existing methods.** |
| `graph/chunk.py` | **append** new functions. Do not modify `plan_concept_files_streaming`, `render_concept_files`, `arun_chunk_pipeline`, or anything they call. |
| `graph/store.py` | add new methods and new tables. **Do not change existing tables or method signatures.** |
| `app.py` | add new endpoints and new models. Existing endpoints keep working unchanged. |

If a task seems to require rewriting an existing function, **stop and report it** instead
of doing it. There is almost always an additive way.

### C2 — Every new behaviour is behind a flag, and the default is today's behaviour

Nothing in this plan may change what the app does until somebody explicitly turns it on.

After every work package, a fresh install with no configuration must behave **exactly as
it does today**. This is checkable: ingest the same document before and after and compare
node counts.

### C3 — The page-assembly pipeline is an option, permanently

This is a product requirement, not a migration step.

The existing "one chunk = one node" pipeline (`ingest_mode="chunks"`) and the new
"chunks assembled into pages" pipeline (`ingest_mode="pages"`) **both stay in the codebase
forever**. The user must be able to switch back at any time, per wiki and per request.

Concretely:

- Never delete or modify `arun_chunk_pipeline`. The new pipeline is a **sibling function**, not a replacement.
- Never make the new path the default.
- Record the mode used in each document's manifest, so a wiki ingested in one mode is unaffected by later flag changes.
- Both modes must be able to coexist **in the same wiki**: document A ingested as chunks, document B ingested as pages, both searchable.

### C4 — Files you must not touch

Do not edit these. They are the valuable, working part of the system:

```
graph/researcher.py      graph/realtime.py
graph/vocab.py           graph/neighborhood.py
graph/gateway.py
```

If your change requires editing one of them, you have designed it wrong. The new pipeline
must produce data in the **shape these files already consume**.

### C5 — Reuse existing patterns; never invent a parallel one

The codebase already has a way to do most things. Find it and extend it.

| Need | Existing thing to reuse |
|---|---|
| background work with progress | `WriteJob` + `Librarian.enqueue()` + `job.progress` |
| "not ready yet" responses | `stages`, `errors`, `_not_ready_detail()`, `/api/ready` |
| lazy per-tenant startup | `STACKS`, `building`, `_ensure_building()`, `_bootstrap_db()` |
| hiding secrets from clients | `_SECRET_KEYS` + `_redact()` in `app.py` |
| cancelling long work | `job.stop_event` + `stop_check()` callbacks |
| bounded parallel startup | the semaphore in `lifespan()` (`app.py:279`) |

### C6 — Each work package is independently revertible

One package = one commit = one feature flag. Reverting the commit must fully remove the
behaviour with no leftovers.

Commit **only** when the package's Verify passes, the existing tests pass, and default
config behaves as before. See **Checkpoints and committing** for the branch, the commit
format and the phase order.

### C7 — Tests

Every work package adds tests to `llm-wiki-dist/tests/`. Follow the style of the existing
`tests/test_ingestion_concurrency.py` — `unittest`, `mock.patch` for LLM calls, no network.

**Never call a real model in a test.** Stub the LLM.

---

## The switch matrix

Everything this plan adds, and how to turn it off.

| Flag | Where | Default | Off means |
|---|---|---|---|
| `ingest_mode` | `Settings` | `"chunks"` | today's pipeline, unchanged |
| `page_stitch` | `Settings` | `False` | assemble pages but write no generated prose at all |
| `vector_backend` | `Settings` | `"sqlite"` | vectors stay in `sqlite-vec` |
| `growi_enabled` | env `WIKI_GROWI_ENABLED` | `false` | no GROWI code loads, no new endpoints |

`Settings` is already editable at runtime through `PATCH /api/settings`, so `ingest_mode`
can be flipped live per wiki without a restart. That is deliberate — it is how the user
switches back.

---

## Map of the work

```
  TRACK A — page assembly            TRACK B — GROWI
  (works on today's SQLite,          (independent of Track A;
   no GROWI needed)                   can start any time)

  WP-1  flags + plumbing             WP-8   growi client (read-only)
  WP-2  shelf builder                WP-9   connection registry
  WP-3  router                       WP-10  admin endpoints
  WP-4  assembler + writer           WP-11  routing by connection
  WP-5  stitcher (optional)          WP-12  publish to GROWI
  WP-6  wire into chunk_and_ingest   WP-13  read from GROWI
  WP-7  VectorIndex seam             WP-14  Qdrant backend

  WP-0 (optional, do first): merge benchmark speedups
  WP-15 (last, optional): deletions
```

**Track A is worth shipping alone.** Do it first. Do not start Track B until WP-6 verifies.

---

## Checkpoints and committing

### The three phases, in order

```
  PHASE 1        make the current ingest better        WP-0 .. WP-7
                 (no GROWI anywhere; ships on its own)
                          |
                          v
  PHASE 2        wire in GROWI                         WP-8 .. WP-14
                 (both storage models coexist)
                          |
                          v
  PHASE 3        delete what GROWI replaced            WP-15
                 (only after phase 2 has run for real)
```

**Never reorder these.** Phase 3 deletes code that Phase 2 must first prove is redundant.
Deleting before proving is how you end up with a broken wiki and no way back.

### Commit once per work package

**One work package = one commit.** Not one commit per file, not one commit for a whole
track.

A work package is committable only when **all four** of these hold:

1. its own **Verify** section passes
2. the existing test suite still passes
3. the new tests for that package pass
4. **default configuration still behaves exactly as before** (constraint C2)

If any one fails, fix it before committing. Do not commit "work in progress" — a
half-finished package in history destroys the property that makes this plan safe, which is
that **any single commit can be reverted cleanly**.

### Branch and commit format

Work on a branch off `dist`. Do not commit to `dist` directly.

```bash
git checkout dist
git checkout -b growi-integration
```

One commit per package, with the package id first so history reads as a checklist:

```
WP-3: route chunks onto shelf pages

Adds route_chunks() and absorb_parked() to graph/pages.py.
kNN to 5 candidate pages, then one small LLM call per chunk.
No existing file modified. Inert unless ingest_mode="pages".

Verify: tests/test_pages_router.py
```

Say **what changed**, **what is now possible**, and **what is still off by default**. The
last line matters most — it is how a reviewer confirms C2 at a glance.

### Tag the phase boundaries

```bash
git tag wp7-track-a-complete     # current ingest improved, no GROWI
git tag wp14-track-b-complete    # GROWI wired in, nothing deleted yet
```

These are the two points worth being able to return to instantly. `wp7` in particular is a
shippable product on its own: better ingest, no new infrastructure, no GROWI.

### Before Phase 3

**Do not start WP-15 until GROWI has been the source of truth for real work**, with real
users, for long enough to trust it. There is no deadline on deletion. Leaving the old code
in place costs nothing but disk.

When you do start, delete in small commits — one subsystem per commit — so a mistake is one
revert, not an archaeology project.

### If a package turns out to be wrong

Revert the commit. Do not patch forward on top of a broken package. That is the entire
reason for the one-package-one-commit rule.

---

## Key insight that keeps this small

Read this before writing any code. It is why Track A is ~1 new file instead of a refactor.

Today's flow:

```
chunk_and_ingest
   -> run_chunk_pipeline(...)          writes out_dir/docs/*.md
                                       writes out_dir/_planning/*.json
   -> ingest_md_output(out_dir)        reads those files -> Nodes
```

`ingest_md_output` does not care **how** the `.md` files were produced. It only cares
about the shape on disk.

So the new pipeline **writes the same shape**, with assembled pages instead of raw slices,
and everything downstream — node creation, embeddings, search items, edges, clustering,
`ask()`, realtime — works with **zero changes**.

Three facts confirmed in the code that make this work:

1. **`Node.source_ranges` is `list[tuple[int, int]]`** (`core.py:299`). A page can already carry many non-contiguous line ranges. **No schema change.**
2. **`_split_frontmatter` already parses frontmatter** (`librarian.py:3520`) even though today's writer never emits any, and **`_parse_ranges` already accepts a JSON list of pairs** (`librarian.py:3536`). So a page file can declare `source_lines: "[[1200,1305],[41002,41110]]"` and the loader reads it correctly. **No loader change.**
3. **`_build_search_items` splits `node.body` into 3000/512-char evidence units** with `start_char`/`end_char` (`librarian.py:1880`). A 600-line assembled page still gets pinpoint evidence retrieval. **No retrieval change.**

One thing you must **not** reuse: `assert_rendered_docs_match_source` (`chunk.py:2514`)
demands exact contiguous coverage of the source. Assembled pages are non-contiguous and
contain generated text, so it would always fail. The new pipeline **does not call it** and
has its own integrity check instead (WP-4).

---

# TRACK A — page assembly

## WP-0 — Port the benchmark speedups *(optional, independent)*

**Goal.** Make today's pipeline faster without changing what it produces.

**Files.** `graph/librarian.py`, `graph/core.py`, `graph/chunk.py`

### ⚠️ Do not merge the branch. Cherry-pick functions.

`benchmark` is **not** a superset of `dist`. It is a diverged older line of work. It gained
parallel ingest, but it **predates the neighbourhood cache and the vocabulary sheet**, so
merging it would *delete* code that `realtime.py` and `vocab.py` depend on — both C4 files.

Verified by diffing the two branches:

| Symbol | `dist` | `benchmark` | Used by |
|---|---|---|---|
| `Librarian.refresh_neighborhood` | ✅ | ❌ | writes `node_neighborhood`, read by realtime |
| `Librarian.rebuild_neighborhoods` | ✅ | ❌ | backfill |
| `Librarian.enqueue_neighborhood` | ✅ | ❌ | enrichment job |
| `Librarian._bootstrap_neighborhoods` | ✅ | ❌ | startup catch-up |
| `GraphStore.set_node_neighborhood` / `get_node_neighborhoods` / `count_node_neighborhoods` / `delete_node_neighborhoods` | ✅ | ❌ | the `node_neighborhood` table |
| `GraphStore.vocabulary_rows`, `nodes_fingerprint` | ✅ | ❌ | **`vocab.py`** — ASR word fixing |
| `GraphStore.get_edges_for_nodes`, `get_nodes_by_source_path` | ✅ | ❌ | realtime subgraph build |
| `Settings.rerank_timeout_seconds`, `document_name` | ✅ | ❌ | gateway, ingest |

`researcher.py:70` imports `neighbors_for_seeds` and `realtime.py:52` imports
`NeighborRef` from `graph/neighborhood.py`. Those readers stay; deleting the writer leaves
an empty cache and silently degrades realtime.

**So: `git merge benchmark` is forbidden. Copy individual functions.**

Get a read-only reference copy without switching branches:

```bash
mkdir -p /tmp/bench
git show refs/heads/benchmark:llm-wiki-dist/graph/librarian.py > /tmp/bench/librarian.py
git show refs/heads/benchmark:llm-wiki-dist/graph/core.py      > /tmp/bench/core.py
git show refs/heads/benchmark:llm-wiki-dist/graph/chunk.py     > /tmp/bench/chunk.py
```

### What to port, in this order

Line numbers are in `/tmp/bench/librarian.py`. They will drift; search by name.

**Step 0.1 — settings** (`graph/core.py`, additive)

```python
    ingest_concurrency: int = 4
    recluster_every: int = 10      # 0 = caller runs one explicit refresh at the end
```

Do **not** port `decompose_query` or `decompose_max_queries` — they are researcher-side and
touching `researcher.py` violates C4.

**Step 0.2 — the state dataclass** (module level, `librarian.py:124` in benchmark)

`class _DocumentIngest` — the per-document working state carried across the three phases.
Copy it verbatim, right after the existing module-level dataclasses.

**Step 0.3 — locks and counters** (inside `Librarian.__init__`)

```python
        self._schema_lock = threading.Lock()   # DDL + cached embed dim
        self._cluster_lock = threading.RLock() # clustering is a read-modify-write
```

`RLock`, not `Lock` — the throttle check calls through to the refresh while already
holding it. Also move `_recluster_every` and add `_ingest_concurrency` so **both** the
background and inline paths read them (in `dist` it is set only in one branch).

**Step 0.4 — the eight methods**

Port these top-level `Librarian` methods, in this order:

| Method | benchmark line | Notes |
|---|---|---|
| `_maybe_recluster_locked` | 773 | split out of `_maybe_recluster`; `0` means "never, caller does it" |
| `_prepare_document` | 1445 | phase 1 for one document |
| `_revise_document_node` | 1520 | supersede/stale bookkeeping |
| `_link_document` | 1551 | phase 3 for one document |
| `_prepare_and_link_nodes` | 1571 | the node-level three-phase runner |
| `_run_ingest_phase` | 1419 | the generic bounded-parallel phase runner |
| `create_document_nodes` | 1342 | **the entry point** |
| `_prepare_node`, `_link_node`, `_persist_node_with_candidates` | 2689, 2700, 2676 | small helpers the above call |

`prepare`, `link`, `post_enrich`, `fill_pending` and `apply_supersessions` are **inner
functions** of the methods above. They come along with their parent — do not port them
separately.

**Step 0.5 — make the old entry point a wrapper**

This is the only edit to an existing method, and it must stay this small:

```python
    def create_document_node(self, body, title=None, document_name=None,
                             source_path=None, source_ranges=None) -> Node:
        """Thin wrapper so single and batch paths share one implementation."""
        return self.create_document_nodes([{
            "body": body, "title": title, "document_name": document_name,
            "source_path": source_path, "source_ranges": source_ranges,
        }])[0]
```

**Step 0.6 — the constant**

`_EDGE_GROUP_SIZE` 4 → 8. One line. It halves the dominant per-node cost.

**Step 0.7 — chunk planning concurrency**

`plan_concept_files_streaming` in benchmark takes `concurrency: int | None = None`
(benchmark `chunk.py:1919`) and derives `worker_count` from it, defaulting to the module
`CONCURRENCY`. Port the parameter and the `worker_count` line only. **Do not port the rest
of benchmark's `chunk.py`** — it has diverged.

### Imports you will need

`from concurrent.futures import ThreadPoolExecutor`, and `Sequence` added to the `typing`
import. Benchmark also dropped `Iterable`; leave `dist`'s imports alone otherwise.

### Why the three phases are safe to parallelise

Worth understanding before you trust it. `GraphStore` hands every thread its own SQLite
connection, WAL serialises the small writes, the LLM client is used only through stateless
one-shot helpers, and token usage is thread-local. Only two things are genuinely shared:
schema setup (mutates the cached embedding dimension and issues DDL) and clustering (reads
the whole graph and rewrites every node's cluster). Those are what the two locks guard.

Ordering is deterministic because phase 2 (revise) walks in the **caller's document
order**, not thread-completion order. Nothing is linked until everything is prepared, so
two documents ingested together can link to each other — which a serial ingest only manages
in one direction.

### Verify

```bash
# 1. baseline BEFORE the port
python -m graph.cli ingest tests/fixtures/sample.md --db /tmp/before.sqlite
sqlite3 /tmp/before.sqlite "select id,title from nodes order by id" > /tmp/before.txt

# 2. after the port
python -m graph.cli ingest tests/fixtures/sample.md --db /tmp/after.sqlite
sqlite3 /tmp/after.sqlite  "select id,title from nodes order by id" > /tmp/after.txt

diff /tmp/before.txt /tmp/after.txt     # must be EMPTY
```

Adjust the CLI invocation to whatever `graph/cli.py` actually exposes — read it first.

Also confirm, and this is the part people forget:

- [ ] node ids, edge count and titles identical
- [ ] wall clock noticeably lower
- [ ] **`select count(*) from node_neighborhood` is still non-zero** — proves you did not delete the neighbourhood writer
- [ ] a realtime request still returns `plan` and `level` events
- [ ] `Researcher.vocabulary()` still builds (it calls `store.vocabulary_rows`)

### Revert

Revert the commit. Nothing later in this plan depends on WP-0.

> **If the branch has drifted too far to port cleanly, skip this package entirely.** Track A
> delivers most of the speedup on its own by running enrichment over ~120 pages instead of
> ~600 chunks. WP-0 is a bonus, not a prerequisite.

---

## WP-1 — Flags and plumbing

**Goal.** Add the switches. Change no behaviour at all.

**Files.** `graph/core.py` (edit), `app.py` (edit)

### Step 1.1 — Add settings

In `graph/core.py`, in `class Settings`, **append** these fields near the ingest settings
(around line 90, after `entity_dedup`). Do not reorder existing fields.

```python
    # --- page assembly (PLAN_GROWI.md Part 2) -----------------------------
    # "chunks" = one chunk becomes one node (the original behaviour).
    # "pages"  = chunks are routed onto assembled wiki pages.
    # Both pipelines are permanent. This is a user-facing switch, never a
    # migration step: a wiki can be flipped back at any time.
    ingest_mode: Literal["chunks", "pages"] = "chunks"

    # Target sizes for assembled pages. Enforced in Python, not by the model.
    page_min_chunks: int = 3
    page_max_chunks: int = 8
    page_min_lines: int = 150
    page_max_lines: int = 900

    # How many candidate pages the router sees per chunk.
    page_route_candidates: int = 5
    # Below this kNN score the router parks the chunk instead of guessing.
    page_route_min_score: float = 0.25
    page_route_concurrency: int = 8

    # Generated connective text (intro, transitions). Off = verbatim only.
    page_stitch: bool = False
    page_stitch_concurrency: int = 4

    # Vector storage backend.
    vector_backend: Literal["sqlite", "qdrant"] = "sqlite"
    qdrant_url: str = ""
```

Add `Literal` to the `typing` import at the top of the file if it is not already there.

Then in `Settings.from_env()`, add the env reads alongside the existing ones:

```python
            ingest_mode=env("WIKI_INGEST_MODE", cls.ingest_mode),
            page_stitch=env_bool("WIKI_PAGE_STITCH", cls.page_stitch),
            vector_backend=env("WIKI_VECTOR_BACKEND", cls.vector_backend),
            qdrant_url=env("QDRANT_URL", cls.qdrant_url),
```

Match whatever helper names `from_env` already uses for strings and booleans — read the
surrounding lines and copy the local style rather than introducing new helpers.

### Step 1.2 — Pass through the option hook that already exists

`DocumentBody.chunk_options` is declared at `app.py:1513` but **never sent to the job** —
`create_document` builds its payload without it. Fix that one omission.

In `app.py`, in `create_document`, the `chunk_and_ingest` branch:

```python
    if len(payload.body.splitlines()) > CHUNK_LINE_THRESHOLD:
        return await _enqueue(
            "chunk_and_ingest",
            {
                "body": payload.body,
                "title": payload.title,
                "document_name": payload.document_name,
                "source_path": payload.source_path,
                "chunk_options": payload.chunk_options or {},   # <-- add this line
            },
        )
```

This gives per-request override of `ingest_mode` for free, without a new endpoint.

### Step 1.3 — Add the node type

In `graph/core.py`:

```python
class NodeType(str, Enum):
    endogenous = "endogenous"
    exogenous = "exogenous"
    page = "page"          # assembled from chunks; see PLAN_GROWI.md Part 2
```

Nothing reads it yet. It exists so pages are distinguishable later.

> **Careful:** `_db_summary_from_path` in `app.py` counts `type IN ('endo','endogenous')`
> and `('exo','exogenous')`. Pages will not appear in those counts. That is fine for now —
> **do not change those queries in this work package.**

**Verify.**
- `GET /api/settings` returns the new fields with the defaults above.
- `PATCH /api/settings {"ingest_mode": "pages"}` is accepted and reflected back.
- Ingest a fixture document: identical result to before (nothing reads `ingest_mode` yet).
- New test: `tests/test_page_settings.py` — defaults are correct, `ingest_mode` round-trips through PATCH, `chunk_options` reaches the job payload.

**Revert.** Revert the commit. Settings fields disappear; nothing referenced them.

---

## WP-2 — The shelf builder (Pass 2)

**Goal.** Turn chunk summaries into a list of empty pages. No content, no routing yet.

**Files.** `graph/pages.py` (**new**)

Create `graph/pages.py`. Everything in Track A lives here except the wiring.

> Read **Appendix B** (turning chunker output into `ChunkRef`) and **Appendix C.1/C.2**
> (prompt skeletons) before writing this.

### Data shapes

```python
from pydantic import BaseModel, Field

class ChunkRef(BaseModel):
    """One verbatim slice. Produced by the existing chunker."""
    id: str                    # stable: short_hash(body + document_name)
    title: str = ""
    summary: str = ""
    topics: list[str] = Field(default_factory=list)
    source_start: int
    source_end: int
    body: str                  # VERBATIM. Never modified anywhere in this module.

class ShelfPage(BaseModel):
    """A destination. Starts empty."""
    id: str
    title: str
    description: str = ""      # one line; this is what the router matches against
    parent_id: str | None = None
    path: str = ""             # "/chapter/page", filled by finalize_shelf
    chunk_ids: list[str] = Field(default_factory=list)

class Shelf(BaseModel):
    pages: list[ShelfPage] = Field(default_factory=list)
```

### Functions

```python
async def summarize_chunks(llm, chunks: list[ChunkRef], *, concurrency: int = 8,
                           stop_check=None) -> list[ChunkRef]:
    """Fill .summary and .topics on chunks that lack them.

    The ChunkSummary model at chunk.py:105 already exists for this and is
    unused in the streaming path. Reuse it — do not define a new schema.
    Returns chunks in the SAME ORDER. One call per chunk, bounded concurrency.
    """

async def plan_shelf(llm, chunks: list[ChunkRef], *, batch_size: int = 100,
                     stop_check=None) -> Shelf:
    """Pass 2. Build the page list from summaries only.

    2a: batches of `batch_size` summaries -> "group into 5-15 topics"; parallel.
    2b: one call merging the local outlines into a 2-level tree.
    Never sends chunk bodies. ~7 calls for a 600-chunk book.
    """

def finalize_shelf(shelf: Shelf) -> Shelf:
    """No LLM. Assign stable ids and paths, dedupe titles, drop empty chapters."""
```

### Rules

- Prompts in **Japanese**, matching `build_concept_split_prompt` (`chunk.py:1689`).
- Use `structured_ainvoke` from `chunk.py` — it already handles retries and JSON repair. Import it; do not write a second one.
- `plan_shelf` sends **summaries only, never bodies**. If you find yourself passing `chunk.body` into a prompt here, you have made it expensive and wrong.
- Page ids are content-derived and stable: `short_hash(title + parent_title)`.
- Raise `ChunkPlanningError` (already in `chunk.py`) on unrecoverable planning failure.

**Verify.** New `tests/test_pages_shelf.py` with a stubbed LLM:
- 250 fake chunks → exactly 3 batch calls + 1 merge call
- no prompt contains any chunk `body`
- `finalize_shelf` produces unique paths, no empty chapters
- ids are stable across two runs with the same input

**Revert.** Delete `graph/pages.py`. Nothing imports it yet.

---

## WP-3 — The router (Pass 3)

**Goal.** Decide which page each chunk belongs to. Still no file writing.

> Prompt skeleton: **Appendix C.3**. Worked example of what good routing looks like:
> **Appendix D**.

**Files.** `graph/pages.py` (extend)

```python
class RouteDecision(BaseModel):
    chunk_id: str
    page_id: str | None       # None = park it, needs a new page
    heading: str = ""         # the "## ..." this chunk gets on that page
    confidence: float = 0.0

async def route_chunks(
    llm, embedder, chunks: list[ChunkRef], shelf: Shelf, *,
    candidates: int = 5, min_score: float = 0.25,
    concurrency: int = 8, stop_check=None,
) -> tuple[list[RouteDecision], list[ChunkRef]]:
    """Pass 3. Returns (decisions, parked_chunks).

    Per chunk:
      1. embed each page's "title + description" ONCE (not per chunk)
      2. embed the chunk SUMMARY (not the body)
      3. kNN -> top `candidates` pages
      4. ONE small LLM call: chunk summary + the candidate titles/descriptions
         -> page_id | "new", a heading, a confidence
      5. below `min_score` or "new" -> park it

    The model NEVER sees the wiki and NEVER sees a chunk body. That is what
    keeps this ~60 output tokens per chunk and stops it being an agent.
    """

async def absorb_parked(llm, parked: list[ChunkRef], shelf: Shelf,
                        stop_check=None) -> tuple[Shelf, list[RouteDecision]]:
    """One extra plan_shelf pass over parked chunks only, then route those.
    Chunks still unrouted after this go to a page titled 'その他' under root."""
```

### Rules

- Embed page descriptions **once** for the whole run. Embedding them per chunk is the obvious mistake here.
- Use `gateway.embedder` through the caller. Do not construct an embedder inside this module.
- **One chunk goes to exactly one page.** If it fits two, the second gets a link (WP-4), never a copy. Duplicated verbatim text is a bug.
- Every chunk ends up somewhere. Losing one is a correctness failure — assert it.

**Verify.** `tests/test_pages_router.py`:
- every chunk appears in exactly one decision (no drops, no duplicates)
- page description embeddings computed once, not once per chunk
- no prompt contains a chunk body
- a chunk with all-low kNN scores is parked, not force-fitted
- after `absorb_parked`, zero chunks remain unrouted

---

## WP-4 — Assemble and write (the important one)

**Goal.** Turn routed chunks into `docs/*.md` + `_planning/*.json` in the **exact shape
`ingest_md_output` already reads**.

> **Read Appendix A first.** It gives the exact JSON of every file you must write, the
> frontmatter gotchas, and the code that proves each field is already understood by the
> loader. Do not write this package from memory.

**Files.** `graph/pages.py` (extend)

### The page file format

```markdown
---
title: "MPFファイル操作"
summary: "ファイルのオープン、読み書き、クローズの手順。"
header: "ファイル入出力"
source_lines: "[[1200,1305],[41002,41110]]"
---

## ファイルのオープン

<!-- chunk: a3f9c1 lines 1200-1305 hash:9d2e -->
（chunk 12 の本文をそのまま。1文字も変更しない。）

## エラー処理

<!-- chunk: 7b21e4 lines 41002-41110 hash:1a8c -->
（chunk 388 の本文をそのまま。）
```

Why this exact shape:

- **frontmatter** — `_split_frontmatter` (`librarian.py:3520`) already parses it. `title`, `summary` and `header` map straight onto `Node.title`, `Node.summary`, `Node.cluster`.
- **`source_lines` as a JSON string of pairs** — `_parse_ranges` (`librarian.py:3536`) already `json.loads` a string and keeps 2-item pairs. This is how a page declares non-contiguous ranges with **no loader change**.
- **`<!-- chunk: ... -->` markers** — invisible in rendered markdown, and they are what makes re-ingest a diff and citation possible later.

### Functions

```python
def size_pages(shelf: Shelf, chunks_by_id: dict[str, ChunkRef], settings) -> Shelf:
    """Pass 3.5. NO LLM. Pure arithmetic.
       < page_min_chunks or < page_min_lines -> merge into nearest sibling
       > page_max_chunks or > page_max_lines -> split at a heading boundary
       chapter with one child -> collapse
    Splitting is always safe: chunks are atomic, so a split falls BETWEEN
    sections, never inside one."""

def assemble_page(page: ShelfPage, decisions, chunks_by_id) -> str:
    """Build the markdown body. Chunk bodies are COPIED, never transformed.
    Sections ordered by source_start (book order) unless a decision says otherwise."""

def write_pages_output(*, out_dir: Path, shelf: Shelf, decisions,
                       chunks_by_id, document_name: str,
                       source_line_count: int) -> SimpleNamespace:
    """Write out_dir/docs/*.md and out_dir/_planning/{metadata,coverage,manifest}.json
    in the same shape arun_chunk_pipeline produces, so ingest_md_output reads
    it unchanged. Returns the same fields arun_chunk_pipeline returns."""

def assert_pages_preserve_chunks(shelf, decisions, chunks_by_id, out_dir: Path) -> None:
    """Integrity check, replacing assert_rendered_docs_match_source.
    For every chunk: its body appears VERBATIM exactly once across all pages.
    Raise RuntimeError naming the chunk if not."""
```

### Rules

- **`assemble_page` must never modify chunk text.** Not whitespace, not headings, nothing. Copy the string.
- Do **not** call `assert_rendered_docs_match_source` — it requires contiguous coverage and will always fail here. Call `assert_pages_preserve_chunks` instead.
- `_planning/manifest.json` must record `"ingest_mode": "pages"` so a document remembers how it was built.
- Filenames: keep the existing numeric-prefix convention so `_doc_sort_key` (`librarian.py:3619`) still orders them.

**Verify.** `tests/test_pages_assemble.py`:
- **the critical test:** for every chunk, `chunk.body in page_body` is true for exactly one page
- frontmatter round-trips: feed the written file through `Librarian._split_frontmatter` + `_parse_ranges` and get the expected multi-range list back
- `size_pages` respects all four bounds; a split never lands inside a chunk
- a page with one chunk merges; a page with 20 splits

---

## WP-5 — The stitcher (Pass 4) *(optional, off by default)*

**Goal.** Add small generated connective text — **without ever touching chunk bodies**.

> Prompt skeleton: **Appendix C.4**.

**Files.** `graph/pages.py` (extend)

```python
class StitchOp(BaseModel):
    op: Literal["insert_intro", "set_heading", "insert_transition_before",
                "add_see_also", "mark_duplicate", "reorder"]
    section_id: str | None = None
    text: str = ""
    target_ids: list[str] = Field(default_factory=list)

async def stitch_page(llm, page: ShelfPage, section_summaries: list[dict],
                      sibling_titles: list[str], stop_check=None) -> list[StitchOp]:
    """Returns OPERATIONS, never markdown.
    Input is headings + summaries + sibling titles. NOT the bodies."""

def apply_stitch_ops(body: str, ops: list[StitchOp]) -> str:
    """Apply ops to the assembled markdown. Our code, not the model's output.
    MUST NOT alter any text between a <!-- chunk: --> marker and the next
    heading. Assert this before returning."""
```

### Rules

- **This is the hallucination guarantee, and it is structural.** The model returns a typed list of ops; our code applies them. Verbatim text is untouchable *by construction*, not by prompt instruction. Do not "simplify" this into asking the model for markdown.
- `apply_stitch_ops` re-runs `assert_pages_preserve_chunks` afterwards. If any chunk body changed, raise.
- Gated on `settings.page_stitch`, default `False`. With it off, pages are pure verbatim + headings, which is a perfectly good product.

**Verify.** `tests/test_pages_stitch.py`:
- a malicious stub returning ops that try to rewrite a chunk body → raises
- ops output never parsed as markdown
- with `page_stitch=False`, no LLM call is made at all

---

## WP-6 — Wire it in

**Goal.** Make the flag actually do something. **This is the smallest work package and the
most important one to get right.**

**Files.** `graph/librarian.py` (edit — one `if`)

In `chunk_and_ingest` (`librarian.py:1493`), after the `llm = make_llm(...)` line and
before `result = run_chunk_pipeline(...)`:

```python
        options = job.payload.get("chunk_options") or {}
        mode = options.get("ingest_mode") or settings.ingest_mode

        if mode == "pages":
            from .pages import run_pages_pipeline

            result = run_pages_pipeline(
                source_text=body,
                document_name=document_name,
                out_dir=out_dir,
                llm=llm,
                embedder=self.gateway.embedder,
                settings=settings,
                on_progress=on_progress,
                stop_check=stop_check,
            )
        else:
            result = run_chunk_pipeline(
                source_text=body,
                document_name=document_name,
                out_dir=out_dir,
                llm=llm,
                on_progress=on_progress,
                stop_check=stop_check,
            )
```

`run_pages_pipeline` is the sibling of `arun_chunk_pipeline`: it runs pass 1 (**by calling
the existing `plan_concept_files_streaming` — do not reimplement chunking**), then
WP-2 → WP-3 → WP-4 → WP-5, and returns the same `SimpleNamespace` fields.

Everything after this `if` — `ingest_md_output`, enrichment, the `finally` cleanup — is
**unchanged**. That is the whole point.

**Verify.**
- `ingest_mode="chunks"` (default): byte-identical result to before this plan started. Same node ids.
- `ingest_mode="pages"`: fewer, larger nodes; every chunk's text present exactly once; `ask()` still answers; `/api/graph` renders.
- Ingest doc A as chunks and doc B as pages **into the same wiki**. Both searchable, no errors. This proves C3.
- Flip the setting back to `"chunks"` and ingest doc C. It behaves as today, and docs A and B are untouched.

**Revert.** Delete the `if` branch, keep the `else` body. One-line revert.

---

## WP-7 — The `VectorIndex` seam *(no behaviour change)*

**Goal.** Put an interface in front of vector storage so Qdrant can be swapped in later
without a rewrite. **Behaviour must be identical after this package.**

**Files.** `graph/vectors.py` (**new**), `graph/librarian.py` (edit — small)

```python
class VectorIndex(Protocol):
    def ensure(self, channel: str, dim: int) -> None: ...
    def upsert(self, channel: str, ids: list[str],
               vectors: list[list[float]], payload: dict | None = None) -> None: ...
    def search(self, channel: str, vector: list[float], k: int,
               filters: dict | None = None) -> list[tuple[str, float]]: ...
    def delete(self, channel: str, ids: list[str]) -> None: ...

class SqliteVecIndex:
    """Wraps the existing GraphStore vector methods. Adds nothing, changes nothing."""
    def __init__(self, store: GraphStore) -> None: ...
```

`channel` is what is a table name today: `body`, `summary`, `bridge`, `search_item`.

Then, in `librarian.py`, route `_ensure_vec`, `_store_vectors` and `_knn_candidates`
through `self.vector_index` instead of calling `self.store` directly. **Keep the method
bodies otherwise identical** — this is a redirection, not a rewrite.

**Verify.** The full existing test suite passes unchanged, and a before/after ingest
produces identical vectors and identical `_knn_candidates` output. If anything differs,
you changed behaviour and must back it out.

---

# TRACK B — GROWI

> Do not start until WP-6 verifies. Track B assumes `growi_enabled=false` by default and
> must add **zero** overhead when off.

## WP-8 — GROWI client (read-only first)

**Files.** `graph/growi.py` (**new**)

```python
class GrowiClient:
    def __init__(self, url: str, api_token: str, *, timeout: float = 30.0): ...

    # --- read ---
    async def health(self) -> bool: ...
    async def get_page(self, path: str | None = None,
                       page_id: str | None = None) -> GrowiPage | None: ...
    async def list_pages(self, root_path: str = "/", *,
                         updated_after: str | None = None,
                         cursor: str | None = None) -> tuple[list[GrowiPage], str | None]: ...

    # --- write (WP-12; leave raising NotImplementedError for now) ---
    async def create_page(self, path: str, body: str) -> GrowiPage: ...
    async def update_page(self, page_id: str, revision_id: str, body: str) -> GrowiPage: ...
```

```python
class GrowiPage(BaseModel):
    page_id: str
    revision_id: str
    path: str
    title: str = ""
    body: str = ""
    updated_at: str = ""
```

### Rules

- Use `httpx.AsyncClient`. `mcp_server.py` already uses `httpx` — copy its error-handling style.
- **Write methods must not be implemented in this package.** Read-only first, so a bug here cannot damage a real wiki.
- Optional `GrowiMongoReader` for fast backfill — **read-only, and it must assert it opened the connection read-only.** Never write to their MongoDB: GROWI maintains `parent`, `descendantCount`, `isEmpty`, `grant` and the revision chain, and pushes updates into Elasticsearch. Writing behind its back corrupts the page tree.

**Verify.** `tests/test_growi_client.py` against a mocked httpx transport. No live server.

---

## WP-9 — Connection registry

**Files.** `graph/registry.py` (**new**)

One SQLite file, `engine.sqlite`, holding:

```sql
CREATE TABLE IF NOT EXISTS growi_connections (
    name          TEXT PRIMARY KEY,     -- the URL segment
    url           TEXT NOT NULL,
    api_token_enc TEXT NOT NULL,        -- encrypted with WIKI_SECRET_KEY
    mongo_uri_enc TEXT,                 -- optional, read-only backfill
    mode          TEXT NOT NULL DEFAULT 'attach',   -- attach | own
    root_path     TEXT NOT NULL DEFAULT '/',
    write_path    TEXT NOT NULL DEFAULT '/inbox',
    sync_cursor   TEXT,
    last_sync_at  TEXT,
    last_error    TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS growi_pages (
    name          TEXT NOT NULL,
    page_id       TEXT NOT NULL,
    revision_id   TEXT NOT NULL,
    path          TEXT NOT NULL,
    index_hash    TEXT,
    indexed_at    TEXT,
    PRIMARY KEY (name, page_id)
);
```

### Rules

- **Tokens are encrypted at rest.** Key from `WIKI_SECRET_KEY`. Use `cryptography.fernet` — do not hand-roll.
- **Never return a token to a client.** Extend the existing `_SECRET_KEYS` / `_redact()` pattern in `app.py`. Do not build a second redaction mechanism.
- If `WIKI_SECRET_KEY` is unset, refuse to store a connection with a clear error. Do not silently store plaintext.

**Verify.** `tests/test_registry.py` — round-trip a connection; assert the raw DB row does
**not** contain the plaintext token; assert the API-shaped dict has it blanked.

---

## WP-10 — Admin endpoints for connections

**Files.** `app.py` (edit — **add** endpoints, do not modify existing ones)

Add `/admin/api/connections/*` **alongside** the existing `/admin/api/dbs/*`. Both work.
That is what makes this revertible and lets the two models coexist during migration.

| Method | Path | Does |
|---|---|---|
| `GET` | `/admin/api/connections` | list, with stage / last sync / page count. **Tokens redacted.** |
| `POST` | `/admin/api/connections/{name}` | register: url, token, mode, root_path, write_path |
| `PATCH` | `/admin/api/connections/{name}` | update fields; blank token means "keep existing" |
| `POST` | `/admin/api/connections/{name}/test` | reach GROWI, report version and page count |
| `POST` | `/admin/api/connections/{name}/resync` | queue a backfill |
| `DELETE` | `/admin/api/connections/{name}` | **detach**: drop our index only. Never touches their wiki. |

Reuse `_require_admin`, `_validate_db_name` (rename to `_validate_name`, keep the old name
as an alias so nothing breaks), `api_error`, and `ADMIN_DB_LOCK`.

> **`DELETE` must be obviously safe.** It deletes rows in our index and nothing in GROWI.
> Say so in the docstring and in the response body (`"growi_untouched": true`).

**Verify.** `tests/test_admin_connections.py` — full CRUD against a mocked client; a
`DELETE` issues zero write calls to GROWI; tokens never appear in any response.

---

## WP-11 — Route a URL segment to a connection

**Files.** `app.py` (edit — small)

Today `db_routing` checks `_db_path(seg).exists()`. Extend it: if `growi_enabled` and the
segment names a registered connection, bind that instead.

`STACKS`, `stages`, `errors`, `building`, `_ensure_building`, `_bootstrap_db` and
`_close_stack` are **reused as-is** — the value is a connection-backed stack instead of a
file-backed one. Do not fork them into a parallel set.

> **Required change:** `WIKI_STARTUP_REQUIRE_ALL_DBS` defaults to `true` (`app.py:246-251`)
> and raises, killing startup, when one entry fails to preload. With external GROWIs that
> is wrong — one unreachable instance must not take the service down. Change the default to
> `false` and log the failure per connection.

**Verify.** Two registered connections reachable at `/prefix/a/` and `/prefix/b/`; one
unreachable reports `not_ready` on its own segment while the other keeps serving; existing
`.sqlite` wikis still route exactly as before.

---

## WP-12 — Publish to GROWI

**Files.** `graph/growi.py` (extend), `graph/librarian.py` (add one job type)

Implement `create_page` / `update_page`. Add a `publish_to_growi` job type in
`_dispatch_job` following the shape of the existing branches.

> **Read the API cheat sheet in Reference material first**, especially the note about
> `revisionId`. `PUT /_api/v3/page/` requires it, and a stale one means somebody edited the
> page — re-fetch, re-apply our marked sections, retry once, never force.

### Rules

- **`mode="attach"` may only write under `write_path`.** Assert the target path starts with it, and raise otherwise. This assertion is the safety mechanism — make it impossible to bypass.
- **Never modify or delete text we did not add.** Update = replace only our marked sections.
- **One revision per page per ingest**, not one per chunk, or page history becomes unreadable.
- Every appended section keeps its `<!-- chunk: ... -->` marker so it can be found and reverted.

**Verify.** `tests/test_growi_publish.py` — in `attach` mode, a write outside `write_path`
raises; an update touches only marked sections; N chunks onto one page produce exactly one
revision.

---

## WP-13 — Read from GROWI

**Files.** `graph/growi.py` (extend), `graph/librarian.py` (add one job type)

Backfill (Mongo if configured, else API crawl) and an incremental sync poller comparing
`revision_id`. Run it as a `WriteJob` with `job.progress` so the existing job UI shows it
for free.

Sync rules: new page → index; `revision_id` changed → re-embed that page only; renamed →
update path, keep vectors; deleted → drop from index.

**Verify.** `tests/test_growi_sync.py` — unchanged revision does zero work; changed
revision re-embeds only that page; deletion removes it; a poller crash does not lose the
cursor.

---

## WP-14 — Qdrant backend

**Files.** `graph/vectors.py` (extend)

Add `QdrantIndex` implementing `VectorIndex`. Selected by `vector_backend="qdrant"`.
`"sqlite"` stays the default.

Follow Qdrant's documented multi-tenancy pattern:

- **one collection** for everything, not one per GROWI
- a `growi_id` payload field on every point
- a payload index on it with **`is_tenant: true`** — without this, filtering is slow
- **`hnsw.m: 0`** and **`payload_m: 16`**, so Qdrant builds a per-tenant index instead of one global graph. Without this, tenants block each other during indexing and adding a wiki stalls the others.
- `channel` (`body` / `summary` / `bridge` / `search_item`) as a second payload field

> **Constraint to write into the code as a check:** one collection requires the same
> embedding model everywhere. On startup, if a registered connection's `embed_dim` differs
> from the collection's, refuse with a clear error rather than corrupting the index.

**Verify.** `tests/test_vectors_qdrant.py` against a local Qdrant or its in-memory mode.
Same-input parity with `SqliteVecIndex`; tenant A's query never returns tenant B's points.

---

## WP-15 — Deletions *(last, optional, only when everything above is proven)*

Only after GROWI has been the source of truth for real work.

Remove: `nodes_fts`, `search_items_fts`, `_fts_query`, the `vec_*` tables (once Qdrant is
in use), `_validate_sqlite_file`, `_migrate_sqlite_file`, `admin_upload_db`,
`admin_copy_db`, and the `MarkdownView` / `DocSidebar` / `UploadView` frontend.

**Never remove:** `arun_chunk_pipeline` or the `ingest_mode="chunks"` path. That is
constraint **C3** and it is permanent.

---

# Appendices

These exist so you never have to reverse-engineer something this plan depends on.

---

## Appendix A — The on-disk contract (read before WP-4)

WP-4 must write exactly this shape, because `ingest_md_output` reads exactly this shape.
Everything here was read out of the current code, not invented.

```
out_dir/
  docs/
    01-ファイル操作.md
    02-エラー処理.md
    ...
  _planning/
    metadata.json
    coverage.json
    manifest.json
    concept-plan.md        <- human-readable, optional for the pages path
```

### `_planning/metadata.json`

Written by `write_metadata_json` (`chunk.py:2478`). Read by
`_load_new_planning_docs_output` (`librarian.py:3427`) to pick the document name and the
per-file `header`.

```json
{
  "original_file_name": "manual.md",
  "inferred_file_name": "MPF操作マニュアル.md",
  "files": [
    { "name": "01-ファイル操作.md", "header": "ファイル入出力" }
  ]
}
```

- `inferred_file_name` becomes `Node.original_document_name` for every node in the document.
- `files[].name` must match the file in `docs/` exactly — it is the join key.
- `files[].header` becomes `Node.cluster` when coverage does not supply one.

### `_planning/coverage.json`

Written by `write_coverage_json` (`chunk.py:2451`). This is where the loader prefers to get
title, summary, header and ranges.

```json
{
  "source_line_count": 60000,
  "file_count": 120,
  "files": [
    {
      "title": "MPFファイル操作",
      "filename": "01-ファイル操作.md",
      "source_start": 1200,
      "source_end": 1305,
      "summary": "ファイルのオープンと読み書き。",
      "header": "ファイル入出力"
    }
  ]
}
```

> **The one field that does not fit a page.** `coverage.files[]` has a *single*
> `source_start`/`source_end` pair, but an assembled page has several non-contiguous
> ranges.
>
> The loader handles this already. Look at `_load_new_planning_docs_output`
> (`librarian.py:3427`): it prefers `cov_rec` start/end, and **falls back to the file's
> frontmatter `source_lines`** when they are absent.
>
> So for the pages pipeline: **omit `source_start`/`source_end` from `coverage.json`** and
> put the real multi-range list in the file's frontmatter. Keep `title`, `summary`,
> `header` and `filename` in coverage — those still apply.

### `_planning/manifest.json`

Shape from `init_manifest` (`chunk.py:454`). Not read by the loader; it is the audit trail.
Record the mode here so a document remembers how it was built (constraint C3):

```json
{
  "source": "manual.md",
  "created_at": "...", "updated_at": "...",
  "files": [], "chunks": [], "coverage": [], "verification_flags": [],
  "planning": {
    "ingest_mode": "pages",
    "strategy": "shelf_route_assemble",
    "file_count": 120,
    "chunk_count": 600,
    "stitch_enabled": false
  }
}
```

### A page file in `docs/`

```markdown
---
title: "MPFファイル操作"
summary: "ファイルのオープン、読み書き、クローズの手順。"
header: "ファイル入出力"
source_lines: "[[1200,1305],[41002,41110]]"
---

## ファイルのオープン

<!-- chunk: a3f9c1e2 lines 1200-1305 hash:9d2e4b -->
（chunk 12 の本文をそのまま。1文字も変更しない。）

## エラー処理

<!-- chunk: 7b21e4d0 lines 41002-41110 hash:1a8c3f -->
（chunk 388 の本文をそのまま。）
```

Why each part works, with the code that proves it:

| Part | Proof it works today |
|---|---|
| frontmatter block | `_split_frontmatter` (`librarian.py:3520`) parses `key: value` lines and strips quotes. It is not full YAML — **keep values on one line.** |
| `source_lines` as a JSON string | `_parse_ranges` (`librarian.py:3536`) calls `json.loads` on a string and keeps 2-item numeric pairs. A list of pairs round-trips exactly. |
| `title` / `summary` / `header` | mapped in `_load_new_planning_docs_output` onto `Node.title`, `Node.summary`, `Node.cluster` |
| numeric filename prefix | `_doc_sort_key` (`librarian.py:3619`) orders files numerically when the name starts with digits |
| `<!-- chunk: ... -->` | invisible when rendered; the anchor for re-ingest diffing and citation |

**Frontmatter gotcha:** the parser splits on the *first* `:` and strips surrounding quotes.
A title containing a colon is fine (it lands in the value), but a **newline inside a value
is not**. Always quote values and keep them single-line.

---

## Appendix B — Turning chunker output into `ChunkRef`

Pass 1 already exists. Do not reimplement it. Call it and convert its output.

`plan_concept_files_streaming` (`chunk.py:1903`) returns `list[ConceptFilePlan]`
(`chunk.py:1044`):

```python
class ConceptFilePlan(BaseModel):
    title: str
    filename: str
    source_start: int      # 1-based, inclusive
    source_end: int        # 1-based, inclusive
    summary: str = ""
```

The conversion, which belongs at the top of `run_pages_pipeline`:

```python
from .chunk import plan_concept_files_streaming, range_to_markdown
from .core import short_hash

async def _pass1_chunks(llm, source_lines, document_name, stop_check=None):
    plans = await plan_concept_files_streaming(
        llm=llm,
        source_lines=source_lines,
        target_lines=100,
        max_extra=MAX_CHUNK_EXTRA,
        stop_check=stop_check,
    )

    chunks = []
    for plan in sorted(plans, key=lambda p: (p.source_start, p.source_end)):
        # range_to_markdown (chunk.py:155) slices the ORIGINAL lines.
        # This is the verbatim body. It is never modified after this point.
        body = range_to_markdown(source_lines, [plan.source_start, plan.source_end])
        chunks.append(ChunkRef(
            id=short_hash(f"{document_name}|{plan.source_start}-{plan.source_end}"),
            title=plan.title,
            summary=plan.summary,
            topics=[],
            source_start=plan.source_start,
            source_end=plan.source_end,
            body=body,
        ))
    return chunks
```

Notes:

- `range_to_markdown` (`chunk.py:155`) is the existing helper that slices source lines with no line-number prefixes. **Use it.** Do not slice by hand.
- The chunk `id` is derived from document name plus range, so it is **stable across runs** — required for re-ingest diffing.
- `ConceptFilePlan.summary` is often empty in the streaming path. That is what `summarize_chunks` in WP-2 fills.

---

## Appendix C — Prompt skeletons

House style, taken from `build_concept_split_prompt` (`chunk.py:1689`): a short
`SystemMessage` stating the role and "structured output only", then a `HumanMessage` with
the task, hard requirements as a bullet list, and the data last. **Japanese output.** Send
through `structured_ainvoke` (`chunk.py:994`), which already handles retries and JSON
repair.

### C.1 — Shelf, step 2a (group a batch of summaries)

```
System:  あなたは技術文書の目次を設計する専門家です。
         構造化出力のみを返してください。

Human:   タスク: 以下は1冊の資料を分割した断片の要約リストです。
         これらを 5〜15 個のトピックにグループ化してください。

         厳格な要件:
         - 各トピックには title（日本語）と description（日本語1行）を付ける
         - description はそのトピックに何が書かれているかを1文で説明する
         - 各断片IDをちょうど1つのトピックに割り当てる
         - 断片を落とさない、重複させない
         - 位置ではなく内容でグループ化する（離れた断片が同じトピックでもよい）

         断片一覧:
         [id] title — summary
         ...
```

**Never put a chunk body in this prompt.** Summaries only. If bodies appear here, the pass
becomes expensive and the design is defeated.

### C.2 — Shelf, step 2b (merge the local outlines)

```
Human:   タスク: 以下は同じ資料の異なる部分から作られた複数の目次案です。
         これらを1つの2階層ツリー（章 -> ページ）に統合してください。

         厳格な要件:
         - 同じ内容が別名で現れている場合は1つに統合する
         - 章は 3〜12 個
         - 各章のページ数は 2〜15
         - すべての断片IDが、ちょうど1つのページに残るようにする
         - title と description は日本語
```

### C.3 — Router (one chunk, five candidates)

The smallest and most frequent call. Keep it tiny — this is where the token budget lives.

```
System:  あなたは文書の断片を、既存のWikiページに振り分ける担当者です。
         構造化出力のみを返してください。

Human:   次の断片を、下の候補ページのどれか1つに割り当ててください。

         断片:
           タイトル: {chunk.title}
           要約: {chunk.summary}

         候補ページ:
           1. [{id}] {title} — {description}
           ... 最大5件 ...

         返すもの:
         - page_id: 最も適した候補のID。どれも適さない場合は "new"
         - heading: この断片にふさわしい日本語の見出し（"## " は付けない）
         - confidence: 0.0〜1.0

         判断基準:
         - 内容が主題として一致するページを選ぶ
         - 少しでも触れている程度なら "new" を選ぶ
         - 迷ったら "new"。無理に押し込まない
```

"迷ったら new" matters: a parked chunk is recoverable in `absorb_parked`; a confidently
misfiled one is not.

### C.4 — Stitcher (returns operations, never markdown)

```
System:  あなたはWikiページの構成を整える編集者です。
         本文は絶対に書き換えません。編集操作のリストのみを返します。
         構造化出力のみを返してください。

Human:   ページ「{page.title}」は以下のセクションで構成されています。

         セクション:
           [{section_id}] {heading} — {summary}
           ...

         同階層の他ページ: {sibling_titles}

         返せる操作は次のみです:
         - insert_intro                 冒頭に3〜5行の導入
         - set_heading                  見出しの表記を統一
         - insert_transition_before     節の切り替わりに1行
         - add_see_also                 関連ページへのリンク
         - mark_duplicate               重複する節に印を付ける（削除はしない）
         - reorder                      節の順序変更（section_id で指定）

         禁止事項:
         - 本文の書き換え、要約、削除
         - 資料に無い事実の追加
```

The section **bodies are not in this prompt** — only headings and summaries. The model
cannot rewrite what it never saw.

---

## Appendix D — Worked example, end to end

A tiny book so you can trace every stage. Source is 400 lines.

**After pass 1** — four verbatim chunks:

| id | lines | title |
|---|---|---|
| `c1` | 1–100 | ファイルを開く |
| `c2` | 101–200 | エラーコード一覧 |
| `c3` | 201–300 | ファイルを閉じる |
| `c4` | 301–400 | エラー処理の実例 |

**After pass 2 (shelf)** — note this is by topic, not position:

```
/ファイル入出力          (chapter)
  /ファイル入出力/基本操作      "ファイルの開閉手順"
/エラー                  (chapter)
  /エラー/エラーコード          "エラーコードとその意味"
```

**After pass 3 (routing)** — `c1` and `c3` are *not adjacent* in the book but belong
together; `c2` and `c4` likewise:

| chunk | page | heading | confidence |
|---|---|---|---|
| `c1` | 基本操作 | ファイルを開く | 0.91 |
| `c3` | 基本操作 | ファイルを閉じる | 0.88 |
| `c2` | エラーコード | エラーコード一覧 | 0.94 |
| `c4` | エラーコード | エラー処理の実例 | 0.79 |

**That regrouping is the entire point of the feature.** In `ingest_mode="chunks"` these
four would be four unrelated nodes in book order.

**After pass 3.5 (sizing)** — each page holds 2 chunks ≈ 200 lines. Below
`page_min_chunks` (3), so with real settings they would merge. In this toy example assume
they pass.

**After pass 4, `docs/01-基本操作.md`:**

```markdown
---
title: "基本操作"
summary: "ファイルの開閉手順。"
header: "ファイル入出力"
source_lines: "[[1,100],[201,300]]"
---

## ファイルを開く

<!-- chunk: c1 lines 1-100 hash:aa11 -->
（1〜100行目をそのまま）

## ファイルを閉じる

<!-- chunk: c3 lines 201-300 hash:bb22 -->
（201〜300行目をそのまま）
```

**After `ingest_md_output`** — two `Node`s, unchanged loader:

```python
Node(
  title="基本操作",
  summary="ファイルの開閉手順。",
  cluster="ファイル入出力",
  source_ranges=[(1, 100), (201, 300)],      # non-contiguous, already supported
  body="## ファイルを開く\n\n（1〜100行目...）\n\n## ファイルを閉じる\n\n（201〜300行目...）",
  type=NodeType.page,
)
```

**Integrity check:** `assert_pages_preserve_chunks` verifies each of `c1`…`c4` bodies
appears verbatim in exactly one page. All four do. Pass.

---

## Appendix E — When it goes wrong

The failures you should expect, and what they mean.

| Symptom | Cause | Fix |
|---|---|---|
| `ingest_md_output` returns zero nodes | `docs/*.md` empty after frontmatter strip, or `docs/` missing | Check Appendix A. `_load_new_planning_docs_output` skips files whose body is empty after stripping. |
| `Node.source_ranges` comes back `[]` | frontmatter `source_lines` is not valid JSON, or values span lines | `_parse_ranges` returns `[]` on `JSONDecodeError` — silently. Print the raw frontmatter value. |
| Node titles are filenames, not real titles | `coverage.json` `filename` does not match the file in `docs/` | The join key must match exactly, including the numeric prefix. |
| `assert_pages_preserve_chunks` fails | something modified a chunk body — usually whitespace normalisation or a heading rewrite | Diff the chunk body against the page slice. Find the code that touched it and stop it. |
| Routing puts almost everything in "その他" | page `description` fields are empty or too vague, so kNN scores are all low | Fix step 2a's prompt to produce real one-line descriptions. This is a shelf-quality bug, not a router bug. |
| Ingest is much slower than expected | page descriptions being embedded once per chunk instead of once per run | Cache the page-vector matrix before the routing loop. |
| Same text appears on two pages | a chunk routed twice | One chunk, one page. Assert it in `route_chunks`. |
| Existing tests fail after WP-7 | you changed behaviour while adding the seam | WP-7 is pure redirection. Back it out and redo it as a wrapper. |
| GROWI `PUT /page/` returns a conflict | stale `revisionId` — somebody edited the page | Re-fetch, re-apply our marked sections, retry once. **Never force.** |
| GROWI writes land in the wrong place | `write_path` not enforced | In `attach` mode this is a **serious bug**. The assertion in WP-12 must be unbypassable. |
| A wiki segment 404s after WP-11 | connection registered but never bootstrapped | Check `stages[name]` and `errors[name]` via `/api/ready`. |

### Debugging habits that pay off here

- **Run the pipeline on a 400-line file first**, not a book. Every stage is inspectable at that size.
- **`_planning/` is your log.** Write the shelf and the routing decisions to it before assembling. If output looks wrong, read those two files before reading code.
- **Diff the two modes.** Ingest the same document as `chunks` and as `pages` into two wikis and compare `/api/graph`. It shows immediately whether content was lost.
- **Assert, do not log, on invariants.** Losing a chunk must crash the job, not print a warning nobody reads.

---

## Do not do these

Collected mistakes, each of which would break a constraint.

| Do not | Why |
|---|---|
| Run **`git merge benchmark`** | It predates the neighbourhood cache and vocabulary sheet, so it deletes code `realtime.py` and `vocab.py` need. Cherry-pick instead — WP-0. |
| Port `decompose_query` / `decompose_max_queries` from benchmark | Researcher-side settings. C4. |
| Rewrite `arun_chunk_pipeline` to add pages | C3. It is a sibling function. |
| Make `ingest_mode="pages"` the default | C2, C3. The user switches it on. |
| Reimplement chunking inside `pages.py` | Call `plan_concept_files_streaming`. |
| Let the stitcher return markdown | It returns typed ops. That is the whole guarantee. |
| Send chunk bodies to the shelf or router | Summaries only. Bodies make it slow and pointless. |
| Modify a chunk body anywhere | Verbatim is the entire product. |
| Call `assert_rendered_docs_match_source` on pages | It requires contiguous coverage; always fails. |
| Write to GROWI's MongoDB | Corrupts the page tree and desyncs Elasticsearch. |
| Write outside `write_path` in `attach` mode | Damages somebody's real wiki. |
| Return a GROWI token in any response | Extend `_redact()`. |
| Edit `researcher.py`, `realtime.py`, `vocab.py`, `neighborhood.py`, `gateway.py` | C4. Produce data in the shape they already read. |
| Add a second "not ready" or job-queue mechanism | C5. Reuse `stages` and `WriteJob`. |
| Call a real model in a test | C7. |

---

## Definition of done, per track

**Track A** is done when all of these hold:

- [ ] Default config produces byte-identical output to before this plan
- [ ] `ingest_mode="pages"` produces assembled pages, every chunk verbatim exactly once
- [ ] Both modes coexist in one wiki
- [ ] Flipping back to `"chunks"` works and leaves existing documents untouched
- [ ] `ask()` and the realtime pipeline work in both modes with no edits to their files
- [ ] Page sizes respect all four bounds
- [ ] `page_stitch=False` makes zero LLM calls in pass 4

**Track B** is done when:

- [ ] `growi_enabled=false` adds no overhead and loads no GROWI code
- [ ] Two GROWIs registered through the admin panel, reachable at their own segments
- [ ] One unreachable GROWI does not affect the others
- [ ] `attach` mode cannot write outside `write_path` — proven by a test
- [ ] `DELETE` on a connection issues zero writes to GROWI
- [ ] Tokens never appear in a response or in a plaintext DB row
- [ ] Dropping the vector volume and rebuilding from GROWI loses nothing

---

## If you get stuck

Report rather than improvise, when:

- a work package seems to need editing a **C4** file
- a work package seems to need rewriting an existing function
- `ingest_md_output` will not read the written files (**re-read WP-4** — the shape is the contract)
- the existing test suite fails after a package that was supposed to change no behaviour

In all four cases the design is wrong, not the constraint. Say what you found and what you
think the smallest correct change is.
