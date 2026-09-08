# Plan: real wikis + GROWI

Two problems, one plan.

1. **The chunker makes slices, not wiki pages.**
2. **We built our own wiki storage (SQLite), and GROWI already is one.**

They are related. Fixing #1 first is what makes #2 easy, so that is the order below.

**Design rule that drives everything:** the LLM never rewrites source text.
Chunks stay **verbatim, cut by line number**. The LLM only decides *where a chunk
goes* and writes the small connective text between chunks. Two reasons:

- **Hallucination.** Copied bytes cannot be hallucinated. A rewrite can.
- **Speed.** At ~50 tok/s, re-typing a whole book is hours of pure decode.
  Deciding where a chunk goes is ~60 tokens.

---

## Part 0 — What the code does today (so we agree on the starting point)

Follow one book through the system:

| Step | Where | What happens |
|---|---|---|
| 1 | `app.py:1991` | Body longer than 300 lines -> job `chunk_and_ingest` |
| 2 | `graph/chunk.py:1903` `plan_concept_files_streaming` | Slide a window over the book. Ask the LLM to cut it into "concept files". Target **100 lines each**. Rule: every source line covered exactly once. |
| 3 | `graph/chunk.py:2158` `render_concept_files` | Write each cut to `docs/NN-name.md`. The text is a **verbatim copy** of those source lines. |
| 4 | `graph/librarian.py:3427` | Each `.md` file -> one `Node` |
| 5 | `graph/librarian.py:1767` `_ingest_one` | Per node: summary call, keywords call, claims call, bridge-probe call, 3 embeddings, kNN, then LLM edge calls in groups of 4 |
| 6 | `graph/librarian.py:1481` | Recluster + rename clusters in Japanese |

### Why the output is not a wiki

A page is **"lines 4300-4405 of the book"** wearing a Japanese title. That is a slice, not an article.

- The same concept in chapter 2 and chapter 9 becomes **two unrelated pages**.
- Page size is decided by line count, not by "is this topic finished". You get 10-line pages next to huge ones.
- There is **no hierarchy**. Only a flat list, a `follows` chain, LLM-guessed semantic edges, and Leiden clusters with generated names.
- **Nothing was ever collected.** Every chunk is alone.

Note: `chunk.py:121-131` already has `H1Plan` / `H1Layout` with a `use_h2_folders` flag. Somebody started building a tree and stopped. We are finishing that idea.

### Why ingestion is slow

Look at step 5. That is roughly **10 LLM calls per node**, run one node after another.

A 60,000-line book -> ~600 slices -> **~6,000 sequential LLM calls**, and roughly
**900k tokens of generation**. At 50 tok/s that is about 5 hours of decode alone.

The `benchmark` branch already fixed a big part of the *scheduling*:

- `create_document_nodes()` — 3 phases (prepare parallel / revise in order / link parallel), so threads run at once and the result is still deterministic
- `_EDGE_GROUP_SIZE` raised 4 -> 8 (halves the most expensive call)
- `recluster_every = 0` — recluster **once** at the end of a batch, not every 10 documents (the old way was O(n^2) on a big book)
- Bounded concurrency inside `plan_concept_files_streaming`

Merge it. But it only makes the wrong thing faster — it still produces slices, and it
still generates ~900k tokens. So we change **how much we generate**, too.

---

## Part 1 — The one big idea

> **Stop treating one chunk as one wiki page. Chunks are the material. Pages are shelves you append material onto.**

```
                   THE BOOK (60,000 lines)
                            |
                            v
   LAYER A: CHUNKS  ...  ~600 pieces, ~100 lines each
   verbatim, cut by line number, complete coverage
   THIS IS THE MATERIAL. NEVER REWRITTEN.
                            |
             (routed by topic, not by position)
                            v
   LAYER B: PAGES  ...  ~120 real wiki pages, in a tree
   each page = several chunks appended in order,
   pasted verbatim, plus ~5% connective text
   THIS IS THE WIKI.
```

Layer A is what `chunk.py` already builds — **keep it exactly as it is**. It gives exact
traceability back to source lines, which is why citations work.

Layer B is new, and it is **assembled, not written**. A page is:

```
  # ページタイトル
  <- 3-5 lines of intro          GENERATED (small)
  ## 見出し A
  <- chunk 12, lines 1200-1305   VERBATIM
  <- 1 line transition           GENERATED (small)
  ## 見出し B
  <- chunk 388, lines 41002-41110  VERBATIM
  ## 関連ページ
  <- link list                   GENERATED (small)
```

About **95% of every page is bytes copied from the book.**

**The trick that avoids expensive agents:** decide the page list *first*, from cheap
summaries only. Then routing a chunk is a vector lookup plus one tiny LLM call that
sees **five candidate page descriptions** — never the wiki, never other chunks.
No agent reads the existing wiki. No agent edits somebody else's page.

---

## Part 2 — Making real wikis (4 passes)

