# Implementation plan — data folder, one writer switch, git-driven sync

Companion to `PLAN_NEO.md` (which makes the `wiki` writer reliable) and to
`IMPLEMENTATION_PLAN_GROWI.md` (which built page-mode, Qdrant, and the GROWI connection).
This document turns the three writers we now have — `chunks`, `pages`, `wiki` (neo) —
into **one switch**, gives every project **one folder layout**, and adds **one sync loop**
that keeps `wiki/` and the graph in step with a git-tracked `raw/`.

Written for an implementer working one work package at a time. It contains the code you
will type. Where it gives code, type that code. Where it says "delete", delete. If a
snippet does not fit the real file, stop and report the exact line — do not patch around it.

**Status: hard plan (2026-09-12).** The neo implementation was reviewed against
`PLAN_NEO.md` and every field this plan reads exists as written:
`state/plan.json.pages[].{number, title, chapter, summary, filename, owner_ranges,
reference_ranges, provenance}` (`pipeline.py:1354-1373`),
`state/manifest.json.pages[].{attempts, judge_score, verbatim_sections}` (`pipeline.py:1263-1266`),
`state/pages/NNN.json` with `content_sha256` (`pipeline.py:764-767`), scratch dirs
`work/page-NNN` and `work/research-NNN`, and `_load_seed_plan` still rejecting a plan whose
`source_sha256` or `source_line_count` differ (`pipeline.py:697-699`) — which is what WP-S6
relies on. `run_root` is still the one line WP-S1 step 3 replaces (`pipeline.py:1291`).
`_run_async_blocking` exists at `graph/chunk.py:2825`.

**Depends on:** `PLAN_NEO.md` WP-0…WP-7 merged. WP-S1 below reads neo's `state/plan.json`,
`state/pages/NNN.json` and `wiki/NNN-*.md`; those are the fields that plan freezes.
If neo is not done yet you can still do WP-S0, WP-S2 and WP-S4 (they do not touch neo).

---

## How to use this document

1. Read **Decisions** and **Hard constraints** first. Constraints override everything.
2. Do one work package at a time, in order. Each has **Goal → Files → Steps → Verify → Revert**.
3. Commit after each package: `sync WP-SN: <goal line>`. One package, one commit.
4. Tests: `unittest`, dotted module paths, no `pytest`:

   ```bash
   cd llm-wiki-dist
   .venv/bin/python -m unittest tests.test_project tests.test_writers tests.test_sync
   for f in tests/test_*.py; do .venv/bin/python -m unittest "tests.$(basename ${f%.py})" || echo "FAILED $f"; done
   ```

---

## Decisions (read once, then do not re-argue them)

| # | Decision | Why |
|---|---|---|
| D1 | **One project = one folder** `data/<project>/` with `mount/ raw/ metadata/ wiki/ graph.sqlite`. The URL segment `{PREFIX}/{db}/` that today picks `DB_DIR/<db>.sqlite` picks `DATA_ROOT/<db>/graph.sqlite` instead. | The per-db stack, routing, admin panel and GROWI registry already key everything by that name. Reuse it. |
| D2 | **`raw/` is the git repository.** `git -C data/<project>/raw`. `metadata/last_sha` is the only sync state. No file-hash table. | `git diff --name-status` already gives A/M/D; a hash table would be a second source of truth. |
| D3 | **One switch: `Settings.ingest_mode ∈ {chunks, pages, wiki}`.** `wiki` = neo. Default stays `chunks`. | The field, its UI, and the `chunk_and_ingest` branch already exist for `pages`. |
| D4 | **All three writers produce the same folder:** `wiki/<rel-dir>/<name>/NNN-*.md` + `wiki/<rel-dir>/<name>/_planning/{metadata,coverage,manifest}.json`. The graph is fed from that folder via the existing `ingest_md_output`. | One loader, one ingest path, one zip, one GROWI publisher, regardless of writer. |
| D5 | **`wiki/` is canonical; GROWI and Obsidian are exports.** GROWI does not serve folders — it is fed through the existing `publish_pages`. Obsidian gets a zip of `wiki/` without `_planning/`. | Keeps GROWI optional; the folder alone is a complete deliverable. |
| D6 | **Document identity = raw-relative path** `f1/a_docx.md`. That string is `original_document_name` in the store, the `delete_document` key, and the `sources` row. | Stable across versions, unique per project, human-readable. |
| D7 | **Wiki folder name** for `f1/a_docx.md` is `wiki/f1/a.docx/` (last `_` before the extension becomes `.`). | What the manager described; the raw name is derived from it and back. |
| D8 | **`M` in `wiki` mode is incremental only when line count is unchanged**; otherwise a full re-run of that file. Other modes always rewrite the file. | Incremental correctness rests on line numbers; when they shift, re-plan. |
| D9 | **neo keeps its own state** at `metadata/state/<rel-dir>/<name>/` (the run root), and `wiki/` only receives published pages. | Resume and incremental updates need `state/`; publishing needs none of it. |
| D10 | **Conversion `mount/ → raw/` is a separate, idempotent step**, tracked in `metadata/convert.json`, done by the **doc-parser** service (`/mnt/common/Code/doc-parser`, `POST /parse`). It sniffs the format itself (pdf, docx, xlsx, pptx, csv) and answers 415 for anything else; we record 415s as unsupported and move on. | One endpoint, one contract the owner says stays stable; nothing to invent on our side. |

---

## Hard constraints

- **C1 — Additive to `librarian.py`, `store.py`, `app.py`.** New methods, new job types, new
  endpoints, new settings. No existing method rewritten except the two `chunk_and_ingest`
  lines named in WP-S1 and the one-line `docs_dir` fallback in WP-S3.
- **C2 — Defaults unchanged.** `ingest_mode="chunks"`, `WIKI_DATA_ROOT` unset ⇒ everything
  behaves exactly as today (`DB_DIR/<db>.sqlite`, uploads through `/api/document`).
- **C3 — `researcher.py`, `realtime.py`, `vocab.py`, `neighborhood.py`, `gateway.py`,
  `growi.py`, `registry.py`, `vectors.py`: zero diff.**
- **C4 — Sync never leaves a half state.** `metadata/last_sha` is written only after every
  change in the batch succeeded. A failed file stops the batch; the next run redoes it.
- **C5 — Nothing writes into `mount/`.** Ever. Open it read-only; if a converter needs a
  scratch copy, copy into `metadata/work/`.
- **C6 — Tests green before every commit** (whole suite, loop command above).
- **C7 — Type the code as given.** Mismatch with the real file ⇒ stop and report.

---

## Reference — folder contract

```
data/<project>/                       one per URL segment {PREFIX}/<project>/
  mount/                              read-only bind mount from the file server (manager)
    f1/a.docx  f2/a.xlsx  f2/b.pptx  f3/c.pdf
  raw/                                git repo; only the converter (or the manager) writes here
    .git/
    f1/a_docx.md  f2/a_xlsx.md  f2/b_pptx.md  f3/c_pdf.md
  metadata/                           never git-tracked, never served
    last_sha                          40 hex chars: the raw/ commit the wiki reflects
    convert.json                      {"f1/a.docx": {"mtime": 1.7e9, "size": 1234, "raw_sha256": "…"}}
    state/f3/c.pdf/                   neo run root for that file (state/, source/, work/, wiki/)
    work/f3/c.pdf/                    scratch for the writer of that file; deleted after publish
  wiki/                               canonical output; GROWI/Obsidian are exports of this
    index.md                          links to every <rel>/<name>/ folder
    f3/c.pdf/
      001-概要.md 002-….md            pages (H1 first line; neo pages have no frontmatter)
      _planning/metadata.json         {"original_file_name": "f3/c_pdf.md", "files": [...]}
      _planning/coverage.json         {"source_line_count": N, "files": [{title, filename, summary, header, source_start, source_end}]}
      _planning/manifest.json         {"planning": {"ingest_mode": "pages"|"chunks", "strategy": ...}, "files": [...]}
  graph.sqlite                        this project's store (librarian/researcher)
```

`_planning/` is the existing `ingest_md_output` contract (see `graph/pages.py:write_pages_output`
and `graph/librarian.py:_load_new_planning_docs_output`). One quirk to respect: the loader
looks coverage up by **canonical** name (`001-x.md` → `x.md`, `_NUMBERED_DOC_RE`), so
`coverage.files[].filename` and `metadata.files[].name` must be the **prefix-stripped** name.

Flow:

