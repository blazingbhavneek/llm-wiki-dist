# Refactor + Team Knowledge-Sync Plan

Two tracks, deliberately written together because they converge:

- **Track A — Refactor.** Shrink the codebase, kill dead code, flatten nested helpers, make every module readable top-to-bottom.
- **Track B — Team knowledge sync.** Local per-developer code graph + this repo as the shared team wiki + a bridge so one agent's discovery becomes every agent's instant answer.

Track B is *why* Track A matters: the sync layer needs a clean, importable service API (not a 3,532-line god file) to expose over MCP. Every Track A decision below is checked against "does this make Track B easier?"

---

## Current inventory

| File | Lines | Verdict |
|---|---|---|
| `graph/graph.py` | 3,532 | God file. 6 classes + bootstrap + prompt-adjacent helpers. **Split.** |
| `graph/langgraph_agent.py` | 1,194 | Two ~500-line functions with nested tool closures. **Flatten.** |
| `db/raw_sqlite.py` | 1,153 | Cohesive SQL layer. Keep. |
| `graph/utils.py` | 587 | ~250 lines are **Japanese prompt constants**, not utils. Split. |
| `graph/models.py` | 534 | Fine. Minor cleanup. |
| `app.py` | 519 | Fine shape. Minor cleanup. |
| `llm/llm.py` | 356 | Keep. |
| `graph/cli.py` | 174 | No importers; dev tool. Keep, verify it still runs. |
| `db/base.py` | 141 | ABC with exactly one implementation. **Delete.** |
| `graph/enrich.py` | 120 | Clean. Keep as-is. |
| `llm/agent.py` | 93 | **Dead. Zero importers. Delete.** |
| `llm/base.py` | 43 | Base class with one subclass. **Merge into `llm/llm.py`.** |
| `llm/utils.py` | 36 | Duplicated in `langgraph_agent.py`. **Consolidate.** |
| `main.py` | 6 | "Hello from llm-wiki-dist!" stub. **Delete.** |
| `frontend/src/App.jsx` | 2,804 | God component: ~370-line `STR` i18n object + ~1,500-line `App()`. **Split.** |
| `frontend/src/components/MarkdownView.jsx` | 1,278 | Oversized. Split. |
| `frontend/frontend/` | — | Stray accidental nested dir (one duplicate `UploadView.jsx`). **Delete.** |

Noise metrics (backend):

- `debug_print(...)` calls: **39** (18 in graph.py, 21 in langgraph_agent.py), each dumping multi-line dict payloads inline — the single biggest readability killer in the ask/early-exit path.
- Raw `print(...)` calls: **75** in graph.py + langgraph_agent.py, including 11 `[GRAPH_AGENT_DEBUG]` prints.
- Nested `def` statements: **143** in graph.py, **12** in langgraph_agent.py.
- `format_lead_candidate` defined **3×** (graph.py:193, langgraph_agent.py:409, utils.py:566).
- `debug_print` defined **2×** (graph.py:168, langgraph_agent.py:77).
- `strip_image_media` defined **2×** (llm/utils.py, langgraph_agent.py:54).
- Star imports in graph.py: `from .enrich import *`, `from .models import *`, `from .utils import *` — hides where every symbol comes from.
- Doubled import blocks in graph.py (`json`, `datetime`, `Callable` imported twice); `app.py` imports `BaseModel` twice (lines 26, 30).

---

# Track A — Refactor

## Phase 0 — Delete dead weight (no behavior change, ~350 lines gone)

1. Delete `llm/agent.py` (93 lines, zero importers).
2. Delete `main.py` (6-line stub); `pyproject.toml` has no entry point for it.
3. Delete `frontend/frontend/` stray directory.
4. Delete `db/base.py` (141 lines). `BaseDatabase` is an ABC with exactly one subclass (`RawSqliteDatabase`), and half its "abstract" methods already have soft `raise NotImplementedError` bodies — ceremony, not abstraction. `db/__init__.py` already exposes `Database = RawSqliteDatabase`; keep that alias as the single seam. If a second backend ever appears, extract a `Protocol` then.
5. Merge `llm/base.py` (43 lines) into `llm/llm.py` — one subclass, same reasoning. Keep the `LlmClient` Protocol in `graph/models.py` as the real boundary.
6. Fix trivia while touching these files: duplicate `BaseModel` import in app.py, doubled import block in graph.py (lines 30–37), `def  query` double-space typo (graph.py:393).

