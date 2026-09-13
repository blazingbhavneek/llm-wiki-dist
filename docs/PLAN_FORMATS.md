# Plan — format-aware chunking: docx · pptx · xlsx · csv · pdf

**Status: hard plan (2026-09-13), written against `neo-hardcoded` at `361fdbb`.** Companion
to `PLAN_GROWI.md` (which decides *where* pages live and how they are indexed) and
`PLAN_NEO.md` (the `wiki` writer). This document decides **how a document is cut into pages
per format, what context the writer sees, and how tables are written, indexed and queried.**

Written for an implementer working one work package at a time. Where it gives code, type
that code. If a snippet does not fit the real file, stop and report the exact line.

**Depends on:** nothing in `PLAN_GROWI.md` — every seam here exists today. Where a step
differs once `PLAN_GROWI` WP-G8 has landed (index fed from GROWI instead of `_planning/`),
it says so inline; the design puts everything the indexer needs *inside the page*, so both
indexers work.

---

## What you asked for, restated

1. **docx** — cut by the heading tree, top-down: a section ≤ 250 lines is a page; a bigger
   one descends into its subsections; a last-level section that is still too big is cut
   into `d = 2, 3, 4…` near-equal pieces until each is ≤ 250 (270 → 135+135; 700 → 234×3).
   Consecutive small siblings are packed together (never across parents). The writer and
   the linker see the **parent chain and the summaries of the sibling pages**.
2. **pptx** — slides are the atoms; the delimiter phrase is configurable (your work PC
   differs); title/divider slides are detected cheaply and **one** LLM call per deck decides
   the sections; groups never cut a slide.
3. **xlsx / csv** — never split a table into partial pages. Page 1 = the full table, verbatim.
   Then analysis pages. **The LLM decides the structure** (which regions are tables, which
   rows/columns are headers, orientation), not a rows-vs-columns heuristic — sheets have
   margins, gaps, multi-row headers and several tables per sheet. Tables are indexed as
   records and answered by a tabular subagent with a query tool.
4. **pdf** — no trustworthy headings; keep today's 250-line observation windows.
5. **One file per format** under `graph/formats/` so the logic is editable in isolation.

---

## Part 0 — What exists (verified)

### 0.1 What the parser gives us (`/mnt/common/Code/doc-parser/formats/*.py`)

| Format | Output shape | Verified on |
|---|---|---|
| docx (`docx.py`, pandoc `--to=gfm`) | ATX headings `#`/`##`/`###`…, GFM tables, `<image-unit>` blocks. Real hierarchy: `# CHAPTER 3`, `## B. MEETING OF CREDITORS`, `### 7. CONDUCTING THE MEETING`. Front matter (cover/TOC) before the first real heading; an empty `#  ` heading at line 330. | `data/raw/docx/docx_handbook_872p_docx.md` (9,577 lines) |
| pptx (`pptx.py`) | `# Presentation`, then `## Slide N` per slide; `### <title>` **only when the slide has a title placeholder** (1 of 26 slides in `presentation-2`); body lines, GFM tables, `<image-unit>`s, `> **Speaker notes:**`. Line counts are dominated by image units (slide 2 = 41 lines, 2 of them text). | `data/raw/pptx/*.md` |
| xlsx (`xlsx.py`) | `# Excel workbook`, then per sheet `## Sheet: <name>` + one HTML `<table>`: header row `Row, A, B, …`, one `<tr>` per row with the row number first, merged cells as `colspan`/`rowspan` **on the anchor only** (covered cells omitted), formulas rendered with cached values, `### Images` list. Sheets over `XLSX_MAX_TABLE_CELLS` (50k) become a sparse `Cell | Content` table. | `data/raw/xlsx/xlsx_financial_analyses_xlsx.md` (5 sheets, 137 rows) |
| csv (`csv.py`) | `# <name>` + one GFM table, ≤ `CSV_MAX_ROWS` (5,000) data rows, a truncation note after. | `data/raw/csv/csv_long_lines_csv.md` |
| pdf (`pdf.py`, MinerU) | Markdown with headings of uneven quality, images, tables. | `data/raw/pdf/oneapi_optimizations_pdf.md` (18,013 lines) |

### 0.2 The current cut (the "250 lines" logic)

`wiki` mode, `graph/wiki/pipeline.py:run_pipeline` (1344–1370): unless `state/plan.json`
resumes, Phase 1 `observe_document` inventories every 250-line window with 50-line
overlap (`windows.py:overlapping_windows`, one LLM call per window), Phase 2
`build_seed_plan` (`document_map.py`) turns the inventories into a `CompiledSeedPlan` —
`pages: [SeedRange(title, summary, chapter, source_start, source_end)]` — validated by
`validate_seed_plan` (120–250): ordered, contiguous, tiles `1..N`, every page titled *and
summarised*, cuts never inside a fence/table/`<image-unit>` (`markdown_blocks.py:BlockIndex`),
pages over `2 × page_target_lines` split at headings. Phase 3 (`_rewrite_all`) only needs
that plan: sections ≤ 80 lines, references = prev/next + 3 lexical (`_select_references`),
judge, intro, links.

**That is the seam.** A structural planner that returns the same `CompiledSeedPlan` (and
passes the same validator) replaces Phase 1–2 with zero LLM calls; Phase 3 is untouched.

A table is one atomic block, so an xlsx sheet becomes one 5,000-line section and the section
writer either times out or falls back to verbatim. Tables need their own writer.

### 0.3 Found while measuring — a one-line bloat fix (WP-F0)

`data/graph.sqlite` is 400 MB for **132 nodes**: node bodies hold 60 MB of inline base64
images (one body is 11 MB), and `GraphStore._reindex_fts` (`store.py:1404`) indexes the raw
body, so FTS5 tokenises 60 MB of base64 too (`nodes_fts_data` = 39k rows). Embeddings and
search items already strip images (`gateway._embed_safe_text`, `librarian.py:2347`); FTS is
the only path that does not. Vectors are 7 MB (6,967 search-item vectors × 256 dims).

---

## Part 1 — Design

### 1.1 The folder

```
graph/formats/
  __init__.py     kind_of(document_name) → "docx"|"pptx"|"xlsx"|"csv"|"pdf"|"md"; dispatch table
  tree.py         shared: heading tree, divisor split, sibling packing, safe cuts, lead()
  context.py      hierarchy summaries (one LLM call per parent) + the 文脈 block for the writer
  docx.py         plan(lines, …) → CompiledSeedPlan            (heading tree)
  pptx.py         plan(lines, …) → CompiledSeedPlan            (slides, delimiter, one judge call)
  pdf.py          plan(lines, …) → None                        (fallback to Phase 1–2; optional heading heuristic)
  tabular.py      grid parsing, regions, LLM structure, records, stats, page rendering, query tool
  xlsx.py         run(source, run_dir, …) — sheet loop over tabular.py
  csv.py          run(source, run_dir, …) — one table over tabular.py
```

Each `*.py` owns exactly one format's logic and imports only `tree.py` / `tabular.py` /
`graph.wiki.*`. Editing docx cutting means editing `docx.py` and nothing else.

### 1.2 The two writers

```
raw/<team>/<name>_<kind>.md
        │ kind_of()
        ├── docx / pptx / pdf / md ──► wiki writer (graph/wiki/pipeline.py)
        │       structural_seed_plan(kind) ──► CompiledSeedPlan  (docx, pptx; pdf → None → Phase 1–2 as today)
        │       summarize_hierarchy()      ──► page.summary + state/context.json
        │       Phase 3 rewrite with the 文脈 block
        └── xlsx / csv ──────────────► tabular writer (graph/formats/tabular.py)
                grid → regions → LLM structure → records/stats → 001 table page + analysis pages
```

Both write the `docs/` + `_planning/` layout `writers.build_wiki_output` already returns, so
`write_wiki`, `publish_output`, `sync_raw`, GROWI publish and the zip are unchanged.

### 1.3 Hierarchy context (docx, pptx)

Every seed page carries `path: ["CHAPTER 3 …", "B. MEETING OF CREDITORS", "7. CONDUCTING …"]`.
After the plan and before Phase 3, **one structured LLM call per parent** reads the parent
chain plus each child page's title and lead (first ~6 lines) and returns a one-line summary
per page and a paragraph for the parent. Phase 3 prompts then get:

```
## 文脈
階層: CHAPTER 3 – ADMINISTRATION OF CHAPTER 13 CASES › B. MEETING OF CREDITORS
親セクションの要約: …
同じ親の他のページ:
- 1. PRESIDING OFFICER — …
- 2. SCHEDULING, NOTICING … — …
前のページ: 6. OATH — …   次のページ: 8. QUESTIONS — …
```

Same-parent siblings become reference candidates before the lexical picks, `chapter`
becomes the joined chain (so coverage/cluster carry it), the nav footer gets a 親 link, and
`index.md` nests by path. The handbook has ~40 parents → ~40 tiny calls, not 400.

### 1.4 Tables

| Step | Who | What |
|---|---|---|
| grid | Python | parse the parser's `<table>` (HTML, spans expanded) or GFM table into `{(row, col): text}` with the sheet's own row numbers / column letters |
| regions | Python | split the used range into islands separated by empty rows/columns; tiny islands (≤ 3 cells) near a bigger one are captions |
| **structure** | **LLM, one call per sheet** | given every region's preview (≤ 12×12 cells + last 2 rows), return per table: title, orientation, header rows, label columns, data range, notes; Python validates ranges and retries with the error; heuristic fallback only after two failures |
| records | Python | header names from the header rows (multi-row joined with ` / `, merged headers forward-filled); one record per data row (or column), keyed `行 12` / `列 D` |
| stats | Python | per column: count, min, max, mean, sum for numerics; distinct values (≤ 20) for text; extreme rows |
| pages | LLM + Python | `001-<sheet>` = overview + header definitions + stats + **verbatim full table**; `NNN-<table>-分析` = LLM interpretation of the stats and extreme rows; optional `NNN-<table>-<i>` slices of ≤ 40 records; every 行/列 citation mechanically checked |
| index | Python | `NodeType.table`; search items = one `record` per row (no 3,000/512-char chunks); big `<table>` blocks stripped from prompts, embeddings and FTS everywhere (`strip_big_tables`); the spec travels inside the page (`<!-- table-spec … -->`) so any indexer can rebuild records from the page alone |
| query | LLM + sqlite | `query_table(node_id, sql)`: records → in-memory sqlite, `set_authorizer` allows SELECT only, `LIMIT 200`; a tabular subagent prompt when the start node is a table |