```
POST /api/sync  ──► WriteJob "sync_project" ──► Librarian.sync_project
                                                    │
                                                    ├─ convert_mount(project)          WP-S5   mount/ → raw/ via doc-parser POST /parse (+ git commit)
                                                    ├─ plan_changes(project)           WP-S4   git diff last_sha..HEAD
                                                    └─ for each change:
                                                         D: rmtree wiki/<...>, rmtree metadata/state/<...>, delete_document(rel)
                                                         A: write_wiki(project, rel, mode) ──► build_wiki_output ──► chunks|pages|wiki
                                                         M: wiki mode → invalidate_pages(hunks) then write_wiki (resume); else as A
                                                            then ingest_md_output(wiki/<...>, raw_source_path=raw/<rel>)
                                                       write metadata/last_sha, wiki/index.md
GET  /api/wiki.zip ──► zip of wiki/ without _planning/
POST /api/growi/publish (existing) ──► pages read from wiki/
```

---

## WP-S0 — Baseline

**Goal:** know the starting state.

**Steps:**

1. `git status` clean, on branch `growi` (or the branch where `PLAN_NEO` landed). Create
   `git checkout -b data-sync`.
2. Run the whole suite; note any pre-existing `FAILED` lines.
3. Confirm `git --version` works in the venv's environment (sync shells out to git).

**Verify:** suite green (or the noted exceptions), git present.

---

## WP-S1 — `ingest_mode="wiki"`: neo runs from the librarian, output is ingestable

**Goal:** an upload through the existing `/api/document` path with `ingest_mode=wiki` runs
neo and ingests its pages, exactly like `pages` mode does today. This is also the seam
every later package uses (`build_wiki_output`).

**Files:** `graph/core.py`, `graph/neo/config.py`, `graph/neo/pipeline.py`, new
`graph/neo/export.py`, new `graph/writers.py`, `graph/librarian.py`,
new `tests/test_neo_export.py`, new `tests/test_writers.py`.

**Steps:**

1. `graph/core.py`, class `Settings`: change

   ```python
       ingest_mode: Literal["chunks", "pages"] = "chunks"
   ```

   to

   ```python
       ingest_mode: Literal["chunks", "pages", "wiki"] = "chunks"
   ```

   and add, right after the `page_stitch_concurrency` line:

   ```python
       # wiki mode (neo): lossless section-wise rewrite, see graph/neo
       wiki_section_target_lines: int = 80
       wiki_write_attempts: int = 3
       wiki_rewrite_concurrency: int = 4
       wiki_output_language: str = "Japanese (日本語)"
       # project data folder (WP-S2); empty = legacy single-sqlite layout
       data_root: str = ""
   ```

   If `Settings.from_env` builds from an explicit field list rather than `dataclasses.fields`,
   add the five names there too (read it; do not guess).

2. `graph/neo/config.py`, class `NeoConfig`: add after `document_slug: str = ""`:

   ```python
       # When set, the run root is exactly this directory (no <slug>-<hash> suffix), so a
       # changed source keeps its state directory and can resume or update incrementally.
       run_dir: str = ""
   ```

3. `graph/neo/pipeline.py`, in `run_pipeline`, replace

   ```python
       run_root = Path(config.output_root) / f"{slug}-{sha256_text(source_text)[:12]}"
   ```

   with

   ```python
       run_root = (
           Path(config.run_dir).resolve()
           if config.run_dir
           else Path(config.output_root) / f"{slug}-{sha256_text(source_text)[:12]}"
       )
   ```

   (`slug` stays computed; it is still used by `_prepare…`-free code paths for naming — if
   it becomes unused after PLAN_NEO WP-7, leave it, it is one line.)

4. Create `graph/neo/export.py`:

   ```python
   """Turn one neo run into the ``docs/`` + ``_planning/`` layout ``ingest_md_output`` reads."""

   from __future__ import annotations

   import re
   import shutil
   from pathlib import Path
   from typing import Any

   from .storage import read_json, write_json_atomic

   _PREFIX_RE = re.compile(r"^\d+-(.+\.md)$")


   def canonical_name(filename: str) -> str:
       """``001-title.md`` → ``title.md`` — what the librarian keys coverage by."""

       match = _PREFIX_RE.match(filename)
       return match.group(1) if match else filename


   def export_ingest_layout(run_root: Path, dest: Path, *, document_name: str) -> Path:
       """Copy published pages and write planning JSON. ``dest`` is recreated."""

       run_root = Path(run_root)
       plan = read_json(run_root / "state" / "plan.json")
       manifest = read_json(run_root / "state" / "manifest.json", default={})
       by_number = {int(item["number"]): item for item in manifest.get("pages", [])}

       dest = Path(dest)
       if dest.exists():
           shutil.rmtree(dest)
       docs = dest / "docs"
       planning = dest / "_planning"
       docs.mkdir(parents=True)
       planning.mkdir(parents=True)

       coverage: list[dict[str, Any]] = []
       metadata: list[dict[str, Any]] = []
       files: list[dict[str, Any]] = []
       for page in plan["pages"]:
           filename = page["filename"]
           source = run_root / "wiki" / filename
           if not source.exists():
               raise FileNotFoundError(f"neo run has no published page {filename}")
           shutil.copyfile(source, docs / filename)
           owner = page["owner_ranges"]
           header = page.get("chapter") or "一般"
           name = canonical_name(filename)
           coverage.append(
               {
                   "title": page["title"],
                   "filename": name,
                   "summary": page.get("summary", ""),
                   "header": header,
                   "source_start": owner[0][0],
                   "source_end": owner[-1][1],
               }
           )
           metadata.append({"name": name, "header": header})
           files.append(
               {
                   "filename": filename,
                   "title": page["title"],
                   "source_ranges": owner,
                   "reference_ranges": page.get("reference_ranges", []),
                   "judge_score": by_number.get(int(page["number"]), {}).get("judge_score"),
                   "verbatim_sections": by_number.get(int(page["number"]), {}).get(
                       "verbatim_sections", []
                   ),
               }
           )

       write_json_atomic(
           planning / "metadata.json",
           {
               "original_file_name": document_name,
               "inferred_file_name": document_name,
               "files": metadata,
           },
       )
       write_json_atomic(
           planning / "coverage.json",
           {
               "source_line_count": plan["source_line_count"],
               "file_count": len(coverage),
               "files": coverage,
           },
       )
       write_json_atomic(
           planning / "manifest.json",
           {
               "source": plan.get("source", ""),
               "source_sha256": plan.get("source_sha256", ""),
               "planning": {
                   "ingest_mode": "pages",
                   "strategy": "neo",
                   "file_count": len(files),
                   "prompt_version": plan.get("prompt_version", ""),
               },
               "files": files,
           },
       )
       review = run_root / "wiki" / "_review.md"
       if review.exists():
           shutil.copyfile(review, planning / "_review.md")
       return dest
   ```

   `"ingest_mode": "pages"` is deliberate: it makes the loader create `NodeType.page`
   nodes, which is what `pages` mode already does and what GROWI publishing expects.

5. Create `graph/writers.py` — the single place that knows the three modes:

   ```python
   """One function per writer mode; all produce the ``docs/`` + ``_planning/`` layout."""

   from __future__ import annotations

   import asyncio
   import shutil
   from pathlib import Path
   from types import SimpleNamespace
   from typing import Any, Callable

   StopCheck = Callable[[], bool] | None
   Progress = Callable[[dict[str, Any]], None] | None


   def neo_config(settings: Any, *, run_dir: Path, resume: bool = True):
       from .neo.config import NeoConfig

       return NeoConfig(
           chat_base_url=settings.chat_base_url,
           chat_api_key=settings.chat_api_key,
           chat_model=settings.chat_model,
           temperature=0.0,
           output_language=getattr(settings, "wiki_output_language", "Japanese (日本語)"),
           section_target_lines=int(getattr(settings, "wiki_section_target_lines", 80)),
           write_attempts=int(getattr(settings, "wiki_write_attempts", 3)),
           rewrite_concurrency=int(getattr(settings, "wiki_rewrite_concurrency", 4)),
           run_dir=str(run_dir),
           resume=resume,
       )


   def run_neo(
       source_path: Path,
       *,
       run_dir: Path,
       settings: Any,
       on_progress: Progress = None,
       stop_check: StopCheck = None,
       resume: bool = True,
   ) -> Path:
       """Run the neo pipeline synchronously; returns the run root."""

       from .chunk import _run_async_blocking
       from .neo.pipeline import run_pipeline

       config = neo_config(settings, run_dir=run_dir, resume=resume)
       return _run_async_blocking(
           run_pipeline(source_path, config=config, on_progress=on_progress, stop_check=stop_check)
       )


   def build_wiki_output(
       *,
       source_path: Path,
       document_name: str,
       out_dir: Path,
       mode: str,
       settings: Any,
       llm: Any,
       embedder: Any,
       state_dir: Path | None = None,
       on_progress: Progress = None,
       stop_check: StopCheck = None,
   ) -> SimpleNamespace:
       """Write ``out_dir/docs`` + ``out_dir/_planning`` from one Markdown source.

       ``state_dir`` is where the ``wiki`` writer keeps its resumable run; the
       other writers are stateless.  Returns ``SimpleNamespace(out_dir, file_count)``.
       """

       source_path = Path(source_path)
       out_dir = Path(out_dir)
       if mode == "wiki":
           from .neo.export import export_ingest_layout

           run_root = run_neo(
               source_path,
               run_dir=state_dir or (out_dir / "neo"),
               settings=settings,
               on_progress=on_progress,
               stop_check=stop_check,
           )
           export_ingest_layout(run_root, out_dir, document_name=document_name)
           return SimpleNamespace(
               out_dir=out_dir, file_count=len(list((out_dir / "docs").glob("*.md")))
           )

       body = source_path.read_text(encoding="utf-8")
       if mode == "pages":
           from .pages import run_pages_pipeline

           return run_pages_pipeline(
               source_text=body,
               document_name=document_name,
               out_dir=out_dir,
               llm=llm,
               embedder=embedder,
               settings=settings,
               on_progress=on_progress,
               stop_check=stop_check,
           )

       from .chunk import run_chunk_pipeline

       return run_chunk_pipeline(
           source_text=body,
           document_name=document_name,
           out_dir=out_dir,
           llm=llm,
           concurrency=max(1, int(getattr(settings, "ingest_concurrency", 4))),
           on_progress=on_progress,
           stop_check=stop_check,
       )
   ```

   Check `_run_async_blocking` exists in `graph/chunk.py` (it is what
   `run_pages_pipeline` uses). If its name differs, use the exact name — do not write a
   new event-loop helper.

