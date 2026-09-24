# Handoff: small-diff wiki sync remains unfinished

Date: 2026-09-24  
Repository: `/home/seigyo/llm-wiki`  
Project used for live testing: `diff_test`

## Status at handoff

The requested behavior is **not complete**.

The latest live run proved that the page-editing phase is finally incremental, but the linker phase is still far too broad:

- 8 source diff hunks caused 6 wiki pages to be edited instead of all 57 pages. This part is scoped correctly.
- Those 6 changed chunks were connected to 101 existing catalog edges.
- The incremental linker then ran many LLM requests, retained/rebuilt 43 edges, and scheduled 40 pages for curation.
- The pasted log had reached `page_curated 15/40`; no final publish/promote output was provided, so the final state of that run is unknown.

Therefore the user’s central requirement—small edits must finish quickly without broad linker curation/publishing—is still violated.

Do not describe this work as finished.

## Non-negotiable requested behavior

For a small edit to an existing document, the required flow is:

1. Parse the changed source document.
2. Diff the previous and current parsed Markdown.
3. Map each diff hunk only to the wiki page that owns those source lines.
4. Give the existing generated page, its current source, and all exact `ADD` / `UPDATE` / `DELETE` hunks to the LLM.
5. Have the LLM apply every edit, including semantic deletion of previously paraphrased text.
6. Permit only immediate local prose adjustments needed to keep the edited passage natural.
7. Check only the links/edges relevant to changed chunks and their old peers.
8. Leave valid existing links alone.
9. Change only links that are no longer valid.
10. Render and publish only changed wiki pages and any specific peer page whose actual visible link changed.
11. Commit/promote the result.

A small edit must not:

- run the normal 57-page wiki generation pipeline;
- regenerate unrelated pages;
- perform catalog-wide candidate discovery;
- relink the whole document;
- curate dozens of pages;
- publish dozens of pages.

For a large source change, rebuilding the changed document is acceptable.

## Exact source edits used for the test

The DOCX contains these surgical changes:

1. Table `表 2.1-3`: change three existing numeric cells in `点数` rows without adding or removing rows/columns.
2. Grammar:
   - old: `需給制御関連画面の呼び出しを行う。`
   - new: `需給制御関連画面を呼び出す。`
3. Typo:
   - old: `需要抑制カーフ゛`
   - new: `需要抑制カーブ`
4. Delete standalone source paragraphs 143 and 170.
5. The surrounding headings, figures, and tables remain only because they were not deletion targets. If a future diff explicitly deletes a heading, figure, table, list, image, or paragraph, that exact item must also be deleted.

The wiki generator may have paraphrased source prose. A deleted source paragraph therefore cannot be removed reliably through literal or fuzzy string matching. The LLM must identify the semantically corresponding generated passage and remove it.

## Earlier unacceptable behavior

Before the current code changes, the same small source diff had this behavior:

- classification: `small`
- ratio: `0.004341`
- hunks: `8`
- all 57 pages appeared as `page_resumed`
- multiple pages were fully researched and rewritten
- a full or broad linker pass followed
- one run processed 47 linker chunks
- another run created/considered 63 edges
- 42 pages were curated and published
- the two deleted source paragraphs could survive because generated wiki text had paraphrased them

A hardcoded/fuzzy paragraph-removal implementation was also attempted earlier. The user explicitly rejected it because it cannot reliably match arbitrary paraphrased wiki prose.

## Code currently changed

The worktree was already heavily dirty before this task. Do not reset or overwrite unrelated changes.

### `graph/wiki/wire.py`

Added structured model output types:

- `IncrementalPagePatch`
  - `edit_ids`
  - `before`
  - `after`
- `IncrementalPageEditResult`
  - `patches`

The model selects exact unique substrings from the existing generated wiki page and supplies their replacements.

### `graph/wiki/prompts.py`

Added `incremental_page_edit_prompt()`.

Its current rules require:

- every `ADD`, `UPDATE`, and `DELETE` to be covered exactly once;
- semantic deletion even when the generated page paraphrases the deleted source;
- no revival of deleted content;
- old update content to be removed and new content inserted;
- additions to use an existing exact anchor;
- `before` to be an exact unique substring of the current page;
- unrelated prose, headings, tables, images, links, and navigation to remain unchanged;
- local surrounding prose may be rewritten only where necessary for continuity;
- structures are deleted when they themselves are the deletion target.