### Pass 1: Chunk the book — *already built, keep it*

`plan_concept_files_streaming` in `chunk.py`. Verbatim, by line number, parallel on the
`benchmark` branch. **Do not change the cutting logic.**

**One change:** each chunk must also produce a short `summary` and 3-5 `topics`.
The `ChunkSummary` model at `chunk.py:105` already exists for exactly this — it is
simply not wired into the streaming path.

Output per chunk: `{id, title, summary, topics[], source_start, source_end}` —
about 50 tokens of metadata, plus the untouched body.

### Pass 2: Build the shelf — *new, cheap*

Goal: decide **what pages should exist**, before anything is filed. From summaries only.

```
Step 2a  Batch the chunk summaries, 100 at a time -> 6 batches.
         Per batch: "group these 100 summaries into 5-15 topics.
           Return topic title + one-line description."
         -> 6 local outlines.          (6 LLM calls, all parallel)

Step 2b  One call: "merge these 6. Same topic under two names = one topic.
           Return a 2-level tree: chapters -> pages."
         -> the page list.             (1 LLM call)

Step 2c  NO LLM. Create every page EMPTY: title, description, path, parent.
```

**~7 LLM calls for the entire book.** The pages have no content yet — they are shelves.

Write the shelf to disk as JSON before filing anything. Look at it. Fix it by hand if it
is wrong. It is cheap to regenerate.

> **When a GROWI already exists, skip Pass 2 entirely.** The shelf is their existing
> page tree. See Part 3.

### Pass 3: Route each chunk onto a page — *new, this is the core*

For each chunk, in five steps. Only step 4 uses the LLM.

| # | Step | Cost |
|---|---|---|
| 1 | Embed each page's `title + description` — **once**, ~120 vectors | tiny |
| 2 | Embed the chunk summary (needed for search anyway) | tiny |
| 3 | kNN chunk -> pages, take **top 5 candidates** | 0 (vector math) |
| 4 | One small LLM call: chunk summary + 5 candidate titles/descriptions -> `page_id` or `"new"`, a `## heading`, a confidence | **~60 output tokens** |
| 5 | Append the **verbatim chunk body** under that heading | 0 |

The LLM call in step 4 never sees the chunk body and never sees the wiki — only five
one-line descriptions. That is what keeps it small and stops it being an agent.

**Rules that keep this sane:**

- **Order inside a page = book order.** Chunks keep their original sequence. It is almost always the coherent one, and it is free.
- **One chunk goes to exactly one page.** If it also fits a second page, add a *link* from that page. Never paste the same text twice — duplication inflates the wiki and breaks dedup.
- **Low confidence or `"new"`** -> park in a holding list. After all chunks are routed, run one extra Pass-2-style call over just the holding list to create the missing pages, then route those.
- **Every appended section carries a hidden marker:** `<!-- chunk: <id> lines 4300-4405 hash:<h> -->`. This is what makes re-ingest and citation work.

Optional speedup: batch 10 chunks that share the same candidate set into one call.
Cuts the call count ~10x. Do this only if the per-call latency turns out to hurt.

### Pass 3.5: Fix the sizes — *no LLM, pure Python*

Now that pages have real content, sizes are known. Fix them with arithmetic:

| Rule | Value |
|---|---|
| Chunks per page | 3-8 |
| Assembled page length | 300-800 lines |
| Under 150 lines | merge into parent or nearest sibling |
| Over 900 lines | split at a heading boundary near the middle |
| Chapter holding one page | the chapter becomes that page |

Splitting is always safe because **chunks are atomic and never cut** — a split only ever
falls between two `##` sections.

This is where "not thousands of lines, not ten lines" is enforced, and it costs nothing.

Note the numbers are larger than a rewritten wiki would use: verbatim text is not
compressed, so the same amount of knowledge takes more lines. That is expected.

### Pass 4: Stitch — *new, and deliberately restricted*

One LLM call per page. **It may not touch chunk bodies.**

The call does not return markdown. It returns a **list of operations**:

| Operation | What it inserts |
|---|---|
| `insert_intro` | 3-5 lines at the top of the page |
| `set_heading` | normalize a section heading |
| `insert_transition_before` | one line before a section where the jump is jarring |
| `add_see_also` | links to sibling pages |
| `mark_duplicate` | flag two sections that repeat, for a human |
| `reorder` | move a section, by id |

Because the output is a list of ops applied by our code, **the verbatim body is
untouchable by construction** — not by asking the model nicely. That is the whole point.

Input: the page title, the ordered list of section headings and their summaries
(**not** the bodies), and sibling page titles. Output ~200 tokens.

### Pass 5: Link the pages — *mostly free*

| Link type | How | Cost |
|---|---|---|
| Parent / child | From the tree path | 0 |
| "See also" | From `add_see_also`, plus title-mention matching | 0 |
| Page -> its chunks | The hidden markers from Pass 3 | 0 |
| Related pages | Embedding kNN between ~120 **pages**, not ~600 chunks | small |
| Typed semantic edges | Existing LLM edge code, on ~120 pages only | ~5x cheaper than today |