6. `graph/librarian.py`, method `chunk_and_ingest`: replace the block

   ```python
           if mode == "pages":
               from .pages import run_pages_pipeline

               result = run_pages_pipeline(
                   ...
               )
           else:
               result = run_chunk_pipeline(
                   ...
               )
   ```

   with

   ```python
           from .writers import build_wiki_output

           result = build_wiki_output(
               source_path=raw_source_path,
               document_name=document_name,
               out_dir=out_dir,
               mode=mode,
               settings=settings,
               llm=llm,
               embedder=self.gateway.embedder,
               state_dir=Path(settings.database_path).parent / "neo" / out_dir.name,
               on_progress=on_progress,
               stop_check=stop_check,
           )
   ```

   (`raw_source_path` is already written a few lines above; `body` stays used by the
   `document_name` fallback.) Remove the now-unused `run_chunk_pipeline` name from the
   `from .chunk import make_llm, run_chunk_pipeline` line at the top of the method.

   The `finally: shutil.rmtree(result.out_dir)` at the end stays — for `wiki` mode the
   resumable state lives in `state_dir`, not in `out_dir`, so deleting `out_dir` is still
   correct.

7. Tests.

   `tests/test_neo_export.py`:

   ```python
   import json, tempfile, unittest
   from pathlib import Path

   from graph.neo.export import canonical_name, export_ingest_layout


   class ExportTests(unittest.TestCase):
       def test_canonical_name_strips_numeric_prefix(self) -> None:
           self.assertEqual(canonical_name("001-概要.md"), "概要.md")
           self.assertEqual(canonical_name("readme.md"), "readme.md")

       def test_export_writes_loader_contract(self) -> None:
           with tempfile.TemporaryDirectory() as directory:
               run = Path(directory) / "run"
               (run / "state").mkdir(parents=True)
               (run / "wiki").mkdir()
               (run / "wiki" / "001-概要.md").write_text("# 概要\n\n本文\n", encoding="utf-8")
               (run / "state" / "plan.json").write_text(json.dumps({
                   "source": "x.md", "source_sha256": "abc", "source_line_count": 10,
                   "prompt_version": "v", "pages": [{
                       "number": 1, "title": "概要", "chapter": "導入", "summary": "説明",
                       "filename": "001-概要.md", "owner_ranges": [[1, 10]], "reference_ranges": []}]}),
                   encoding="utf-8")
               (run / "state" / "manifest.json").write_text(json.dumps({"pages": [
                   {"number": 1, "judge_score": 95, "verbatim_sections": []}]}), encoding="utf-8")
               dest = Path(directory) / "out"
               export_ingest_layout(run, dest, document_name="f1/x_docx.md")

               self.assertTrue((dest / "docs" / "001-概要.md").exists())
               coverage = json.loads((dest / "_planning" / "coverage.json").read_text(encoding="utf-8"))
               self.assertEqual(coverage["files"][0]["filename"], "概要.md")
               self.assertEqual(coverage["files"][0]["source_start"], 1)
               self.assertEqual(coverage["files"][0]["source_end"], 10)
               metadata = json.loads((dest / "_planning" / "metadata.json").read_text(encoding="utf-8"))
               self.assertEqual(metadata["original_file_name"], "f1/x_docx.md")
               manifest = json.loads((dest / "_planning" / "manifest.json").read_text(encoding="utf-8"))
               self.assertEqual(manifest["planning"]["ingest_mode"], "pages")
               self.assertEqual(manifest["files"][0]["judge_score"], 95)
   ```

   `tests/test_writers.py` — proves `build_wiki_output` routes by mode without any model:

   ```python
   import tempfile, unittest
   from pathlib import Path
   from types import SimpleNamespace
   from unittest import mock

   from graph import writers


   class BuildWikiOutputTests(unittest.TestCase):
       def test_wiki_mode_runs_neo_then_exports(self) -> None:
           with tempfile.TemporaryDirectory() as directory:
               root = Path(directory)
               source = root / "a.md"
               source.write_text("# a\n", encoding="utf-8")
               with mock.patch.object(writers, "run_neo", return_value=root / "run") as run_neo, \
                    mock.patch("graph.neo.export.export_ingest_layout") as export:
                   (root / "out" / "docs").mkdir(parents=True)
                   (root / "out" / "docs" / "001-a.md").write_text("# a\n", encoding="utf-8")
                   result = writers.build_wiki_output(
                       source_path=source, document_name="a.md", out_dir=root / "out",
                       mode="wiki", settings=SimpleNamespace(), llm=None, embedder=None,
                       state_dir=root / "state",
                   )
               run_neo.assert_called_once()
               self.assertEqual(run_neo.call_args.kwargs["run_dir"], root / "state")
               export.assert_called_once_with(root / "run", root / "out", document_name="a.md")
               self.assertEqual(result.file_count, 1)

       def test_neo_config_maps_settings(self) -> None:
           settings = SimpleNamespace(chat_base_url="u", chat_api_key="k", chat_model="m",
                                      wiki_section_target_lines=50, wiki_write_attempts=2,
                                      wiki_rewrite_concurrency=1, wiki_output_language="ja")
           config = writers.neo_config(settings, run_dir=Path("/tmp/x"))
           self.assertEqual((config.chat_model, config.section_target_lines, config.write_attempts,
                             config.rewrite_concurrency, config.run_dir), ("m", 50, 2, 1, "/tmp/x"))
   ```

   And one librarian-level test in the existing `tests/test_pages_wire.py` style: enqueue a
   `chunk_and_ingest` job with `chunk_options={"ingest_mode": "wiki"}` while
   `graph.writers.build_wiki_output` is patched to write a minimal `docs/` + `_planning/`
   (copy the fixture from `test_export_writes_loader_contract`), and assert
   `store.get_nodes_by_document(document_name)` has one node of type `page`. Look at how
   `test_pages_wire.py` builds its `Librarian` and copy that setup.

**Verify:**

```bash
cd llm-wiki-dist && .venv/bin/python -m unittest tests.test_neo_export tests.test_writers tests.test_pages_wire tests.test_neo_chunking
.venv/bin/python -c "from graph.core import Settings; s=Settings(); print(s.ingest_mode, s.wiki_write_attempts)"
```

Expected: OK; prints `chunks 3`.

**Revert:** `git checkout -- graph/ && rm graph/writers.py graph/neo/export.py tests/test_neo_export.py tests/test_writers.py`.

---

## WP-S2 — The project folder: `graph/project.py` and `WIKI_DATA_ROOT`

**Goal:** one small module knows every path; the app picks `DATA_ROOT/<db>/graph.sqlite`
when `WIKI_DATA_ROOT` is set and behaves exactly as before when it is not.

**Files:** new `graph/project.py`, `app.py`, new `tests/test_project.py`.

**Steps:**