### 1.5 Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Structural plans produce the **same** `CompiledSeedPlan` and go through the **same** `validate_seed_plan`. | Zero changes in Phase 3; the validator already enforces tiling, atomic blocks and titles. |
| D2 | Target = `WIKI_STRUCTURE_TARGET_LINES` (250) with `WIKI_STRUCTURE_MIN_LINES` (40) for packing. | Your numbers; separate from `window_target_lines` so Phase 1 windows can change independently. |
| D3 | Summaries come from one call per parent, not one per page. | Cheap, and the parent paragraph is what gives the writer "what the topic above is about". |
| D4 | pptx section detection = cheap candidates + one deck-level judge call; no per-slide judge. | One call sees the whole deck; a per-slide judge sees nothing. |
| D5 | Table structure is decided by the LLM per sheet; Python only validates and falls back. | Sheets have gaps, margins, multi-row headers, several tables — a shape heuristic is wrong on real files (you said so). |
| D6 | The full table is published once, verbatim, on the sheet page; nothing else ever contains it whole. | Partial tables are useless to read; whole tables are poison in prompts/embeddings/FTS. |
| D7 | Records live in `search_items` (field `record`) and the query tool re-parses the page body; no new rows table. | The body is cached in sqlite anyway; parsing 5k HTML rows is ~100 ms; one less table to keep in sync. |
| D8 | The tabular query tool is SQL over in-memory sqlite with `set_authorizer`. | stdlib, no pandas; the safety check is the engine's, not a regex. |
| D9 | pdf keeps Phase 1–2. `WIKI_PDF_USE_HEADINGS=1` lets the docx planner take a PDF whose headings look sane. | Off by default; MinerU headings are not trustworthy. |
| D10 | Structural planning applies to `wiki` mode only. | It is the mode you run; `chunks`/`pages` keep their own cutters (ceiling noted). |

---

## Part 2 — Hard constraints

- **C1 — Phase 1–2 code untouched.** `windows.py`, `document_map.py`, `markdown_blocks.py`,
  `images.py`, `ids.py`, `storage.py`, `schemas.py`, and the phase-1/2 prompt builders change
  by zero lines. `validate_seed_plan` is *called*, not edited.
- **C2 — Phase 3 changes are additive:** new keyword arguments with defaults that leave the
  prompts byte-identical when the 文脈 block is empty.
- **C3 — Every seed plan passes `validate_seed_plan`**; a planner that cannot (malformed
  headings, unclosed fences) returns `None` and Phase 1–2 run as today. Never a half plan.
- **C4 — Never lose a row.** A table page carries the parser's table verbatim; records are
  derived, never edited; the analysis pages only cite.
- **C5 — Doc-parser untouched.** Delimiters are read on our side (`WIKI_SLIDE_DELIMITER`).
- **C6 — Tests green before every commit** (`for f in tests/test_*.py; do …` as in `PLAN_GROWI.md`).
- **C7 — Type the code as given.**
- **C8 — Commit per package:** `formats WP-FN: <goal line>`.

---

## Part 3 — Work packages

### WP-F0 — Stop indexing base64 in FTS (found in 0.3)

**Files:** `graph/store.py`, `tests/test_neighborhood.py` (or a new tiny test).

1. `store.py` top: `from .core import strip_image_media` (check the existing `from .core import …`
   line and add the name there).
2. `_reindex_fts` (1404): `node.body` → `strip_image_media(node.body)`.
3. Bump `SEARCH_INDEX_VERSION` in `librarian.py` (grep it) so `bootstrap` rebuilds; add a
   test: upsert a node whose body contains `<image-unit>…data:image/png;base64,AAAA…</image-unit>`
   and assert `keyword_search("AAAA")` is empty while the description text is found.

**Verify:** after bootstrap on a copy of `data/graph.sqlite`, `VACUUM;` → file size drops
from 400 MB to well under 100 MB; `curl …/api/search?q=<word>` unchanged.

---

### WP-F1 — `graph/formats/` skeleton, kind detection, the seam

**Goal:** the pipeline asks the format package for a plan before running Phase 1–2.

**Files:** new `graph/formats/__init__.py`, `graph/wiki/config.py`, `graph/wiki/wire.py`,
`graph/wiki/pipeline.py`, `graph/writers.py`, `graph/core.py`, `tests/test_formats_kind.py`.

1. `graph/formats/__init__.py`:

   ```python
   """One module per source format. Everything here returns plain data for graph.wiki."""

   from __future__ import annotations

   from pathlib import PurePosixPath
   from typing import Any

   KINDS = {"docx", "pptx", "xlsx", "csv", "pdf", "md"}
   TABULAR = {"xlsx", "csv"}


   def kind_of(document_name: str) -> str:
       """`team/x_docx.md` → `docx`; anything unrecognised → `md`."""
       stem = PurePosixPath(document_name).stem
       _base, sep, ext = stem.rpartition("_")
       ext = ext.lower()
       return ext if sep and ext in KINDS else "md"


   def is_tabular(kind: str) -> bool:
       return kind in TABULAR


   async def structural_seed_plan(lines: list[str], *, kind: str, config: Any, model: Any, on_progress=None, stop_check=None):
       """A CompiledSeedPlan for formats with structure, else None (Phase 1–2 run)."""
       if kind == "docx":
           from . import docx

           return docx.plan(lines, config=config)
       if kind == "pptx":
           from . import pptx

           return await pptx.plan(lines, config=config, model=model, on_progress=on_progress, stop_check=stop_check)
       if kind == "pdf":
           from . import pdf

           return pdf.plan(lines, config=config)
       return None
   ```

2. `graph/wiki/config.py` `WikiConfig`: add

   ```python
       # --- structural planning (graph/formats) ---------------------------------
       source_kind: str = "md"              # docx | pptx | xlsx | csv | pdf | md
       structure_target_lines: int = 250    # a page from the heading tree / slide groups
       structure_min_lines: int = 40        # pack consecutive siblings below this
       slide_delimiter: str = r"^## Slide (\d+)\s*$"
       slide_title: str = r"^### (.+?)\s*$"
       pdf_use_headings: bool = False
   ```

3. `graph/wiki/wire.py` `SeedRange`: add `path: list[str] = Field(default_factory=list)`
   (heading chain, top → down; the LLM planner leaves it empty). `schemas.py` is untouched
   (`CompiledSeedPlan.pages` is `list[SeedRange]`).

4. `graph/wiki/pipeline.py`:
   - `SeedPage` (77): add `path: list[str] = field(default_factory=list)` after `reference_ranges`.
   - `_plan_pages` (216): pass `path=list(entry.path)` into `SeedPage(...)`; and set
     `chapter=(entry.chapter or " › ".join(entry.path)).strip()`.
   - `_load_seed_plan` (726–737): add `path=[str(p) for p in item.get("path", [])]`.
   - wherever `plan_json["pages"]` is built (~1354–1373, the dict with `number, title,
     chapter, summary, filename, owner_ranges, …`): add `"path": page.path`.
   - `run_pipeline` (1344–1362): replace the two calls with

     ```python
             from ..formats import structural_seed_plan
             from .document_map import validate_seed_plan

             seed_plan = None
             structural = await structural_seed_plan(
                 lines, kind=config.source_kind, config=config, model=model,
                 on_progress=on_progress, stop_check=stop_check,
             )
             if structural is not None:
                 seed_plan, error = validate_seed_plan(
                     structural,
                     source_line_count=len(lines),
                     block_index=build_block_index(lines),
                     lines=lines,
                     page_target_lines=config.page_target_lines,
                 )
                 if seed_plan is None:
                     _emit(on_progress, "seed", "structural_rejected", reason=error)
             if seed_plan is None:
                 observations = await observe_document(…unchanged…)
                 seed_plan = await build_seed_plan(…unchanged…)
             _emit(on_progress, "seed", "structural" if structural is not None and seed_plan is not None else "llm", pages=len(seed_plan.pages))
             pages = _plan_pages(seed_plan)
     ```

     (`build_block_index` comes from `.markdown_blocks`; check the existing import list.)

5. `graph/writers.py` `wiki_config(settings, *, run_dir, resume=True, source_kind="md")`: pass
   `source_kind=source_kind` and the five settings below into `WikiConfig(...)`. `run_wiki`
   and `build_wiki_output` get a `source_kind: str = "md"` keyword and forward it;
   `build_wiki_output` computes `kind = kind_of(document_name)` when the caller passes none.

6. `graph/core.py` `Settings` (after the `wiki_*` block) and `from_env`:

   ```python
       structure_target_lines: int = 250           # WIKI_STRUCTURE_TARGET_LINES
       structure_min_lines: int = 40               # WIKI_STRUCTURE_MIN_LINES
       slide_delimiter: str = r"^## Slide (\d+)\s*$"   # WIKI_SLIDE_DELIMITER
       slide_title: str = r"^### (.+?)\s*$"            # WIKI_SLIDE_TITLE
       pdf_use_headings: bool = False              # WIKI_PDF_USE_HEADINGS
       tabular_slice_records: int = 40             # WIKI_TABULAR_SLICE_RECORDS (0 = no slice pages)
       tabular_preview_rows: int = 12              # WIKI_TABULAR_PREVIEW_ROWS
       tabular_preview_cols: int = 12              # WIKI_TABULAR_PREVIEW_COLS
   ```

7. `tests/test_formats_kind.py`: `kind_of("t/a_docx.md") == "docx"`, `kind_of("t/notes.md") == "md"`,
   `kind_of("t/weird_zip.md") == "md"`, `is_tabular("csv")`.

**Verify:** suite green; a `wiki`-mode run of any `_pdf.md` behaves exactly as before
(progress shows `seed llm`).

---

### WP-F2 — `formats/tree.py` + `formats/docx.py`: the heading tree cut

**Files:** new `graph/formats/tree.py`, `graph/formats/docx.py`, `tests/test_formats_docx.py`.