**Verify:** `uvicorn app:app` boots; `/api/health`, `/api/graph`, `/api/search?q=x` respond.

## Phase 1 — One home per helper (~200 lines gone, big readability win)

1. `format_lead_candidate`: keep the `graph/utils.py` copy, delete the other two, import it.
2. `debug_print`: delete both definitions. Replace all 39 call sites with one-line `log.debug("early_exit.router decision=%s", ...)` calls (policy in Phase 3). The current pattern — 10-line dict literals passed to a homegrown printer — doubles the visual length of `_try_early_exit`.
3. `strip_image_media`: keep the `llm/utils.py` version, delete the langgraph copy plus its private regex constants.
4. Kill star imports in `graph/graph.py`: explicit `from .models import Node, Edge, Settings, ...`. Mechanical, and immediately reveals any other unused symbols.

**Verify:** app boots; `/api/ask` returns an answer on the test DB (`WIKI_DB=.wiki3/test.sqlite`).

## Phase 2 — Split `graph/graph.py` (3,532 → ~5 files)

Cut along class boundaries that already exist:

```
graph/
  runtime.py       SharedGraphRuntime, GraphDbSession                    (~200)
  read_session.py  GraphReadSession (get/search/ask/early-exit/subagent) (~900)
  write_session.py GraphWriteSession (ingest/revision/cluster/enrich)    (~1500)
  services.py      ReadGraphService, WriteGraphService, WriteJob,
                   job_to_dict, bootstrap_database                       (~600)
  prompts.py       ALL Japanese prompt constants (from utils.py AND any
                   inline prompt text in langgraph_agent.py)             (~300)
  graph.py         temporary re-export shim so app.py / cli.py imports
                   keep working; delete at the end of the series
```

Rules for the split:

- **Move, don't rewrite.** Each commit moves one class verbatim, fixes imports, boots the app. No logic edits inside move commits — keeps diffs reviewable.
- If `write_session.py` needs a second cut, extract the markdown-ingest family (`_load_md_output`, `_load_old_manifest_output`, `_load_new_planning_docs_output`, `_split_frontmatter`, `_parse_ranges`, `_title_from_markdown`, `_humanize`, `_canonical_doc_name`, `_doc_sort_key`, `_chain_edges`) into `graph/ingest.py` — those ~15 methods use `self` only for settings; they're really a parser module.
- Delete the ~30 one-line `_db_get_node`/`_db_upsert_node`/… delegation wrappers on both sessions (~120 lines). They exist so call sites read `self._db_get_node(x)` instead of `self.db.database.get_node(x)`. Replace with a `self.store` property returning `self.db.database`, call `self.store.get_node(x)` directly — same brevity, zero wrapper boilerplate, and grepping a DB method finds real call sites.

**Verify after each move commit:** boot + `/api/ask` + one write round-trip (`POST /api/document`).

## Phase 3 — Flatten nested helpers + logging policy

**Policy (add to CLAUDE.md so it sticks):**
- A nested `def` is allowed only when it genuinely closes over local mutable state (the `work()` pattern for `asyncio.to_thread`, `emit` callbacks). Everything else becomes a module-level function or method.
- No `print()` in library code. One `log = logging.getLogger(...)` per module; `log.debug` traces, `log.info` state changes, `log.warning` swallowed errors. `print` survives only in `cli.py`.
- Debug payloads: one line, lazy `%s` formatting, no dict literals spanning >3 lines.

**Targets, in order of pain:**

