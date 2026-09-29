# codex-projc.md — Compacted Handoff

- Source: `codex-projc.md`, 53,033 lines / 3.8MB (Codex conversation log, llm-wiki project, Tokyo Electric Power PG document pipeline).
- Original covered 2026-09-29 session. This file replaces it: each 500-line window of the original is summarized below (L = original line range), followed by a consolidated handoff.
- Activity details (full command outputs) were dropped; each window keeps: user/assistant text in full, activity commands + failures + status.

## Per-window summaries

### L1–500 (WIN 001)
- User pasted pipeline log: `[plan] compile_retry` attempts 544–549 for `…/F訓練シミュレータ/04印字編/Ｂ改定/04章_005事故状変内容印字項目説明.doc` with constant error "page boundaries around 9 place an entire page inside table 1-22; merge or move those semantic pages and return the complete plan again" — asking why it keeps retrying.
- Agent started digging: `git status` (modified: `growi-search/config.py`, `growi-search/gateway.py`, `jev/backends/__init__.py`, `jev/config.py`, `jev/test_jev.py`, `pyproject.toml`, `uv.lock`; untracked `jev/backends/llm2jev.py`, `requirements.txt`); began reading `graph/wiki/document_map.py` and `graph/wiki/config.py`.

### L501–1000 (WIN 002)
- Read `graph/wiki/document_map.py` (seed planning, `SeedPlanningError`, retry loop) and `graph/wiki/config.py`.
- Assistant: retry is a bounded `for` loop in `_compile_seed_plan()` — not `while True`; log count ~552 means configured budget is set far above code default (100) or the process is restarting/resuming and continuing numbered artifact files.

### L1001–1500 (WIN 003)
- Grepped `WIKI_*` env config in `graph/config.py` (timeouts etc.), searched repo for the stuck doc name / `prun-` ids; read `graph/wiki/__main__.py` and `document_map.py` compile section (L711 `raise SeedPlanningError(last_error…)`).
- Located compile artifacts under `data/.東京電力PG基幹指令システム-candidates/op-41ade9786f21418d926ea349cb18c23a/…/work/planning/compile/` and a parallel `…_candidates_backup/` tree.

### L1501–2000 (WIN 004)
- Read `publisher/pipeline.py` (L1-620): `PARSER_TIMEOUT = 7200.0`, `thread.join(timeout=1.0)`, error plumbing in pipeline events.

### L2001–2500 (WIN 005)
- Read more `publisher/pipeline.py`; grepped run lifecycle: `graph/workspace/writer.py:547 decision.regenerate |= set(failed)`.
- Timestamped compile artifacts of the stuck doc: `attempt-0543/0544/0545-error.txt` at 2026-09-29 09:12 — numbering continues across runs; read `graph/wiki/pipeline.py` L1300-1515 (`validate_seed_plan` → `structural_rejected` event).

### L2501–3000 (WIN 006)
- Read `graph/wiki/pipeline.py` seed-validation section and `publisher/pipeline.py` L620-1240 (generate-stage `retry_later` warnings, CancelledError handling).

### L3001–3500 (WIN 007)
- Listed current candidate-op compile dir; grepped sync/retry (`main.py:192 retry_failed(project)`, `publisher/pipeline.py:497` "cannot sync from a dirty last-good working tree"); read pipeline L530-760 (embedder error handling).

### L3501–4000 (WIN 008)
- Read `graph/workspace/writer.py` L230-315 (failed-outcome handling), `graph/workspace/project.py` L1-220, `graph/wiki/config.py` L1-75 (`map_attempts` default 100, `planner_attempts` 5, token caps).

### L4001–4500 (WIN 009)
- Read `publisher/queue.py` L820-1125: job lanes, `lane='fast' AND status='failed'` checks, `finish(project, jobs, error=…)`.

### L4501–5000 (WIN 010)
- Confirmed attempt history of stuck doc: `attempt-0001-error.txt` 2026-09-28T21:14 → 0545+ (continuous); searched `pipeline.json` / `watch-queue.sqlite` project state; scripted analysis of `attempt-*-error.txt`.
- Read `graph/workspace/writer.py` L1-240/340-470/447-700 (incremental edits, `decision.regenerate`).

### L5001–5500 (WIN 011)
- **ANSWER to Q1** (assistant): seed-plan compiler `graph/wiki/document_map.py:589-635` resumes from existing response files, sets `attempt` to highest previous + grants another `map_attempts=100`; `document_map.py:681-711` retries unchanged on the same validation error; `graph/wiki/config.py:21` per-run budget 100. Bounded per invocation but effectively unbounded across resumed invocations (attempts 1–100 … 545–552, identical deterministic error). Root semantic issue: model repeatedly places a boundary at line 9 inside atomic table `1-22`; HTTP 200 only means model responded — local validator rejects every response. Outer retry also at `publisher/pipeline.py:646-650` (re-appends failed doc to `changed`). No files modified.
- **USER Q2**: in normal runs the model sees validator output and improves the response — where is that logic, why not in this path?

### L5501–6000 (WIN 012)
- Assistant began tracing retry feedback: grepped `seed_plan_compile_prompt|last_error|previous_plan|feedback|validator` (hits: `writer.py:218`, `legacy.py:1016`); read `graph/wiki/prompts.py` L1-560 — prompt builders DO embed `検証エラー:\n{last_error}` (L72-80).

### L6001–6500 (WIN 013)
- Read `graph/wiki/model.py` + `graph/clients/chat.py` (one bounded structured call, `request_timeout`); `graph/wiki/pipeline.py` L870-1048 (page writing, `check_section`, `missing_important_information` judgment) and L400-445.

### L6501–7000 (WIN 014)
- Read `graph/wiki/prompts.py` L213-285/L350-405 (second `検証エラー: {last_error}` embedding, L219-231); `graph/wiki/legacy.py` L1685-1918 (coverage errors); began inspecting the stuck doc's compile attempt files.

### L7001–7500 (WIN 015)
- **ANSWER to Q2** (assistant): feedback exists in this path but only as prompt text, not a repair strategy. Path: `writer.py:354-368` → `graph/wiki/pipeline.py:1396-1434` → `document_map.py:635-699`; validator error passed to next prompt at `document_map.py:644`, formatted by `prompts.py:225-233`; `attempt-0544-prompt.md` proves the model received exact error + previous plan. Missing: `validate_seed_plan()` (`document_map.py:167`) can only snap an existing boundary to a table start/end; when whole table is `1-22` and model has two pages (`1-8`, `9-22`) there is no valid snap, so it errors; retry re-asks with same semantic plan; no deterministic "merge pages" repair, no no-progress detection, no fallback. Stronger logic lives in legacy planner: auto-repair `legacy.py:1470-1569`, validator-feedback retry + boundary suggestions `legacy.py:1790-1887`; used by `mode="chunks"` (`writer.py:373-380`), NOT the current `mode="wiki"` path.
- **USER Q3** (audit request): another agent was asked to show validator feedback to LLM and continue the conversation (not plain retry), with temperature 0.7 so output differs; user believes this logic is in the newer pipeline too. Directive: do NOT change anything — find it, and audit the full wiki builder pipeline for every step where LLM → structured output is manually validated and feedback generated; feedback must be shown back to LLM with retry (as remembered: retry limit 100, doc skipping, retry later).