1. `graph/formats/tree.py`:

   ```python
   """Heading tree → seed ranges. Pure functions; the rule is the one in PLAN_FORMATS 'restated'."""

   from __future__ import annotations

   import math
   import re
   from dataclasses import dataclass, field
   from typing import Sequence

   from graph.chunk import scan_markdown_fences
   from graph.wiki.markdown_blocks import BlockIndex, build_block_index
   from graph.wiki.wire import SeedRange

   HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*#*\s*$")


   @dataclass
   class Section:
       level: int                      # 0 = document root
       title: str
       start: int                      # heading line, 1-based (root: 1)
       end: int                        # inclusive
       children: list["Section"] = field(default_factory=list)

       @property
       def size(self) -> int:
           return self.end - self.start + 1


   def heading_tree(lines: Sequence[str], *, title: str = "document") -> Section:
       """ATX headings outside fenced blocks become nested sections that tile 1..N."""
       fenced = set()
       scan = scan_markdown_fences(list(lines))          # graph/chunk.py:686 → openings/closings/unclosed
       if scan.unclosed is not None:
           raise ValueError(f"unclosed fence at line {scan.unclosed.line_number}")
       for opening, closing in zip(scan.openings, scan.closings):
           fenced.update(range(opening.line_number, closing.line_number + 1))
       root = Section(level=0, title=title, start=1, end=len(lines))
       stack = [root]
       for number, line in enumerate(lines, start=1):
           if number in fenced:
               continue
           match = HEADING_RE.match(line)
           if not match:
               continue
           level, text = len(match.group(1)), match.group(2).strip()
           if not text:
               continue                    # pandoc's empty "#  " headings are noise
           while stack[-1].level >= level:
               stack.pop()
           node = Section(level=level, title=text, start=number, end=len(lines))
           stack[-1].children.append(node)
           stack.append(node)
       _close(root, len(lines))
       return root


   def _close(node: Section, end: int) -> None:
       node.end = end
       for index, child in enumerate(node.children):
           child_end = node.children[index + 1].start - 1 if index + 1 < len(node.children) else end
           _close(child, child_end)


   def lead(lines: Sequence[str], start: int, end: int, *, limit: int = 200) -> str:
       """First non-heading, non-blank, non-markup text of a range — the mechanical summary."""
       out: list[str] = []
       for line in lines[start - 1 : end]:
           text = line.strip()
           if not text or text.startswith("#") or text.startswith("<") or text.startswith("|") or text.startswith("!["):
               continue
           out.append(text)
           if sum(len(t) for t in out) >= limit:
               break
       return " ".join(out)[:limit] or "（本文なし）"


   def divisor_split(start: int, end: int, *, target: int, index: BlockIndex) -> list[tuple[int, int]]:
       """Cut start..end into d near-equal pieces, d = 2, 3, … until each ≤ target; cuts are block-safe."""
       size = end - start + 1
       d = 2
       while math.ceil(size / d) > target:
           d += 1
       pieces: list[tuple[int, int]] = []
       cursor = start
       for part in range(1, d + 1):
           if part == d:
               pieces.append((cursor, end))
               break
           ideal = start + round(size * part / d)
           cut = index.nearest_safe_cut(ideal, backsearch=target // 4, forward_limit=target // 4)
           cut = max(cursor + 1, min(cut, end))
           pieces.append((cursor, cut - 1))
           cursor = cut
       return [(s, e) for s, e in pieces if e >= s]


   def pages_for(
       node: Section,
       *,
       lines: Sequence[str],
       index: BlockIndex,
       target: int,
       min_lines: int,
       path: tuple[str, ...] = (),
   ) -> list[SeedRange]:
       """The rule: fits → one page; has children → descend and pack; leaf → divisor split."""
       chain = path + ((node.title,) if node.level > 0 else ())
       if node.size <= target:
           return [_page(node.title, node.start, node.end, chain, lines)]
       if not node.children:
           parts = divisor_split(node.start, node.end, target=target, index=index)
           return [
               _page(f"{node.title}（{i}/{len(parts)}）", s, e, chain, lines)
               for i, (s, e) in enumerate(parts, start=1)
           ]
       items: list[list[SeedRange]] = []
       preamble_end = node.children[0].start - 1
       if preamble_end >= node.start:
           # the section's own text before its first child (for the root: cover, TOC …)
           if preamble_end - node.start + 1 <= target:
               items.append([_page(node.title, node.start, preamble_end, chain, lines)])
           else:
               parts = divisor_split(node.start, preamble_end, target=target, index=index)
               items.append([
                   _page(f"{node.title}（{i}/{len(parts)}）", s, e, chain, lines)
                   for i, (s, e) in enumerate(parts, start=1)
               ])
       for child in node.children:
           items.append(pages_for(child, lines=lines, index=index, target=target, min_lines=min_lines, path=chain))
       # top-level sections (chapters) are never merged with each other, only their children are
       return _pack(items, target=target, min_lines=min_lines, merge=node.level >= 1)


   def _page(title: str, start: int, end: int, chain: tuple[str, ...], lines: Sequence[str]) -> SeedRange:
       return SeedRange(
           title=title, summary=lead(lines, start, end), chapter=" › ".join(chain),
           source_start=start, source_end=end, path=list(chain),
       )


   def _pack(items: list[list[SeedRange]], *, target: int, min_lines: int, merge: bool = True) -> list[SeedRange]:
       """Merge consecutive single-page siblings while the sum stays ≤ target.

       A child that was itself split (len > 1) is a wall: nothing merges across it,
       except a tiny preamble/sibling (< min_lines) that is glued onto its first page.
       With merge=False only that tiny-glue rule applies (used for top-level chapters).
       """
       out: list[SeedRange] = []
       pending: SeedRange | None = None

       def size(page: SeedRange) -> int:
           return page.source_end - page.source_start + 1

       def flush() -> None:
           nonlocal pending
           if pending is not None:
               out.append(pending)
               pending = None

       for group in items:
           if len(group) > 1:
               if pending is not None and size(pending) < min_lines:
                   first = group[0]
                   group[0] = first.model_copy(update={"source_start": pending.source_start})
                   pending = None
               flush()
               out.extend(group)
               continue
           page = group[0]
           if pending is None:
               pending = page
           elif size(pending) + size(page) <= target and (merge or size(pending) < min_lines):
               pending = pending.model_copy(update={
                   "title": pending.title if size(page) < min_lines else f"{pending.title} 〜 {page.title}",
                   "source_end": page.source_end,
                   "summary": pending.summary,
               })
           else:
               flush()
               pending = page
       flush()
       return out
   ```

   `MarkdownFenceScan` (`graph/chunk.py:639`) exposes `openings`, `closings`, `unclosed`
   (each with `.line_number`) — exactly what `markdown_blocks._scan_fence_blocks` reads.
   `BlockIndex.nearest_safe_cut` signature is `(candidate, *, backsearch=0, forward_limit=None)`
   (`markdown_blocks.py:77`). `docx.plan` must catch the `ValueError` and return `None` (C3).

2. `graph/formats/docx.py`:

   ```python
   """docx: pandoc ATX headings are the structure. Edit the cut here and nowhere else."""

   from __future__ import annotations

   from typing import Any, Sequence

   from graph.wiki.markdown_blocks import build_block_index
   from graph.wiki.schemas import CompiledSeedPlan

   from .tree import heading_tree, pages_for


   def plan(lines: Sequence[str], *, config: Any) -> CompiledSeedPlan | None:
       try:
           tree = heading_tree(lines)
       except ValueError:
           return None                     # unclosed fence: let Phase 1–2 deal with it (C3)
       if not tree.children:
           return None                     # no headings at all: let Phase 1–2 observe it
       pages = pages_for(
           tree,
           lines=lines,
           index=build_block_index(list(lines)),
           target=int(config.structure_target_lines),
           min_lines=int(config.structure_min_lines),
       )
       return CompiledSeedPlan(summary="heading-tree plan", pages=pages)
   ```

3. `tests/test_formats_docx.py` — build small synthetic documents and assert:
   (a) 270-line leaf → two pages of 135; (b) 700-line leaf → three pages ≤ 250;
   (c) parent > 250 with 16 children of 10 lines → packed into pages ≤ 250, order kept,
   titles `A 〜 F`; (d) a fence spanning the ideal cut moves the cut outside the fence;
   (e) the front matter before the first `#` is its own page; (f) `path` of a `###` page is
   `[h1, h2, h3]`; (g) the returned plan passes `validate_seed_plan` with a real `BlockIndex`;
   (h) run `plan()` on `data/raw/docx/docx_handbook_872p_docx.md` (skip if absent): every
   page ≤ 250 lines except pages that are one atomic block, and no page smaller than
   `min_lines` unless it is the last child of its parent.

**Prototype evidence (2026-09-13, the `tree.py` above run verbatim against `data/raw/`):**
handbook 9,577 lines → 50 pages, sizes 44–246, tiles, `validate_seed_plan` OK, chapters
separate, `B. MEETING OF CREDITORS` (16 tiny `###`) packed into one 234-line page;
IT-policies 870 lines → 10 pages (48–156); synthetic 270-line leaf → 135+135, 700 → 233/234/233.
The oneAPI PDF (18,013 lines) also produced a sane 91-page tree (40–250) — that is what
`WIKI_PDF_USE_HEADINGS=1` would give for that file.

**Verify:** `python -m graph.wiki data/raw/docx/IT-policies-…_docx.md --output /tmp/w`
(dev CLI; add `--kind docx` if `__main__.py` does not derive it from the name) finishes
with progress `seed structural`; `state/plan.json` pages tile the file; open five pages —
each is one section or a numbered part of one.

---

### WP-F3 — `formats/context.py`: parent + sibling summaries for the writer

**Files:** new `graph/formats/context.py`, `graph/wiki/prompts.py`, `graph/wiki/pipeline.py`,
`tests/test_formats_context.py`.

