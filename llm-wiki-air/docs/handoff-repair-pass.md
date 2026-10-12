# Handoff: a `--repair` pass for existing wiki pages

For whoever implements this. Plain requirements, no code written yet.

Reviewed project: `data/東北中給需給運用計画_test/wiki` — 1352 pages, 941 MB,
20 source documents. Read-only review by four readers plus corpus scanners.
No wiki file was changed.

---

## 1. What went wrong in this run

Structure is fine. Content that came from images, screens and OCR is bad.

- Every page has one H1. Nav links work. Only 2 pages are almost empty.
- Page scores by document: 8/10 for B1 機能仕様書, but 2-5/10 for
  画面・印字仕様書 and for the re-chunked docs in `D版送付用資料(追加)`.

Real problems (path is relative to `data/東北中給需給運用計画_test/wiki/`):

| Problem | Example you can check | Why it happened |
|---|---|---|
| Made-up table headers | `B1/…年間需給運用計画 画面・印字仕様書.pdf/097-第4.11-系統制約設定エクセル(系統制約設定).md:116` has `Station ID / Status / Energy Consumption (kWh)` over rows that are 系統制約 上限/下限. Also `:68` `Vessel`, `:66` `Temperature / Wind Power`. 107 such tables in 90 of 175 files | the vision step writes a generic English schema instead of the real column names |
| Wrong topic in a page | `D版送付用資料(追加)/#33136…_r4.pdf/002-職務経歴書・自己PRの書き方.md:1,60-89` is about job application papers. `…/016-鉄道安全・原子力発電技術資料….md:3` is about railways | a junk image was described and that description became the page text |
| Made-up acronym | `要求仕様書/…/001-文書ヘッダ・変更履歴・目的.md:111` says `TSO（Total Solar Onshore）` | model guessed |
| Internal labels as page titles | `D版送付用資料/#33136…_r3.pdf/010-【要再確認】原文範囲-801-1050行(1).md:1` | chunk-window name used as title |
| Model refusal printed in the page | `D版送付用資料(追加)/M要件-266D…/005-日毎算出.md:5` and `…/007-運用計画諸元設定.md:5` say `（原文に内容が存在しないため、出力することができません。）` | nothing blocks refusal text |
| Endless repetition | `…_r3.pdf/027-….md:10-160` has 113 lines of `%% …`, including `%% Initial Plan Details` 17 times in a row. `…_r4.pdf/003-….md:161` repeats `of the result` 93 times. `要求仕様書/…/020-….md:41-121` has 66 `%%` lines | the OCR/diagram text already loops, and fences are copied as-is |
| Same block pasted many times in one page | the legend `〔データタイプ/桁数〕` appears 4 times in `B1/…年間画面…/015-….md:22,36,50,64` and 6 times in `B1/週間・翌日・当日…/055-….md:9,23,37,51,65,79`. `改 定 欄` + the same table header 29 times in `…/173-改定欄-副番AE-AS….md` | page fragments were joined with no de-dup |
| One table cut into many blocks | `B1/…年間画面…/131-….md:9,17,28,35` — one No.0-51 table split into 8 blocks, each repeating the header | tables split at page-fragment edges |
| Table structure broken | `B1/週間機能…/134-….md:16` shows `| colspan="3">無効 |` — an HTML attribute left inside a markdown cell. `要求仕様書/…/051-….md:26` is one 30,367-char line with 1,810 empty `<td></td>` | half-converted tables, and empty cells never removed |
| Wrong Chinese characters | `取引ガイド/188-….md:108` `低压` (should be 低圧), `…/069-….md:11` `入カ` for 入力, `B1/…年間画面…/120-….md:112` `其他` for その他, `B1/週間機能…/017-….md:38` `单位` | OCR model output, never checked |
| Wrong parent page | 12 files in `B1/…年間画面….pdf/` (013-019) say `親:` is `020-第4.5.4-…`, a later sibling | parent was defaulted, not resolved |
| Huge pages | largest page is 4.2 MB: `D版送付用資料(追加)/#33136…_r4.pdf/016-….md` | base64 images inside the markdown body |

Why nothing was caught: the judge only checked for **missing** information and
was told to ignore repeated sentences. So adding junk, repeating, or wrong
language could never fail a page.

---

## 2. NOT problems — do not "fix" these

A repair pass that "fixes" these destroys real information.