### L7501–8000 (WIN 016)
- Audit begins (read-only): listed all `graph/wiki|formats|linker|workspace` py files; grepped `.structured(|validate_|check_|feedback|last_error|retry_temperature|temperature=|map_attempts|attempts` (hits: `writer.py:199` check_section, `writer.py:218` incremental-edit RuntimeError).

### L8001–8500 (WIN 017)
- Grepped for temperature 0.7 / retry-100 / skip / retry_later (`main.py:117,123 retry_failed`); read `graph/wiki/windows.py` L180-315 (`validate_inventory`), `graph/formats/context.py`, `graph/formats/__init__.py`; started deep read of `graph/wiki/pipeline.py` L540-850 (`_structured_with_artifacts`, `調査呼び出し失敗` error strings).

### L8501–9000 (WIN 018)
- Continued `graph/wiki/pipeline.py` L1048-1345 (structured-call retry plumbing, error propagation).

### L9001–9500 (WIN 019)
- Read `graph/formats/tabular.py` (error wrappers `return f"error: {exc}"`), started `publisher/queue.py` L240-790 (GROWI publication recovery; "publisher settings are required to recover an interrupted GROWI publication").

### L9501–10000 (WIN 020)
- Finished `publisher/queue.py` audit; read `graph/linker/service.py`, `graph/linker/neo.py`, `graph/linker/chunks.py` L1-380 (pattern: "one retry, then the caller's fallback").

### L10001–10500 (WIN 021)
- Read `graph/formats/context.py|pptx.py|docx.py|pdf.py` (format parsers, "context cancelled"); grepped linker structured calls (`PageReferencePlan|EdgeSuggestions|NeoEdgeSuggestions`); read `linker/neo.py|legacy.py|jev_judge.py` sections.

### L10501–11000 (WIN 022)
- Deeper reads of `graph/linker/chunks.py` and `graph/linker/service.py` (task error handling, `item.meta, error = await task`).

### L11001–11500 (WIN 023)
- Re-read the compile loop precisely: `graph/wiki/document_map.py` L120-220/580-715 (`last_error = ""` L587, `validate_seed_plan` L595), `graph/wiki/windows.py` L210-285, `graph/wiki/incremental.py` L200-255, `graph/wiki/page.py` L210-290, `graph/wiki/config.py` L13-55.

### L11501–12000 (WIN 024)
- **AUDIT REPORT (Q3 answer)** (assistant): newer pipeline has a partial version, inconsistently implemented. Failing compile path HAS: `document_map.py:655` retries SeedPlan; `:644` passes validator error + previous JSON plan; `prompts.py:225` shows feedback; `:659` retry temperature `1.0` (NOT the intended 0.7 — `config.py:49`). But: previous response is serialized into a new human prompt (not continued conversation); no no-progress detection; no mechanical "merge pages" repair; semantic plan keeps saying `1-8`/`9-22`; `structured_ainvoke()` (`chat.py:76`) only retries malformed JSON/schema, not manual validator failures.
- **Stage audit table** (feedback retry | temperature): window observation yes/0.0; regional planning yes/0.0; semantic planning empty-result only/0.0; final seed compile yes (error+prev plan)/retry 1.0, limit 100; parent summaries NO (fallback); reference research same-prompt only/base; section rewriting yes (text validator)/base; incremental structured page edits yes (feedback prompt, no hotter retry); PPTX section planning NO (filter/fallback); tabular citation checks yes (text feedback, no hotter retry); linker metadata/edges sanitize-or-skip, NO feedback retry.
- **Gaps found**: `graph/wiki/pipeline.py:418` (reference retry resends same prompt, no validator feedback); `graph/workspace/writer.py:173` (feeds errors back but never hotter temp); `graph/formats/pptx.py:85` (invalid section decisions filtered/fallbacked, no repair ask); `graph/linker/service.py:223,342` (filter/skip, no repair); `graph/linker/chunks.py:161` (metadata validation silently drops invalid entities/behaviours).
- "100 attempts + skip doc + retry later" is split: compiler budget `config.py:21` (100) + outer doc retry `publisher/pipeline.py:646` (retry once then fail; queue retry `publisher/queue.py:759`). No files changed.
- **USER Q4**: keep serialized-prompt style as-is (it works); implement the feedback retry loop at unsafe places with MINIMAL change, only where a manual validator produces proper feedback; don't break anything else.

### L12001–12500 (WIN 025)
- Pre-edit inspection: `graph/formats/pptx.py` L65-145, `graph/wiki/model.py` L70-115, `graph/wiki/pipeline.py` L404-442 (`last_error = ""` L417), `graph/workspace/writer.py`; baseline `git status/diff`; `prompts.py` L285-355, `pipeline.py` L116-145/L1187-1275; file headers (`PipelineError`, `ResumeUnavailable`).

### L12501–13000 (WIN 026)
- **EDITS APPLIED** (diffs): `temperature=config.retry_temperature if last_error else None` added to planner retries (`config.py`/`windows.py`/`document_map.py`); `last_error` param + `検証エラー: {last_error}` embedding in `prompts.py`; new `_reference_validation_error()` in `pipeline.py` (e.g. "useful_factsが空の場合はno_useful_information_reasonを具体的に書くこと"), `prompt_factory(last_error or None)`, `temperature=retry_temperature if last_error else None`, `validator=lambda candidate: _reference_validation_error(candidate, selected)`; `pptx.py` slide-section planning now retries invalid section starts with feedback (and proper CancelledError handling replacing bare `except Exception:`); `writer.py` incremental structured edits sample hotter after failed manual check.
- Assistant: changes in place; checking syntax + focused regressions.

### L13001–13500 (WIN 027)
- `git diff --check && git diff --stat` pass; grepped call sites (`reference_research_prompt(`, `_structured_with_artifacts(`, `judge_sections(`); `python -m compileall -q graph/wiki graph/formats/pptx.py graph/workspace/writer.py` OK; `pytest` → "command not found" (✗ 127) — no system pytest; agent will use project runner, no installs; smoke-tested `reference_research_prompt(...)` via python heredoc; read `tests/test_wiki_reference_context.py` L1-150 and `tests/test_mount_diff_pipeline.py` L530-585.

### L13501–14000 (WIN 028)
- Verified edit placement (`rg retry_temperature=` → `windows.py:251`, `document_map.py:400` etc.).
- Tests: `PYTHONPATH=. python tests/test_wiki_reference_context.py` PASS; `tests/test_mount_diff_pipeline.py` → FAILED (1 failure, 27 skipped) = pre-existing missing fixture `configs/diff_test_local.ini`, unrelated.
- Two behavioral heredoc checks that the new retries carry feedback + `temperature=0.7`: first runs failed (AssertionError), corrected and passed.
- `PYTHONPATH=. python tests/test_search_speedup.py` → FAIL `test_jev_error_falls_back_to_llm_for_that_chunk` ("Jev edge judge failed for target: unavailable") — to be resolved.

### L14001–14500 (WIN 029)
- Added acceptance gate `if _reference_validation_error(result, selected) is None:`; re-ran checks (pass); full diff review; re-audited all structured calls.
- Assistant (JA): implementation in; outer "100 attempts + defer document" control untouched; only where manual validators can return concrete fix messages, feedback now goes into the next LLM input and 2nd+ attempts regenerate at temperature 0.7.

