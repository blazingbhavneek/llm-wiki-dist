# Jev/Classifier Search Upgrade — Research Report

Scope: `growi-search/` (the live search service) + the wiki build/sync pipeline
(`main.py`, `publisher/`, `graph/`). **No code was changed.** This report documents
what exists today, how the Jev model works, and the proposed ES + Jev hybrid
retrieval design.

---

## 1. What we have today (current logic)

### 1.1 The wiki pipeline (how pages and `00-目次` get made)

`main.py sync` runs: mount → **convert** (doc-parser → raw Markdown) →
**wiki generation** (section-wise rewrite) → **linker** (entity/behaviour
discovery per chunk, edges vetted per page, rendered into a `llm-wiki-links`
footer) → **publish** to GROWI.

**`index` now runs inside the publish sweep (updated pipeline).**
`_publish_sweep` gained `settings` and, after a clean publish batch, calls
`build_index(settings, only=<docs just published>, locked=True, ledger=<in-memory
ledger>)`; the delete branch calls `delete_document_index` so a removed
document's `00-目次` is trashed. `sync_once`, `delete_sources`, `move_sources`
and `publish_only` all pass `settings`, so **sync / watch / queue work / publish
all keep the index current automatically**. Failure policy: index errors are
logged (`stage=index`) and never fail the batch — a TOC page must not roll back
published documents; a batch that failed to publish indexes nothing, and its
retry indexes once. `main.py index` remains for manual repair (`--delete`,
`--no-publish`). Consequence for the sweep: index coverage is automatic, but a
document whose index upsert errored is silently missing → the slow
read-all-pages path (§3.2) must stay.

The `index` step (`publisher/index.py`) writes two kinds of pages into GROWI:

- `<document>/00-目次` — one per document. A Markdown list of *page cards*, each:
  `- [title](target) — summary` plus indented `- 章:` chapter,
  `- キーワード:` keywords, and `- エンティティ:` entity names **where the linker
  chunk metadata role == `defines`** (`document_cards()` in `publisher/index.py`)
  — i.e. **this page is the page that defines that entity**
- `/<target>/00-目次` — a root index listing every document with summary,
  `- ページ数:` page count, and aggregated keywords. **Always re-rendered from
  every document on every build** (a scoped `only=[...]` run cannot shrink it),
  so it always lists all documents and links each one to its `00-目次`

Key fact for the new design: **entity-defining information already exists per page**
in the `00-目次` cards (the `エンティティ` field), and in the linker SQLite catalog
(`data/<target>/metadata/wiki-linker.sqlite`) for anything richer.

### 1.2 growi-search today (all Elasticsearch + LLM-agent based)

Read-only FastAPI service over live GROWI. No local DB. Pieces:

| File | Role |
| --- | --- |
| `growi_client.py` | `/_api/search` (GROWI's Elasticsearch) + `/_api/v3` page/children reads |
| `researcher.py` | `IndexMap`, `ResearchSession`, lead agent + subagents, SSE events |
| `gateway.py` | `LlmClient` (chat), `Embedder`, `Reranker` (`/v1/rerank`, optional) |
| `markdown.py` | section split, link extraction, `parse_index()` for `00-目次` cards |
| `prompts.py` | ja prompts: router / shallow / lead / subagent |

**Current retrieval flow for `/api/ask`:**

1. **One ES call** (`GrowiSearchClient.search_pages`) → up to `search_candidates=30`
   hits; reranked on `title\npath\nsnippet` (falls back to ES order if no reranker).
   `00-目次` pages are filtered out of results.
2. **Index map merge** (`IndexMap`): reads root `00-目次` + every `<doc>/00-目次`
   into in-memory page *cards* (TTL 600s, stale-while-revalidate). Cards are ranked
   by IDF bigram-overlap + embedding cosine (RRF fusion) + optional reranker.
   Merged into the ES candidate list.
3. **Router LLM call** (`ROUTER_PROMPT`): picks `shallow` (answer from ≤2 hydrated
   pages with section-level rerank) or `deep`.
4. **Deep mode**: lead agent (ReAct, tools `search`/`explore`/`finish`) picks distinct
   **seed pages** and calls `explore`, which spawns bounded parallel subagents
   (default 2, tools `read`/`follow_link` outgoing-only/`search`/`finish`). All reads
   are charged to a shared per-run budget (12 page fetches, 6 searches per run) with
   request-local memoization + a process TTL page cache.
5. Lead `finish(answer, cited_node_ids)` → cited, streamed over SSE.

**Weakness today:** candidate discovery is similarity-only — ES keywords on page
text, plus IDF/embedding overlap on index cards. There is **no page-level
semantic relevance judgment** anywhere before chat-LLM budget is spent. If the
question's vocabulary misses what the cards and ES index contain, the relevant
region is unreachable except by luck of a subagent's outgoing links; nothing
cheaply answers "is this page relevant?" and nothing classifies recursively.

---

## 2. The Jev model (what it is and how it's used)

[`chaoliangUNSW/Jev-Style-0.8B-Decision-v3`](https://huggingface.co/chaoliangUNSW/Jev-Style-0.8B-Decision-v3)
— a *System One* decision model (fine-tune of Qwen3.5-0.8B, Apache-2.0):

- It **does not generate text**. One forward pass returns a **calibrated probability
  per option** for a typed question about a "state".
- Answer types: **choice** (N named options), **noul** (yes/no probability — exactly
  what we need for relevance), **score** (ordered levels).
- **25,600-token input**, accuracy flat from 1K to 24K tokens (preregistered claim,
  passed). This is the source of the "25K chunk" constraint.
- **Read once, ask many**: the state is rendered once and *any number of questions*
  are scored in one call (`many_mode="batched"`) — 2.6–4.6× faster than one call per
  question on 4K-token states. This is what makes per-page classification affordable.
- **Multilingual; Japanese is listed in the model's languages** and it beats Laya
  multilingual in all 51 evaluated MASSIVE locales (avg 71.7%). Caveat: the card
  says the fine-tuning pool "covers 19 languages" but never lists them, so it is
  **not documented whether the ja locale was trained or held out** — the question
  can be asked in Japanese, but validate ja calibration ourselves (§3.7).
- Probabilities are well calibrated (ECE 0.054 macro; ECE 0.027 on zero-shot
  tweet_topic) → a **>0.5 threshold on `p(はい)` is meaningful**, not arbitrary.
- Runs locally: 1.50 GB bf16 transformers / **0.53 GB Q4_K_M GGUF** (llama.cpp
  `jev-score`) / MLX. The repo ships `jev_style_decision.py`, a self-contained
  runtime handling rendering, verdict readout and fitted temperatures.

```python
from jev_style_decision import JevStyleDecision
m = JevStyleDecision(".")
r = m.decide(state_dict, question, options={"yes": ..., "no": ...}, category=...)
r["answer"], r["probabilities"]   # e.g. yes ≈ 0.91
```

Published latency (M1 Max, GGUF F16): 10 questions over a 4K-token state in
**1,381 ms**; 8K-token states answered in **2.3–2.6 s**. Fit for this project: it
is essentially a **fast, local, calibrated `relevance(page, query) → [0,1]`
oracle** — tens of ms per page when batched — replacing chat-LLM relevance
judgment in the search loop.

---

## 3. Proposed algorithm: ES + Jev classifier tree

Per query, run an **exhaustive Jev sweep over every document in the wiki** —
ES does not pre-filter the sweep. Documents with a `00-目次` are judged from
their cards first, then confirmed by full reads; documents without one have
every page read and judged. The sweep then expands **recursively across all
documents along entity-defining links**. Every high-confidence page ends up as
a **seed for the existing subagents**, which explore from there as they do
today. ES keeps its existing roles (fast `/api/search` lane, lead/subagent
`search` tool), it just no longer decides what the classifier looks at.

### 3.1 Question asked to the classifier

Type = yes/no, asked in Japanese: **noul** if the shipped runtime exposes it
(the card documents noul but its quickstart only demonstrates the two-option
`choice` API — check `jev_style_decision.py` at implementation time); a
`choice` over {はい, いいえ} matches the documented quickstart exactly and
gives the same calibrated `p(はい)`. The *state* is the candidate page
(content or index card) plus the other pages that define the entities used in it;
the *question* references the user query. Template:

```
状態:
# 対象ページ
タイトル: <title>
パス: <path>
本文（または目次カードの要約・キーワード）:
<page content / card text>

# このページで使われているエンティティを定義している他のページ
- <entity A> → <定義ページAのタイトル>: <定義ページAの要約>
- <entity B> → <定義ページBのタイトル>: <定義ページBの要約>

質問: 上記のページ（およびそのエンティティ定義ページ）は、
次の質問に答えるために関連しますか？
質問: <user query>

選択肢: はい / いいえ
```

Readout: `p("はい") > 0.5` → read full content / candidate; higher bar (e.g.
≥ 0.8 on the full content) → **confirmed page → seed**. Because the runtime
supports many questions per state, all cards (or pages) of one document are
scored in one call with the state rendered once.

### 3.2 The sweep — every document, two ways

Enumerate **all** documents (root `00-目次` lists every one, §1.1; folder
enumeration is the fallback). For each document:

**With `00-目次` (fast: judge cards, then read what passes):**
1. Parse the cards programmatically (`markdown.parse_index` gives
   title/summary/chapter/keywords/**entities`).
2. One batched Jev call: state = the document's card set, one `はい/いいえ`
   question per card (state rendered once). Entity-defining lookup, precisely:
   a card's `エンティティ` list means **that card's own page defines those
   entities** (`role == "defines"`). So for entity `E` used on page `X`, the
   defining page is *any card in any document whose `エンティティ` list
   contains `E`* — those are exactly the edges §3.3 walks.
   **High confidence → read that page's full content.**
3. **Full-read confirmation**: fetch each high-confidence page's full body
   (GROWI `get_page`) plus the pages that define the entities it uses, and ask
   the same question on the *full* content. High confidence again →
   **confirmed page**, enters the seed pool, and its entity links are queued
   for §3.3.

**Without `00-目次` (slow: read all pages regardless):**
1. Enumerate the document's pages (`growi_client.list_children` — **one level
   per call**, recurse into subfolders) and judge **every page** with the same
   question, on full content (no card stage). Live-GROWI-only entity context:
   `エンティティ` lines of sibling indexes if any; else page-vs-query alone
   (growi-search never reads the linker SQLite; §3.7).
2. A page + its entity-defining pages must fit the 25,600-token window;
   **otherwise split into 25K-token chunks with 10K overlap** and classify
   each chunk. **Include an entity-defining page in the state only if it is
   itself inside that chunk's region** — never definitions from a different
   part of the document. Page confidence = max over its chunks.
3. High confidence → confirmed page, same as above.

### 3.3 The tree — recursion across all docs over entity links

The "tree" is the sweep itself, and its edges are **entity-defining links**,
not ordinary page hyperlinks:

1. A confirmed page uses entities `E1…En`; the card `エンティティ` fields give
   the entity → defining-page map **wiki-wide**. Each defining page — even in
   a different document — becomes a child node: judge it with the same
   question (card stage first if its document has an index, full read if not).
2. A defining page that is itself relevant has its own entities → recurse.
   A `visited` set guarantees convergence; a low-confidence verdict prunes
   that subtree (a wrong いいえ only prunes, never fabricates).
3. Documents reached this way that weren't visited in the first pass still get
   their full sweep (§3.2) if their index says so — the frontier is a queue of
   (document, page) nodes, the 0.8B model is the pruner, entity defs are the
   edges. That is how the whole wiki is searched for relevant material without
   relying on whose hyperlinks happen to point where.

ES stays in the *answering* flow (lead/subagent `search`, `/api/search` fast
lane), not in the sweep.

Budget: Jev calls cost no GROWI read and no chat LLM. The full-read steps do
(fast-path step 3, and effectively all of the slow path), which today charge the shared
12-page `RunBudget` — far too small for an exhaustive sweep. The sweep needs
its **own budget**, and its reads should go through the normal `_fetch_page`
path so they warm the shared `PageCache`/memo — a page the classifier read is
free for the subagents that later explore it.

### 3.4 Seeding the subagents

When the sweep + entity recursion finish, **every confirmed high-confidence
page becomes a seed**. Group them into distinct starts and hand them to the
existing `explore(node_ids)` flow — subagents then read, follow their own
hyperlinks, search, and report exactly as today; the classifier does not gate
their exploration. ES-derived candidates remain only as fallback starts if the
sweep confirms nothing.

### 3.5 What changes vs. today (summary table)

| Concern | Today | Proposed |
| --- | --- | --- |
| Candidate discovery for seeds | ES + index-card keyword/embed/rerank | **exhaustive Jev sweep of every doc** (cards → full-read confirm; all pages when no index) |
| Relevance judgment per page | none (chat LLM router sees snippets only) | **Jev yes/no, ja prompt, p>0.5**, entity-defining pages in the state |
| Cross-document following | luck of hyperlinks / ES vocabulary | **entity-defining links walked recursively over all docs** |
| Seed selection for subagents | lead LLM picks from ES snippets | confirmed sweep pages as seeds; ES only as fallback |
| Subagent exploration | read/follow_link/search | **unchanged** (not gated) |
| Latency/cost of judgment | chat LLM call(s) | 0.8B local pass, batched many-questions (~ms/page) |

### 3.6 Natural hook points (for the future implementation)

- New **sweep stage** before routing: enumerate docs → per-doc card batch →
  full-read confirm → entity-link frontier loop; feed confirmed pages into
  `_seed_ids`/`_seed_context` (already consumed by `_seeded`) and `explore`.
- `IndexMap` — reuse `snapshot()` cards for the card stage; its entity fields
  are the entity→definer map for §3.3. Docs with missing/failed index pages
  fall to the slow path via `list_children`.
- Subagent flow — **no change** (§3.4).
- `config.py` — new settings: `jev_base_url` (or local path), `jev_threshold`
  (0.5), `jev_seed_threshold`, `jev_chunk_tokens` (25600), `jev_chunk_overlap`
  (10000), `jev_enabled`. Serve the model either as the shipped
  `jev_style_decision.py` runtime in-process, or a small llama.cpp/`jev-score`
  sidecar — same interface as `Reranker` (`score(query, docs) → probs`), so the
  lazy integration is a sibling class of `Reranker` in `gateway.py`.
- SSE: new `jev_gate` event type so the UI can show which pages the classifier
  kept/pruned (mirrors the existing `map` event).

### 3.7 Risks / notes

- **Japanese support is real but under-documented**: ja is in the language list
  and covered by the 51-locale MASSIVE eval, but that eval is *intent*
  classification and the card never lists its 19 fine-tuning languages. Expect
  good, unproven ja calibration → keep ES as recall insurance, prefer the
  two-threshold design (>0.5 candidate, high-confidence seed) so a wrong "いいえ"
  only prunes, never fabricates, and calibrate thresholds on a handful of real ja
  wiki pages before trusting 0.5.
- Card summaries in `00-目次` are one line (`_one_line(limit=300)`); the fast
  path's first stage judges *cards*, so recall depends on summary quality —
  that's exactly why the full-read confirmation exists.
- Entity-defining pages in the slow path (no index): only the linker SQLite has
  `role=="defines"` data pre-indexed; at query time in growi-search (live GROWI
  only), the practical source is the `エンティティ` lines of whatever indexes
  exist, or heuristics (pages whose title equals an entity name in the page).
- Chunked slow-path pages can be counted "relevant" twice (overlap) — dedupe by
  taking max probability per page.
- 25,600 tokens is the *whole input* limit: state (page + entity defs) **plus**
  the ja question template must fit; budget the template (~300 tok) off the top.

---

## 4. TL;DR

Today: ES keyword search → reranker → index-card map → chat-LLM router →
lead agent + subagents, with no cheap semantic relevance gate.
Proposed: per query, sweep **every document in the wiki** with the local 0.8B
**Jev-Style decision model** answering the Japanese yes/no question
「このページ（とそのエンティティ定義ページ）は質問に関連しますか？」—
judging `00-目次` cards when the index exists and confirming passes with full
reads; judging every page directly when it doesn't (25K-token chunks, 10K
overlap). High-confidence pages recursively pull in the **pages that define
the entities they use, across all documents**, and every confirmed page
becomes a **seed for the existing subagent teams**, which explore ungated as
today. ES stays as the fast lane and agent tool.
