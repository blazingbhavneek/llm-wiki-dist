# `sync --fast`: a shorter wiki and linker path

## Goal and boundary

Build readable, source-faithful wiki pages and useful links with substantially fewer model calls. `--fast` selects a generation policy for `sync`; it does not change source discovery, parsing, the durable queue, candidate recovery, human overlays, GROWI revision checks, or publication ownership. Keep the existing isolated sync behavior by default. An operator can already combine `--fast` with `--no-isolated` when batch transaction speed matters more than per-document failure isolation.

The hard quality rule is **no known missing, changed, or invented source facts in published wiki text**. Link precision matters more than link recall: omit a doubtful cross-document link. Fast mode may produce fewer explanatory links and less prose than the standard path.

This is a proposal against the current code, not an implementation plan for the separate `refactor_plan.md` architecture.

## Where the time goes today

The route is `main.py:cmd_sync` → `publisher/queue.py:work_once` → `publisher/pipeline.py:sync_once` → `graph/workspace/writer.py:write_wiki_pages`, followed by the linker, publication, and the final index reconciliation. Default isolated sync first accepts each document's wiki, then `link_pending_isolated` creates a separate candidate for each pending document.

For a new document with **P** pages, **S** rewritten sections, and **C** linker chunks, the structured-format path can make roughly P reference-research calls, one context call per parent, S rewrite calls, S judge calls, P intro calls, C metadata calls, plus edge confirmation and page curation calls. Retries add more. PPTX section planning adds one deck-level call. Unstructured documents also pay for observation and seed planning. These are call counts, not measured wall times; parser and local Jev time can also dominate.

## Wiki generation policy

| Current stage | Fast policy | Quality retained |
| --- | --- | --- |
| Parse source to raw Markdown | Keep the current parser and image handling. Markdown/TXT passthrough stays as it is. | Tables, figures, formulas, source-version validation. |
| DOCX seed plan | Keep the heading-tree plan in `graph/formats/docx.py`. | Document hierarchy and complete, contiguous source ownership. |
| PPTX seed plan | Keep slide atoms and the single deck-level section call in `graph/formats/pptx.py`. | Slide order, section grouping, and intact slide content. |
| PDF, TXT, weakly structured Markdown seed plan | Keep the existing observation and seed-planning fallback. | Topic boundaries where source structure cannot be trusted. |
| Strongly structured Markdown seed plan | Keep the existing validated heading-tree shortcut. | Native headings without planning calls. |
| Hierarchy summarization | Skip model calls. Pass the known path, neighboring titles, and existing seed summaries as deterministic writer context. | Enough context to name the page and avoid abrupt starts. |
| Cross-page reference research | Skip `_research_references` and its fact insertion. | Each source line still belongs to exactly one page; intra-document navigation remains. |
| Section rewrite | Keep source-order sections and bounded concurrency. Ask for one wiki-style rewrite per section, including a useful opening in its first section. | Readable prose, exact identifiers, tables, code, images, conditions, and source deletions. |
| Section judge | Keep one independent judge call per rewritten section. Check omissions, contradictions, unsupported additions, and obsolete text after an update. | A semantic check alongside mechanical checks. |
| Repair | Make one targeted retry only when mechanical checks or the judge identify a concrete problem. If it still fails, publish that source section verbatim and mark it for review. | The fast path never knowingly publishes a draft with omitted facts. |
| Intro generation | Skip `_write_intro`. The first section supplies the opening; a brief existing seed summary may be used when needed. | One fewer call per page, without an extra opportunity to invent facts. |
| Intra-document links and navigation | Keep deterministic `link_titles`, page index, and navigation footer. | Basic navigation even if the cross-document linker finds little. |

The current writer can return a clean draft after the judge reports missing information when its retry budget expires (`graph/wiki/pipeline.py:_write_section`). Fast mode must change that outcome to verbatim fallback or a failed build. A judge timeout should not be treated as approval. Preserve the current source and generated artifacts so the fallback is inspectable. Apply the same safe heading-number cleanup to a verbatim fallback while leaving its factual body intact.

Remove chapter and section numbering from generated **titles and headings**, including names coming from the seed plan. Recognize formatting prefixes only. Preserve version numbers, numbered steps, API names, dates, identifiers, cell coordinates, and other meaningful numbers in the body. The judge must compare against the *current* source so a deleted fact is not carried forward from a cached page.

Keep the existing source-range validation, atomic block boundaries, image placeholder restoration, code-token checks, incremental update tiers, checkpoints, and manifest/coverage output. Fast and standard rewrites need distinct prompt or state versions so switching modes cannot reuse an incompatible page draft. Existing structured plans can be reused when their source hash and validation pass. When a changed source crosses from standard to fast generation or back, force a full document rebuild: the incremental editor can otherwise preserve old cross-page additions or a previous style without running the new policy. An unchanged source remains a no-op until explicitly forced.

### Spreadsheets and CSV