### L14501–15000 (WIN 030)
- Full re-reads for verification: `graph/wiki/model.py`, `graph/clients/chat.py`, `graph/formats/context.py`, `graph/wiki/pipeline.py` L930-1120.

### L15001–15500 (WIN 031)
- Confirmed `temperature=config.retry_temperature if last_error else None` at `document_map.py:400,510,661`; read `graph/formats/pptx.py` L1-120, `graph/wiki/config.py` L1-110; began reading legacy planner for comparison (`legacy.py` L1680-1915/L2380-2475, `prompts.py` L45-190).

### L15501–16000 (WIN 032)
- Continued legacy comparison: `legacy.py` L940-1065, `PARTITION_RETRY_ATTEMPTS`/`temperature`/`ChatOpenAI(` greps, `split_window_until_valid|build_co…` greps.

### L16001–16500 (WIN 033)
- Read `legacy.py` header (`TIMEOUT=300`, `ChunkPlanningError`, `JobCancelled`, `make_llm`, TEMPERATURE greps); `clean_workdir`; `pipeline.py` L400-465/L600-715 (new `prompt_factory(last_error or None)` path); `git diff --stat`; `document_map.py` L560-720, `windows.py` L210-290.

### L16501–17000 (WIN 034)
- Assistant (JA): full search found the legacy splitter `graph/wiki/legacy.py` also has a manual-validation → next-prompt loop; minimal fix added there too (existing 6-attempt cap kept, non-first attempts at temperature 0.7); pure schema-conversion/fallback spots untouched.
- **Q4 COMPLETION SUMMARY**: retry temperature now `0.7` after validation failures; validator feedback passed into next prompt; added to window/regional/semantic/seed planning, reference research (incl. invalid source-range feedback), PPTX section planning, incremental page edits, legacy concept splitting; invalid cached reference results no longer blindly resumed; existing retry limits + document-skipping unchanged; serialized previous response stays a new human prompt (as requested). Validation: `compileall`, `git diff --check`, `tests/test_wiki_reference_context.py` 4 passed. Pre-existing unrelated worktree changes preserved.
- **USER Q5**: pasted fresh log — SAME error still at attempts 672–674 (10:26–10:27), loop still spinning after the fix.

### L17001–17500 (WIN 035)
- Inspected run dir: `attempt-0672/673/674-error.txt` (135 bytes each, 10:26–10:27); python analysis of response JSON; consulted langchain output-parsing docs (`parsing_error`); printed full error.

### L17501–18000 (WIN 036)
- Grepped boundary-error machinery: `legacy.py:909 enrich_boundary_error_with_suggestions`, `legacy.py:1648-1653 validate_safe_internal_boundary`; confirmed temperature edits present at `document_map.py:400/510/661`.

### L18001–18500 (WIN 037)
- Read `document_map.py` L120-235, `graph/wiki/markdown_blocks.py` L1-155 (`MalformedBlockError`, unclosed `<image-unit>`); config plumbing `graph/config.py` L250-315 (`wiki_request_timeout` default 300); `writer.py` L260-310, `graph/wiki/__main__.py`.

### L18501–19000 (WIN 038)
- Read `writer.py` L310-390/L500-595, `pipeline.py` L1400-1460; `make_llm` signature in `graph/clients/chat.py:15` (`temperature: float = 0.0, timeout: int = 300`); explored candidate data root for the stuck doc.

### L19001–19500 (WIN 039)
- Grepped `validate_seed_plan|page boundaries|table` (hit `document_map.py:566`).
- **USER Q6**: "my brother [previous agent] tried to make some fixes but they weren't applied and I still saw almost infinite retry — see where it went wrong."

### L19501–20000 (WIN 040)
- Assistant: earlier edits ARE in the working tree, but the failing compile validator itself was not changed; `attempt: 672` suggests numbering resumes across runs; verifying whether "100 attempts then skip" is enforced per invocation. Read `document_map.py` L100-240/L520-760.

### L20001–20500 (WIN 041)
- Verified retry plumbing: `pipeline.py:436 temperature=retry_temperature if last_error else None`; `make_llm`/`ChatOpenAI` construction; `wiki/config.py` L1-90, `prompts.py` L200-270.

### L20501–21000 (WIN 042)
- Grepped `SeedPlanningError|run_wiki|retry later|defer|map_attempts` (jev backends raise `JevUnavailable(... after retry)`); read `publisher/pipeline.py` L580-675, `publisher/queue.py` L920-1005, `writer.py` L280-390.

### L21001–21500 (WIN 043)
- `sha256sum` of attempt-0672/673/674 prompts+responses: **byte-identical**; dumped the 22-line original markdown.
- **Assistant**: artifacts settle it — feedback patch WAS active (prompts already contained validator error); failure is the validator only says "merge or move" while the semantic plan insists on two pages; all 22 lines are one indivisible Markdown table → exactly one valid repair: delete boundary 9, return one page 1–22. Plan: make validator feedback exact + regression test; also verify temperature propagation in installed LangChain.

### L21501–22000 (WIN 044)
- Inspected `langchain_openai.ChatOpenAI.with_structured_output` signature/source (temperature handling); re-grepped `validate_seed_plan|_compile_seed_plan` (callers `pipeline.py:1469`, `document_map.py:566`); read `markdown_blocks.py` L1-220, `graph/wiki/wire.py` L1-110, `tests/test_wiki_reference_context.py` L1-140.

### L22001–22500 (WIN 045)
- `git show HEAD:` comparison of `document_map.py`/`prompts.py`/`config.py`.
- **Assistant — precise miss found**: the failing compile retry loop (validator feedback + retry temp) already existed in HEAD before the previous edits; the patch mostly added the same pattern to other stages and changed 1.0→0.7; it never fixed this compiler's ambiguous feedback. Now patching only that gap + making repeated identical invalid answers produce changing, stronger feedback so deterministic generation can't receive the same prompt 100 times.
- New/updated test file: `tests/test_wiki_seed_plan_retry.py`; diff shows `last_error += (` escalation in compile loop.

### L22501–23000 (WIN 046)
- Diff review; `compileall` OK; `python -m unittest tests.test_wiki_seed_plan_retry` PASS.
- Assistant: validator now tells the model to merge pages 1–2 into exactly one page covering 1–22; retries at temp 0.7; repeated identical answer changes next feedback; **seed-plan version bumped** so next run starts a clean compile checkpoint instead of resuming attempt 679 under old prompt identity. Final call-site audit.

### L23001–23500 (WIN 047)
- Audited all `await model.structured(` call sites; re-ran both unittest modules; read `context.py`, `tabular.py` L430-530, `pipeline.py` L940-1085, `writer.py` L140-205, `document_map.py` L178-222/L596-730; verified new test file status (untracked).

### L23501–24000 (WIN 048)
- Regression test `tests/test_wiki_seed_plan_retry.py`: `test_indivisible_table_error_gives_the_exact_merge`; heredoc asserts error contains "Merge pages #1-#2 into exactly one page covering 1-22".
- **Assistant**: confirms previous fix never touched the failing compile path (loop + hot retry already in HEAD; patch changed other stages + temp 0.7). Real failure: indivisible table, conflicting semantic plan, "merge or move" too vague, byte-identical prompts/responses, attempt numbers accumulate across runs.