- **HTML tables are correct.** `B1/…週間需給運用計画 機能仕様書.pdf/061-第9章-9.8-調整見込み量作成.md` keeps 6 `rowspan` and 4 `colspan`. Markdown pipe tables cannot hold merged cells, so HTML is the right choice. Rule: emit HTML tables; use a markdown pipe table only for a small flat grid with no merges. **Never** rewrite an existing HTML table as markdown.
- `K K K`, `KKKKKKKKK` — redacted names in the source. Keep. (Only the
  inconsistent length is cosmetic.)
- `#REF!`, `99:99`, `0.000` runs — real cell values or errors from the source
  spreadsheet. Keep them as they are.
- `text_image`, `<nl>`, `<fcel>`, `<lcel>`, `%%` inside code fences, `⚫`
  bullets — these come from the converted source. The writer must copy fences
  and rows unchanged, so it cannot remove them. Fix them in `convert`, or leave
  them and only report.
- `## 欠番` repeated — 欠番 means a deliberate gap in numbering. Faithful.
- `## 原文の図・画像` repeated — one heading per image. That is the convention.
  Only report when the heading is there **and** no image follows.
- LaTeX like `$\Sigma$`, `$\bigcirc$`, and `<sup>1</sup>` footnotes — intended.
- `&lt;xcal:date-time&gt;` in a header — an XML field name, correctly escaped.
- Mixed `・` and `·`, `_` in titles, bold vs plain captions, some headings
  numbered and some not — cosmetic. Ignore.
- Reading order not strictly increasing — the source itself is not always
  monotonic. Only check section numbers against the source ranges.
- Two folders for the same PDF (`D版送付用資料/M要件-266D…` and
  `D版送付用資料(追加)/M要件-266D…`, 69% same words; `#33136` r3 vs r4, 78%
  same) — this is a duplicate **input** problem. A page repair cannot fix it.
  Report it.
- Truncated `<image-description>` text — known bug, out of scope.

---

## 3. What already exists (use it)

- Page → source lines: `metadata/state/<doc>/state/pages/NNN.json` holds
  `source_ranges` (lines the page owns), `reference_ranges` (lines it borrows),
  `provenance{snapshot_path, sha256}`, `title`, `filename`, `judge_score`,
  `missing_important_information`, `defects` (added 2026-10-07),
  `verbatim_sections`.
- Source text with the same line numbers: `metadata/state/<doc>/source/original.md`.
  Check it against `provenance.source_document.sha256`. If the sha differs, the
  source changed → run a normal `sync`, not a repair.
- Old prompts and verdicts: `metadata/state/<doc>/work/page-NNN/`
  (`section-01-attempt-01.md`, `section-01-judge-01.json`).
- Write loop: `_write_section` in `graph/wiki/pipeline.py:1270`; mechanical
  checks `check_section` and `quality_defects` in `graph/wiki/page.py`; judge
  prompt `page_judge_prompt` in `graph/wiki/prompts.py:325`; safe fallback
  `_verbatim_section` in `pipeline.py:1233`; report file `wiki/_review.md`.
- Small in-place edits: `incremental_page_edit_prompt` (`prompts.py:489`) and
  `IncrementalPageEditResult` (`wire.py:109`).
- Policies: `common/policy.py` (`strict_judge`, `offline`, `cache_key`).

---

## 4. What to build

```
python -m runner.cli sync --repair [mount-rel ...]
```

1. Work on one page at a time. Give the model the page plus its
   `source_ranges` **and** `reference_ranges` — the same window the judge uses
   today. Do not give it the whole document: page 104 repeats page 102's table
   (`B1/週間・翌日・当日…/104-….md:136-156`) because a neighbour was in context.
2. Never add content from a range another page owns.
3. Keep `filename`, page number, H1 and the nav footer unchanged. The filename
   is a GROWI path. Title fixes need a separate flag.
4. Two steps, cheapest first:
   a. no model: de-dup repeated blocks, merge repeated contentless headings,
      fix half-converted cells, drop empty `<td>` runs;
   b. one rewrite for pages that fail any hard rule, then judge again, max 2
      tries, then fall back to the source section and list the page in
      `_review.md`.
5. Do not lose anything. After a repair, run `check_section` (all table rows,
   fences verbatim, image tokens once, all code tokens present) and
   `quality_defects`. A prettier page that lost a token is rejected.
6. Never blame the writer for the source. `quality_defects(draft,
   source_text=…)` already skips repeats, headings and markers that the source
   also has. Keep that rule, or the repair loops and every attempt fails.
