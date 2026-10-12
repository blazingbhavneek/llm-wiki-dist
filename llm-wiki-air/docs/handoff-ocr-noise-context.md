# Handoff: removing OCR noise from wikis without losing facts

Design task, not an implementation task. **Do not change code yet.** Read the files
listed below, reproduce the measurements, then write a proposal. A previous review
pass (`handoff-repair-pass.md`) described the symptoms; this note adds root causes
and the design questions that need an answer before anyone touches a prompt.

## 1. What we are trying to decide

The fast pipeline (generate + `--repair`) currently cannot delete OCR/parser garbage,
and in some cases is punished for trying. We want a rule set where:

- real information survives, always;
- parser noise (degenerate repeats, fake English schema, refusal sentences, split
  tables) can actually be removed;
- the writer and judge stop spending their 4 attempts on non-defects;
- the roles get enough context to know what a fragment is, without inventing facts.

Questions to answer, in this order:

1. What is the taxonomy of "OCR noise" vs "badly parsed but real" vs "real and
   repeated on purpose"? We only have anecdotes today. Each class needs a name, a
   detector, and a decision: fix in `convert`, fix in `repair`, or leave and report.
2. Where should noise be removed? Cleaning in `convert` fixes every downstream phase
   once; cleaning in `repair` needs the judge to authorise every deletion. The
   current prompts make the second path nearly impossible (section 4).
3. How do we make the guard rails asymmetric **in the right direction**? Today every
   check only fires when the candidate *changes* something, so the cheapest
   winning move is always "copy the source verbatim".
4. What context should `wiki writer`, `repair writer` and `judge` see, and in which
   capacity? Preceding source text (~10k chars) is the obvious proposal, but the
   prompts currently forbid treating surrounding context as evidence, and two
   measured regressions came from exactly this kind of bleed (section 6). Decide:
   neighbour context as *non-evidence framing* only, or as admissible evidence with
   a declared range. These have different failure modes.
5. What is the metric that says a page got better? Coverage against `source_ranges`
   alone is not it (a page at `judge_score: 100` can still ship triple-duplicated
   prose). Propose a small gold set and a scoring rubric that can run before/after.
6. Should `repair_status` remain a gate? It is demonstrably unreliable (section 3).

## 2. Corpus and how to reproduce the numbers

Two trees, same 20 source documents, same page filenames:

- production (older): `data/東北中給需給運用計画/wiki/`
- repaired (`--repair`, 18 of 20 docs done): `data/東北中給需給運用計画_test/wiki/`
- per-document repair state, judge verdicts and every writer draft:
  `data/東北中給需給運用計画_test/metadata/state/<doc>/state/pages/NNN.json` and
  `.../work/repair/wiki-repair-ja-7-fast-v5/page-NNN/rewrite-0K.md` +
  `rewrite-0K-mechanical.json`, `initial-judge-prompt.md`
- ground truth used by the judge: `.../state/<doc>/source/original.md`

Page-level outcomes across the 20 documents: **627 repaired, 137 review, 137 clean**;
714 of 860 page files differ from production. `要求仕様書` (132 pages) and
`取引ガイド_ver.9_260401.pdf` (360 pages, in progress) are not repaired yet.

The measurement scripts below were ad hoc and are not committed. Rebuild them; they
are the only objective yardstick we have.

- body-only repetition: strip `data:image/...`, `<details>...</details>`,
  `<image-unit>` / `<image-description>`, ``` fenced blocks; drop table lines
  (`^|`, `<td`, `<tr`), separators and headings; then count identical-line runs
  (>=3), within-line character loops, and repeated 3-line blocks.
  Result over 860 files, production -> repaired: pages flagged 111 -> 114,
  identical runs 97 -> 89, within-line loops 6 -> 4, repeated blocks 1274 -> 1278.
  **Prose-level repetition is essentially unchanged, and only ~5 pages per tree
  still hold a true pathological loop.** All the visible horror lives in the
  stripped parts (OCR `text_image`/`flowchart` blocks and mermaid fences).

## 3. Evidence that the loop vetoes real fixes

- `D版送付用資料/#33136_予備率一定配分全般（需計画面）_r3.pdf`, page 029
  (`029-翌日需給運用と簡易計算結果.md`): all 4 writer attempts rejected with
  `仕様上重要な識別子・定数を維持すること: 13aaaaaaaaaaaaaaaaaaaaaaaaaaaaa, ...`
  — the OCR degeneration itself was extracted as a required identifier. Final
  state `repair_attempts: 4`, `repair_selected_attempt: 0`, `judge_score: 40`,
  `status: review`, and the loop-laden original was restored.
