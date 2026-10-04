# Handoff: plain Markdown + .txt ingest formats

Implementation completed on 2026-10-02. The remainder of this document records the agreed scope,
design, and acceptance checks.

## What I asked for (exact scope)

1. Plain `.txt` and plain `.md` documents are not really supported today. Make them work.
2. `.txt` has no notion of headings/sections, so it must go through the **same pipeline as pdf**
   (observe -> plan -> chunk, the LLM seed-plan path). Not a new pipeline. The same one.
3. `.md` is one of two behaviours, chosen **automatically from the document itself**:
   - properly broken into sections/subsections -> use the **docx** logic (heading tree chunking),
   - otherwise -> use the **txt/pdf** logic.
4. When chunking markdown, these must never be split across pages: images, fenced code blocks,
   highlight/quote blocks, markdown tables, html tables.
5. Share as much existing logic as possible. Keep the diff minimal.
6. **NO NEW CONFIG KNOBS.** No `md_use_headings`, no env var, no INI key, no new Settings field,
   no new WikiConfig field. The structurally-unstructured decision is computed from the document,
   always on. Do not add a flag "for flexibility".

## The zero-knobs plan (agreed)

- **Txt**: `kind_of` alias `"txt": "pdf"`. One word. Txt then runs the exact pdf pipeline
  (headings not trusted -> observe/plan/chunk). Plus 3 lines so `.txt` is scanned and copied as
  text instead of being sent to the doc-parser.
- **Markdown**: new `graph/formats/md.py`, no config gate. It tries the heading tree; if the doc is
  genuinely sectioned -> docx chunking; if not -> return `None` -> pdf/LLM path. Decision computed
  from the doc, always on.