1. Create `graph/project.py`:

   ```python
   """Paths of one project's data folder. Pure; nothing here touches the network."""

   from __future__ import annotations

   from dataclasses import dataclass
   from pathlib import Path, PurePosixPath


   def wiki_folder_name(raw_name: str) -> str:
       """``a_docx.md`` → ``a.docx``; ``notes.md`` → ``notes``."""

       stem = PurePosixPath(raw_name).stem
       base, sep, ext = stem.rpartition("_")
       return f"{base}.{ext}" if sep and base and ext.isalnum() else stem


   def raw_name_for(mount_name: str) -> str:
       """``a.docx`` → ``a_docx.md`` (inverse of :func:`wiki_folder_name`)."""

       path = PurePosixPath(mount_name)
       ext = path.suffix.lstrip(".").lower()
       return f"{path.stem}_{ext}.md" if ext else f"{path.stem}.md"


   @dataclass(frozen=True)
   class Project:
       root: Path

       @property
       def mount(self) -> Path:
           return self.root / "mount"

       @property
       def raw(self) -> Path:
           return self.root / "raw"

       @property
       def metadata(self) -> Path:
           return self.root / "metadata"

       @property
       def wiki(self) -> Path:
           return self.root / "wiki"

       @property
       def database(self) -> Path:
           return self.root / "graph.sqlite"

       @property
       def last_sha_path(self) -> Path:
           return self.metadata / "last_sha"

       @property
       def convert_log_path(self) -> Path:
           return self.metadata / "convert.json"

       def raw_file(self, rel: str) -> Path:
           return self.raw / rel

       def wiki_dir(self, rel: str) -> Path:
           """``f1/a_docx.md`` → ``wiki/f1/a.docx``."""

           path = PurePosixPath(rel)
           return self.wiki / path.parent / wiki_folder_name(path.name)

       def state_dir(self, rel: str) -> Path:
           path = PurePosixPath(rel)
           return self.metadata / "state" / path.parent / wiki_folder_name(path.name)

       def work_dir(self, rel: str) -> Path:
           path = PurePosixPath(rel)
           return self.metadata / "work" / path.parent / wiki_folder_name(path.name)

       def ensure(self) -> "Project":
           for directory in (self.raw, self.metadata, self.wiki):
               directory.mkdir(parents=True, exist_ok=True)
           return self

       def raw_files(self) -> list[str]:
           """Every tracked-looking Markdown file under raw/, as raw-relative POSIX paths."""

           return sorted(
               path.relative_to(self.raw).as_posix()
               for path in self.raw.rglob("*.md")
               if ".git" not in path.parts
           )
   ```

2. `app.py`: next to `DB_DIR = …` add

   ```python
   DATA_ROOT = Path(os.environ["WIKI_DATA_ROOT"]).resolve() if os.environ.get("WIKI_DATA_ROOT") else None
   ```

   and change `_db_path`:

   ```python
   def _db_path(db: str) -> Path:
       if DATA_ROOT is not None:
           from graph.project import Project

           return Project(DATA_ROOT / db).ensure().database
       return DB_DIR / f"{db}.sqlite"
   ```

   In `_build_stack`, after `settings.database_path = db_path`, add:

   ```python
       if DATA_ROOT is not None:
           settings.data_root = str(Path(db_path).parent)
   ```

   Wherever the admin panel lists databases by globbing `DB_DIR/*.sqlite`, add the
   `DATA_ROOT` branch: list directories under `DATA_ROOT` that contain `graph.sqlite` **or**
   a `raw/` folder. Find that code with `grep -n "glob(\"\*.sqlite\")" app.py`. Wherever it
   creates a database (`POST /admin/api/dbs/{db}`), the `_db_path()` call above already
   creates the folder — confirm no second `DB_DIR` reference remains in that handler.

3. `tests/test_project.py`:

   ```python
   import tempfile, unittest
   from pathlib import Path

   from graph.project import Project, raw_name_for, wiki_folder_name


   class ProjectPathTests(unittest.TestCase):
       def test_names_round_trip(self) -> None:
           self.assertEqual(wiki_folder_name("a_docx.md"), "a.docx")
           self.assertEqual(wiki_folder_name("my_file_pptx.md"), "my_file.pptx")
           self.assertEqual(wiki_folder_name("notes.md"), "notes")
           self.assertEqual(raw_name_for("a.docx"), "a_docx.md")
           self.assertEqual(raw_name_for("Report.PDF"), "Report_pdf.md")
           self.assertEqual(wiki_folder_name(raw_name_for("f/b.xlsx".split("/")[-1])), "b.xlsx")

       def test_paths(self) -> None:
           with tempfile.TemporaryDirectory() as directory:
               project = Project(Path(directory)).ensure()
               self.assertEqual(project.wiki_dir("f1/a_docx.md"), project.wiki / "f1" / "a.docx")
               self.assertEqual(project.state_dir("f1/a_docx.md"), project.metadata / "state" / "f1" / "a.docx")
               (project.raw / "f1").mkdir()
               (project.raw / "f1" / "a_docx.md").write_text("x", encoding="utf-8")
               (project.raw / ".git").mkdir()
               (project.raw / ".git" / "junk.md").write_text("x", encoding="utf-8")
               self.assertEqual(project.raw_files(), ["f1/a_docx.md"])
   ```

**Verify:**

```bash
cd llm-wiki-dist && .venv/bin/python -m unittest tests.test_project
WIKI_DATA_ROOT=/tmp/wikidata .venv/bin/python -c "import app; print(app._db_path('demo'))"
ls /tmp/wikidata/demo
```

Expected: `/tmp/wikidata/demo/graph.sqlite`; `ls` shows `metadata raw wiki`. Without the env
var, `_db_path('demo')` must still print `.wiki_docker/demo.sqlite` (or whatever `DB_DIR` is).

**Revert:** `git checkout -- app.py && rm graph/project.py tests/test_project.py`.

---

## WP-S3 — `write_wiki`: one file in, one wiki folder out

**Goal:** `write_wiki(project, rel, mode, …)` produces `wiki/<rel-dir>/<name>/` in the
contract layout for any mode, and the librarian loader accepts that folder.

**Files:** `graph/writers.py`, `graph/librarian.py` (one line), `tests/test_writers.py`.

**Steps:**

1. `graph/librarian.py`, `_load_new_planning_docs_output`: change

   ```python
           docs_dir = out_path / "docs"

           if not docs_dir.exists():
               raise FileNotFoundError(f"no docs directory found in {out_path}")
   ```

   to

   ```python
           docs_dir = out_path / "docs"
           if not docs_dir.exists() and any(out_path.glob("*.md")):
               # Published wiki folders keep pages at the top level and planning in _planning/.
               docs_dir = out_path

           if not docs_dir.exists():
               raise FileNotFoundError(f"no docs directory found in {out_path}")
   ```

   That is the only librarian edit in this package.

2. `graph/writers.py`, append:

   ```python
   def publish_output(staged: Path, target: Path) -> int:
       """Move ``docs/*.md`` to the top of ``target`` and keep ``_planning/`` beside them."""

       staged = Path(staged)
       target = Path(target)
       if target.exists():
           shutil.rmtree(target)
       target.mkdir(parents=True)
       count = 0
       for page in sorted((staged / "docs").glob("*.md")):
           shutil.copyfile(page, target / page.name)
           count += 1
       if (staged / "_planning").exists():
           shutil.copytree(staged / "_planning", target / "_planning")
       return count


   def write_wiki(
       project: Any,
       rel: str,
       *,
       mode: str,
       settings: Any,
       llm: Any,
       embedder: Any,
       on_progress: Progress = None,
       stop_check: StopCheck = None,
   ) -> Path:
       """raw/<rel> → wiki/<rel-dir>/<name>/. Scratch lives in metadata/work and is removed."""

       work = project.work_dir(rel)
       if work.exists():
           shutil.rmtree(work)
       work.mkdir(parents=True)
       try:
           result = build_wiki_output(
               source_path=project.raw_file(rel),
               document_name=rel,
               out_dir=work / "out",
               mode=mode,
               settings=settings,
               llm=llm,
               embedder=embedder,
               state_dir=project.state_dir(rel),
               on_progress=on_progress,
               stop_check=stop_check,
           )
           target = project.wiki_dir(rel)
           publish_output(result.out_dir, target)
           return target
       finally:
           shutil.rmtree(work, ignore_errors=True)


   def write_index(project: Any) -> None:
       """wiki/index.md: one link per published folder, in path order."""

       lines = ["# Wiki", ""]
       for planning in sorted(project.wiki.rglob("_planning/metadata.json")):
           folder = planning.parent.parent
           rel = folder.relative_to(project.wiki).as_posix()
           pages = sorted(page.name for page in folder.glob("*.md"))
           lines.append(f"## {rel}")
           lines.extend(f"- [{page[:-3]}]({rel}/{page})" for page in pages)
           lines.append("")
       (project.wiki / "index.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
   ```