Today the expensive LLM linking runs on all 600 chunks. Now it runs on 120 pages.

### What it costs — 60,000-line book, rough estimates

Generation (decode) is the bottleneck at ~50 tok/s, so count **output tokens**:

| | Today | Full rewrite | **Append (this plan)** |
|---|---|---|---|
| Chunk metadata | in per-node calls | 72k | 72k |
| Shelf / outline | 0 | 10k | 10k |
| Writing pages | 0 | **630k** | 0 |
| Routing | 0 | 0 | 36k |
| Stitch ops | 0 | 0 | 24k |
| Enrichment + edges | 900k (on 600 chunks) | 96k (120 pages) | 96k (120 pages) |
| **Total output** | **~900k** | **~810k** | **~240k** |
| At 50 tok/s, serial | ~5 h | ~4.5 h | **~80 min** |
| At 4x concurrency | ~75 min | ~68 min | **~20 min** |

A full rewrite barely helps — it just moves the generation from enrichment into prose.
**Appending is the actual win**, because the book is copied, not re-typed.

### What we give up by not rewriting — be honest

| | Cost |
|---|---|
| Duplicate material | **Not merged.** The stitcher only flags it. Two chunks explaining the same thing stay as two sections. |
| Narrative flow | A page reads like a well-ordered collection of excerpts, not one essay. |
| Page length | Longer, because verbatim text is uncompressed. |
| Mixed register | Chunks from different chapters may have different tone. |

That is the trade: **accuracy and speed, in exchange for polish.** For a technical manual
this is the right trade — an engineer wants the exact original wording anyway.

**Escape hatch:** keep a per-page `rewrite: true` flag. A human sets it on the handful of
pages where merging really matters (a glossary assembled from 20 scattered mentions),
and only those pages pay the rewrite cost. Best of both, chosen by a person.

### Files to touch for Part 2

| File | Change |
|---|---|
| `graph/chunk.py` | keep Pass 1 untouched; add `plan_shelf()` (Pass 2), `route_chunks()` (Pass 3), `stitch_page()` (Pass 4) |
| `graph/librarian.py` | `chunk_and_ingest` calls the new passes; **chunks** get embeddings + FTS only, **pages** get the full enrichment |
| `graph/store.py` | add `page_id`, `parent_path`, `chunk_ids`. `source_ranges` is **already** `list[tuple[int,int]]` (`core.py:299`), so a page can already hold many non-contiguous ranges — almost no schema change |
| `graph/core.py` | new node type `page`, beside `endogenous` / `exogenous` |
| `graph/researcher.py` | search chunk-level evidence, answer with and cite **pages**. `_build_search_items` (`librarian.py:1880`) already splits a body into 3000/512-char evidence units with `start_char`/`end_char`, so big assembled pages still retrieve precisely |

Do Part 2 **before** touching GROWI. It has value on its own, and it works with the
SQLite you already have.

---

## Part 3 — GROWI

### What GROWI is

An open-source (MIT) company wiki from GROWI Labs. Node.js + React. Pages in
**MongoDB**, search through **Elasticsearch** with Japanese kuromoji, optional Redis.
Pages are hierarchical by path (`/manual/api/errors`). Official Docker images, REST API
v3, and an official MCP server (`@growi/mcp-server`).

### Why this is a real fit, not just "another database"

| We built | GROWI already has it |
|---|---|
| `nodes.body` = page text | MongoDB `pages` + `revisions` |
| `nodes_fts`, `search_items_fts` (SQLite FTS5) | Elasticsearch with Japanese analysis |
| ~500 lines of `/admin/api/dbs/*` — create / upload / copy / rename / delete `.sqlite` | Page tree management, built in |
| `db_routing` middleware, one `.sqlite` per wiki | Page paths + user groups |
| `MarkdownView.jsx`, `DocSidebar.jsx`, `UploadView.jsx` | A real wiki UI: editor, diff, history, comments, tags, attachments |
| nothing | Permissions, ACL, SSO / LDAP |

**We are maintaining a worse version of a wiki so we can hang a RAG engine off it.**

### Why the integration *improves GROWI*

GROWI's own AI feature ("knowledge assistant") is built on **OpenAI's hosted Vector
Store**. On an internal network with local vLLM models — exactly this setup, with
`10.160.144.101` serving chat, `ruri-v3` embeddings and a reranker on ports
51024/51025/51029 — that feature **cannot be used at all**.

What we have that GROWI does not:

- local embeddings + local reranker, no data leaving the network
- the concept graph: edges, clusters, claims, neighbourhood cache
- the book chunker — GROWI has no way to eat a 3000-page PDF
- the realtime answering pipeline in `realtime.py` (plan -> fast answer -> deep -> anticipation, streamed)

**GROWI is the wiki. We are the brain.**

