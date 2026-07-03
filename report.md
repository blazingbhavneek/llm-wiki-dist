# Local Code Graph Selection Report

**Decision: adopt `codebase-memory-mcp` as the per-developer local code graph that pairs with our cloud wiki. `CodeGraph` is the benched fallback; `Graphify` is the shape-different second fallback. Everything else — the original report's list plus a fresh web sweep of ~10 more tools — is rejected for this slot (some deferred to other slots).**

*Revision 2 (2026-07-03): added "Extended sweep" section — independent web search for tools the original report missed (Graphify, code-review-graph, CodeGraphContext, Sverklo, GitNexus, jcodemunch-mcp, Claude Context, Greptile, …). The sweep did not change the winner; it strengthened the pick with independent benchmarks and added one genuine contender (Graphify) worth knowing about.*

Context: our system (see `plan.md`, Track B) is a team wiki server (this repo — FastAPI + SQLite graph + agent-note reuse cache) plus a per-developer local code graph, joined by a thin MCP bridge. The local graph's job: answer "where is X defined / who calls X / what breaks if X changes" fast and cheap, and supply `{repo, commit, path, symbol}` provenance when an agent saves a discovery note to the wiki.

Selection criteria (derived from the five gaps in plan.md):

1. **Persistent, incremental local index** — agent queries must be sub-second and token-cheap; no re-parsing per question.
2. **MCP server** — the coding agent is the join point; the tool must speak MCP natively.
3. **Programmatic/CLI access outside MCP** — the bridge must query it to build `code_refs` when saving wiki notes (Gap 2).
4. **Git/commit awareness** — staleness of wiki notes is judged by commit distance (Gap 3); symbol-level diff mapping is a direct upgrade.
5. **Language fit** — our stack is Python + JavaScript/JSX.
6. **Team rollout cost** — install friction × every laptop; ops parity with the wiki (SQLite) preferred.
7. **License** — permissive, self-hostable.

All claims below verified against the live GitHub repos on 2026-07-03 (the original research report had errors — e.g. fabricated arXiv IDs — so nothing was taken on faith).

---

## Tier 1 — serious candidates (local code graph slot)

### 1. codebase-memory-mcp — SELECTED

MIT, ~25.2k stars (verified), single static binary, zero runtime dependencies. Indexes into SQLite at `~/.cache/codebase-memory-mcp/`; auto-sync file watcher; 158 languages via vendored tree-sitter grammars, with hybrid LSP giving semantic type resolution for Python, TypeScript, Go, Java, Rust and others. 14 MCP tools (`search_graph`, `trace_path`, `get_architecture`, `detect_changes`, `query_graph` with an openCypher subset, dead-code detection, semantic search). CLI mode exposes the same queries outside MCP. Team-shared compressed graph artifact (`.codebase-memory/graph.db.zst`) lets teammates skip reindexing. Indexed the Linux kernel (28M LOC) in ~3 minutes; sub-ms queries.

**Why it works with our app — criterion by criterion:**

- **Persistent index (1):** yes — SQLite graph, watcher-maintained. Exactly the "pre-indexed, don't re-derive" property the wiki relies on for its own notes.
- **MCP (2):** native, auto-configures Claude Code and 10 other agents.
- **Bridge access (3):** CLI + Cypher subset means our wiki bridge can resolve "which symbols did this session touch" programmatically and stamp `CodeRef{repo, commit, path, symbol}` onto notes. Its symbol vocabulary becomes our `CodeRef.symbol` vocabulary directly — no translation layer to write or maintain.
- **Git awareness (4):** `detect_changes` maps a git diff onto affected graph nodes. This upgrades our planned staleness check from file-level (`git diff --name-only <note_sha>..HEAD` ∩ note paths) to **symbol-level**: a wiki note goes stale only when the symbol it describes actually changed, not when anything in the same file moved. Fewer false invalidations → higher cache hit rate → the whole point of Track B.
- **Languages (5):** Python and JS/JSX in its "good" tier (75–89% relationship accuracy), Python additionally covered by the LSP hybrid. Adequate; see risk below.
- **Rollout (6):** one `curl | bash`, one binary, no Node/Python runtime to babysit per laptop. SQLite matches the wiki's storage — same mental model, same backup story. The shared graph artifact mirrors our team-wiki philosophy: index once, everyone reuses.
- **License (7):** MIT.

**Bonus not in the criteria:** `get_architecture` output can seed the wiki — pipe it through `POST /api/document` per repo so the wiki starts life with an architecture page for every codebase before any agent discovery lands.