3. `tests/test_writers.py`, add:

   ```python
   class WriteWikiTests(unittest.TestCase):
       def test_write_wiki_publishes_flat_pages_and_planning(self) -> None:
           from graph.project import Project

           def fake_build(**kwargs):
               out = Path(kwargs["out_dir"])
               (out / "docs").mkdir(parents=True)
               (out / "_planning").mkdir()
               (out / "docs" / "001-a.md").write_text("# a\n", encoding="utf-8")
               (out / "_planning" / "metadata.json").write_text("{}", encoding="utf-8")
               return SimpleNamespace(out_dir=out, file_count=1)

           with tempfile.TemporaryDirectory() as directory:
               project = Project(Path(directory)).ensure()
               (project.raw / "f1").mkdir()
               (project.raw / "f1" / "a_docx.md").write_text("# a\n", encoding="utf-8")
               with mock.patch.object(writers, "build_wiki_output", side_effect=fake_build):
                   target = writers.write_wiki(project, "f1/a_docx.md", mode="chunks",
                                               settings=SimpleNamespace(), llm=None, embedder=None)
               self.assertEqual(target, project.wiki / "f1" / "a.docx")
               self.assertTrue((target / "001-a.md").exists())
               self.assertTrue((target / "_planning" / "metadata.json").exists())
               self.assertFalse((target / "docs").exists())
               self.assertFalse(project.work_dir("f1/a_docx.md").exists())
               writers.write_index(project)
               index = (project.wiki / "index.md").read_text(encoding="utf-8")
               self.assertIn("[001-a](f1/a.docx/001-a.md)", index)
   ```

   And a loader test in `tests/test_pages_wire.py` (or a new `tests/test_flat_loader.py`):
   build the same flat folder (`001-a.md` + `_planning/{metadata,coverage,manifest}.json`
   from the WP-S1 fixture) and assert `Librarian._load_md_output(folder)` returns one node
   titled from coverage. This proves the one-line loader change.

**Verify:** `unittest tests.test_writers tests.test_pages_wire` OK.

**Revert:** `git checkout -- graph/writers.py graph/librarian.py tests/`.

---

## WP-S4 — Sync: `graph/sync.py`, job `sync_project`, `POST /api/sync`

**Goal:** `git diff last_sha..HEAD -- raw/` drives add/modify/delete; each change writes the
wiki folder and (re)ingests it; `last_sha` moves only when the whole batch succeeded.

**Files:** new `graph/sync.py`, `graph/librarian.py`, `app.py`, new `tests/test_sync.py`.

**Steps:**

1. Create `graph/sync.py`:

   ```python
   """git-driven sync of raw/ → wiki/ → graph. Only ``sync_project`` has side effects."""

   from __future__ import annotations

   import logging
   import re
   import shutil
   import subprocess
   from dataclasses import dataclass
   from pathlib import Path
   from typing import Any, Callable

   from .project import Project

   log = logging.getLogger(__name__)

   StopCheck = Callable[[], bool] | None
   Progress = Callable[[dict[str, Any]], None] | None

   _HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


   @dataclass(frozen=True)
   class Change:
       status: str  # "A" | "M" | "D"
       rel: str     # raw-relative POSIX path, e.g. "f1/a_docx.md"


   def git(project: Project, *args: str) -> str:
       result = subprocess.run(
           ["git", "-C", str(project.raw), *args],
           check=True,
           capture_output=True,
           text=True,
       )
       return result.stdout


   def head_sha(project: Project) -> str:
       return git(project, "rev-parse", "HEAD").strip()


   def last_sha(project: Project) -> str | None:
       path = project.last_sha_path
       if not path.exists():
           return None
       value = path.read_text(encoding="utf-8").strip()
       return value or None


   def plan_changes(project: Project) -> list[Change]:
       """Everything that changed in raw/ since the last successful sync."""

       previous = last_sha(project)
       if previous is None:
           return [Change("A", rel) for rel in project.raw_files()]
       if previous == head_sha(project):
           return []
       changes: list[Change] = []
       for line in git(
           project, "diff", "--name-status", "--no-renames", f"{previous}..HEAD", "--", "."
       ).splitlines():
           status, _, rel = line.partition("\t")
           status = status[:1]
           if status not in ("A", "M", "D") or not rel.endswith(".md"):
               continue
           changes.append(Change(status, rel))
       return changes


   def changed_hunks(project: Project, rel: str) -> list[tuple[int, int, int, int]]:
       """(old_start, old_len, new_start, new_len) for every hunk of one modified file."""

       previous = last_sha(project)
       if previous is None:
           return []
       hunks: list[tuple[int, int, int, int]] = []
       for line in git(project, "diff", "-U0", f"{previous}..HEAD", "--", rel).splitlines():
           match = _HUNK_RE.match(line)
           if match:
               old_start, old_len, new_start, new_len = match.groups()
               hunks.append(
                   (int(old_start), int(old_len or 1), int(new_start), int(new_len or 1))
               )
       return hunks


   def sync_project(
       project: Project,
       librarian: Any,
       *,
       mode: str,
       settings: Any,
       llm: Any,
       embedder: Any,
       on_progress: Progress = None,
       stop_check: StopCheck = None,
   ) -> dict[str, Any]:
       """Apply every change; move last_sha only when all of them succeeded."""

       from .writers import write_index, write_wiki

       def emit(**event: Any) -> None:
           if on_progress:
               on_progress(event)

       def stop() -> bool:
           return bool(stop_check and stop_check())

       changes = plan_changes(project)
       head = head_sha(project)
       done: list[dict[str, Any]] = []
       for index, change in enumerate(changes, start=1):
           if stop():
               raise RuntimeError("sync cancelled")
           emit(stage="sync", step=change.status, current=index, total=len(changes), file=change.rel)
           if change.status == "D":
               shutil.rmtree(project.wiki_dir(change.rel), ignore_errors=True)
               shutil.rmtree(project.state_dir(change.rel), ignore_errors=True)
               result = librarian.delete_document(change.rel)
               done.append({"file": change.rel, "status": "D", **result})
               continue
           if change.status == "M" and mode == "wiki":
               from .neo.incremental import invalidate_pages  # WP-S6; until then this import fails → see step 4

               invalidate_pages(
                   project.state_dir(change.rel),
                   changed_hunks(project, change.rel),
                   project.raw_file(change.rel).read_text(encoding="utf-8"),
               )
           target = write_wiki(
               project, change.rel, mode=mode, settings=settings, llm=llm, embedder=embedder,
               on_progress=lambda event, rel=change.rel: emit(stage="write", file=rel, **event),
               stop_check=stop_check,
           )
           nodes = librarian.ingest_md_output(
               target, stop_check=stop_check, raw_source_path=project.raw_file(change.rel)
           )
           done.append({"file": change.rel, "status": change.status, "pages": len(nodes)})
       write_index(project)
       project.metadata.mkdir(parents=True, exist_ok=True)
       project.last_sha_path.write_text(head + "\n", encoding="utf-8")
       return {"head": head, "changes": done}
   ```

2. `graph/librarian.py`: in `_dispatch_job`, before the `raise ValueError`, add

   ```python
           if job.type == "sync_project":
               return self.sync_project(job)
   ```

   and add the method next to `chunk_and_ingest`:

   ```python
       def sync_project(self, job: WriteJob) -> dict[str, Any]:
           """Bring wiki/ and the graph in line with raw/ (git) for this project."""
           from .chunk import make_llm
           from .project import Project
           from .sync import sync_project

           data_root = getattr(self.settings, "data_root", "")
           if not data_root:
               raise ValueError("sync_project needs WIKI_DATA_ROOT (Settings.data_root)")
           project = Project(Path(data_root)).ensure()
           settings = self.settings
           llm = make_llm(
               model=settings.chat_model,
               base_url=settings.chat_base_url,
               api_key=settings.chat_api_key,
               temperature=settings.chat_temperature,
           )

           def on_progress(update: dict[str, Any]) -> None:
               job.progress = update

           return sync_project(
               project,
               self,
               mode=str(job.payload.get("ingest_mode") or getattr(settings, "ingest_mode", "chunks")),
               settings=settings,
               llm=llm,
               embedder=self.gateway.embedder,
               on_progress=on_progress,
               stop_check=lambda: job.stop_event.is_set(),
           )
   ```

3. `app.py`, next to `POST /api/ingest`:

   ```python
   class SyncBody(BaseModel):
       ingest_mode: str | None = None


   @app.post("/api/sync")
   async def sync_project(payload: SyncBody | None = None) -> dict:
       if DATA_ROOT is None:
           raise HTTPException(
               status_code=400,
               detail=api_error("WIKI_DATA_ROOT is not configured", False, "no_data_root"),
           )
       return await _enqueue("sync_project", {"ingest_mode": (payload.ingest_mode if payload else None)})
   ```

   Put `SyncBody` with the other request models. If `_enqueue` takes a different shape,
   copy exactly what `/api/ingest` does.

4. Until WP-S6 exists, make the `M`-in-`wiki`-mode branch a full rewrite: wrap the
   `invalidate_pages` import in `try: … except ImportError: shutil.rmtree(project.state_dir(change.rel), ignore_errors=True)`.
   WP-S6 removes the `try`.

