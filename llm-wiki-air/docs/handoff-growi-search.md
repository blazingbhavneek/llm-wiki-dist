# Handoff — growi-search Jev pipeline, answer-quality + adaptive retrieval session

> Condensed replacement for the transcript of the Codex session that ran from
> `codex-growi-search.md` (2026-09-28, ended 17:32, ~25,700 lines). Root causes, decisions, file
> paths, standing user constraints and open items preserved; pasted uvicorn logs and diff dumps dropped.
> Prior context lives in `codex-growi-search.md` (itself a handoff) — this session started by reading it.

**Repo**: `/home/seigyo/llm-wiki` · **HEAD**: `9b8d178` ("scoped the JEV, far better results,
commented out elasticsearch noise, --continue flag is working now too")
**Uncommitted**: `growi-search/researcher.py` (+315/−36 vs HEAD) and `growi-search/prompts.py` —
these hold the last three fixes (structured intent, shortlist coverage gate, exact-term body gate).
Commits made during the session: `b1bb565` → `9b8d178` (the earlier diff-pipeline fix in
`graph/workspace/writer.py` is committed).

---

## 1. Where it started

Stage 1 (目次 scan + query rewrite) and stage 2 (card sweep) were working —
`Jev 目次 digest: 4 文書`, `documents=28 cards=552 pages=6 confirmed=6`, explorers ran and reported —
yet the UI still showed `Wiki内で、この質問に答える記述は見つかりませんでした。`
User's question: *"which stage is f***ing up"*. It turned out to be the last 5% of the pipeline,
and then a chain of further failures surfaced one by one.

## 2. Bugs found and fixed (chronological)

1. **Citation loss at the verify boundary.** The Jev path stores confirmed page IDs in
   `_seed_ids`, but the lead's answer only carried IDs from its own `finish`/`explore` call. Answer
   without citations → `_verified()` (`growi-search/researcher.py:2499`) treated it as "no evidence" and replaced the answer with the
   not-found string. Fix: retain seed + report IDs for the report-answering path
   (`researcher.py:2901` prints `Lead omitted citations; verifying against N seed/report pages`),
   plus `_degraded_answer()` (`researcher.py:2761`) so a failed lead still returns findings.
2. **Agent-invented character caps.** `LEAD_INPUT_CHARS = 24000` and a 12,000-char compiler split
   were introduced, silently discarding content. User caught it. Removed; inputs are now partitioned
   into **complete** chunks against `llm_context_tokens` (default 131072, `WIKI_LLM_CONTEXT_TOKENS`).
   Verification keeps the draft intact rather than checking only the pages that fit.
3. **Sweep admitted unreachable pages and re-judged bodies.** Two paths broke the intended flow: a
   fallback that added every first-level child under `/` as a Jev document, and a second Jev pass on
   accepted page bodies. Both removed from the active path. Jev now scores **page entries in
   reachable document-level `00-目次`** only; a "yes" entry is hydrated as a seed without re-scoring
   its body (`_jev_accept_card`, `researcher.py:2139`).
4. **Agent regression — project-root discovery deleted.** The agent's edit replaced the user's
   existing discovery with `state.docs`, collapsing stage 1 from all document 目次 to 14. User:
   *"i already had it implemented, who tf reverted this shit, this logic was here already."*
   Restored `_project_root_seeds` (`researcher.py:249`). Key correction from the user:
   **root is per project** — GROWI `/` is a container; each project has its own root `00-目次`; only
   first-level projects that have one are scanned (`/user` has none and is skipped).
5. **`Evidence.field="jev_card"` crash.** Pydantic `ValidationError` — allowed literals are
   `growi_es`, `title_path`, `section`, `linked_context`, `index_map`, `jev`. Changed to `"jev"`.
   This killed a 3-minute run inside `_jev_accept_card`.
6. **Lead required a `finish` tool call.** All 16 explorers + both compiler levels completed, then a
   tool-using lead agent was launched and answered in plain text without calling `finish` →
   `回答作成エージェントが失敗、調査結果から直接回答を作成: lead did not submit a finished answer`.
   Fix: accept a normal final text response; only report failure when there is genuinely no answer,
   and **final synthesis is now mandatory after L1/L2 compilation regardless of
   `WIKI_LEAD_AFTER_REPORTS`** (an env var must not route compiled reports back into the agent).
7. **The 109 KB garbage answer.** `_synthesize_reports()` (`growi-search/researcher.py:2807`) threw away the model's answer whenever its
   citation parser recognised no citation, substituting the **entire 86-seed prompt**, then
   `_retain_raw_reports()` (`growi-search/researcher.py:2780`) appended all 16 raw explorer reports again — so the response began with
   `node_id`, `why_matched`, `next_action`. Fix: final synthesis receives **only** the L2 compiled
   report, never the raw seed inventory; raw reports rescue a genuinely empty answer only;
   enumeration prompts forbid replacing entries with "etc.".
8. **Mirror sync traceback spam.** Background `GET /_api/v3/pages/recent` `httpx.ReadTimeout`
   printed full tracebacks every poll. Collapsed to one concise retry warning (`mirror.py:95`).
9. **Stage 1 had no notion of scope.** It asked only "does this 目次 relate to the topic", so a wide
   query said yes to everything and the precise rewrite came *after* that broad gate. Added a
   **structured intent pass** before the first gate: `JevIntent`
   (`target`, `scope`, broad/narrow flag, required evidence, **exact terms that must appear**,
   exclusions) via `llm.complete_structured(JEV_INTENT_PROMPT, …)` (`researcher.py:1939-1973`),
   with a plain-text fallback if structured output is unsupported. The intent is carried into the TOC
   summaries and the second requery, and is emitted as the `jev_intent` SSE event.
10. **Narrow-query false positives (the last fix, still unverified).** For
    `3rd parameter of mpf_mfs_open`, the card stage accepted 目次 card metadata, hydrated pages
    without checking their bodies, and the compiler correctly said "not in the supplied pages" while
    citing `001-M-plus申請開始手順`. Fix: for narrow queries an accepted page must contain the exact
    required term in title, path or body (`_jev_required_terms`, `researcher.py:2021`, applied at
    `:2004` and `:2153-2158`); unrelated hydrated cards are pruned before becoming seeds; if all
    candidates are pruned, the normal fallback route searches the full index.

## 3. Architecture as it now stands

**Two-stage Jev, now intent-scoped and adaptive:**
1. Full document-level `00-目次` Jev scope pass (unchanged; chunked at 15k, per-page scoring).
2. Structured intent pass → scoped requery (the precise page-level question).
3. Scoped query → **BM25 + embedding + configured reranker** → top-20 document shortlist
   (`JEV_TOC_RETRIEVAL_K = 20`, `researcher.py:636`) → LLM coverage check returning 十分/不足.
   **Fails closed**: broad/all/list questions, intent failure, or missing exact terms ⇒ scan all
   reachable documents. Only a structured, narrow, exact shortlist may take the early path.
   Zero page candidates from the shortlist ⇒ the same producer continues with remaining documents —
   **no second sweep, no separate SSE stage**. Coverage fields ride on the existing
   `jev_toc_digest` event (no new event type).
4. Card scoring on document `00-目次` entries → yes ⇒ hydrate page as seed (no body re-scoring).

**Compiler routing on deduplicated accepted seeds:**

| accepted seeds | path |
|---|---|
| < 10 | direct final compiler over the complete page bodies (no explorers) |
| 10–32 | document-local subagents (≤5 seeds per slice) → direct final compiler (one stage, no L1) |
| > 32 | refined query + second full Jev scan + two-stage compilation |

Second-pass seeds are merged with first-pass so the second scan cannot discard information.

**Retrieval internals** (the top-20 shortlist): embedder `cl-nagoya/ruri-v3-310m` at
`WIKI_EMBED_BASE_URL=http://10.160.144.101:51024/v1`; keyword channel is custom IDF-weighted token
overlap (NFKC + lowercase, Latin/digit words ≥2, CJK character bigrams), score
`sum(log(1 + document_count / df))`; the two channels fuse by reciprocal-rank fusion `1/(60 + rank)`,
then the reranker judges. Degrades to keyword-only if the embedding endpoint is down, and to the
exhaustive reachable scan if neither channel finds a signal. No reranker in the fusion itself.

**Subagent grouping** (`_seed_groups`, `researcher.py:835`): group by document first, slice each
document at `jev_subagent_group_size = 5`, one subagent per slice, each told which seeds belong to
others. `jev_subagent_groups = 8` is now a **compatibility argument only** — the hard truncation was
removed so extra groups queue in the executor instead of being silently dropped. Note the seed count
does not equal the subagent count (10 seeds in 2 docs ≈ 3 subagents; 31 seeds across several docs ≈ 7–8).

**Generation-token limits (OUTPUT only, never input truncation):** subagent report 16384 (clamped
256–16384, `config.py:184`), level-1 fold 32768 (`report_fold_tokens`, `config.py:127`), final
compiler 65536 (`final_compiler_tokens`, `config.py:128`), applied via
`gateway.LlmClient._for_output_tokens(max_tokens)`. Level-1 compilers run in a **separate pool**
(`compiler_concurrency = 4`, `config.py:129`) beside explorers and start as each 4-report batch
lands; fewer than 8 groups skip L1 entirely. Compiler prompts now require Japanese output
(`prompts.py:67`, `:82`).

**Temporarily disabled (commented out, not deleted):**
- Elasticsearch/lead-driven subagent creation — `researcher.py:3221`, `:3241`; lead prompt told not
  to call `explore`.
- Minimum-read enforcement in `_sub_finish` — `researcher.py:3379`, so Jev-scoped explorers can
  finish with whatever they found.

## 4. Standing user rules — read before editing anything

- **No truncation. No character limits.** Inputs are always complete. 16k/32k/64k are *generation*
  caps for the final LLM call of each stage, "hard implemented logic", not input budgets.
- **No information loss** from subagents → L1 → L2 → final. Summarising is fine; dropping is not.
- **Level 2 sees only level-1 outputs.** Raw reports stay outside model calls as a lossless
  fallback only — with 16–20 subagents, feeding raw reports to the final compiler recreates the
  context blowup the hierarchy exists to prevent.
- **Jev only on pages reachable from a project root `00-目次`**, judged from the document-level
  目次 card; read the full page only after a "yes".
- **Root = per project**, not GROWI `/`.
- **Don't run tests** — the user tests in the frontend/app. Minimal changes, reuse existing code.
- Don't invent a new stage or new SSE event when a rearrangement of the existing one will do.

## 5. Verification state

- **No tests were run in this session** — only `git diff --check`. The user asked repeatedly
  ("Dont run tests, i will run them myself in frontend"). Everything above is static-reviewed, not
  executed.
- `uv run` fails in the agent sandbox (`/home/seigyo/.cache/uv`: Read-only file system); tests are
  `python -m unittest` from inside `growi-search/` (pytest not installed). `test_app` and
  socket-binding tests can't run in the sandbox.
- Runtime deps come from a **different** checkout's venv
  (`/home/seigyo/c_repo/bhavneek/llm-wiki-neo/llm-wiki-air/.venv`, python 3.13) while source is
  `/home/seigyo/llm-wiki/growi-search`. uvicorn runs **without `--reload`** → every change needs a
  restart before testing.
- Observed metric trend across the session (same question, "all functions of moove library"):
  `917 pages / 183 s / confirmed 6` → `cards=552 pages=6 / 48 s` → `documents=14 cards=461 pages=86
  confirmed=86 / 30 s`. Stage-1 目次 scan ~59–103 documents.
- **`avg_p=0.00` in `JEV 走査` is now expected**, because body re-scoring was removed and `avg_p` is
  only populated by body verdicts; `card_avg_p`/`最高 p` are the live numbers. Display artifact
  worth cleaning up, not a bug.
- Two answer dumps used as fixtures during the session —
  `give me all functions of moove library.md` (109 KB, the garbage output) and
  `give me all functions of moove library(1).md` (14,962 bytes, much better but lossy) — are **no
  longer in the repo**. `root-00-mokuji.md` from the previous session is also gone.

## 6. Open items, in priority order

1. **Re-test the last three fixes** (uncommitted): structured `JevIntent`, fail-closed shortlist
   coverage gate, and the exact-term body gate. Repro case: `3rd parameter of mpf_mfs_open` —
   must return the real function page, not "not found", and must not cite `001-M-plus申請開始手順`.
2. **Broad-request completeness signal.** The agent flagged its own caveat: page count alone can let
   a broad "all functions" query exit early after two relevant pages. Early exit for broad scope
   should require an explicit complete index, not just a count.
3. `JEV_TOC_RETRIEVAL_K = 20` and the 十分/不足 coverage LLM call are new and untuned.
4. `avg_p=0.00` display artifact (see §5).
5. `_jev_toc_digest` (`growi-search/researcher.py:1874`) still has **test-only callers** — dead in production.
   It is the function that produces the `[文書名] 要約` format `JEV_QUERY_REWRITE_PROMPT` expects.
6. The `<10 / 10–32 / >32` thresholds are seed-count based; the safer form the user agreed to is
   `subagents < 16 AND combined report input fits the final compiler context`.
7. Commit `growi-search/researcher.py` + `growi-search/prompts.py`.
8. Mirror still times out on `/_api/v3/pages/recent`; only the log noise was fixed, not the cause.
   Mirror relists every ~30 min and `IndexMap` has its own TTL — allow for that when testing.

---
---

# Addendum (2026-09-28, follow-on session) — 目次 information-density spec

> **NOT STARTED. Recorded only — no code was changed in this session.**
> The next agent starts here. Everything under §1 is the user's requirement, in the user's
> own terms; §2–§6 is the supporting analysis produced while taking the request, so it does
> not have to be re-derived.

**Repo**: `/home/seigyo/llm-wiki` · **HEAD**: still `9b8d178` · **Uncommitted**: unchanged from
the previous session (`growi-search/researcher.py`, `growi-search/prompts.py`) + this file.

---

## 1. What the user wants — denser, parseable 目次 built at build time

**Per doc page (computed per page at build time, then pushed to GROWI):**
- **More keywords**, and **section-wise keywords** (per section, not one flat page list).
- **Ordering requirement, explicitly stated**: this extraction must happen **before the linker
  runs**. Otherwise the keywords are polluted by cross-links / 「関連資料」 and the information
  gets diffused across unrelated pages. (See §5 for the code facts behind this.)
- **A summary of what the page contains and what *kind* of information it is** — a general
  overview of the page, *not* the name of its subject.
  - Wrong: 「mpf_mfs_open の API リファレンス」
  - Right: 「mpf_mfs_open の API リファレンス、XYZ パラメータ、使用例」
- **Point-wise detailed summaries** — a point list of what the page covers, rather than prose.
- **General information about the page, not specifics**, because the specifics get updated later
  and a specific-heavy index goes stale on the next source revision.
- **Open question the user raised, undecided**: instead of paragraph-level summaries, store
  **line-by-line summaries** that can be parsed later for keyword / embedding search. The actual
  requirement is "searchable units small enough to be indexed and matched separately";
  line-by-line is one candidate granularity, not a decision.
- **The keyword extraction logic itself is the acknowledged weak point** and must be strengthened,
  not just raised in count. It is currently the shared 12-keyword `KEYWORD_PROMPT` in
  `graph/common/prompts.py:15`, unchanged from the old engine.
- All of the above must be produced **at build time**, be **parseable** (machine-readable, not
  prose), and be **pushed to GROWI** so JEV/目次 passes read it rather than re-deriving it.

**The 目次 hierarchy (three levels, each one the input for a JEV-like descend/don't-descend pass):**
1. **Document-level 目次** — what each *page* contains (the dense per-page entry above).
2. **Folder-of-documents 目次** — a summary of what each *document* in the folder contains.
3. **Folder-of-folders 目次** — what that whole folder contains, general.

The point of the hierarchy is that a JEV-like pass can score at the level it is at and decide
whether to read deeper, instead of being handed one flat page-card list.

## 2. Standing constraints that still apply (unchanged from §4 of the main handoff)

- No truncation, no character limits on inputs; 16k/32k/64k are *generation* caps only.
- No information loss between stages.
- JEV only on pages reachable from a **project-root** `00-目次`; root is per project, not GROWI `/`.
- Don't run tests — the user tests in the frontend/app. Minimal changes, reuse existing code.
- Don't invent a new stage or a new SSE event when a rearrangement of an existing one will do.
- `uv run` fails in the agent sandbox; runtime deps come from the `llm-wiki-air/.venv` checkout.

## 3. Measured baseline (why the current index is too thin)

Measured on `data/Moove` (14 documents):
- 458 published wiki pages → **473 目次 cards**, avg **5.25 entities** / **12.2 keywords** per
  card, **one summary line** each, 2,485 entity strings total.
- The stale (Sep-18, `wiki-chunk-meta-2`) `metadata/wiki-linker.sqlite` shows what the builder is
  capable of per page: 2,214 H2 chunks, **avg 7.26 claims/chunk** (~16k atomic facts), 13,408
  entity rows, 3,899 behaviours, `bridge_probe` filled 2214/2214.
- Old engine for the same corpus indexed ~10–20 units per page (title + summary + ≤20 claims +
  3000-char big chunks + **512-char** small chunks, each embedded *and* BM25-indexed)
  → roughly **20× more indexed retrieval units** than the 473 cards now.
- Repro of the miss: `031-直接編成ファイル操作API-(オープン・レコード操作).md` has 9 sections whose
  chunk metadata contains `cpuname, filenum, sbnum, bufsize, opentype, fcb, MPF_MFS_READLOCK…`,
  but its card exposes one first-section summary + 8 `defines` entities. `bufsize` / `opentype`
  never reach the index, so "3rd parameter of mpf_mfs_open" is unroutable from the card — and the
  body is never scored, because admission is card-only (`growi-search/researcher.py:1542`).

## 4. Old vs new — what to reuse

**Old (`/home/seigyo/c_repo/bhavneek/llm-wiki-neo/llm-wiki-dist`) per page**: `title` / `summary` /
one row **per claim** / `big_chunk` 3000 / `small_chunk` 512 / one row per table *record*
(`graph/librarian.py:2286`, sizes at `graph/config.py:140`); each row embedded into `vec_search_item`
**and** item FTS, plus node FTS, `vec_body`, `vec_summary`, `vec_bridge`, entity exact-index,
cluster names, and corpus vocabulary/ASR repair (`graph/vocab.py:1`). Query: vocabulary repair →
`classify_question` channel-weight profile (`graph/realtime.py:426`) → 6 channels → weighted RRF →
per-node evidence caps → cross-encoder **snippet** rerank → MMR (`graph/researcher.py:1214`).

**New**: one card per page (`publisher/index.py:71`), and every query-time channel — IDF-bigram
(`growi-search/researcher.py:422`), card embedding, reranker (`:444`), entity index, JEV card
verdict (`:1542`) — reads the *same* card text (`growi-search/researcher.py:224`).

**Regressions to undo (hooks already exist, plumbing is not the blocker):**
- `graph/linker/prompts.py:85` removes `claims`, `bridge_probe`, `role_judge` from the chunk-meta
  schema → `claims_json` / `bridge_probe` are permanently empty even though
  `graph/linker/catalog.py:89`, `:94`, `:334` still store, FTS-index and embed them.
- Same prompt narrowed entities from 漏れなく to 「重要なものを最大15個」 (+ a cross-document-ambiguity
  exclusion) and behaviours from 漏れなく to 最大10個. That tuning bought link precision and paid
  with retrieval surface.
- `graph/wiki/prompts.py:271` changed `reference_research` from full numbered originals to
  `target_summary` — cross-page facts are now merged from page summaries, so fewer facts reach
  the page bodies at all.
- `CLAIM_PROMPT` / `BRIDGE_PROBE_PROMPT` are still present and unused (`graph/common/prompts.py:24`).
- `Chunk.claims` / `Chunk.bridge_probe` (`graph/linker/chunks.py:99`) and the claim cleanup in
  `validate_meta` (`:172`) are intact.
- `growi-search/` never opens the linker catalog; the FTS-trigram table with a `claims` column is
  a live query surface nothing reads.

## 5. The before-linker ordering requirement (verified)

`graph/linker/render.py:124` `render_page` is what injects inline links and the
`## 関連資料` footer into the published page body. Consequences for the new pass:
- Chunk metadata already runs on pre-link text: `make_chunks` calls `strip_reader_references`
  (`graph/linker/chunks.py:147`) and `model_text` also trims the nav footer (`:133`). Any new
  per-page/section keyword+summary pass must keep that property — either run it before
  `render_page`, or strip reader references from the body first. Never extract keywords from a
  body that already carries inline anchors/footer reasons.
- The 目次 is (re)generated during the publish sweep, i.e. after rendering, so the dense entry
  must be taken from the stored pre-link metadata (`_planning/chunks.json` / catalog), **not**
  from the rendered page body. Otherwise the 目次 drifts with every link edit.

## 6. Known traps for whoever implements this

- **One card per page is load-bearing.** `IndexMap.card_for` / `card_for_target` return the
  *first* card matching a page id, `entity_definers` maps entity→cards, and `_jev_score_cards`
  batches one JEV question per card. Section-level cards change all of these and multiply the JEV
  card-sweep cost (~473 → ~2k+ cards for Moove); say so before switching granularity.
- `validate_meta` drops any entity whose `kind` is empty (`graph/linker/chunks.py:172`) while
  nothing in the prompt asks for `kind` — a silent entity sink.
- Publisher caps: 12 keywords, **8 entities filtered to `role == "defines"`**, and the page summary
  is `coverage.summary` or the **first** section summary only
  (`publisher/index.py:34`, `:80`, `:83`).
- 目次 pages themselves are excluded from JEV classification (`skip_index_pages`,
  `growi-search/researcher.py:1471`) and are not indexed into the map, so a folder-level summary is
  currently invisible to retrieval unless it is added as its own card kind.
- `MAX_DOCUMENT_*` / `MAX_FOLDER_*` caps (`publisher/index.py:35-38`) already truncate the
  folder/document level; a denser level 1 will hit them first.
- Mirror relists ~every 30 min and `IndexMap` has its own TTL — allow for that when testing.

## 7. Decisions the next agent needs before writing code

1. Card granularity: page-level card with more fields, or section-level cards (and does a JEV pass
   then run at section level or still page level?).
2. Where the dense metadata lives: 目次 markdown (parseable by `growi-search/markdown.py`),
   `_planning/chunks.json`, or the linker catalog — and which one JEV actually reads.
3. Point-wise summary format: bullet list in the 目次 body vs structured JSON in `_planning`.
4. Whether the "line-by-line summary" idea is taken, and if so what "line" means for tables,
   code blocks and images (the old engine's answer was 512-char windows + per-table-record rows).
5. Where the folder-of-folders summary comes from (LLM roll-up at publish time vs the existing
   `folder_summary`/`render_folder_card` counters in `publisher/index.py:188`).