### Append mode makes the GROWI story much stronger

This is the part that changed. Because Pass 3 files a chunk onto an **existing** page,
the shelf does not have to be one we built:

```
   Existing GROWI, 800 pages already written by humans
                          |
                          v
   Embed each page's title + first lines   -> the shelf
                          |
   New 3000-page PDF -> Pass 1 chunks -> Pass 3 routes them
                          |
                          v
   Chunks land as new ## sections on the RIGHT existing pages,
   verbatim, marked, with a link back to the source PDF.
   Nothing a human wrote is overwritten.
```

That is a feature GROWI has no way to offer: **drop a manual in, and it files itself into
the wiki you already have.** Because nothing is rewritten, a reviewer can see exactly
what was added and where it came from, and revert one section without touching the page.

Safety rules for writing into somebody's real wiki:

- Append only. **Never** modify or delete text we did not add.
- Every added section is wrapped in its marker comment, so it can be found and removed.
- Land in a draft/staging path first (`/inbox/<book>/...`) and let a human approve the move — at least until it is trusted.
- One GROWI revision per page per ingest, not one per chunk, so page history stays readable.

### Where the line goes

| Thing | Owner after integration |
|---|---|
| Page text, title, path, history, permissions, UI, attachments | **GROWI** (MongoDB) |
| Full-text search | **GROWI** (Elasticsearch) |
| Chunks / evidence | us |
| Vectors, edges, clusters, neighbourhood cache | us |
| Chunk -> shelf -> route -> stitch | us |
| `ask` / `ask_realtime` | us |

> **Our database stops being where the wiki lives. It becomes an index pointing at GROWI pages.**

Every row we keep carries `growi_page_id` and `growi_revision_id`. If our index is
deleted, nothing is lost — we rebuild it by reading GROWI again.

### "Can I connect to an existing GROWI?" — yes. Two ways.

**Mode A — API (use this one).**

- Admin API token; `GET /_api/v3/pages/...` to read, `POST /_api/v3/pages` to create, update to write
- Works with any GROWI version, and with hosted GROWI.cloud
- Respects permissions
- Cannot corrupt anything
- Slower for a first big backfill

**Mode B — read MongoDB directly (backfill only).**

- Read the `pages` and `revisions` collections
- Fast enough to index a large existing wiki in one pass
- **Read-only. No exceptions.**

> **Never write to their MongoDB.** GROWI keeps its own bookkeeping (`parent`,
> `descendantCount`, `isEmpty`, `grant`, the revision chain) and pushes updates into
> Elasticsearch. Writing behind its back corrupts the page tree and desyncs search.
> **Writes always go through the API.**

In practice: Mode B for the first full crawl, Mode A for everything after.

### Staying in sync

Store per page: `growi_page_id`, `growi_revision_id`, `path`, `updated_at`, `index_hash`.

| Event | What we do |
|---|---|
| New page | chunk it if huge, embed, add to the graph |
| Edited (revision id changed) | re-embed that page, refresh its edges |
| Renamed / moved | update the path, keep the vectors |
| Deleted | remove from the index |

How we hear about it: **poll first** — every few minutes, list pages by `updatedAt` and
compare revision ids. Boring, reliable, works everywhere. GROWI's admin Global
Notification can also fire an outgoing webhook on a trigger path, which is faster;
verify the payload shape on your version before depending on it.

### Re-ingesting a changed book

Because every section carries `<!-- chunk: <id> lines … hash:<h> -->`:

| Case | What happens |
|---|---|
| Chunk unchanged | skip, no call, no write |
| Chunk text changed | replace **that section only**, in place |
| Chunk deleted from source | remove that section |
| New chunk | route it normally |

Neighbouring sections are never disturbed, and a human's edits to other parts of the
page survive.

### How a book gets in

```
   PDF / big book
        |
        v
   parser (MinerU)  ->  markdown
        |
   Pass 1  chunk (verbatim)  ->  our index only, never its own GROWI page
        |
   Pass 2  shelf             ->  the GROWI page tree (or reuse theirs)
        |
   Pass 3  route + append    ->  PUT to the API, one revision per page
        |
   Pass 4  stitch (ops only) ->  intro, headings, transitions
        |
   Pass 5  link              ->  markdown links, $lsx() on chapter pages
```

`$lsx()` is a bundled GROWI feature that renders a list of child pages. Put it on each
chapter page and the navigation builds itself.

---

## Part 3.5 — How it is deployed: one engine, many GROWIs

**llm-wiki is its own small stack — the app plus a Qdrant container — and it connects out
to as many GROWI instances as you register.** GROWI instances are external. We never run
inside one, and we never own their data.

This keeps today's UX exactly as it is. Where the admin panel used to create, copy, rename
and delete `.sqlite` files, it now registers and manages **GROWI connections**. Where a URL
segment used to pick a `.sqlite`, it now picks a connection.