5. `tests/test_sync.py` — uses a real temporary git repo and a fake librarian:

   ```python
   import subprocess, tempfile, unittest
   from pathlib import Path
   from types import SimpleNamespace
   from unittest import mock

   from graph import sync
   from graph.project import Project


   def git(repo: Path, *args: str) -> None:
       subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                      env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"})


   class FakeLibrarian:
       def __init__(self) -> None:
           self.deleted: list[str] = []
           self.ingested: list[str] = []

       def delete_document(self, name):
           self.deleted.append(name)
           return {"deleted": 1}

       def ingest_md_output(self, path, stop_check=None, raw_source_path=None, concurrency=None):
           self.ingested.append(Path(path).relative_to(Path(path).parents[2]).as_posix())
           return [object()]


   class SyncTests(unittest.TestCase):
       def setUp(self) -> None:
           self.tmp = tempfile.TemporaryDirectory()
           self.project = Project(Path(self.tmp.name)).ensure()
           git(self.project.raw, "init", "-q")
           (self.project.raw / "f1").mkdir()
           (self.project.raw / "f1" / "a_docx.md").write_text("# a\nline\n", encoding="utf-8")
           git(self.project.raw, "add", ".")
           git(self.project.raw, "commit", "-qm", "one")

       def tearDown(self) -> None:
           self.tmp.cleanup()

       def fake_write(self, project, rel, **kwargs):
           target = project.wiki_dir(rel)
           target.mkdir(parents=True, exist_ok=True)
           (target / "001-a.md").write_text("# a\n", encoding="utf-8")
           (target / "_planning").mkdir(exist_ok=True)
           (target / "_planning" / "metadata.json").write_text("{}", encoding="utf-8")
           return target

       def test_first_sync_adds_everything_and_records_head(self) -> None:
           self.assertEqual(sync.plan_changes(self.project), [sync.Change("A", "f1/a_docx.md")])
           librarian = FakeLibrarian()
           with mock.patch("graph.writers.write_wiki", side_effect=self.fake_write):
               result = sync.sync_project(self.project, librarian, mode="chunks",
                                          settings=SimpleNamespace(), llm=None, embedder=None)
           self.assertEqual(librarian.ingested, ["f1/a.docx"])
           self.assertEqual(sync.last_sha(self.project), sync.head_sha(self.project))
           self.assertEqual(result["changes"][0]["status"], "A")
           self.assertTrue((self.project.wiki / "index.md").exists())
           self.assertEqual(sync.plan_changes(self.project), [])

       def test_modify_and_delete_are_detected_from_git(self) -> None:
           self.test_first_sync_adds_everything_and_records_head()
           (self.project.raw / "f1" / "a_docx.md").write_text("# a\nchanged\n", encoding="utf-8")
           (self.project.raw / "f1" / "b_pdf.md").write_text("# b\n", encoding="utf-8")
           git(self.project.raw, "add", ".")
           git(self.project.raw, "commit", "-qm", "two")
           self.assertEqual(sorted(c.status + " " + c.rel for c in sync.plan_changes(self.project)),
                            ["A f1/b_pdf.md", "M f1/a_docx.md"])
           self.assertEqual(sync.changed_hunks(self.project, "f1/a_docx.md"), [(2, 1, 2, 1)])
           git(self.project.raw, "rm", "-q", "f1/a_docx.md")
           git(self.project.raw, "commit", "-qm", "three")
           librarian = FakeLibrarian()
           with mock.patch("graph.writers.write_wiki", side_effect=self.fake_write):
               sync.sync_project(self.project, librarian, mode="chunks",
                                 settings=SimpleNamespace(), llm=None, embedder=None)
           self.assertEqual(librarian.deleted, ["f1/a_docx.md"])
           self.assertFalse(self.project.wiki_dir("f1/a_docx.md").exists())
           self.assertEqual(librarian.ingested, ["f1/b.pdf"])

       def test_failed_write_does_not_move_last_sha(self) -> None:
           librarian = FakeLibrarian()
           with mock.patch("graph.writers.write_wiki", side_effect=RuntimeError("boom")):
               with self.assertRaises(RuntimeError):
                   sync.sync_project(self.project, librarian, mode="chunks",
                                     settings=SimpleNamespace(), llm=None, embedder=None)
           self.assertIsNone(sync.last_sha(self.project))
   ```

   (`sync_project` imports `write_wiki` from `.writers` inside the function, so patching
   `graph.writers.write_wiki` works.)

**Verify:** `unittest tests.test_sync` OK (3 tests); whole suite green. Then by hand:

```bash
export WIKI_DATA_ROOT=/tmp/wikidata
mkdir -p /tmp/wikidata/demo/raw/f1 && cd /tmp/wikidata/demo/raw && git init -q
cp <some 300-line markdown> f1/manual_pdf.md && git add . && git commit -qm init
# start the app as usual, then:
curl -X POST http://localhost:8000/<PREFIX>/demo/api/sync -H 'content-type: application/json' -d '{"ingest_mode":"chunks"}'
```

Expected: job completes; `/tmp/wikidata/demo/wiki/f1/manual.pdf/` has `NN-*.md` + `_planning/`;
`metadata/last_sha` equals `git -C raw rev-parse HEAD`; `/api/search` finds content.

**Revert:** `git checkout -- app.py graph/librarian.py && rm graph/sync.py tests/test_sync.py`.

---

## WP-S5 — Convert `mount/ → raw/` through doc-parser

**Goal:** an idempotent step that turns every file in `mount/` into Markdown in `raw/` and
commits, so sync has something to diff. The converter is the **doc-parser** service.

**The doc-parser contract** (`/mnt/common/Code/doc-parser/server.py`, verified 2026-09-12;
the owner says the endpoint stays stable even as the parsers change):

| | |
|---|---|
| Request | `POST {base}/parse?images=true&describe_images=true`, multipart field **`file`** |
| Headers (optional) | `X-LLM-Base-URL`, `X-LLM-API-Key`, `X-LLM-Model` — the vision model used for image descriptions; omit to use the server's `.env` |
| Format detection | by **bytes**, not extension (`formats.detect`); pdf, docx, xlsx, pptx, csv today |
| Response | JSON `{"markdown": str, "parser": str, "image_count": int, "duration_s": float, "meta": {}}` |
| Streaming | for slow parsers (PDF/MinerU) the body starts with whitespace heartbeats every 15 s, then one JSON value — `response.json()` still works; on failure the streamed body is `{"error": "..."}` with HTTP 200 |
| Errors | 400 empty file, **415 unsupported format**, 500 parse failed |
| Health | `GET {base}/health` → `{"status": "ok"}` |

**Files:** new `graph/convert.py`, `graph/sync.py` (one call), `graph/core.py` (one
setting), new `tests/test_convert.py`.

**Steps:**

1. `graph/core.py`, `Settings`: add `parser_base_url: str = ""` next to `data_root`.
   Empty = conversion disabled (the manager delivers `raw/` himself).