- `B1/…週間需給運用計画 機能仕様書.pdf/055-第9章-9.4-調整電源計画集計.md:24` —
  the same two-sentence note appears 3 times; page shipped as `repaired`, attempt 2,
  `judge_score: 100`.
- `D版送付用資料/#33130,33132_…_r3.pdf/024-時刻別数値表.md:15` — `充放電量(MWh)`
  three times in one header line; `repaired`, `judge_score: 100`.
- `graph/fast/repair.py:591` `_loop_fragment` fired on legitimate markdown table
  separators: 326 of 685 mechanical rejections corpus-wide, of which ~226 are
  `------` / `------------------------` / `| :--- | :--` rows. 46 of the 137
  `review` pages were blocked by that check alone.
- `D版送付用資料(追加)/M要件-266D_…/005-日毎算出.md:5` still contains
  「（提供された原文に内容が含まれていないため、出力することができません。）」 —
  4 candidates rejected, `selection: "original"`.
- `B1/(社外秘)需給運用計画 年間需給運用計画 画面・印字仕様書.pdf/097-第4.11-系統制約設定エクセル….md`
  burned 4 attempts, changed one nav label, and keeps three sentences the judge
  itself called 原文に根拠なし, every time.

Regressions introduced by the repair pass, which any proposal must not repeat:

- 図/表 caption IDs: source 391, production carried 373, repaired carries 317 in
  `B1/…週間需給運用計画 機能仕様書.pdf` (56 lost, 0 recovered); 節番号 dropped from
  6 H1s while filenames still carry them.
- `B1/…年間需給運用計画 画面・印字仕様書.pdf/088-…火力コンバインド運転条件….md`:
  104 markdown rows became an HTML table with 86 `<tr>`, losing the ④-⑩ callout
  markers and `除外-3バンド`; still labeled `repaired`.
- `…/137-…` and `…/119-…`: multi-line `<image-unit>` inside table cells now breaks
  8 rows (new defect class).
- `D版送付用資料(追加)/M要件-266D_…/001-読合せ計画.md`: a 44 KB cross-page duplicate
  of page 002, truncated mid-cell with unclosed HTML, shipped at `judge_score: 40`.
- Every lead paragraph in that document was deleted (`014` prose 1500 -> 72 chars).

## 4. Why the pipeline cannot delete noise today (read these first)

- `graph/fast/repair.py:1471` builds `required_tokens = _required_fact_tokens(
  owned_source, policy)` and `:638` rejects any candidate missing one. Degenerate
  runs qualify as "identifiers", so OCR garbage becomes protected content.
- `graph/fast/repair.py:660-665` collects `_loop_fragment` values present in the
  source into `source_loops` and only rejects fragments **absent** from it. A
  source-backed loop can never be a mechanical defect; deleting one can cost an
  attempt. The incentive is strictly "copy it".
- `graph/fast/wiki.py:57` — 「中国語簡体字、…%%、無限反復、同じ文・語・見出しの不自然な
  反復、…を報告する。**原文に本当にある必要な反復は欠陥にしない。**」 The clause exists
  to protect 「K K K」, 「#REF!」, 「99:99」, repeated 「## 欠番」 and the one-heading-per-image
  「## 原文の図・画像」 convention. It also shields every parser loop.
- `graph/fast/wiki.py:38-43` ("修復は要約ではない … 一件ずつ照合する") makes collapsing
  k identical source lines read as 欠落. Coverage scoring wins: measured `judge_score:
  100` on pages with triple-duplicated prose.
- `MAX_REWRITE_ATTEMPTS = 4` (`graph/fast/repair.py:37`) plus the fallback to the
  original text means a page whose noise removal costs 4 attempts ships as the noisy
  original, with the failure invisible except in `state/pages/NNN.json`.

## 5. What each role sees right now (accurate as of HEAD)

- Repair judge and repair writer: **only** the owned line ranges plus explicitly
  permitted reference ranges, rendered with line numbers
  (`graph/fast/repair.py:206-211` `_numbered_ranges`; inserted at `:447-448` for the
  judge and `:557-558` for the writer). Ranges come from
  `metadata/state/<doc>/state/pages/NNN.json` keys `source_ranges` /
  `reference_ranges`. No neighbour lines at all. Some owned ranges are nearly
  empty (page 017 of `#33136_…_r3.pdf` owns line 1862 only — a blank line), so the
  role is asked to repair text it cannot place.