**Why it might NOT work (accepted risks):**

- Tree-sitter relationship accuracy is 75–89% for our languages — call edges can be wrong or missing, especially dynamic Python (`getattr`, decorators, DI). Mitigation: acceptance test before rollout (below); the LSP hybrid covers Python's worst cases.
- Graph lives in `~/.cache` per machine — the shared artifact helps, but per-branch divergence between teammates' graphs is possible. Acceptable: the wiki, not the local graph, is the source of shared truth.
- Young project velocity risk: fast-moving, breaking schema changes possible. Mitigation: pin the binary version in team setup docs.

### 2. CodeGraph (colbymchenry) — FALLBACK, benched

MIT, ~57.3k stars (verified). Local SQLite (`.codegraph/codegraph.db`) with FTS5; incremental sync via native OS file events, debounced. 20+ languages incl. Python (claims 100% cross-file dependency coverage on the psf/requests benchmark) and JS/JSX. One primary MCP tool, `codegraph_explore`, deliberately designed to answer a structural question in a *single call* returning source, call paths, and blast radius — very token-lean. CLI + programmatic Node.js library API. Curl installer with bundled runtime.

**Why it would work:** meets criteria 1, 2, 5, 6, 7 cleanly. The single-call `codegraph_explore` design is arguably *better* than codebase-memory-mcp's 14-tool surface for agent token economy — fewer round trips. Python accuracy claim is stronger than cbm's tier rating.

**Why rejected (for now):**

- **No git/commit awareness** — nothing like `detect_changes`. Our Gap 3 staleness check stays file-level; we'd write and own the diff→symbol mapping ourselves. That's real code in our bridge that cbm gives us for free.
- **No team-sharing** — every laptop cold-indexes every repo. Fine for small repos, friction at scale.
- **Programmatic API is Node-only** (Node 22.5+) — our stack is Python; the bridge would have to shell out to the CLI only, losing the richer library access.
- Not eliminated: if the acceptance bench shows cbm's call-edge accuracy is materially worse on our actual repos, CodeGraph's stronger Python resolution wins and we eat the two missing features.

### 3. Serena (oraios) — REJECTED for this slot; optional later, different slot

MIT, ~26k stars. MCP server wrapping live language servers: find-symbol, find-references, type hierarchy, plus symbol-level *editing* (rename, move, inline, replace-symbol-body).

**Why it would work:** best-in-class semantic precision (real LSP, not tree-sitter heuristics); 40+ languages; light install (`uv tool install serena-agent`), Python-native tooling.

**Why rejected:** **it is not a code graph.** No persistent index, no caching — every query hits a language server on demand. Fails criterion 1 outright: no pre-indexed store, no cheap repeated queries, no artifact to share, nothing for the bridge to mine for `code_refs` in bulk, no git awareness. It's an editing/navigation toolkit, i.e. a *complement* to the winner, not a competitor. Revisit only if agents need safe symbol-level refactoring tools; it can run alongside cbm without conflict.

---

## Tier 2 — wrong slot or wrong scale (rejected without benching)

