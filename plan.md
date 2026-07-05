# Plan — LLM Wiki × codegraph: team knowledge sync

One goal: **as developers use their coding agents (GitHub Copilot CLI at the company), everything durable an agent learns becomes instantly available to every other developer's agent — with zero change to how users work.** One agent discovers a root cause on one PC; the next PC's agent reuses it instead of re-researching, cutting calls, tokens, and time.

Two components:

- **This repo (llm-wiki)** — the team-shared brain. FastAPI + sqlite graph + vectors + FTS + LLM enrichment pipeline. Deployed once on a company server.
- **codegraph** (`vendor/codegraph`, TypeScript, MIT) — per-developer local code intelligence: symbols, call graphs, blast radius, auto-sync file watcher. Chosen over `codebase-memory-mcp` (C, no HTTP client, 68k LOC, stack mismatch) — decision record below.

---

## Refactor status (old Track A) — DONE

The refactor plan this file used to carry has landed. Current state, verified:

- Backend rebuilt as an actor model: `graph/core.py` (models/settings/prompts), `graph/store.py` (sqlite, thread-local connections), `graph/gateway.py` (LLM/embed/rerank clients), `graph/librarian.py` (all writes + enrichment queue), `graph/researcher.py` (ask/early-exit/deep research). Old `db/`, `llm/`, `embeddings/` packages deleted; god files gone.
- Frontend split: `App.jsx` 2,804 → 555 lines; components in `src/components/layout/` + `markdown/`; state in `src/hooks/` (`useAskStream`, `useGraphData`, …). `MarkdownView.jsx` 1,278 → 220.
- `scripts/smoke.sh` gates endpoint health.

Residual cleanup (fold into normal work, no dedicated track):

- `graph/cli.py`: run each subcommand once post-split; fix or delete broken ones.
- `Settings`: move `max_reads`/`max_agents` semaphore values out of constructor magic numbers.
- `WriteJob.user_id` plumbed but never set — gets wired in Phase W2 below (attribution).

---

## Decision record — why codegraph, why wrap not fork

Evaluated `codebase-memory-mcp` (previously vendored, now removed) vs `colbymchenry/codegraph`:

| Criterion | codebase-memory-mcp | codegraph |
|---|---|---|
| Language / fork maintainability | 68k LOC C; team is Python/JS | TypeScript; team's language |
| Outbound HTTP (needed to reach wiki) | **None** — would require adding libcurl+TLS to a static C binary | `fetch()` built into Node |
| Embeddable as library | No | Yes — npm package, `main`/`types` entry points |
| Indexer maturity | Fine | Better: framework-aware, auto-sync watcher, benchmarked |
| Rollout | Static binary | Bundled-Node installers / npm; fine |

**Wrap, don't fork.** codegraph exports a programmatic API. We write our own thin MCP server (the *bridge*) that imports codegraph as a dependency. We own zero upstream lines; company-paced upstream updates are a version bump. `vendor/codegraph` is a plain clone (own `.git`, ignored by this repo) used for reading source and pinning the version we build against.

Fallback trigger: if codegraph's index fails the acceptance test on the biggest company repo ("where is X / who calls X / blast radius", each <1s, <2k tokens), revisit. Nothing else reopens the decision.

---

## Architecture

```
Developer PC                                        Company server
┌────────────────────────────────────┐             ┌──────────────────────────┐
│ GitHub Copilot CLI (agent)         │             │ llm-wiki (this repo)     │
│        │ MCP (stdio)               │             │ FastAPI app.py           │
│        ▼                           │             │  researcher (reads)      │
│ team-memory bridge (ours, TS)      │    HTTPS    │  librarian (writes +     │
│  ├─ codegraph (npm dep, local      │────────────▶│   enrichment queue)      │
│  │   index of the repo being       │             │  store (sqlite+vec+FTS)  │
│  │   worked on)                    │             │  gateway (LLM)           │
│  ├─ wiki client (fetch)            │             │                          │
│  └─ git (provenance + staleness)   │             │ POST /api/traces  (new)  │
└────────────────────────────────────┘             │ GET  /api/search         │
                                                   │ POST /api/exogenous      │
                                                   └──────────────────────────┘
```

One install per dev PC: the bridge (which bundles codegraph). One config entry in Copilot CLI. Wiki server needs no MCP surface — the bridge translates MCP↔REST. (The old "Gap 1 `mcp_server.py`" idea is dead: the MCP endpoint lives client-side in the bridge, where git and the local index also live.)

**Design principle: knowledge flows automatically.** No reliance on agents virtuously calling a save tool. Both directions are passive side effects of normal agent work.

### Flow 1 — Team knowledge arrives unasked (read path)

The agent calls the code tools it already uses (`codegraph_explore`, etc.). The bridge serves them from the local codegraph index, and **piggybacks** a wiki lookup (`GET /api/search` with the same terms + symbols from the local result) on every call. Relevant team notes get appended to the tool response:

