# New growi-search — plan

Status (2026-09-29): **implemented, not yet run or tested.** The plan below is kept as the
design. Where the code differs from it, **Implementation notes** at the end says so. The
**Handoff** section at the very end is for the agent that runs and tests it on the work PC.

## Why

The growi-search backend has grown into one 3,700-line `researcher.py` with stacked JEV stages,
four LLM calls that run one after another before any page is read, a mirror that stops syncing,
and three search modes (exhaustive, cascade, walker). It is slow, it misses answers, and agents
can't change it safely any more. We rewrite the backend. The frontend stays.

## What changes, in one picture

```
TODAY
GROWI ──mirror polls (freezes)──► growi-search ──► 目次 cards only (1 line per page)
                                      │
                                      └─► JEV + 4 LLM calls in a row ──► subagents ──► answer

NEW
wiki builder ──publish──► GROWI  (content pages + 目次 pages that carry a full data block)
                            │
growi-search ──reads 目次───┘──► local Qdrant (built and owned by growi-search)
     │
     └──► finder ──► two-dial researcher ──► compiler ──► answer
```

The builder only talks to GROWI, as it does today. Everything search needs travels inside the
目次 pages.

## The 4 parts

### 1. Wiki builder: small changes (`graph/`, `publisher/`)

- **Richer metadata per section, from the same LLM call.** The builder already runs one
  `chunk_meta` call per section, on pre-link text. We extend that call (no new pass). The fields
  the linker uses today (`summary`, `keywords`, `entity`, `entities`, `behaviours`) stay exactly
  as they are, so link quality doesn't move. What gets added or switched back on, for search:

  | field                        | what it holds                                                                                                                                              | handoff wish it covers                                         |
  | ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
  | `kind`                       | what kind of information the section is: 「mpf_mfs_open の API リファレンス、パラメータ、使用例」, not just 「mpf_mfs_open」                             | "what the page contains and what kind of info it is"          |
  | `points`                     | a point list of what the section covers, in general words (no values), so it stays true after small edits                                                | point-wise summary; "general, not specifics"                  |
  | `search_terms`               | every name, function, parameter, constant, error code and value in the section, plus the words a reader would type to find it. No cap. | more keywords, a stronger keyword prompt, all names (漏れなく) |
  | `facts` (today's `claims`)   | short one-line facts, specifics included                                                                                                                   | claims back on; small searchable units                         |
  | `bridge` (`bridge_probe`)    | 1–2 sentences on the wider topic the section connects to                                                                                                   | bridge back on (the old engine embedded it)                    |

  The specifics live in `facts` and `search_terms`, and the summary, `kind` and `points` stay
  general. When a section changes, its hash changes and all of its fields are rebuilt, so the
  specifics never go stale.

  Three small fixes ride along:
  - `chunk_meta` must see the whole section. Today `model_text` (`graph/linker/chunks.py:134`)
    cuts it at 12,000 characters and drops big tables, so parameter tables never reach the
    metadata.
  - The prompt must ask for each entity's `kind`. `validate_meta` silently drops every entity
    without one.
  - `validate_meta` keeps only 12 keywords and 20 claims. The new `search_terms` and `facts` get
    no such cap.
- **Page entries built from their sections (no extra LLM call).**
  - page summary: `coverage.summary`, as today
  - page `kind`: its sections' kinds
  - page `points`: its sections' points
  - page terms: all of its sections' terms

  This replaces today's "first section summary only" card.
- **A data block at the bottom of every 目次 page.** Below the human-readable list, each 目次
  gets a collapsed block of JSON holding everything the builder knows. It has no caps: the
  `MAX_*` limits in `publisher/index.py` apply only to the visible list.

  ```
  document 目次   → per page:    page id, revision, path, title, summary, kind, points
                    per section: heading, line range, text hash, summary, kind, points,
                                 keywords, search_terms, facts, entities (name, kind, role),
                                 behaviours (who does what), bridge
  folder 目次     → per child document/folder: name, 目次 path, summary, page count,
                    + hash of that child's block
  root 目次       → the same, for the project's top level
  ```

  These are the 3 levels the handoff asked for. The block is built from the pre-link metadata
  (`_planning/chunks.json`), never from the rendered page, so cross-links can't pollute it.
  The visible part of the 目次 gets denser as well: each page shows its kind and points.
- **One small shared parser** (e.g. `graph/common/mokuji_data.py`): `render(...)` for the
  builder and `parse(markdown)` for growi-search, returning pages, sections and documents.
  One format and one parser on both sides, so the two can't drift apart.

### 2. Storage: a local Qdrant inside growi-search

- growi-search builds it from the 目次 data blocks and the page bodies, and computes the
  embeddings itself. A section whose text hash hasn't changed is never embedded again.
- One point per section (plus one per fact and per window of a long section), each carrying
  meaning vectors, sparse BM25 vectors and everything else as payload. Page, document and
  folder entries are points too. A `level` field tells them apart.
- **Where every data block field goes in Qdrant, and what uses it:**

  ```
  section body (read from GROWI, links    → dense "body" + sparse "body_bm25"   → FIND: meaning + exact words
    stripped with the builder's own          (long sections and big tables are
    strip_reader_references)                  split into windows / row groups,
                                              so nothing is cut before embedding)
  heading + title + search_terms          → sparse "terms" (BM25)               → FIND: names, parameters, error codes
    + keywords + entity names
  summary + kind + points                 → dense "gist"                        → FIND: "what is this section about"
  bridge                                  → dense "bridge"                      → how/why questions that no section
                                                                                   states directly (file deadlock)
  facts                                   → one small point per fact            → needle hunts, "3rd parameter of X"
                                            (level=fact, points to its section)
  entities (name, kind, role)             → payload, indexed by name            → definition(name): role=defines
  behaviours (subject, action, object)    → payload                             → GROUP threads by shared names;
                                                                                   researchers follow who does what
  page id, revision, path, document,      → payload, indexed                    → project scope, group by page, sync
    project, heading, line range, hash
  page / document / folder entries        → points with level=page/document/    → list and broad questions:
                                            folder                                 find the right document first
  ```

  One point per fact plus the windows give the small, separately searchable units the handoff
  asked for (its "line-by-line summary" idea), without an extra LLM pass.
- Japanese needs its own tokens for BM25: growi-search makes the sparse vectors itself (Latin
  words + CJK character bigrams, like today's keyword channel), and Qdrant applies the IDF.
- It runs in local file mode (`QdrantClient(path=...)`), so there's no server to run. Switching
  to a Qdrant server later only means changing the connection line.
- Chroma was rejected because its BM25 search only works in the cloud version.
- The Qdrant data is disposable: delete the folder and growi-search rebuilds it from GROWI.

### 3. Sync: follow the 目次 hash tree

```
every ~30 s, per project:  read the root 目次 only (1 GROWI call)
  → compare each child's hash with what we indexed last time
  → walk down ONLY into changed folders/documents
  → changed document: re-read its page list, fetch pages whose revision changed,
    re-index those sections; delete points of pages that disappeared
```

- Hand edits in GROWI still reach search: the builder's watch loop pulls them
  (`pull_growi_once`), rebuilds the 目次, and the new hashes show the change.
- This needs no audit log, no `/pages/recent` and no 30-minute relist, which were the three
  things that froze the old mirror. The mirror is deleted.

### 4. growi-search backend: rewritten

**Finder** (fast, no LLM)

```
question → Qdrant hybrid search (meaning + keywords + exact names) → reranker
         → JEV scores ALL the top sections at the same time, asking 2 things about each:
              A: does it answer the question directly?
              B: does it help answer it?
         → still hitting near the bottom of the list? → one more wave, again all at once
```

**Two dials** (measured from what the finder found, not guessed from the question)

```
WHERE    how many places hold relevant material   → how many researchers per round
HOW FAR  is the answer written down, or must it   → how many research rounds
         be pieced together from several parts?
```

**Research loop**

```
quick exit only if: A is high AND the question isn't asking how or why → 1 LLM call answers
otherwise:
  group evidence into threads by shared names/links (not by document)
  round 1: one researcher per thread, all at once (can read anything, follow links, look up definitions)
  lead check, once every report is in: "is the explanation complete? what is still open?"
  open gaps → next round (max ~3 rounds)
  compiler: 1 stage if all reports fit in one LLM input, else 2 stages
```

**The whole search, step by step**

Each step runs its own work all at once, then waits for all of it before deciding the next step.
See **Speed** below for why.

```
question
  │
  ▼
FIND  (fast, no LLM)
  local Qdrant hybrid search: meaning + BM25 keywords + exact names (mpf_mfs_open, EBUSY)
  → reranker sorts sections, best first
  │
  ▼
JEV WAVE: score the top ~100 sections ALL AT ONCE, 2 questions each
  (it reads each section once):
    A: "does this section answer the question directly?"
    B: "does this section help answer it?"
  wait for every answer
  │
  ▼
look at the answers (no LLM):
  hits near the bottom of the list, or nothing found at all?
    → next wave: the next ~100 sections + the neighbours of documents that hit
      (lists usually sit together in one chapter), all at once, wait for every answer
  otherwise → stop
  │
  ▼
evidence map: the helpful sections, the names in them, the links between them
  │
  ▼
QUICK EXIT?  only if A is high (written in one place) AND the question isn't asking how or why
  │   yes → read those pages → 1 LLM call answers → DONE
  │   no
  ▼
GROUP the evidence into THREADS by connection (shared function names / links),
not by document
  │
  ▼
ROUND 1: one researcher per thread, ALL AT ONCE         (WHERE dial = how many threads)
  wait for every report
  - may read ANY section or page, nothing is blocked
  - tools: read, follow_link, search (local Qdrant), definition(name)
  - report = what I found  +  what I still don't understand
  │
  ▼
LEAD CHECK  (1 LLM call over ALL the reports)
  "is the explanation complete? which questions are still open?"
  ├─ gaps     → each gap becomes a new question for the next round,
  │             seeded with the sections from BOTH sides of the gap
  │             + a fresh search on the gap's words
  │             → ROUND 2 (all gaps at once) → LEAD CHECK → ...  (HOW FAR dial = how many rounds,
  │                                                      hard cap ~3)
  └─ complete
  │
  ▼
COMPILER
  all reports fit in one LLM input? → 1 stage
  otherwise                         → 2 stages (all L1 folds at once → L2 sees only L1 outputs)
  │
  ▼
answer + citations → check against the source pages → DONE
```

**Example runs** (made-up numbers, just to show where each question lands)

```
3rd param of mpf_mfs_open    FIND: A=0.95, 1 page         → quick exit, 1 LLM call
what is file deadlock        FIND: A low, 3 threads       → 2 rounds, 1-stage compiler
all functions of moove       FIND: 12 threads             → 1 wide round, no gaps → 2-stage compiler
why does X fail after Y      FIND: A low, many threads    → wide + several rounds → 2-stage
```

**"what is file deadlock" in detail** (no page defines it)

```
FIND      best A ≈ 0.3                    → nothing states the answer
          helpful sections in 3 areas     → lock functions, file open modes, error/wait behaviour
QUICK?    no (A is low)
GROUP     3 threads (they share lock / file-handle names)
ROUND 1   r1: "function type A takes the file lock and holds it"
          r2: "function type B waits for that lock"
          r3: "a certain error appears when the wait never ends"
          open question: "what if A and B run at the same time?"
LEAD      gap found → round 2
ROUND 2   1 researcher, seeded with r1's + r2's sections
          → "A and B running together each wait on the other: that is the file deadlock"
LEAD      complete
COMPILER  reports are small → 1 stage → answer
```

### Speed: run wide, then decide

The rule: **cheap work runs all at once first; expensive work starts only after every cheap
answer is in.** JEV is cheap and the LLM researchers are expensive. One JEV "yes" can make
researchers unnecessary (a direct answer means a quick exit, or the hit lands in a thread that
already exists), so no researcher starts until every JEV answer has arrived. Nothing is computed
for nothing.

**One question, top to bottom**

```
question
  │
  ▼
Qdrant search + rerank                                   (milliseconds)
  │
  ▼
JEV wave 1:   s1  s2  s3  …  s100   ← all at once     ═╗
  wait for every answer                                 ║
  (hit pages are prefetched from GROWI meanwhile)       ║  cheap: no LLM spent yet
  │                                                     ║
  ▼                                                     ║
DECIDE: still hitting near the bottom, or nothing at all?
  yes → JEV wave 2: next ~100 + neighbours of documents that hit, all at once, wait
  no  → FIND is done                                   ═╝
  │
  ▼
DECIDE: strong A and the question isn't asking how or why?
  yes → 1 LLM call answers → DONE   (no researcher ever started)
  no
  │
  ▼
researchers:  r1  r2  r3  …  rN   ← one per thread, all at once   ═╗
  wait for every report                                             ║
  │                                                                 ║  expensive: LLM
  ▼                                                                 ║
lead check (1 LLM call, sees ALL reports)                           ║
  gaps → gap researchers, all at once, wait → lead check (cap ~3)   ║
  complete                                                         ═╝
  │
  ▼
compiler: 1 stage, or 2 stages with all L1 folds at once
  → answer streams to the user token by token
```

**Why each step waits for all of its work**

```
wait for every JEV answer  before researchers  → a direct hit may make researchers unnecessary
wait for every report      before lead check   → one report may already fill another's gap
wait for the lead check    before compiling    → don't compile an answer that is about to change
```

**Inside a step, everything runs in parallel**

- **JEV** scores every section of a wave at once, on its own server and pool.
- **Researchers** all start together, up to the LLM slot limit. The shared LLM server's
  concurrency stays where it is (house rule); any extra researchers queue, most promising
  thread first.
- **Inside a researcher**, tool calls (read, search, definition) run in parallel, and every page
  read is cached for everyone.
- **L1 folds** run together.
- **The UI stays alive the whole time**: JEV progress, found sections, researcher notes and
  answer tokens all stream as they happen.

**When is it done?**

```
FIND done       when a wave's lower half has no new hits
                (it goes one wave wider while nothing at all has been found yet)
research done   when the lead check says complete, or ~3 gap rounds, or the time budget runs out
the run ends    after the compiler; anything still running is cancelled
stop button     cancels every task at once
```

**Resources, each with its own limit**

```
LLM (shared server) → existing concurrency, no bumps
JEV                 → its own server and pool
embedder, reranker  → small pools of their own
local Qdrant        → in-process, milliseconds
GROWI page reads    → cached, prefetched as soon as JEV accepts a section
```

**How it's built**: one asyncio event loop per question. Each step is a `gather` over its tasks
(the "all at once, then wait"), each resource has its own semaphore, and one stop signal cancels
every task (the stop button, done, or a time budget).

## What we keep and what we delete (`growi-search/`)

| file                                                                   | fate                                                                                  |
| ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `frontend/`                                                          | keep as is                                                                            |
| `growi_client.py`, `markdown.py`, `models.py`, `page_cache.py` | keep (small, and they work)                                                           |
| `app.py`                                                             | keep the routes, SSE streaming and prefix handling; rewire them to the new researcher |
| `gateway.py`, `config.py`                                          | rewrite small: keep the LLM/embed/rerank clients, drop the dozens of old knobs        |
| `researcher.py`, `prompts.py`                                      | rewrite from scratch (same file names)                                                |
| `mirror.py`, `walker.py`, `cascade.py`                           | delete                                                                                |

The API routes and the SSE event names the frontend already understands stay the same.

## Rules that carry over

- LLM inputs are never truncated; the 16k/32k/64k numbers cap output only.
- No information is lost between researchers, L1 and L2. L2 sees only L1 outputs.
- Each project has its own root. GROWI `/` is only a container.
- Only pages listed in a project's 目次 tree are searched.
- Keywords and summaries come from pre-link text, before the linker adds links.
- No concurrency bumps on the shared LLM server, and thinking is never switched off.

## Build order

1. Builder: extend `chunk_meta` (the new fields plus the 3 fixes), write the 目次 data block
   (with child hashes), and add the shared parser. Republish the 目次 of existing projects.
2. growi-search: the hash-tree sync and the local Qdrant index.
3. The finder, the two dials, the research loop and the compiler, built async from day one
   (the event loop, per-step gather and resource limits come first, then the parts plug in).
4. Rewire `app.py` and delete the old files.
5. Tune the dials on a small question set: `3rd parameter of mpf_mfs_open`,
   `what is file deadlock`, `all functions of moove library`, plus one hunt for a detail that
   could be anywhere.

## Open decisions

Each one shows what the code does today. Any of them can still be changed.

1. Should handwritten GROWI pages (not made by the builder) be searchable too?
   **Built: no.** Only pages listed in a 目次 data block are indexed.
2. Is it OK to re-describe every section once? **Built: yes.** `CHUNK_META_VERSION` is now
   `wiki-chunk-meta-8`, so the next linker run re-describes every section.
3. Folder-level summaries: an LLM roll-up at publish time, or built without an LLM?
   **Built: no LLM.** They reuse the existing `folder_summary` / `document_summary` text.
4. Should the data block be visible to readers or fully hidden? **Built: a collapsed
   `<details>` section titled 「検索用データ」.**
5. Size: with every fact in it, a large document's data block can reach roughly 0.5–1 MB
   (Moove averages ~160 sections per document). If GROWI's editor or API struggles with pages
   that big, the fallback is to move the block to one hidden child page per document, read by
   the same parser.
6. Two handoff items are left out on purpose:
   - `role_judge` is internal to the linker and isn't search data.
   - The `reference_research` change (`graph/wiki/prompts.py:271`) makes pages merge facts from
     other pages' summaries instead of their full text. Undoing it changes the page bodies, not
     the 目次, so it is a separate decision.

## Implementation notes (where the code differs from the plan above)

- **Field names.** Inside the builder the facts and the bridge keep their old names, `claims`
  and `bridge_probe` (the linker's catalog and FTS already use them). The data block
  publishes them as `facts` and `bridge`. The new fields are `kind`, `points` and
  `search_terms`. The linker's own fields (`keywords`, `entities`, `behaviours`) are
  unchanged.
- **Output cap for `chunk_meta`.** `META_MAX_TOKENS` went from 6000 to 16384. It is an
  output cap (thinking included), and the new lists have no count cap.
- **The folder and root blocks carry no kind or terms per child.** They hold the child's
  name, 目次 path, summary, page count and hash. growi-search builds a document's own search
  point from that document's block.
- **Hand edits.** Besides the hash tree there is a **revision sweep**, every
  `WIKI_SEARCH_REVISION_SWEEP_SECONDS` (300 s):
  - it lists each document's pages once
  - it re-indexes any page whose GROWI revision moved

  So a hand edit becomes searchable without a builder run. Its section metadata stays the
  last one the builder wrote.
- **Section alignment.** growi-search rebuilds the sections from the published page with
  `mokuji_data.search_sections()`. That function:
  - drops the publisher's `<!-- chunk -->` markers and the linker footer
  - unwraps links
  - splits on H2 exactly like the builder

  Metadata is matched by ordinal and heading. If they disagree (a hand edit), it falls back
  to heading only.
- **Windows.** A section longer than 3000 characters is split into windows at paragraph
  boundaries. A long table becomes row groups that each repeat the header. Every line is
  kept.
- **Quick exit rule.** All four must hold:
  - A is high
  - JEV says the question is not how/why
  - the direct pages are at most 3
  - either JEV says the question does not want a full list, or every hit is on the direct
    pages

  If a direct page can't be read, or the pages don't fit one LLM input, it researches
  instead.
- **Answer check.** Citations are checked against the pages the run actually saw, then
  rewritten as `page_id : title`. The old extra LLM pass that re-read every cited page was
  dropped for speed.
- **Failed researcher.** Its seed sections go into the reports as they are, so nothing is
  lost.
- **Failed compiler.** If the final call returns nothing, the raw reports are shown.
- **Startup guard.** If an embedder is configured but unreachable, startup fails on
  purpose. Starting BM25-only would wipe the index and rebuild it twice.
- **The frontend's settings screen** still sends its old knobs (depth, net). Only
  `chat_*`, `subagent_concurrency` and `subagent_max_steps` are used now; the rest are
  ignored.

## Handoff for the agent on the work PC

Nothing below has been run. It passes a compile check, ruff (`F`, `E9`) on the changed
files, and an import of every changed module. There were no tests and no live calls.

### What changed

```
graph/linker/wire.py         ChunkMeta + kind, points, search_terms
graph/linker/prompts.py      chunk_meta asks for them, claims/bridge_probe back on,
                             entity kind asked for, CHUNK_META_VERSION → wiki-chunk-meta-8
graph/linker/chunks.py       meta_text(): the whole section (no 12,000-char cut, big tables
                             kept) for chunk_meta; validate_meta keeps the new fields uncapped;
                             META_MAX_TOKENS 16384
graph/common/mokuji_data.py  NEW: render/parse of the data block, block_hash, search_sections
publisher/index.py           visible 目次 shows 情報の種類 + 要点; data_blocks() appends the
                             block (with Merkle child hashes) to every document/folder/root 目次
pyproject.toml               + qdrant-client

growi-search/config.py       rewritten small (new knobs: see growi-search/README.md)
growi-search/gateway.py      chat_model() factory, Embedder (retrieval prefixes), Reranker, build_jev
growi-search/store.py        NEW: local Qdrant (file mode), sparse BM25 tokens, hybrid search,
                             in-memory catalog of sections/pages/definitions
growi-search/sync.py         NEW: root 目次 poll → hash-tree walk → per-page re-index; revision sweep
growi-search/prompts.py      rewritten: JEV A/B + question profile, researcher, lead check,
                             fold, final, standalone follow-up
growi-search/researcher.py   rewritten: finder, dials, rounds, compiler, views (asyncio);
                             one summary log line per question; optional JSON trace per question
                             (WIKI_SEARCH_TRACE_DIR) with every score and decision, for tuning
growi-search/app.py          lifespan starts/stops the new service; /api/ready has "index";
                             logging for this service's own loggers (WIKI_LOG_LEVEL, default INFO)
growi-search/README.md       rewritten for the new service
deleted                      mirror.py, walker.py, cascade.py, eval_run.py, eval_compare.py and
                             their tests (test_cascade*, test_walker*, test_mirror, test_eval,
                             test_researcher, test_gateway)
growi-search/tests/test_app.py   updated for the new ready fields and start/close (not run)

jev/backends/hosted.py       now the vanilla POST /v1/systemone client (was the custom /score one)
jev/backends/llm2jev.py      now the custom batched POST /score client (was the /v1/systemone one);
                             different states now go out in parallel too
jev/config.py, backends/__init__.py
                             aliases systemone/sglang/vllm/jpt → hosted; llm2jev → the custom one;
                             WIKI_JEV_HTTP_CONCURRENCY (old WIKI_JEV_LLM2JEV_CONCURRENCY still read)
jev/test_jev.py              the two backend test classes swapped to match (not run)
```

**JEV backend swap: check this on the work PC.** Any setting that said
`WIKI_JEV_BACKEND=llm2jev` (or `wiki_jev_backend = llm2jev` in a project INI) to reach a
stock llm2jev server must now say `hosted`. The custom batched logic moved to `llm2jev` is
the only batched HTTP code found anywhere: this repo, its git history, the stash,
`~/Downloads/growi-llm-wiki` and the Trash copies. It is the old `hosted` `/score`
contract, with `many_mode: "batched"`. If the custom logic you meant sent several states in
one call (for example a `{"requests": [...]}` body), that code was not found; drop it into
`jev/backends/llm2jev.py`.

### Rollout, in order

1. **Install:** `uv sync` in the repo, or
   `uv pip install qdrant-client` into whatever venv runs growi-search. The work PC runs it
   from another checkout's venv.
2. **Builder: re-describe the sections.** The metadata version changed, so the next full
   linker run re-describes every section with the new fields. That is one LLM call per
   section, the same cost as a first build. Watch the first few
   `work/linker/<run>/meta-*.json` artifacts:
   - `kind`, `points`, `search_terms`, `claims` and `bridge_probe` are filled
   - no `*-error.txt` fallbacks from hitting the 16k cap
3. **Builder: republish the 目次.** Run `main.py index`, or a normal publish sweep. Then open
   one document 目次 in GROWI:
   - the 「検索用データ」 block sits at the bottom
   - its first line is `{"hash":…,"level":"document",…}`
   - the root 目次 has `child` lines with hashes
4. **Config:**
   - Embed and rerank URLs now come from the shared `.env` too. Remove any
     `WIKI_EMBED_BASE_URL=` / `WIKI_RERANK_BASE_URL=` blanks in `growi-search/.env`, unless
     BM25-only is wanted.
   - Set `WIKI_LLM_CONTEXT_TOKENS` to the real model window.
   - Set `WIKI_JEV_ENABLED=1` plus the JEV backend.
5. **Start growi-search** with a single worker. On first start the log shows `indexed …`
   lines, then `index ready: {...}`, and `/api/ready` → `index.ready: true`.

### Tests to write and run (none exist for the new code yet)

The user runs the frontend checks. Write the unit tests with stdlib `unittest` and fakes,
following `tests/test_app.py`.

1. `mokuji_data`:
   - `render` → `parse` gives back the same level, name, records and hash.
   - Changing one record changes the hash.
   - A broken JSON line is skipped and the rest still parses.
   - `search_sections()` on a real published page gives the same ordinals and headings as
     `graph.linker.chunks.split_page()` on its `_planning/pages/*.md` original. Use a page
     that has a chunk marker, a links footer, inline links and a fence containing `## `.
2. `publisher/index.data_blocks`:
   - Changing one section's metadata changes that document's hash, every folder hash above
     it, and the root hash.
   - Sibling documents keep their hashes.
   - A document that is also a folder lists its sub-folders as `child` records.
   - Running twice gives byte-identical output, so an unchanged 目次 is never rewritten.
3. `graph/linker/chunks`:
   - `validate_meta` keeps more than 12 `search_terms` and more than 20 `claims`.
   - `meta_text` keeps a table with more than 40 rows and text past 12,000 characters.
   - `model_text` is unchanged.
4. `store` (Qdrant `:memory:` or a temp dir, with a fake embedder):
   - `tokens()` keeps `mpf_mfs_open` whole and makes CJK bigrams.
   - A second `upsert` of the same specs embeds nothing (returns 0).
   - `delete` removes the key from the catalog.
   - `names_in("mpf_mfs_open の第3引数")` finds the name.
   - The hybrid search returns the section that holds an exact identifier.
5. `sync` (fake GROWI client serving root, folder and document 目次 and pages):
   - The first pass indexes everything.
   - A second pass with an unchanged root makes one GROWI call.
   - Changing one child hash walks only that branch.
   - A moved page revision (revision sweep) re-indexes only that page.
   - A document dropped from the root block has its points deleted.
   - A 目次 whose block can't be parsed keeps its old points.
   - A GROWI error mid-pass deletes nothing.
6. `researcher` (fake JEV engine with `adecide_batch`, fake chat model, and a store filled as
   in 4):
   - High A, low mechanism → exactly one LLM call and no `subagent_start` event.
   - A how/why question never quick-exits.
   - Waves stop when the lower half of a wave has no hits.
   - A tail hit pulls in the neighbours of that page.
   - Hits that share an entity name land in one thread.
   - A lead check that returns a gap starts round 2, seeded with its pages.
   - `max_rounds` is respected.
   - The compiler takes one stage when everything fits, and folds when it doesn't. Test this
     by lowering `WIKI_LLM_CONTEXT_TOKENS`.
   - The stop event raises `AgentStopped` and cancels the researchers.
7. `app`: `tests/test_app.py` as updated, plus one SSE run through a fake researcher.

### End-to-end checks (frontend)

| Question | Expected |
| --- | --- |
| `3rd parameter of mpf_mfs_open` | `route: shallow`, one LLM call, cites the function page, not `001-M-plus申請開始手順` |
| `what is file deadlock` | `route: deep`, a few threads, a lead-check gap, round 2, an answer that combines the facts |
| `all functions of moove library` | many hits, neighbour waves, wide round 1, likely a 2-stage compiler, no items dropped |
| One detail that could be anywhere | the JEV waves go wider until the tail stops hitting |

For each question, write down:
- the `jev_complete` numbers
- the thread count (`subagents_spawned`)
- the rounds
- the total time

Then tune these, in this order:
1. `WIKI_JEV_DIRECT_THRESHOLD` and `WIKI_JEV_HELP_THRESHOLD`
2. `WIKI_JEV_WAVE_SIZE` and `WIKI_JEV_MAX_WAVES`
3. `WIKI_MAX_THREADS`

### Known gaps and risks

- **Data block size** (open decision 5): check that GROWI accepts and renders the largest
  document 目次. If it doesn't, move the block to a hidden child page read by the same
  parser.
- **Local Qdrant** is brute force and warns above 20k points. Moove should be about 25k:
  ~2.2k sections, ~16k facts, plus windows and pages. If search gets slow, switch
  `QdrantClient(path=…)` to a Qdrant server (one line in `store.py`).
- **`est_tokens()`** is a rough estimate with no real tokenizer. It errs high for Japanese.
  If a compiler call is rejected for context length, lower `WIKI_LLM_CONTEXT_TOKENS`.
- **JEV prompts** (A, B, mechanism, list) and all thresholds are untuned.
- **The linker's catalog-row fallback** (`graph/linker/service.py` `_row_meta`) does not
  carry the new fields. It only matters when `_planning/chunks.json` is missing for a
  chunk.
- **A researcher holds one LLM slot for its whole life,** so two big questions at once
  queue behind each other. This is by design (no concurrency bumps).

## Tuning guide: what to change when the researcher gets it wrong

This section is for tuning with tests and human feedback. It gives:
- how to see what a question did
- how to find the step that failed
- symptom tables: what you see → why → what to change
- a feedback loop
- a reference of every knob

Environment knobs are read once at startup, and uvicorn runs without `--reload`, so
**restart growi-search after changing one**. From the frontend's settings screen, only
these can change per question: `chat_base_url`, `chat_model`, `chat_temperature`,
`subagent_concurrency` and `subagent_max_steps`.

Some rules never change while tuning (house rules):
- no raising the shared LLM server's concurrency
- never switching thinking off
- never cutting LLM input
- the 16k/32k/64k numbers cap output only

### 1. See what a question did

Turn these on for every tuning session:

```
WIKI_SEARCH_TRACE_DIR=/somewhere/traces     one JSON file per question (the main tool)
WIKI_USAGE_LOG_PATH=/somewhere/usage.jsonl  one line per question: LLM calls, researchers,
                                            seconds, input/output tokens
WIKI_LOG_LEVEL=INFO                         (default) sync progress, JEV engine stats and
                                            one summary line per question
```

The summary line looks like this:

```
question done in 212.4s: route=research scored=200 hits=37 threads=6 researchers=8 llm_calls=61 stopped=False
```

**What the trace file holds**

| Field | Meaning |
| --- | --- |
| `route` | `quick` (1-call answer), `research`, `conversation` (a follow-up answered from the chat), `not_found`. |
| `standalone` | The follow-up rewritten as a question that stands on its own (only when there was chat context). |
| `seconds` | Seconds since the question arrived when each step ended: `find`, `round 1`, `lead check 1`, `round 2`, …, `end`. |
| `find.candidates` | How many sections the search handed on (facts and whole pages are turned into their sections). |
| `find.pinned` | Sections that **define** a name written in the question (the exact-name channel); they always go first. |
| `find.hot_documents` | Documents that matched as a whole; their pages are used when widening. |
| `find.ranked` | The first 60 sections after reranking: `key`, `page`, `heading`. |
| `find.waves` | Every JEV wave, every section in it, with `a` (answers directly) and `b` (helps answer). |
| `find.mechanism`, `find.list` | JEV on the question itself: is it how/why? does it want a full list? (0–1) |
| `find.hits` | Sections with `a ≥ WIKI_JEV_DIRECT_THRESHOLD` or `b ≥ WIKI_JEV_HELP_THRESHOLD`. |
| `decision` | `direct_pages` (pages with an A hit), `hit_pages`, `quick_exit`, and `quick_fell_back` (quick exit chosen but not possible). |
| `threads` | Section keys given to each round-1 researcher (the WHERE dial). |
| `researchers[]` | Per researcher: `round`, `focus` (the gap it chased), `seeds`, `read` (pages), `tool_calls`, `called_finish`, `findings` (full text), `open_questions`, `cited` — or `error`. |
| `lead_checks[]` | Per lead check: `calls`, `complete`, `gaps` (question, seed pages, search words), `kept`, `unreadable` (replies that were not JSON). |
| `compile` | `reports`, `report_tokens` (estimated), `folds` (one entry per fold level, with the fold `outputs`). |
| `settings` | The knob values in force for this question. |
| `answer` | The final answer `text` and `cited` page ids. |

A section key looks like `s|<GROWI page id>|<section number>|<window number>`. A section
longer than 3000 characters has more than one window.

**Other quick checks**
- `/api/search?q=…` runs the hybrid search alone, with no JEV and no LLM. If the right page
  is not in its first ~20 results, the problem is the index or retrieval, not the researcher.
- `/api/ready` → `index`: `{ready, error, documents, pages, sections}`.
- The chat's activity lines map to the events:

  | Activity line | Event | What it tells you |
  | --- | --- | --- |
  | 「JEV 走査 …」 | `jev_complete` | Its "ページ" count is really sections. |
  | 「索引から …」 | `map` | Documents, pages and hits found. |
  | 「N 人のエクスプローラーを起動」 | round start | Threads in that round. |
  | 「“…” を検索中」 with no explorer name, after round 1 | lead-check gap | A gap being searched. |

### 2. Find the step that failed

Walk this from the top for every bad answer. Stop at the first "no".

```
bad answer
  │
  ├─ does /api/search show the right page for the question's key words?
  │     no → INDEX (§3.1)
  │
  ├─ is the right section in trace find.ranked or in any find.waves entry?
  │     no → RETRIEVAL (§3.2)
  │
  ├─ in find.waves, does it have a high a or b?
  │     no → JEV (§3.3)
  │
  ├─ route = quick?
  │     yes, and the answer is wrong or incomplete    → EXITED TOO EARLY (§3.4)
  │     no, but one page really held the whole answer → DID NOT EXIT EARLY ENOUGH (§3.5)
  │
  ├─ route = research: does some researcher's findings text hold the fact?
  │     no → RESEARCHER (§3.6)
  │
  ├─ did the answer need a link between facts that nobody made? (lead_checks)
  │     yes → LEAD CHECK AND ROUNDS (§3.7)
  │
  └─ the fact is in the findings but missing or wrong in the answer → COMPILER (§3.8)

and for any question: TOO SLOW (§3.9), TOO SHALLOW (§3.10), WRONG INFORMATION (§3.11),
FOLLOW-UPS (§3.12), ERRORS (§3.13)
```

### 3. Symptoms and fixes

Change **one** thing at a time. Re-ask the same questions and compare the new trace with
the old one.

#### 3.1 Index

| You see | Why | Change |
| --- | --- | --- |
| `/api/ready` `index.sections` is 0 or small; log says "has no search data block" | The 目次 pages were published by the old builder | Run `main.py index` (or a publish sweep) with the new builder |
| A page is in GROWI but never in `/api/search` | It is not listed in any 目次 (a handwritten page), or its 目次 row has no page id (published without the ledger) | Publish it through the builder. If handwritten pages must be searchable, see open decision 1 |
| A hand edit in GROWI is not found yet | The revision sweep runs every 300 s; or the listing failed (log: "revision sweep skipped") | Wait, or lower `WIKI_SEARCH_REVISION_SWEEP_SECONDS` (e.g. 120). An edited section's kind/points/terms stay old until the builder rebuilds |
| Headings in the trace don't match the page | A hand edit moved sections, so metadata was matched by heading instead | Let the builder pull and rebuild; nothing to tune |
| `kind` / `points` / `search_terms` are empty in the data block | `chunk_meta` hit its output cap or failed (`meta-*-error.txt` in the linker run folder); the lead paragraph above the first section is summary-only by design | Raise `META_MAX_TOKENS` (`graph/linker/chunks.py`), check the model. After any change to the chunk_meta prompt, bump `CHUNK_META_VERSION` |
| Log repeats "index sync pass failed" | One GROWI call keeps failing | Fix that page or GROWI; a failed pass deletes nothing |
| After changing the embed model or its prefixes, search is empty for a while | The stored vectors no longer match, so the index rebuilds (by design) | Wait for "index ready" in the log |
| You changed `WINDOW_CHARS`, `tokens()` or `BM25_K1` and nothing changed | Unchanged pages are never re-indexed, and these are not part of the change check | Stop growi-search, delete `WIKI_SEARCH_STORE_DIR`, start it again (full rebuild) |

#### 3.2 Retrieval: the right section never reaches JEV

| You see | Why | Change |
| --- | --- | --- |
| The question names a function, but its defining section is not in `find.pinned` | The name is shorter than 3 characters; or the builder did not record it as `role: defines`; or it is spelled differently | Check the section's `entities` in the data block. The minimum length is in `Store.names_in` (`store.py`) |
| The right section is in `/api/search` but not in `find.ranked` or the waves | Too few candidates, or the waves stopped before reaching it | Raise `WIKI_SEARCH_POOL` (300 → 600), `WIKI_JEV_WAVE_SIZE` (100 → 150) or `WIKI_JEV_MAX_WAVES` (4 → 6) |
| High in `/api/search` but low in `find.ranked` | The reranker pushes it down | Compare with the reranker off (unset `WIKI_RERANK_BASE_URL`, restart). If it is better off, lower `WIKI_RERANK_POOL` (only the top N get reordered) or use another rerank model |
| The question uses other words than the page (synonyms, English vs Japanese, abbreviations) | BM25 can't match them and the meaning vectors are weak on them | Improve the `search_terms` instruction in `chunk_meta_prompt` (`graph/linker/prompts.py`: more synonyms, both languages), bump `CHUNK_META_VERSION`, rebuild. Check the embed prefixes match the model |
| Identifiers with unusual characters (`A-B`, `x.y`) don't match exactly | The sparse tokenizer splits them | `_IDENT_RE` in `store.py`, then delete the store folder |
| The needed part of a long section never shows up | It sits in a later window that ranks low | Usually fine; if common, a larger `WINDOW_CHARS` in `sync.py` (then delete the store folder) |
| Exact names should matter more than meaning, or the other way round | All five channels (3 dense, 2 sparse) fuse with equal weight (RRF) | Code: give that channel a larger prefetch `limit` in `Store.search`, or search twice and merge |

#### 3.3 JEV: scored, but judged wrongly

**Calibrate the two thresholds first.** Pick 10–20 questions whose right sections you
know. From their traces (`find.waves`), write down `a` and `b` for the right sections and for
some clearly wrong ones. Then choose:
- `WIKI_JEV_HELP_THRESHOLD` (B): just below the `b` of most right sections. Lower means more
  recall but more noise, so more and bigger threads.
- `WIKI_JEV_DIRECT_THRESHOLD` (A): above the `a` of nearly all wrong sections. Higher means
  fewer wrong quick exits, but more research.

Keep direct ≥ help; startup refuses otherwise.

| You see | Why | Change |
| --- | --- | --- |
| The right section has `b` below the help threshold | JEV says no to a useful section | Lower `WIKI_JEV_HELP_THRESHOLD` (0.5 → 0.35). If `b` is near 0, reword `jev_helps_question` in `growi-search/prompts.py` |
| Many hits are unrelated; many threads; researchers read off-topic pages | B is too loose | Raise `WIKI_JEV_HELP_THRESHOLD` (0.5 → 0.6 or 0.7), or reword B to require the question's own subject |
| High `a` on a section about a neighbouring function or product | A is too loose | Raise `WIKI_JEV_DIRECT_THRESHOLD` (0.8 → 0.9); tighten `jev_direct_question` |
| Low `a` on the section that literally holds the answer | A is too strict | Lower `WIKI_JEV_DIRECT_THRESHOLD` (0.8 → 0.7) and watch §3.4 |
| `find.mechanism` is high for plain lookups, or low for how/why questions | The wording of `JEV_MECHANISM_QUESTION` | Reword it (`growi-search/prompts.py`), or move the 0.5 cut in `Researcher._answer` |
| `find.list` is wrong | The wording of `JEV_LIST_QUESTION` | Same as above |
| Every `a` and `b` is 0.0 | JEV requests are failing (log: "jev request failed") | Check the JEV server or model; the "Jev stats" log line shows engine errors |
| The trace's `jev_complete` mean looks low even though hits are fine | It is the mean `b` over **all** scored sections, and most are unrelated | Nothing; judge by hits and by the right sections' scores |

#### 3.4 Exited too early (`route = quick`, answer wrong or incomplete)

| You see | Why | Change |
| --- | --- | --- |
| The answer is about a neighbouring function | A is too loose | Raise `WIKI_JEV_DIRECT_THRESHOLD`; tighten `jev_direct_question` |
| Only part of a list is answered | `find.list` was under 0.5, or the rest of the list scored `b` below the help threshold (so it didn't count as a hit outside the direct pages) | Lower `WIKI_JEV_HELP_THRESHOLD`; move the list cut in `_answer` (0.5 → 0.3); lower `WIKI_QUICK_MAX_PAGES` |
| A how/why question answered shallowly | `find.mechanism` was under 0.5 | Move the mechanism cut in `_answer` (0.5 → 0.3); reword `JEV_MECHANISM_QUESTION` |
| Correct, but misses context from linked pages | The quick exit reads only the direct pages | Make the quick exit rarer (higher A threshold) or accept it |

#### 3.5 Did not exit early enough (researched a simple question)

Read `decision` in the trace:

| `decision` shows | Why | Change |
| --- | --- | --- |
| `direct_pages` is empty | No section reached the A threshold | Calibrate (§3.3), lower `WIKI_JEV_DIRECT_THRESHOLD`; first check the answering section was scored at all (§3.2) |
| More `direct_pages` than `WIKI_QUICK_MAX_PAGES` | The answer is written in several places (e.g. version copies) | Raise `WIKI_QUICK_MAX_PAGES` (3 → 5); the cost is one bigger call |
| `find.mechanism` ≥ 0.5 | JEV thinks it is a how/why question | Reword the question text, or move the cut |
| `find.list` ≥ 0.5, and `hit_pages` has pages outside `direct_pages` | Other pages scored B hits | Raise `WIKI_JEV_HELP_THRESHOLD`, or code: in `_answer`, allow the quick exit when the extra pages have only B hits |
| `quick_fell_back: true` | A direct page could not be read, or the pages don't fit one call | Check GROWI. If the pages are just big, research is the right path |

#### 3.6 Researchers (trace `researchers[]`)

| You see | Why | Change |
| --- | --- | --- |
| `called_finish: false` and `tool_calls` near twice `WIKI_SUBAGENT_MAX_STEPS` | It ran out of steps; a fallback call wrote its report from what it read | Raise `WIKI_SUBAGENT_MAX_STEPS` (20 → 30), or give it less per thread: lower `SEED_SHARE` (`researcher.py`), raise `WIKI_MAX_THREADS` |
| `called_finish: false` with few tool calls | It answered in plain text (kept as its findings) | Fine. If the findings are thin, tighten `RESEARCHER_PROMPT` |
| `read` is empty and the findings only repeat the seeds | It didn't open the pages | `RESEARCHER_PROMPT`: require reading whole pages for list and how/why questions |
| `read` lists many unrelated pages | It follows links too eagerly | `RESEARCHER_PROMPT`; lower `WIKI_SUBAGENT_MAX_STEPS` |
| `error` mentions context length, or the LLM server returns 400 | Seeds plus pages read overflowed the model window | Lower `SEED_SHARE` (0.25 → 0.15) and `READ_CHARS` (20000 → 12000) in `researcher.py`; make `WIKI_LLM_CONTEXT_TOKENS` the real window |
| `error` is a timeout | The LLM server is slow or busy | `WIKI_REQUEST_TIMEOUT`; never more concurrency |
| Two researchers cover the same pages | A big thread was split by the seed budget, or small threads were merged | Raise `SEED_SHARE` a little, or lower `WIKI_MAX_THREADS` |
| One huge thread mixes unrelated topics | A shared name glued them together | In `_threads`, names found on more than `max(3, 40% of hit pages)` pages are ignored; lower the 0.4 |
| Many tiny threads for one topic | Their names don't overlap (different spellings), so every page is its own thread | Lower `WIKI_MAX_THREADS` (the smallest threads merge, same document first); improve entity names in the builder |
| `cited` is empty | The findings name no page ids, or ids the run never saw (those are dropped on purpose) | `RESEARCHER_PROMPT`: every fact must carry its `page_id` |
| `open_questions` is empty on a how/why question that isn't explained | The researcher is overconfident | `RESEARCHER_PROMPT`: list the missing link explicitly |

#### 3.7 Lead check and rounds (trace `lead_checks[]`)

| You see | Why | Change |
| --- | --- | --- |
| `complete: true` in round 1, but the answer lacks the link between facts | The lead check is too lenient | Make `LEAD_CHECK_PROMPT` stricter, e.g. "for how/why, every step of the chain needs a source" |
| `unreadable` > 0 | The reply was not JSON, which counts as complete, so research stops | Tighten the prompt; lower `WIKI_CHAT_TEMPERATURE` (0.2 → 0) |
| The same gap comes back every round | It can't be answered from the wiki, or its seeds are wrong | Lower `WIKI_MAX_ROUNDS`; or code: in `_gap_tasks`, skip a gap whose question matches an earlier `focus` |
| Gaps quietly vanish | Their `seed_node_ids` were unknown and the search found nothing | `LEAD_CHECK_PROMPT`: seed ids must come from the reports' 根拠ページ |
| Round 2 has too many researchers and is slow | Too many gaps | Lower `WIKI_MAX_GAPS` (6 → 3) |
| Rounds stop while gaps remain | `WIKI_RUN_SECONDS` passed (checked before every lead check), or `WIKI_MAX_ROUNDS` was reached | Raise either |

#### 3.8 Compiler (trace `compile`)

| You see | Why | Change |
| --- | --- | --- |
| `folds` is not empty for a modest question | The reports are over the final budget (context − `WIKI_FINAL_COMPILER_TOKENS` − prompt) | Set `WIKI_LLM_CONTEXT_TOKENS` to the true window; or lower `WIKI_FINAL_COMPILER_TOKENS` (65536 → 32768) to leave more room for input |
| Items in the findings are missing from the answer | A fold or the final call dropped them | Compare `folds[].outputs` with the findings to see which stage; tighten `FOLD_PROMPT` / `FINAL_PROMPT`; better, avoid folding (row above) |
| The answer stops mid-sentence | The final output cap was hit | Raise `WIKI_FINAL_COMPILER_TOKENS` (at most half the context; startup checks) |
| The answer starts 「回答の生成に失敗…」 followed by raw reports | The final call returned nothing | Check the LLM server and the output cap |
| The server rejects a compiler call as too long | `est_tokens()` underestimated (ASCII-heavy code text) | Lower `WIKI_LLM_CONTEXT_TOKENS` by 10–20% |
| The same fact shows twice with different values | Folds keep both sides of a conflict on purpose | Fine (a real conflict in the wiki); or have `FINAL_PROMPT` flag it as a conflict |

#### 3.9 Too slow

Read `seconds` in the trace. It shows which step took the time.

| Slow step | Change |
| --- | --- |
| `find` (more than ~30 s) | JEV throughput: lower `WIKI_JEV_WAVE_SIZE` or `WIKI_JEV_MAX_WAVES`; JEV engine batching (`WIKI_JEV_MAX_BATCH_REQUESTS`, `WIKI_JEV_MAX_BATCH_TOKENS`); for the HTTP backends, `WIKI_JEV_HTTP_CONCURRENCY` (states in flight, default 20; each section is its own state). Reranker: lower `WIKI_RERANK_POOL`. Also check the embed server |
| `round 1` | If threads > `WIKI_SEARCH_LLM_MAX_CONCURRENCY`, researchers queue in batches. Set `WIKI_MAX_THREADS` equal to the LLM slots (or a small multiple). Lower `WIKI_SUBAGENT_MAX_STEPS`. Raise `WIKI_JEV_HELP_THRESHOLD` (fewer hits, so fewer threads) |
| Many rounds | Lower `WIKI_MAX_ROUNDS`, `WIKI_MAX_GAPS` or `WIKI_RUN_SECONDS` |
| Compile folds | §3.8, first row |
| Simple questions go to research | §3.5 |
| `queued_for_agent` shows | More questions at once than `WIKI_SERVICE_MAX_AGENTS`, or they wait for LLM slots (by design) |

#### 3.10 Too fast or too shallow

| You see | Change |
| --- | --- |
| A list is incomplete and research ended after one round | Lower `WIKI_JEV_HELP_THRESHOLD`; raise `WIKI_JEV_MAX_WAVES` / `WIKI_JEV_WAVE_SIZE`; make `LEAD_CHECK_PROMPT` check completeness; raise `WIKI_MAX_ROUNDS` |
| A how/why answer lists facts but never explains how they combine | The lead check should have raised a gap: see §3.7 row 1. Also `FINAL_PROMPT` |
| Few researchers on a broad question | Lower `WIKI_JEV_HELP_THRESHOLD`; raise `WIKI_MAX_THREADS` |
| The waves stop while hits are still coming | The tail rule: FIND stops when the **lower half** of a wave has no hit. Code in `_find`: widen when a hit is in the last two thirds, or always run one extra wave for list questions |

#### 3.11 Wrong information

First find which step first said the wrong thing: the `researchers[].findings`, then
`compile.folds[].outputs`, then `answer.text`.

| Kind | Where it starts | Change |
| --- | --- | --- |
| A fact no page says (invented) | Findings: the researcher. Only in folds or the answer: the compiler | Lower `WIKI_CHAT_TEMPERATURE`; tighten the `_ACCURACY` text in `prompts.py`. If it keeps happening, add back a check pass that re-reads the cited pages before `_finish` (like the old `ANSWER_VERIFY_PROMPT`). It costs one more call |
| A true fact about the wrong subject (neighbouring function or version) | Wrong seeds (§3.3) or a mixed thread (§3.6) | Raise the thresholds; lower the 0.4 common-name share; the exact-name channel pins the defining section |
| An outdated fact | A GROWI edit not re-indexed yet, or old builder metadata | §3.1 |
| A how/why explanation that goes further than the sources | The researcher combined facts without a source for each step | `RESEARCHER_PROMPT` and `FINAL_PROMPT`: mark reasoning as reasoning and cite both sources |
| A cited page the answer did not use | When the answer has no 引用 section, `_finish` cites every page the reports cited, then every hit page | `FINAL_PROMPT`; or code: make the fallback use only report-cited pages |
| A missing citation | Only pages the run actually saw can be cited, and an id the model mistyped is dropped | `FINAL_PROMPT`: copy the page ids exactly |

#### 3.12 Follow-up questions

| You see | Change |
| --- | --- |
| `route = conversation`, but the follow-up needed new wiki facts | Make `STANDALONE_PROMPT` stricter about when ANSWER is allowed |
| `standalone` changed the subject | `STANDALONE_PROMPT`: keep the user's subject and add only what the chat context supplies |

#### 3.13 Errors in the log or the chat

| Message | Meaning |
| --- | --- |
| "the embedder is configured but unreachable" (startup) | The embed server is down: fix it, or unset `WIKI_EMBED_BASE_URL` to run BM25-only (the index then rebuilds) |
| "Jev is enabled but … unavailable" (startup) | The JEV model or server is missing |
| "rerank failed, keeping fusion order" | Reranker down for this question; it goes on without it |
| "query embedding failed" | Keywords-only search for this question |
| "researcher N failed" | Its seed sections were passed on as its report; see its `error` in the trace |
| chat error `growi_unavailable` / `llm_unavailable` | GROWI or the LLM is not reachable |

### 4. Human feedback loop

1. **Collect.** For each answer a person judges, keep:
   - the question
   - one label (list below)
   - the page(s) that hold the right answer, and for lists the missing items
   - the trace file name
2. **Label → where to look:**

   | Label | Section |
   | --- | --- |
   | correct | nothing |
   | not found but it exists | §2, top down |
   | missing items | §3.10, §3.2, §3.8 |
   | wrong fact | §3.11 |
   | wrong subject | §3.3, §3.6 |
   | shallow explanation | §3.7, §3.10 |
   | answered too quickly (quick exit was wrong) | §3.4 |
   | researched a simple question | §3.5 |
   | too slow | §3.9 |
   | bad citation | §3.11 |
   | follow-up misunderstood | §3.12 |

3. **Keep a regression set.** Put a small JSON Lines file next to this doc, one question
   per line: `{question, expect_pages, expect_items, kind}`, where kind is `detail`, `list`,
   `mechanism` or `needle`. Start with the four questions from the end-to-end checks and add
   every question a person flagged.
4. **After every change**, ask the whole set again and compare with the traces from before:
   - `route`
   - whether the expected pages are among the hit pages
   - whether the answer cites the expected pages
   - whether the expected items appear in `answer.text`
   - `seconds.end` and `llm_calls`

   Keep the change only if nothing got worse.
5. **Log every change** at the end of this doc: date, knob, old → new, why, and the
   before/after on the regression set.

**Tune in this order** (later steps depend on earlier ones):
1. the index is complete
2. retrieval (`/api/search`)
3. JEV thresholds (calibration)
4. the quick-exit rule
5. threads and researcher steps
6. lead check and rounds
7. compiler budgets
8. speed

### 5. Knob reference

**Environment knobs** (restart after a change)

| Knob | Default | Raise it to … | Lower it to … |
| --- | --- | --- | --- |
| `WIKI_JEV_HELP_THRESHOLD` | 0.5 | cut noise, fewer threads, faster | find more (lists, needles), more research |
| `WIKI_JEV_DIRECT_THRESHOLD` | 0.8 | fewer wrong quick exits | more quick exits |
| `WIKI_SEARCH_POOL` | 300 | reach sections that rank low | save search time |
| `WIKI_RERANK_POOL` | 200 | rerank more | fewer rerank calls / trust fusion order |
| `WIKI_JEV_WAVE_SIZE` | 100 | judge more per wave (lists) | faster FIND |
| `WIKI_JEV_MAX_WAVES` | 4 | keep widening longer (lists, needles) | faster FIND |
| `WIKI_QUICK_MAX_PAGES` | 3 | quick-exit when the answer spans more pages | stricter quick exits |
| `WIKI_MAX_THREADS` | 12 | more, smaller researchers | fewer queue batches (match the LLM slots) |
| `WIKI_MAX_ROUNDS` | 3 | chase more gaps | faster |
| `WIKI_MAX_GAPS` | 6 | chase more gaps per round | faster rounds |
| `WIKI_RUN_SECONDS` | 1200 | allow more rounds | stop starting rounds sooner (soft: running researchers finish) |
| `WIKI_SUBAGENT_MAX_STEPS` | 20 | deeper reading per researcher | faster researchers |
| `WIKI_SUBAGENT_CONCURRENCY` | = LLM slots | — (never above the slots) | leave slots for other users |
| `WIKI_SUBAGENT_REPORT_TOKENS` | 16384 | longer reports | — |
| `WIKI_LEAD_CHECK_TOKENS` | 16384 | longer gap lists | — |
| `WIKI_REPORT_FOLD_TOKENS` | 32768 | longer L1 folds | more input room per fold |
| `WIKI_FINAL_COMPILER_TOKENS` | 65536 | longer answers | more input room: fewer folds |
| `WIKI_LLM_CONTEXT_TOKENS` | 131072 | must equal the real window | lower it if calls are rejected as too long |
| `WIKI_CHAT_TEMPERATURE` | 0.2 | — | steadier JSON and fewer inventions |
| `WIKI_SEARCH_SYNC_SECONDS` | 30 | less GROWI polling | builder changes show sooner |
| `WIKI_SEARCH_REVISION_SWEEP_SECONDS` | 300 | less GROWI listing | hand edits show sooner |
| `WIKI_SERVICE_MAX_AGENTS` | 4 | more questions at once | — |

**Constants in the code** (edit the file, restart)

| Where | Name / rule | Default | What it does | Note |
| --- | --- | --- | --- | --- |
| `researcher.py` | `SEED_SHARE` | 0.25 | Share of a researcher's context its seed sections may fill; bigger threads split | Lower it if researchers overflow the context |
| `researcher.py` | `READ_CHARS` | 20000 | A longer page is shown section by section (never cut inside a section) | Lower it if researchers overflow the context |
| `researcher.py` `_answer` | mechanism and list cuts | 0.5 / 0.5 | Quick exit only when both are below the cut | See §3.4 / §3.5 |
| `researcher.py` `_answer` | no-hit fallback | best 5 sections | One researcher when JEV found nothing | |
| `researcher.py` `_find` | tail rule | lower half | Widen when a hit lands in the lower half of the wave | See §3.10 |
| `researcher.py` `_find` | no-JEV fallback | top 20 | Sections sent to research when JEV is off | |
| `researcher.py` `_threads` | common-name rule | `max(3, 0.4 × hit pages)` | A name found on more pages than this connects nothing | See §3.6 |
| `researcher.py` tools | search / definition sizes | 8 of 24 / 5 (2 with full text) | Researcher tool results | |
| `researcher.py` `_gap_tasks` | gap seeds | 2 windows per named page + 5 from search | Seeds for a gap researcher | |
| `researcher.py` | `RERANK_BATCH`, `JEV_BATCH` | 32 / 10 | Request sizes and the progress tick | |
| `store.py` | `names_in` | length ≥ 3, max 20 | The exact-name channel | |
| `store.py` | `BM25_K1`, `tokens()` | 1.2 / words + identifiers + CJK bigrams | Sparse scoring | Delete the store folder after changing |
| `store.py` `search` | channels | 3 dense + 2 sparse, equal RRF | Fusion | See §3.2 |
| `sync.py` | `WINDOW_CHARS` | 3000 | Window size for long sections | Delete the store folder after changing |
| `prompts.py` | JEV questions, `RESEARCHER_PROMPT`, `LEAD_CHECK_PROMPT`, `FOLD_PROMPT`, `FINAL_PROMPT`, `QUICK_ANSWER_PROMPT`, `STANDALONE_PROMPT` | — | All model behaviour | No rebuild needed |
| `graph/linker/prompts.py` | `chunk_meta_prompt` (kind, points, search_terms, claims, bridge) | — | The metadata search relies on | Bump `CHUNK_META_VERSION`, then re-run the linker and `main.py index` |
| `graph/linker/chunks.py` | `META_MAX_TOKENS`, `META_TEMPERATURE` | 16384 / 0.7 | chunk_meta output cap and temperature | |

### 6. Tuning log

(Add one line per change: date · knob · old → new · why · regression-set before/after.)