### L24001–24500 (WIN 049)
- Read `writer.py` L250-335, `pipeline.py` L1380-1510; grepped resume mechanics (`writer.py:570 UpdateDecision(tier=3, reason="resume-failed")`).

### L24501–25000 (WIN 050)
- Read `pipeline.py` L1-130/L730-840, `document_map.py` L540-730, `schemas.py` L45-90.
- **Assistant**: one risky detail in last change — bumping the global seed-plan version would invalidate completed per-document plans; correcting: completed plans keep their version and stay resumable; only the failed compiler checkpoint gets a new identity. Also converting the impossible atomic-table case into a **deterministic merge** (succeeds without asking the model to rediscover an impossible boundary).

### L25001–25500 (WIN 051)
- **USER Q7**: if a run stopped at 50 failed attempts, don't continue from there — start from request 1 fresh (let the model make fresh mistakes); and don't set model temp 0 anywhere — always 0.7 (temp 0 makes the model get stuck in thinking loops).
- **Assistant**: agreed — 0.7 baseline for every wiki model call including first attempts; failed compile stage restarts at attempt 1 on rerun; only that failed compile-stage directory is reset; completed observation/regional/semantic/page-writing/doc state stays intact. Read `graph/wiki/storage.py`; grepped all `temperature=0` sites.

### L25501–26000 (WIN 052)
- New config comment "Short in-stage repair budget. A failed document is checkpointed and retried later" (in-stage budget = 50); document_map comment "A failed compile is one resumable stage, not a conversation to continue" (attempt-1 reset logic).
- **Assistant**: new table rule — move boundary before/after the whole table when both resulting pages stay non-empty; if the document is only that table with no non-empty split, Python merges the requested pages into one full-table page; never manufactures an empty wiki page; consumes no validator retry.
- First attempts now use `config.temperature` (0.7) instead of `None` (diff: `if last_error else config.temperature`).

### L26001–26500 (WIN 053)
- Fresh-stage check passes: two separate invocations both emit compile attempt `1`, both call the model at 0.7; table-only `1–8 / 9–22` proposal deterministically compiled into one `1–22` page without an LLM retry. Running `tests.test_update_tiers` + `test_wiki_reference_context` (protects existing progress).

### L26501–27000 (WIN 054)
- Grepped all `temperature=0.0` defaults; **edit**: `graph/clients/chat.py:15 make_llm(... temperature: float = 0.0 ...)` → `0.7` (global default temp now 0.7). Re-ran unittests + compileall: pass.

### L27001–27500 (WIN 055)
- **Q7 COMPLETION (assistant)**: "You were right: 100 retries inside one stage was the wrong behavior." Rerun with resume preserves: completed documents, observation/regional/semantic/rewritten-page checkpoints; only the failed compile-stage workspace starts fresh at attempt 1; compile stage now allows **5 attempts**, then defers the document for later; successful seed plans keep version `ja-11` (not invalidated); avoid `--force`/no-resume to keep preservation. Atomic tables handled deterministically in `document_map.py:208`: boundary moves outside table when safe → move; would create empty page → merge affected pages; single-table document → one full-table page; no empty/full fake pair, no LLM retry for this structure.
- **USER Q8**: log shows incremental-edit failures "model could not apply incremental edits to 003-改定履歴(3).md: current source image img-… must appear exactly once" (several img ids, files 001–004), then `update_decision {"tier": 3, "reason": "escalated-most-pages", "hunks": 0}` and `[seed] start {"source_lines": 1063}` — "why escalated? nothing fucking changed?"
- **Assistant**: escalation triggered by incremental image-preservation validator, not text change. Source snapshot at escalation is byte-identical to current raw markdown; full rebuild happened because incremental path treated image-unit re-identification as failed content edits, then "most pages failed" threshold → tier 3; the `hunks: 0` is misleading (log emitted after decision object replaced by fresh tier-3 decision, discarding original patch counts). Investigating why unchanged image units got "must appear exactly once" failures; specific img ids `rg`'d with NO matches in workdir; comparing preserved `_backup` tree via `git diff --no-index`.

### L27501–28000 (WIN 056)
- Backup comparison (`git diff --no-index` vs `_candidates_backup` tree); image greps in raw/source/wiki; read `metadata/pipeline.json` + `_planning/source.json` (both `"last_error": ""`).

### L28001–28500 (WIN 057)
- Read `publisher/pipeline.py` L300-490, `publisher/queue.py` L160-210/L860-985; grepped `classify_update|source_sha256|target_sha256`.

### L28501–29000 (WIN 058)
- Read `graph/workspace/writer.py` L430-515, `graph/wiki/incremental.py` L1-150 (classification fallback `{"kind": "large", "ratio": 1.0, "hunks": 0, "reason": "classification-failed:…"}`).

### L29001–29500 (WIN 059)
- **Assistant — Q8 root cause**: the `.doc` binary hash is UNCHANGED, but the PARSER output changed from 1,005 lines with `<embed src="media/...">` to 1,063 lines with inline `<image-unit>` blocks + descriptions. Unchanged-source continuation was re-parsed (wrong); should have reused existing parsed Markdown and resumed the unfinished stage. Fixing continuation path + preserving original patch statistics on genuine escalation so `hunks: 0` can't hide what happened. Read related tests (`test_mount_diff_pipeline.py` L1-230/L420-590/L1360-1445).

### L29501–30000 (WIN 060)
- More test reads; `class SourceFile`, `publisher/scanner.py`, `graph/workspace/project.py` L1-110; began the edit.

### L30001–30500 (WIN 061)
- Edits applied; `compileall` OK; no existing test covered "must appear exactly once"/`patch_escalated` (rg ✗); ran `tests.test_mount_diff_pipeline.DiffPipelineSafetyTest.test_unchanged_source_resume_reuses_parsed_markdown` + `test_update_tiers` + `test_wiki_reference_context` — pass.
- **Q8 ANSWER (assistant)**: original `.doc` unchanged (binary SHA identical); escalation because parser output changed (`<embed>` → inline `<image-unit>`, 1,005→1,063 lines, pages 1–4 affected); incremental edits failed to preserve every new image placeholder; 4/5 pages needed regeneration > 50% threshold → tier 3; `hunks: 0` wrong because escalation replaced the decision object and erased stats before logging. **Fixes**: unchanged-source continuation reuses existing parsed Markdown (`publisher/pipeline.py:470`); forced runs still reparse; escalation preserves real hunks/pages in logs (`graph/workspace/writer.py:557`); 35 relevant tests pass. The currently running process already entered the old full-rebuild path — restart needed; it works in a candidate worktree; `[seed] start` alone has NOT promoted the rebuild over the last-good project.
- **USER Q9**: make image units interchangeable across the repo — embed format and image-unit format must be parsed and treated equivalent; only a changed inside hash/image counts as "changed".
- **Assistant**: plan — one canonical identity for `<embed>`, `<img>`, Markdown images, `<image-unit>` wrappers; ignore wrapper/description changes, still detect real image-byte changes; trace every parser/hash/diff/placeholder/restoration path first.