1. `graph/formats/context.py`:

   ```python
   """One LLM call per parent: a paragraph for the parent, one line per child page."""

   from __future__ import annotations

   from pathlib import Path
   from typing import Any, Sequence

   from pydantic import BaseModel, Field

   from graph.wiki.storage import read_json, write_json_atomic

   from .tree import lead


   class PageLine(BaseModel):
       number: int = 0
       summary: str = ""


   class ParentSummary(BaseModel):
       parent_summary: str = ""
       pages: list[PageLine] = Field(default_factory=list)


   def _prompt(chain: Sequence[str], pages: Sequence[Any], lines: Sequence[str], language: str) -> str:
       heading = " › ".join(chain) or "（文書全体）"
       rows = "\n".join(
           f"- #{page.number} 「{page.title}」（原文 {page.owner_ranges[0][0]}-{page.owner_ranges[-1][1]}行）: "
           f"{lead(lines, page.owner_ranges[0][0], page.owner_ranges[-1][1], limit=400)}"
           for page in pages
       )
       return (
           f"次のセクション「{heading}」に属するページ一覧です。各ページの冒頭を示します。\n\n{rows}\n\n"
           f"1) このセクション全体が何について述べているかを 2〜4 文で `parent_summary` に書く。\n"
           f"2) 各ページについて、1 行（60 字以内）の要約を `pages[].summary` に書く。番号は必ず一致させる。\n"
           f"出力言語: {language}。JSON のみを返す。"
       )


   async def summarize_hierarchy(
       pages: Sequence[Any], lines: Sequence[str], *, model: Any, config: Any, checkpoint: Path, stop_check=None
   ) -> dict[str, str]:
       """Fill page.summary in place; return {parent_key: parent_summary}. Resumable via checkpoint."""
       from langchain_core.messages import HumanMessage

       cached = read_json(checkpoint, default={}) if checkpoint.exists() else {}
       groups: dict[tuple[str, ...], list[Any]] = {}
       for page in pages:
           groups.setdefault(tuple(page.path[:-1]), []).append(page)
       context: dict[str, str] = dict(cached.get("parents", {}))
       page_summaries: dict[str, str] = dict(cached.get("pages", {}))
       for chain, group in groups.items():
           key = " › ".join(chain)
           if key in context and all(str(p.number) in page_summaries for p in group):
               continue
           if stop_check and stop_check():
               raise RuntimeError("context cancelled")
           try:
               result = await model.structured(
                   ParentSummary, [HumanMessage(content=_prompt(chain, group, lines, config.output_language))]
               )
               by_number = {int(item.number): item.summary.strip() for item in result.pages}
           except Exception:                      # noqa: BLE001 - mechanical fallback below
               result, by_number = ParentSummary(), {}
           context[key] = result.parent_summary.strip() or lead(lines, group[0].owner_ranges[0][0], group[-1].owner_ranges[-1][1], limit=400)
           for page in group:
               page_summaries[str(page.number)] = by_number.get(page.number) or page.summary or lead(
                   lines, page.owner_ranges[0][0], page.owner_ranges[-1][1]
               )
           write_json_atomic(checkpoint, {"parents": context, "pages": page_summaries})
       for page in pages:
           page.summary = page_summaries.get(str(page.number), page.summary)
       return context


   def context_block(page: Any, pages: Sequence[Any], parents: dict[str, str], *, limit: int = 12) -> str:
       """The 文脈 block for one page; empty string when the plan has no hierarchy."""
       if not page.path:
           return ""
       key = " › ".join(page.path[:-1])
       siblings = [p for p in pages if tuple(p.path[:-1]) == tuple(page.path[:-1]) and p.number != page.number]
       previous = next((p for p in pages if p.number == page.number - 1), None)
       following = next((p for p in pages if p.number == page.number + 1), None)
       out = ["## 文脈", f"階層: {' › '.join(page.path)}"]
       if parents.get(key):
           out.append(f"親セクションの要約: {parents[key]}")
       if siblings:
           out.append("同じ親の他のページ:")
           out.extend(f"- {p.title} — {p.summary[:120]}" for p in siblings[:limit])
       neighbours = []
       if previous:
           neighbours.append(f"前のページ: {previous.title} — {previous.summary[:120]}")
       if following:
           neighbours.append(f"次のページ: {following.title} — {following.summary[:120]}")
       out.extend(neighbours)
       return "\n".join(out) + "\n"
   ```

   `ModelPort.structured(schema, messages, *, max_output_tokens=None)` — schema first, then
   messages (`pipeline.py:444`); `ModelPort.text(messages, *, max_output_tokens=None)`.
   `HumanMessage` is what `Prompt.messages()` produces for the other builders.

2. `graph/wiki/prompts.py` (Phase 3 builders only):
   - `section_write_prompt(…, feedback=(), context: str = "")`: when `context` is non-empty,
     insert it verbatim right after the page title/summary lines of the prompt body (find the
     line that renders `page_summary`; put `context` on the next line). Empty → unchanged.
   - `intro_prompt(…, context: str = "")`: same.
   - `reference_research_prompt(…)`: no change.