```
   ┌───────────────────────── our stack ─────────────────────────┐
   │                                                             │
   │   ┌──────────────────┐          ┌────────────────────────┐  │
   │   │    llm-wiki      │          │        qdrant          │  │
   │   │ chunk / route    │◄────────►│ one collection,        │  │
   │   │ embed / ask      │          │ growi_id payload filter│  │
   │   │ admin panel      │          └────────────────────────┘  │
   │   └───┬──────────┬───┘                                      │
   │       │          │      ┌──────────────────────────┐        │
   │       │          └─────►│ engine.sqlite            │        │
   │       │                 │ connections + tokens,    │        │
   │       │                 │ sync cursors, edges,     │        │
   │       │                 │ clusters, neighbourhood  │        │
   │       │                 └──────────────────────────┘        │
   └───────┼─────────────────────────────────────────────────────┘
           │  REST API v3 per connection
           │
     ┌─────┴──────┬──────────────┬──────────────┐
     ▼            ▼              ▼              ▼
 ┌────────┐  ┌────────┐    ┌────────┐    ┌────────┐
 │ GROWI  │  │ GROWI  │    │ GROWI  │    │ GROWI  │   external, each with
 │ manual │  │meetings│    │ specs  │    │  ...   │   its own mongo + ES
 └────────┘  └────────┘    └────────┘    └────────┘
   /prefix/manual/   /prefix/meetings/   /prefix/specs/
```

### The admin panel: same shape, different objects

| Today | After |
|---|---|
| `POST /admin/api/dbs/{db}` creates a `.sqlite` | registers a GROWI connection: URL, token, root path, mode |
| `GET /admin/api/dbs` lists files with row counts | lists connections with page counts, last sync, reachability |
| `upload` / `copy` | dropped — meaningless for a connection |
| `rename` | rename the connection label |
| `DELETE` | **detach**: drop our index for that GROWI. Their wiki is untouched. |
| `/prefix/{db}/` | `/prefix/{growi_name}/` |

**`db_routing`, `STACKS`, `stages`, `errors`, `building`, `_ensure_building`,
`_bootstrap_db` and `_close_stack` all survive.** The URL segment resolves to a connection
instead of a file path; everything downstream is unchanged. That is the single biggest
reason this migration is cheap — the multi-tenant machinery you already wrote is exactly
the machinery you need.

### Two ways to add a GROWI

| Mode | `mode` field | What happens |
|---|---|---|
| **Fresh** | `own` | Register an empty GROWI. llm-wiki creates the page tree as it ingests books, and may write anywhere under `root_path`. |
| **Attach** | `attach` *(default)* | Register an existing GROWI. llm-wiki crawls it, builds its index around it, and only ever **appends** into `write_path`. Existing pages are read, never modified. |

`attach` is the default, because the safe mode should be the one you get by accident.

### The connection registry — new, and it needs its own store

Each connection is a row:

| Field | Example |
|---|---|
| `name` | `manual` — this is the URL segment |
| `url` | `https://growi.internal:3000` |
| `api_token` | **encrypted at rest** |
| `mongo_uri` | optional, read-only, backfill only |
| `mode` | `attach` / `own` |
| `root_path` | `/` |
| `write_path` | `/inbox` |
| `sync_cursor` | last `updatedAt` seen |
| `stage`, `last_error`, `last_sync_at` | health for the admin list |

Plus a per-page table: `growi_page_id`, `revision_id`, `path`, `index_hash`.

This lives in **one `engine.sqlite`** — the app's own small database, not a wiki.

> **New security surface.** Config used to be environment variables; now admin-supplied
> **API tokens sit at rest in our database**. Encrypt them with a key from
> `WIKI_SECRET_KEY`, and redact them on read. `app.py` already does exactly this shape of
> redaction for model keys via `_SECRET_KEYS` and `_redact()` — extend that, do not invent
> a second pattern.

### Configuration surface

Environment now holds only **global defaults**, because per-GROWI settings moved into the
registry:

| Variable | Meaning |
|---|---|
| `QDRANT_URL` | `http://qdrant:6333` |
| `WIKI_ENGINE_DIR` | where `engine.sqlite` lives — replaces `WIKI_DB_DIR` |
| `WIKI_SECRET_KEY` | encrypts stored GROWI API tokens |
| `WIKI_ADMIN_PASSWORD` | unchanged, already exists |
| `WIKI_PREFIX` | unchanged, already exists |
| `WIKI_SYNC_INTERVAL_SECONDS` | default poll interval, overridable per connection |
| `OPENAI_BASE_URL`, `WIKI_MODEL`, `WIKI_EMBED_BASE_URL`, `WIKI_RERANK_BASE_URL` | unchanged |

### docker-compose sketch

Our stack is now **two containers**. The GROWI instances are somebody else's problem —
they may already exist, on other hosts entirely.