### L30501–31000 (WIN 062)
- Full image-machinery audit: `writer.py:193/196` (unknown/unresolved image placeholders), `page.py:260` (image-token must appear exactly once); read `tests/test_parser_client_route.py` L130-210, `tests/test_update_tiers.py` L520-590, `graph/wiki/images.py` L1-360.

### L31001–31500 (WIN 063)
- Read `graph/workspace/parser_client.py` L1-190, `publisher/pipeline.py` L220-265, `graph/common/markdown.py` L55-130, `graph/config.py` L1540-1670.
- Assistant: checking whether legacy `<embed>` references still have recoverable media bytes or persisted hashes — determines if cross-format equivalence can be exact.

### L31501–32000 (WIN 064)
- Deep reads: `graph/wiki/images.py` L1-280, `graph/wiki/incremental.py` L1-260, `graph/common/markdown.py` L1-220, `graph/config.py` L1-180.

### L32001–32500 (WIN 065)
- Deep reads continued: `graph/workspace/writer.py` L1-320, `graph/wiki/markdown_blocks.py` L1-240, `graph/growi/client.py` L1-280, `growi-search/researcher.py` L1-240.

### L32501–33000 (WIN 066)
- Continued deep reads: `writer.py` L1-320, `markdown_blocks.py` L1-240, `growi/client.py` L1-280, `growi-search/researcher.py` L1-240.

### L33001–33500 (WIN 067)
- Image-machinery greps (`strip_image_media|extract_image_units|neutralize_image_descriptions|diff_lines`); found `media/` files in candidate op; read `growi/client.py` L310-430, `test_parser_client_route.py` L1-240, `test_update_tiers.py` L520-610.

### L33501–34000 (WIN 068)
- GROWI image-attachment handling ("GROWI attachment response contains no image path", "invalid embedded image data"); read `writer.py` L70-180, `incremental.py` L240-330/L450-530 (tier-1 patch loop).

### L34001–34500 (WIN 069)
- First diff review of image changes (`git diff -- graph/wiki/images.py graph/wiki/incremental.py graph/common/markdown.py …`); read `images.py` L1-520.

### L34501–35000 (WIN 070)
- Read `images.py` (continued), `markdown_blocks.py` L1-220, `common/markdown.py` L1-140, `config.py` L1525-1595.

### L35001–35500 (WIN 071)
- Searched candidate data for actual media files (`image10.png` etc.).
- **Assistant**: old Markdown only stores paths (no image bytes) — cross-format equivalence must therefore key on identity/path + payload hash, not bytes alone.

### L35501–36000 (WIN 072)
- Read `parser_client.py` L1-230, `publisher/pipeline.py` L140-220/L500-660; grepped image regexes (`_IMAGE_UNIT_RE|IMAGE_UNIT_RE|DATA_IMAGE_RE|_MEDIA_RE`) across repo.

### L36001–36500 (WIN 073)
- Read `common/markdown.py` L60-135, `config.py` L1540-1585, `legacy.py` L350-430/L800-845/L1235-1310; data-heredoc probing; read `windows.py` L300-360, `pipeline.py` L1490-1540, `page.py` L90-140.

### L36501–37000 (WIN 074)
- Read `growi/client.py` L300-470, `writer.py` L80-220, `incremental.py` L230-500 (`apply_update only handles tiers 0-2`).

### L37001–37500 (WIN 075)
- Scanned all `source/original.md` files for `<image…` forms; grepped `parse/llm-wiki|image-description|image-unit`; checked for a local `doc-parser/` dir (none).
- **Assistant — normalization design**: preserve source line numbers: each image becomes ONE identity token; wrapper-only extra lines become blank lines; range mapper accounts for parser-added lines without sending no-op wrapper changes to the LLM; a changed inline payload still yields a different SHA-256 token = real edit.

### L37501–38000 (WIN 076)
- Located the actual `.doc` (source_mount in `configs/projectC.ini`/`.env`); olefile/magic probe (file not at guessed path); grepped `sanitize_lines|sanitized_source|numbered_prompt_block`; read `graph/config.py` L1-80, `growi/client.py` L1-60, `tests/test_growi_images.py` L1-80; began edits (image data decode error handling).

### L38001–38500 (WIN 077)
- **Q9 EDITS**: `<image-unit>` parse error now "unclosed or unmatched <image-unit> block"; incremental image errors normalized: "unrelated image {id} was changed or removed", "current source image {id} must appear exactly once", "deleted image {id} is still present"; image-regex audit (`_IMAGE_UNIT_RE|_IMAGE_DATA_RE|ImageMarkup|…`); `compileall` OK; behavioral heredoc checks; read `test_update_tiers.py` L1-100/tail, `test_growi_images.py` L80-140.

### L38501–39000 (WIN 078)
- **Assistant**: core parser + integrity checks now share ONE identity rule; extending remaining consumers (atomic chunking, GROWI publishing, sanitizers, legacy path) so no second regex-based interpretation can reintroduce the mismatch. Read `legacy.py` chunking sections.

### L39001–39500 (WIN 079)
- New test added: `tests.test_update_tiers.IncrementalDecisionTest.test_image_spellings_share_byte_identity_and_wrapper_only_diff_is_tier_zero` (runner: `.venv/bin/python -m unittest`). Grepped `unit_sha256|media_sha256`; read `publisher/pipeline.py` L40-120/L420-470, `publisher/history.py` L1-190 (`source_sha256` in history items).

### L39501–40000 (WIN 080)
- Read pipeline/history; ran broad test batch (`test_update_tiers`, `test_growi_images`, `test_parser_client_route`, `test_mount_diff_pipeline`, `test_wiki_reference_context`) — two failures surfaced (`test_failed_patch_escalates_to_page_regeneration`, `test_continue_adopts_dirty_same_base_candidate_after_failed_transaction`) — fixing.

### L40001–40500 (WIN 081)
- Image-regex audit across `graph|publisher|growi-search`; started full diff review; new shared module `graph/common/images.py` introduced.

### L40501–41000 (WIN 082)
- Full diff review continued (images.py, incremental.py, markdown_blocks.py, legacy.py, growi/client.py, tests).

### L41001–41500 (WIN 083)
- Edits; read `graph/common/images.py` L1-290, `markdown_blocks.py` L120-175, `wiki/images.py` L120-230.

### L41501–42000 (WIN 084)
- Targeted test passes; read `pyproject.toml` L1-130; `.venv/bin/python -m compileall -q graph publisher growi-search/researcher.py`; `git diff --check`; ran `growi-search` suite (`tests.test_researcher -v`) — passing.

### L42001–42500 (WIN 085)
- **Assistant**: representation-only update path now remaps physical line ranges and resumes WITHOUT patching pages; tests confirm changed decoded image bytes still produce a real update. Final regression pass; verifying user's unrelated dirty files not overwritten.
- **Edge case found**: same logo/image can legitimately occur multiple times — per-image "must occur exactly once" would wrongly reject; gate changed to compare COUNTS per content hash (multiset), still detecting real add/delete/replace.