| Candidate | What it is | Why rejected for the local-graph slot |
|---|---|---|
| **DeepWiki-open** | LLM-generated repo wikis + diagrams | Competes with *our own product*, not with the local graph. Useful as a design reference for wiki-page generation, nothing to install. |
| **RepoAgent / CodeWiki** | AST-driven repo documentation generators | Same: they generate docs; our wiki *is* the doc store. Mine their ideas (git-change-tracked doc regeneration mirrors our `cascading_update`), adopt nothing. |
| **CocoIndex** | Incremental indexing engine (delta-only reprocessing, lineage) | Solves a pipeline problem we already solved: our write queue + enrichment worker *is* an incremental pipeline. Adopting it means rewriting working ingest for zero user-visible gain. Reconsider only past SQLite scale. |
| **Sourcebot** | Self-hosted team code search + ask | Team code *search* is a different product. The wiki stores conclusions *about* code, not code. Revisit if the team asks "where is this string" more than "how does this work". |
| **Zoekt** | Trigram code-search backend | Infrastructure primitive for building a Sourcegraph-alike; we are not building code search. |
| **OpenGrok** | Source cross-reference engine (Java) | Server-side xref for humans; heavy JVM deploy; no MCP; wrong decade of ergonomics for agent use. |
| **Kythe** | Language-agnostic indexing ecosystem | Build-system-integrated indexers, enterprise-grade complexity. Weeks of setup for what cbm does in one binary. |
| **SCIP** | Symbol/reference interchange format | A protocol, not a tool. Worth *knowing about*: if we ever swap local-graph vendors, `CodeRef.symbol` could adopt SCIP symbol syntax for stability. Not adoptable by itself. |
| **Stack Graphs** | GitHub's name-resolution graphs | Archived upstream. Concept only; dead as a dependency. |
| **Tree-sitter / Universal Ctags** | Parser / symbol-extractor primitives | Building blocks — choosing these means *building* our own indexer, which plan.md explicitly rules out ("buy, don't build"; wrong layer, solved problem). cbm already embeds tree-sitter. |
| **Sourcegraph** | Enterprise code intelligence | No longer open-core; enterprise pricing for capability we get free. Wrong cost profile for this team. |
| **Joern / codebadger** | Code Property Graph platforms (security/dataflow) | Deep static analysis is a *later, optional* provider of extra `code_refs` edges (taint/dataflow), not the daily-driver graph. Heavy (Scala/JVM), slow indexing, security-focused query language. |
| **Semgrep / CodeQL** | Pattern/static-analysis engines | Rule-based finding of bug patterns — CI tools, not knowledge graphs. Nothing for the bridge to query about structure. |
| **Aider repo-map** | Graph-ranked context selector inside Aider | Embedded in Aider's loop; not a standalone queryable service. The *idea* (PageRank over dependency graph to pick context) is already how cbm ranks. |
| **Repomix / Gitingest / code2prompt** | Repo → one big prompt packers | One-shot context stuffing. We have retrieval; packing the repo into a prompt is the opposite of the token-saving architecture. |

---

## Extended sweep — tools the original report missed (verified 2026-07-03)

### Graphify (safishamsi) — STRONG CONTENDER, rejected; second fallback

MIT, ~76.6k stars, YC S26 company, Python. Both an agent *skill* and an MCP server (stdio/HTTP). 36 tree-sitter grammars incl. Python and JS/JSX; also ingests SQL schemas, Terraform, Markdown, PDFs, images, video transcripts into one graph. Outputs `graph.json` + HTML visualization; `--update` incremental; **git post-commit hooks** for auto-rebuild and a **git merge driver** for `graph.json`; team sharing by committing `graphify-out/` to the repo, plus a shared-HTTP MCP mode where one process serves the whole team. CLI (`graphify extract/query/path`) *and* a Python module — and our bridge is Python.

**Why it would work:** best team-sharing story of any candidate (graph committed to the repo, merge-driver conflict handling); Python-native programmatic access; huge community; multi-repo graphing.

**Why rejected for this slot:**

- **Call-edge accuracy:** tree-sitter-only. The independent 3-way comparison (Medium, Jul 2026) confirms codebase-memory-mcp is the only one of the pack with hybrid-LSP type resolution — real type info refining call edges across 12 languages vs syntax-only parsing. For "who calls X / what breaks", that's the core competency, and Graphify is weaker at exactly it.
- **Query surface is generic-graph** (`graph_query`, `get_node`, `get_neighbors`, `shortest_path`), not code-intelligence-shaped (no callers/blast-radius/`detect_changes` primitives). The bridge would reimplement code semantics on top of generic traversals.
- **Multi-modal overlap with our own product:** Graphify graphs docs/PDFs/architecture — that is our wiki's job. Running both creates two competing knowledge stores for non-code content; exactly the confusion Track B exists to prevent.
- Non-code extraction needs an LLM API key (or local Ollama) — extra per-laptop config we don't need since we only want the code layer.

**Steal from it anyway:** the committed-graph-artifact + git-hook + merge-driver pattern is the cleanest team-sync mechanic seen anywhere; if codebase-memory-mcp's `.zst` artifact sharing proves clunky, copy Graphify's approach for distributing the local graph.

### code-review-graph — rejected (different product)

MIT, ~16k stars, Python, SQLite + FTS5, 23 languages. Purpose-built for **PR review**: risk-scored diffs, blast-radius across dependencies, auto-configures agents, has its own lightweight team "wiki" share. Honest benchmarks (8.2× average token reduction, admits small single-file changes can cost *more* than a raw read).

Rejected: it's a PR-review workflow tool, not a general always-on code graph — optimized for "review this diff", not "answer structural questions all session". Its built-in mini-wiki would also compete with ours. Worth revisiting later as a *CI-side* complement (its risk-scoring on PRs could post `cascading-update`-style invalidations to our wiki — Gap 3's optional CI hook), not as the local graph.

### Sverklo — watchlist, not adopted