3. `graph/wiki/pipeline.py`:
   - after `_verify_ranges(pages, len(lines))` / the resume branch, and before `_rewrite_all`:

     ```python
         from ..formats.context import summarize_hierarchy

         parents = {}
         if any(page.path for page in pages):
             _emit(on_progress, "context", "start", parents=len({tuple(p.path[:-1]) for p in pages}))
             parents = await summarize_hierarchy(
                 pages, lines, model=model, config=config,
                 checkpoint=state_root / "context.json", stop_check=stop_check,
             )
             _emit(on_progress, "context", "done")
     ```

     and thread `parents` into `_rewrite_all(…, parents=parents)` → `_rewrite_page(…, parents)`
     → `_write_section(…, context=context_block(page, pages, parents))` and
     `_write_intro(…, context=…)` where they call `section_write_prompt` / `intro_prompt`.
   - `_select_references` (559): before the lexical scoring, add same-parent siblings:

     ```python
       family = [item for item in others if item.path and item.path[:-1] == page.path[:-1]]
     ```

     and return `sorted(dict.fromkeys(adjacent + family[:limit] + picks), key=lambda item: item.number)`.
     (Keep `limit` semantics: the lexical `picks` stay ≤ limit.)
   - `_nav_footer` (867): if `page.path[:-1]` is non-empty and some page `q` has
     `q.path == page.path[:-1]` (the parent's preamble page) or is the first page with that
     prefix, prepend `親: [q.title](q.filename)`.
   - `_index_text` (1225): group pages by `path[:-1]`, print a `## ` line per parent chain
     before its pages (pages without a path stay in one flat list). Keep the per-page line.
   - plan.json / manifest already carry `summary`; `_manifest` needs nothing new.

4. `tests/test_formats_context.py`: with a `FakeModel` (as in `tests/test_wiki_chunking.py`)
   returning `ParentSummary`, assert one call per distinct parent, `page.summary` filled,
   `context_block` mentions the chain, the parent summary, siblings and prev/next; a model
   that raises → summaries fall back to `lead()`; `context_block` is `""` for a page with no path.

**Verify:** run the handbook in `wiki` mode; `work/page-NNN/section-01-attempt-01-prompt.md`
contains `## 文脈` with the chain; `state/context.json` has ~40 parents.

---

### WP-F4 — `formats/pptx.py`: slides → sections → groups

**Files:** new `graph/formats/pptx.py`, `tests/test_formats_pptx.py`.

```python
"""pptx: one `## Slide N` block per slide is the atom; one judge call per deck decides sections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from pydantic import BaseModel, Field

from graph.wiki.markdown_blocks import IMAGE_UNIT_CLOSE, IMAGE_UNIT_OPEN
from graph.wiki.schemas import CompiledSeedPlan
from graph.wiki.wire import SeedRange

from .tree import divisor_split, lead


@dataclass
class Slide:
    number: int
    start: int
    end: int
    title: str
    text_lines: int
    first_text: str
    has_table: bool

    @property
    def size(self) -> int:
        return self.end - self.start + 1


def split_slides(lines: Sequence[str], *, delimiter: str, title: str) -> list[Slide]:
    delim, title_re = re.compile(delimiter), re.compile(title)
    starts = [(n, m) for n, line in enumerate(lines, start=1) if (m := delim.match(line))]
    slides: list[Slide] = []
    for index, (start, match) in enumerate(starts):
        end = starts[index + 1][0] - 1 if index + 1 < len(starts) else len(lines)
        number = int(match.group(1)) if match.groups() else index + 1
        heading, text, first, table, inside = "", 0, "", False, False
        for line in lines[start:end]:            # lines after the delimiter line
            s = line.strip()
            if s.startswith(IMAGE_UNIT_OPEN):
                inside = True
            if inside:
                inside = not s.startswith(IMAGE_UNIT_CLOSE)
                continue
            if not s:
                continue
            if (m := title_re.match(s)) and not heading:
                heading = m.group(1).strip()
                continue
            if s.startswith("|") or s.startswith("<table"):
                table = True
            text += 1
            first = first or s[:80]
        slides.append(Slide(number, start, end, heading, text, first, table))
    return slides


def divider_candidates(slides: Sequence[Slide]) -> list[int]:
    return [s.number for s in slides if (s.title and s.text_lines <= 2) or s.text_lines <= 2]


class Section(BaseModel):
    start_slide: int = 0
    title: str = ""


class DeckSections(BaseModel):
    sections: list[Section] = Field(default_factory=list)


def _inventory(slides: Sequence[Slide], candidates: Sequence[int]) -> str:
    rows = [
        f"{s.number}\t{'★' if s.number in candidates else ' '}\t{s.text_lines}行\t{s.title or s.first_text}"
        for s in slides
    ]
    return "スライド番号\t区切り候補\t本文行数\t見出し/冒頭\n" + "\n".join(rows)


async def judge_sections(slides: Sequence[Slide], *, model: Any, language: str) -> list[Section]:
    """One structured call: which slides start a section, and its title. Validated; falls back to candidates."""
    from langchain_core.messages import HumanMessage

    candidates = divider_candidates(slides)
    prompt = (
        "これはプレゼンテーションの各スライドの一覧です（★は本文がほとんど無い区切り候補）。\n"
        "内容のまとまりごとに『どのスライドから新しいセクションが始まるか』と、そのセクションの見出しを決めてください。\n"
        "スライド1は必ず最初のセクションです。区切り候補以外のスライドから始めても構いませんが、理由が明確な場合だけにしてください。\n\n"
        f"{_inventory(slides, candidates)}\n\n出力言語: {language}。JSON のみ。"
    )
    numbers = {s.number for s in slides}
    try:
        result = await model.structured(DeckSections, [HumanMessage(content=prompt)])
        starts = sorted({sec.start_slide: sec.title for sec in result.sections if sec.start_slide in numbers}.items())
    except Exception:                                  # noqa: BLE001
        starts = []
    if not starts or starts[0][0] != min(numbers):
        starts = [(n, next(s.title or s.first_text for s in slides if s.number == n)) for n in (candidates or [min(numbers)])]
        if starts[0][0] != min(numbers):
            starts.insert(0, (min(numbers), slides[0].title or slides[0].first_text or "はじめに"))
    return [Section(start_slide=n, title=t or f"スライド {n} から") for n, t in starts]


async def plan(lines: Sequence[str], *, config: Any, model: Any, on_progress=None, stop_check=None) -> CompiledSeedPlan | None:
    slides = split_slides(lines, delimiter=config.slide_delimiter, title=config.slide_title)
    if len(slides) < 2:
        return None
    sections = await judge_sections(slides, model=model, language=config.output_language)
    starts = [sec.start_slide for sec in sections]
    groups: list[tuple[Section, list[Slide]]] = []
    for index, sec in enumerate(sections):
        upper = starts[index + 1] if index + 1 < len(starts) else 10**9
        groups.append((sec, [s for s in slides if sec.start_slide <= s.number < upper]))
    # a section that is only its divider (< 10 text lines) joins the next one (previous if last)
    merged: list[tuple[Section, list[Slide]]] = []
    for sec, group in groups:
        if sum(s.text_lines for s in group) < 10 and merged and (sec is groups[-1][0]):
            merged[-1] = (merged[-1][0], merged[-1][1] + group)
        elif sum(s.text_lines for s in group) < 10 and not (sec is groups[-1][0]):
            merged.append((sec, group))       # carried into the next append below
        else:
            if merged and sum(s.text_lines for s in merged[-1][1]) < 10:
                prev_sec, prev_group = merged.pop()
                sec, group = Section(start_slide=prev_sec.start_slide, title=prev_sec.title or sec.title), prev_group + group
            merged.append((sec, group))
    target = int(config.structure_target_lines)
    pages: list[SeedRange] = []
    deck_title = next((l.lstrip("# ").strip() for l in lines[:3] if l.startswith("# ")), "プレゼンテーション")
    preamble_end = slides[0].start - 1
    if preamble_end >= 1:
        pages.append(SeedRange(title=deck_title, summary=lead(lines, 1, preamble_end), chapter=deck_title, source_start=1, source_end=preamble_end, path=[deck_title]))
    for sec, group in merged:
        start, end = group[0].start, group[-1].end
        chain = [deck_title, sec.title]
        if end - start + 1 <= target:
            pages.append(SeedRange(title=sec.title, summary=lead(lines, start, end), chapter=" › ".join(chain), source_start=start, source_end=end, path=chain))
            continue
        # too big: divisor rule, but cuts only at slide starts
        pieces = _slide_divisor(group, target=target)
        for i, (s, e) in enumerate(pieces, start=1):
            pages.append(SeedRange(title=f"{sec.title}（{i}/{len(pieces)}）", summary=lead(lines, s, e), chapter=" › ".join(chain), source_start=s, source_end=e, path=chain))
    return CompiledSeedPlan(summary="slide-section plan", pages=pages)


def _slide_divisor(group: Sequence[Slide], *, target: int) -> list[tuple[int, int]]:
    import math

    total = group[-1].end - group[0].start + 1
    d = 2
    while math.ceil(total / d) > target:
        d += 1
    per = math.ceil(len(group) / d)
    chunks = [group[i : i + per] for i in range(0, len(group), per)]
    return [(c[0].start, c[-1].end) for c in chunks if c]
```

The merge loop above is deliberately plain; if it reads badly, rewrite it as: build `groups`,
then walk once, folding any group with `< 10` text lines into its right neighbour (or the
left one when it is last). Same behaviour, choose the clearer form and keep the tests green.

`tests/test_formats_pptx.py`: (a) `split_slides` on `data/raw/pptx/presentation-2_pptx.md`
gives 26 slides, slide 2 has `text_lines == 2` despite 41 lines; (b) candidates include
slide 25 (5 lines) and 2; (c) with a fake model answering `sections=[{1,"A"},{9,"B"}]` the
plan has pages titled `A`, `B`, both `path == [deck, section]`; (d) a fake model that raises
→ candidates become the sections and slide 1 starts; (e) a 2,000-line group is cut only at
slide starts and every piece ≤ 250; (f) a different delimiter (`^--- page (\d+)`) works via
`config.slide_delimiter`; (g) the plan passes `validate_seed_plan`.

**Verify:** `python -m graph.wiki data/raw/pptx/presentation-4_pptx.md …` → `seed structural`,
`state/plan.json` pages are slide groups, `work/planning/` has no window inventories.

---

### WP-F5 — `formats/pdf.py`: passthrough, optional heading heuristic

```python
"""pdf: MinerU headings are not trusted. Phase 1–2 decide, unless WIKI_PDF_USE_HEADINGS says the tree looks sane."""

from __future__ import annotations

from typing import Any, Sequence

from graph.wiki.schemas import CompiledSeedPlan


def plan(lines: Sequence[str], *, config: Any) -> CompiledSeedPlan | None:
    if not getattr(config, "pdf_use_headings", False):
        return None
    from .tree import heading_tree

    try:
        tree = heading_tree(lines)
    except ValueError:
        return None
    headings = _count(tree)
    levels = _depth(tree)
    # sane = at least one heading per 300 lines, at most 3 levels, and no level-1 section over 60% of the file
    if headings < len(lines) / 300 or levels > 3 or any(c.size > 0.6 * len(lines) for c in tree.children):
        return None
    from . import docx

    return docx.plan(lines, config=config)


def _count(node) -> int:
    return len(node.children) + sum(_count(c) for c in node.children)


def _depth(node) -> int:
    return 1 + max((_depth(c) for c in node.children), default=0) if node.children else 0
```

Test: default config → `None`; with `pdf_use_headings=True` on a synthetic doc with a sane
tree → a plan; on a doc with one heading in 5,000 lines → `None`.

---

### WP-F6 — `formats/tabular.py`, `xlsx.py`, `csv.py`: the table writer

**Files:** new `graph/formats/tabular.py`, `graph/formats/xlsx.py`, `graph/formats/csv.py`,
`graph/writers.py`, `tests/test_formats_tabular.py`.

1. `graph/formats/tabular.py` — the shared machinery. Grid first:

   ```python
   """Tables: grid → regions → LLM structure → records → pages. The LLM decides structure; Python checks it."""

   from __future__ import annotations

   import csv as _csv
   import io
   import json
   import re
   import sqlite3
   from dataclasses import dataclass, field
   from html.parser import HTMLParser
   from typing import Any, Literal, Sequence

   from pydantic import BaseModel, Field

   Cell = tuple[int, int]                     # (row, col) 1-based, absolute sheet coordinates


   @dataclass
   class Grid:
       cells: dict[Cell, str]
       col_letters: dict[int, str] = field(default_factory=dict)   # 1 → "A"

       @property
       def rows(self) -> list[int]:
           return sorted({r for r, _ in self.cells})

       @property
       def cols(self) -> list[int]:
           return sorted({c for _, c in self.cells})

       def get(self, row: int, col: int) -> str:
           return self.cells.get((row, col), "")

       def letter(self, col: int) -> str:
           return self.col_letters.get(col) or _letter(col)


   def _letter(col: int) -> str:
       out = ""
       while col > 0:
           col, rem = divmod(col - 1, 26)
           out = chr(65 + rem) + out
       return out


   class _TableParser(HTMLParser):
       """The doc-parser layout: header `<th>` row = Row, A, B…; each `<tr>` starts with the row number.

       Only span anchors are emitted, so covered cells are tracked to keep columns aligned.
       """

       def __init__(self) -> None:
           super().__init__()
           self.rows: list[list[tuple[str, int, int]]] = []
           self._row: list[tuple[str, int, int]] | None = None
           self._cell: list[str] | None = None
           self._span = (1, 1)
           self.headers: list[str] = []
           self._in_th = False

       def handle_starttag(self, tag, attrs):
           a = dict(attrs)
           if tag == "tr":
               self._row = []
           elif tag in ("td", "th") and self._row is not None:
               self._cell = []
               self._span = (int(a.get("rowspan", 1) or 1), int(a.get("colspan", 1) or 1))
               self._in_th = tag == "th"

       def handle_endtag(self, tag):
           if tag in ("td", "th") and self._cell is not None and self._row is not None:
               text = "".join(self._cell).strip()
               if self._in_th:
                   self.headers.append(text)
               else:
                   self._row.append((text, *self._span))
               self._cell = None
           elif tag == "tr" and self._row is not None:
               if self._row:
                   self.rows.append(self._row)
               self._row = None

       def handle_data(self, data):
           if self._cell is not None:
               self._cell.append(data)


   def grid_from_html(table_html: str) -> Grid:
       parser = _TableParser()
       parser.feed(table_html)
       letters = {i: name for i, name in enumerate(parser.headers[1:], start=1)}   # skip "Row"
       cells: dict[Cell, str] = {}
       covered: set[Cell] = set()
       for raw in parser.rows:
           if not raw:
               continue
           row_number = int(re.sub(r"\D", "", raw[0][0]) or 0)
           col = 1
           for text, rowspan, colspan in raw[1:]:
               while (row_number, col) in covered:
                   col += 1
               if text:
                   cells[(row_number, col)] = text
               for dr in range(rowspan):
                   for dc in range(colspan):
                       if (dr, dc) != (0, 0):
                           covered.add((row_number + dr, col + dc))
               col += colspan
       return Grid(cells=cells, col_letters=letters)


   def grid_from_gfm(lines: Sequence[str]) -> Grid:
       """A GFM table (csv parser output): header row = row 1, data from row 2; the `---` row is skipped."""
       cells: dict[Cell, str] = {}
       row = 0
       for line in lines:
           if not line.strip().startswith("|"):
               continue
           parts = [p.strip() for p in line.strip().strip("|").split("|")]
           if all(re.fullmatch(r":?-{3,}:?", p) for p in parts):
               continue
           row += 1
           for col, text in enumerate(parts, start=1):
               if text:
                   cells[(row, col)] = text.replace("\\|", "|")
       return Grid(cells=cells)
   ```

   Regions and the structure call:

   ```python
   @dataclass
   class Region:
       id: int
       r1: int
       c1: int
       r2: int
       c2: int
       cell_count: int

       def preview(self, grid: Grid, *, rows: int, cols: int) -> str:
           picked = list(range(self.r1, min(self.r2, self.r1 + rows - 1) + 1))
           if self.r2 - self.r1 + 1 > rows:
               picked += [self.r2 - 1, self.r2]
           out = []
           for r in picked:
               vals = [f"{grid.letter(c)}:{grid.get(r, c)[:24]}" for c in range(self.c1, min(self.c2, self.c1 + cols - 1) + 1) if grid.get(r, c)]
               out.append(f"行{r}: " + " | ".join(vals))
           return "\n".join(out)


   def find_regions(grid: Grid) -> list[Region]:
       """Islands separated by fully empty rows, then by fully empty columns inside each band."""
       rows, cols = grid.rows, grid.cols
       if not rows:
           return []
       occupied_rows = set(rows)
       bands: list[tuple[int, int]] = []
       start = rows[0]
       for r in range(rows[0], rows[-1] + 1):
           if r not in occupied_rows:
               if start is not None and start <= r - 1:
                   bands.append((start, r - 1))
               start = None
           elif start is None:
               start = r
       if start is not None:
           bands.append((start, rows[-1]))
       regions: list[Region] = []
       for r1, r2 in bands:
           used = sorted({c for (r, c) in grid.cells if r1 <= r <= r2})
           c_start = used[0]
           for i, c in enumerate(used):
               nxt = used[i + 1] if i + 1 < len(used) else None
               if nxt is None or nxt > c + 1:
                   count = sum(1 for (r, cc) in grid.cells if r1 <= r <= r2 and c_start <= cc <= c)
                   regions.append(Region(len(regions) + 1, r1, c_start, r2, c, count))
                   if nxt is not None:
                       c_start = nxt
       # captions: ≤3-cell islands directly above/beside a bigger one are folded into it
       big = [r for r in regions if r.cell_count > 3]
       for small in [r for r in regions if r.cell_count <= 3]:
           host = next((b for b in big if b.r1 - 2 <= small.r2 < b.r1 or (b.r1 <= small.r1 <= b.r2 and abs(b.c1 - small.c2) <= 2)), None)
           if host:
               host.r1, host.c1 = min(host.r1, small.r1), min(host.c1, small.c1)
               host.r2, host.c2 = max(host.r2, small.r2), max(host.c2, small.c2)
               host.cell_count += small.cell_count
       out = [r for r in regions if r.cell_count > 3] or regions
       for i, r in enumerate(out, start=1):
           r.id = i
       return out


   class TableSpec(BaseModel):
       region: int = 0
       title: str = ""
       orientation: Literal["rows", "columns"] = "rows"
       header_rows: list[int] = Field(default_factory=list)     # absolute row numbers holding column names
       label_cols: list[int] = Field(default_factory=list)      # absolute columns naming each row (e.g. A = line item)
       data_rows: list[int] = Field(default_factory=list)       # [first, last]
       data_cols: list[int] = Field(default_factory=list)       # [first, last]
       notes: str = ""


   class SheetStructure(BaseModel):
       summary: str = ""
       tables: list[TableSpec] = Field(default_factory=list)
       ignore: list[int] = Field(default_factory=list)          # region ids that are notes/legends


   def validate_structure(structure: SheetStructure, regions: Sequence[Region]) -> str | None:
       by_id = {r.id: r for r in regions}
       if not structure.tables:
           return "no tables identified; every sheet with data has at least one"
       for t in structure.tables:
           region = by_id.get(t.region)
           if region is None:
               return f"table '{t.title}' names unknown region {t.region}"
           if len(t.data_rows) != 2 or len(t.data_cols) != 2:
               return f"table '{t.title}': data_rows and data_cols must be [first, last]"
           r1, r2 = t.data_rows
           c1, c2 = t.data_cols
           if not (region.r1 <= r1 <= r2 <= region.r2 and region.c1 <= c1 <= c2 <= region.c2):
               return f"table '{t.title}': data range {t.data_rows}×{t.data_cols} leaves region {region.id} ({region.r1}-{region.r2}, {region.c1}-{region.c2})"
           if t.orientation == "rows" and any(not (region.r1 <= h < r1) for h in t.header_rows):
               return f"table '{t.title}': header_rows must lie above the data rows"
           if t.orientation == "columns" and any(not (region.c1 <= h < c1) for h in t.label_cols):
               return f"table '{t.title}': label_cols must lie left of the data columns"
       return None


   def heuristic_structure(regions: Sequence[Region], grid: Grid) -> SheetStructure:
       """Only after the model failed twice: first row = headers, first text column = labels."""
       tables = []
       for r in regions:
           first_text_col = next((c for c in range(r.c1, r.c2 + 1) if not _numeric(grid.get(r.r1 + 1, c))), r.c1)
           tables.append(TableSpec(region=r.id, title=f"領域 {r.id}", header_rows=[r.r1], label_cols=[first_text_col],
                                   data_rows=[min(r.r1 + 1, r.r2), r.r2], data_cols=[r.c1, r.c2]))
       return SheetStructure(summary="（自動判定）", tables=tables)


   def structure_prompt(sheet: str, regions: Sequence[Region], grid: Grid, *, rows: int, cols: int, error: str | None, language: str) -> str:
       blocks = "\n\n".join(f"### 領域 {r.id}（行{r.r1}-{r.r2}, 列{grid.letter(r.c1)}-{grid.letter(r.c2)}, {r.cell_count}セル）\n{r.preview(grid, rows=rows, cols=cols)}" for r in regions)
       fix = f"\n\n前回の回答は却下されました: {error}\n修正して返してください。" if error else ""
       return (
           f"シート「{sheet}」の非空セルの塊（領域）です。各領域について、表なのか（注記・凡例なら ignore）、\n"
           f"表なら: 何の表か(title)、レコードが行なのか列なのか(orientation)、列名を持つ行(header_rows: 複数行の見出しは全て)、\n"
           f"各行の名前になっている列(label_cols)、データ範囲(data_rows=[最初,最後], data_cols=[最初,最後]; 列は数字で、A=1)を決めてください。\n"
           f"余白・空行・見出しの上の説明行はデータに含めないこと。\n\n{blocks}{fix}\n\n出力言語: {language}。JSON のみ。"
       )


   async def decide_structure(sheet: str, grid: Grid, regions: Sequence[Region], *, model: Any, config: Any, attempts: int = 2) -> SheetStructure:
       from langchain_core.messages import HumanMessage

       error: str | None = None
       for _ in range(attempts):
           try:
               structure = await model.structured(
                   SheetStructure,
                   [HumanMessage(content=structure_prompt(sheet, regions, grid, rows=config.tabular_preview_rows, cols=config.tabular_preview_cols, error=error, language=config.output_language))],
               )
           except Exception as exc:                    # noqa: BLE001
               error = f"{type(exc).__name__}: {exc}"
               continue
           error = validate_structure(structure, regions)
           if error is None:
               return structure
       return heuristic_structure(regions, grid)
   ```

   Records, stats, rendering, the spec-in-page contract, and the query tool:

   ```python
   _NUM_RE = re.compile(r"^[-+]?[¥$€]?\s*\d[\d,]*(\.\d+)?\s*%?$")


   def _numeric(text: str) -> float | None:
       t = text.strip().replace(",", "").replace("¥", "").replace("$", "").replace("€", "").rstrip("%").strip()
       if not t or not _NUM_RE.match(text.strip()):
           return None
       try:
           return float(t)
       except ValueError:
           return None


   @dataclass
   class Record:
       key: str                     # "行 12" or "列 D"
       label: str
       values: dict[str, str]


   def headers_for(grid: Grid, spec: TableSpec) -> dict[int, str]:
       """Column (or row) → name, multi-row joined with ' / ', merged/blank headers forward-filled."""
       names: dict[int, str] = {}
       last = ""
       if spec.orientation == "rows":
           c1, c2 = spec.data_cols
           for c in range(c1, c2 + 1):
               parts = [grid.get(h, c) for h in spec.header_rows if grid.get(h, c)]
               name = " / ".join(parts) or last or grid.letter(c)
               last = name if parts else last
               names[c] = name
       else:
           r1, r2 = spec.data_rows
           for r in range(r1, r2 + 1):
               parts = [grid.get(r, l) for l in spec.label_cols if grid.get(r, l)]
               name = " / ".join(parts) or last or f"行{r}"
               last = name if parts else last
               names[r] = name
       return names


   def records_for(grid: Grid, spec: TableSpec) -> tuple[list[str], list[Record]]:
       names = headers_for(grid, spec)
       records: list[Record] = []
       if spec.orientation == "rows":
           r1, r2 = spec.data_rows
           for r in range(r1, r2 + 1):
               values = {names[c]: grid.get(r, c) for c in names if grid.get(r, c)}
               if not values:
                   continue
               label = " ".join(grid.get(r, l) for l in spec.label_cols if grid.get(r, l))
               records.append(Record(f"行 {r}", label, values))
       else:
           c1, c2 = spec.data_cols
           for c in range(c1, c2 + 1):
               values = {names[r]: grid.get(r, c) for r in names if grid.get(r, c)}
               if not values:
                   continue
               label = " ".join(grid.get(h, c) for h in spec.header_rows if grid.get(h, c))
               records.append(Record(f"列 {grid.letter(c)}", label, values))
       columns = list(dict.fromkeys(name for rec in records for name in rec.values))
       return columns, records


   def stats_for(columns: Sequence[str], records: Sequence[Record]) -> list[dict[str, Any]]:
       out = []
       for name in columns:
           raw = [rec.values.get(name, "") for rec in records if rec.values.get(name, "")]
           nums = [n for n in (_numeric(v) for v in raw) if n is not None]
           entry: dict[str, Any] = {"column": name, "filled": len(raw)}
           if nums and len(nums) >= len(raw) * 0.6:
               entry.update(kind="numeric", min=min(nums), max=max(nums), mean=sum(nums) / len(nums), sum=sum(nums))
               entry["max_key"] = next(rec.key for rec in records if _numeric(rec.values.get(name, "")) == max(nums))
               entry["min_key"] = next(rec.key for rec in records if _numeric(rec.values.get(name, "")) == min(nums))
           else:
               distinct = list(dict.fromkeys(raw))
               entry.update(kind="text", distinct=len(distinct), top=distinct[:20])
           out.append(entry)
       return out


   SPEC_MARK = "<!-- table-spec: {} -->"
   SPEC_RE = re.compile(r"^<!-- table-spec: (\{.*\}) -->\s*$", re.MULTILINE)


   def spec_comment(sheet: str, spec: TableSpec) -> str:
       return SPEC_MARK.format(json.dumps({"sheet": sheet, **spec.model_dump()}, ensure_ascii=False))


   def specs_in_page(body: str) -> list[dict[str, Any]]:
       return [json.loads(m.group(1)) for m in SPEC_RE.finditer(body)]


   def grids_in_page(body: str) -> list[Grid]:
       """Every <table>…</table> block on a page, in order (the sheet's verbatim table)."""
       return [grid_from_html(m.group(0)) for m in re.finditer(r"<table>.*?</table>", body, re.DOTALL)]


   def records_from_page(body: str) -> list[tuple[dict[str, Any], list[str], list[Record]]]:
       """What any indexer needs: [(spec, columns, records)] rebuilt from the page alone."""
       grids = grids_in_page(body)
       if not grids:
           gfm = [l for l in body.splitlines() if l.strip().startswith("|")]
           grids = [grid_from_gfm(gfm)] if gfm else []
       out = []
       for spec_dict in specs_in_page(body):
           spec = TableSpec.model_validate({k: v for k, v in spec_dict.items() if k != "sheet"})
           grid = grids[0] if grids else Grid(cells={})
           columns, records = records_for(grid, spec)
           out.append((spec_dict, columns, records))
       return out


   def query_records(columns: Sequence[str], records: Sequence[Record], sql: str, *, limit: int = 200) -> str:
       """SELECT-only SQL over an in-memory table `t`; sqlite's authorizer is the guard, not a regex."""
       conn = sqlite3.connect(":memory:")
       quoted = ", ".join(f'"{c.replace(chr(34), chr(39))}"' for c in ["key", "label", *columns])
       conn.execute(f"CREATE TABLE t ({quoted})")
       rows = []
       for rec in records:
           vals = [rec.key, rec.label] + [
               (_numeric(rec.values.get(c, "")) if _numeric(rec.values.get(c, "")) is not None else rec.values.get(c, "")) for c in columns
           ]
           rows.append(vals)
       conn.executemany(f"INSERT INTO t VALUES ({', '.join('?' * (len(columns) + 2))})", rows)

       def authorizer(action, *_args):
           return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION) else sqlite3.SQLITE_DENY

       conn.set_authorizer(authorizer)
       statement = sql.strip().rstrip(";")
       if ";" in statement:
           return "error: one statement only"
       if not re.search(r"\blimit\b", statement, re.IGNORECASE):
           statement += f" LIMIT {limit}"
       try:
           cursor = conn.execute(statement)
           head = [d[0] for d in cursor.description or []]
           out = io.StringIO()
           writer = _csv.writer(out)
           writer.writerow(head)
           writer.writerows(cursor.fetchmany(limit))
           return out.getvalue()[:8000]
       except sqlite3.Error as exc:
           return f"error: {exc}"
       finally:
           conn.close()
   ```

   Rendering and the writer loop (one sheet at a time; `run_tabular` mirrors `run_wiki`):

   ```python
   def render_table_page(sheet: str, structure: SheetStructure, tables: list[tuple[TableSpec, list[str], list[Record], list[dict]]], table_html: str, grid: Grid) -> str:
       out = [f"# {sheet}", "", structure.summary, ""]
       for spec, columns, records, stats in tables:
           out += [f"## {spec.title}", "", spec_comment(sheet, spec), "",
                   f"- レコード: {'行' if spec.orientation == 'rows' else '列'}（{len(records)} 件）",
                   f"- データ範囲: 行 {spec.data_rows[0]}-{spec.data_rows[1]}, 列 {grid.letter(spec.data_cols[0])}-{grid.letter(spec.data_cols[1])}", ""]
           if spec.notes:
               out += [spec.notes, ""]
           out += ["### 列の定義", ""] + [f"- **{c}**" for c in columns] + ["", "### 統計", "", "| 列 | 種類 | 値 |", "|---|---|---|"]
           for s in stats:
               summary = f"min {s['min']:g}（{s['min_key']}） / max {s['max']:g}（{s['max_key']}） / mean {s['mean']:.4g} / sum {s['sum']:g}" if s["kind"] == "numeric" else f"{s['distinct']} 種類: {', '.join(map(str, s['top'][:8]))}"
               out.append(f"| {s['column']} | {s['kind']} | {summary} |")
           out.append("")
       out += ["## 元データ（原本）", "", table_html, ""]
       return "\n".join(out)


   def analysis_prompt(sheet: str, spec: TableSpec, columns: Sequence[str], records: Sequence[Record], stats: Sequence[dict], *, language: str, feedback: Sequence[str] = ()) -> str:
       table = ["| key | label | " + " | ".join(columns) + " |", "|" + "---|" * (len(columns) + 2)]
       table += [f"| {r.key} | {r.label} | " + " | ".join(r.values.get(c, "") for c in columns) + " |" for r in records]
       fb = ("\n\n前回の指摘:\n" + "\n".join(f"- {f}" for f in feedback)) if feedback else ""
       return (
           f"シート「{sheet}」の表「{spec.title}」です。統計:\n{json.dumps(stats, ensure_ascii=False)}\n\nレコード:\n" + "\n".join(table) +
           f"\n\nこの表から読み取れることを Markdown で書いてください: 何の表か、主要な傾向、極端な値、注意点。"
           f"数値や行に言及するときは必ず `行 12` / `列 D` の形式でキーを引用すること。存在しないキーを書かないこと。{fb}\n出力言語: {language}。"
       )


   CITE_RE = re.compile(r"(行|列)\s*([A-Z]+|\d+)")


   def check_citations(markdown: str, records: Sequence[Record]) -> list[str]:
       keys = {rec.key.replace(" ", "") for rec in records}
       bad = sorted({f"{k}{n}" for k, n in CITE_RE.findall(markdown) if f"{k}{n}" not in keys})
       return [f"存在しないキーを引用しています: {', '.join(bad[:10])}"] if bad else []
   ```

   ```python
   async def write_tables(
       *, sheets: list[tuple[str, str, list[str]]], run_dir, model, config, on_progress=None, stop_check=None
   ) -> list[dict[str, Any]]:
       """sheets = [(name, table_html_or_gfm, source_lines_of_that_sheet)]. Writes docs/ + _planning/ under run_dir."""
       from langchain_core.messages import HumanMessage
       from pathlib import Path

       from graph.wiki.storage import write_json_atomic, write_text_atomic

       docs = Path(run_dir) / "docs"
       docs.mkdir(parents=True, exist_ok=True)
       number = 0
       files: list[dict[str, Any]] = []
       for sheet, table_text, (line_start, line_end) in sheets:
           if stop_check and stop_check():
               raise RuntimeError("tabular cancelled")
           grid = grid_from_html(table_text) if table_text.lstrip().startswith("<table") else grid_from_gfm(table_text.splitlines())
           regions = find_regions(grid)
           if not regions or "_Sparse cell view" in table_text:
               structure = SheetStructure(summary="（大きすぎるため原本のみ）", tables=[])
           else:
               structure = await decide_structure(sheet, grid, regions, model=model, config=config)
           tables = []
           for spec in structure.tables:
               columns, records = records_for(grid, spec)
               if records:
                   tables.append((spec, columns, records, stats_for(columns, records)))
           number += 1
           name = f"{number:03d}-{_slug(sheet)}.md"
           write_text_atomic(docs / name, render_table_page(sheet, structure, tables, table_text, grid))
           files.append({"filename": name, "title": sheet, "kind": "table", "source_ranges": [[line_start, line_end]], "summary": structure.summary})
           for spec, columns, records, stats in tables:
               slices = [records] if config.tabular_slice_records <= 0 else [records[i : i + config.tabular_slice_records] for i in range(0, len(records), config.tabular_slice_records)]
               targets = [("分析", records if len(records) <= 60 else records[:30] + records[-10:])] + ([(f"{i + 1}", s) for i, s in enumerate(slices)] if len(slices) > 1 else [])
               for suffix, subset in targets:
                   feedback: list[str] = []
                   text = ""
                   for _attempt in range(3):
                       text = await model.text([HumanMessage(content=analysis_prompt(sheet, spec, columns, subset, stats, language=config.output_language, feedback=feedback))])
                       feedback = check_citations(text, subset)
                       if not feedback:
                           break
                   if feedback:
                       text = "> 引用チェックに失敗したため、統計のみを掲載します。\n\n" + json.dumps(stats, ensure_ascii=False, indent=1)
                   number += 1
                   fname = f"{number:03d}-{_slug(sheet)}-{_slug(spec.title)}-{suffix}.md"
                   body = f"# {spec.title} — {suffix}\n\n元の表: [{sheet}]({name})（{spec.title}）\n\n{text}\n"
                   write_text_atomic(docs / fname, body)
                   files.append({"filename": fname, "title": f"{spec.title} — {suffix}", "kind": "analysis", "source_ranges": [[line_start, line_end]], "summary": text.strip().splitlines()[0][:120] if text.strip() else ""})
           if on_progress:
               on_progress({"stage": "tabular", "sheet": sheet, "tables": len(tables)})
       planning = Path(run_dir) / "_planning"
       planning.mkdir(exist_ok=True)
       write_json_atomic(planning / "manifest.json", {"planning": {"ingest_mode": "pages", "strategy": "tabular"}, "files": files})
       write_json_atomic(planning / "coverage.json", {"files": [{"title": f["title"], "filename": re.sub(r"^\d+-", "", f["filename"]), "summary": f["summary"], "header": "表", "source_start": f["source_ranges"][0][0], "source_end": f["source_ranges"][0][1]} for f in files]})
       write_json_atomic(planning / "metadata.json", {"files": [{"name": re.sub(r"^\d+-", "", f["filename"]), "header": "表"} for f in files]})
       return files


   def _slug(text: str) -> str:
       from graph.wiki.ids import slugify   # the same slug the wiki writer uses for filenames

       return slugify(text, fallback="sheet")
   ```

   (`"ingest_mode": "pages"` + coverage/metadata with prefix-stripped names is the contract
   `_load_new_planning_docs_output` reads today — see PLAN_SYNC folder contract. Once
   PLAN_GROWI WP-G8 lands, the indexer reads the page instead; the spec comment and the
   `kind:table` marker token below make that work.)

2. `graph/formats/xlsx.py`:

   ```python
   """xlsx: one `## Sheet:` block = one table page (+ analyses). Sheet loop only; logic lives in tabular.py."""

   from __future__ import annotations

   import re
   from pathlib import Path
   from typing import Any

   from .tabular import write_tables

   SHEET_RE = re.compile(r"^## Sheet: (.+?)\s*$")


   def split_sheets(lines: list[str]) -> list[tuple[str, str, tuple[int, int]]]:
       starts = [(n, m.group(1)) for n, line in enumerate(lines, start=1) if (m := SHEET_RE.match(line))]
       out = []
       for i, (start, name) in enumerate(starts):
           end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(lines)
           block = "\n".join(lines[start:end])
           table = block[block.find("<table>") : block.rfind("</table>") + len("</table>")] if "<table>" in block else block
           out.append((name, table, (start, end)))
       return out


   async def run(source_path: Path, *, run_dir: Path, model: Any, config: Any, on_progress=None, stop_check=None):
       lines = source_path.read_text(encoding="utf-8").splitlines()
       return await write_tables(sheets=split_sheets(lines), run_dir=run_dir, model=model, config=config, on_progress=on_progress, stop_check=stop_check)
   ```

3. `graph/formats/csv.py`:

   ```python
   """csv: the whole file is one GFM table; one table page (+ analyses)."""

   from __future__ import annotations

   from pathlib import Path
   from typing import Any

   from .tabular import write_tables


   async def run(source_path: Path, *, run_dir: Path, model: Any, config: Any, on_progress=None, stop_check=None):
       lines = source_path.read_text(encoding="utf-8").splitlines()
       title = next((l.lstrip("# ").strip() for l in lines if l.startswith("# ")), source_path.stem)
       return await write_tables(sheets=[(title, "\n".join(lines), (1, len(lines)))], run_dir=run_dir, model=model, config=config, on_progress=on_progress, stop_check=stop_check)
   ```

4. `graph/writers.py` `build_wiki_output`: at the top, after computing `kind`:

   ```python
       if is_tabular(kind):
           from .chunk import _run_async_blocking
           from .formats import csv as csv_format, xlsx as xlsx_format
           from .wiki.model import ChatModelPort

           config = wiki_config(settings, run_dir=state_dir or (out_dir / "wiki-state"), source_kind=kind)
           runner = xlsx_format.run if kind == "xlsx" else csv_format.run
           _run_async_blocking(
               runner(source_path, run_dir=out_dir, model=ChatModelPort(config), config=config, on_progress=on_progress, stop_check=stop_check)
           )
           return SimpleNamespace(out_dir=out_dir, file_count=len(list((out_dir / "docs").glob("*.md"))))
   ```

   Read `graph/wiki/model.py` for the real constructor of the chat port (it may take the
   config or the three chat fields); `wiki_config` must forward the new `tabular_*`,
   `structure_*`, `slide_*`, `pdf_use_headings` settings into `WikiConfig`.

5. `tests/test_formats_tabular.py`:
   (a) `grid_from_html` on the first sheet of `data/raw/xlsx/xlsx_financial_analyses_xlsx.md`
   keeps row numbers and expands `colspan` (cell `(1, 1)` = `3. Analyses by segment`, nothing at `(1, 2)`);
   (b) a synthetic sheet with two islands separated by an empty row → two regions; a
   1-cell caption above the second is folded into it; (c) `validate_structure` rejects a
   data range outside its region and `header_rows` below the data; (d) `records_for` with
   two header rows joins names with ` / `; orientation `columns` transposes; (e) `stats_for`
   marks a `¥1,234` column numeric and finds `max_key`; (f) `query_records` runs
   `SELECT label, "売上" FROM t ORDER BY "売上" DESC` and refuses `DROP TABLE t` and
   `SELECT 1; SELECT 2`; (g) `check_citations` flags `行 999`; (h) `records_from_page(render_table_page(...))`
   round-trips the same records; (i) a fake model → `write_tables` writes `001-…md` with the
   verbatim `<table>` and an analysis page, and `_planning/manifest.json` lists both.

**Verify:** `build_wiki_output(mode="wiki")` on `data/raw/xlsx/xlsx_financial_analyses_xlsx.md`
→ 5 table pages + analyses; open `001-…md`: the summary names the sheet correctly, header
definitions match the visible headers, the full table is at the bottom.

---

### WP-F7 — Index tables: `NodeType.table`, record search items, stripped prompts

**Files:** `graph/core.py`, `graph/librarian.py`, `graph/gateway.py`, `graph/store.py`,
`graph/growi.py` (marker token), `tests/test_formats_index.py`.

1. `graph/core.py`: `NodeType.table = "table"`; add next to `strip_image_media`:

   ```python
   _BIG_TABLE_RE = re.compile(r"<table>.*?</table>", re.DOTALL)


   def strip_big_tables(text: str, *, max_rows: int = 40) -> str:
       """Whole spreadsheets never belong in a prompt, an embedding, or an FTS row."""
       if not text or "<table" not in text:
           return text

       def replace(match: re.Match[str]) -> str:
           rows = match.group(0).count("<tr")
           if rows <= max_rows:
               return match.group(0)
           return f"[表: {rows} 行 — query_table で検索]"

       return _BIG_TABLE_RE.sub(replace, text)
   ```

2. `graph/gateway.py`: every `strip_image_media(x)` site (77, 115, 133, 339, 357) becomes
   `strip_big_tables(strip_image_media(x))`; `_embed_safe_text` (552) applies
   `strip_big_tables` to its result. `graph/store.py` `_reindex_fts` (WP-F0) likewise.

3. `graph/librarian.py`:
   - `_build_search_items` (2299): at the top

     ```python
             if node.type == NodeType.table:
                 from .formats.tabular import records_from_page

                 items: list[dict] = []
                 add("title", node.title, 0, None, None)
                 for spec, columns, records in records_from_page(node.body):
                     for ordinal, rec in enumerate(records):
                         text = f"{spec.get('title', '')} {rec.key} {rec.label}: " + "; ".join(f"{k}={v}" for k, v in rec.values.items())
                         add("record", text[:512], ordinal, None, None)
                 return items
     ```

     (`add` and `items` are the closure already defined in that function — place this after
     they exist; read the function before editing.)
   - `_fill_derived_fields` / `_extract_claims`: the body they see is already stripped by the
     gateway, so nothing to do. `Settings` gets `weight_record_vec: float = 1.2` and
     `item_vec_weight` (`graph/core.py`, imported by researcher.py:57) maps `"record"` to it.
   - The loader (`_load_new_planning_docs_output`, until WP-G8) and `sync_growi.node_from_page`
     (after WP-G8) set `type=NodeType.table` when the body contains a `<!-- table-spec:` comment.

4. `graph/growi.py` `wrap_page` (PLAN_GROWI WP-G4): if the body contains `<!-- table-spec:`,
   the marker gets a `kind:table` token (`… hash:<h> kind:table -->`); harmless for the
   marker regex (`.*?-->`), and the GROWI-side indexer can read it without parsing the body.

5. Test: a table node built from `render_table_page(...)` output yields `record` search
   items whose text carries the key and every header; `strip_big_tables` leaves a 5-row
   table alone and replaces a 200-row one; `keyword_search("行 12")`-style FTS finds the record.

---

### WP-F8 — Research: `query_table` tool + tabular subagent

**Files:** `graph/researcher.py`, `graph/core.py` (prompt), `tests/test_formats_query.py`.

1. `graph/researcher.py`:
   - a helper near `_sub_read`:

     ```python
     def _query_table(session: Any, node_id: str, sql: str) -> str:
         from .core import NodeType
         from .formats.tabular import query_records, records_from_page

         node = session.store.get_node(node_id)
         if node is None or node.type != NodeType.table:
             return "error: not a table node"
         parts = records_from_page(node.body)
         if not parts:
             return "error: this table page carries no records"
         spec, columns, records = parts[0]
         header = "columns: key, label, " + ", ".join(f'"{c}"' for c in columns) + "\n"
         return _sanitize_tool_output(header + query_records(columns, records, sql))
     ```

   - `class QueryTableArgs(BaseModel)`: `node_id: str`, `sql: str` with a docstring in the
     same language/style as `ReadArgs` (grep it): "表ノードに対して SELECT 文を実行する。テーブル名は
     t、列名は read の結果に書かれている。LIMIT 200。"
   - `_sub_tools` and `_lead_tools`: add
     `StructuredTool.from_function(lambda node_id, sql: _query_table(ctx.session, node_id, sql), name="query_table", description=QueryTableArgs.__doc__ or "", args_schema=QueryTableArgs)`.
     (For `_lead_tools` use the lead context's session accessor; read `_lead_tools` at 444.)
   - `_sub_read` (the `read` tool): when the node is a table, append
     `"\n\n（この表は query_table(node_id, sql) で検索できます。列: …）"` with the column list from
     `records_from_page`.
   - `run_subagent` (850): after `ctx = SubagentContext(...)`, pick the system prompt:

     ```python
         start = session.store.get_node(run.start_id)
         system_prompt = TABULAR_SUBAGENT_SYSTEM_PROMPT if start is not None and start.type == NodeType.table else SUBAGENT_SYSTEM_PROMPT
     ```

     and pass `system_prompt` to `_compile_agent`.

2. `graph/core.py`: `TABULAR_SUBAGENT_SYSTEM_PROMPT` — copy `SUBAGENT_SYSTEM_PROMPT` and add,
   in the tool list, `query_table` with the rule: "数値の質問には必ず query_table で集計してから答える。
   結果の行は `行 12` のキーで引用する。表を読み下して推測しない。"

3. Tests: a fake session whose store holds one table node → `_query_table` returns CSV with
   the header line; a non-table node → `error:`; `DROP` → `error:`.

**Verify:** ask `/prefix/<team>/` "売上が最大の行はどれ？" on an ingested workbook: the trace
shows `query_table` with an `ORDER BY … DESC LIMIT 1` and the answer cites `行 N`.

---

### WP-F9 — Wiring, env, docs

1. `graph/wiki/__main__.py`: derive `source_kind` from the input filename with `kind_of`
   (add `--kind` to override) and pass it into `WikiConfig`.
2. `graph/writers.py` `write_wiki`: `build_wiki_output(..., source_kind=kind_of(rel))`.
3. `README` env table: `WIKI_STRUCTURE_TARGET_LINES`, `WIKI_STRUCTURE_MIN_LINES`,
   `WIKI_SLIDE_DELIMITER`, `WIKI_SLIDE_TITLE`, `WIKI_PDF_USE_HEADINGS`,
   `WIKI_TABULAR_SLICE_RECORDS`, `WIKI_TABULAR_PREVIEW_ROWS/COLS`. Your work PC: set
   `WIKI_SLIDE_DELIMITER` to that parser's phrase as a regex with one capture group for the
   slide number (or none — then slides are numbered by order).
4. `SettingsView.jsx`: the eight fields under a "形式別チャンク" group, same pattern as the
   `wiki_*` group.
5. Cross-link: `PLAN_GROWI.md` §1.4 — add one line: "the writer is chosen by
   `formats.kind_of(rel)`: tables → tabular writer, everything else → `ingest_mode`."

**Done when:** the three real files — handbook docx, `presentation-4.pptx`,
`xlsx_financial_analyses.xlsx` — go through `sync_raw` and produce, respectively: a page
tree that mirrors the chapters with `## 文脈` in every section prompt; slide-group pages
with section titles the deck actually uses; five sheet pages with correct headers and
analyses whose row citations all resolve — and a tabular question is answered via
`query_table`.