Generic full-rewrite prompts were also strengthened to prohibit restoring content absent from the current source.

### `graph/workspace/writer.py`

Current incremental page behavior:

- computes a line diff with `difflib.SequenceMatcher`;
- uses `SMALL_DIFF_RATIO = 0.01`;
- maps hunks through page `owner_ranges` using `_owner_hunks_by_page()`;
- calls `invalidate_pages(..., preserve_outputs=True)` for small diffs;
- bypasses `build_wiki_output()` for the small path;
- exports cached pages directly with `export_ingest_layout()`;
- sends only owning pages to `_apply_incremental_edits()`;
- applies exact model-selected page patches through `_apply_model_patches()`;
- requires all edit IDs exactly once;
- rejects missing, repeated, or overlapping `before` spans;
- protects existing and newly introduced image units;
- checks tables/code/verbatim blocks and identifiers;
- writes successful edits into both staged output and persistent state cache;
- emits `[wiki] incremental_edit`;
- returns exact changed page paths through `WriteResult.changed_pages`;
- uses the cache-only path with zero model edits when the parsed Markdown is unchanged.

Removed/rejected behavior:

- no `_apply_surgical_edits` path;
- no hardcoded paragraph deletion;
- no fuzzy semantic matching in Python;
- no `surgical_reuse` progress event.

### `graph/linker/service.py`

Current intended incremental linker behavior in the code:

- accepts a `changed_pages` scope;
- refreshes metadata only for changed chunks;
- skips global pending embeddings in incremental mode;
- skips global legacy/Neo candidate discovery in incremental mode;
- snapshots old edges before `Catalog.reconcile()` removes edges attached to changed chunks;
- uses old linked peers as incremental candidates;
- preserves accepted old edge label/summary/source/via values;
- marks peers for rendering when an old edge is rejected;
- adds changed pages to render scope;
- records linker marker scope as `incremental`;
- emits `[linker] incremental_scope`.

Important current implementation detail:

When old edges are converted back into incremental candidates, the current code constructs them as:

```python
Candidate(peer, str(edge["source"]), via)
```

That reconstruction does not populate the candidate’s existing `programmatic`, `label`, or `summary` fields. This is recorded only as current code state; no fix is proposed in this handoff.

### `publisher/pipeline.py`

Existing changes collect `WriteResult.changed_pages`, pass them into the linker as the affected-page scope, extend that set with pages returned by the linker, and pass the resulting set to GROWI publishing.

The broad 40-page curation in the latest run therefore propagated into the publish scope because the linker reported those pages as affected.

### `tests/test_mount_diff_pipeline.py`

Current safety coverage includes:

- a paraphrased deleted paragraph is removed using a fake structured LLM patch;
- the small path fails the test if `build_wiki_output()` is invoked;
- a second identical parsed input uses cached pages, produces no model call, and reports no changed pages;
- prompts explicitly require complete `ADD` / `UPDATE` / `DELETE` handling;
- incremental linking raises if global legacy candidate search is called;
- one accepted old edge remains unchanged and does not mark its peer page affected;
- one rejected old edge is removed and marks only its peer page affected.

The synthetic linker test contains one old peer edge. It did not expose the real document’s 101-edge fan-out.

## Latest live run: exact evidence

Command run by the user:

```bash
.venv/bin/python main.py -v sync --project diff_test
```

Run ID:

```text
prun-e0b19ad3576846cd
```

Classification:

```text
[diff-classified] {"path": "test.docx", "decision": "small", "ratio": 0.004341, "hunks": 8, "reason": "below-threshold"}
```

Parse:

```text
[parse] done {"file": "test.docx", "characters": 1899672, "elapsed_seconds": 4.4}
```

Wiki edit result:

```text
[wiki] incremental_edit {"invalidated_pages": 9, "changed_pages": 6, "edits": 8, "link_scope": "changed_chunks_and_existing_peers"}
[wiki] done {"file": "test_docx.md", "touched_documents": 0, "elapsed_seconds": 40.3}
```