The current XLSX path makes source table pages, then turns every sheet into a one-line "story" and runs that story through the full observation, planning, research, rewrite, and judge pipeline. Fast mode should stop after the source table pages:

- Use the existing grid/region parser and deterministic row-oriented `heuristic_structure` for ordinary sheets. Keep the original table, row or cell addresses, formulas, and source-cell metadata visible.
- Skip the per-sheet row/column LLM choice, workbook-wide `_append_story`, and generated analysis pages. CSV should likewise skip its analysis-page calls.
- Preserve raw VBA and its existing concise module description where present. A workbook with macros can retain those bounded calls without paying for the whole workbook story.
- Populate the current manifest, coverage, and index fields from sheet name, table headers, row count, and source ranges. Search must still have a useful title and summary for each sheet.

The loss is narrative synthesis across sheets. The original values and their provenance remain available and searchable.

## Linker policy

Keep the current chunk IDs, catalog, content-hash cache, changed-chunk reconciliation, embeddings for changed chunks, link rendering, protected human regions, and `_planning/chunks.json` format. The index and search consume chunk metadata, so an empty metadata shortcut would save calls at a substantial search cost.

| Current linker work | Fast policy |
| --- | --- |
| One metadata LLM call per new H2 chunk | Batch the chunks of a page into one bounded extraction request, returning the existing `ChunkMeta` fields keyed by chunk ID. Split only oversized pages. Validate each result against its own chunk; retry missing or invalid chunks individually. Reuse existing hash-cached metadata unchanged. |
| Broad entity, 1–3 hop, and FTS candidate expansion | Keep exact entity/definition matches, useful 1-hop candidates, and a small FTS/embedding shortlist. Skip 2- and 3-hop expansion. Reserve slots for both same-document and cross-document candidates so siblings cannot crowd out other documents. |
| Wide local Jev screening and verification | Keep Jev for the small shortlist where available, with stricter acceptance for cross-document links. Keep deterministic exact-definition links. |
| LLM tie-break for every accepted cross-document edge and uncertain edge | Skip routine LLM tie-breaks. Retain only high-confidence Jev results with supporting exact name/title evidence; omit uncertain edges. An unavailable Jev engine should use the same conservative exact-match rule rather than silently launching the expensive LLM path. |
| One LLM page-curation call for changing candidate sets | Select a few verified links deterministically, retain still-valid existing choices, and render via the current footer/inline renderer. Inline links require an exact text anchor; the rest go in the footer. |

This deliberately reduces conceptual and multi-hop link recall. It should preserve high-confidence definition links and a small set of related pages. Reuse the existing `linker.json`, `links.json`, and navigation artifacts, but give fast metadata, edge decisions, and curation their own versions so standard-mode caches are not mislabeled as fast results. Record actual metadata calls, fallback calls, Jev decisions, and accepted/omitted edges in the linker marker.

Batching metadata is the largest linker change and the only routine new-link LLM work retained. If its output proves unreliable on large pages, keep the existing per-chunk extraction for those pages; do not lower index quality by silently accepting empty fields.

## Integration and scope

1. Add `sync --fast` in `main.py` and carry one explicit fast-policy value through `Settings` or the existing run arguments to `sync_once`, `write_wiki_pages`, the wiki pipeline, and `run_linkers`. Avoid a second sync implementation.
2. Keep scan, parse, overlay, publication, retry, and recovery behavior. Keep `watch`, `build`, `link`, and ordinary `sync` unchanged unless fast mode is explicitly selected.
3. Complete all selected wiki pages before linking, as isolated sync already does. The separate linker candidates remain the default safety boundary. `--fast --no-isolated` can use the existing batch route when the workload and failure tolerance justify it.
4. Use the existing scoped index update for changed documents and their ancestors when related-document indexing does not require a whole-project refresh. Preserve the root index and deleted-document cleanup. Avoid a second unconditional whole-tree publication pass where the scoped sweep already reconciled it.
5. Store `generation_policy=fast-v1` in planning/state markers and invalidate only the affected wiki or linker caches when switching policy. A source-unchanged sync should still do no generation work.

## Acceptance check before calling it fast

Use the same small corpus in standard and fast mode: a long DOCX with headings and tables, a PPTX with images, an unstructured PDF, an XLSX/XLSM with formulas and VBA, and a changed source that deletes a fact. Record parse, plan, rewrite, judge, linker, and publish wall time plus LLM call count. Review source-range coverage, identifier/table/image preservation, judge omissions, unsupported claims, stale deleted facts, and representative search/index results. Compare link precision and recall separately. A fast run may have fewer links; it must not publish known wrong ones or lose source facts.

Expected call budget for a typical structured document becomes approximately **one PPTX planning call if applicable + one rewrite and one judge per section + one linker metadata call per ordinary page**. Repairs and oversized pages add calls. PDF/TXT planning and parser time remain, so this proposal promises a much shorter generation path, not a fixed end-to-end time for every file.