```yaml
services:
  qdrant:
    image: qdrant/qdrant:latest
    volumes: [qdrant_data:/qdrant/storage]

  llm-wiki:
    image: llm-wiki:latest
    ports: ["8000:8000"]
    environment:
      - QDRANT_URL=http://qdrant:6333
      - WIKI_ENGINE_DIR=/engine
      - WIKI_SECRET_KEY=${WIKI_SECRET_KEY}
      - WIKI_ADMIN_PASSWORD=${WIKI_ADMIN_PASSWORD}
      - OPENAI_BASE_URL=http://10.160.144.101:51029/v1
      - WIKI_EMBED_BASE_URL=http://10.160.144.101:51024/v1
      - WIKI_RERANK_BASE_URL=http://10.160.144.101:51025/v1
    volumes: [engine_data:/engine]
    depends_on: [qdrant]

volumes:
  qdrant_data:
  engine_data:
```

GROWI instances are added at runtime through the admin panel, not here. If you also want a
fresh GROWI for testing, bring up `growilabs/growi-docker-compose` separately and register
it — remembering that its Elasticsearch must be **built, not pulled**, because the stock
image lacks the `kuromoji` and `ICU` plugins GROWI requires.

### The property that makes this safe

> **Both our volumes hold nothing but derived state**, except the connection registry —
> which is a handful of URLs and tokens you can retype in a minute.

Drop the Qdrant volume and the index rebuilds from GROWI. Nothing a human wrote lives on
our side. `docker volume rm` is a supported recovery action, not a disaster.

### Startup — the existing machinery already does this

1. Load the connection registry from `engine.sqlite`.
2. Preload each connection at startup, bounded by `WIKI_STARTUP_BOOTSTRAP_CONCURRENCY`, exactly as `.sqlite` files are preloaded today (`app.py:252-326`).
3. Per connection: reach GROWI, resume from `sync_cursor`, or run a full backfill when the index is empty. Report `not_ready` with a stage while it runs.
4. Start one sync poller per connection.

`_bootstrap_db()` becomes `_bootstrap_connection()` and everything around it is unchanged.
`/api/ready`, `stages`, `errors` and `_not_ready_detail()` already report per-segment,
which is now per-GROWI.

> **One concrete change required.** `WIKI_STARTUP_REQUIRE_ALL_DBS` defaults to `true`
> (`app.py:247-251`) and raises, killing startup, when one entry fails to preload. With N
> external GROWIs that default is clearly wrong — one unreachable instance must not take
> the whole service down. Flip it: log, mark that connection failed, serve the others.

### What happens when something breaks

| Failure | Result |
|---|---|
| **llm-wiki is down** | **Every GROWI is unaffected.** People read and edit normally; they just have no AI. Zero blast radius. |
| One GROWI unreachable | That segment reports `not_ready`. **Every other wiki keeps working.** `stages[name]` already models this exactly. |
| GROWI down after indexing | We keep answering from our index — stale, but useful. Sync resumes on return. |
| Qdrant volume lost | Rebuild from the registered GROWIs. Costs one backfill each, loses nothing. |
| `engine.sqlite` lost | Re-register the connections, then rebuild. The only real loss is the token list. |
| A page deleted in GROWI | Dropped from the index on the next sync. |

Rows one and two are the argument that gets this approved: **we cannot break a wiki, and
one bad wiki cannot break the others.**

### Isolation between wikis

One Qdrant collection, partitioned by a `growi_id` payload filter — see Part 4.5. Detaching
a wiki is a filtered delete, and no query can cross the filter.

If some corpus genuinely must be sealed off at the process level, run a second llm-wiki
stack. But the payload filter is the default answer now, not a second container.

### Reaching it from the browser

The chat panel is a **GROWI script plugin** — script plugins can inject UI components and
call external APIs, and are installed from a GitHub repository through the admin screen.

With many GROWIs behind one engine, the plugin must say **which** instance it is calling
from. Two options:

- **Explicit:** the plugin is configured with its connection name and calls `/prefix/{name}/api/...`. Simple, obvious, recommended.
- **Inferred:** llm-wiki matches the request `Origin` against registered connection URLs. Fewer moving parts for the installer, but breaks behind an unexpected proxy.

Use the explicit form. It is one config field and it fails loudly instead of silently.

Because the plugin now calls across origins, CORS matters: allow exactly the registered
GROWI origins rather than the current `allow_origins=["*"]` (`app.py:802-807`).

---

## Part 4 — What dies, what lives

**Delete:**

- `store.py` — `nodes_fts`, `search_items_fts`, `_fts_query`, page-body storage, and the four `vec_*` tables (they move to Qdrant)
- `librarian.py` — `create_document_node` / `update_node` / `delete_node` become "index this GROWI page"
- `app.py` — the sqlite-file specifics: `_validate_sqlite_file`, `_migrate_sqlite_file`, `_db_summary_from_path`, `_unlink_db_files`, `admin_upload_db`, `admin_copy_db`
- frontend — `MarkdownView.jsx`, `DocSidebar.jsx`, `UploadView.jsx`