This is materially better than the prior 57-page rewrite. There were no `seed`, `context`, `research`, `section_judged`, or 57 `page_resumed` events in the supplied log.

Linker result:

```text
[linker] chunks 6/6 (100%) {"document": "test_docx.md", "catalog_total": 238}
[linker] incremental_scope {"document": "test_docx.md", "changed_chunks": 6, "existing_edges": 101}
[linker] edge_target_done 6/6 (100%) {"document": "test_docx.md"}
[linker] done {"document": "test_docx.md", "edges": 43}
[linker] page_curated 1/40 ...
```

The supplied log continued through:

```text
[linker] page_curated 15/40
```

Verdict: **not acceptable and not complete**. The linker is technically in `incremental_scope`, but its practical scope is still broad enough to defeat the purpose of the small-diff path.

## Catalog evidence captured during the live run

The current project database is:

```text
data/diff_test/metadata/wiki-linker.sqlite
```

A read-only SQLite query during the run showed:

```text
chunks: 238
edges: 1074
```

Edge counts by source:

```text
define: 879
hop1:   115
hop2:    55
use:     17
hop3:     8
```

This confirms that the live Neo catalog has very high edge fan-out, dominated by `define` edges. The latest incremental run found 101 old edges attached to only 6 changed chunks. The current one-edge unit fixture did not model this scale.

## Current live/project state

- `data/diff_test` exists again now.
- `data/diff_test/metadata/wiki-linker.sqlite` exists.
- Earlier in the session, `data/diff_test` was absent after reset; it was restored/recreated before the latest live run.
- `configs/diff_test.ini` points to:
  - source mount: `/home/seigyo/llm-wiki/data/diff_test/mount`
  - target: `diff_test`
  - GROWI URL: `http://10.160.152.38:3000/`
  - linker mode: `neo`
- The config contains the replacement GROWI token supplied by the user. The token is intentionally not copied into this handoff.
- The pasted live log did not contain final candidate commit, GROWI publish, or history promotion lines. Do not assume the run completed or promoted.

## Verification already run

Successful checks after the latest code changes:

```bash
python -m py_compile \
  graph/workspace/writer.py \
  graph/wiki/prompts.py \
  graph/wiki/wire.py \
  graph/linker/service.py \
  tests/test_mount_diff_pipeline.py

.venv/bin/python -m unittest -v \
  tests.test_mount_diff_pipeline.DiffPipelineSafetyTest \
  tests.test_parser_client_route

git diff --check
```

Result:

- 14 diff-pipeline safety tests passed.
- 6 parser client tests passed.
- 20 relevant tests passed in total.
- Python compilation passed.
- `git diff --check` passed.

Other test/environment facts:

- 23 live integration tests are opt-in through `RUN_DIFF_INTEGRATION=1`.
- A prior complete-module run failed two fixture-presence tests while `data/diff_test/mount/test.docx` was absent. The directory exists again now, but the complete module was not rerun after that restoration.
- `tests.test_xlsm_lineage_equivalence` has three unrelated failures because `/home/seigyo/parser/formats/xlsm_lineage.py` is absent.

## Files relevant to this task

Modified:

- `graph/workspace/writer.py`
- `graph/wiki/prompts.py`
- `graph/wiki/wire.py`
- `graph/linker/service.py`
- `publisher/pipeline.py` (contains earlier affected-page propagation work)

Untracked at the latest status check:

- `tests/test_mount_diff_pipeline.py`
- `configs/diff_test.ini`
- `handoff.md`

There are many additional modified/untracked files from broader work in this repository. Preserve them. Do not use destructive Git commands or assume all dirty changes belong to this specific task.

## Final factual summary

- Model-driven semantic page editing exists.
- Hardcoded/fuzzy paragraph deletion is gone.
- The normal 57-page wiki writer is bypassed for small parsed diffs.
- The latest small edit changed 6 wiki pages for 8 hunks.
- The linker still expanded those 6 chunks through 101 old edges into 40 curated pages.
- The final content/publish result of the latest live run was not provided.
- The requested small-diff pipeline is therefore still unfinished.