### L42501–43000 (WIN 086)
- **Q9 COMPLETION (assistant)**: repo-wide image equivalence implemented — one canonical image parser in `graph/common/images.py` supporting `<image-unit>`, `<img>`, `<embed>`, Markdown images; inline images compared by SHA-256 of decoded bytes (base64 formatting, descriptions, alt, dimensions, wrapper no longer count as changes); external images with same normalized source path equivalent across wrappers; representation-only line changes remap page ranges as tier 0 without LLM (`incremental.py:47`); integrity compares occurrence COUNTS per content hash (repeated logos OK); prompt sanitization, chunk boundaries, incremental patching, GROWI publishing, legacy processing, growi-search sanitization all updated; unchanged source binaries keep reusing parsed Markdown. Validation: 80 focused pipeline tests passed; compile + `git diff --check` pass; no existing progress deleted.
- **USER Q10**: if the document hash is unchanged, why parse it again? Log: `04章_003…` shows `[parse] start` (parser doc, 97792 bytes) then `[parse] resumed` — parse stage entered at all for an unchanged doc.

### L43001–43500 (WIN 087)
- Read `publisher/pipeline.py` L430-680 (stage=index error handling, `shutil.rmtree(wiki_dir)`), `writer.py` L450-630; located the 005 doc + `_doc.md` files in data.

### L43501–44000 (WIN 088)
- Probed 005's source row: `"last_error": "SeedPlanningError: page boundaries around 9 place an entire page inside table 1-22; …"` (failed row, `source_sha256` blank); read `publisher/scanner.py` L1-130, `publisher/queue.py` L240-480/L880-1010 — resume condition: `not source or bool(source.get("last_error")) or not project.raw_file(raw_rel).exists()`.

### L44001–44500 (WIN 089)
- **Assistant (JA) — Q10 root cause**: on failure, `_source_row()` deliberately blanks `source_sha256` to `""`, so identical bytes next run look like an "unverified change" and the reuse condition fails even when parsed Markdown exists. Failure flag is already kept in `last_error`, so the hash should survive failures. Fixing + adding a test that the parser is not called again for the same hash after a failure.

### L44501–45000 (WIN 090)
- **Edits**: `publisher/pipeline.py:87` no longer erases `source_sha256` on failure (comment: failed wiki/generate stage ≠ source bytes changed; `last_error` marks it); test asserts failed row keeps `source_sha256 == "same-hash"` with `last_error` set.
- Assistant (JA): "previously failed + same input bytes + existing raw Markdown" now → `stage=parse resumed` without calling DOC/PDF parser; regression running.
- **Follow-up fix**: old stored failed rows (source_sha256 already empty) also skip the wasteful reparse — the queue's current target hash is used in the check; if it matches the file and raw Markdown exists, the old row is treated as `resumed` (works for the existing 005 state too).
- `DiffPipelineSafetyTest` runs pass; one probe heredoc hit `ModuleNotFoundError: publisher.workspace` (wrong import in throwaway script, corrected).

### L45001–45500 (WIN 091)
- **Q10 ANSWER (assistant)**: failed rows were erasing `source_sha256` → unchanged docs treated as needing fresh parse. Fixed in `publisher/pipeline.py`: failed rows retain the source hash (`last_error` still triggers retry); stale rows reuse parsed Markdown when staged blob identity + hash match; parsing skipped when raw Markdown exists and source unchanged; forced runs still reparse; safe fallback when raw Markdown/identity unavailable. `stage=parse start` may still appear (stage-begin log) but is followed by `stage=parse resumed` — parser not called. Validation: all 20 `DiffPipelineSafetyTest` tests pass.
- **USER Q11**: "why this?" + log: `04章_003/004` still show `[parse] start`→`resumed`; big JSON batch where many `F_組合せ試験/06_F3試験実施版/初版/SI*.doc` entries show `"status": "changed"` yet `"tier": 0, "reason": "unchanged"` (contradictory display); and a REAL error: `02章_01一般事項.doc: FileNotFoundError: wiki run has no published page 001-…` + `pipeline omitted claimed paths: [02章_01一般事項.doc]`.
- **Assistant (JA)**: log mixes at least 3 problems — corrupted JSON display, `no-previous-state` full rebuild, and the real error (missing published wiki page); matching each to its code site. Greps: `graph/wiki/export.py:43` (`FileNotFoundError: wiki run has no published page`), `graph/growi/client.py:864`. Read `export.py` L1-90, `queue.py` L990-1025, `pipeline.py` L620-650, `writer.py` L480-620.

### L45501–46000 (WIN 092)
- Read `export.py` L1-90, `queue.py` L990-1025, `pipeline.py` L620-650, `writer.py` L250-290/L480-620/L620-645 (`no wiki output for`, `shutil.rmtree(work)`).
- **USER Q12**: "ans in english" (previous answer had been Japanese).
- **Q11 ANSWER (English, assistant)** — three separate issues: (1) `tier: 3, reason: "no-previous-state"` on F3 PDFs = wiki STATE missing/incomplete (`state/source/original.md` or `state/plan.json` absent) → full rebuild, not a source-hash change; `touched: []` is not proof pages weren't generated (linker work happens later in batch). (2) `status: "changed"` + `tier: 0, reason: "unchanged"` is misleading but expected — queue status means "queued source was processed", not content changed; authoritative fields are `tier/reason/rebuild`; those entries did NOT modify pages. (3) REAL failure: `FileNotFoundError: wiki run has no published page` — plan references a page filename missing under `state/.../wiki/` (stale/incomplete wiki state; `plan.json` inconsistent with generated pages); `pipeline omitted claimed paths` is a secondary queue error (doc claimed, pipeline failed before completion). Mangled JSON (`human_edithed`, duplicated `rebuild`) = terminal output interleaving (`^[[A` escapes), not real dict keys.

### L46001–46500 (WIN 093)
- **USER Q13**: how to fix / easiest way to continue WITHOUT starting over; will new runs be safe? — no code changes, advice only.
- Assistant: tracing resume path + failed doc state, read-only. Greps (`resume-failed` at `writer.py:589`, `test_update_tiers.py:499`); read `queue.py` L560-640, `main.py` L100-215.

### L46501–47000 (WIN 094)
- **Assistant**: completed documents are already preserved; only the claimed batch containing the broken document failed. Checking whether the failed candidate worktree still exists (determines if `--continue` can reuse checkpoints). Located the broken doc's `work` dir; listed configs (`target_name` per ini).

### L47001–47500 (WIN 095)
- Read `project.py` work_dir/state_dir; **`.venv/bin/python main.py queue status --project projectC`** — dumped queue rows for the candidate op.