```
[codegraph result: source, call paths, blast radius]
── Team knowledge ──
"PaymentReconciler.retry double-fires on timeout — root cause: …"
  (rahul@dev-pc2, 12 days ago, commit a3f21c — fresh: no touched files changed since)
```

The teammate's discovery lands inside the agent's normal workflow *before* it burns tokens re-deriving it. Wiki unreachable → codegraph result returns alone; the bridge never blocks on the network (short timeout, silent degrade).

The bridge also exposes an explicit `wiki_ask(question)` tool wrapping `POST /api/ask` (server early-exit: cached-note `reuse` ≈ 1 LLM call, else research) — useful, but the system does not depend on agents calling it.

### Flow 2 — Knowledge leaves unsaved (write path)

The bridge **background-posts agent session context to `POST /api/traces`** (new endpoint). The *server* mines it: an extraction job in the librarian's existing enrichment queue has the LLM read the trace and pull out durable discoveries — root causes, invariants, gotchas, decisions — or, for most sessions, nothing. Extracted discoveries become `exogenous` notes with `code_refs` auto-derived from paths/symbols present in the trace.

Why server-side extraction: the wiki already owns the LLM gateway, dedup enrichment, embedding, clustering, and the write queue. The client stays dumb (collect + POST); quality control lives where the machinery is.

The bridge also keeps an explicit `save_note(question, body)` tool with a strict save-policy in its description (reusable discoveries only) — a bonus path, not the load-bearing one. The bridge auto-fills `code_refs` on it from the local codegraph index + git, so refs are exact, never hallucinated.

### Code provenance — `CodeRef` on nodes

Notes must match by *code identity*, not just semantics:

```python
class CodeRef(BaseModel):      # graph/core.py
    repo: str                  # "org/service-a"
    commit: str | None         # SHA at discovery time
    path: str | None
    symbol: str | None         # qualified name, codegraph vocabulary
```

- `Node.code_refs: list[CodeRef]` stored as a JSON column in `store.py` (follow the existing ensure-columns migration pattern).
- Index `repo`/`path`/`symbol` into the node's `search_items` so hybrid search matches them — a question about `PaymentReconciler.retry` finds the note with zero phrasing overlap.
- Attribution: note `origin` = `{user}@{hostname}` from the bridge; wire `WriteJob.user_id`.

### Staleness — checked where git lives (client-side)

A code note is trustworthy relative to a SHA. When the bridge is about to show a note whose `code_refs` hit the current repo:

- `git diff --name-only <note_sha>..HEAD` ∩ note's paths.
- Empty → serve as-is. Non-empty → serve flagged `stale: true` + changed paths; agent verifies before trusting; a corrected save supersedes the old note via the wiki's existing `_supersede` machinery.
- No server-side file watching, no server repo clones. Optional later: CI hook on merge-to-main posts invalidations.

### Quality gate — protect the cache

Shared agent memory fails when junk accumulates and `reuse` serves it. Defenses, all server-side or structural:

- Extraction prompt is the primary gate (Flow 2): high bar for "durable"; most traces yield nothing.
- Existing entity-dedup enrichment merges near-duplicate notes — verify it runs on `exogenous` nodes.
- Attribution + `DELETE /api/node/{id}` = moderation path.
- Hit-rate tracking: log router `reuse` events with note id; notes never reused in N weeks are prune candidates via supersede.

---

## Build order

```
Phase W0  Setup (instruction.md): remove old vendor, clone+build codegraph,
          index it. Acceptance test on biggest company repo.
Phase W1  Bridge skeleton (new repo or bridge/ dir, TypeScript):
          MCP server over stdio, codegraph as dependency, passthrough tools.
          Verify in Copilot CLI: agent answers code questions via bridge.
Phase W2  Wiki server: CodeRef schema + search_items indexing + origin/user_id.
          (No new server MCP work — REST only.)
Phase W3  Flow 1: piggyback wiki /api/search into bridge tool responses +
          staleness flagging + wiki_ask/save_note explicit tools.
Phase W4  Flow 2: POST /api/traces endpoint + librarian extraction job;
          bridge background trace posting.
Phase W5  Pilot: 2 devs, bridge in Copilot CLI config, measure reuse hits
          (router reuse-event counter). Tune extraction prompt on real traces.
```

Each phase independently shippable; Flow 1 alone already delivers "teammate's knowledge reduces my agent's calls."

## What NOT to adopt (unchanged conclusions)

- **No repo packers** (Repomix/Gitingest) — one-shot context stuffing; we have retrieval.
- **No CocoIndex** — librarian's ingest+enrichment queue *is* the incremental pipeline at this scale.
- **No Joern/CodeQL/Semgrep** in the hot path — optional future `code_refs` providers.
- **No Zoekt/Sourcebot** — team code *search* ≠ team *conclusions about code*; revisit only if "where is this string" dominates.
- **No forking codegraph** — wrap it; forks of fast-moving repos rot.