1. **`GraphReadSession._try_early_exit` (graph.py:753–1036, ~280 lines).** Strip the 15 `debug_print` blocks (~120 lines alone), then split the three router outcomes into `_answer_by_reuse(decision, results)` and `_answer_by_shallow(question, results)`, keeping the router call in the parent. Nested `describe()` → module-level `_describe_candidate(result)`.
2. **`_run_lead` (graph.py:1037).** Delete inline `import copy/threading/traceback`, delete the 5 `[GRAPH_AGENT_DEBUG]` prints, replace the `copy.copy(self)` shallow-clone hack with an explicit constructor path: `GraphReadSession(self.runtime, langgraph_db, overrides_from=self)` — a session knows how to make a sibling on a new DB handle. 60 lines → ~20.
3. **`run_lead_agent` (langgraph_agent.py:444–940, ~500 lines).** Nested `search_tool`/`explore_tool`/`finish_tool` closures each carry 50+ lines of prompt formatting. Extract a `LeadContext` dataclass (session, run state, emit, stop_event, budgets); make tools module-level functions taking `ctx`. `run_lead_agent` becomes: build ctx → build tools → compile graph → drive loop (~120 lines). Same treatment for `run_subagent` (942–1194).
4. **Sanitizer family (langgraph_agent.py:124–286, ~160 lines, 6 functions).** Defensive handling of every possible LangChain content shape. Collapse to two functions (`sanitize_messages`, `sanitize_text`), move to `llm/utils.py` next to `strip_image_media` where they belong conceptually.
5. **`bootstrap_database` (graph.py:3358–3532, ~175 lines).** Split into `_ensure_vectors(ws)`, `_ensure_search_items(ws)`, `_ensure_clusters(ws)` — the three gates are already separated by print banners; make banners the function names, 20 prints → 6 `log.info` lines.
6. **`ask_stream` in app.py.** Nested `run_agent`/`stream` genuinely close over queue/task state — acceptable. But move run-registry bookkeeping (`agent_runs`, `agent_stop_events` module globals + cleanup) into a small `AgentRunRegistry` class so the endpoint reads linearly.

**Verify:** manual pass — ask via early-exit reuse path, shallow path, deep path; stop mid-run; document upload; recluster.

## Phase 4 — Frontend

1. Delete `frontend/frontend/`.
2. `App.jsx` (2,804): extract `STR` (~370 lines) into the existing `i18n.jsx`. Extract each layout component already defined in the file (`LeftSidebar`, `TopBar`, `RightDocumentRail`, `AnswerSourcesSidebar`, `SearchResultsCenter`, `SearchResultCard`, `AppFooter`, …) into `src/components/layout/`. Target: `App.jsx` ≤ 500 lines of routing/state.
3. `App()` itself (~1,500 lines): extract state clusters into hooks — `useGraphData`, `useAskStream` (SSE lifecycle), `useWriteJobs` (polling), `useSettings`. Each hook becomes testable and the JSX tree stops scrolling for 20 screens.
4. `MarkdownView.jsx` (1,278): split renderer config / node-link handling / mermaid embedding into 3 files.
5. Confirm `frontend/dist` and `node_modules` are gitignored.

**Verify:** `npm run build` clean; manual click-through: graph view, chat ask with streaming, doc sidebar, settings save.

## Phase 5 — Consistency sweep (own commit)