7. Write back `content_sha256`, `defects`, `judge_score` and a new tag
   `rewrite_version="wiki-repair-ja-1"` into `state/pages/NNN.json`. Then let
   index and publisher see the change.
8. Never run by itself. AGENTS.md forbids bulk migration, and an unchanged
   project must stay a no-op (`runner/compat_check.py`).
9. Use `policy_of(config)` and `cache_key`, so a `--fast` project stays fast.
10. Reuse existing image tokens. Do not paste base64 into a rewritten page.
11. Cost: about 1 judge call per page, 1-2 rewrite calls for failures. 1352
    pages ≈ 3-4k calls. Add `--limit` and skip pages already clean at this
    repair version.

---

## 5. Judge checklist

**Must fix (hard)**
- Any source row, fence or code token is missing.
- A table header, unit, number, label or acronym expansion is not in the source.
- Page text is about something outside its own source ranges.
- Refusal text, or a one-line stub standing in for real content.
- Repeated line or in-line endless repeat, when the source does not repeat it.
- Same heading 3+ times and no content under it.
- Wrong-script characters: `CHINESE_ONLY_GLYPHS` in `graph/wiki/page.py`
  (值 压 单 项 关 变 应 杀 酱 查 运 总 发 图 录). Each is a different codepoint
  from the Japanese form, so correct Japanese never matches. Verified:
  `候補 低圧 数値 単位 図表` clean, `低压` flagged.
- Table cell count that does not match its header row; HTML attributes leaking
  into a markdown cell; merged cells replaced by a flattened pipe table.
- Nav `親` pointing at a later sibling or a page that is not the parent.

**Should fix (soft, report only)**
- Generic headings like `### 全体的な構成`; repeated intro sentences.
- Tables split into many blocks that repeat the header.
- Page over a size budget; junk markers that came from the source.

**Ignore** — everything in section 2.

---

## 6. Verify your work

- `python -m compileall -q common convert wiki linker index publisher runner graph`
- `python -m unittest discover -s tests` — slow (many minutes), not hung.
- `python -m runner.compat_check --project configs/<project>.ini` before deploy.
- Add `tools/wiki_quality_report.py`: run `quality_defects()` over a project
  with the real `source_text` for each page and print counts per class. Use it
  as the before/after number. Precompute per document; scanning a 137 MB source
  per page takes minutes.
- Test fixtures: pick 6 pages from section 1, one per problem class.

---

## 7. Already changed today (uncommitted)

`graph/wiki/{config,page,pipeline,prompts,wire}.py` and new
`tests/test_wiki_quality_checks.py` (`.gitignore` has `tests/*`, so whitelist it
like the other tests if it should be tracked). `AGENTS.md` and
`graph/fast/inline_linker.py` were already dirty before this work.

- `wire.py`: `PageJudgeResult.defects` list (additive; `coverage_score` rules
  unchanged).
- `prompts.py`: `QUALITY_RULES` added to the writer, and the judge now reports
  defects: repeats, made-up content, wrong language, junk, empty headings,
  off-topic. It is told that source-faithful repeats, table rows, identifier
  spelling and fence content are **not** defects.
- `pipeline.py`: judge defects drive the retry, defect-free candidates win,
  `strict_judge` blocks publish, and defects are saved per page under a new
  `defects` key.
- `page.py`: `quality_defects()`, `repeated_loop_fragment()`, `authored_prose()`,
  called from `check_section(draft, source_text=…)`.
- `config.py`: `REWRITE_PROMPT_VERSION` `wiki-sections-ja-4` → `-ja-5`.

Checked: `compileall` clean, `tests/test_wiki_quality_checks.py` (7 cases) plus
`test_md_txt_formats` and `test_update_tiers` pass. Full `unittest discover` was
not run to completion.

**One risk to decide:** because of the version bump, the next `sync` rewrites
every page of every project. That re-applies the new rules to the 1352 bad
pages, but it costs a full regeneration and changes healthy projects too. Keep
the bump, or gate it behind `--force` / `--repair`.

---

## 8. Open questions

1. Keep the version bump, or gate it?
2. May `--repair` clean up junk that came from `convert` inside copied fences
   and tables, or only report it?
3. May it fix page titles and nav parents, given GROWI paths?
4. Which docs first? Worst scores: `D版送付用資料(追加)/M要件-266D` (2),
   `D版送付用資料(追加)/#33136…_r4` (3), `#33136…_r3` (4), B1 年間 画面・印字
   (5), B1 月間 画面・印字 (5).