**Survives, repurposed** — this list got much longer, and that is good news:

| Kept | Now does |
|---|---|
| `db_routing` middleware | segment picks a **connection**, not a file |
| `STACKS`, `stages`, `errors`, `building` | one entry per **GROWI**, not per file |
| `_bootstrap_db`, `_ensure_building`, `_close_stack` | bootstrap/teardown a **connection** |
| startup preload + concurrency semaphore | preload **connections** |
| `/admin/api/dbs/*` | `/admin/api/connections/*` |
| `AdminApp.jsx` (1447 lines) | manages **connections**, not files |
| `_SECRET_KEYS`, `_redact()` | now also redacts GROWI API tokens |
| `/api/ready`, `_not_ready_detail()` | per-connection readiness |

**Keep, barely touched** (the valuable part):

- `graph/researcher.py`, `graph/realtime.py`, `graph/vocab.py`, `graph/neighborhood.py`, `graph/gateway.py`
- `graph/chunk.py` — pass 1 untouched, plus the new passes
- `_rrf_fuse` and the weighted-RRF settings
- `mcp_server.py` — our MCP tools

The earlier draft of this plan said to delete the admin panel and the routing middleware.
**That was wrong.** Serving many GROWIs needs exactly the multi-tenant machinery already
written; only the object it points at changes.

---

## Part 4.5 — Where do the vectors live?

Serving many GROWIs settles this. **Qdrant.**

### Why not the Elasticsearch that GROWI already runs

That was the right answer when llm-wiki sat beside exactly one GROWI. With N instances it
collapses: **whose Elasticsearch?** Each GROWI has its own cluster, on its own lifecycle,
sized for its own wiki. Writing our index into somebody else's cluster couples us to every
one of them, and makes "detach this wiki" a cross-cluster problem.

It also delivered less than hoped: ES's native `rrf` retriever is **Enterprise-licensed**,
so on the free tier you fuse client-side anyway.

### Why not stay on `sqlite-vec`

For one small corpus it is still fine. But the model changed: N wikis in one engine means
one shared index with tenant filtering, and a brute-force scan over every tenant's vectors
is precisely the thing that stops scaling. Adding a wiki would slow down every other wiki.

### Qdrant

| | |
|---|---|
| **License** | Apache 2.0 — no tier gating, unlike ES RRF |
| **Ops** | one container, one volume, one Rust binary |
| **Multi-tenancy** | first-class, and documented — see below |
| **Detaching a wiki** | a filtered delete |
| **Independence** | tied to no GROWI's lifecycle |

### The multi-tenancy pattern to use

Qdrant's own documented recommendation, and it fits this exactly:

- **One collection** for everything. Not one per GROWI — many collections cost resource overhead.
- Tag every point with a **`growi_id`** payload field.
- Create a payload index on it with **`is_tenant: true`**. This step is what makes it fast; without it you are filtering the hard way.
- Set **`hnsw.m: 0`** and **`payload_m: 16`** so Qdrant builds a per-tenant index instead of one global graph. Without this, tenants block each other during indexing — adding a wiki stalls the others.
- Keep the existing channel names (`body`, `summary`, `bridge`, `search_item`) as a second payload field, or as named vectors on one point.

> **The constraint this imposes: every GROWI must use the same embedding model.** A single
> collection requires homogeneous vectors. Fine with one `ruri-v3` everywhere — but a wiki
> that needed a different embedder would need its own collection, and that is a decision to
> make deliberately, not by accident.

### What stays in `engine.sqlite`, no matter what

The connection registry and its encrypted tokens, sync cursors, per-page revision ids,
edges, clusters, `node_neighborhood`, the write-job queue, and chunk markers.

Small, relational, and no vector database is a better home for it. **SQLite does not
disappear — it stops holding wiki text and becomes the engine's own bookkeeping.**

---

## Part 5 — Do it in this order

Each phase ends with something that works. You can stop after any of them.

### Phase 0 — merge the speedup (1-2 days)

Take from `benchmark`: 3-phase `create_document_nodes`, `_EDGE_GROUP_SIZE = 8`,
`recluster_every = 0`, chunk planning concurrency. Do not change the output shape yet.

**Done when:** same result as today, several times faster. No GROWI involved.

### Phase 1 — route and append, still in SQLite (1-2 weeks)

Build Pass 2 (shelf), Pass 3 (route + append), Pass 3.5 (sizes), Pass 4 (stitch),
Pass 5 (link). Chunks stop being wiki pages.

**Done when:** one real book produces ~120 assembled pages in a tree, each 300-800
lines, every section traceable to its source line range — and `ask()` still answers
correctly.

**Check before moving on:** open ten pages and read them. Is the *routing* right? A
misfiled chunk is the only real failure mode of this design, and it is visible by eye.
Tune the candidate count and the routing prompt here, not later.

### Phase 2 — publish to GROWI (1 week)