- `WriteJob.user_id` plumbed but never set — Track B needs it (notes should record which teammate's agent wrote them). **Keep and wire in Track B Gap 2.**
- `ReadGraphService` semaphores: app.py passes `max_reads=16, max_agents=16` vs docstring defaults 8/2 — move real values into `Settings`, drop constructor magic numbers.
- `graph/cli.py`: run each subcommand once; fix or delete broken ones; update imports to survive the Phase 2 split.
- Drop `# region` markers — they imply structure the file no longer needs once split.

## Expected outcome

- **~1,200–1,500 backend lines deleted** (dead files ~350, debug noise ~400, delegation wrappers ~120, duplicate helpers ~150, collapsed sanitizers/bootstrap ~200+).
- No file over ~1,000 lines except `db/raw_sqlite.py` (cohesive) and `write_session.py`.
- Zero nested defs that don't close over local state; zero `print` outside CLI; one definition per helper.
- Import layering becomes: `app.py → graph.services → graph.{read,write}_session → graph.{runtime,prompts,utils,models} → db, llm, embeddings`. That layering is exactly what Track B exposes over MCP.

### Commit sequence

```
1. chore: smoke script + delete dead modules (llm/agent, main.py, db/base, frontend/frontend)
2. refactor: single home for shared helpers; kill star imports
3. refactor: move prompts to graph/prompts.py
4. refactor: split graph.py into runtime/read_session/write_session/services (1 commit per move)
5. refactor: flatten early-exit + lead-agent helpers; logging policy
6. refactor: frontend App.jsx split
7. chore: consistency sweep
```

Each commit independently bootable. Write `scripts/smoke.sh` in commit 1 (curl the 6 endpoints above against the test DB) so the whole series has a gate.

---

# Track B — Local code graph ↔ team wiki sync

## Correction to the tool report's framing

The report treats "local graph", "team wiki", and "search" as three products to shop for. The actual system has **this repo as the team brain** and needs only two external pieces:

1. A **local code graph** per developer (buy, don't build — CodeGraph / codebase-memory-mcp class; commodity now).
2. A **thin bridge** (MCP server + provenance schema) that this repo mostly already supports but doesn't expose.

Key realization: **the cross-team answer cache already exists in this codebase.** The flow — "agent discovers something, writes a doc, another agent at another PC gets it instantly" — is exactly:

- `POST /api/exogenous` with `question` + `body` + `source_node_ids` → creates an *agent note* node (`NodeType.exogenous`), embedded + FTS-indexed, provenance-linked via `reference` edges.
- Next `ask()` runs `_try_early_exit` → `search_with_evidence` surfaces the note → the router's **`reuse`** mode returns the note body verbatim in ~1 LLM call instead of a multi-subagent deep run. The token/time saving is built in.
- Staleness already has machinery: `cascading_update` + `_regenerate_exogenous_node` rebuild derived notes when supporting sources change.

So Track B is not "build a knowledge-sync system"; it's **five gaps** between what exists and what the team workflow needs.

## Architecture

```
Developer laptop                          Team server (this repo)
┌──────────────────────────┐             ┌────────────────────────────┐
│ coding agent (Claude etc.)│            │ FastAPI app (app.py)       │
│   │                       │            │  read svc / write queue    │
│   ├─ local code graph MCP │            │  sqlite graph + vectors    │
│   │  (symbols, calls,     │            │  enrichment worker         │
│   │   deps, blast radius) │            └────────▲───────────────────┘
│   │                       │                     │ MCP (new, Gap 1)
│   └─ wiki bridge MCP ─────┼─────────────────────┘
│      ask_team / save_note │   ask-first, save-after-discovery
└──────────────────────────┘
```

Query discipline the bridge enforces (this is the whole trick):

1. **ask_team(question, code_context)** *before* the agent burns tokens researching: the wiki early-exit either returns a cached note (done, ~free) or returns candidate context that seeds the local investigation.
2. Agent works locally, using its local code graph for symbols/calls/blast radius.
3. On a *durable* discovery (root cause, gotcha, architectural fact — not routine edits), **save_note(question, body, code_refs)** writes back. The write queue + enrichment pipeline dedupes, links, and clusters it.
4. Teammate's agent hits step 1 with a similar question → `reuse` fires → instant answer, zero re-research.

## The five gaps (build order)

### Gap 1 — MCP surface (the enabler; ~1 day after refactor Phase 2)

The wiki is REST-only; agents speak MCP. Add `mcp_server.py` exposing four tools over the *refactored* service layer (why Track A Phase 2 matters — `ReadGraphService`/`WriteGraphService` become directly importable):

- `wiki_ask(question, code_context?) → {answer, cited_nodes, mode}` — wraps `ReadGraphService.ask`; `mode` tells the agent whether it got a cached note (`reuse`) or fresh research.
- `wiki_search(text) → candidates` — cheap; seeds context without a full ask.
- `wiki_save_note(question, body, code_refs, origin) → job` — wraps the `create_exogenous` enqueue.
- `wiki_note_status(job_id)` — write-queue visibility.

Use the official Python MCP SDK, streamable-HTTP transport, mounted in-process with the FastAPI app — one deployment, shared runtime.

### Gap 2 — Code provenance on nodes (small schema change)

Today provenance is document-shaped (`source_path`, `source_ranges`). Add code-shaped provenance so notes match by *code identity*, not just semantics:

```python
class CodeRef(BaseModel):      # graph/models.py
    repo: str                  # e.g. "org/service-a"
    commit: str | None         # SHA the discovery was made at
    path: str | None           # file path
    symbol: str | None         # "ClassName.method" — matches local-graph vocabulary

class Node(...):
    code_refs: list[CodeRef] = []
```

- Store as a JSON column (migration mirrors the existing `_ensure_node_columns` pattern in raw_sqlite).
- Index `repo`, `path`, `symbol` into the node's `search_items` so hybrid search matches them — an agent asking about `PaymentReconciler.retry` finds the note even with zero semantic overlap in phrasing.
- Wire `WriteJob.user_id` / note `origin` = `{user}@{hostname}` so notes are attributable (Phase 5 tie-in).

### Gap 3 — Staleness = commit distance (reuse existing cascade)

A code note is trustworthy relative to a SHA. Cheap policy — no file watching, no server-side repo clone:

- `wiki_ask` accepts the caller's current `{repo, commit}`. When the router picks `reuse` on a note whose `code_refs` mention that repo at an older commit, the bridge (client-side, where git lives) runs `git diff --name-only <note_sha>..HEAD` and intersects with the note's paths.
  - No overlap → serve as-is.
  - Overlap → serve with `stale: true` + changed paths; the agent verifies before trusting, and on confirmation calls `wiki_save_note` again → the existing `_supersede` machinery replaces the old note and remaps edges.
- Later, optional: CI hook on merge-to-main posts an invalidation for affected notes via the existing revision pipeline.

### Gap 4 — Local code graph choice (buy, don't build)

Symbol indexing does not belong in this repo — wrong layer, solved problem. Bench in this order:

1. **codebase-memory-mcp** — single static binary, zero runtime deps, fastest team rollout.
2. **CodeGraph** — richer blast-radius queries if 1 falls short.
3. **Serena** — heavier; adds semantic *editing*; only if agents need it.

Acceptance test: "where is X defined / who calls X / what breaks if X changes", each in <1s and <2k tokens on the biggest work repo. The wiki never talks to these tools directly — the agent is the join point; the bridge just copies `{repo, commit, path, symbol}` from local-graph answers into `code_refs` when saving notes.

### Gap 5 — Note quality gate (protect the cache)

Failure mode of shared agent memory: garbage accumulates → router `reuse`s junk → team distrusts the wiki. Cheap defenses:

- Bridge-side save policy (prompt contract in the MCP tool description): save only *reusable* discoveries — root causes, invariants, gotchas, decisions. Not "renamed a variable."
- Existing entity-dedup enrichment already merges near-duplicate notes — verify it runs on exogenous nodes.
- Attribution from Gap 2 + existing `DELETE /api/node/{id}` = moderation path.
- Track hit rate: log router `reuse` events with note id; a note never reused in N weeks is a prune candidate via the existing supersede flow. Trivial `meta` counter after Gap 1.

## Sequencing against Track A

```
Week 1    A-Phase 0+1 (deletions, dedupe)        ← safe, immediate
Week 1-2  A-Phase 2 (split graph.py)             ← unblocks MCP work
Week 2    B-Gap 1 (MCP server)  ∥  A-Phase 3 (flatten helpers)
Week 3    B-Gap 2 (code_refs)   ∥  A-Phase 4 (frontend)
Week 3-4  B-Gap 3+5 (staleness, quality)  ∥  bench B-Gap 4 tools
Week 4    Pilot: 2 devs, bridge in their agent config, measure reuse hits
```

The tracks share almost no files after Phase 2, so they genuinely parallelize.

## What NOT to adopt (answers to the report)

- **No repo packers** (Repomix / Gitingest / code2prompt) — they solve one-shot context stuffing; you have retrieval.
- **No CocoIndex yet** — the ingest + enrichment queue *is* an incremental pipeline; adopt an engine only past sqlite scale.
- **No Joern / CodeQL / Semgrep** in the hot path — deep static graphs are a later, optional provider of extra `code_refs` edges.
- **No Zoekt / Sourcebot initially** — team code *search* is a different product; the wiki stores *conclusions about* code, not code. Revisit if the team asks "where is this string" more than "how does this work".