Hybrid search + graph + ranking + memory, SQLite + ONNX, 12 languages, bi-temporal git awareness, CLI. Sounds close to ideal on paper — but the only comparison ranking it highly is *its own blog*, cold-start indexing is admitted-slow on large repos, and 12 languages is the thinnest coverage of the serious candidates. No independent validation found. Re-check in 6 months.

### Remaining sweep finds — rejected quickly

| Tool | What | Why rejected |
|---|---|---|
| **CodeGraphContext** | MCP + CLI indexing into a graph DB (FalkorDB/KuzuDB default; Neo4j optional), 23 langs, `cgc watch` | Pre-1.0 (~3.9k stars), graph-database dependency heavier than SQLite, no git awareness. Nothing it does that the winner doesn't. |
| **GitNexus** | Knowledge graph in KuzuDB + browser UI, ad-hoc Cypher | Graph-only (weak semantic recall), no git awareness, no CLI for the bridge. UI-first, agent-second. |
| **code-grapher (mufasadb)** | Small MCP graph server | Early-stage, minimal traction; superset covered by winner. |
| **jcodemunch-mcp** | Search + tree-sitter symbols, best-in-class definition lookup (0.65 F1) | **Dual-licensed — paid for commercial use.** Fails criterion 7 outright. |
| **Claude Context** | Embedding search over code (Milvus + BM25) | Requires external vector-DB service per laptop; search-only, no structure. Our wiki already owns semantic search. |
| **code-graph-mcp / Code Pathfinder / Local Code Search MCP** | Lightweight graph / AST search / lexical search | Each covers a thin slice (5–6 languages, basic ranking, sparse docs) of what the winner does whole. |
| **Greptile** | Hosted code-review bot, cloud graph | Proprietary; code leaves the machine. Non-starter for a team knowledge system we host precisely to keep context in-house. |

### Independent validation picked up in the sweep