New module `graph/growi.py`, API client only. After Pass 4, push each page to GROWI.
Our SQLite stays the source of truth; GROWI is a mirror.

**Done when:** ingest a book, open GROWI, browse the tree, and it reads like a wiki.
Nothing was removed on our side, so nothing can break.

### Phase 3 — many GROWIs through the admin panel (1-2 weeks)

The payoff phase, and the one that reuses the most existing code.

Repoint the admin panel from `.sqlite` files to **GROWI connections**, and repoint
`db_routing` from a file path to a connection. Build the registry in `engine.sqlite`, with
encrypted tokens. Move vectors to Qdrant with the `growi_id` tenant filter. Then, per
connection: build the shelf from **their** pages, backfill, and file new chunks onto their
existing pages through the API — append only, into a staging path, with markers.

**Done when:** you register two different GROWIs in the admin panel, reach them at
`/prefix/manual/` and `/prefix/specs/`, drop a PDF on one, and it files itself into the
right pages — while the other wiki is unaffected, and a reviewer can revert every single
thing that was added.

**This is the phase that sells the whole project.**

### Phase 4 — make GROWI the source of truth (1 week)

Smaller than it looks, because Phase 3 did the structural work. Delete the FTS tables, the
`vec_*` tables, the sqlite-file helpers (`_validate_sqlite_file`, `_migrate_sqlite_file`,
upload/copy) and the wiki-viewer frontend. Ship the chat as a GROWI script plugin,
configured with its connection name. Tighten CORS to the registered GROWI origins.

**Done when:** dropping the Qdrant volume and rebuilding from the registered GROWIs loses
nothing.

---

## Part 6 — Check these before you start

1. **Who stands up the GROWI instances?** Our own stack is only two containers (llm-wiki + Qdrant) — light. But each GROWI needs MongoDB + Elasticsearch + Redis, and Elasticsearch alone wants ~2 GB RAM. **If you are attaching to GROWIs that already exist, that cost is already paid and this stops being your problem.** If you must stand up the first one yourself, ask ops first.
2. **Elasticsearch needs the `kuromoji` and `ICU` plugins** for Japanese search. Confirm they can be installed offline.
3. **Is there an existing GROWI already?** Which version — v7.3+ if you want the official MCP server. Do you get an admin API token, and read access to MongoDB?
4. **Adding a wiki becomes two steps, not one.** Today a typo in the URL used to create a wiki. Now a GROWI must exist first, then you register it in the admin panel. That is a safety improvement, but confirm nobody depended on the one-step behaviour.
5. **Isolation changes.** One `.sqlite` per wiki was hard separation; a Qdrant payload filter plus GROWI ACL is softer. If any wiki holds data that must not leak, confirm the tenant filter and GROWI's group permissions are enough, or run a second llm-wiki stack.
6. **Same embedding model everywhere?** One Qdrant collection needs homogeneous vectors. With one `ruri-v3` this is free — but confirm no wiki will need a different embedder. See Part 4.5.
7. **Where does `WIKI_SECRET_KEY` come from?** GROWI API tokens now sit at rest in `engine.sqlite`. Decide who holds that key before the first connection is registered.
8. **Will anyone accept machine-appended sections in their wiki?** This is a people question, not a technical one. The staging path and the markers exist to make the answer yes.
9. **Do you have admin tokens for every GROWI you intend to register?** Each connection needs its own. Getting one is a conversation with whoever owns that wiki, and it is the slowest step in Phase 3.

---

## The summary

1. Keep chunking **verbatim by line number**. The LLM never rewrites source text — copied bytes cannot be hallucinated.
2. Chunks are **material**, not pages. Pages are **shelves** you append material onto.
3. Decide the shelf **first** from cheap summaries — about 7 LLM calls for a whole book.
4. Routing a chunk = kNN to 5 candidate pages + **one ~60-token call**. The model never sees the wiki. No agents.
5. The stitcher returns **operations, not markdown**, so verbatim text is untouchable by construction.
6. Control page size in **Python**: 3-8 chunks, 300-800 lines, split only at heading boundaries.
7. ~900k generated tokens today, ~810k for a full rewrite, **~240k for append** — about 20 minutes instead of 5 hours.
8. What we give up: duplicates are flagged, not merged, and pages read as ordered excerpts. Per-page `rewrite: true` is the escape hatch.
9. **One engine, many GROWIs.** The admin panel stops managing `.sqlite` files and starts managing **GROWI connections**; `/prefix/{name}/` picks a connection. `db_routing`, `STACKS` and the whole preload path survive unchanged — that machinery is exactly what multi-tenancy needs.
10. Vectors go to **Qdrant**: one collection, `growi_id` payload filter with `is_tenant: true`. Not Elasticsearch — with N instances there is no single ES to use. `engine.sqlite` keeps the registry, tokens, graph and clusters.
11. The payoff: **drop a PDF onto any registered GROWI and it files itself into the right pages** — append only, marked, revertible, and one broken wiki cannot touch the others.