### L47501–48000 (WIN 096)
- Queue probe: **191 jobs with status `failed`** (one atomic batch); read `queue.py` L500-575 (candidate adoption by `base_commit`).
- **Q13 ANSWER (recovery recipe)**: no restart needed; completed work is inside candidate `op-41ade...`; source snapshot matches current raw. Steps: 1) stop watcher/worker; 2) move ONLY the broken doc's stale `plan.json` aside; 3) resume with `--continue`:
  - `CAND='data/.東京電力PG基幹指令システム-candidates/op-41ade9786f21418d926ea349cb18c23a/東京電力PG基幹指令システム'`
  - `BROKEN="$CAND/metadata/state/Bシステム設計/BC0機能仕様書（別）/F訓練シミュレータ/01機能・オペレーション編/Ｊ改定/02章_01一般事項.doc"`
  - `RECOVERY_DIR="$(mktemp -d)"; mv "$BROKEN/state/plan.json" "$RECOVERY_DIR/plan.json"`
  - `.venv/bin/python main.py -v sync --project projectC --continue`
  - Why: `--continue` adopts the candidate (keeps completed docs + LLM checkpoints); removing the inconsistent plan puts the doc in resumable initial-build mode; observation/planning checkpoints reused; 7 missing pages generated; ~190 other failed jobs were failed only by the shared atomic batch → should resolve as unchanged. **Do NOT run plain `sync` first** (orphan candidate may be pruned).
  - Future-protection gap: `plan.json exists + source unchanged + page files missing → tier 0 → export FileNotFoundError`; minimal permanent fix belongs in `writer.py` (completeness check before accepting tier 0, then `resume=True`); `export.py` stays strict. No changes made.
- **USER Q14**: "is this change minimal? so future errors don't occur?" — **Assistant**: yes — one helper + one decision branch + one regression test (~20-30 lines) in `graph/workspace/writer.py`: before tier 0 verify `plan.json` exists, every planned page exists under `wiki/`, `run.json`+`manifest.json` exist; if incomplete → classify `interrupted-initial-build`, call builder with `resume=True`. No queue/parser/exporter redesign.
- **USER Q15**: "do it" — **Assistant**: implementing completeness guard in workspace writer only, routing incomplete unchanged runs through existing resume path, one focused regression test; no queue/exporter behavior changes.

### L48001–48500 (WIN 097)
- `git status`; read `tests/test_update_tiers.py` L300-445 (existing pattern: `patch.object(writer, "build_wiki_output", side_effect=AssertionError("full wiki runner invoked"))`).

### L48501–49000 (WIN 098)
- Read `writer.py` L430-480; grepped `manifest.json|run.json` (graph/wiki, workspace, tests); read `incremental.py` L230-320.
- **Assistant**: guard preserves a valid stored plan + completed page files; only unchanged source with incomplete run is redirected to `resume=True`; normal unchanged/incremental paths untouched. Edit applied; `git diff -- graph/workspace/writer.py tests/test_update_tiers.py` reviewed.

### L49001–49500 (WIN 099)
- **Assistant**: code change in place; no `pytest` in env — running repo tests via `unittest`; checking interrupted-run case. Ran `WriterTierTest -v` + `tests.test_update_tiers tests.test_mount_diff_pipeline -v`.
- **Assistant**: integration safety test exposed a compatibility concern — some valid existing states PREDATE `run.json`/`manifest.json`; narrowing the completeness check to the actual unsafe condition (planned page files missing) so old completed runs aren't unnecessarily rebuilt.

### L49501–50000 (WIN 100)
- Guard narrowed; `git diff --check`; suites pass.
- **Assistant**: one adjacent edge case in the same guard — source CHANGED while prior run incomplete must rebuild fresh (not incremental export from missing pages).
- **Q15 COMPLETION (assistant)**: minimal fix implemented — validate every page in `plan.json` exists before tier 0/export; unchanged incomplete runs preserve checkpoints + resume stored plan; changed incomplete runs rebuild cleanly; no queue/exporter changes; the current broken candidate is correctly detected as incomplete. **No more need to move `plan.json`** — continue with `.venv/bin/python main.py -v sync --project projectC --continue`. Tests: 34 update-tier + 20 queue/pipeline safety passed. Change in `graph/workspace/writer.py:456`, regression in `tests/test_update_tiers.py:536`. No candidate/generated data modified.
- **USER Q16**: all docs done, right? It started the linker phase — clear the linker process; switch linker to LOCAL GGUF instead of API via CONFIG (project INI overriding .env), possible?

### L50001–50500 (WIN 101)
- Grepped linker backend config: `jev/config.py:65` `WIKI_JEV_BACKEND must be torch|gguf|hosted|llm2jev`; `:71 WIKI_JEV_GGUF_MANY_MODE exact|batched`; `wiki_linker_tiebreak_floor` (default 0.4, `jev_judge.py:185`). `ps` checks for live linker/jev/llama processes.

### L50501–51000 (WIN 102)
- **Assistant**: no active Python/JEV/linker process, but queue still records 191 jobs `running` in the retained candidate transaction; distinguishing "wiki done, linker interrupted" from "docs unfinished". Probed candidate dir + marker states; read `graph/linker/service.py` L390-470 (`wiki_linker_judge llm|jev`, `wiki_linker_mode legacy|neo`; doc without complete marker → full linker work).

### L51001–51500 (WIN 103)
- `ls jev/models/chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF` (local model dir exists); read `service.py` L225-260.

### L51501–52000 (WIN 104)
- Read `graph/linker/__main__.py` L45-110 (`engine.adecide_batch`); began Q16 work: edit `configs/projectC.ini` (explicit local GGUF selection, project settings > .env) + reset partial linker DB/markers.
- Ran `tests.test_search_speedup` → **FAIL again: `test_jev_error_falls_back_to_llm_for_that_chunk`** (same test that failed back in WIN 028).

### L52001–52500 (WIN 105)
- **Q16 STATE (assistant)**: docs effectively finished — 191/191 wiki output folders; 188 normal docs complete; 3 Excel docs with markers; linker interrupted: 188 markers `pending`, 3 `render_pending`, no live linker process.
- Config now resolves to GGUF independently of .env; smoke-testing the actual local `Q4_K_M` runtime before clearing the partial linker DB; probed `jev/engine.py` (`decide_batch` L150, `decide_many` L162), `graph/linker/catalog.py` L140-205.

### L52501–53033 (WIN 106–107) — END OF LOG
- **Assistant**: local GGUF smoke test succeeded (`backend=gguf`, Q4 model + local `jev-score` binary); backed up + cleared ONLY partial linker artifacts: SQLite catalog, three partial `links.json` (34 edges), all 191 markers → `pending`; all wiki pages/planning/chunks/checkpoints preserved; **backup: `/tmp/llm-wiki-linker-reset-op-41ade-20260929-123658.zip`**.
- Read `tests/test_search_speedup.py` L555-605 (the failing Jev test: mocks `judge_edges` → RuntimeError("unavailable"), expects LLM fallback call) and `graph/config.py` L158-185.
- Final code change: `configs/projectC.ini` adds `wiki_linker_tiebreak_floor = 0` (alongside `wiki_linker_judge = jev`, `wiki_jev_backend = gguf`).
- **Final assistant message**: generation complete (191/191; 188 normal + 3 Excel); linker state cleared as above; projectC.ini overrides .env with local GGUF at `configs/projectC.ini:12`; resume with `.venv/bin/python main.py -v sync --project projectC --continue` (do NOT omit `--continue` — completed docs not yet promoted from candidate). Note: chunk metadata generation can still use the main chat model when no cached metadata exists; eliminating every linker-related API call would be a separate change.
- **FINAL UNANSWERED USER QUESTION (L53031)**: "are you sure it would [use] my local gguf? where is the gguf file located?" — next agent must answer this (GGUF path per `.env:47`: `/home/seigyo/llm-wiki/jev/models/chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF`, config `WIKI_JEV_GGUF_LOCAL_PATH`; score binary `WIKI_JEV_SCORE_BIN=…/build/jev-score`).