2. Create `graph/convert.py`:

   ```python
   """mount/ → raw/ conversion through doc-parser. Idempotent via metadata/convert.json."""

   from __future__ import annotations

   import json
   import logging
   from pathlib import Path
   from typing import Any, Callable

   import requests

   from .project import Project, raw_name_for
   from .sync import git

   log = logging.getLogger(__name__)

   # doc-parser sniffs bytes, so the only reason to skip by name is noise.
   SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


   class UnsupportedDocument(RuntimeError):
       """doc-parser answered 415: not a format it can parse."""


   def parse_document(path: Path, *, base_url: str, settings: Any, timeout_s: float = 7200) -> str:
       """One synchronous doc-parser call; returns the Markdown body."""

       headers = {
           key: value
           for key, value in {
               "X-LLM-Base-URL": getattr(settings, "chat_base_url", ""),
               "X-LLM-API-Key": getattr(settings, "chat_api_key", ""),
               "X-LLM-Model": getattr(settings, "chat_model", ""),
           }.items()
           if value
       }
       with path.open("rb") as handle:
           reply = requests.post(
               f"{base_url.rstrip('/')}/parse",
               params={"images": "true", "describe_images": "true"},
               headers=headers,
               files={"file": (path.name, handle)},
               # connect 30 s; the read timeout is per chunk and the server heartbeats
               # every 15 s, so 120 s means "no bytes at all for two minutes".
               timeout=(30, 120),
               stream=True,
           )
       if reply.status_code == 415:
           raise UnsupportedDocument(reply.text[:200])
       reply.raise_for_status()
       body = json.loads(reply.content)  # heartbeats are leading whitespace: valid JSON
       if "error" in body and "markdown" not in body:
           raise RuntimeError(f"doc-parser: {body['error']}")
       return str(body["markdown"])


   def _stat_key(path: Path) -> dict[str, Any]:
       stat = path.stat()
       return {"mtime": stat.st_mtime, "size": stat.st_size}


   def _raw_target(project: Project, rel: str) -> Path:
       return project.raw / Path(rel).parent / raw_name_for(Path(rel).name)


   def convert_mount(
       project: Project,
       *,
       parser_base_url: str,
       settings: Any,
       on_progress: Callable[[dict[str, Any]], None] | None = None,
   ) -> dict[str, Any]:
       """Convert new/changed mount files, drop raw files whose source vanished, commit."""

       log_path = project.convert_log_path
       seen: dict[str, Any] = (
           json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {}
       )
       converted: list[str] = []
       removed: list[str] = []
       unsupported: list[str] = []
       failed: list[str] = []
       present: set[str] = set()

       for path in sorted(project.mount.rglob("*")):
           if not path.is_file() or path.name in SKIP_NAMES or path.name.startswith("~$"):
               continue
           rel = path.relative_to(project.mount).as_posix()
           present.add(rel)
           key = _stat_key(path)
           previous = seen.get(rel, {})
           if previous.get("mtime") == key["mtime"] and previous.get("size") == key["size"]:
               if previous.get("unsupported"):
                   unsupported.append(rel)
               continue
           if on_progress:
               on_progress({"stage": "convert", "file": rel})
           try:
               markdown = parse_document(path, base_url=parser_base_url, settings=settings)
           except UnsupportedDocument as exc:
               seen[rel] = {**key, "unsupported": str(exc)}
               unsupported.append(rel)
               continue
           except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
               log.warning("convert failed for %s: %s", rel, exc)
               failed.append(rel)
               continue
           target = _raw_target(project, rel)
           target.parent.mkdir(parents=True, exist_ok=True)
           target.write_text(markdown, encoding="utf-8")
           seen[rel] = key
           converted.append(rel)

       for rel in list(seen):
           if rel not in present:
               _raw_target(project, rel).unlink(missing_ok=True)
               seen.pop(rel)
               removed.append(rel)

       if converted or removed:
           git(project, "add", "-A", ".")
           git(project, "commit", "-qm", f"convert: +{len(converted)} -{len(removed)}")
       project.metadata.mkdir(parents=True, exist_ok=True)
       log_path.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")
       if unsupported:
           log.warning("convert: %d unsupported: %s", len(unsupported), unsupported[:5])
       return {
           "converted": converted,
           "removed": removed,
           "unsupported": unsupported,
           "failed": failed,
       }
   ```

   `requests` is already used by `parser/server.py`; confirm it is importable from the
   `llm-wiki-dist` venv (`.venv/bin/python -c "import requests"`). If not, add nothing —
   use `urllib.request` with the same multipart body (ask before doing that).

   Note the `failed` list: a transient parser error leaves the file **unrecorded** in
   `convert.json`, so the next sync retries it. An unsupported file is recorded with its
   mtime/size, so it is not re-sent until it changes.

3. `graph/sync.py`, in `sync_project`, first lines of the body (before `changes = …`):

   ```python
       parser_base_url = str(getattr(settings, "parser_base_url", "") or "")
       if parser_base_url and project.mount.exists():
           from .convert import convert_mount

           emit(stage="convert", step="start")
           report = convert_mount(
               project, parser_base_url=parser_base_url, settings=settings, on_progress=on_progress
           )
           emit(stage="convert", step="done", **{k: len(v) for k, v in report.items()})
   ```

4. `tests/test_convert.py`: patch `graph.convert.parse_document` with a side effect that
   returns `"# md\n"` for `.pdf`/`.docx` and raises `UnsupportedDocument("nope")` for
   `.zip`; put `mount/f1/a.pdf`, `mount/f1/b.docx`, `mount/f1/c.zip` in a temp project with
   an initialised raw repo (copy the `git()` helper from `test_sync.py`); call
   `convert_mount(..., settings=SimpleNamespace())`; assert `raw/f1/a_pdf.md` and
   `raw/f1/b_docx.md` exist, `git log --oneline` gained one line,
   `unsupported == ["f1/c.zip"]`; call again and assert `parse_document` was called exactly
   3 times in total (nothing re-sent, including the unsupported one). Then delete
   `mount/f1/a.pdf`, call again, assert `raw/f1/a_pdf.md` is gone and a new commit exists.
   One more test: `parse_document` raising `RuntimeError` for one file → that file is in
   `failed`, the others are converted, and a second call retries it.

   Also a real-contract test that needs no server: feed `parse_document` a fake `requests`
   (patch `graph.convert.requests.post`) whose `.content` is `b"\n\n\n{\"markdown\": \"# x\", \"parser\": \"pdf\"}"`
   and `.status_code == 200` — assert it returns `"# x"` (proves the heartbeat whitespace
   is handled); and one with `status_code == 415` — assert `UnsupportedDocument`.

**Verify:** `unittest tests.test_convert tests.test_sync` OK. Manual: start doc-parser
(`uv run uvicorn server:app --port 8000` in its folder), set `parser_base_url`, drop one PDF
and one DOCX into `mount/`, `POST /api/sync`, see both `raw/…_pdf.md` / `…_docx.md`
committed and both wiki folders appear.

**Revert:** `git checkout -- graph/sync.py graph/core.py && rm graph/convert.py tests/test_convert.py`.

---

## WP-S6 — Incremental `M` in `wiki` mode: `graph/neo/incremental.py`

**Goal:** when a raw file changes without changing its line count, only pages whose owned
or imported lines were touched are rewritten; everything else resumes from neo's sidecar
state. Any line-count change falls back to a full re-run of that file.

**Files:** new `graph/neo/incremental.py`, `graph/sync.py` (remove the `try`), new
`tests/test_neo_incremental.py`.

**Steps:**

1. Create `graph/neo/incremental.py`:

   ```python
   """Decide what a source edit invalidates in an existing neo run."""

   from __future__ import annotations

   import shutil
   from pathlib import Path
   from typing import Sequence

   from .storage import normalize_source, read_json, sha256_text, split_source_lines, write_json_atomic

   Hunk = tuple[int, int, int, int]  # old_start, old_len, new_start, new_len


   def _overlaps(ranges: Sequence[Sequence[int]], start: int, end: int) -> bool:
       return any(int(s) <= end and start <= int(e) for s, e in ranges)


   def invalidate_pages(run_root: Path, hunks: Sequence[Hunk], new_source_text: str) -> str:
       """Return ``"full"`` (state removed, re-plan) or ``"resumed"`` (touched pages removed).

       Resume is only safe when every line keeps its number, i.e. every hunk replaces
       exactly as many lines as it removes.  Then the seed plan stays valid and only the
       pages that own or import a changed line need rewriting.
       """

       run_root = Path(run_root)
       plan_path = run_root / "state" / "plan.json"
       if not plan_path.exists() or not hunks:
           shutil.rmtree(run_root, ignore_errors=True)
           return "full"
       plan = read_json(plan_path)
       new_lines = split_source_lines(normalize_source(new_source_text))
       if len(new_lines) != int(plan.get("source_line_count", -1)) or any(
           old_len != new_len for _, old_len, _, new_len in hunks
       ):
           shutil.rmtree(run_root, ignore_errors=True)
           return "full"

       touched: list[dict] = []
       for page in plan["pages"]:
           ranges = list(page.get("owner_ranges", [])) + list(page.get("reference_ranges", []))
           if any(_overlaps(ranges, old_start, old_start + old_len - 1) for old_start, old_len, _, _ in hunks):
               touched.append(page)
       for page in touched:
           (run_root / "wiki" / page["filename"]).unlink(missing_ok=True)
           (run_root / "state" / "pages" / f"{int(page['number']):03d}.json").unlink(missing_ok=True)
           shutil.rmtree(run_root / "work" / f"page-{int(page['number']):03d}", ignore_errors=True)
           shutil.rmtree(run_root / "work" / f"research-{int(page['number']):03d}", ignore_errors=True)
       # The seed plan is still valid for the new text; let resume accept it.
       plan["source_sha256"] = sha256_text(new_source_text)
       write_json_atomic(plan_path, plan)
       return "resumed"
   ```

   Why `source_sha256` is rewritten: `pipeline._load_seed_plan` refuses a plan whose
   recorded sha differs from the current source. After the checks above the plan *is* still
   valid, so we tell it so. Untouched pages are then resumed by
   `_resume_rewritten_page` (their sidecar sha still matches their file), touched pages have
   no sidecar and are rewritten.

2. `graph/sync.py`: remove the `try/except ImportError` from WP-S4 step 4; the import is
   now unconditional. Add the returned mode to the progress event:
   `emit(stage="sync", step="invalidate", file=change.rel, result=<returned string>)`.