- **Reuse**: pdf's "are these headings real?" test moves to `tree.py`, both call it. Md just allows
  deeper nesting and ignores a leading `# Title` (without that, normal markdown fails the test;
  measured on this repo's data, see Evidence).
- **Not broken apart**: code fences, pipe tables, images already protected. Multi-line HTML tables
  and `>` quote blocks are NOT. Two small scanners added to the block index, and every chunker
  picks it up from that one shared index.
- **Incremental edits**: `_structural_shape` learns `md` (3 lines) so a restructured markdown
  rebuilds instead of drifting page titles. Txt already works because it is kind `pdf`.
- Config files untouched (`graph/config.py`, `graph/wiki/config.py`, `graph/workspace/writer.py`
  need no edit). ~1 new source file, ~80 lines total.

## Files involved (verified)

Format dispatch / planners:
- `graph/formats/__init__.py:8` `KINDS`, `graph/formats/__init__.py:12` `kind_of` (has the
  `aliases = {"xlsm": "xlsx", "xls": "xlsx", "doc": "docx"}` dict to extend with `"txt": "pdf"`),
  `graph/formats/__init__.py:30` `structural_seed_plan` (dispatches docx/pptx/pdf; everything else
  falls to `return None` at line 48 = the LLM path).
- `graph/formats/docx.py:13` `plan` = `heading_tree` + `pages_for`. The docx logic to reuse.
- `graph/formats/pdf.py:10` `plan` = reject headings unless trustworthy (`pdf_use_headings` gate at
  line 11, usability test at line 19, `_count`/`_depth` helpers at lines 26-31), then delegate to
  `docx.plan`. Move that test to `tree.py`, keep pdf's numbers so pdf behaviour is byte-identical.
- `graph/formats/tree.py:28` `heading_tree` (raises `ValueError` on an unclosed fence; skips lines
  inside fences), `graph/formats/tree.py:76` `divisor_split`, `graph/formats/tree.py:95` `pages_for`,
  `graph/formats/tree.py:122` `_pack`. Put the shared usability helper here.
- New: `graph/formats/md.py`.

Where the plan is consumed / fallback lives:
- `graph/wiki/pipeline.py:1489-1530` calls `structural_seed_plan`, validates it, otherwise runs
  `observe_document` -> `build_seed_plan`. A `None` return IS the pdf path; no edit needed here.
- `graph/wiki/document_map.py:148` `validate_seed_plan` (contiguous 1..N partition, snaps cuts to
  atomic-block boundaries, merges when a block cannot be split).
- `graph/wiki/page.py:55` `split_sections` (writer-level sections, same index).
- `graph/wiki/windows.py:37` `overlapping_windows` (observation windows, transport-only, deliberately
  allowed to overlap/nest).

Block integrity (the "never break it" part):
- `graph/wiki/markdown_blocks.py:159` `_scan_table_blocks` (GFM pipe rows only today),
  `graph/wiki/markdown_blocks.py:177` `_scan_fence_blocks` (``` and ~~~, already atomic),
  `graph/wiki/markdown_blocks.py:131` `_scan_image_unit_blocks` (shows the char-offset -> line-number
  mapping pattern to copy for html tables, via `find_images`, `graph/common/images.py:130`),
  `graph/wiki/markdown_blocks.py:26` `BlockKind` literal, `graph/wiki/markdown_blocks.py:190`
  `build_block_index`, `graph/wiki/markdown_blocks.py:208` `atomic_windows`.
- `graph/common/markdown.py:68` `is_tableish_line`, `graph/common/markdown.py:40`
  `scan_markdown_fences`.
- New scanners here: closed `<table ...> ... </table>` spans, and contiguous `>` quote runs.

Txt ingest (the 3 lines):
- `publisher/scanner.py:12` `SUPPORTED` has no `.txt`, so sync/watch skips txt silently.
- `publisher/pipeline.py:318` `_parse` short-circuits only `item.parser == "md"` (verbatim read);
  `publisher/pipeline.py:927` has the same md-only check for the parse-size guard.
- `graph/workspace/convert.py:32` `if path.suffix.lower() == ".md"` -> verbatim, else parser call.
- Put one shared `VERBATIM = {".md", ".txt"}` in `graph/workspace/project.py` (lowest layer, already
  imported by both sides) instead of repeating the literal.
- `graph/workspace/project.py:18` `raw_name_for` already turns `notes.txt` into `notes_txt.md`, so no
  naming change is needed; the wiki folder becomes `notes.txt`, consistent with `spec.pdf`/`a.docx`.

Incremental update tiers:
- `graph/wiki/incremental.py:218` `_structural_shape` returns `None` for anything not in
  `{"docx", "pdf"}`; add `md`. Called from `decide_update` at `graph/wiki/incremental.py:339-346`
  (gate: only applies when the stored plan already equals the heading planner shape, so LLM-built
  documents are untouched).
- `graph/workspace/writer.py:366-370` and `graph/workspace/writer.py:497-514` pass the kind through;
  no edit needed.

## Evidence (measured, not guessed)

- `build_block_index` on a synthetic markdown doc: cuts before every line inside a multi-line HTML
  `<table>` and inside a multi-line `>` quote report `cut_is_safe == True`. Fences (14-17), the pipe
  table (19-21) and the markdown image (23) are correctly atomic. So html tables + quotes are the
  only real integrity gaps.
- Shape test on `data/**/raw/*.md` (8 files, >=20 lines): 4 of 8 start with a single `# Title`
  wrapper, so the biggest root child covers the whole doc and pdf's `share > 0.6` rule rejects well
  structured markdown (share 1.00). Collapsing a single full-height title child first drops those
  docs to 0.19-0.35, i.e. pass. That is why md needs `collapse_title` and pdf must keep it off.
- `pages_for` on the same corpus produced no oversize pages except one on a 617-line pdf raw, so the
  docx chunker behaves on markdown-shaped input.

## Acceptance checks

- New small assert-based test (no fixtures, no framework beyond the existing `unittest` style):
  structured markdown -> structural plan with `chapter`/`path` set; heading-less markdown -> `None`;
  a `.txt` document -> `None`; `build_block_index` finds no safe cut inside an html table, a `>`
  quote block, a fence and a pipe table.
- Run `python -m pytest tests/test_update_tiers.py tests/test_mount_diff_pipeline.py` (the `kind="md"`
  cases there use heading-less `line N` fixtures, so `_structural_shape` must still return `None` for
  them; pdf/docx numbers must not move).
- Real smoke: `build wiki` on `data/diff_test/raw/robot_hookup_v2.md` (74 lines, 8 headings, starts
  with `# Title`) -> expect the heading/chapter plan, not the LLM plan.

## Do not

- Do not add a setting/env/INI/Settings/WikiConfig field for any of this.
- Do not change pdf or docx thresholds; pdf keeps its exact numbers and `collapse_title` off.
- Do not touch `graph/wiki/legacy.py:360` (the old `--mode chunks` chunker has its own duplicated
  scanner; out of scope unless asked).
- Do not make an unclosed `<table>` raise. Keep only closed pairs atomic, otherwise existing
  pdf/docx documents with an unpaired table tag start hard-failing.