- Wiki (generation) writer: `SectionSession.context`
  (`graph/fast/writer.py:26-45`, prompt at `graph/fast/writer.py:47-62` via
  `graph/wiki/prompts.py:420-472`), filled by `context_block()` in
  `graph/formats/context.py:78-95`. That context is **hierarchy path, parent
  summary, sibling titles with `summary[:120]`, previous and next page title +
  summary** — page metadata, never source text.
- Standard (non-fast) policy already has real overlap:
  `graph/wiki/windows.py:37-45` `overlapping_windows(target=250, overlap=50)`, used
  with `window_inventory_prompt` (`graph/wiki/prompts.py:84`). Fast policy assigns
  disjoint section ranges instead. Look here before inventing a new context window.
- Counterweight already in the prompts: `graph/fast/wiki.py:26` and `:85` —
  「ページ要約、周辺コンテキストを原文扱いせず」. Adding preceding characters to the
  prompt without changing this rule either violates it or silently turns context
  into license to invent.

## 6. Counter-evidence the proposal must weigh

Neighbour context is not free. Two measured regressions look like context bleed:

- `#33136_…_r3.pdf` pages 005/006/007 and 023/025 import each other's content; 173
  of 174 table rows on page 006 are byte-identical duplicates of page 007.
- `M要件-266D_…/001-読合せ計画.md` gained 44 KB because the judge asked for rows that
  legitimately belong to page 002 — the content was real, the placement was wrong.

Any "give the writer the previous 10k chars" design needs a rule for *where a fact
lives*: a page may reference a neighbour to understand a term, but must not
re-own a neighbour's rows. Say explicitly how that is enforced and how it is
checked mechanically, because the judge cannot police it alone.

## 7. Guard rails (from `AGENTS.md`, non-negotiable)

- Frozen data contract: do not rename paths, JSON keys, schema versions, SQLite
  tables, markers, IDs, prompt/version constants, Git refs or GROWI paths. Note that
  caption-ID loss and dropped 節番号 already violate "IDs are not removed" — a
  proposal must add a check, not a migration.
- No bulk migration of existing project data; upgrade artifacts lazily, per
  document, keeping old bytes when a deterministic upgrade would differ.
- HTML tables with `rowspan`/`colspan` are correct output — never rewrite an HTML
  table as markdown. `K K K`, `#REF!`, `99:99`, `0.000` runs, `## 欠番`, and the
  one-heading-per-image convention are real content.
- A phase writes only its own `common.paths.DataLayout` paths; no phase ordering
  inside a phase; no second implementation of sync or publication.
- Verify with:
  `python -m compileall -q common convert wiki linker index publisher runner`
  `python -m unittest discover -s tests`
  `python -m runner.compat_check --project configs/<project>.ini` for real projects.

## 8. Files to read before proposing anything

- `graph/fast/repair.py` — `_numbered_ranges` 206, judge prompt 400-470, writer
  prompt 480-570, `_loop_fragment` 591, `_mechanical_errors` 608-694,
  `_required_fact_tokens` 695+, `_load_document` 1374-1490, `_commit_outcome`
  1494-1560, selection/fallback 800-870, `_promote` 1657.
- `graph/fast/wiki.py` — `generation_prompt_rules` 98, judge rules 26-60, writer
  rules 65-92.
- `graph/fast/writer.py` 26-120, `graph/formats/context.py` 78-95,
  `graph/wiki/prompts.py` 84 / 420-540, `graph/wiki/windows.py` 37-60.
- Artifacts for concrete pages: `page-029/` (vetoed noise removal), `page-097/`
  (4 attempts, no change), `page-001/` of `M要件-266D` (bad judge instruction),
  and any `rewrite-0K-mechanical.json` whose error names a `------` row.

## 9. Expected deliverable

1. Noise taxonomy with detector + owner (convert / repair / judge / report-only),
   each backed by 3 real examples from this corpus.
2. The smallest rule change set that lets the writer collapse source-backed
   duplication without being scored as loss, and that stops `required_tokens` and
   `_loop_fragment` from protecting garbage and table separators.
3. A context design for the three roles: what window, which capacity (evidence vs
   framing), how placement/ownership is enforced, cost in tokens, and what it does
   to `judge_score` semantics.
4. An eval: ~20 hand-picked pages (noise-heavy, table-heavy, image-heavy, empty
   range, HTML-table page) with a rubric, plus the body-repetition and caption-ID
   counts from section 2 as regression gates. No promotion of a document that loses
   caption IDs or gains broken table rows.
5. An explicit "will not do" list.

Do not ship anything to `data/東北中給需給運用計画` (production). Do not re-run repair
on the 18 finished documents while this is being decided; `取引ガイド_ver.9_260401.pdf`
and `要求仕様書` are still pending and are the natural pilot set.