3. `tests/test_neo_incremental.py`:

   ```python
   import json, tempfile, unittest
   from pathlib import Path

   from graph.neo.incremental import invalidate_pages


   def make_run(root: Path, text: str) -> Path:
       (root / "state" / "pages").mkdir(parents=True)
       (root / "wiki").mkdir()
       lines = text.splitlines()
       plan = {"source_sha256": "old", "source_line_count": len(lines), "pages": [
           {"number": 1, "filename": "001-a.md", "owner_ranges": [[1, 5]], "reference_ranges": [[8, 8]]},
           {"number": 2, "filename": "002-b.md", "owner_ranges": [[6, 10]], "reference_ranges": []},
       ]}
       (root / "state" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
       for n, name in ((1, "001-a.md"), (2, "002-b.md")):
           (root / "wiki" / name).write_text("x", encoding="utf-8")
           (root / "state" / "pages" / f"{n:03d}.json").write_text("{}", encoding="utf-8")
       return root


   class IncrementalTests(unittest.TestCase):
       def test_same_length_edit_invalidates_only_touching_pages(self) -> None:
           text = "\n".join(f"l{i}" for i in range(1, 11))
           with tempfile.TemporaryDirectory() as d:
               run = make_run(Path(d), text)
               new = text.replace("l7", "L7")
               self.assertEqual(invalidate_pages(run, [(7, 1, 7, 1)], new), "resumed")
               self.assertTrue((run / "wiki" / "001-a.md").exists())
               self.assertFalse((run / "wiki" / "002-b.md").exists())
               self.assertFalse((run / "state" / "pages" / "002.json").exists())
               plan = json.loads((run / "state" / "plan.json").read_text(encoding="utf-8"))
               self.assertNotEqual(plan["source_sha256"], "old")

       def test_reference_range_hit_invalidates_importing_page(self) -> None:
           text = "\n".join(f"l{i}" for i in range(1, 11))
           with tempfile.TemporaryDirectory() as d:
               run = make_run(Path(d), text)
               invalidate_pages(run, [(8, 1, 8, 1)], text.replace("l8", "L8"))
               self.assertFalse((run / "wiki" / "001-a.md").exists())  # imports line 8
               self.assertFalse((run / "wiki" / "002-b.md").exists())  # owns line 8

       def test_line_count_change_is_full(self) -> None:
           text = "\n".join(f"l{i}" for i in range(1, 11))
           with tempfile.TemporaryDirectory() as d:
               run = make_run(Path(d), text)
               self.assertEqual(invalidate_pages(run, [(7, 1, 7, 2)], text + "\nextra"), "full")
               self.assertFalse(run.exists())
   ```

**Verify:** `unittest tests.test_neo_incremental tests.test_sync` OK. Manual: in the WP-S4
project with `ingest_mode=wiki`, edit one word in `raw/…md` (same line count), commit,
`POST /api/sync`, and confirm in the job progress that only one or two pages were written
(`write` events name only those sections) and the rest show `page_resumed`.

**Revert:** `git checkout -- graph/sync.py && rm graph/neo/incremental.py tests/test_neo_incremental.py`.

---

## WP-S7 — Exports: Obsidian zip and GROWI publish from `wiki/`

**Goal:** `GET /api/wiki.zip` downloads `wiki/` without `_planning/`; the existing GROWI
publish job can take its pages from `wiki/`.

**Files:** `app.py`, `graph/librarian.py` (one helper), `tests/test_wiki_zip.py`.

**Steps:**

1. `app.py`:

   ```python
   @app.get("/api/wiki.zip")
   async def wiki_zip() -> StreamingResponse:
       if DATA_ROOT is None:
           raise HTTPException(status_code=400, detail=api_error("WIKI_DATA_ROOT is not configured", False, "no_data_root"))
       from graph.project import Project

       project = Project(DATA_ROOT / current_db.get())
       buffer = io.BytesIO()
       with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
           for path in sorted(project.wiki.rglob("*")):
               if not path.is_file() or "_planning" in path.parts:
                   continue
               archive.write(path, path.relative_to(project.wiki).as_posix())
       buffer.seek(0)
       return StreamingResponse(
           buffer,
           media_type="application/zip",
           headers={"Content-Disposition": f'attachment; filename="{current_db.get()}-wiki.zip"'},
       )
   ```

   Add `import io, zipfile` and `from fastapi.responses import StreamingResponse` if missing.
   Use the same "which db am I" accessor the other endpoints use (`current_db.get()` is what
   the routing middleware sets — confirm by reading `db_routing`).

2. `graph/librarian.py`, next to `publish_to_growi`, add a helper (additive) that builds
   the `pages` list `publish_pages` expects from the wiki folder:

   ```python
       def growi_pages_from_wiki(self, wiki_root: Path, write_path: str) -> list[dict[str, str]]:
           """[{path, body}] for every published page; path = write_path/<rel-dir>/<name>/<page>."""
           pages: list[dict[str, str]] = []
           for md in sorted(wiki_root.rglob("*.md")):
               if "_planning" in md.parts or md.name == "index.md":
                   continue
               rel = md.relative_to(wiki_root).with_suffix("").as_posix()
               pages.append({"path": f"{write_path.rstrip('/')}/{rel}", "body": md.read_text(encoding="utf-8")})
           return pages
   ```

   Then in `publish_to_growi`, where the job payload provides `pages`, accept an
   alternative payload key `from_wiki: true` that calls this helper with
   `Path(self.settings.data_root) / "wiki"` and the connection's `write_path`. Keep the
   existing payload path working unchanged.

3. `tests/test_wiki_zip.py`: build a temp project with `wiki/f1/a.docx/001-a.md`,
   `wiki/f1/a.docx/_planning/x.json`, `wiki/index.md`; call the zip-building part (factor
   the loop into `graph/project.py: zip_wiki(project) -> bytes` so it is testable without
   FastAPI) and assert the archive names are exactly `["f1/a.docx/001-a.md", "index.md"]`.
   Test `growi_pages_from_wiki` the same way: one page, path `"/wiki/f1/a.docx/001-a"`.

**Verify:** tests OK; `curl -o w.zip …/api/wiki.zip && unzip -l w.zip` shows no `_planning`.
Open the unzipped folder in Obsidian: `index.md` links resolve.

---

## WP-S8 — Frontend: the switch and the buttons

**Goal:** a non-technical user can pick the writer, press Sync, and download the zip.

**Files:** `frontend/src/components/SettingsView.jsx`, `frontend/src/components/QueueView.jsx`,
`frontend/src/api.js`, one new small component or two buttons in the upload/queue view.

**Steps:**

1. `SettingsView.jsx`: `ingest_mode` options become `chunks | pages | wiki`; add the four
   `wiki_*` fields under a "Wiki writer" group (same pattern the `page_*` fields use — see
   `IMPLEMENTATION_PLAN_GROWI.md` WP-F1 for how that group was added).
2. `api.js`: `syncProject(mode)` → `POST api/sync`; `wikiZipUrl()` → `api/wiki.zip`.
3. `QueueView.jsx`: render `sync_project` jobs; stage labels for `convert`, `sync`, `write`,
   `ingesting` (copy the label map used for page-mode stages).
4. Two buttons near the upload form: **Sync from raw/** (disabled with a tooltip when the
   backend answers `no_data_root`) and **Download wiki (zip)**.

**Verify:** `npm run build` clean; click Sync on the WP-S4 project and watch the queue.

---

## Appendix A — Where each old thing goes

| Today | After |
|---|---|
| `python -m graph.neo file.md --output .wiki/neo` | still works (dev only); production path is `ingest_mode=wiki` via librarian |
| `DB_DIR/<db>.sqlite` | `DATA_ROOT/<db>/graph.sqlite` when `WIKI_DATA_ROOT` set; unchanged otherwise |
| `chunk_and_ingest` `if mode == "pages" … else …` | `build_wiki_output(mode=…)` |
| per-upload `chunked/<name>-<hash>/` scratch | unchanged for uploads; project sync uses `metadata/work/<…>/` |
| GROWI `publish_to_growi` with explicit pages | unchanged; `from_wiki: true` reads `wiki/` |
| `zip.py` (zips the repo) | untouched; wiki export is `/api/wiki.zip` |

## Appendix B — Deliberate simplifications (`ponytail:` ceilings)

- **Sequential sync.** One file at a time; parallelism lives inside each writer
  (`rewrite_concurrency`, `ingest_concurrency`). Add batch parallelism only if a project has
  hundreds of files and sync wall time is the complaint.
- **Renames are D + A.** `--no-renames` on purpose; a rename re-ingests the file. Cheap
  correctness now; add `R` handling (move `wiki/`, `metadata/state/`, and
  `store.rename_document`) when renames are frequent.
- **Incremental only for equal line counts.** Every other edit re-plans the file. Extend
  to "shift page ranges by the hunk delta" only after the equal-length path has run on real
  edits for a while.
- **Converter = doc-parser, whatever it supports.** We do not inspect extensions; a 415 is
  recorded and skipped until the file changes. If the manager delivers `raw/` himself, set
  `parser_base_url=""` and WP-S5 is inert.
- **Change detection for `mount/` is mtime+size.** A file rewritten with identical mtime
  and size is missed. Switch the key to a content hash if that ever happens in practice.
- **`index.md` is regenerated from the folder**, not from any planning data. Good enough
  for Obsidian; GROWI has its own navigation.
- **No metadata folder schema beyond two files** (`last_sha`, `convert.json`) plus neo's
  `state/`. Add more only when a consumer for it exists.