## Consolidated handoff

**Project**: `/home/seigyo/llm-wiki` — llm-wiki pipeline converting Tokyo Electric Power PG main-command-system documents (.doc/.xlsx) into wiki pages + GROWI publication. Main candidate operation: `data/.東京電力PG基幹指令システム-candidates/op-41ade9786f21418d926ea349cb18c23a/東京電力PG基幹指令システム` (a `_candidates_backup` sibling tree exists). Project config: `configs/projectC.ini`, env: `.env`. Test runner in this env: `.venv/bin/python -m unittest tests.test_X` (no system `pytest`; bare `python` needs `PYTHONPATH=.`).

**Session timeline (2026-09-29, ~09:12–12:45)**:
1. **Q1** — endless `[plan] compile_retry` (552+ attempts, same error "page boundaries around 9 place an entire page inside table 1-22"). Root cause: compile loop in `graph/wiki/document_map.py` is bounded per invocation (100) but resumes attempt numbering across runs → effectively unbounded; model kept proposing 1-8/9-22 inside one indivisible 22-line table.
2. **Q2** — validator feedback IS sent to the model (prompt text) but no repair logic on the `mode="wiki"` path (legacy `mode="chunks"` had auto-repair + boundary suggestions).
3. **Q3** — full pipeline audit (no changes): per-stage table of feedback-retry/temperature; gaps at `pipeline.py:418` (reference research), `writer.py:173` (no hotter temp), `pptx.py:85`, `linker/service.py:223,342`, `linker/chunks.py:161`.
4. **Q4** — implemented validator-feedback retries minimally: `temperature=config.retry_temperature if last_error else config.temperature` (0.7) across window/regional/semantic/seed planning, reference research (new `_reference_validation_error()` in `graph/wiki/pipeline.py`), PPTX section planning, incremental page edits, legacy concept splitting; `make_llm` default temperature 0.0→0.7 (`graph/clients/chat.py:15`); invalid cached reference results no longer blindly resumed.
5. **Q5/Q6** — retry loop STILL spinning (attempts 672-674 byte-identical). True miss: the compile loop + hot retry already existed in HEAD; feedback text was too vague ("merge or move"). Fix: exact feedback ("Merge pages #1-#2 into exactly one page covering 1-22"), escalating feedback when the identical invalid answer repeats, and **deterministic Python merge** for atomic tables (`document_map.py:208`) so the impossible case needs no LLM retry.
6. **Q7** — 100 in-stage retries was wrong: compile stage now **5 attempts** then defers the doc (checkpointed, retried later); failed compile stage restarts at attempt 1 on rerun (only that stage dir reset); completed plans keep version `ja-11`; temp 0.7 baseline everywhere (no temp 0).
7. **Q8** — tier-3 escalation with `hunks: 0` on an UNCHANGED .doc: parser output format had migrated `<embed src=…>` → inline `<image-unit>` (1005→1063 lines); reparse on unchanged source + misleading log. Fix: unchanged-source continuation reuses parsed Markdown (`publisher/pipeline.py:470`); escalation logs keep real stats (`graph/workspace/writer.py:557`).
8. **Q9** — repo-wide image equivalence: canonical parser `graph/common/images.py` (`<image-unit>`, `<img>`, `<embed>`, MD images); inline images by SHA-256 of decoded bytes; external by normalized path; wrapper/description changes = tier 0 (no LLM); integrity = occurrence counts per content hash.
9. **Q10** — failed queue rows erased `source_sha256` → unchanged docs re-parsed; fixed in `publisher/pipeline.py` (hash survives failures; stale rows matched via queue's current hash).
10. **Q11** — log triage: (a) `no-previous-state` tier-3 = missing wiki state, (b) `status: changed` + `tier: 0 unchanged` = queue-status semantics, misleading, (c) real `FileNotFoundError: wiki run has no published page` = `plan.json` inconsistent with generated pages (`graph/wiki/export.py:43`); mangled JSON = terminal interleaving.
11. **Q13/Q14/Q15** — recovery + permanent fix: completeness guard in `graph/workspace/writer.py:456` (every planned page must exist; `run.json`/`manifest.json` markers NOT required — pre-existing states predate them; incomplete+unchanged → resume stored plan; incomplete+changed → fresh rebuild; regression `tests/test_update_tiers.py:536`). Recovery recipe (superseded by the guard): move broken doc's `state/plan.json` aside + `main.py -v sync --project projectC --continue`.
12. **Q16** — generation verified complete (191/191: 188 normal + 3 Excel); partial linker state backed up (`/tmp/llm-wiki-linker-reset-op-41ade-20260929-123658.zip`) and cleared (catalog DB, 3 partial `links.json`, 191 markers → `pending`); `configs/projectC.ini` now sets `wiki_linker_judge = jev`, `wiki_jev_backend = gguf`, `wiki_linker_tiebreak_floor = 0` (project INI overrides `.env`); local GGUF runtime smoke-tested.

**Current state**: candidate `op-41ade…` holds all 191 completed wiki docs, NOT yet promoted. Linker is queued (191 `pending` markers) and configured for local GGUF JEV. Queue still shows 191 jobs `running` in the candidate transaction.

**Resume command** (the single next action):
```bash
.venv/bin/python main.py -v sync --project projectC --continue
```
Do NOT omit `--continue` (orphan candidate may be pruned; completed docs not yet promoted).

**Open items for next agent**:
1. **Unanswered final question**: "are you sure it would [use] my local gguf? where is the gguf file located?" — verify and show: `WIKI_JEV_GGUF_LOCAL_PATH=/home/seigyo/llm-wiki/jev/models/chaoliangUNSW/Jev-Style-0.8B-Decision-v3-GGUF` (`.env:47`), score binary `WIKI_JEV_SCORE_BIN=…/Jev-Style-0.8B-Decision-v3-GGUF/build/jev-score` (`.env:53`), config keys `wiki_jev_backend = gguf` in `configs/projectC.ini:12-13`, backend validation `jev/config.py:65`. Confirm the .gguf file actually exists in that dir.
2. **Failing test (unresolved at log end)**: `tests/test_search_speedup.py::LinkerJevJudgeTest::test_jev_error_falls_back_to_llm_for_that_chunk` — JEV unavailable → LLM fallback expected (`accepted=[{"chunk_b": "peer"}], calls=1, fallbacks=1`) but got `([], 0, 1)`: `_filter_target` in `graph/linker/service.py` did not call `_filter_groups` fallback. Failed at original L13969 and L51980; test source read at L52802; no fix visible before log ends. Check whether `wiki_linker_tiebreak_floor = 0` changed behavior; if still failing, fix `_filter_target` fallback path (careful: it's a strict-mode neo/jev path).
3. Chunk metadata generation can still call the main chat API when no cached metadata exists — user may want that eliminated ("separate change" per final message).
4. Pre-existing unrelated failures, not caused by this session's edits: `tests/test_mount_diff_pipeline.py` needs `configs/diff_test_local.ini` (missing fixture).
5. Worktree had pre-existing unrelated dirty files (growi-search/*, jev/*, pyproject.toml, uv.lock, requirements.txt, jev/backends/llm2jev.py) — preserved, do not clobber.