- arXiv 2603.27277 ("Codebase-Memory: Tree-Sitter-Based Knowledge Graphs for LLM Code Exploration via MCP"): across **31 real-world repositories — 83% answer quality, 10× fewer tokens, 2.1× fewer tool calls** vs file-by-file exploration. First third-party benchmark of the winner; numbers are more modest than the project's own "99% fewer tokens" marketing, and credible for it.
- The 3-way Medium comparison independently confirms the winner's differentiator: it is the only candidate refining call edges with LSP type information; competitors are syntax-only.
- Caveats surfaced: Windows needs WSL2 (fine — team is macOS/Linux), and "memory layer is shallow" (irrelevant — our wiki *is* the memory layer; we explicitly don't want the local tool competing on that).

---

## Decision rationale, condensed

codebase-memory-mcp is the only candidate that satisfies **all seven criteria**, and it uniquely provides the two features that reduce code *we* would otherwise write and own:

1. `detect_changes` → symbol-level staleness for wiki notes (Gap 3 nearly free).
2. CLI/Cypher access → `code_refs` provenance extraction (Gap 2 trivial).

CodeGraph loses on exactly those two (plus Node-only library API) while winning slightly on token-per-query and claimed Python accuracy — hence fallback, not rejection. Graphify — the biggest tool the original report missed — has the best team-distribution mechanics but generic-graph queries, tree-sitter-only call edges, and a multi-modal scope that collides with our wiki; second fallback, and a pattern source. Serena fails the category definition. Everything else is a different product, a primitive we refuse to build on, license-poisoned, or dead.

Fallback order if the acceptance gate fails: **CodeGraph** (purpose-built code intelligence, strongest Python claims) → **Graphify** (if the failure mode is distribution/team-sync rather than query accuracy).

## Hackability & integration surface (added after team discussion)

Two clarifications that came up:

**GitLab: a non-issue.** None of the tier-1 candidates talk to a code forge. They index the *local checkout* and the *local `.git` directory* — GitHub vs GitLab vs Bitbucket never enters the picture. Forge APIs only matter for wiki-generator tools (DeepWiki-open et al.), which we rejected anyway. No modification is needed to run any of these against GitLab-hosted repos.

**"Hackable" means integration surface, not source-patching.** The plan's bridge (Python, ours) is where all custom logic lives; the local graph tool should be consumed, not forked. What matters is how many clean connection points a tool exposes:

| Tool | Source hackability | Integration surface (what the bridge can use without forking) |
|---|---|---|
| **codebase-memory-mcp** | Low — C, single static binary | **Best.** (1) CLI with JSON output + openCypher subset; (2) MCP; (3) **open SQLite data file** — the graph is a plain SQLite DB our Python bridge can read directly with stdlib `sqlite3`, same engine as the wiki. Binary stays untouched → upgrades stay trivial. |
| **CodeGraph** | Medium — Node.js | CLI; MCP; Node-only library API (wrong language for our bridge); SQLite file also readable in principle (schema undocumented). |
| **Graphify** | High — Python, pip-installable | CLI; MCP; Python module; committed `graph.json` (open format). Most forkable — but also the largest codebase of the three (multi-modal ingestion, visualization, office/media parsers), so "easy to modify" ≠ "small to understand". |

Conclusion: codebase-memory-mcp is simultaneously the least *forkable* and the most *connectable* — and connectable is what the architecture needs. If a requirement ever genuinely demands patching indexer internals (not just reading its output), that is the trigger to re-evaluate toward Graphify, per the fallback chain. Do not patch the binary; treat "we need to fork the graph tool" as a design smell in the bridge first.

## Acceptance test before team rollout (gate)

On our largest work repo, via MCP from a real agent session:

1. "Where is `<known symbol>` defined?" — correct file:line, <1s, <2k tokens.
2. "Who calls `<known function>`?" — ≥90% of known callers found (spot-check against grep), <1s.
3. "What breaks if `<core class>` changes?" — blast radius includes the known dependents.
4. `detect_changes` on a 10-commit-old SHA — changed symbols match `git diff` reality.
5. Cold index time on the repo acceptable (<5 min) and shared-artifact restore works on a second machine.

Fail ⇒ rerun the same five against CodeGraph and re-decide.

---

# Final verdict

**codebase-memory-mcp is the local code graph for this project.** Locked pending only the acceptance gate above.

The complete case, in one place:

1. **Only candidate satisfying all seven criteria** — persistent SQLite index, native MCP, CLI/Cypher for the bridge, git-diff→symbol mapping (`detect_changes`), Python+JSX coverage with LSP-hybrid call edges, single-binary rollout, MIT.
2. **Best call-edge accuracy in the field** — the only tool refining edges with real LSP type information; every competitor is syntax-only tree-sitter. "Who calls X / what breaks" is the job; this is the differentiator.
3. **Independently validated** — third-party 31-repo benchmark: 83% answer quality, 10× token reduction, 2.1× fewer tool calls.
4. **Two plan.md gaps nearly free** — `detect_changes` gives symbol-level staleness for wiki notes (Gap 3); CLI/SQLite access makes `code_refs` extraction trivial (Gap 2).
5. **Most connectable, least forkable — and that's the right trade.** Lightweight to *run* (one static binary, zero deps per laptop), but not "short" to *read* — it's a C codebase vendoring 158 grammars; nobody should open it. The short part is **ours**: the bridge glue is ~200 lines of Python calling its CLI or reading its open SQLite file (stdlib `sqlite3`, same engine as the wiki). All "hacking" happens in our bridge, in our language, under our control — the binary stays pristine and upgradeable.
6. **GitLab-proof** — indexes local checkout + local `.git`; no forge dependency exists to hack around.

Risk register (accepted): tree-sitter tier accuracy 75–89% for our languages (mitigated by LSP hybrid + acceptance gate), per-machine cache divergence (wiki is the shared truth, local graph is disposable), young-project churn (pin binary version), Windows needs WSL2 (team is macOS/Linux).

Fallback chain: gate fails on **query accuracy** → CodeGraph. Need dies on **distribution/team-sync** or we genuinely must patch indexer internals → Graphify. Neither trigger is expected; both are written down so the re-decision takes an hour, not a week.

Next concrete step (Track B, plan.md): install on two laptops, run the five-point gate on our largest work repo, then build the bridge MCP (`wiki_ask` / `wiki_save_note` + `code_refs` from cbm's CLI).

---

*Verified sources (fetched 2026-07-03): [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) · [CodeGraph](https://github.com/colbymchenry/codegraph) · [Serena](https://github.com/oraios/serena) · [Graphify](https://github.com/safishamsi/graphify) · [CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext) · [12-server comparison (Sverklo blog — vendor-authored, read skeptically)](https://sverklo.com/blog/practical-guide-mcp-code-intelligence/) · [3-way comparison, Medium](https://coder11.medium.com/code-review-graph-vs-graphify-vs-codebase-memory-mcp-4a2357cc2e71) · [arXiv 2603.27277](https://arxiv.org/pdf/2603.27277). Original-report Tier-2 assessments based on its descriptions plus prior knowledge; none were load-bearing for the decision.*