---

## Appendix A — Deliberate ceilings (`ponytail:` comments)

- **Sibling packing is greedy, left to right.** A 240-line child followed by a 20-line one
  leaves the small one alone. Good enough; a DP packer is a later luxury.
- **Divisor cuts are near-equal by lines, snapped to safe cuts.** They do not look for the
  best paragraph boundary semantically. Titles are `（i/n）`; the writer's intro explains the part.
- **pptx: one judge call, no retry loop.** A bad answer falls back to candidates; the
  candidates alone already produce readable groups.
- **Tables: one structure call per sheet, two attempts, then the heuristic.** Sheets with
  more than ~12 regions get truncated previews; if a real workbook needs it, batch regions.
- **Records are re-parsed from the page on every `query_table`.** ~100 ms for 5k rows. Cache
  in `search_items` only if a workbook is queried in tight loops.
- **`_numeric` is a regex.** Dates, fractions, "1.2M" stay text; extend when a real sheet needs it.
- **Structural planning only in `wiki` mode.** `chunks`/`pages` keep their own cutters.
- **`strip_big_tables` threshold is 40 rows** everywhere; small tables still travel whole.

## Appendix B — Questions only you can answer

1. Slide delimiter on the work PC: paste the exact phrase; the default here is
   `^## Slide (\d+)\s*$`.
2. For sheets that are one big list (5,000 rows), do you want slice pages at all
   (`WIKI_TABULAR_SLICE_RECORDS=0` gives overview + one analysis only)?
3. Should the parent-summary call also run for `pdf` when Phase 1–2 plans it (the LLM plan
   has `chapter` but no `path`)? Cheap to add: derive `path=[chapter]`.
