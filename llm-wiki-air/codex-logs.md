# Codex conversation

## User

See the main.py and graph backend, particularly sync path, currently each document ingestion, from parser to publish is taking a lot of time. I want to measure how much time for 2 docs. So, firt of all read the code, the current print function across the codebase is v messy, so i wannt make default print statemtnes debug mode, so write a custom print function, which by default prints debug, then there is prod prints, which can be called using print(whatever, prod=True) or something easy like this + tqdm's . IN this prints i want to measure which stage the current doc is in, and how much time that stage took, + tqdm of how many docs processed. DOnt change anything from the code, just observe and list all the files involved.
Read the whole sync path. Nothing modified — observation only.

Sync Flow (python main.py sync)

main.py:170 cmd_sync → pipeline.republish_if_stale → worker_lock → loop {queue.scan → queue.work_once} → index.build_index → _report.
publisher/queue.py:1030 work_once → _work_once_locked (queue.py:838): claim jobs → candidate git worktree → move_sources/delete_sources → pipeline.sync_once → commit/promote → build_index.
publisher/pipeline.py:706 sync_once is the real per-document pipeline. Per-doc loop at pipeline.py:812 over changed.

 #      Stage                                                Where it runs                                                                                       Existing timer?
━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 1      scan / queue-claim                                   publisher/queue.py:261, queue.py:882                                                                none
─────  ───────────────────────────────────────────────────  ──────────────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────
 2      capture (GROWI pull + human overlay)                 pipeline.py:336 _capture_remote, pipeline.py:371                                                    none
─────  ───────────────────────────────────────────────────  ──────────────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────
 3      parse (parser HTTP + image describe)                 pipeline.py:818-874 → graph/workspace/parser_client.py                                              yes elapsed_seconds pipeline.py:874
─────  ───────────────────────────────────────────────────  ──────────────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────
 4      wiki (seed→context→research→write→rewrite→export)    pipeline.py:877-904 → graph/workspace/writer.py:484 → graph/wiki/pipeline.py:1407                   yes pipeline.py:904
─────  ───────────────────────────────────────────────────  ──────────────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────
 5      linker (batch, all docs)                             pipeline.py:929-957 → writer.py:758 run_linkers → graph/linker/service.py                           started service.py:397, never emitted
─────  ───────────────────────────────────────────────────  ──────────────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────
 6      growi-publish                                        pipeline.py:475 _publish_sweep, timer at :547                                                       yes :590 + log.info :572
─────  ───────────────────────────────────────────────────  ──────────────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────
 7      index                                                publisher/index.py:432 build_index (called from _publish_sweep :613, work_once :1044,               none
                                                             cmd_sync :211)
─────  ───────────────────────────────────────────────────  ──────────────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────
 8      history candidate/promote/rollback                   queue.py:921/994/1002, publisher/history.py                                                         none

Files Involved (write set for the print + timing + tqdm work)

main.py — _progress (main.py:67) is the only renderer for progress events; _report (main.py:71) prints final JSON rows; -v/--verbose gate at main.py:416; 22 print() sites, all machine-readable JSON except cmd_check.

publisher/pipeline.py — stage boundaries + the 3 existing timers; missing timers for capture/linker/index.

publisher/queue.py — batch-level queue-claim/history events; where tqdm total per batch (result["paths"], jobs) is known.

publisher/index.py — index stage, already emits current/total (index.py:525, :567), no elapsed.

graph/linker/service.py — emits linker page_curated/done with current/total; has started at :397 but no elapsed in the done event (:843).

graph/workspace/writer.py — wiki/linker sub-step events (:562, :617, :683, :749).

graph/wiki/pipeline.py — _emit helper at :132; sub-stages seed, context, research, write, rewrite, publish.

graph/wiki/legacy.py — the messy prints: 13 bare print() in chunks :507, :1568, :1842, :1886, :1901, :1913, :2022, :2421-2472 ([Enrichment], [Planning], [WARNING]). Only real polluter of stdout in library code.

graph/formats/tabular.py, graph/formats/xlsx.py — extra tabular/excel-story stages for xlsx/csv docs (:521-635, :183-280).

Read-Only Hot Path (no changes needed)

graph/growi/client.py (GrowiPublisher.publish_documents :816, publish_pages :668), graph/workspace/parser_client.py, graph/clients/chat.py, graph/clients/embeddings.py, graph/wiki/document_map.py (plan), graph/wiki/windows.py (observe), graph/wiki/incremental.py, graph/wiki/export.py, graph/linker/{catalog,neo,legacy,chunks,render,javascript→jev_judge}.py, publisher/{scanner,ledger,history,human_changes,activity}.py, graph/workspace/project.py, graph/config.py.

Not in the ingest path: publisher/human_semantic.py (untracked, zero importers), legacy_recovery.py, live_verification.py.

Observability Today Is Split Three Ways

Event bus: on_progress({"stage","step","current","total",...}) — 40+ sites, wired only when -v (_progress if args.verbose else None).
logging: 16 calls, all in publisher/pipeline.py, format set at main.py:512 (LOG_LEVEL, default INFO) — the stage=… elapsed=… lines live here.
Bare print(): main.py CLI output + 13 polluting lines in graph/wiki/legacy.py.
tqdm 4.70.1 is already installed (transitive via transformers/openai) and imported nowhere.

Gaps For "2 docs, per-stage seconds"

No stage timer for capture, linker, index, queue/history — three of eight stages are unmeasured.
cmd_sync discards scan()'s return (main.py:187), so the run's doc total is thrown away; that dict (added/updated/moved/deleted) is the natural tqdm total.
Per-page publish granularity is absent — publish_documents is one opaque call per sweep.

Note Before I Write Anything

Ladder says reuse, not rebuild: _progress already parses stage/step/current/total, and 3 stages already ship elapsed_seconds. The lazy version is one new print(..., prod=True) shim + _progress upgraded to tqdm + 3 missing timers + legacy.py's 13 prints flipped to debug — not a new logging framework.

main.py JSON rows are prod output (_report, queue status, check); a default-to-debug print must not swallow them.

Want me to implement that minimal version now — new graph/common/verbose.py (or a print shim in main.py), tqdm bar in _progress, and the 3 missing timers?
  Worked for 2m 9s · 1:49 PM

oh ok, most of the prints etc are basically loggin only, unify them using logging level debug first, currently most of it is logging, we will use simple normal prints for tqdm/production logs then, currently make each current log, print etc with -v flag, a uniform debug level log, we would make it so that if we activate -v it would all appear back in unison. This is v v important, dont touch anything else, this is v v minimal and surgical change at many small places.
Nothing else touched — here's exactly my footprint.

Changed (4 files)

graph/wiki/legacy.py — all mine: added import logging (:5), log = logging.getLogger(__name__) (:20), and 13 print( → log.debug( at :510, :1571, :1845, :1889, :1904, :1916, :2025, :2424, :2434, :2444, :2456, :2464, :2475. Message text untouched.

publisher/pipeline.py — 7 lines only: log.info( → log.debug( at :582, :809, :829, :856, :919, :1376, :1612.

publisher/human_changes.py — 4 lines only: log.info( → log.debug( at :833, :847, :1000, :1254.

main.py — three spots: log = logging.getLogger(__name__) at :40; _progress now log.debug(...) instead of print(..., flush=True) at :80; main() parses args before basicConfig and uses level="DEBUG" when -v, else LOG_LEVEL/INFO, and caps httpx/httpcore/h11/urllib3/openai/langchain/anthropic at INFO so -v isn't a socket firehose.

Deliberately not touched

Every CLI print() in main.py (the JSON result rows, check lines, _report) — those stay prod for your later tqdm work.
All log.warning / log.error, and logging.error in _report.
All on_progress/on_event wiring, including the _progress if args.verbose else None ternaries and -v flag definition.
No new files, no tqdm yet. graph/config.py, graph/growi/client.py and the .gitignore/README/tests churn are your other agent, not me.

Verified

py_compile clean on all four files.

Default level: _progress emits nothing. With -v: [parse] done 1/2 (50%) {"file": "a.docx", "elapsed_seconds": 12.3} — so -v pulls progress, pipeline timing, and legacy [Planning]/[Enrichment] into one stream.

python main.py -h still fine. No test in tests/ captures stdout, so the print→logging swap can't affect them.

Two calls for you

Six of those legacy lines were real degradation notices ([WARNING] Discontinuous source ranges, the three Failed to infer ... Using fallback, using mechanical fallback). Uniform-debug hides them at default INFO. Want them log.warning instead?

I ran the test files and some fail, but with a second agent live in the tree and .git read-only I couldn't get a clean baseline before you stopped me. Want me to re-run them now that you've stopped the other agent, or leave it? continue the past agent's work to make the current logs all debug, we will add normal prints later

## Assistant

I’ll inspect the current worktree and the sync-path logging sites, preserve unrelated changes, and make only the minimal logging-level edits needed so the existing diagnostic output is unified under `-v`/DEBUG. Then I’ll run focused syntax checks and report the exact files changed.

## Activity

    $ git status --short && git diff --stat && git diff -- main.py publisher/pipeline.py publisher/human_changes.py graph/wiki/legacy.py
     M .gitignore
     M README.md
     M graph/config.py
     M graph/growi/client.py
     M graph/wiki/legacy.py
     M main.py
     M publisher/human_changes.py
     M publisher/pipeline.py
     M tests/test_human_changes.py
    ?? docs/human-semantic-calibration.json
    ?? publisher/activity.py
    ?? publisher/human_semantic.py
    ?? publisher/legacy_recovery.py
    ?? publisher/live_verification.py
    ?? publisher/prompts/
    ?? tests/test_human_sync_rollout.py
     .gitignore                  |   1 +
     README.md                   |  41 +++++
     graph/config.py             |  50 ++++++
     graph/growi/client.py       | 231 +++++++++++++++++++++++++-
     graph/wiki/legacy.py        |  29 ++--
     main.py                     |  88 +++++++++-
     publisher/human_changes.py  | 387 +++++++++++++++++++++++++++++++++++++++++---
     publisher/pipeline.py       | 182 +++++++++++++++++++--
     tests/test_human_changes.py |   8 +
     9 files changed, 959 insertions(+), 58 deletions(-)
    diff --git a/graph/wiki/legacy.py b/graph/wiki/legacy.py
    index 71a600e..f41f373 100644
    --- a/graph/wiki/legacy.py
    +++ b/graph/wiki/legacy.py
    @@ -2,6 +2,7 @@ from __future__ import annotations
    
     import asyncio
     import json
    +import logging
     import re
     import shutil
     import threading
    @@ -17,6 +18,8 @@ from pydantic import BaseModel, Field
    
     from graph.common.images import find_images
    
    +log = logging.getLogger(__name__)
    +
     # region Config and Models
    
    
    @@ -504,7 +507,7 @@ def add_or_update_file_record(
                         last[1] = max(last[1], end)
                     else:
                         merged.append([start, end])
    -                    print(
    +                    log.debug(
                             f"[WARNING] Discontinuous source ranges in {filename}: {record['source_ranges']}"
                         )
    
    @@ -1565,7 +1568,7 @@ def try_auto_repair_concept_partition_boundaries(
             if repaired_boundary == bad_boundary:
                 return None, error
    
    -        print(
    +        log.debug(
                 f"[Planning] {label}: auto-repairing unsafe boundary "
                 f"before line {bad_boundary} -> before line {repaired_boundary}"
             )
    @@ -1839,7 +1842,7 @@ async def split_window_until_valid(
             if stop_check and stop_check():
                 raise JobCancelled("chunk planning cancelled")
    
    -        print(
    +        log.debug(
                 f"[Planning] {label}: split attempt {attempt}, "
                 f"source lines {source_start}-{source_end}"
             )
    @@ -1883,7 +1886,7 @@ async def split_window_until_valid(
                 )
    
                 if repaired is not None:
    -                print(
    +                log.debug(
                         f"[Planning] {label}: accepted after automatic boundary repair."
                     )
                     return repaired
    @@ -1898,7 +1901,7 @@ async def split_window_until_valid(
             except Exception as exc:
                 last_error = f"{type(exc).__name__}: {exc}"
    
    -        print(
    +        log.debug(
                 f"[Planning] {label}: invalid split on attempt {attempt}; retrying. "
                 f"Reason: {last_error}"
             )
    @@ -1910,7 +1913,7 @@ async def split_window_until_valid(
    
         if CHUNK_FALLBACK_MECHANICAL:
             fallback_title = f"{label} 自動分割"
    -        print(
    +        log.debug(
                 f"[Planning] {label}: using mechanical fallback after "
                 f"{PARTITION_RETRY_ATTEMPTS} attempts. Last error: {last_error}"
             )
    @@ -2019,7 +2022,7 @@ async def plan_concept_files_streaming(
                 committed.extend(split[:-1])
                 pending = split[-1]
    
    -            print(
    +            log.debug(
                     "[Planning] Carrying pending concept forward: "
                     f"{pending.title} [{pending.source_start}-{pending.source_end}]"
                 )
    @@ -2418,7 +2421,7 @@ async def enrich_concept_plan(
         if not files:
             return EnrichmentResult(inferred_file_name="document.md", files=[])
    
    -    print(f"[Enrichment] Inferring global name...")
    +    log.debug("[Enrichment] Inferring global name...")
         try:
             global_name_raw = await structured_ainvoke(
                 llm,
    @@ -2428,7 +2431,7 @@ async def enrich_concept_plan(
             )
             global_name = GlobalName.model_validate(global_name_raw).inferred_file_name
         except Exception as e:
    -        print(f"[Enrichment] Failed to infer global name: {e}. Using fallback.")
    +        log.debug(f"[Enrichment] Failed to infer global name: {e}. Using fallback.")
             global_name = "ドキュメント.md"
    
         if not global_name.endswith(".md"):
    @@ -2438,7 +2441,7 @@ async def enrich_concept_plan(
         inferred_headers = []
    
         # First chunk
    -    print(f"[Enrichment] Inferring header for chunk 1/{len(files)}...")
    +    log.debug(f"[Enrichment] Inferring header for chunk 1/{len(files)}...")
         if stop_check and stop_check():
             raise JobCancelled("chunk enrichment cancelled")
         try:
    @@ -2450,7 +2453,7 @@ async def enrich_concept_plan(
             )
             first_header = ChunkHeader.model_validate(first_raw).header
         except Exception as e:
    -        print(f"[Enrichment] Failed to infer header for chunk 1: {e}. Using fallback.")
    +        log.debug(f"[Enrichment] Failed to infer header for chunk 1: {e}. Using fallback.")
             first_header = "一般"
         inferred_headers.append(first_header)
    
    @@ -2458,7 +2461,7 @@ async def enrich_concept_plan(
         for i in range(1, len(files)):
             if stop_check and stop_check():
                 raise JobCancelled("chunk enrichment cancelled")
    -        print(f"[Enrichment] Inferring header for chunk {i+1}/{len(files)}...")
    +        log.debug(f"[Enrichment] Inferring header for chunk {i+1}/{len(files)}...")
             prompt = build_subsequent_chunk_prompt(
                 original_filename=original_filename,
                 current=files[i],
    @@ -2469,7 +2472,7 @@ async def enrich_concept_plan(
                 raw = await structured_ainvoke(llm, ChunkHeader, prompt, max_output_tokens=100)
                 header = ChunkHeader.model_validate(raw).header
             except Exception as e:
    -            print(
    +            log.debug(
                     f"[Enrichment] Failed to infer header for chunk {i+1}: {e}. "
                     "Using fallback."
                 )
    diff --git a/main.py b/main.py
    index e11114b..fc8b864 100644
    --- a/main.py
    +++ b/main.py
    @@ -37,6 +37,8 @@ from graph.workspace.project import open_project
     PROJECT_ROOT = Path(__file__).resolve().parent
     config.PROJECT_ROOT = PROJECT_ROOT
    
    +log = logging.getLogger(__name__)
    +
    
     def _settings(args: argparse.Namespace) -> Settings:
         settings = Settings.from_env(getattr(args, "project", ""))
    @@ -75,7 +77,7 @@ def _progress(event: dict[str, Any]) -> None:
             event.pop("current", None)
             event.pop("total", None)
         details = json.dumps(event, ensure_ascii=False, default=str)
    -    print(f"[{stage}] {step}{progress} {details}".rstrip(), flush=True)
    +    log.debug(f"[{stage}] {step}{progress} {details}".rstrip())
    
    
     def _report(result: dict[str, Any]) -> int:
    @@ -219,7 +221,58 @@ def cmd_sync(args: argparse.Namespace) -> int:
     def cmd_pull(args: argparse.Namespace) -> int:
         from publisher.pipeline import pull_growi_once
    
    -    return _report(pull_growi_once(_settings(args)))
    +    settings = _settings(args)
    +    result = (
    +        pull_growi_once(settings, force_inventory=True)
    +        if getattr(args, "inventory", False)
    +        else pull_growi_once(settings)
    +    )
    +    if result.get("human_sync"):
    +        print(json.dumps({"human_sync": result["human_sync"]}, ensure_ascii=False, default=str))
    +    return _report(result)
    +
    +
    +def cmd_human(args: argparse.Namespace) -> int:
    +    from publisher.human_changes import HumanStore
    +
    +    settings = _settings(args)
    +    project = open_project(settings)
    +    store = HumanStore(project)
    +    if args.human_command == "status":
    +        print(json.dumps(store.project_summary(), ensure_ascii=False, default=str))
    +        return 0
    +    if args.human_command == "resolve":
    +        combined = ""
    +        if args.text_file:
    +            combined = Path(args.text_file).read_text(encoding="utf-8")
    +        result = store.resolve(
    +            args.edit_id,
    +            action=args.action,
    +            expected_revision=args.revision,
    +            combined_text=combined,
    +            document=args.document or "",
    +        )
    +        print(json.dumps(result, ensure_ascii=False))
    +        return 0
    +    if args.human_command == "recover-legacy":
    +        from publisher.legacy_recovery import recover_legacy_ancestor
    +
    +        result = recover_legacy_ancestor(store, args.document)
    +        print(json.dumps(result, ensure_ascii=False, default=str))
    +        return 0 if result.get("status") in {"recovered", "no_legacy_pin"} else 1
    +    if args.human_command == "live-plan":
    +        from publisher.live_verification import LiveVerificationReport
    +
    +        report = LiveVerificationReport.create(project, settings, args.path)
    +        print(json.dumps({
    +            "report": str(report.path),
    +            "resolved_boundary": report.data["disposable_path"],
    +            "endpoint": report.data["endpoint"],
    +            "confirmation_code": report.data["confirmation_code"],
    +            "status": report.data["status"],
    +        }, ensure_ascii=False))
    +        return 0
    +    raise ValueError(f"unknown human command: {args.human_command}")
    
    
     def cmd_watch(args: argparse.Namespace) -> int:
    @@ -414,7 +467,27 @@ def build_parser() -> argparse.ArgumentParser:
         build.add_argument("--force", action="store_true", help="regenerate even when the raw source is unchanged")
         build.set_defaults(fn=cmd_build)
         publish = sub.add_parser("publish", help="publish the current wiki tree only"); project_flags(publish); publish.set_defaults(fn=cmd_publish)
    -    pull = sub.add_parser("pull", help="capture GROWI edits into the local human overlay"); project_flags(pull); pull.set_defaults(fn=cmd_pull)
    +    pull = sub.add_parser("pull", help="capture GROWI edits into the local human overlay"); project_flags(pull)
    +    pull.add_argument("--inventory", action="store_true", help="force a complete read-only inventory below the configured GROWI root")
    +    pull.set_defaults(fn=cmd_pull)
    +    human = sub.add_parser("human", help="inspect and resolve durable human overlays"); project_flags(human)
    +    human_sub = human.add_subparsers(dest="human_command", required=True)
    +    human_status = human_sub.add_parser("status", help="write and print the project-wide human-sync summary")
    +    project_flags(human_status)
    +    human_resolve = human_sub.add_parser("resolve", help="apply one revision-checked operator decision")
    +    project_flags(human_resolve)
    +    human_resolve.add_argument("edit_id")
    +    human_resolve.add_argument("--action", required=True, choices=("keep-human", "accept-source", "combine", "suppress", "delete", "retry-match"))
    +    human_resolve.add_argument("--revision", required=True, help="last inspected GROWI revision")
    +    human_resolve.add_argument("--document", help="expected raw document identity")
    +    human_resolve.add_argument("--text-file", help="UTF-8 combined body for --action combine")
    +    human_recover = human_sub.add_parser("recover-legacy", help="recover a uniquely verified pure ancestor from project Git")
    +    project_flags(human_recover)
    +    human_recover.add_argument("document", help="raw-relative document path")
    +    human_live = human_sub.add_parser("live-plan", help="create a local-only redacted plan for a disposable live verification subtree")
    +    project_flags(human_live)
    +    human_live.add_argument("--path", required=True, help="confirmed disposable path below the configured project boundary")
    +    human.set_defaults(fn=cmd_human)
         index = sub.add_parser("index", help="publish per-document + root index pages for growi-search"); project_flags(index)
         index.add_argument("items", nargs="*", metavar="raw-rel", help="raw-relative paths; omit for every linked document")
         index.add_argument("--no-publish", action="store_true", help="only write metadata/index/, do not touch GROWI")
    @@ -438,8 +511,15 @@ def build_parser() -> argparse.ArgumentParser:
    
    
     def main() -> int:
    -    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
         args = build_parser().parse_args()
    +    logging.basicConfig(
    +        level="DEBUG" if getattr(args, "verbose", False) else os.environ.get("LOG_LEVEL", "INFO"),
    +        format="%(asctime)s %(levelname)s %(message)s",
    +    )
    +    if getattr(args, "verbose", False):
    +        # -v means "show our debug", not "show httpx's every socket read".
    +        for noisy in ("httpx", "httpcore", "h11", "urllib3", "openai", "langchain", "anthropic"):
    +            logging.getLogger(noisy).setLevel(logging.INFO)
         return int(args.fn(args) or 0)
    
    
    diff --git a/publisher/human_changes.py b/publisher/human_changes.py
    index 5877ecb..5510d66 100644
    --- a/publisher/human_changes.py
    +++ b/publisher/human_changes.py
    @@ -460,6 +460,288 @@ class HumanStore:
             self.validate(data)
             write_json_atomic(self.root / "pages" / f"{data['marker_id']}.json", data)
    
    +    def record_observation(
    +        self,
    +        *,
    +        mode: str,
    +        decision: str,
    +        local_path: str,
    +        page_id: str,
    +        revision_id: str,
    +        before: str,
    +        after: str,
    +        proposed_operation: str,
    +        proposed_status: str,
    +        match_reason: str,
    +        algorithm_version: str = "deterministic-observer-v1",
    +    ) -> dict:
    +        """Persist a text-redacted, idempotent rollout observation."""
    +
    +        self._initialize()
    +        identity = sha256_text(
    +            "\0".join((mode, local_path, page_id, revision_id, algorithm_version))
    +        )
    +        record = {
    +            "schema_version": VERSION,
    +            "observation_id": "hobs-" + identity[:24],
    +            "mode": mode,
    +            "decision": decision,
    +            "local_path": local_path,
    +            "page_id": page_id,
    +            "revision_id": revision_id,
    +            "before_sha256": sha256_text(before),
    +            "after_sha256": sha256_text(after),
    +            "proposed_operation": proposed_operation,
    +            "proposed_status": proposed_status,
    +            "match_reason": match_reason,
    +            "algorithm_version": algorithm_version,
    +            "observed_at": now(),
    +        }
    +        path = self.root / "observations" / f"{identity}.json"
    +        if path.exists():
    +            existing = read_json(path)
    +            comparable = dict(record)
    +            comparable["observed_at"] = existing.get("observed_at")
    +            if existing != comparable:
    +                raise ValueError("observation identity collision")
    +            return existing
    +        write_json_atomic(path, record)
    +        return record
    +
    +    def record_event(
    +        self,
    +        *,
    +        mode: str,
    +        decision: str,
    +        local_path: str,
    +        page_id: str = "",
    +        revision_id: str = "",
    +        reason: str = "",
    +    ) -> dict:
    +        """Persist a redacted safety/audit event without page contents."""
    +
    +        self._initialize()
    +        identity = sha256_text("\0".join((mode, decision, local_path, page_id, revision_id, reason)))
    +        path = self.root / "events" / f"{identity}.json"
    +        if path.exists():
    +            return read_json(path)
    +        record = {
    +            "schema_version": VERSION,
    +            "event_id": "hevt-" + identity[:24],
    +            "mode": mode,
    +            "decision": decision,
    +            "local_path": local_path,
    +            "page_id": page_id,
    +            "revision_id": revision_id,
    +            "reason": reason,
    +            "time": now(),
    +        }
    +        write_json_atomic(path, record)
    +        return record
    +
    +    def observations(self) -> list[dict]:
    +        result = []
    +        for path in sorted((self.root / "observations").glob("*.json")):
    +            row = read_json(path)
    +            if not isinstance(row, dict) or row.get("schema_version") != VERSION:
    +                raise ValueError(f"invalid human observation: {path.name}")
    +            result.append(row)
    +        return result
    +
    +    def events(self) -> list[dict]:
    +        result = []
    +        for path in sorted((self.root / "events").glob("*.json")):
    +            row = read_json(path)
    +            if not isinstance(row, dict) or row.get("schema_version") != VERSION:
    +                raise ValueError(f"invalid human event: {path.name}")
    +            result.append(row)
    +        return result
    +
    +    def project_summary(self, *, write: bool = True) -> dict:
    +        """Return the project-wide operator index without copying protected text."""
    +
    +        self._initialize()
    +        rows: list[dict[str, Any]] = []
    +        counts: dict[str, int] = {}
    +        for path in sorted((self.root / "documents").glob("*.json")):
    +            document = read_json(path)
    +            if document.get("alias_of"):
    +                continue
    +            if document.get("schema_version") != VERSION:
    +                raise ValueError(f"invalid human document record: {path.name}")
    +            self.validate(document)
    +            raw_rel = str(document.get("raw_rel") or "")
    +            prefix = self.project.wiki_dir(raw_rel).relative_to(self.project.wiki).as_posix() if raw_rel else ""
    +            dashboard = str(document.get("dashboard_filename") or DASHBOARD)
    +            retained = str(document.get("retained_filename") or RETAINED)
    +            for edit in document.get("edits", []):
    +                status = str(edit.get("status") or "blocked")
    +                counts[status] = counts.get(status, 0) + 1
    +                target = edit.get("current_target") or {}
    +                anchor = edit.get("anchor") or {}
    +                page = str(target.get("path") or anchor.get("old_local_path") or "")
    +                rows.append({
    +                    "edit_id": str(edit.get("edit_id") or ""),
    +                    "project": self.project.root.name,
    +                    "document": raw_rel,
    +                    "page": page,
    +                    "status": status,
    +                    "source_id": str(edit.get("source_id") or document.get("source_id") or ""),
    +                    "first_source_sha256": str(edit.get("first_source_sha256") or document.get("source_sha256") or ""),
    +                    "first_seen_at": str(edit.get("created_at") or ""),
    +                    "last_remote_revision": str(edit.get("last_seen_revision") or ""),
    +                    "last_remote_at": str(edit.get("last_seen_at") or edit.get("updated_at") or ""),
    +                    "last_applied_source_sha256": str(edit.get("last_applied_source_sha256") or ""),
    +                    "last_applied_at": str(edit.get("last_applied_at") or ""),
    +                    "reason": str(edit.get("match_reason") or edit.get("fallback_reason") or ""),
    +                    "action": "none" if status == "deleted" else "keep-human|accept-source|combine|suppress|retry-match",
    +                    "dashboard": f"{prefix}/{dashboard}" if prefix else dashboard,
    +                    "retained": f"{prefix}/{retained}" if prefix else retained,
    +                })
    +        for path in sorted((self.root / "pages").glob("*.json")):
    +            page = read_json(path)
    +            if not page.get("blocked"):
    +                continue
    +            local_path = str(page.get("local_path") or "")
    +            planning = self.project.wiki / Path(local_path).parent / "_planning" / "source.json"
    +            source = read_json(planning, default={})
    +            raw_rel = str(source.get("raw") or "")
    +            counts["blocked"] = counts.get("blocked", 0) + 1
    +            rows.append({
    +                "edit_id": "page-" + str(page.get("marker_id") or path.stem),
    +                "project": self.project.root.name,
    +                "document": raw_rel,
    +                "page": local_path,
    +                "status": "blocked",
    +                "source_id": str(page.get("source_id") or ""),
    +                "first_source_sha256": str(page.get("source_sha256") or ""),
    +                "first_seen_at": str(page.get("updated_at") or ""),
    +                "last_remote_revision": str(page.get("observed_revision") or ""),
    +                "last_remote_at": str(page.get("updated_at") or ""),
    +                "last_applied_source_sha256": "",
    +                "last_applied_at": "",
    +                "reason": str(page.get("blocked") or ""),
    +                "action": "repair-remote-and-retry",
    +                "dashboard": str(Path(local_path).parent / DASHBOARD),
    +                "retained": str(Path(local_path).parent / RETAINED),
    +            })
    +        proposal_count = len(self.observations())
    +        counts["observe_only_proposal"] = proposal_count
    +        rows.sort(key=lambda row: (row["project"], row["document"], row["page"], row["edit_id"]))
    +        prior_summary = read_json(self.root / "operator-summary.json", default={})
    +        stable_payload = {"counts": dict(sorted(counts.items())), "rows": rows}
    +        prior_stable = {"counts": prior_summary.get("counts", {}), "rows": prior_summary.get("rows", [])}
    +        result = {
    +            "schema_version": VERSION,
    +            "generated_at": (
    +                prior_summary.get("generated_at")
    +                if prior_summary.get("schema_version") == VERSION and prior_stable == stable_payload
    +                else now()
    +            ),
    +            "counts": stable_payload["counts"],
    +            "unresolved": sum(value for key, value in counts.items()
    +                              if key in {"active", "conflict", "orphaned", "legacy_pinned", "blocked"}),
    +            "blocked": sum(value for key, value in counts.items() if key in {"legacy_pinned", "blocked"}),
    +            "rows": rows,
    +        }
    +        if write:
    +            write_json_atomic(self.root / "operator-summary.json", result)
    +            lines = ["# Human sync operator summary", "", "## Counts", ""]
    +            lines.extend(f"- {key}: {value}" for key, value in result["counts"].items())
    +            lines.extend(["", "## Records", ""])
    +            for row in rows:
    +                dashboard_link = "../../wiki/" + row["dashboard"]
    +                lines.append(
    +                    f"- `{row['edit_id']}` [{row['document']}]({dashboard_link}) "
    +                    f"status={row['status']} page=`{row['page']}` revision=`{row['last_remote_revision']}` "
    +                    f"action={row['action']}"
    +                )
    +            write_text_atomic(self.root / "operator-summary.md", "\n".join(lines).rstrip() + "\n")
    +        return result
    +
    +    def resolve(
    +        self,
    +        edit_id: str,
    +        *,
    +        action: str,
    +        expected_revision: str,
    +        combined_text: str = "",
    +        document: str = "",
    +    ) -> dict:
    +        """Apply one revision-checked operator decision by stable edit ID."""
    +
    +        allowed = {"keep-human", "accept-source", "combine", "suppress", "delete", "retry-match"}
    +        if action not in allowed:
    +            raise ValueError(f"unknown human resolution action: {action}")
    +        matches: list[tuple[dict, dict]] = []
    +        for path in sorted((self.root / "documents").glob("*.json")):
    +            journal = read_json(path)
    +            if journal.get("alias_of") or (document and journal.get("raw_rel") != document):
    +                continue
    +            for edit in journal.get("edits", []):
    +                if edit.get("edit_id") == edit_id:
    +                    matches.append((journal, edit))
    +        if len(matches) != 1:
    +            raise ValueError("human edit ID is unknown or duplicated")
    +        journal, edit = matches[0]
    +        resolution_id = "hresolve-" + sha256_text(
    +            "\0".join((edit_id, action, expected_revision, sha256_text(combined_text)))
    +        )[:24]
    +        for resolution in edit.get("resolution_history", []):
    +            if resolution.get("resolution_id") == resolution_id:
    +                return resolution
    +        if not expected_revision or str(edit.get("last_seen_revision") or "") != expected_revision:
    +            raise ValueError("stale human resolution revision")
    +        page_marker = str((edit.get("anchor") or {}).get("page_marker_id") or "")
    +        baseline = self.page(page_marker) if page_marker and re.fullmatch(r"[A-Za-z0-9_-]+", page_marker) else {}
    +        if baseline and baseline.get("observed_revision") not in {"", expected_revision}:
    +            raise ValueError("page changed while resolving human edit")
    +        raw_rel = str(journal.get("raw_rel") or "")
    +        folder = self.project.wiki_dir(raw_rel)
    +        rendered = "\n".join(
    +            page.read_text(encoding="utf-8") for page in sorted(folder.glob("*.md"))
    +        )
    +        if rendered:
    +            found = sum(1 for match in marker_matches(rendered) if match.group(1) == edit_id)
    +            if edit.get("status") in {"active", "conflict"} and found != 1:
    +                raise ValueError("human edit marker is missing or duplicated")
    +        previous = str(edit.get("status") or "")
    +        if action == "keep-human":
    +            if edit.get("conflict", {}).get("source_blob"):
    +                edit["keep_human_source_blob"] = edit["conflict"]["source_blob"]
    +            edit["status"] = "active"
    +        elif action == "accept-source":
    +            edit.update({"status": "deleted", "deleted_from_revision": expected_revision})
    +        elif action == "combine":
    +            if not combined_text:
    +                raise ValueError("combine requires non-empty combined_text")
    +            edit["human_after_blob"] = self.put(combined_text)
    +            base = self.get(edit["base_before_blob"])
    +            edit["human_delta"] = [
    +                {"start": start, "end": end, "replacement_blob": self.put(replacement)}
    +                for start, end, replacement in _changes(base, combined_text)
    +            ]
    +            edit["status"] = "active"
    +        elif action in {"suppress", "delete"}:
    +            edit.update({"status": "deleted", "deleted_from_revision": expected_revision})
    +        else:
    +            edit["status"] = "orphaned"
    +            edit["current_target"] = {}
    +        resolution = {
    +            "resolution_id": resolution_id,
    +            "action": action,
    +            "expected_revision": expected_revision,
    +            "previous_status": previous,
    +            "result_status": edit["status"],
    +            "time": now(),
    +        }
    +        edit.setdefault("resolution_history", []).append(resolution)
    +        edit["updated_at"] = resolution["time"]
    +        self.save(journal)
    +        self.render(raw_rel)
    +        self.project_summary()
    +        return resolution
    +
         def generated(self, raw_rel: str, pages: dict[str, str]) -> None:
             """Called with freshly exported source pages before any overlay/linking."""
             document = self.document(raw_rel)
    @@ -503,13 +785,41 @@ class HumanStore:
                 return {}
             if str(data.get("prepared_revision") or "") == page.revision_id:
                 return {}
    -        # A recorded conflict belongs to this prepared attempt: the attempt started
    -        # without one and a recorded publication clears it. A rejected PUT never landed,
    -        # so an advanced revision is someone else's edit and the old baseline rules.
    -        if str(data.get("publication_error") or ""):
    +        attempt_id = str(data.get("prepared_attempt_id") or "")
    +        # Only an error for this attempt vetoes it.  Stale metadata from an
    +        # earlier attempt cannot approve or reject a later write.
    +        if (str(data.get("publication_error") or "")
    +                and str(data.get("publication_error_attempt_id") or "") == attempt_id):
                 return {}
             return data
    
    +    def retire_unlanded(self, marker_id: str, page: Any) -> bool:
    +        """Retire a prepared write proven not to have changed the inspected page."""
    +
    +        data = self.page(marker_id) if marker_id else {}
    +        if not data or str(data.get("prepared_revision") or "") != page.revision_id:
    +            return False
    +        confirmation = data.get("publication_confirmation") or {}
    +        if confirmation.get("attempt_id") == data.get("prepared_attempt_id"):
    +            return False
    +        self.settle_prepared(data, status="not_landed")
    +        return True
    +
    +    def settle_prepared(self, data: dict, *, status: str) -> None:
    +        attempt_id = str(data.get("prepared_attempt_id") or "")
    +        for attempt in data.get("attempt_history", []):
    +            if attempt.get("attempt_id") == attempt_id:
    +                attempt["status"] = status
    +                attempt["settled_at"] = now()
    +        for key in (
    +            "prepared_attempt_id", "prepared_path", "prepared_page_id",
    +            "prepared_revision", "prepared_remote_blob", "prepared_local_blob",
    +            "prepared_generated_blob", "publication_confirmation",
    +            "publication_error", "publication_error_attempt_id",
    +        ):
    +            data.pop(key, None)
    +        self.save_page(data)
    +
         def prepared_match(self, marker_id: str, page: Any) -> dict:
             """Return the pending prepared record whose exact body the page already holds."""
             data = self.prepared_pending(marker_id, page)
    @@ -534,11 +844,11 @@ class HumanStore:
             local = self.get(str(data["prepared_local_blob"])) if data.get("prepared_local_blob") else ""
             generated_blob = str(data.get("prepared_generated_blob") or "")
             generated = self.get(generated_blob) if generated_blob else ""
    -        for key in ("prepared_path", "prepared_page_id", "prepared_revision",
    -                    "prepared_remote_blob", "prepared_local_blob", "prepared_generated_blob"):
    -            data.pop(key, None)
    -        self.save_page(data)
             if body == page.body:
    +            for attempt in data.get("attempt_history", []):
    +                if attempt.get("attempt_id") == data.get("prepared_attempt_id"):
    +                    attempt.update({"status": "recovered_exact", "settled_at": now()})
    +            self.save_page(data)
                 row.update({"page_id": page.page_id, "growi_path": page.path, "revision_id": page.revision_id})
                 self.remember_page(local_path, row, remote or page.body, local, published=True)
                 if generated_blob:
    @@ -547,10 +857,23 @@ class HumanStore:
                     current = self.page(marker)
                     current["generated_blob"] = generated_blob
                     self.save_page(current)
    -            log.info("human_sync event=adopt_exact_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
    +            log.debug("human_sync event=adopt_exact_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
                 return {"exact": True}
    -        log.info("human_sync event=rebase_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
    -        return {"exact": False, "remote": body, "local": local, "generated": generated}
    +        confirmation = data.get("publication_confirmation") or {}
    +        # Records written by the accepted TODO-1 implementation predate
    +        # attempt IDs. They retain the conservative legacy rebase behavior;
    +        # all newly prepared writes require an exact confirmation chain.
    +        legacy_prepared = not data.get("prepared_attempt_id")
    +        if not legacy_prepared and not (
    +            confirmation.get("attempt_id") == data.get("prepared_attempt_id")
    +            and confirmation.get("page_id") == page.page_id
    +            and confirmation.get("path") == page.path
    +            and confirmation.get("remote_blob") == data.get("prepared_remote_blob")
    +        ):
    +            raise ValueError("ambiguous prepared publication outcome")
    +        log.debug("human_sync event=rebase_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
    +        return {"exact": False, "remote": body, "local": local, "generated": generated,
    +                "attempt_id": data.get("prepared_attempt_id")}
    
         def remember_page(self, local_path: str, row: dict, remote: str, local: str, *, published: bool) -> None:
             marker = row["marker_id"]
    @@ -563,9 +886,14 @@ class HumanStore:
                 for key in ("deleted_page_id", "deleted_revision", "deleted_path", "deleted_remote_blob", "deleted_at", "legacy_pinned"):
                     data.pop(key, None)
                 # A recorded publication settles any prepared write for this page.
    +            active_attempt = str(data.get("prepared_attempt_id") or "")
    +            for attempt in data.get("attempt_history", []):
    +                if attempt.get("attempt_id") == active_attempt and attempt.get("status") in {"prepared", "confirmed"}:
    +                    attempt.update({"status": "published", "settled_at": now()})
                 for key in ("prepared_path", "prepared_page_id", "prepared_revision",
                             "prepared_remote_blob", "prepared_local_blob", "prepared_generated_blob",
    -                        "publication_error"):
    +                        "prepared_attempt_id", "publication_confirmation",
    +                        "publication_error", "publication_error_attempt_id"):
                     data.pop(key, None)
                 data["published_revision"] = row["revision_id"]
                 data["published_remote_blob"] = data["remote_blob"]
    @@ -617,7 +945,9 @@ class HumanStore:
                       "human_delta": [{"start": s, "end": e, "replacement_blob": self.put(t)}
                                       for s, e, t in _changes(block.text, human)],
                       "created_from_revision": before_revision, "last_seen_revision": revision,
    -                  "created_at": now(), "updated_at": now(), "current_target": {}, "conflict": {}}
    +                  "first_source_sha256": str(document.get("source_sha256") or ""),
    +                  "created_at": now(), "last_seen_at": now(), "updated_at": now(),
    +                  "current_target": {}, "conflict": {}, "match_reason": "captured_remote_delta"}
             document["edits"].append(record)
             return record
    
    @@ -687,6 +1017,9 @@ class HumanStore:
             previous_regions, new_regions = regions(before), regions(remote)
             old_records = {e["edit_id"]: e for e in document["edits"] if e["status"] != "deleted"}
             old_statuses = {edit_id: edit["status"] for edit_id, edit in old_records.items()}
    +        for record in old_records.values():
    +            record["last_seen_revision"] = row["observed_revision_id"]
    +            record["last_seen_at"] = now()
    
             def commit(count: int) -> None:
                 document["captured_revisions"].append(revision_key)
    @@ -695,7 +1028,7 @@ class HumanStore:
                     "before_blob": self.put(before), "after_blob": self.put(remote), "time": now(),
                 })
                 self.save(document)
    -            log.info("human_sync event=capture page=%s operations=%d revision=%s", local_path, count, row["observed_revision_id"])
    +            log.debug("human_sync event=capture page=%s operations=%d revision=%s", local_path, count, row["observed_revision_id"])
             # An explicit removal of a stored human region is a tombstone. Edits to
             # its body become a new replacement below, with the original in history.
             for edit_id, body in previous_regions.items():
    @@ -850,6 +1183,10 @@ class HumanStore:
             for record in document["edits"]:
                 if record["status"] == "deleted":
                     continue
    +            previous_application = (
    +                record.get("status"), dict(record.get("current_target") or {}),
    +                record.get("last_applied_source_sha256", ""),
    +            )
                 base, human = self.get(record["base_before_blob"]), self.get(record["human_after_blob"])
                 target = self._match(record, candidates)
                 if record["status"] == "legacy_pinned":
    @@ -865,6 +1202,7 @@ class HumanStore:
                 if target is None:
                     record["status"] = "orphaned" if record["status"] != "legacy_pinned" else "legacy_pinned"
                     record["current_target"] = {}
    +                record["match_reason"] = "no_unambiguous_deterministic_target"
                     result.orphaned.append(record["edit_id"])
                     anchor = record["anchor"]
                     text = human or ("Human requested deletion of this source block:\n\n" + base)
    @@ -880,6 +1218,7 @@ class HumanStore:
                     # to tombstone it. Only capture creates a deleted record.
                     record["status"] = "absorbed" if status == "deleted" else status
                     record["current_target"] = {"path": target.path, "heading": target.heading, "ordinal": target.ordinal}
    +                record["match_reason"] = "deterministic_anchor_match"
                     record["conflict"] = {}
                     if status == "conflict":
                         if record.get("keep_human_source_blob") == sha256_text(target.text):
    @@ -897,10 +1236,22 @@ class HumanStore:
                         text = "\n" + text
                     replacements.setdefault(target.path, []).append((target.start, target.end, text))
                     destination = target.path
    -            record["last_applied_source_sha256"] = document.get("source_sha256", "")
    +            source_sha256 = document.get("source_sha256", "")
    +            current_application = (record.get("status"), dict(record.get("current_target") or {}), source_sha256)
    +            record["last_applied_source_sha256"] = source_sha256
    +            if current_application != previous_application:
    +                record["last_applied_at"] = now()
                 if record["status"] in {"conflict", "orphaned", "legacy_pinned"}:
    -                dashboard.append(f"- {record['status']}: [{record['anchor']['heading_path'][0] or 'Human note'}]"
    -                                 f"({Path(destination).name}) — `{record['edit_id']}`\n")
    +                dashboard.append(
    +                    f"- {record['status']}: [{record['anchor']['heading_path'][0] or 'Human note'}]"
    +                    f"({Path(destination).name}) — `{record['edit_id']}`; "
    +                    f"source=`{record.get('source_id', '')}`; "
    +                    f"first_seen=`{record.get('created_at', '')}`; "
    +                    f"last_revision=`{record.get('last_seen_revision', '')}`; "
    +                    f"last_applied=`{record.get('last_applied_source_sha256', '')}`; "
    +                    f"reason={record.get('match_reason', '')}; "
    +                    "actions=keep-human|accept-source|combine|suppress|retry-match\n"
    +                )
             effective = dict(pure)
             for path, edits in replacements.items():
                 for start, end, text in sorted(edits, reverse=True):
    @@ -939,7 +1290,7 @@ class HumanStore:
                 if state.get("status") != "disabled":
                     state["status"] = "pending"
                     write_json_atomic(marker, state)
    -        log.info("human_sync event=render source=%s changed=%d conflict=%d orphaned=%d",
    +        log.debug("human_sync event=render source=%s changed=%d conflict=%d orphaned=%d",
                      document["source_id"], len(result.changed_pages), len(result.conflicts), len(result.orphaned))
             return result
    
    diff --git a/publisher/pipeline.py b/publisher/pipeline.py
    index c84d2c3..4872bc9 100644
    --- a/publisher/pipeline.py
    +++ b/publisher/pipeline.py
    @@ -267,7 +267,13 @@ def _publisher(settings: Any) -> GrowiPublisher | None:
             str(getattr(settings, "growi_token", "") or os.environ.get("GROWI_TOKEN", "")),
             timeout=float(getattr(settings, "growi_timeout", 30)),
         )
    -    return GrowiPublisher(client, connection)
    +    from graph.config import HumanSyncPolicy
    +
    +    return GrowiPublisher(
    +        client,
    +        connection,
    +        human_sync_policy=HumanSyncPolicy.resolve(getattr(settings, "human_sync_mode", "off")),
    +    )
    
    
     @contextmanager
    @@ -327,7 +333,14 @@ def _pending_link_rels(project: Project, settings: Any, rels: list[str] | None =
         return [rel for rel in candidates if project.wiki_dir(rel).exists() and (force or not links_up_to_date(project, rel, mode=mode))]
    
    
    -def _capture_remote(project: Project, ledger: Ledger, publisher: Any, *, only: set[str] | None = None) -> tuple[list[str], list[str], set[str]]:
    +def _capture_remote(
    +    project: Project,
    +    ledger: Ledger,
    +    publisher: Any,
    +    *,
    +    only: set[str] | None = None,
    +    page_ids: set[str] | None = None,
    +) -> tuple[list[str], list[str], set[str]]:
         """Capture while local files still represent the previous accepted output."""
         if not hasattr(publisher, "pull_changes"):
             return [], [], set()
    @@ -341,7 +354,11 @@ def _capture_remote(project: Project, ledger: Ledger, publisher: Any, *, only: s
                 "growi_path": page.path, "page_id": page.page_id, "revision_id": page.revision_id,
                 "marker_id": publisher.page_marker_id(project, path), "marker_seed": publisher.page_marker_seed(project, path),
             } for path, page in discovered.items()})
    -    pages = {path: row for path, row in ledger.published_pages.items() if Path(path).parent.as_posix() in folders}
    +    pages = {
    +        path: row for path, row in ledger.published_pages.items()
    +        if Path(path).parent.as_posix() in folders
    +        and (page_ids is None or str(row.get("page_id") or "") in page_ids)
    +    }
         unchanged = {doc for doc, folder in folders.items()
                      if ledger.published_documents.get(doc, {}).get("content_sha256") == _content_hash(folder)}
         pulled, failures, blocked = publisher.pull_changes(project, pages, unchanged)
    @@ -351,6 +368,125 @@ def _capture_remote(project: Project, ledger: Ledger, publisher: Any, *, only: s
         return pulled, failures, blocked
    
    
    +def _capture_detected(
    +    project: Project,
    +    ledger: Ledger,
    +    publisher: Any,
    +    settings: Any,
    +    *,
    +    force_inventory: bool = False,
    +) -> tuple[list[str], list[str], set[str], dict[str, Any]]:
    +    """Use activities as hints and the normal pull path as authority."""
    +
    +    from .activity import ActivityDetector
    +    from .human_changes import HumanStore
    +    from graph.growi.client import _page_stamps
    +
    +    if not hasattr(publisher.client, "list_activities") or not hasattr(publisher.client, "list_all_pages"):
    +        pulled, failures, blocked = _capture_remote(project, ledger, publisher)
    +        return pulled, failures, blocked, {
    +            "fallback_reason": "activity_api_not_supported_by_client",
    +            "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
    +        }
    +
    +    detector = ActivityDetector(
    +        project,
    +        endpoint=_growi_url(settings),
    +        boundary=str(publisher.connection.root_path),
    +        overlap_seconds=int(getattr(settings, "human_sync_activity_overlap_seconds", 60)),
    +    )
    +    batch = detector.poll(publisher.client, ledger.published_pages)
    +    inventory = None
    +    try:
    +        cursor = detector.cursor()
    +    except (OSError, ValueError, TypeError):
    +        cursor = {}
    +    last_inventory = str(cursor.get("last_inventory_at") or "")
    +    audit_seconds = int(getattr(settings, "human_sync_activity_audit_seconds", 3600))
    +    audit_due = not last_inventory
    +    if last_inventory and audit_seconds > 0:
    +        try:
    +            from datetime import datetime, timezone
    +
    +            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_inventory.replace("Z", "+00:00"))).total_seconds()
    +            audit_due = age >= audit_seconds
    +        except ValueError:
    +            audit_due = True
    +    discovery_failures: list[str] = []
    +    if force_inventory or batch.fallback_reason or batch.unknown_page_ids or audit_due:
    +        inventory = detector.inventory(publisher.client, ledger.published_pages)
    +        batch.selected_page_ids.update(inventory.selected_page_ids)
    +        store = HumanStore(project)
    +        for page in inventory.discovered_pages:
    +            stamps = _page_stamps(page.body)
    +            marker = stamps[0].group("id") if len(stamps) == 1 else ""
    +            baseline = store.page(marker) if marker else {}
    +            local_path = str(baseline.get("local_path") or "")
    +            if marker and local_path and store.prepared_match(marker, page):
    +                ledger.published_pages[local_path] = {
    +                    "growi_path": page.path,
    +                    "page_id": page.page_id,
    +                    "revision_id": str(baseline.get("prepared_revision") or ""),
    +                    "marker_id": marker,
    +                    "marker_seed": local_path,
    +                }
    +                batch.selected_page_ids.add(page.page_id)
    +            elif marker:
    +                reason = "unledgered owned page has no exact prepared publication evidence"
    +                if local_path:
    +                    row = {"marker_id": marker, "page_id": page.page_id,
    +                           "growi_path": page.path, "revision_id": ""}
    +                    store.block_page(local_path, row, reason, page)
    +                batch.metrics.setdefault("inventory_blocks", 0)
    +                batch.metrics["inventory_blocks"] += 1
    +                discovery_failures.append(f"inventory: {page.path}: {reason}")
    +        batch.metrics.update({f"inventory_{key}": value for key, value in inventory.metrics.items()})
    +    pulled, failures, blocked = _capture_remote(
    +        project, ledger, publisher, page_ids=batch.selected_page_ids
    +    ) if batch.selected_page_ids else ([], [], set())
    +    failures.extend(discovery_failures)
    +    if inventory is not None:
    +        ambiguous = [row for row in inventory.classifications
    +                     if row["classification"] in {"ambiguous", "duplicate_ownership"}]
    +        failures.extend(
    +            f"inventory: {row['classification']}: {row.get('path') or row.get('marker_id') or ''}"
    +            for row in ambiguous
    +        )
    +    reported_fallback = batch.fallback_reason
    +    if not failures:
    +        resettable = {
    +            "endpoint_changed", "malformed_cursor", "sequence_gap", "cursor_too_old",
    +            "activity_pagination_gap", "clock_skew",
    +        }
    +        if inventory is not None and batch.fallback_reason in resettable:
    +            from datetime import datetime, timezone
    +
    +            batch.cursor = {
    +                "schema_version": 1,
    +                "endpoint_identity": detector.endpoint_identity,
    +                "last_processed_at": datetime.now(timezone.utc).isoformat(),
    +                "ids_at_last_timestamp": [],
    +                "recent_ids": [],
    +                "last_sequence": None,
    +            }
    +            batch.fallback_reason = ""
    +        if not batch.cursor:
    +            batch.cursor = detector.cursor()
    +            batch.cursor["endpoint_identity"] = detector.endpoint_identity
    +        if inventory is not None:
    +            from datetime import datetime, timezone
    +
    +            batch.cursor["last_inventory_at"] = datetime.now(timezone.utc).isoformat()
    +        detector.commit(batch)
    +    metrics = {
    +        **batch.metrics,
    +        "fallback_reason": reported_fallback,
    +        "classifications": inventory.classifications if inventory is not None else [],
    +        "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
    +    }
    +    return pulled, failures, blocked, metrics
    +
    +
     def _published_page_row(project: Project, publisher: Any, path: str, page: Any) -> dict[str, Any]:
         row = {"growi_path": page.path, "page_id": page.page_id, "revision_id": page.revision_id,
                "marker_id": publisher.page_marker_id(project, path),
    @@ -464,7 +600,7 @@ def _publish_sweep(
                         if not path.startswith(published_prefixes)
                     }
                 ledger.published_pages.update(updated_pages)
    -            log.info("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
    +            log.debug("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
                 if on_progress:
                     on_progress({
                         "stage": "growi-publish",
    @@ -660,7 +796,8 @@ def sync_once(
                     HumanStore(project).audit()
                     checkpoint_live(project, f"capture blocked GROWI revision {run_id}")
                 return {"run_id": run_id, "scan": scan, "done": [], "failures": capture_failures,
    -                    "cancelled": False, "index_paths": sorted(scoped_raw) if wanted is not None else None}
    +                    "cancelled": False, "index_paths": sorted(scoped_raw) if wanted is not None else None,
    +                    "human_sync": dict(getattr(publisher, "human_sync_summary", {}))}
             touched_raw: set[str] = set()
             deleted = scan.deleted if wanted is None else sorted(set(scan.deleted) & wanted)
             removed, touched, remove_failures = _remove_sources(project, ledger, {
    @@ -690,7 +827,7 @@ def sync_once(
                 if (str(getattr(settings, "embed_backend", "server")) == "off"
                         and isinstance(exc, ValueError)
                         and str(exc) == "the linker requires WIKI_EMBED_BACKEND=server"):
    -                log.info("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    +                log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
                 else:
                     log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
                 embedder = None
    @@ -710,7 +847,7 @@ def sync_once(
                     requested_resume = False
                 started = time.monotonic()
                 try:
    -                log.info("run=%s path=%s stage=parse start", run_id, rel)
    +                log.debug("run=%s path=%s stage=parse start", run_id, rel)
                     if on_progress:
                         on_progress({
                             "stage": "parse",
    @@ -737,7 +874,7 @@ def sync_once(
                         ):
                             assert previous_markdown is not None
                             markdown = previous_markdown
    -                        log.info("run=%s path=%s stage=parse resumed", run_id, rel)
    +                        log.debug("run=%s path=%s stage=parse resumed", run_id, rel)
                             if on_progress:
                                 on_progress({"stage": "parse", "step": "resumed", "file": rel})
                         else:
    @@ -800,7 +937,7 @@ def sync_once(
                         "reason": getattr(result, "reason", ""),
                         "human_edits_overwritten": list(getattr(result, "human_edits_overwritten", [])),
                     })
    -                log.info("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
    +                log.debug("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
                 except asyncio.CancelledError:
                     cancelled = True
                     break
    @@ -888,6 +1025,7 @@ def sync_once(
             "failures": failures,
             "cancelled": cancelled,
             "index_paths": index_paths,
    +        "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
         }
    
    
    @@ -1256,7 +1394,7 @@ def restore_publication(
                     continue
                 if candidate_store.get(prepared["prepared_remote_blob"]) == page.body:
                     known_revisions.setdefault(page.page_id, set()).add(page.revision_id)
    -                log.info("human_sync event=recover_exact_prepared page=%s revision=%s", page.page_id, page.revision_id)
    +                log.debug("human_sync event=recover_exact_prepared page=%s revision=%s", page.page_id, page.revision_id)
                     return True
             return False
    
    @@ -1385,7 +1523,7 @@ def restore_publication(
         return checkpoint_live(live, "restore last-good publication")
    
    
    -def pull_growi_once(settings: Any) -> dict[str, Any]:
    +def pull_growi_once(settings: Any, *, force_inventory: bool = False) -> dict[str, Any]:
         """Pull user revisions into local wiki state without rebuilding or publishing."""
         project = open_project(settings)
         history_enabled = (project.root / ".git").is_dir()
    @@ -1402,18 +1540,29 @@ def pull_growi_once(settings: Any) -> dict[str, Any]:
         with _lock(project):
             ledger = load_ledger(ledger_path)
             try:
    -            pulled, failures, _blocked = _capture_remote(project, ledger, publisher)
    +            pulled, failures, _blocked, detector = _capture_detected(
    +                project, ledger, publisher, settings, force_inventory=force_inventory
    +            )
                 save_ledger(ledger_path, ledger)
    +            from .human_changes import HumanStore
    +
    +            operator = HumanStore(project).project_summary()
    +            detector["operator"] = {key: operator[key] for key in ("counts", "unresolved", "blocked")}
                 if history_enabled:
                     from .history import checkpoint_live
    -                from .human_changes import HumanStore
    
                     HumanStore(project).audit()
                     checkpoint_live(project, f"pull GROWI {run_id}")
             except Exception as exc:
                 pulled = []
                 failures = [f"pull: {type(exc).__name__}: {exc}"]
    -    return {"run_id": run_id, "done": [{"status": "pulled", "path": path} for path in pulled], "failures": failures}
    +            detector = {"fallback_reason": "pull_exception"}
    +    return {
    +        "run_id": run_id,
    +        "done": [{"status": "pulled", "path": path} for path in pulled],
    +        "failures": failures,
    +        "human_sync": {**detector, **dict(getattr(publisher, "human_sync_summary", {}))},
    +    }
    
    
     def build_wiki_only(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    @@ -1481,7 +1630,7 @@ def link_raw(settings: Any, *, only: list[str] | None = None, force: bool = Fals
                 if (str(getattr(settings, "embed_backend", "server")) == "off"
                         and isinstance(exc, ValueError)
                         and str(exc) == "the linker requires WIKI_EMBED_BACKEND=server"):
    -                log.info("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    +                log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
                 else:
                     log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
                 embedder = None
    @@ -1525,7 +1674,8 @@ def publish_only(settings: Any) -> dict[str, Any]:
                 from .history import checkpoint_live
    
                 checkpoint_live(project, f"publish {run_id}" if not failures else f"publish {run_id} (incomplete)")
    -    return {"run_id": run_id, "done": [], "failures": failures}
    +    return {"run_id": run_id, "done": [], "failures": failures,
    +            "human_sync": dict(getattr(publisher, "human_sync_summary", {}))}
    
    
     def republish_if_stale(settings: Any) -> dict[str, Any] | None:
    ✓ • 0ms

## Activity

    $ rg -n --glob '*.py' '(?<![A-Za-z_])(log|logger)\.(debug|info|warning|error|exception|critical)\(|(?<![A-Za-z_])print\(' .
    rg: regex parse error:
        (?:(?<![A-Za-z_])(log|logger)\.(debug|info|warning|error|exception|critical)\(|(?<![A-Za-z_])print\()
           ^^^^
    error: look-around, including look-ahead and look-behind, is not supported
    
    Consider enabling PCRE2 with the --pcre2 flag, which can handle backreferences
    and look-around.
    ✗ (2) • 0ms

## Activity

    $ rg -n --glob '*.py' '(log|logger)\.(debug|info|warning|error|exception|critical)\(|print\(' .
    ./main.py:80:    log.debug(f"[{stage}] {step}{progress} {details}".rstrip())
    ./main.py:85:        print(json.dumps(row, ensure_ascii=False))
    ./main.py:155:            print(f"{name:7} skipped (not configured)")
    ./main.py:159:            print(f"{name:7} {response.status_code} {url}")
    ./main.py:162:            print(f"{name:7} DOWN {url} ({type(exc).__name__})")
    ./main.py:165:    print(jev_line)
    ./main.py:168:    print(f"data    {project.root.resolve()}  mount={project.mount} ingest={settings.ingest_mode} linker={'off' if not settings.wiki_linker_enabled else settings.wiki_linker_mode}")
    ./main.py:181:        print(f"growi endpoint or markers changed: full republish {stale['run_id']}", flush=True)
    ./main.py:231:        print(json.dumps({"human_sync": result["human_sync"]}, ensure_ascii=False, default=str))
    ./main.py:242:        print(json.dumps(store.project_summary(), ensure_ascii=False, default=str))
    ./main.py:255:        print(json.dumps(result, ensure_ascii=False))
    ./main.py:261:        print(json.dumps(result, ensure_ascii=False, default=str))
    ./main.py:267:        print(json.dumps({
    ./main.py:303:        print(json.dumps(scan(settings, only=args.items or None, settle_seconds=args.settle, force=args.force), ensure_ascii=False))
    ./main.py:307:            print(json.dumps(row, ensure_ascii=False))
    ./main.py:310:        print(json.dumps({"retried": retry_failed(project)}))
    ./main.py:322:                print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
    ./main.py:340:    print(json.dumps(result, ensure_ascii=False))
    ./main.py:390:            print({"mode": None, "documents": 0, "chunks": 0, "edges": 0}); return 0
    ./main.py:394:        print({"mode": mode[0] if mode else None, **counts})
    ./main.py:408:        print(json.dumps({"touched": result.touched_documents}, ensure_ascii=False))
    ./main.py:410:            print("run `python main.py publish` to push the touched documents to GROWI", file=sys.stderr)
    ./jev/parity.py:47:    print(json.dumps(summary, indent=2)); engine.close()
    ./jev/engine.py:221:        except Exception: log.exception("closing Jev backend")
    ./jev/engine.py:268:            log.info("Jev stats: %s", stats_summary(self._stats, time.monotonic() - self._started))
    ./jev/benchmark.py:47:    print("| " + " | ".join(headers) + " |\n|" + "|".join("---" for _ in headers) + "|")
    ./jev/benchmark.py:48:    for row in rows: print("| " + " | ".join(str(row[k]) for k in headers) + " |")
    ./jev/backends/torch.py:158:            log.warning("WIKI_JEV_SHARE_STATE is unavailable: cache continuation exceeded the parity tolerance")
    ./jev/backends/torch.py:170:        log.info("Jev fast kernels available: %s", backend.stats["fast_kernels"])
    ./jev/backends/torch.py:179:            except Exception as exc: log.warning("torch.compile unavailable: %s", exc)
    ./growi-search/sync.py:183:                log.warning("index sync pass failed (retrying in %ss): %s", self.settings.sync_seconds, self.last_error)
    ./growi-search/sync.py:214:            log.info("index ready: %s", self.store.counts())
    ./growi-search/sync.py:232:                log.warning("%s has no search data block: republish it with the new builder (`main.py index`)", ref)
    ./growi-search/sync.py:288:            log.warning("page %s unreadable, kept its old index entries: %s", page_id, exc)
    ./growi-search/sync.py:297:            log.info("document listing failed (%s), using builder revisions: %s", doc_path, exc)
    ./growi-search/sync.py:342:            log.info("indexed %s: %d/%d pages changed, %d texts embedded", ref, len(changed), len(records), embedded)
    ./growi-search/sync.py:353:                log.info("revision sweep skipped %s: %s", ref, exc)
    ./growi-search/sync.py:377:            log.info("removed document %s from the index", ref)
    ./zip.py:26:    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    ./growi-search/researcher.py:201:        log.info("jev request failed: %s", result)
    ./growi-search/researcher.py:512:            log.warning("query embedding failed, searching by keywords only: %s", exc)
    ./growi-search/researcher.py:796:            log.warning("rerank failed, keeping fusion order: %s", exc)
    ./growi-search/researcher.py:933:            log.warning("researcher %d failed: %s: %s", index, type(result).__name__, result)
    ./growi-search/researcher.py:1203:        log.info("question done in %.1fs: route=%s scored=%d hits=%d threads=%d researchers=%d llm_calls=%d stopped=%s",
    ./growi-search/researcher.py:1218:            log.info("trace write failed: %s", exc)
    ./growi-search/researcher.py:1233:            log.info("usage log failed: %s", exc)
    ./growi-search/gateway.py:98:            log.warning("embedder unavailable: %s", exc)
    ./growi-search/gateway.py:122:            log.warning("embeddings rejected (%s); retrying with %d parts per %d-char slice",
    ./growi-search/gateway.py:153:            log.warning("reranker unavailable: %s", exc)
    ./growi-search/gateway.py:214:        log.warning("jev engine construction failed: %s; jev disabled", exc)
    ./growi-search/walker.py:188:                    log.info("walker ES rescue failed: %s", exc)
    ./tests/test_mount_diff_pipeline.py:1281:            print(f"warning: failed to clean {self.settings.target_name}: {exc}")
    ./publisher/human_changes.py:860:            log.debug("human_sync event=adopt_exact_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
    ./publisher/human_changes.py:874:        log.debug("human_sync event=rebase_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
    ./publisher/human_changes.py:1031:            log.debug("human_sync event=capture page=%s operations=%d revision=%s", local_path, count, row["observed_revision_id"])
    ./publisher/human_changes.py:1293:        log.debug("human_sync event=render source=%s changed=%d conflict=%d orphaned=%d",
    ./growi-search/mirror.py:99:                log.warning("mirror sync deferred: %s", exc)
    ./growi-search/mirror.py:101:                log.exception("mirror sync failed")
    ./growi-search/mirror.py:135:                log.info("mirror metadata unreadable; rebuilding catalog")
    ./growi-search/mirror.py:143:                log.info("mirror catalog unreadable; rebuilding")
    ./growi-search/mirror.py:258:            log.info("ignoring page with invalid id or revision (%s)", page.path)
    ./growi-search/mirror.py:320:                        log.info("mirror warm failed (%s): %s", futures[future][0], exc)
    ./growi-search/mirror.py:322:                    log.info("mirror warmed %d/%d pages", min(offset + len(batch), len(todo)), len(todo))
    ./growi-search/mirror.py:339:                log.info("GROWI audit log unavailable; falling back to recent pages")
    ./growi-search/mirror.py:344:                log.error("GROWI audit log unavailable: %s", exc)
    ./publisher/index.py:193:            log.warning("Jev related document judge failed: %s", exc)
    ./publisher/index.py:631:        log.warning("index page for %s: %s: %s", document, type(exc).__name__, exc)
    ./growi-search/eval_run.py:181:                print(f"[{item['id']}] evaluation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    ./growi-search/eval_compare.py:209:                    print(f"judge failed for {item_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
    ./growi-search/eval_compare.py:216:        print(rendered)
    ./publisher/pipeline.py:603:            log.debug("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
    ./publisher/pipeline.py:615:            log.error("run=%s stage=publish error=%s: %s", run_id, type(exc).__name__, exc)
    ./publisher/pipeline.py:652:                log.warning("run=%s stage=index error=%s", run_id, problem)
    ./publisher/pipeline.py:654:            log.warning("run=%s stage=index error=%s: %s", run_id, type(exc).__name__, exc)
    ./publisher/pipeline.py:830:                log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    ./publisher/pipeline.py:832:                log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
    ./publisher/pipeline.py:850:                log.debug("run=%s path=%s stage=parse start", run_id, rel)
    ./publisher/pipeline.py:877:                        log.debug("run=%s path=%s stage=parse resumed", run_id, rel)
    ./publisher/pipeline.py:940:                log.debug("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
    ./publisher/pipeline.py:951:                    log.warning("run=%s path=%s stage=generate retry_later error=%s: %s", run_id, rel, type(exc).__name__, exc)
    ./publisher/pipeline.py:959:                log.error("run=%s path=%s stage=generate error=%s: %s", run_id, rel, type(exc).__name__, exc)
    ./publisher/pipeline.py:1272:                        log.warning("run=%s stage=index error=%s", run_id, problem)
    ./publisher/pipeline.py:1274:                    log.warning("run=%s stage=index error=%s: %s", run_id, type(exc).__name__, exc)
    ./publisher/pipeline.py:1397:                log.debug("human_sync event=recover_exact_prepared page=%s revision=%s", page.page_id, page.revision_id)
    ./publisher/pipeline.py:1633:                log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    ./publisher/pipeline.py:1635:                log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
    ./growi-search/cascade.py:128:        log.info("cascade rewrite failed: %s", exc)
    ./growi-search/cascade.py:368:            log.info("cascade subagent %s failed: %s", index, exc)
    ./growi-search/app.py:143:                print(f"[growi-search] Jev: {status}", flush=True)
    ./growi-search/app.py:150:                print(f"[growi-search] embedder: {embedder.identity if embedder else 'none (BM25 only)'}; "
    ./growi-search/app.py:157:                log.warning("GROWI health check failed at startup: %s", exc)
    ./growi-search/app.py:158:            log.info(
    ./growi-search/app.py:364:                log.exception("agent run failed")
    ./graph/linker/service.py:190:            log.warning("Jev page curation failed for %s: %s", page_rel, exc)
    ./graph/linker/service.py:253:            log.warning("Jev linker engine unavailable for page curation: %s", exc)
    ./graph/linker/service.py:380:            log.warning("Jev edge judge failed for %s: %s", target.chunk_id, exc)
    ./graph/linker/service.py:448:                log.warning("Jev linker engine unavailable; using LLM: %s", exc)
    ./graph/linker/service.py:565:                    log.warning("Jev alias resolution failed for %s: %s", document, exc)
    ./graph/linker/service.py:698:                            log.warning("Jev primary definer failed for %s: %s", canon, exc)
    ./graph/linker/__main__.py:91:        print("No LLM edge decisions available for calibration")
    ./graph/linker/__main__.py:104:        print(f"threshold={threshold:.1f} precision={precision:.3f} recall={recall:.3f} sample={len(pairs)}")
    ./graph/linker/__main__.py:106:    print(f"best_precision_at_least_0.9={best[0]:.1f} recall={best[1]:.3f}" if best else "no threshold reached 0.9 precision")
    ./graph/linker/__main__.py:129:            print({"mode": None, "documents": 0, "chunks": 0, "edges": 0})
    ./graph/linker/__main__.py:137:        print({"mode": mode[0] if mode else None, **counts})
    ./graph/linker/jev_judge.py:65:            log.warning("Jev role check failed for chunk %s: %s", item.chunk_id, exc)
    ./graph/linker/jev_judge.py:262:        log.warning("LLM tie-break failed for %s: %s", target.chunk_id, exc)
    ./graph/wiki/__main__.py:82:        print(
    ./graph/wiki/__main__.py:89:    print(json.dumps({"run": str(result), "wiki": str(result / "wiki")}, indent=2))
    ./graph/wiki/prompts.py:48:    def fingerprint(self) -> str:
    ./graph/wiki/legacy.py:510:                    log.debug(
    ./graph/wiki/legacy.py:1571:        log.debug(
    ./graph/wiki/legacy.py:1845:        log.debug(
    ./graph/wiki/legacy.py:1889:                log.debug(
    ./graph/wiki/legacy.py:1904:        log.debug(
    ./graph/wiki/legacy.py:1916:        log.debug(
    ./graph/wiki/legacy.py:2025:            log.debug(
    ./graph/wiki/legacy.py:2424:    log.debug("[Enrichment] Inferring global name...")
    ./graph/wiki/legacy.py:2434:        log.debug(f"[Enrichment] Failed to infer global name: {e}. Using fallback.")
    ./graph/wiki/legacy.py:2444:    log.debug(f"[Enrichment] Inferring header for chunk 1/{len(files)}...")
    ./graph/wiki/legacy.py:2456:        log.debug(f"[Enrichment] Failed to infer header for chunk 1: {e}. Using fallback.")
    ./graph/wiki/legacy.py:2464:        log.debug(f"[Enrichment] Inferring header for chunk {i+1}/{len(files)}...")
    ./graph/wiki/legacy.py:2475:            log.debug(
    ✓ • 2ms

## Assistant

The worktree contains a large, unrelated human-sync change set, so I’m isolating the logging edits and won’t rewrite or revert any of it. The prior logging edits are present; I’m now checking for remaining `INFO`/bare diagnostic output specifically on the sync hot path before deciding whether any surgical follow-up is actually needed.

## Activity

    $ rg -n --glob '*.py' '(log|logger)\.info\(|print\(' main.py publisher graph/wiki graph/workspace graph/linker graph/formats graph/growi graph/clients graph/common 2>/dev/null; git diff --unified=0 -- graph/wiki/legacy.py main.py publisher/pipeline.py publisher/human_changes.py
    graph/linker/__main__.py:91:        print("No LLM edge decisions available for calibration")
    graph/linker/__main__.py:104:        print(f"threshold={threshold:.1f} precision={precision:.3f} recall={recall:.3f} sample={len(pairs)}")
    graph/linker/__main__.py:106:    print(f"best_precision_at_least_0.9={best[0]:.1f} recall={best[1]:.3f}" if best else "no threshold reached 0.9 precision")
    graph/linker/__main__.py:129:            print({"mode": None, "documents": 0, "chunks": 0, "edges": 0})
    graph/linker/__main__.py:137:        print({"mode": mode[0] if mode else None, **counts})
    graph/wiki/prompts.py:48:    def fingerprint(self) -> str:
    graph/wiki/__main__.py:82:        print(
    graph/wiki/__main__.py:89:    print(json.dumps({"run": str(result), "wiki": str(result / "wiki")}, indent=2))
    main.py:85:        print(json.dumps(row, ensure_ascii=False))
    main.py:155:            print(f"{name:7} skipped (not configured)")
    main.py:159:            print(f"{name:7} {response.status_code} {url}")
    main.py:162:            print(f"{name:7} DOWN {url} ({type(exc).__name__})")
    main.py:165:    print(jev_line)
    main.py:168:    print(f"data    {project.root.resolve()}  mount={project.mount} ingest={settings.ingest_mode} linker={'off' if not settings.wiki_linker_enabled else settings.wiki_linker_mode}")
    main.py:181:        print(f"growi endpoint or markers changed: full republish {stale['run_id']}", flush=True)
    main.py:231:        print(json.dumps({"human_sync": result["human_sync"]}, ensure_ascii=False, default=str))
    main.py:242:        print(json.dumps(store.project_summary(), ensure_ascii=False, default=str))
    main.py:255:        print(json.dumps(result, ensure_ascii=False))
    main.py:261:        print(json.dumps(result, ensure_ascii=False, default=str))
    main.py:267:        print(json.dumps({
    main.py:303:        print(json.dumps(scan(settings, only=args.items or None, settle_seconds=args.settle, force=args.force), ensure_ascii=False))
    main.py:307:            print(json.dumps(row, ensure_ascii=False))
    main.py:310:        print(json.dumps({"retried": retry_failed(project)}))
    main.py:322:                print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
    main.py:340:    print(json.dumps(result, ensure_ascii=False))
    main.py:390:            print({"mode": None, "documents": 0, "chunks": 0, "edges": 0}); return 0
    main.py:394:        print({"mode": mode[0] if mode else None, **counts})
    main.py:408:        print(json.dumps({"touched": result.touched_documents}, ensure_ascii=False))
    main.py:410:            print("run `python main.py publish` to push the touched documents to GROWI", file=sys.stderr)
    diff --git a/graph/wiki/legacy.py b/graph/wiki/legacy.py
    index 71a600e..f41f373 100644
    --- a/graph/wiki/legacy.py
    +++ b/graph/wiki/legacy.py
    @@ -4,0 +5 @@ import json
    +import logging
    @@ -19,0 +21,2 @@ from graph.common.images import find_images
    +log = logging.getLogger(__name__)
    +
    @@ -507 +510 @@ def add_or_update_file_record(
    -                    print(
    +                    log.debug(
    @@ -1568 +1571 @@ def try_auto_repair_concept_partition_boundaries(
    -        print(
    +        log.debug(
    @@ -1842 +1845 @@ async def split_window_until_valid(
    -        print(
    +        log.debug(
    @@ -1886 +1889 @@ async def split_window_until_valid(
    -                print(
    +                log.debug(
    @@ -1901 +1904 @@ async def split_window_until_valid(
    -        print(
    +        log.debug(
    @@ -1913 +1916 @@ async def split_window_until_valid(
    -        print(
    +        log.debug(
    @@ -2022 +2025 @@ async def plan_concept_files_streaming(
    -            print(
    +            log.debug(
    @@ -2421 +2424 @@ async def enrich_concept_plan(
    -    print(f"[Enrichment] Inferring global name...")
    +    log.debug("[Enrichment] Inferring global name...")
    @@ -2431 +2434 @@ async def enrich_concept_plan(
    -        print(f"[Enrichment] Failed to infer global name: {e}. Using fallback.")
    +        log.debug(f"[Enrichment] Failed to infer global name: {e}. Using fallback.")
    @@ -2441 +2444 @@ async def enrich_concept_plan(
    -    print(f"[Enrichment] Inferring header for chunk 1/{len(files)}...")
    +    log.debug(f"[Enrichment] Inferring header for chunk 1/{len(files)}...")
    @@ -2453 +2456 @@ async def enrich_concept_plan(
    -        print(f"[Enrichment] Failed to infer header for chunk 1: {e}. Using fallback.")
    +        log.debug(f"[Enrichment] Failed to infer header for chunk 1: {e}. Using fallback.")
    @@ -2461 +2464 @@ async def enrich_concept_plan(
    -        print(f"[Enrichment] Inferring header for chunk {i+1}/{len(files)}...")
    +        log.debug(f"[Enrichment] Inferring header for chunk {i+1}/{len(files)}...")
    @@ -2472 +2475 @@ async def enrich_concept_plan(
    -            print(
    +            log.debug(
    diff --git a/main.py b/main.py
    index e11114b..fc8b864 100644
    --- a/main.py
    +++ b/main.py
    @@ -39,0 +40,2 @@ config.PROJECT_ROOT = PROJECT_ROOT
    +log = logging.getLogger(__name__)
    +
    @@ -78 +80 @@ def _progress(event: dict[str, Any]) -> None:
    -    print(f"[{stage}] {step}{progress} {details}".rstrip(), flush=True)
    +    log.debug(f"[{stage}] {step}{progress} {details}".rstrip())
    @@ -222 +224,52 @@ def cmd_pull(args: argparse.Namespace) -> int:
    -    return _report(pull_growi_once(_settings(args)))
    +    settings = _settings(args)
    +    result = (
    +        pull_growi_once(settings, force_inventory=True)
    +        if getattr(args, "inventory", False)
    +        else pull_growi_once(settings)
    +    )
    +    if result.get("human_sync"):
    +        print(json.dumps({"human_sync": result["human_sync"]}, ensure_ascii=False, default=str))
    +    return _report(result)
    +
    +
    +def cmd_human(args: argparse.Namespace) -> int:
    +    from publisher.human_changes import HumanStore
    +
    +    settings = _settings(args)
    +    project = open_project(settings)
    +    store = HumanStore(project)
    +    if args.human_command == "status":
    +        print(json.dumps(store.project_summary(), ensure_ascii=False, default=str))
    +        return 0
    +    if args.human_command == "resolve":
    +        combined = ""
    +        if args.text_file:
    +            combined = Path(args.text_file).read_text(encoding="utf-8")
    +        result = store.resolve(
    +            args.edit_id,
    +            action=args.action,
    +            expected_revision=args.revision,
    +            combined_text=combined,
    +            document=args.document or "",
    +        )
    +        print(json.dumps(result, ensure_ascii=False))
    +        return 0
    +    if args.human_command == "recover-legacy":
    +        from publisher.legacy_recovery import recover_legacy_ancestor
    +
    +        result = recover_legacy_ancestor(store, args.document)
    +        print(json.dumps(result, ensure_ascii=False, default=str))
    +        return 0 if result.get("status") in {"recovered", "no_legacy_pin"} else 1
    +    if args.human_command == "live-plan":
    +        from publisher.live_verification import LiveVerificationReport
    +
    +        report = LiveVerificationReport.create(project, settings, args.path)
    +        print(json.dumps({
    +            "report": str(report.path),
    +            "resolved_boundary": report.data["disposable_path"],
    +            "endpoint": report.data["endpoint"],
    +            "confirmation_code": report.data["confirmation_code"],
    +            "status": report.data["status"],
    +        }, ensure_ascii=False))
    +        return 0
    +    raise ValueError(f"unknown human command: {args.human_command}")
    @@ -417 +470,21 @@ def build_parser() -> argparse.ArgumentParser:
    -    pull = sub.add_parser("pull", help="capture GROWI edits into the local human overlay"); project_flags(pull); pull.set_defaults(fn=cmd_pull)
    +    pull = sub.add_parser("pull", help="capture GROWI edits into the local human overlay"); project_flags(pull)
    +    pull.add_argument("--inventory", action="store_true", help="force a complete read-only inventory below the configured GROWI root")
    +    pull.set_defaults(fn=cmd_pull)
    +    human = sub.add_parser("human", help="inspect and resolve durable human overlays"); project_flags(human)
    +    human_sub = human.add_subparsers(dest="human_command", required=True)
    +    human_status = human_sub.add_parser("status", help="write and print the project-wide human-sync summary")
    +    project_flags(human_status)
    +    human_resolve = human_sub.add_parser("resolve", help="apply one revision-checked operator decision")
    +    project_flags(human_resolve)
    +    human_resolve.add_argument("edit_id")
    +    human_resolve.add_argument("--action", required=True, choices=("keep-human", "accept-source", "combine", "suppress", "delete", "retry-match"))
    +    human_resolve.add_argument("--revision", required=True, help="last inspected GROWI revision")
    +    human_resolve.add_argument("--document", help="expected raw document identity")
    +    human_resolve.add_argument("--text-file", help="UTF-8 combined body for --action combine")
    +    human_recover = human_sub.add_parser("recover-legacy", help="recover a uniquely verified pure ancestor from project Git")
    +    project_flags(human_recover)
    +    human_recover.add_argument("document", help="raw-relative document path")
    +    human_live = human_sub.add_parser("live-plan", help="create a local-only redacted plan for a disposable live verification subtree")
    +    project_flags(human_live)
    +    human_live.add_argument("--path", required=True, help="confirmed disposable path below the configured project boundary")
    +    human.set_defaults(fn=cmd_human)
    @@ -441 +513,0 @@ def main() -> int:
    -    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    @@ -442,0 +515,8 @@ def main() -> int:
    +    logging.basicConfig(
    +        level="DEBUG" if getattr(args, "verbose", False) else os.environ.get("LOG_LEVEL", "INFO"),
    +        format="%(asctime)s %(levelname)s %(message)s",
    +    )
    +    if getattr(args, "verbose", False):
    +        # -v means "show our debug", not "show httpx's every socket read".
    +        for noisy in ("httpx", "httpcore", "h11", "urllib3", "openai", "langchain", "anthropic"):
    +            logging.getLogger(noisy).setLevel(logging.INFO)
    diff --git a/publisher/human_changes.py b/publisher/human_changes.py
    index 5877ecb..5510d66 100644
    --- a/publisher/human_changes.py
    +++ b/publisher/human_changes.py
    @@ -462,0 +463,282 @@ class HumanStore:
    +    def record_observation(
    +        self,
    +        *,
    +        mode: str,
    +        decision: str,
    +        local_path: str,
    +        page_id: str,
    +        revision_id: str,
    +        before: str,
    +        after: str,
    +        proposed_operation: str,
    +        proposed_status: str,
    +        match_reason: str,
    +        algorithm_version: str = "deterministic-observer-v1",
    +    ) -> dict:
    +        """Persist a text-redacted, idempotent rollout observation."""
    +
    +        self._initialize()
    +        identity = sha256_text(
    +            "\0".join((mode, local_path, page_id, revision_id, algorithm_version))
    +        )
    +        record = {
    +            "schema_version": VERSION,
    +            "observation_id": "hobs-" + identity[:24],
    +            "mode": mode,
    +            "decision": decision,
    +            "local_path": local_path,
    +            "page_id": page_id,
    +            "revision_id": revision_id,
    +            "before_sha256": sha256_text(before),
    +            "after_sha256": sha256_text(after),
    +            "proposed_operation": proposed_operation,
    +            "proposed_status": proposed_status,
    +            "match_reason": match_reason,
    +            "algorithm_version": algorithm_version,
    +            "observed_at": now(),
    +        }
    +        path = self.root / "observations" / f"{identity}.json"
    +        if path.exists():
    +            existing = read_json(path)
    +            comparable = dict(record)
    +            comparable["observed_at"] = existing.get("observed_at")
    +            if existing != comparable:
    +                raise ValueError("observation identity collision")
    +            return existing
    +        write_json_atomic(path, record)
    +        return record
    +
    +    def record_event(
    +        self,
    +        *,
    +        mode: str,
    +        decision: str,
    +        local_path: str,
    +        page_id: str = "",
    +        revision_id: str = "",
    +        reason: str = "",
    +    ) -> dict:
    +        """Persist a redacted safety/audit event without page contents."""
    +
    +        self._initialize()
    +        identity = sha256_text("\0".join((mode, decision, local_path, page_id, revision_id, reason)))
    +        path = self.root / "events" / f"{identity}.json"
    +        if path.exists():
    +            return read_json(path)
    +        record = {
    +            "schema_version": VERSION,
    +            "event_id": "hevt-" + identity[:24],
    +            "mode": mode,
    +            "decision": decision,
    +            "local_path": local_path,
    +            "page_id": page_id,
    +            "revision_id": revision_id,
    +            "reason": reason,
    +            "time": now(),
    +        }
    +        write_json_atomic(path, record)
    +        return record
    +
    +    def observations(self) -> list[dict]:
    +        result = []
    +        for path in sorted((self.root / "observations").glob("*.json")):
    +            row = read_json(path)
    +            if not isinstance(row, dict) or row.get("schema_version") != VERSION:
    +                raise ValueError(f"invalid human observation: {path.name}")
    +            result.append(row)
    +        return result
    +
    +    def events(self) -> list[dict]:
    +        result = []
    +        for path in sorted((self.root / "events").glob("*.json")):
    +            row = read_json(path)
    +            if not isinstance(row, dict) or row.get("schema_version") != VERSION:
    +                raise ValueError(f"invalid human event: {path.name}")
    +            result.append(row)
    +        return result
    +
    +    def project_summary(self, *, write: bool = True) -> dict:
    +        """Return the project-wide operator index without copying protected text."""
    +
    +        self._initialize()
    +        rows: list[dict[str, Any]] = []
    +        counts: dict[str, int] = {}
    +        for path in sorted((self.root / "documents").glob("*.json")):
    +            document = read_json(path)
    +            if document.get("alias_of"):
    +                continue
    +            if document.get("schema_version") != VERSION:
    +                raise ValueError(f"invalid human document record: {path.name}")
    +            self.validate(document)
    +            raw_rel = str(document.get("raw_rel") or "")
    +            prefix = self.project.wiki_dir(raw_rel).relative_to(self.project.wiki).as_posix() if raw_rel else ""
    +            dashboard = str(document.get("dashboard_filename") or DASHBOARD)
    +            retained = str(document.get("retained_filename") or RETAINED)
    +            for edit in document.get("edits", []):
    +                status = str(edit.get("status") or "blocked")
    +                counts[status] = counts.get(status, 0) + 1
    +                target = edit.get("current_target") or {}
    +                anchor = edit.get("anchor") or {}
    +                page = str(target.get("path") or anchor.get("old_local_path") or "")
    +                rows.append({
    +                    "edit_id": str(edit.get("edit_id") or ""),
    +                    "project": self.project.root.name,
    +                    "document": raw_rel,
    +                    "page": page,
    +                    "status": status,
    +                    "source_id": str(edit.get("source_id") or document.get("source_id") or ""),
    +                    "first_source_sha256": str(edit.get("first_source_sha256") or document.get("source_sha256") or ""),
    +                    "first_seen_at": str(edit.get("created_at") or ""),
    +                    "last_remote_revision": str(edit.get("last_seen_revision") or ""),
    +                    "last_remote_at": str(edit.get("last_seen_at") or edit.get("updated_at") or ""),
    +                    "last_applied_source_sha256": str(edit.get("last_applied_source_sha256") or ""),
    +                    "last_applied_at": str(edit.get("last_applied_at") or ""),
    +                    "reason": str(edit.get("match_reason") or edit.get("fallback_reason") or ""),
    +                    "action": "none" if status == "deleted" else "keep-human|accept-source|combine|suppress|retry-match",
    +                    "dashboard": f"{prefix}/{dashboard}" if prefix else dashboard,
    +                    "retained": f"{prefix}/{retained}" if prefix else retained,
    +                })
    +        for path in sorted((self.root / "pages").glob("*.json")):
    +            page = read_json(path)
    +            if not page.get("blocked"):
    +                continue
    +            local_path = str(page.get("local_path") or "")
    +            planning = self.project.wiki / Path(local_path).parent / "_planning" / "source.json"
    +            source = read_json(planning, default={})
    +            raw_rel = str(source.get("raw") or "")
    +            counts["blocked"] = counts.get("blocked", 0) + 1
    +            rows.append({
    +                "edit_id": "page-" + str(page.get("marker_id") or path.stem),
    +                "project": self.project.root.name,
    +                "document": raw_rel,
    +                "page": local_path,
    +                "status": "blocked",
    +                "source_id": str(page.get("source_id") or ""),
    +                "first_source_sha256": str(page.get("source_sha256") or ""),
    +                "first_seen_at": str(page.get("updated_at") or ""),
    +                "last_remote_revision": str(page.get("observed_revision") or ""),
    +                "last_remote_at": str(page.get("updated_at") or ""),
    +                "last_applied_source_sha256": "",
    +                "last_applied_at": "",
    +                "reason": str(page.get("blocked") or ""),
    +                "action": "repair-remote-and-retry",
    +                "dashboard": str(Path(local_path).parent / DASHBOARD),
    +                "retained": str(Path(local_path).parent / RETAINED),
    +            })
    +        proposal_count = len(self.observations())
    +        counts["observe_only_proposal"] = proposal_count
    +        rows.sort(key=lambda row: (row["project"], row["document"], row["page"], row["edit_id"]))
    +        prior_summary = read_json(self.root / "operator-summary.json", default={})
    +        stable_payload = {"counts": dict(sorted(counts.items())), "rows": rows}
    +        prior_stable = {"counts": prior_summary.get("counts", {}), "rows": prior_summary.get("rows", [])}
    +        result = {
    +            "schema_version": VERSION,
    +            "generated_at": (
    +                prior_summary.get("generated_at")
    +                if prior_summary.get("schema_version") == VERSION and prior_stable == stable_payload
    +                else now()
    +            ),
    +            "counts": stable_payload["counts"],
    +            "unresolved": sum(value for key, value in counts.items()
    +                              if key in {"active", "conflict", "orphaned", "legacy_pinned", "blocked"}),
    +            "blocked": sum(value for key, value in counts.items() if key in {"legacy_pinned", "blocked"}),
    +            "rows": rows,
    +        }
    +        if write:
    +            write_json_atomic(self.root / "operator-summary.json", result)
    +            lines = ["# Human sync operator summary", "", "## Counts", ""]
    +            lines.extend(f"- {key}: {value}" for key, value in result["counts"].items())
    +            lines.extend(["", "## Records", ""])
    +            for row in rows:
    +                dashboard_link = "../../wiki/" + row["dashboard"]
    +                lines.append(
    +                    f"- `{row['edit_id']}` [{row['document']}]({dashboard_link}) "
    +                    f"status={row['status']} page=`{row['page']}` revision=`{row['last_remote_revision']}` "
    +                    f"action={row['action']}"
    +                )
    +            write_text_atomic(self.root / "operator-summary.md", "\n".join(lines).rstrip() + "\n")
    +        return result
    +
    +    def resolve(
    +        self,
    +        edit_id: str,
    +        *,
    +        action: str,
    +        expected_revision: str,
    +        combined_text: str = "",
    +        document: str = "",
    +    ) -> dict:
    +        """Apply one revision-checked operator decision by stable edit ID."""
    +
    +        allowed = {"keep-human", "accept-source", "combine", "suppress", "delete", "retry-match"}
    +        if action not in allowed:
    +            raise ValueError(f"unknown human resolution action: {action}")
    +        matches: list[tuple[dict, dict]] = []
    +        for path in sorted((self.root / "documents").glob("*.json")):
    +            journal = read_json(path)
    +            if journal.get("alias_of") or (document and journal.get("raw_rel") != document):
    +                continue
    +            for edit in journal.get("edits", []):
    +                if edit.get("edit_id") == edit_id:
    +                    matches.append((journal, edit))
    +        if len(matches) != 1:
    +            raise ValueError("human edit ID is unknown or duplicated")
    +        journal, edit = matches[0]
    +        resolution_id = "hresolve-" + sha256_text(
    +            "\0".join((edit_id, action, expected_revision, sha256_text(combined_text)))
    +        )[:24]
    +        for resolution in edit.get("resolution_history", []):
    +            if resolution.get("resolution_id") == resolution_id:
    +                return resolution
    +        if not expected_revision or str(edit.get("last_seen_revision") or "") != expected_revision:
    +            raise ValueError("stale human resolution revision")
    +        page_marker = str((edit.get("anchor") or {}).get("page_marker_id") or "")
    +        baseline = self.page(page_marker) if page_marker and re.fullmatch(r"[A-Za-z0-9_-]+", page_marker) else {}
    +        if baseline and baseline.get("observed_revision") not in {"", expected_revision}:
    +            raise ValueError("page changed while resolving human edit")
    +        raw_rel = str(journal.get("raw_rel") or "")
    +        folder = self.project.wiki_dir(raw_rel)
    +        rendered = "\n".join(
    +            page.read_text(encoding="utf-8") for page in sorted(folder.glob("*.md"))
    +        )
    +        if rendered:
    +            found = sum(1 for match in marker_matches(rendered) if match.group(1) == edit_id)
    +            if edit.get("status") in {"active", "conflict"} and found != 1:
    +                raise ValueError("human edit marker is missing or duplicated")
    +        previous = str(edit.get("status") or "")
    +        if action == "keep-human":
    +            if edit.get("conflict", {}).get("source_blob"):
    +                edit["keep_human_source_blob"] = edit["conflict"]["source_blob"]
    +            edit["status"] = "active"
    +        elif action == "accept-source":
    +            edit.update({"status": "deleted", "deleted_from_revision": expected_revision})
    +        elif action == "combine":
    +            if not combined_text:
    +                raise ValueError("combine requires non-empty combined_text")
    +            edit["human_after_blob"] = self.put(combined_text)
    +            base = self.get(edit["base_before_blob"])
    +            edit["human_delta"] = [
    +                {"start": start, "end": end, "replacement_blob": self.put(replacement)}
    +                for start, end, replacement in _changes(base, combined_text)
    +            ]
    +            edit["status"] = "active"
    +        elif action in {"suppress", "delete"}:
    +            edit.update({"status": "deleted", "deleted_from_revision": expected_revision})
    +        else:
    +            edit["status"] = "orphaned"
    +            edit["current_target"] = {}
    +        resolution = {
    +            "resolution_id": resolution_id,
    +            "action": action,
    +            "expected_revision": expected_revision,
    +            "previous_status": previous,
    +            "result_status": edit["status"],
    +            "time": now(),
    +        }
    +        edit.setdefault("resolution_history", []).append(resolution)
    +        edit["updated_at"] = resolution["time"]
    +        self.save(journal)
    +        self.render(raw_rel)
    +        self.project_summary()
    +        return resolution
    +
    @@ -506,4 +788,5 @@ class HumanStore:
    -        # A recorded conflict belongs to this prepared attempt: the attempt started
    -        # without one and a recorded publication clears it. A rejected PUT never landed,
    -        # so an advanced revision is someone else's edit and the old baseline rules.
    -        if str(data.get("publication_error") or ""):
    +        attempt_id = str(data.get("prepared_attempt_id") or "")
    +        # Only an error for this attempt vetoes it.  Stale metadata from an
    +        # earlier attempt cannot approve or reject a later write.
    +        if (str(data.get("publication_error") or "")
    +                and str(data.get("publication_error_attempt_id") or "") == attempt_id):
    @@ -512,0 +796,27 @@ class HumanStore:
    +    def retire_unlanded(self, marker_id: str, page: Any) -> bool:
    +        """Retire a prepared write proven not to have changed the inspected page."""
    +
    +        data = self.page(marker_id) if marker_id else {}
    +        if not data or str(data.get("prepared_revision") or "") != page.revision_id:
    +            return False
    +        confirmation = data.get("publication_confirmation") or {}
    +        if confirmation.get("attempt_id") == data.get("prepared_attempt_id"):
    +            return False
    +        self.settle_prepared(data, status="not_landed")
    +        return True
    +
    +    def settle_prepared(self, data: dict, *, status: str) -> None:
    +        attempt_id = str(data.get("prepared_attempt_id") or "")
    +        for attempt in data.get("attempt_history", []):
    +            if attempt.get("attempt_id") == attempt_id:
    +                attempt["status"] = status
    +                attempt["settled_at"] = now()
    +        for key in (
    +            "prepared_attempt_id", "prepared_path", "prepared_page_id",
    +            "prepared_revision", "prepared_remote_blob", "prepared_local_blob",
    +            "prepared_generated_blob", "publication_confirmation",
    +            "publication_error", "publication_error_attempt_id",
    +        ):
    +            data.pop(key, None)
    +        self.save_page(data)
    +
    @@ -537,4 +846,0 @@ class HumanStore:
    -        for key in ("prepared_path", "prepared_page_id", "prepared_revision",
    -                    "prepared_remote_blob", "prepared_local_blob", "prepared_generated_blob"):
    -            data.pop(key, None)
    -        self.save_page(data)
    @@ -541,0 +848,4 @@ class HumanStore:
    +            for attempt in data.get("attempt_history", []):
    +                if attempt.get("attempt_id") == data.get("prepared_attempt_id"):
    +                    attempt.update({"status": "recovered_exact", "settled_at": now()})
    +            self.save_page(data)
    @@ -550 +860 @@ class HumanStore:
    -            log.info("human_sync event=adopt_exact_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
    +            log.debug("human_sync event=adopt_exact_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
    @@ -552,2 +862,15 @@ class HumanStore:
    -        log.info("human_sync event=rebase_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
    -        return {"exact": False, "remote": body, "local": local, "generated": generated}
    +        confirmation = data.get("publication_confirmation") or {}
    +        # Records written by the accepted TODO-1 implementation predate
    +        # attempt IDs. They retain the conservative legacy rebase behavior;
    +        # all newly prepared writes require an exact confirmation chain.
    +        legacy_prepared = not data.get("prepared_attempt_id")
    +        if not legacy_prepared and not (
    +            confirmation.get("attempt_id") == data.get("prepared_attempt_id")
    +            and confirmation.get("page_id") == page.page_id
    +            and confirmation.get("path") == page.path
    +            and confirmation.get("remote_blob") == data.get("prepared_remote_blob")
    +        ):
    +            raise ValueError("ambiguous prepared publication outcome")
    +        log.debug("human_sync event=rebase_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
    +        return {"exact": False, "remote": body, "local": local, "generated": generated,
    +                "attempt_id": data.get("prepared_attempt_id")}
    @@ -565,0 +889,4 @@ class HumanStore:
    +            active_attempt = str(data.get("prepared_attempt_id") or "")
    +            for attempt in data.get("attempt_history", []):
    +                if attempt.get("attempt_id") == active_attempt and attempt.get("status") in {"prepared", "confirmed"}:
    +                    attempt.update({"status": "published", "settled_at": now()})
    @@ -568 +895,2 @@ class HumanStore:
    -                        "publication_error"):
    +                        "prepared_attempt_id", "publication_confirmation",
    +                        "publication_error", "publication_error_attempt_id"):
    @@ -620 +948,3 @@ class HumanStore:
    -                  "created_at": now(), "updated_at": now(), "current_target": {}, "conflict": {}}
    +                  "first_source_sha256": str(document.get("source_sha256") or ""),
    +                  "created_at": now(), "last_seen_at": now(), "updated_at": now(),
    +                  "current_target": {}, "conflict": {}, "match_reason": "captured_remote_delta"}
    @@ -689,0 +1020,3 @@ class HumanStore:
    +        for record in old_records.values():
    +            record["last_seen_revision"] = row["observed_revision_id"]
    +            record["last_seen_at"] = now()
    @@ -698 +1031 @@ class HumanStore:
    -            log.info("human_sync event=capture page=%s operations=%d revision=%s", local_path, count, row["observed_revision_id"])
    +            log.debug("human_sync event=capture page=%s operations=%d revision=%s", local_path, count, row["observed_revision_id"])
    @@ -852,0 +1186,4 @@ class HumanStore:
    +            previous_application = (
    +                record.get("status"), dict(record.get("current_target") or {}),
    +                record.get("last_applied_source_sha256", ""),
    +            )
    @@ -867,0 +1205 @@ class HumanStore:
    +                record["match_reason"] = "no_unambiguous_deterministic_target"
    @@ -882,0 +1221 @@ class HumanStore:
    +                record["match_reason"] = "deterministic_anchor_match"
    @@ -900 +1239,5 @@ class HumanStore:
    -            record["last_applied_source_sha256"] = document.get("source_sha256", "")
    +            source_sha256 = document.get("source_sha256", "")
    +            current_application = (record.get("status"), dict(record.get("current_target") or {}), source_sha256)
    +            record["last_applied_source_sha256"] = source_sha256
    +            if current_application != previous_application:
    +                record["last_applied_at"] = now()
    @@ -902,2 +1245,10 @@ class HumanStore:
    -                dashboard.append(f"- {record['status']}: [{record['anchor']['heading_path'][0] or 'Human note'}]"
    -                                 f"({Path(destination).name}) — `{record['edit_id']}`\n")
    +                dashboard.append(
    +                    f"- {record['status']}: [{record['anchor']['heading_path'][0] or 'Human note'}]"
    +                    f"({Path(destination).name}) — `{record['edit_id']}`; "
    +                    f"source=`{record.get('source_id', '')}`; "
    +                    f"first_seen=`{record.get('created_at', '')}`; "
    +                    f"last_revision=`{record.get('last_seen_revision', '')}`; "
    +                    f"last_applied=`{record.get('last_applied_source_sha256', '')}`; "
    +                    f"reason={record.get('match_reason', '')}; "
    +                    "actions=keep-human|accept-source|combine|suppress|retry-match\n"
    +                )
    @@ -942 +1293 @@ class HumanStore:
    -        log.info("human_sync event=render source=%s changed=%d conflict=%d orphaned=%d",
    +        log.debug("human_sync event=render source=%s changed=%d conflict=%d orphaned=%d",
    diff --git a/publisher/pipeline.py b/publisher/pipeline.py
    index c84d2c3..4872bc9 100644
    --- a/publisher/pipeline.py
    +++ b/publisher/pipeline.py
    @@ -270 +270,7 @@ def _publisher(settings: Any) -> GrowiPublisher | None:
    -    return GrowiPublisher(client, connection)
    +    from graph.config import HumanSyncPolicy
    +
    +    return GrowiPublisher(
    +        client,
    +        connection,
    +        human_sync_policy=HumanSyncPolicy.resolve(getattr(settings, "human_sync_mode", "off")),
    +    )
    @@ -330 +336,8 @@ def _pending_link_rels(project: Project, settings: Any, rels: list[str] | None =
    -def _capture_remote(project: Project, ledger: Ledger, publisher: Any, *, only: set[str] | None = None) -> tuple[list[str], list[str], set[str]]:
    +def _capture_remote(
    +    project: Project,
    +    ledger: Ledger,
    +    publisher: Any,
    +    *,
    +    only: set[str] | None = None,
    +    page_ids: set[str] | None = None,
    +) -> tuple[list[str], list[str], set[str]]:
    @@ -344 +357,5 @@ def _capture_remote(project: Project, ledger: Ledger, publisher: Any, *, only: s
    -    pages = {path: row for path, row in ledger.published_pages.items() if Path(path).parent.as_posix() in folders}
    +    pages = {
    +        path: row for path, row in ledger.published_pages.items()
    +        if Path(path).parent.as_posix() in folders
    +        and (page_ids is None or str(row.get("page_id") or "") in page_ids)
    +    }
    @@ -353,0 +371,119 @@ def _capture_remote(project: Project, ledger: Ledger, publisher: Any, *, only: s
    +def _capture_detected(
    +    project: Project,
    +    ledger: Ledger,
    +    publisher: Any,
    +    settings: Any,
    +    *,
    +    force_inventory: bool = False,
    +) -> tuple[list[str], list[str], set[str], dict[str, Any]]:
    +    """Use activities as hints and the normal pull path as authority."""
    +
    +    from .activity import ActivityDetector
    +    from .human_changes import HumanStore
    +    from graph.growi.client import _page_stamps
    +
    +    if not hasattr(publisher.client, "list_activities") or not hasattr(publisher.client, "list_all_pages"):
    +        pulled, failures, blocked = _capture_remote(project, ledger, publisher)
    +        return pulled, failures, blocked, {
    +            "fallback_reason": "activity_api_not_supported_by_client",
    +            "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
    +        }
    +
    +    detector = ActivityDetector(
    +        project,
    +        endpoint=_growi_url(settings),
    +        boundary=str(publisher.connection.root_path),
    +        overlap_seconds=int(getattr(settings, "human_sync_activity_overlap_seconds", 60)),
    +    )
    +    batch = detector.poll(publisher.client, ledger.published_pages)
    +    inventory = None
    +    try:
    +        cursor = detector.cursor()
    +    except (OSError, ValueError, TypeError):
    +        cursor = {}
    +    last_inventory = str(cursor.get("last_inventory_at") or "")
    +    audit_seconds = int(getattr(settings, "human_sync_activity_audit_seconds", 3600))
    +    audit_due = not last_inventory
    +    if last_inventory and audit_seconds > 0:
    +        try:
    +            from datetime import datetime, timezone
    +
    +            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_inventory.replace("Z", "+00:00"))).total_seconds()
    +            audit_due = age >= audit_seconds
    +        except ValueError:
    +            audit_due = True
    +    discovery_failures: list[str] = []
    +    if force_inventory or batch.fallback_reason or batch.unknown_page_ids or audit_due:
    +        inventory = detector.inventory(publisher.client, ledger.published_pages)
    +        batch.selected_page_ids.update(inventory.selected_page_ids)
    +        store = HumanStore(project)
    +        for page in inventory.discovered_pages:
    +            stamps = _page_stamps(page.body)
    +            marker = stamps[0].group("id") if len(stamps) == 1 else ""
    +            baseline = store.page(marker) if marker else {}
    +            local_path = str(baseline.get("local_path") or "")
    +            if marker and local_path and store.prepared_match(marker, page):
    +                ledger.published_pages[local_path] = {
    +                    "growi_path": page.path,
    +                    "page_id": page.page_id,
    +                    "revision_id": str(baseline.get("prepared_revision") or ""),
    +                    "marker_id": marker,
    +                    "marker_seed": local_path,
    +                }
    +                batch.selected_page_ids.add(page.page_id)
    +            elif marker:
    +                reason = "unledgered owned page has no exact prepared publication evidence"
    +                if local_path:
    +                    row = {"marker_id": marker, "page_id": page.page_id,
    +                           "growi_path": page.path, "revision_id": ""}
    +                    store.block_page(local_path, row, reason, page)
    +                batch.metrics.setdefault("inventory_blocks", 0)
    +                batch.metrics["inventory_blocks"] += 1
    +                discovery_failures.append(f"inventory: {page.path}: {reason}")
    +        batch.metrics.update({f"inventory_{key}": value for key, value in inventory.metrics.items()})
    +    pulled, failures, blocked = _capture_remote(
    +        project, ledger, publisher, page_ids=batch.selected_page_ids
    +    ) if batch.selected_page_ids else ([], [], set())
    +    failures.extend(discovery_failures)
    +    if inventory is not None:
    +        ambiguous = [row for row in inventory.classifications
    +                     if row["classification"] in {"ambiguous", "duplicate_ownership"}]
    +        failures.extend(
    +            f"inventory: {row['classification']}: {row.get('path') or row.get('marker_id') or ''}"
    +            for row in ambiguous
    +        )
    +    reported_fallback = batch.fallback_reason
    +    if not failures:
    +        resettable = {
    +            "endpoint_changed", "malformed_cursor", "sequence_gap", "cursor_too_old",
    +            "activity_pagination_gap", "clock_skew",
    +        }
    +        if inventory is not None and batch.fallback_reason in resettable:
    +            from datetime import datetime, timezone
    +
    +            batch.cursor = {
    +                "schema_version": 1,
    +                "endpoint_identity": detector.endpoint_identity,
    +                "last_processed_at": datetime.now(timezone.utc).isoformat(),
    +                "ids_at_last_timestamp": [],
    +                "recent_ids": [],
    +                "last_sequence": None,
    +            }
    +            batch.fallback_reason = ""
    +        if not batch.cursor:
    +            batch.cursor = detector.cursor()
    +            batch.cursor["endpoint_identity"] = detector.endpoint_identity
    +        if inventory is not None:
    +            from datetime import datetime, timezone
    +
    +            batch.cursor["last_inventory_at"] = datetime.now(timezone.utc).isoformat()
    +        detector.commit(batch)
    +    metrics = {
    +        **batch.metrics,
    +        "fallback_reason": reported_fallback,
    +        "classifications": inventory.classifications if inventory is not None else [],
    +        "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
    +    }
    +    return pulled, failures, blocked, metrics
    +
    +
    @@ -467 +603 @@ def _publish_sweep(
    -            log.info("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
    +            log.debug("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
    @@ -663 +799,2 @@ def sync_once(
    -                    "cancelled": False, "index_paths": sorted(scoped_raw) if wanted is not None else None}
    +                    "cancelled": False, "index_paths": sorted(scoped_raw) if wanted is not None else None,
    +                    "human_sync": dict(getattr(publisher, "human_sync_summary", {}))}
    @@ -693 +830 @@ def sync_once(
    -                log.info("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    +                log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    @@ -713 +850 @@ def sync_once(
    -                log.info("run=%s path=%s stage=parse start", run_id, rel)
    +                log.debug("run=%s path=%s stage=parse start", run_id, rel)
    @@ -740 +877 @@ def sync_once(
    -                        log.info("run=%s path=%s stage=parse resumed", run_id, rel)
    +                        log.debug("run=%s path=%s stage=parse resumed", run_id, rel)
    @@ -803 +940 @@ def sync_once(
    -                log.info("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
    +                log.debug("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
    @@ -890,0 +1028 @@ def sync_once(
    +        "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
    @@ -1259 +1397 @@ def restore_publication(
    -                log.info("human_sync event=recover_exact_prepared page=%s revision=%s", page.page_id, page.revision_id)
    +                log.debug("human_sync event=recover_exact_prepared page=%s revision=%s", page.page_id, page.revision_id)
    @@ -1388 +1526 @@ def restore_publication(
    -def pull_growi_once(settings: Any) -> dict[str, Any]:
    +def pull_growi_once(settings: Any, *, force_inventory: bool = False) -> dict[str, Any]:
    @@ -1405 +1543,3 @@ def pull_growi_once(settings: Any) -> dict[str, Any]:
    -            pulled, failures, _blocked = _capture_remote(project, ledger, publisher)
    +            pulled, failures, _blocked, detector = _capture_detected(
    +                project, ledger, publisher, settings, force_inventory=force_inventory
    +            )
    @@ -1406,0 +1547,4 @@ def pull_growi_once(settings: Any) -> dict[str, Any]:
    +            from .human_changes import HumanStore
    +
    +            operator = HumanStore(project).project_summary()
    +            detector["operator"] = {key: operator[key] for key in ("counts", "unresolved", "blocked")}
    @@ -1409 +1552,0 @@ def pull_growi_once(settings: Any) -> dict[str, Any]:
    -                from .human_changes import HumanStore
    @@ -1416 +1559,7 @@ def pull_growi_once(settings: Any) -> dict[str, Any]:
    -    return {"run_id": run_id, "done": [{"status": "pulled", "path": path} for path in pulled], "failures": failures}
    +            detector = {"fallback_reason": "pull_exception"}
    +    return {
    +        "run_id": run_id,
    +        "done": [{"status": "pulled", "path": path} for path in pulled],
    +        "failures": failures,
    +        "human_sync": {**detector, **dict(getattr(publisher, "human_sync_summary", {}))},
    +    }
    @@ -1484 +1633 @@ def link_raw(settings: Any, *, only: list[str] | None = None, force: bool = Fals
    -                log.info("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    +                log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    @@ -1528 +1677,2 @@ def publish_only(settings: Any) -> dict[str, Any]:
    -    return {"run_id": run_id, "done": [], "failures": failures}
    +    return {"run_id": run_id, "done": [], "failures": failures,
    +            "human_sync": dict(getattr(publisher, "human_sync_summary", {}))}
    ✓ • 29ms

## Activity

    $ sed -n '1,230p' main.py && sed -n '560,675p' publisher/pipeline.py && sed -n '810,970p' publisher/pipeline.py && sed -n '1240,1290p' publisher/pipeline.py && sed -n '1600,1650p' publisher/pipeline.py && rg -n --glob '*.py' 'on_progress|stage=' publisher graph | head -200
    """One entry point for the publisher. `python main.py -h`.
    
    check                       ping chat/embed/parser/GROWI endpoints + the local Jev backend
    convert                     external mount -> raw Markdown only
    build wiki [<raw-rel>...]   raw/ -> wiki pages only
    build link [<raw-rel>...]   link pending wiki pages only
    build all [<raw-rel>...]    wiki batch first, then link batch (bare build is an alias)
    publish                     publish the current wiki/ tree to GROWI
    pull                        capture GROWI edits and update the local human overlay
    index [<raw-rel>...]        publish per-document + root index pages for growi-search
    sync [<mount-rel>...]       scan and drain queue -> candidate -> GROWI -> commit
                                then reconcile every index page against the wiki tree
    watch [<mount-rel>...]      queued 10-second metadata watcher + worker
    queue scan|work|status      operate the persistent watcher queue
    reset                       trash all publisher-owned GROWI pages
    link                        link every pending wiki (same as `build link`)
    link status | relink <doc> | rebuild --mode legacy|neo [--no-edges]
    
    Every command selects one INI from configs/ (or an absolute INI) with --project.
    """
    
    from __future__ import annotations
    
    import argparse
    import json
    import logging
    import os
    import sys
    import time
    from pathlib import Path
    from typing import Any
    
    from graph import config
    from graph.config import Settings, resolve_project_path
    from graph.workspace.project import open_project
    
    PROJECT_ROOT = Path(__file__).resolve().parent
    config.PROJECT_ROOT = PROJECT_ROOT
    
    log = logging.getLogger(__name__)
    
    
    def _settings(args: argparse.Namespace) -> Settings:
        settings = Settings.from_env(getattr(args, "project", ""))
        # downstream .env names the chat endpoint WIKI_CHAT_*; upstream reads OPENAI_*/WIKI_MODEL
        overrides = {
            "chat_base_url": os.environ.get("WIKI_CHAT_BASE_URL", ""),
            "chat_api_key": os.environ.get("WIKI_CHAT_API_KEY", ""),
            "chat_model": os.environ.get("WIKI_CHAT_MODEL", ""),
        }
        for key, value in overrides.items():
            if value:
                setattr(settings, key, value)
        if getattr(args, "data_root", None):
            settings.data_root = str(resolve_project_path(args.data_root).resolve())
        if getattr(args, "mode", None) and args.command != "link":
            settings.ingest_mode = args.mode
        linker = getattr(args, "linker", None)
        if linker == "off":
            settings.wiki_linker_enabled = False
        elif linker:
            settings.wiki_linker_enabled = True
            settings.wiki_linker_mode = linker
        if getattr(args, "timeout", None):
            settings.wiki_request_timeout = args.timeout
        return settings
    
    
    def _progress(event: dict[str, Any]) -> None:
        event = dict(event)
        stage = str(event.pop("stage", "work"))
        step = str(event.pop("step", ""))
        current, total = event.get("current"), event.get("total")
        progress = ""
        if isinstance(current, int) and isinstance(total, int) and total > 0:
            progress = f" {current}/{total} ({current * 100 // total}%)"
            event.pop("current", None)
            event.pop("total", None)
        details = json.dumps(event, ensure_ascii=False, default=str)
        log.debug(f"[{stage}] {step}{progress} {details}".rstrip())
    
    
    def _report(result: dict[str, Any]) -> int:
        for row in result["done"]:
            print(json.dumps(row, ensure_ascii=False))
        for failure in result["failures"]:
            logging.error("%s", failure)
        return 1 if result["failures"] else 0
    
    
    def _jev_check(settings: Settings) -> tuple[str, int]:
        """Resolve the project's Jev backend and, for gguf, start the local runtime once.
    
        The linker and the index builder only log a warning when the Jev engine fails to
        start and then fall back to the LLM, so a broken GGUF path would surface as slow,
        differently-scored links instead of an error. This proves the configured snapshot
        and jev-score binary answer one decision, without any sync, and it never downloads:
        a missing snapshot is reported rather than fetched.
        """
    
        from jev import JevConfig, JevQuestion, get_engine_for, reset_engine
    
        try:
            config = JevConfig.from_settings(settings)
        except Exception as exc:
            return f"jev     DOWN config ({type(exc).__name__}: {exc})", 1
        wanted = (str(getattr(settings, "wiki_linker_judge", "llm")) == "jev"
                  or bool(getattr(settings, "wiki_index_related_docs", False)))
        if not wanted:
            return f"jev     skipped (judge={settings.wiki_linker_judge}, related-docs off)", 0
        if config.backend != "gguf":
            return f"jev     {config.backend} backend (local start not checked)", 0
        if not config.gguf_local_path:
            return "jev     DOWN wiki_jev_gguf_local_path is empty", 1
        folder = Path(config.gguf_local_path).expanduser()
        files = ("jev_style_decision_gguf.py", "readout_config.json", "tokenizer/tokenizer.json",
                 f"Jev-Style-0.8B-Decision-v3-{config.gguf_quant}.gguf")
        missing = [name for name in files if not (folder / name).is_file()]
        binary = Path(config.gguf_binary).expanduser() if config.gguf_binary else folder / "build" / "jev-score"
        if not binary.is_file():
            missing.append(str(binary))
        elif not os.access(binary, os.X_OK):
            return f"jev     DOWN {binary} is not executable", 1
        if missing:
            return f"jev     DOWN missing from {folder}: {', '.join(missing)}", 1
        try:
            engine = get_engine_for(settings)
            result = engine.decide(
                {"section": {"page": "check", "heading": "接続手順",
                             "text": "本節ではシステムAからシステムBへの接続手順を説明する。"}},
                JevQuestion("この節は接続手順を説明しているか？"),
            )
        except Exception as exc:
            return f"jev     DOWN {type(exc).__name__}: {exc}", 1
        finally:
            reset_engine()
        return (f"jev     gguf {config.gguf_quant}/{config.gguf_many_mode} "
                f"p_yes={float(result.p_yes):.3f} {binary}"), 0
    
    
    def cmd_check(args: argparse.Namespace) -> int:
        import requests
    
        settings = _settings(args)
        growi = str(settings.growi_url or os.environ.get("GROWI_URL", "")).rstrip("/")
        targets = {
            "chat": f"{settings.chat_base_url.rstrip('/')}/models",
            "embed": f"{settings.embed_base_url.rstrip('/')}/models",
            "parser": f"{settings.parser_base_url.rstrip('/')}/health" if settings.parser_base_url else "",
            "growi": f"{growi}/_api/v3/healthcheck" if growi else "",
        }
        bad = 0
        for name, url in targets.items():
            if not url:
                print(f"{name:7} skipped (not configured)")
                continue
            try:
                response = requests.get(url, timeout=5)
                print(f"{name:7} {response.status_code} {url}")
                bad += response.status_code >= 400
            except Exception as exc:
                print(f"{name:7} DOWN {url} ({type(exc).__name__})")
                bad += 1
        jev_line, jev_bad = _jev_check(settings)
        print(jev_line)
        bad += jev_bad
        project = open_project(settings)
        print(f"data    {project.root.resolve()}  mount={project.mount} ingest={settings.ingest_mode} linker={'off' if not settings.wiki_linker_enabled else settings.wiki_linker_mode}")
        return 1 if bad else 0
    
    
    def cmd_sync(args: argparse.Namespace) -> int:
        from publisher import pipeline
        from publisher.queue import retry_failed, scan, work_once, worker_lock
    
        settings = _settings(args)
        project = open_project(settings)
        combined: dict[str, Any] = {"done": [], "failures": []}
        stale = pipeline.republish_if_stale(settings)
        if stale is not None:
            print(f"growi endpoint or markers changed: full republish {stale['run_id']}", flush=True)
            combined["failures"].extend(stale["failures"])
        with worker_lock(project):
            retry_failed(project)
            first = True
            resume = bool(getattr(args, "continue_run", False))
            while True:
                scan(
                    settings,
                    only=args.items or None,
                    settle_seconds=0,
                    force=args.force and first,
                    verify_content=True,
                )
                first = False
                result = work_once(settings, on_event=_progress if args.verbose else None, continue_run=resume)
                # Only the first batch can resume a kept worktree; later batches
                # in the same process are always fresh.
                resume = False
                if result is None:
                    break
                combined["done"].extend(result.get("done", []))
                combined["failures"].extend(result.get("failures", []))
                if result.get("failures"):
                    break
        if not combined["failures"]:
            # Index pages are derived output, so reconcile them against the whole wiki tree
            # here: a wiki built before its index exists catches up, and a page whose body
            # already matches GROWI is read but never rewritten.
            from publisher.index import build_index
    
            try:
                index = build_index(settings, on_progress=_progress if args.verbose else None)
            except Exception as exc:  # a stale table of contents must not fail a sync
                index = {"done": [], "failures": [f"index: {type(exc).__name__}: {exc}"]}
            combined["done"].extend(index["done"])
            combined["failures"].extend(index["failures"])
        return _report(combined)
    
    
    def cmd_pull(args: argparse.Namespace) -> int:
        from publisher.pipeline import pull_growi_once
    
        settings = _settings(args)
        result = (
            pull_growi_once(settings, force_inventory=True)
            if getattr(args, "inventory", False)
            else pull_growi_once(settings)
        )
        if result.get("human_sync"):
            for document, folder in sorted(folders.items())
            if document not in blocked
        ]
        not_ready = {project.wiki_dir(rel).relative_to(project.wiki).as_posix()
                     for rel in _wiki_raw_rels(project)
                     if project.wiki_dir(rel).is_dir() and (only is None or rel in only)} - set(folders) - blocked
        failures.extend(f"{document}: linker output is not ready" for document in sorted(not_ready))
        if publisher is not None:
            started = time.monotonic()
            try:
                if documents and begin_publish is not None:
                    begin_publish()
                if on_progress:
                    on_progress({
                        "stage": "growi-publish",
                        "step": "start",
                        "current": 0,
                        "total": len(documents),
                        "documents": len(documents),
                    })
                publish_args = {
                    "known_pages": known_pages,
                    **({"only_pages": only_pages} if only_pages is not None else {}),
                }
                pages = publisher.publish_documents(
                    project, [raw_rel for _, _, raw_rel in documents], **publish_args
                )
                published_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                for document, folder, raw_rel in documents:
                    ledger.published_documents[document] = {
                        "content_sha256": _content_hash(folder),
                        "growi_path": publisher.doc_path(project, raw_rel),
                        "raw_rel": raw_rel,
                        "published_at": published_at,
                    }
                updated_pages = {path: _published_page_row(project, publisher, path, page) for path, page in pages.items()}
                if only_pages is None:
                    published_prefixes = tuple(document.rstrip("/") + "/" for document, _, _ in documents)
                    ledger.published_pages = {
                        path: row for path, row in ledger.published_pages.items()
                        if not path.startswith(published_prefixes)
                    }
                ledger.published_pages.update(updated_pages)
                log.debug("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
                if on_progress:
                    on_progress({
                        "stage": "growi-publish",
                        "step": "done",
                        "current": len(documents),
                        "total": len(documents),
                        "pages": len(pages),
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                    })
            except Exception as exc:
                failures.append(f"publish: {type(exc).__name__}: {exc}")
                log.error("run=%s stage=publish error=%s: %s", run_id, type(exc).__name__, exc)
        for document, row in list(ledger.published_documents.items()):
            if scoped_documents is not None and document not in scoped_documents:
                continue
            if (project.wiki / document).is_dir() or publisher is None:
                continue
            raw_rel = _document_raw_rel(document)
            try:
                if begin_publish is not None:
                    begin_publish()
                prefix = document.rstrip("/") + "/"
                if hasattr(publisher, "assert_known_revisions"):
                    publisher.assert_known_revisions({
                        path: page for path, page in ledger.published_pages.items() if path.startswith(prefix)
                    })
                delete_args = {"known_pages": {path: page for path, page in ledger.published_pages.items() if path.startswith(prefix)}} if isinstance(publisher, GrowiPublisher) else {}
                publisher.delete_document(project, raw_rel, **delete_args)
                delete_document_index(publisher, document)
                ledger.published_documents.pop(document, None)
                ledger.published_pages = {
                    path: row for path, row in ledger.published_pages.items() if not path.startswith(prefix)
                }
            except Exception as exc:
                failures.append(f"{document}: {type(exc).__name__}: {exc}")
        if publisher is not None and settings is not None and not failures:
            # Index pages are derived: they follow a batch that published cleanly, and a
            # failure here is logged rather than failed, because rolling back a published
            # document over a table of contents is worse. A failed batch indexes nothing and
            # the retry indexes it once.
            try:
                for problem in build_index(
                    settings,
                    only=sorted(only) if only is not None else None,
                    locked=True,
                    ledger=ledger,
                    on_progress=on_progress,
                )["failures"]:
                    log.warning("run=%s stage=index error=%s", run_id, problem)
            except Exception as exc:
                log.warning("run=%s stage=index error=%s: %s", run_id, type(exc).__name__, exc)
        return failures
    
    
    def _remove_sources(project: Project, ledger: Ledger, sources: dict[str, str]) -> tuple[list[dict[str, Any]], set[str], list[str]]:
        done: list[dict[str, Any]] = []
        touched_raw: set[str] = set()
        failures: list[str] = []
        for rel, raw_rel in sources.items():
            try:
                source = dict(ledger.sources.get(rel) or {})
                from .human_changes import HumanStore
    
                HumanStore(project).archive(raw_rel)
                if (project.wiki_dir(raw_rel) / "_planning" / "linker.json").exists():
                    from graph.linker import remove_document
                    touched = remove_document(project, raw_rel)
                    touched_raw.update(touched)
                else:
                    touched = []
                shutil.rmtree(project.wiki_dir(raw_rel), ignore_errors=True)
                shutil.rmtree(project.state_dir(raw_rel), ignore_errors=True)
            if deleted:
                save_ledger(ledger_path, ledger)
            changed = set(scan.added) | set(scan.changed)
            if wanted is not None:
                changed &= wanted
                if force:
                    changed |= wanted & set(scan.files)
            changed = sorted(changed)
            incremental_pages: set[str] = set(captured_pages)
            regenerated_pages: set[str] = set()
            incremental_publish = bool(changed) and not deleted
            scoped_candidates = None if include_pending else (sorted(scoped_raw) if wanted is not None else None)
            pending_before = _pending_link_rels(project, settings, scoped_candidates)
            model = _model(settings, project) if changed or pending_before else None
            try:
                embedder = Embedder(settings) if changed or pending_before else None
            except Exception as exc:
                if (str(getattr(settings, "embed_backend", "server")) == "off"
                        and isinstance(exc, ValueError)
                        and str(exc) == "the linker requires WIKI_EMBED_BACKEND=server"):
                    log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
                else:
                    log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
                embedder = None
            source_details = source_details or {}
            retried: set[str] = set()
            for rel in changed:  # grows: a failed document is retried once at the end
                if should_continue is not None and not should_continue():
                    cancelled = True
                    break
                item = scan.files[rel]
                raw_rel = _raw_rel(item)
                details = source_details.get(rel, {})
                previous_source = dict(ledger.sources.get(rel) or {})
                classification = str(details.get("classification") or "none")
                requested_resume = not force if resume is None else resume
                if classification == "forced":
                    requested_resume = False
                started = time.monotonic()
                try:
                    log.debug("run=%s path=%s stage=parse start", run_id, rel)
                    if on_progress:
                        on_progress({
                            "stage": "parse",
                            "step": "start",
                            "file": rel,
                            "parser": item.parser,
                            "bytes": item.size,
                        })
                    with _progress_heartbeat(on_progress, stage="parse", file=rel):
                        raw_path = project.raw_file(raw_rel)
                        previous_markdown = (
                            raw_path.read_text(encoding="utf-8")
                            if (previous_source or details.get("source_sha256")) and raw_path.exists()
                            else None
                        )
                        if _can_resume_parsed_source(
                            previous_source,
                            item,
                            raw_path,
                            requested_resume=requested_resume,
                            classification=classification,
                            known_source_sha256=str(details.get("source_sha256") or ""),
                            known_source_blob_oid=str(details.get("source_blob_oid") or ""),
                        ):
                            assert previous_markdown is not None
                            markdown = previous_markdown
                            log.debug("run=%s path=%s stage=parse resumed", run_id, rel)
                            if on_progress:
                                on_progress({"stage": "parse", "step": "resumed", "file": rel})
                        else:
                            parse_args = (
                                {"previous_markdown": previous_markdown}
                                if previous_markdown is not None
                                else {}
                            )
                            markdown = _parse(item, project.mount / rel, settings, **parse_args)
                    _assert_source_unchanged(item, project.mount / rel)
                    if previous_markdown is not None and classification != "forced":
                        _check_parse_size(rel, previous_markdown, markdown)
                    if on_progress:
                        on_progress({
                            "stage": "parse",
                            "step": "done",
                            "file": rel,
                            "characters": len(markdown),
                            "elapsed_seconds": round(time.monotonic() - started, 1),
                        })
                    _write_raw(project.raw_file(raw_rel), markdown)
                    wiki_started = time.monotonic()
                    if on_progress:
                        on_progress({"stage": "wiki", "step": "start", "file": raw_rel})
                    with _progress_heartbeat(on_progress, stage="wiki", file=raw_rel):
                        result = write_wiki_pages(
                            project, raw_rel, mode=str(settings.ingest_mode), settings=settings,
                            llm=model, embedder=embedder, on_progress=on_progress,
                            resume=requested_resume,
                            stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                            identity_seed=str(
                                previous_source.get("id_seed")
                                or ((Path(raw_rel).parent / wiki_folder_name(Path(raw_rel).name)).as_posix() if previous_source else "")
                                or details.get("source_id")
                                or raw_rel
                            ),
                        )
                    if getattr(result, "rebuild", "full") == "incremental":
                        incremental_pages.update(getattr(result, "changed_pages", []))
                        regenerated_pages.update(getattr(result, "regenerated_pages", []))
                    else:
                        incremental_publish = False
                    if on_progress:
                        on_progress({
                            "stage": "wiki",
                            "step": "done",
                            "file": raw_rel,
                            "touched_documents": len(result.touched),
                            "elapsed_seconds": round(time.monotonic() - wiki_started, 1),
                        })
                    ledger.sources[rel] = _source_row(item, raw_rel, details=details, previous=previous_source)
                    _store_source(project, item, ledger.sources[rel], project.mount / rel)
                    save_ledger(ledger_path, ledger)
                    done.append({
                        "path": rel,
                        "status": "changed" if previous_source else "added",
                        "touched": result.touched,
                        "rebuild": "incremental" if getattr(result, "rebuild", "full") == "incremental" else "full",
                        "tier": getattr(result, "tier", 3),
                        "reason": getattr(result, "reason", ""),
                        "human_edits_overwritten": list(getattr(result, "human_edits_overwritten", [])),
                    })
                    log.debug("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
                except asyncio.CancelledError:
                    cancelled = True
                    break
                except Exception as exc:
                    if should_continue is not None and not should_continue():
                        cancelled = True
                        break
                    if rel not in retried:
                        retried.add(rel)
                        changed.append(rel)
                        log.warning("run=%s path=%s stage=generate retry_later error=%s: %s", run_id, rel, type(exc).__name__, exc)
                        continue
                    # Second failure: skip until the next sync.  Keep the source
                    # digest in the error row so an unchanged retry can skip parse.
                    error = f"{type(exc).__name__}: {exc}"[:500]
                    ledger.sources[rel] = _source_row(item, raw_rel, error, details=details, previous=previous_source)
                    save_ledger(ledger_path, ledger)
                    failures.append(f"{rel}: {error}")
                    log.error("run=%s path=%s stage=generate error=%s: %s", run_id, rel, type(exc).__name__, exc)
            pending_links = _pending_link_rels(project, settings, scoped_candidates)
            if should_continue is not None and not should_continue():
                cancelled = True
            if pending_links and not failures and not cancelled:
                try:
                    if on_progress:
                        on_progress({
                            "stage": "linker",
                            "step": "batch_start",
                            "current": 0,
                            "total": len(pending_links),
                    begin_publish()
                for job, old_raw, new_raw, new_document, moved_pages in moves:
                    remote = publisher.move_document(project, old_raw, new_raw, moved_pages)
                    for local_path, page in remote.items():
                        moved_pages[local_path] = _published_page_row(project, publisher, local_path, page)
                    ledger.published_pages.update(moved_pages)
                    if new_document in ledger.published_documents:
                        ledger.published_documents[new_document]["growi_path"] = publisher.doc_path(project, new_raw)
                    save_ledger(ledger_path, ledger)
                    done.append({"path": str(job.rel), "from": str(job.from_rel), "status": "moved", "rebuild": "move"})
                if touched_raw:
                    index_paths.update(touched_raw)
                    failures.extend(_publish_sweep(
                        project, ledger, publisher, run_id, only=touched_raw, on_progress=on_progress,
                        settings=settings,
                        **({"begin_publish": begin_publish} if begin_publish is not None else {}),
                    ))
                if moves and not failures:
                    # A pure move does not need a document publish sweep, but its old/new
                    # document indexes and both ancestor trees still have to move. Indexes
                    # are derived output, so keep their failures non-transactional just as
                    # _publish_sweep does for add/update/delete.
                    from .index import build_index
    
                    try:
                        for problem in build_index(
                            settings,
                            only=sorted(index_paths),
                            locked=True,
                            ledger=ledger,
                            on_progress=on_progress,
                        )["failures"]:
                            log.warning("run=%s stage=index error=%s", run_id, problem)
                    except Exception as exc:
                        log.warning("run=%s stage=index error=%s: %s", run_id, type(exc).__name__, exc)
                save_ledger(ledger_path, ledger)
            except Exception as exc:
                failures.append(f"move: {type(exc).__name__}: {exc}")
        return {
            "run_id": run_id,
            "done": done,
            "failures": failures,
            "cancelled": False,
            "index_paths": sorted(index_paths),
        }
    
    
    def _sources_by_id(ledger: Ledger) -> dict[str, tuple[str, dict[str, Any]]]:
        return {
            str(row.get("source_id")): (rel, row)
            for rel, row in ledger.sources.items()
            for rel in rels:
                if rel not in pending:
                    done.append({"path": rel, "status": "up-to-date"})
                    continue
                try:
                    result = write_wiki_pages(project, rel, mode=str(settings.ingest_mode), settings=settings, llm=model, embedder=None, on_progress=on_progress, resume=not force)
                    done.append({"path": rel, "status": "built", "touched": result.touched})
                except Exception as exc:
                    failures.append(f"{rel}: {type(exc).__name__}: {exc}")
        return {"run_id": run_id, "done": done, "failures": failures}
    
    
    def link_raw(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Link pending wiki documents without regenerating their pages."""
        project = open_project(settings)
        rels = list(dict.fromkeys(rel.strip().lstrip("/") for rel in only)) if only is not None else _wiki_raw_rels(project)
        missing = [rel for rel in rels if not project.wiki_dir(rel).exists()]
        if missing:
            raise FileNotFoundError(f"no wiki output for: {missing}")
        pending = _pending_link_rels(project, settings, rels, force=force)
        run_id = "lrun-" + uuid.uuid4().hex[:16]
        done: list[dict[str, Any]] = []
        failures: list[str] = []
        with _lock(project):
            if not pending:
                return {"run_id": run_id, "done": [{"status": "up-to-date"}], "failures": []}
            model = _model(settings, project)
            try:
                embedder = Embedder(settings)
            except Exception as exc:
                if (str(getattr(settings, "embed_backend", "server")) == "off"
                        and isinstance(exc, ValueError)
                        and str(exc) == "the linker requires WIKI_EMBED_BACKEND=server"):
                    log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
                else:
                    log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
                embedder = None
            try:
                touched = run_linkers(project, pending, settings=settings, llm=model, embedder=embedder, on_progress=on_progress)
                done.append({"status": "linked", "documents": pending, "touched": touched})
            except Exception as exc:
                failures.append(f"link: {type(exc).__name__}: {exc}")
        return {"run_id": run_id, "done": done, "failures": failures}
    
    
    def build_raw(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Generate the complete wiki batch first, then link it as one batch."""
        wiki = build_wiki_only(settings, only=only, force=force, on_progress=on_progress)
        project = open_project(settings)
        link_only = [rel for rel in only if project.wiki_dir(rel.strip().lstrip("/")).exists()] if only else None
        links = link_raw(settings, only=link_only, force=force, on_progress=on_progress)
    graph/workspace/writer.py:325:    on_progress: Progress = None,
    graph/workspace/writer.py:342:            on_progress=on_progress,
    graph/workspace/writer.py:358:    on_progress: Progress = None,
    graph/workspace/writer.py:379:        run_async_blocking(runner(source_path, run_dir=out_dir, model=model, config=config, on_progress=on_progress, stop_check=stop_check))
    graph/workspace/writer.py:389:            on_progress=on_progress,
    graph/workspace/writer.py:418:        on_progress=on_progress,
    graph/workspace/writer.py:492:    on_progress: Progress = None,
    graph/workspace/writer.py:567:    if on_progress:
    graph/workspace/writer.py:568:        on_progress(decision_event())
    graph/workspace/writer.py:599:                    state_dir=state_root, on_progress=on_progress, stop_check=stop_check,
    graph/workspace/writer.py:616:                if on_progress:
    graph/workspace/writer.py:617:                    on_progress({"stage": "wiki", "step": "patch_escalated", "file": rel, "pages": sorted(failed), "errors": failed})
    graph/workspace/writer.py:625:                        state_dir=state_root, on_progress=on_progress, stop_check=stop_check,
    graph/workspace/writer.py:636:            } and on_progress:
    graph/workspace/writer.py:637:                on_progress(decision_event())
    graph/workspace/writer.py:644:                state_dir=state_root, on_progress=on_progress, stop_check=stop_check,
    graph/workspace/writer.py:682:        if (overlay.conflicts or overlay.orphaned) and on_progress:
    graph/workspace/writer.py:683:            on_progress({"stage": "wiki", "step": "human_overlay", "file": rel,
    graph/workspace/writer.py:699:    on_progress: Progress = None, stop_check: StopCheck = None,
    graph/workspace/writer.py:704:        on_progress=on_progress, stop_check=stop_check,
    graph/workspace/writer.py:708:        on_progress=on_progress, stop_check=stop_check,
    graph/workspace/writer.py:720:    on_progress: Progress = None,
    graph/workspace/writer.py:748:            if on_progress:
    graph/workspace/writer.py:749:                on_progress({"stage": "linker", "step": "embedder_unavailable", "error": str(exc)[:200]})
    graph/workspace/writer.py:751:    result = run_async_blocking(link_document(project, rel, model=model, embedder=embedder, settings=settings, on_progress=on_progress, stop_check=stop_check))
    graph/workspace/writer.py:760:    on_progress: Progress = None, stop_check: StopCheck = None,
    graph/workspace/writer.py:779:        on_progress=on_progress, stop_check=stop_check, changed_pages=changed_pages,
    graph/workspace/convert.py:15:def convert_mount(project: Project, *, parser_base_url: str, settings: Any, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    graph/workspace/convert.py:49:        if on_progress:
    graph/workspace/convert.py:50:            on_progress({"stage": "convert", "file": rel})
    publisher/index.py:433:                on_progress: Callable[[dict[str, Any]], None] | None = None,
    publisher/index.py:448:                on_progress=on_progress,
    publisher/index.py:524:            if on_progress:
    publisher/index.py:525:                on_progress({"stage": "index", "step": "document", "current": indexed, "total": total, "document": document})
    publisher/index.py:566:            if on_progress:
    publisher/index.py:567:                on_progress({"stage": "index", "step": "folder", "current": indexed, "total": total, "folder": folder})
    publisher/queue.py:966:                                         on_progress=on_event, prepare_publish=prepare_publish,
    publisher/queue.py:975:                        should_continue=is_current, include_pending=True, on_progress=on_event,
    publisher/queue.py:1049:                on_progress=on_event,
    publisher/queue.py:1145:                            emit("index", build_index(settings, on_progress=on_event))
    graph/formats/pptx.py:128:async def plan(lines: Sequence[str], *, config: Any, model: Any, on_progress=None, stop_check=None) -> CompiledSeedPlan | None:
    graph/formats/xlsx.py:171:    on_progress=None,
    graph/formats/xlsx.py:181:    if on_progress:
    graph/formats/xlsx.py:182:        on_progress({
    graph/formats/xlsx.py:209:        on_progress=on_progress,
    graph/formats/xlsx.py:213:    if on_progress:
    graph/formats/xlsx.py:214:        on_progress({
    graph/formats/xlsx.py:269:        if on_progress:
    graph/formats/xlsx.py:270:            on_progress({
    graph/formats/xlsx.py:278:    if on_progress:
    graph/formats/xlsx.py:279:        on_progress({
    graph/formats/xlsx.py:346:async def run(source_path: Path, *, run_dir: Path, model: Any, config: Any, on_progress=None, stop_check=None):
    graph/formats/xlsx.py:354:        on_progress=on_progress,
    graph/formats/xlsx.py:369:        on_progress=on_progress,
    publisher/pipeline.py:514:    on_progress: Callable[[dict[str, Any]], None] | None = None,
    publisher/pipeline.py:547:                        run_linkers(project, pending, settings=settings, llm=None, embedder=None, on_progress=on_progress)
    publisher/pipeline.py:572:            if on_progress:
    publisher/pipeline.py:573:                on_progress({
    publisher/pipeline.py:603:            log.debug("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
    publisher/pipeline.py:604:            if on_progress:
    publisher/pipeline.py:605:                on_progress({
    publisher/pipeline.py:615:            log.error("run=%s stage=publish error=%s: %s", run_id, type(exc).__name__, exc)
    publisher/pipeline.py:650:                on_progress=on_progress,
    publisher/pipeline.py:652:                log.warning("run=%s stage=index error=%s", run_id, problem)
    publisher/pipeline.py:654:            log.warning("run=%s stage=index error=%s: %s", run_id, type(exc).__name__, exc)
    publisher/pipeline.py:742:    on_progress: Callable[[dict[str, Any]], None] | None = None,
    publisher/pipeline.py:832:                log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
    publisher/pipeline.py:850:                log.debug("run=%s path=%s stage=parse start", run_id, rel)
    publisher/pipeline.py:851:                if on_progress:
    publisher/pipeline.py:852:                    on_progress({
    publisher/pipeline.py:859:                with _progress_heartbeat(on_progress, stage="parse", file=rel):
    publisher/pipeline.py:877:                        log.debug("run=%s path=%s stage=parse resumed", run_id, rel)
    publisher/pipeline.py:878:                        if on_progress:
    publisher/pipeline.py:879:                            on_progress({"stage": "parse", "step": "resumed", "file": rel})
    publisher/pipeline.py:890:                if on_progress:
    publisher/pipeline.py:891:                    on_progress({
    publisher/pipeline.py:900:                if on_progress:
    publisher/pipeline.py:901:                    on_progress({"stage": "wiki", "step": "start", "file": raw_rel})
    publisher/pipeline.py:902:                with _progress_heartbeat(on_progress, stage="wiki", file=raw_rel):
    publisher/pipeline.py:905:                        llm=model, embedder=embedder, on_progress=on_progress,
    publisher/pipeline.py:920:                if on_progress:
    publisher/pipeline.py:921:                    on_progress({
    publisher/pipeline.py:940:                log.debug("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
    publisher/pipeline.py:951:                    log.warning("run=%s path=%s stage=generate retry_later error=%s: %s", run_id, rel, type(exc).__name__, exc)
    publisher/pipeline.py:959:                log.error("run=%s path=%s stage=generate error=%s: %s", run_id, rel, type(exc).__name__, exc)
    publisher/pipeline.py:965:                if on_progress:
    publisher/pipeline.py:966:                    on_progress({
    publisher/pipeline.py:973:                with _progress_heartbeat(on_progress, stage="linker", file="batch"):
    publisher/pipeline.py:976:                        on_progress=on_progress,
    publisher/pipeline.py:983:                if on_progress:
    publisher/pipeline.py:984:                    on_progress({
    publisher/pipeline.py:1006:                "on_progress": on_progress,
    publisher/pipeline.py:1111:    on_progress: Callable[[dict[str, Any]], None] | None = None,
    publisher/pipeline.py:1231:                    settings=settings, llm=None, embedder=None, on_progress=on_progress,
    publisher/pipeline.py:1253:                    project, ledger, publisher, run_id, only=touched_raw, on_progress=on_progress,
    publisher/pipeline.py:1270:                        on_progress=on_progress,
    publisher/pipeline.py:1272:                        log.warning("run=%s stage=index error=%s", run_id, problem)
    publisher/pipeline.py:1274:                    log.warning("run=%s stage=index error=%s: %s", run_id, type(exc).__name__, exc)
    publisher/pipeline.py:1568:def build_wiki_only(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    publisher/pipeline.py:1605:                result = write_wiki_pages(project, rel, mode=str(settings.ingest_mode), settings=settings, llm=model, embedder=None, on_progress=on_progress, resume=not force)
    publisher/pipeline.py:1612:def link_raw(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    publisher/pipeline.py:1635:                log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
    publisher/pipeline.py:1638:            touched = run_linkers(project, pending, settings=settings, llm=model, embedder=embedder, on_progress=on_progress)
    publisher/pipeline.py:1645:def build_raw(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    publisher/pipeline.py:1647:    wiki = build_wiki_only(settings, only=only, force=force, on_progress=on_progress)
    publisher/pipeline.py:1650:    links = link_raw(settings, only=link_only, force=force, on_progress=on_progress)
    graph/formats/tabular.py:507:    on_progress=None,
    graph/formats/tabular.py:520:    if on_progress:
    graph/formats/tabular.py:521:        on_progress({"stage": progress_stage, "step": "start", "total": len(sheets)})
    graph/formats/tabular.py:535:            if on_progress:
    graph/formats/tabular.py:536:                on_progress({
    graph/formats/tabular.py:576:            if on_progress:
    graph/formats/tabular.py:577:                on_progress({
    graph/formats/tabular.py:632:        if on_progress:
    graph/formats/tabular.py:633:            on_progress({"stage": "tabular", "sheet": sheet, "tables": len(tables)})
    graph/formats/tabular.py:634:    if on_progress:
    graph/formats/tabular.py:635:        on_progress({"stage": progress_stage, "step": "complete", "current": len(sheets), "total": len(sheets)})
    graph/linker/service.py:242:    on_progress: Progress = None,
    graph/linker/service.py:280:        if on_progress:
    graph/linker/service.py:281:            on_progress({"stage": "linker", "step": "page_curated", "page": page_rel, "current": completed, "total": len(jobs)})
    graph/linker/service.py:393:    on_progress: Progress = None, stop_check: StopCheck = None, render: bool = True,
    graph/linker/service.py:435:    if on_progress:
    graph/linker/service.py:436:        on_progress({"stage": "linker", "step": "pending", "document": rel})
    graph/linker/service.py:519:            if on_progress:
    graph/linker/service.py:520:                on_progress({
    graph/linker/service.py:699:            if incremental_scope and on_progress:
    graph/linker/service.py:700:                on_progress({
    graph/linker/service.py:746:                if on_progress:
    graph/linker/service.py:747:                    on_progress({"stage": "linker", "step": "edge_target_done", "document": rel, "current": completed, "total": len(unresolved)})
    graph/linker/service.py:826:                rendered_docs = await render_pages(project, catalog, pages, model=model, settings=settings, mode=mode, on_progress=on_progress)
    graph/linker/service.py:842:            if on_progress:
    graph/linker/service.py:843:                on_progress({"stage": "linker", "step": "done", "document": rel, "edges": len(edge_rows)})
    graph/linker/service.py:847:        if on_progress:
    graph/linker/service.py:848:            on_progress({"stage": "linker", "step": "failed", "document": rel, "error": str(exc)[:200]})
    graph/linker/service.py:857:    on_progress: Progress = None, stop_check: StopCheck = None,
    graph/linker/service.py:867:            on_progress=on_progress, stop_check=stop_check, render=False,
    graph/linker/service.py:883:                touched_docs = await render_pages(project, catalog, pages, model=model, settings=settings, mode=mode, on_progress=on_progress)
    graph/formats/__init__.py:32:    on_progress=None, stop_check=None,
    graph/formats/__init__.py:42:            lines, config=config, model=model, on_progress=on_progress, stop_check=stop_check
    graph/wiki/__main__.py:88:    result = asyncio.run(run_pipeline(args.source, config=config, on_progress=progress))
    graph/formats/csv.py:11:async def run(source_path: Path, *, run_dir: Path, model: Any, config: Any, on_progress=None, stop_check=None):
    graph/formats/csv.py:19:    return await write_tables(sheets=[(title, body, (1, len(lines)))], run_dir=run_dir, model=model, config=config, on_progress=on_progress, stop_check=stop_check)
    graph/wiki/pipeline.py:621:    on_progress: Progress,
    graph/wiki/pipeline.py:648:            _emit(on_progress, "research", "resumed", page=page.title)
    graph/wiki/pipeline.py:662:        on_progress,
    graph/wiki/pipeline.py:715:            on_progress,
    graph/wiki/pipeline.py:935:    on_progress: Progress,
    graph/wiki/pipeline.py:997:                    on_progress,
    graph/wiki/pipeline.py:1045:                on_progress, "write", "section_retry",
    graph/wiki/pipeline.py:1072:            _emit(on_progress, "write", "judge_unavailable",
    graph/wiki/pipeline.py:1080:            on_progress, "write", "section_judged",
    graph/wiki/pipeline.py:1105:    _emit(on_progress, "write", "section_verbatim",
    graph/wiki/pipeline.py:1177:    on_progress: Progress,
    graph/wiki/pipeline.py:1192:        stop_check=stop_check, on_progress=on_progress,
    graph/wiki/pipeline.py:1211:            stop_check=stop_check, on_progress=on_progress,
    graph/wiki/pipeline.py:1268:    on_progress: Progress,
    graph/wiki/pipeline.py:1295:            _emit(on_progress, "rewrite", "page_resumed",
    graph/wiki/pipeline.py:1308:                stop_check=stop_check, on_progress=on_progress,
    graph/wiki/pipeline.py:1325:            on_progress, "rewrite", "page_done",
    graph/wiki/pipeline.py:1412:    on_progress: Progress = None,
    graph/wiki/pipeline.py:1452:        _emit(on_progress, "seed", "resumed", pages=len(pages), source_lines=len(lines))
    graph/wiki/pipeline.py:1454:        _emit(on_progress, "seed", "start", source_lines=len(lines))
    graph/wiki/pipeline.py:1466:            on_progress=on_progress,
    graph/wiki/pipeline.py:1479:                _emit(on_progress, "seed", "structural_rejected", reason=error)
    graph/wiki/pipeline.py:1488:                on_progress=on_progress,
    graph/wiki/pipeline.py:1498:                on_progress=on_progress,
    graph/wiki/pipeline.py:1500:        _emit(on_progress, "seed", "structural" if structural is not None and seed_plan is not None else "llm", pages=len(seed_plan.pages))
    graph/wiki/pipeline.py:1508:        _emit(on_progress, "context", "start", parents=len({tuple(page.path[:-1]) for page in pages}))
    graph/wiki/pipeline.py:1517:        _emit(on_progress, "context", "done")
    graph/wiki/pipeline.py:1567:    _emit(on_progress, "seed", "done", pages=len(pages), images=len(units))
    graph/wiki/pipeline.py:1584:        on_progress=on_progress,
    graph/wiki/pipeline.py:1635:    _emit(on_progress, "publish", "done", output=str(wiki_root), review_pages=len(flagged))
    graph/wiki/legacy.py:2638:    on_progress: Callable[[dict[str, Any]], None] | None = None,
    graph/wiki/legacy.py:2648:            on_progress=on_progress,
    graph/wiki/legacy.py:2661:    on_progress: Callable[[dict[str, Any]], None] | None = None,
    graph/wiki/legacy.py:2691:    if on_progress:
    graph/wiki/legacy.py:2692:        on_progress(
    graph/wiki/legacy.py:2738:        if on_progress:
    graph/wiki/legacy.py:2739:            on_progress(
    graph/wiki/legacy.py:2821:    if on_progress:
    graph/wiki/legacy.py:2822:        on_progress(
    graph/wiki/document_map.py:376:    on_progress: Callable[[dict], None] | None,
    graph/wiki/document_map.py:474:        if on_progress:
    graph/wiki/document_map.py:475:            on_progress(
    graph/wiki/document_map.py:513:    on_progress: Callable[[dict], None] | None,
    graph/wiki/document_map.py:529:                if on_progress:
    graph/wiki/document_map.py:530:                    on_progress({"stage": "plan", "step": "semantic", "cached": True})
    graph/wiki/document_map.py:562:                if on_progress:
    graph/wiki/document_map.py:563:                    on_progress(
    graph/wiki/document_map.py:575:    if on_progress:
    graph/wiki/document_map.py:576:        on_progress(
    graph/wiki/document_map.py:591:    on_progress: Callable[[dict], None] | None,
    graph/wiki/document_map.py:618:                if on_progress:
    graph/wiki/document_map.py:619:                    on_progress(
    graph/wiki/document_map.py:689:                if on_progress:
    graph/wiki/document_map.py:690:                    on_progress(
    graph/wiki/document_map.py:716:        if on_progress:
    graph/wiki/document_map.py:717:            on_progress(
    graph/wiki/document_map.py:738:    on_progress: Callable[[dict], None] | None = None,
    graph/wiki/document_map.py:753:        on_progress=on_progress,
    ✓ • 1ms

## Activity

    $ rg -n 'verbose|LOG_LEVEL|log\.info|log\.warning|log\.error|print\(' README.md publisher/queue.py publisher/index.py graph/linker/service.py graph/workspace/writer.py graph/wiki/pipeline.py graph/wiki/legacy.py jev/engine.py jev/backends/torch.py
    jev/backends/torch.py:158:            log.warning("WIKI_JEV_SHARE_STATE is unavailable: cache continuation exceeded the parity tolerance")
    jev/backends/torch.py:170:        log.info("Jev fast kernels available: %s", backend.stats["fast_kernels"])
    jev/backends/torch.py:179:            except Exception as exc: log.warning("torch.compile unavailable: %s", exc)
    jev/engine.py:268:            log.info("Jev stats: %s", stats_summary(self._stats, time.monotonic() - self._started))
    graph/linker/service.py:190:            log.warning("Jev page curation failed for %s: %s", page_rel, exc)
    graph/linker/service.py:253:            log.warning("Jev linker engine unavailable for page curation: %s", exc)
    graph/linker/service.py:380:            log.warning("Jev edge judge failed for %s: %s", target.chunk_id, exc)
    graph/linker/service.py:448:                log.warning("Jev linker engine unavailable; using LLM: %s", exc)
    graph/linker/service.py:565:                    log.warning("Jev alias resolution failed for %s: %s", document, exc)
    graph/linker/service.py:698:                            log.warning("Jev primary definer failed for %s: %s", canon, exc)
    publisher/index.py:193:            log.warning("Jev related document judge failed: %s", exc)
    publisher/index.py:631:        log.warning("index page for %s: %s: %s", document, type(exc).__name__, exc)
    ✓ • 12ms

## Activity

    $ rg -n --glob '*.py' '(logging|logger|log)\.info\(' --glob '!growi-search/**' .; rg -n --glob '*.py' '(logging|logger|log)\.(warning|error|exception|critical)\(' publisher graph jev main.py
    ./jev/engine.py:268:            log.info("Jev stats: %s", stats_summary(self._stats, time.monotonic() - self._started))
    ./jev/backends/torch.py:170:        log.info("Jev fast kernels available: %s", backend.stats["fast_kernels"])
    main.py:87:        logging.error("%s", failure)
    publisher/pipeline.py:615:            log.error("run=%s stage=publish error=%s: %s", run_id, type(exc).__name__, exc)
    publisher/pipeline.py:652:                log.warning("run=%s stage=index error=%s", run_id, problem)
    publisher/pipeline.py:654:            log.warning("run=%s stage=index error=%s: %s", run_id, type(exc).__name__, exc)
    publisher/pipeline.py:832:                log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
    publisher/pipeline.py:951:                    log.warning("run=%s path=%s stage=generate retry_later error=%s: %s", run_id, rel, type(exc).__name__, exc)
    publisher/pipeline.py:959:                log.error("run=%s path=%s stage=generate error=%s: %s", run_id, rel, type(exc).__name__, exc)
    publisher/pipeline.py:1272:                        log.warning("run=%s stage=index error=%s", run_id, problem)
    publisher/pipeline.py:1274:                    log.warning("run=%s stage=index error=%s: %s", run_id, type(exc).__name__, exc)
    publisher/pipeline.py:1635:                log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
    jev/engine.py:221:        except Exception: log.exception("closing Jev backend")
    publisher/index.py:193:            log.warning("Jev related document judge failed: %s", exc)
    publisher/index.py:631:        log.warning("index page for %s: %s: %s", document, type(exc).__name__, exc)
    jev/backends/torch.py:158:            log.warning("WIKI_JEV_SHARE_STATE is unavailable: cache continuation exceeded the parity tolerance")
    jev/backends/torch.py:179:            except Exception as exc: log.warning("torch.compile unavailable: %s", exc)
    graph/linker/service.py:190:            log.warning("Jev page curation failed for %s: %s", page_rel, exc)
    graph/linker/service.py:253:            log.warning("Jev linker engine unavailable for page curation: %s", exc)
    graph/linker/service.py:380:            log.warning("Jev edge judge failed for %s: %s", target.chunk_id, exc)
    graph/linker/service.py:448:                log.warning("Jev linker engine unavailable; using LLM: %s", exc)
    graph/linker/service.py:565:                    log.warning("Jev alias resolution failed for %s: %s", document, exc)
    graph/linker/service.py:698:                            log.warning("Jev primary definer failed for %s: %s", canon, exc)
    graph/linker/jev_judge.py:65:            log.warning("Jev role check failed for chunk %s: %s", item.chunk_id, exc)
    graph/linker/jev_judge.py:262:        log.warning("LLM tie-break failed for %s: %s", target.chunk_id, exc)
    ✓ • 0ms

## Assistant

Two routine `INFO` messages remain in the ingestion dependency path (`jev` backend stats/kernel availability); they would still print at the default `INFO` level during sync. I’m folding those into the same DEBUG-only behavior. I’m leaving warnings/errors and machine-readable CLI output intact because they are failure/results channels, not routine progress logs.

## Activity

    file changes: Completed · 2 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/jev/backends/torch.py
    @@ -169,3 +169,3 @@
                 backend.stats["fast_kernels"] = False
    -        log.info("Jev fast kernels available: %s", backend.stats["fast_kernels"])
    +        log.debug("Jev fast kernels available: %s", backend.stats["fast_kernels"])
             renderer = getattr(runtime, "renderer", None)
    Update { move_path: None }: /home/seigyo/llm-wiki/jev/engine.py
    @@ -267,3 +267,3 @@
             if self.stats_seconds and time.monotonic() - self._last_stats_log >= self.stats_seconds:
    -            log.info("Jev stats: %s", stats_summary(self._stats, time.monotonic() - self._started))
    +            log.debug("Jev stats: %s", stats_summary(self._stats, time.monotonic() - self._started))
                 self._last_stats_log = time.monotonic()

## Activity

    $ python -m py_compile main.py publisher/pipeline.py publisher/human_changes.py graph/wiki/legacy.py jev/engine.py jev/backends/torch.py && git diff --check && printf '%s\n' '--- non-debug INFO outside growi-search ---' && rg -n --glob '*.py' '(logging|logger|log)\.info\(' --glob '!growi-search/**' . || true && printf '%s\n' '--- logging-only diff ---' && git diff --unified=0 -- main.py publisher/pipeline.py publisher/human_changes.py graph/wiki/legacy.py jev/engine.py jev/backends/torch.py | rg '^(diff --git|\+.*(log\.debug|logging\.getLogger|basicConfig|logging\.error)|-.*(log\.(info|warning|error)|print\())'
    --- non-debug INFO outside growi-search ---
    --- logging-only diff ---
    diff --git a/graph/wiki/legacy.py b/graph/wiki/legacy.py
    +log = logging.getLogger(__name__)
    -                    print(
    +                    log.debug(
    -        print(
    +        log.debug(
    -        print(
    +        log.debug(
    -                print(
    +                log.debug(
    -        print(
    +        log.debug(
    -        print(
    +        log.debug(
    -            print(
    +            log.debug(
    -    print(f"[Enrichment] Inferring global name...")
    +    log.debug("[Enrichment] Inferring global name...")
    -        print(f"[Enrichment] Failed to infer global name: {e}. Using fallback.")
    +        log.debug(f"[Enrichment] Failed to infer global name: {e}. Using fallback.")
    -    print(f"[Enrichment] Inferring header for chunk 1/{len(files)}...")
    +    log.debug(f"[Enrichment] Inferring header for chunk 1/{len(files)}...")
    -        print(f"[Enrichment] Failed to infer header for chunk 1: {e}. Using fallback.")
    +        log.debug(f"[Enrichment] Failed to infer header for chunk 1: {e}. Using fallback.")
    -        print(f"[Enrichment] Inferring header for chunk {i+1}/{len(files)}...")
    +        log.debug(f"[Enrichment] Inferring header for chunk {i+1}/{len(files)}...")
    -            print(
    +            log.debug(
    diff --git a/jev/backends/torch.py b/jev/backends/torch.py
    -        log.info("Jev fast kernels available: %s", backend.stats["fast_kernels"])
    +        log.debug("Jev fast kernels available: %s", backend.stats["fast_kernels"])
    diff --git a/jev/engine.py b/jev/engine.py
    -            log.info("Jev stats: %s", stats_summary(self._stats, time.monotonic() - self._started))
    +            log.debug("Jev stats: %s", stats_summary(self._stats, time.monotonic() - self._started))
    diff --git a/main.py b/main.py
    +log = logging.getLogger(__name__)
    -    print(f"[{stage}] {step}{progress} {details}".rstrip(), flush=True)
    +    log.debug(f"[{stage}] {step}{progress} {details}".rstrip())
    +    logging.basicConfig(
    +            logging.getLogger(noisy).setLevel(logging.INFO)
    diff --git a/publisher/human_changes.py b/publisher/human_changes.py
    -            log.info("human_sync event=adopt_exact_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
    +            log.debug("human_sync event=adopt_exact_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
    -        log.info("human_sync event=rebase_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
    +        log.debug("human_sync event=rebase_prepared_write page=%s revision=%s", page.page_id, page.revision_id)
    -            log.info("human_sync event=capture page=%s operations=%d revision=%s", local_path, count, row["observed_revision_id"])
    +            log.debug("human_sync event=capture page=%s operations=%d revision=%s", local_path, count, row["observed_revision_id"])
    -        log.info("human_sync event=render source=%s changed=%d conflict=%d orphaned=%d",
    +        log.debug("human_sync event=render source=%s changed=%d conflict=%d orphaned=%d",
    diff --git a/publisher/pipeline.py b/publisher/pipeline.py
    -            log.info("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
    +            log.debug("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
    -                log.info("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    +                log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    -                log.info("run=%s path=%s stage=parse start", run_id, rel)
    +                log.debug("run=%s path=%s stage=parse start", run_id, rel)
    -                        log.info("run=%s path=%s stage=parse resumed", run_id, rel)
    +                        log.debug("run=%s path=%s stage=parse resumed", run_id, rel)
    -                log.info("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
    +                log.debug("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
    -                log.info("human_sync event=recover_exact_prepared page=%s revision=%s", page.page_id, page.revision_id)
    +                log.debug("human_sync event=recover_exact_prepared page=%s revision=%s", page.page_id, page.revision_id)
    -                log.info("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    +                log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    ✓ • 101ms

## Activity

    $ python main.py -h >/tmp/llm-wiki-main-help.txt && sed -n '1,8p' /tmp/llm-wiki-main-help.txt && git status --short && git diff --stat -- main.py publisher/pipeline.py publisher/human_changes.py graph/wiki/legacy.py jev/engine.py jev/backends/torch.py
    usage: python main.py [-h] [--project PROJECT] [--data-root DATA_ROOT] [-v]
                          {check,convert,sync,watch,queue,build,wiki,publish,pull,human,index,reset,link} ...
    
    One entry point for the publisher. `python main.py -h`.
    
    check                       ping chat/embed/parser/GROWI endpoints + the local Jev backend
    convert                     external mount -> raw Markdown only
    build wiki [<raw-rel>...]   raw/ -> wiki pages only
     M .gitignore
     M README.md
     M graph/config.py
     M graph/growi/client.py
     M graph/wiki/legacy.py
     M jev/backends/torch.py
     M jev/engine.py
     M main.py
     M publisher/human_changes.py
     M publisher/pipeline.py
     M tests/test_human_changes.py
    ?? "configs/diff_test copy.ini"
    ?? docs/human-semantic-calibration.json
    ?? publisher/activity.py
    ?? publisher/human_semantic.py
    ?? publisher/legacy_recovery.py
    ?? publisher/live_verification.py
    ?? publisher/prompts/
    ?? tests/test_human_sync_rollout.py
     graph/wiki/legacy.py       |  29 ++--
     jev/backends/torch.py      |   2 +-
     jev/engine.py              |   2 +-
     main.py                    |  88 ++++++++++-
     publisher/human_changes.py | 387 ++++++++++++++++++++++++++++++++++++++++++---
     publisher/pipeline.py      | 182 +++++++++++++++++++--
     6 files changed, 637 insertions(+), 53 deletions(-)
    ✓ • 157ms

## Assistant

Completed the DEBUG-only normalization for routine sync-path logs.

Changed/verified:

- `graph/wiki/legacy.py`: bare diagnostic prints → `log.debug`
- `publisher/pipeline.py`: routine `INFO` logs → `DEBUG`
- `publisher/human_changes.py`: human-sync `INFO` logs → `DEBUG`
- `jev/engine.py` and `jev/backends/torch.py`: remaining sync-related `INFO` logs → `DEBUG`
- `main.py`: progress output uses DEBUG logging; `-v` enables DEBUG

Warnings/errors and machine-readable CLI output remain unchanged.

Validation passed:

- `py_compile`
- `git diff --check`
- `python main.py -h`

No unrelated worktree changes were modified.

## User

Now, i want to add normal print statements, which show: A TQDM of how many documents processed. 1 tqdm of progress of current stage, after the stage is done, remove that tqdm and print how much time it took for that stage for that doc. Make sure the tqdm logs are not accumulated because of these prints. Dont make any change yet, see how would make these changes and where you would add these prints. My image for current pipeline is 1. parser stage 2. planner (for making chunks) 3. wiki reserach 5. wiki writer + judge 6. linker (entity maker) 7. linker main 8. publish. Do you have better split for phase wise? i wanna see which phase is taking longest

## Assistant

I’ll map the existing progress events and stage boundaries to your eight phases, then propose the smallest instrumentation design and exact insertion points. I won’t edit files.

## Activity

    $ sed -n '150,225p' main.py; sed -n '820,1015p' publisher/pipeline.py; sed -n '520,620p' publisher/pipeline.py; sed -n '800,880p' publisher/queue.py; sed -n '1010,1070p' publisher/queue.py; sed -n '430,585p' publisher/index.py
            "growi": f"{growi}/_api/v3/healthcheck" if growi else "",
        }
        bad = 0
        for name, url in targets.items():
            if not url:
                print(f"{name:7} skipped (not configured)")
                continue
            try:
                response = requests.get(url, timeout=5)
                print(f"{name:7} {response.status_code} {url}")
                bad += response.status_code >= 400
            except Exception as exc:
                print(f"{name:7} DOWN {url} ({type(exc).__name__})")
                bad += 1
        jev_line, jev_bad = _jev_check(settings)
        print(jev_line)
        bad += jev_bad
        project = open_project(settings)
        print(f"data    {project.root.resolve()}  mount={project.mount} ingest={settings.ingest_mode} linker={'off' if not settings.wiki_linker_enabled else settings.wiki_linker_mode}")
        return 1 if bad else 0
    
    
    def cmd_sync(args: argparse.Namespace) -> int:
        from publisher import pipeline
        from publisher.queue import retry_failed, scan, work_once, worker_lock
    
        settings = _settings(args)
        project = open_project(settings)
        combined: dict[str, Any] = {"done": [], "failures": []}
        stale = pipeline.republish_if_stale(settings)
        if stale is not None:
            print(f"growi endpoint or markers changed: full republish {stale['run_id']}", flush=True)
            combined["failures"].extend(stale["failures"])
        with worker_lock(project):
            retry_failed(project)
            first = True
            resume = bool(getattr(args, "continue_run", False))
            while True:
                scan(
                    settings,
                    only=args.items or None,
                    settle_seconds=0,
                    force=args.force and first,
                    verify_content=True,
                )
                first = False
                result = work_once(settings, on_event=_progress if args.verbose else None, continue_run=resume)
                # Only the first batch can resume a kept worktree; later batches
                # in the same process are always fresh.
                resume = False
                if result is None:
                    break
                combined["done"].extend(result.get("done", []))
                combined["failures"].extend(result.get("failures", []))
                if result.get("failures"):
                    break
        if not combined["failures"]:
            # Index pages are derived output, so reconcile them against the whole wiki tree
            # here: a wiki built before its index exists catches up, and a page whose body
            # already matches GROWI is read but never rewritten.
            from publisher.index import build_index
    
            try:
                index = build_index(settings, on_progress=_progress if args.verbose else None)
            except Exception as exc:  # a stale table of contents must not fail a sync
                index = {"done": [], "failures": [f"index: {type(exc).__name__}: {exc}"]}
            combined["done"].extend(index["done"])
            combined["failures"].extend(index["failures"])
        return _report(combined)
    
    
    def cmd_pull(args: argparse.Namespace) -> int:
        from publisher.pipeline import pull_growi_once
    
        settings = _settings(args)
        result = (
                for rel in deleted
            })
            done.extend(removed)
            touched_raw.update(touched)
            failures.extend(remove_failures)
            if deleted:
                save_ledger(ledger_path, ledger)
            changed = set(scan.added) | set(scan.changed)
            if wanted is not None:
                changed &= wanted
                if force:
                    changed |= wanted & set(scan.files)
            changed = sorted(changed)
            incremental_pages: set[str] = set(captured_pages)
            regenerated_pages: set[str] = set()
            incremental_publish = bool(changed) and not deleted
            scoped_candidates = None if include_pending else (sorted(scoped_raw) if wanted is not None else None)
            pending_before = _pending_link_rels(project, settings, scoped_candidates)
            model = _model(settings, project) if changed or pending_before else None
            try:
                embedder = Embedder(settings) if changed or pending_before else None
            except Exception as exc:
                if (str(getattr(settings, "embed_backend", "server")) == "off"
                        and isinstance(exc, ValueError)
                        and str(exc) == "the linker requires WIKI_EMBED_BACKEND=server"):
                    log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
                else:
                    log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
                embedder = None
            source_details = source_details or {}
            retried: set[str] = set()
            for rel in changed:  # grows: a failed document is retried once at the end
                if should_continue is not None and not should_continue():
                    cancelled = True
                    break
                item = scan.files[rel]
                raw_rel = _raw_rel(item)
                details = source_details.get(rel, {})
                previous_source = dict(ledger.sources.get(rel) or {})
                classification = str(details.get("classification") or "none")
                requested_resume = not force if resume is None else resume
                if classification == "forced":
                    requested_resume = False
                started = time.monotonic()
                try:
                    log.debug("run=%s path=%s stage=parse start", run_id, rel)
                    if on_progress:
                        on_progress({
                            "stage": "parse",
                            "step": "start",
                            "file": rel,
                            "parser": item.parser,
                            "bytes": item.size,
                        })
                    with _progress_heartbeat(on_progress, stage="parse", file=rel):
                        raw_path = project.raw_file(raw_rel)
                        previous_markdown = (
                            raw_path.read_text(encoding="utf-8")
                            if (previous_source or details.get("source_sha256")) and raw_path.exists()
                            else None
                        )
                        if _can_resume_parsed_source(
                            previous_source,
                            item,
                            raw_path,
                            requested_resume=requested_resume,
                            classification=classification,
                            known_source_sha256=str(details.get("source_sha256") or ""),
                            known_source_blob_oid=str(details.get("source_blob_oid") or ""),
                        ):
                            assert previous_markdown is not None
                            markdown = previous_markdown
                            log.debug("run=%s path=%s stage=parse resumed", run_id, rel)
                            if on_progress:
                                on_progress({"stage": "parse", "step": "resumed", "file": rel})
                        else:
                            parse_args = (
                                {"previous_markdown": previous_markdown}
                                if previous_markdown is not None
                                else {}
                            )
                            markdown = _parse(item, project.mount / rel, settings, **parse_args)
                    _assert_source_unchanged(item, project.mount / rel)
                    if previous_markdown is not None and classification != "forced":
                        _check_parse_size(rel, previous_markdown, markdown)
                    if on_progress:
                        on_progress({
                            "stage": "parse",
                            "step": "done",
                            "file": rel,
                            "characters": len(markdown),
                            "elapsed_seconds": round(time.monotonic() - started, 1),
                        })
                    _write_raw(project.raw_file(raw_rel), markdown)
                    wiki_started = time.monotonic()
                    if on_progress:
                        on_progress({"stage": "wiki", "step": "start", "file": raw_rel})
                    with _progress_heartbeat(on_progress, stage="wiki", file=raw_rel):
                        result = write_wiki_pages(
                            project, raw_rel, mode=str(settings.ingest_mode), settings=settings,
                            llm=model, embedder=embedder, on_progress=on_progress,
                            resume=requested_resume,
                            stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                            identity_seed=str(
                                previous_source.get("id_seed")
                                or ((Path(raw_rel).parent / wiki_folder_name(Path(raw_rel).name)).as_posix() if previous_source else "")
                                or details.get("source_id")
                                or raw_rel
                            ),
                        )
                    if getattr(result, "rebuild", "full") == "incremental":
                        incremental_pages.update(getattr(result, "changed_pages", []))
                        regenerated_pages.update(getattr(result, "regenerated_pages", []))
                    else:
                        incremental_publish = False
                    if on_progress:
                        on_progress({
                            "stage": "wiki",
                            "step": "done",
                            "file": raw_rel,
                            "touched_documents": len(result.touched),
                            "elapsed_seconds": round(time.monotonic() - wiki_started, 1),
                        })
                    ledger.sources[rel] = _source_row(item, raw_rel, details=details, previous=previous_source)
                    _store_source(project, item, ledger.sources[rel], project.mount / rel)
                    save_ledger(ledger_path, ledger)
                    done.append({
                        "path": rel,
                        "status": "changed" if previous_source else "added",
                        "touched": result.touched,
                        "rebuild": "incremental" if getattr(result, "rebuild", "full") == "incremental" else "full",
                        "tier": getattr(result, "tier", 3),
                        "reason": getattr(result, "reason", ""),
                        "human_edits_overwritten": list(getattr(result, "human_edits_overwritten", [])),
                    })
                    log.debug("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
                except asyncio.CancelledError:
                    cancelled = True
                    break
                except Exception as exc:
                    if should_continue is not None and not should_continue():
                        cancelled = True
                        break
                    if rel not in retried:
                        retried.add(rel)
                        changed.append(rel)
                        log.warning("run=%s path=%s stage=generate retry_later error=%s: %s", run_id, rel, type(exc).__name__, exc)
                        continue
                    # Second failure: skip until the next sync.  Keep the source
                    # digest in the error row so an unchanged retry can skip parse.
                    error = f"{type(exc).__name__}: {exc}"[:500]
                    ledger.sources[rel] = _source_row(item, raw_rel, error, details=details, previous=previous_source)
                    save_ledger(ledger_path, ledger)
                    failures.append(f"{rel}: {error}")
                    log.error("run=%s path=%s stage=generate error=%s: %s", run_id, rel, type(exc).__name__, exc)
            pending_links = _pending_link_rels(project, settings, scoped_candidates)
            if should_continue is not None and not should_continue():
                cancelled = True
            if pending_links and not failures and not cancelled:
                try:
                    if on_progress:
                        on_progress({
                            "stage": "linker",
                            "step": "batch_start",
                            "current": 0,
                            "total": len(pending_links),
                            "documents": len(pending_links),
                        })
                    with _progress_heartbeat(on_progress, stage="linker", file="batch"):
                        touched = run_linkers(
                            project, pending_links, settings=settings, llm=model, embedder=embedder,
                            on_progress=on_progress,
                            stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                            affected_pages=incremental_pages if incremental_publish else None,
                            regenerated_pages=regenerated_pages if incremental_publish else None,
                        )
                    touched_raw.update(touched)
                    done.append({"path": "*", "status": "linked", "documents": len(pending_links), "touched": touched})
                    if on_progress:
                        on_progress({
                            "stage": "linker",
                            "step": "batch_done",
                            "current": len(pending_links),
                            "total": len(pending_links),
                            "touched": len(touched),
                        })
                except asyncio.CancelledError:
                    cancelled = True
                except Exception as exc:
                    if should_continue is not None and not should_continue():
                        cancelled = True
                    else:
                        failures.append(f"link: {type(exc).__name__}: {exc}")
            if should_continue is not None and not should_continue():
                cancelled = True
            publish_only = scoped_raw | set(pending_links) | touched_raw if wanted is not None else None
    
    def _publish_sweep(
        project: Project,
        ledger: Ledger,
        publisher: GrowiPublisher | None,
        run_id: str,
        *,
        only: set[str] | None = None,
        only_pages: set[str] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        begin_publish: Callable[[], None] | None = None,
        settings: Any | None = None,
        captured: bool = False,
    ) -> list[str]:
        from .index import build_index, delete_document_index  # index imports this module
    
        failures: list[str] = []
        if settings is not None and not getattr(settings, "wiki_linker_enabled", True):
            rels = [rel for rel in _wiki_raw_rels(project) if only is None or rel in only]
            run_linkers(project, rels, settings=settings, llm=None, embedder=None)
        folders = _folders(project)
        if publisher is not None:
            _assert_unique_growi_locations(project, set(folders), publisher)
        scoped_documents = (
            {project.wiki_dir(rel).relative_to(project.wiki).as_posix() for rel in only}
            if only is not None
            else None
        )
        if scoped_documents is not None:
            folders = {document: folder for document, folder in folders.items() if document in scoped_documents}
        blocked: set[str] = set()
        if publisher is not None:
            try:
                if not captured:
                    pulled, conflicts, blocked = _capture_remote(project, ledger, publisher, only=only)
                    failures.extend(conflicts)
                    if pulled:
                        if only_pages is not None:
                            only_pages = set(only_pages) | set(pulled)
                    if not conflicts and settings is not None:
                        pending = _pending_link_rels(project, settings, sorted(only) if only is not None else None)
                        if pending:
                            run_linkers(project, pending, settings=settings, llm=None, embedder=None, on_progress=on_progress)
                    folders = _folders(project)
                    if scoped_documents is not None:
                        folders = {doc: folder for doc, folder in folders.items() if doc in scoped_documents}
                known_pages = dict(ledger.published_pages)
                if hasattr(publisher, "assert_known_revisions"):
                    publisher.assert_known_revisions({path: row for path, row in known_pages.items()
                                                     if Path(path).parent.as_posix() in folders})
            except Exception as exc:
                blocked = set(folders)
                failures.append(f"pull: {type(exc).__name__}: {exc}")
        documents = [
            (document, folder, _document_raw_rel(document))
            for document, folder in sorted(folders.items())
            if document not in blocked
        ]
        not_ready = {project.wiki_dir(rel).relative_to(project.wiki).as_posix()
                     for rel in _wiki_raw_rels(project)
                     if project.wiki_dir(rel).is_dir() and (only is None or rel in only)} - set(folders) - blocked
        failures.extend(f"{document}: linker output is not ready" for document in sorted(not_ready))
        if publisher is not None:
            started = time.monotonic()
            try:
                if documents and begin_publish is not None:
                    begin_publish()
                if on_progress:
                    on_progress({
                        "stage": "growi-publish",
                        "step": "start",
                        "current": 0,
                        "total": len(documents),
                        "documents": len(documents),
                    })
                publish_args = {
                    "known_pages": known_pages,
                    **({"only_pages": only_pages} if only_pages is not None else {}),
                }
                pages = publisher.publish_documents(
                    project, [raw_rel for _, _, raw_rel in documents], **publish_args
                )
                published_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                for document, folder, raw_rel in documents:
                    ledger.published_documents[document] = {
                        "content_sha256": _content_hash(folder),
                        "growi_path": publisher.doc_path(project, raw_rel),
                        "raw_rel": raw_rel,
                        "published_at": published_at,
                    }
                updated_pages = {path: _published_page_row(project, publisher, path, page) for path, page in pages.items()}
                if only_pages is None:
                    published_prefixes = tuple(document.rstrip("/") + "/" for document, _, _ in documents)
                    ledger.published_pages = {
                        path: row for path, row in ledger.published_pages.items()
                        if not path.startswith(published_prefixes)
                    }
                ledger.published_pages.update(updated_pages)
                log.debug("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
                if on_progress:
                    on_progress({
    
    def _transaction(project: Project, operation_id: str, base: str, phase: str, commit: str = "") -> None:
        with _connect(project) as conn:
            conn.execute(
                """INSERT INTO transactions(operation_id,candidate_commit,base_commit,phase,started_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET
                   candidate_commit=excluded.candidate_commit,phase=excluded.phase""",
                (operation_id, commit, base, phase, time.time()),
            )
    
    
    def _record_revision(project: Project, operation_id: str, page: Any) -> None:
        if not getattr(page, "page_id", "") or not getattr(page, "revision_id", ""):
            return
        with _connect(project) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO transaction_revisions(operation_id,page_id,revision_id) VALUES(?,?,?)",
                (operation_id, str(page.page_id), str(page.revision_id)),
            )
    
    
    def _transaction_revisions(project: Project, operation_id: str) -> dict[str, set[str]]:
        with _connect(project) as conn:
            rows = conn.execute(
                "SELECT page_id,revision_id FROM transaction_revisions WHERE operation_id=?", (operation_id,)
            )
            revisions: dict[str, set[str]] = {}
            for row in rows:
                revisions.setdefault(str(row["page_id"]), set()).add(str(row["revision_id"]))
            return revisions
    
    
    def _finish_transaction(project: Project, operation_id: str) -> None:
        with _connect(project) as conn:
            conn.execute("DELETE FROM transaction_revisions WHERE operation_id=?", (operation_id,))
            conn.execute("DELETE FROM transactions WHERE operation_id=?", (operation_id,))
    
    
    def _work_once_locked(settings: Any, *, on_event: Any = None, continue_run: bool = False) -> dict[str, Any] | None:
        """Run one immutable candidate from last-good, then publish and promote it.
    
        With ``continue_run`` (``--continue``) an interrupted building/prepared
        candidate is reused with its LLM checkpoints intact; otherwise leftover
        worktrees are discarded up front.  Deletion is deferred: the worker keeps
        its worktree on exit (``keep=True``) and the next startup decides.
        """
        from .pipeline import delete_sources, move_sources, restore_publication, sync_once
    
        project = open_project(settings)
        ensure_repository(project)
        resumable_id: str | None = None
        resumable_commit = ""
        if continue_run:
            resumable = _resumable_transaction(project)
            if resumable is None and _adopt_resumable_orphan(project) is not None:
                resumable = _resumable_transaction(project)
            if resumable is not None:
                resumable_id = str(resumable["operation_id"])
                resumable_commit = str(resumable["candidate_commit"] or resumable["base_commit"])
            _prune_orphan_candidates(project, keep=resumable_id)
        else:
            prune_candidates(project)
        with _connect(project) as conn:
            unfinished = conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone() is not None
        if unfinished:
            recover(project, settings, preserve_operation_id=resumable_id)
            if resumable_id is not None:
                refreshed = _resumable_transaction(project)
                if refreshed is None or str(refreshed["operation_id"]) != resumable_id:
                    resumable_id = None
                    resumable_commit = ""
                else:
                    resumable_commit = str(refreshed["candidate_commit"] or refreshed["base_commit"])
        jobs = claim(project, "fast")
        if not jobs:
            with _connect(project) as conn:
                if conn.execute("SELECT 1 FROM jobs WHERE lane='fast' AND status='failed' LIMIT 1").fetchone():
                    return None
            jobs = claim(project, "slow")
        if not jobs:
            return None
            finish(project, jobs, error="superseded by a newer mount event", retry=True)
        else:
            completed = {
                str(row.get("path")) for row in result.get("done", [])
                if row.get("status") in {"added", "changed", "deleted", "moved"}
            }
            omitted = sorted(job.rel for job in jobs if job.rel not in completed)
            if omitted:
                result.setdefault("failures", []).append(f"pipeline omitted claimed paths: {omitted}")
        if result.get("cancelled"):
            pass
        elif result.get("failures"):
            finish(project, jobs, error="; ".join(result["failures"]))
        else:
            finish(project, jobs)
        _finish_transaction(project, operation_id)
        result.pop("scan", None)
        return {"lane": jobs[0].lane, "jobs": len(jobs), "paths": [job.rel for job in jobs], **result}
    
    
    def work_once(settings: Any, *, on_event: Any = None, continue_run: bool = False) -> dict[str, Any] | None:
        """Promote one candidate, then reconcile derived indexes from live state."""
        from .pipeline import _lock
    
        with _lock(open_project(settings)):
            result = _work_once_locked(settings, on_event=on_event, continue_run=continue_run)
            if result is None or result.get("failures") or result.get("cancelled"):
                return result
    
            # metadata/index is deliberately derived and is not part of the Git commit.
            # GROWI indexes were reconciled in the candidate publication; reconcile them
            # idempotently from promoted state as well, while materializing the matching
            # local copies before another publisher can change the live tree.
            try:
                from .index import build_index
    
                index = build_index(
                    settings,
                    only=result.get("index_paths"),
                    on_progress=on_event,
                    locked=True,
                )
                result["index"] = {
                    "updated": len(index.get("done", [])),
                    "failures": list(index.get("failures", [])),
                }
            except Exception as exc:
                result["index"] = {
                    "updated": 0,
                    "failures": [f"index: {type(exc).__name__}: {exc}"],
                }
            return result
    
    
    @contextmanager
    def worker_lock(project: Project):
        import fcntl
    
        path = project.metadata / "watch-worker.lock"
        with path.open("a+") as handle:
            try:
    
    
    def build_index(settings: Any, *, only: list[str] | None = None, publish: bool = True,
                    on_progress: Callable[[dict[str, Any]], None] | None = None,
                    locked: bool = False, ledger: Ledger | None = None) -> dict[str, Any]:
        """Refresh document indexes and the containing folder index tree.
    
        ``only`` scopes document writes and their ancestor folder writes.
        ``locked`` and ``ledger`` let a publish sweep call this while it already holds the
        project lock, linking against the page IDs that sweep has just published.
        """
        project = open_project(settings)
        if not locked:
            with _lock(project):
                return build_index(
                    settings,
                    only=only,
                    publish=publish,
                    on_progress=on_progress,
                    locked=True,
                    ledger=ledger,
                )
        connection = _connection(settings)
        publisher = _publisher(settings) if publish else None
        if publish and publisher is None:
            raise RuntimeError("GROWI_URL is required for index (use --no-publish to only write metadata/index/)")
        run_id = "idx-" + uuid.uuid4().hex[:16]
        done: list[dict[str, Any]] = []
        failures: list[str] = []
        folders = _folders(project)
        paths = list(folders)
        tree = folder_tree(paths)
        if connection is not None:
            remote_owners: dict[str, str] = {}
            for rel in set(paths) | set(tree):
                remote_path = _index_link(connection, rel)
                previous = remote_owners.get(remote_path)
                if previous is not None and previous != rel:
                    raise ValueError(
                        f"index locations resolve to the same GROWI path {remote_path!r}: "
                        f"{previous!r}, {rel!r}"
                    )
                remote_owners[remote_path] = rel
        cards_by_document = {doc: document_cards(path) for doc, path in folders.items()}
        summaries = {doc: document_summary(doc, cards_by_document[doc]) for doc in folders}
        related = _related_documents(settings, folders, summaries, connection)
        index_root = project.metadata / "index"
        scoped = set(paths) if only is None else {
            project.wiki_dir(rel.strip().lstrip("/")).relative_to(project.wiki).as_posix() for rel in only
        }
        affected = set(tree) if only is None else {""}
        for document in scoped:
            parts = Path(document).parts
            affected.update("/".join(parts[:i]) for i in range(1, len(parts)))
        # Include the scoped leaf even when it no longer exists. That is what lets a
        # delete or move remove its old document index, not only refresh its parents.
        affected.update(scoped)
        # Keep a collision document fresh when its child-folder listing changes.
        doc_scope = scoped | (set(summaries) & affected)
        if getattr(settings, "wiki_linker_judge", "llm") == "jev" and getattr(settings, "wiki_index_related_docs", False):
            doc_scope = set(paths)
        total = len(doc_scope) + len(affected)
        indexed = 0
        with contextlib.nullcontext() if locked else _lock(project):
            ledger = ledger if ledger is not None else load_ledger(project.metadata / "pipeline.json")
            target = str(settings.target_name).strip("/")
            # Every block is computed (cheap, local files only) so unchanged siblings keep their hashes.
            blocks = data_blocks(tree, summaries, cards_by_document, connection,
                                 lambda doc, filename: ledger.published_pages.get(f"{doc}/{filename}", {}), target)
            for document, folder in sorted(folders.items()):
                doc_path = growi_path(connection.write_path, document) if connection else f"/{document}"
                cards = cards_by_document[document]
                if document not in doc_scope:
                    continue
    
                def link_for(filename: str, _doc=document, _doc_path=doc_path) -> str:
                    row = ledger.published_pages.get(f"{_doc}/{filename}", {})
                    return f"/{row['page_id']}" if row.get("page_id") else growi_path(_doc_path, filename)
    
                body = render_document_index(Path(document).name, cards, link_for, related.get(document, []))
                body += _subfolder_section(document, tree, summaries, connection)
                body += "\n" + blocks[document]
                write_text_atomic(project.metadata / "index" / document / "index.md", body)
                status = "written"
                if publisher is not None:
                    try:
                        page, changed = asyncio.run(_upsert(publisher.client, _index_link(connection, document), body, mode=connection.mode,
                                                            write_path=connection.write_path, root_path=connection.root_path))
                        status = "indexed" if changed else "unchanged"
                    except Exception as exc:  # one document must not stop the others
                        failures.append(f"{document}: {type(exc).__name__}: {exc}")
                        continue
                indexed += 1
                done.append({"document": document, "pages": len(cards), "status": status})
                if on_progress:
                    on_progress({"stage": "index", "step": "document", "current": indexed, "total": total, "document": document})
            for folder in sorted(affected - set(summaries), key=str):
                index_path = index_root / (folder if folder else "") / "index.md"
                if folder not in tree:
                    _delete_local_index(index_path, index_root)
                    status = "deleted"
                    if publisher is not None:
                        try:
                            if asyncio.run(_delete_if_index(publisher.client, _index_link(connection, folder))):
                                status = "deleted"
                        except Exception as exc:
                            failures.append(f"folder {folder}: {type(exc).__name__}: {exc}")
                    done.append({"folder": folder, "status": status})
                    continue
                if folder and not tree[folder].documents and not tree[folder].folders:
                    _delete_local_index(index_path, index_root)
                    status = "deleted"
                    if publisher is not None:
                        try:
                            asyncio.run(_delete_if_index(publisher.client, _index_link(connection, folder)))
                        except Exception as exc:
                            failures.append(f"folder {folder}: {type(exc).__name__}: {exc}")
                    done.append({"folder": folder, "status": status})
                    continue
                title = target if not folder else Path(folder).name
                kind = "root" if not folder else "folder"
                body = render_folder_index(title, kind,
                                           _folder_cards(folder, tree, summaries, connection, root=not folder))
                body += "\n" + blocks[folder]
                write_text_atomic(index_path, body)
                status = "written"
                try:
                    if publisher is not None:
                        _, changed = asyncio.run(_upsert(
                            publisher.client, _index_link(connection, folder), body,
                            mode=connection.mode, write_path=connection.write_path, root_path=connection.root_path))
                        status = "indexed" if changed else "unchanged"
                except Exception as exc:
                    failures.append(f"folder {folder}: {type(exc).__name__}: {exc}")
                done.append({"folder": folder, "status": status})
                indexed += 1
                if on_progress:
                    on_progress({"stage": "index", "step": "folder", "current": indexed, "total": total, "folder": folder})
            if only is None:
                expected_rels = set(summaries) | set(tree)
                expected_local = {
                    (index_root / (rel if rel else "") / "index.md").resolve(strict=False)
                    for rel in expected_rels
                }
                removed_local = []
                for stale in sorted(index_root.rglob("index.md")) if index_root.exists() else ():
                    if stale.resolve(strict=False) in expected_local:
                        continue
                    if _delete_local_index(stale, index_root):
                        removed_local.append(stale.relative_to(index_root).as_posix())
                if removed_local:
                    done.append({"stale_local_indexes": removed_local, "status": "deleted"})
                if publisher is not None and hasattr(publisher.client, "list_all_pages"):
                    expected_remote = {_index_link(connection, rel) for rel in expected_rels}
                    try:
                        removed_remote = asyncio.run(_delete_stale_indexes(
    ✓ • 0ms

## Activity

    $ sed -n '1400,1645p' graph/wiki/pipeline.py; sed -n '230,290p' graph/linker/service.py; sed -n '400,535p' graph/linker/service.py; sed -n '720,860p' graph/linker/service.py; sed -n '500,660p' graph/formats/tabular.py; sed -n '160,285p' graph/formats/xlsx.py
                    "verbatim_sections": by_number[page.number].verbatim_sections,
                }
                for page in pages
            ],
        }
    
    
    async def run_pipeline(
        source_path: Path | str,
        *,
        config: WikiConfig | None = None,
        model: ModelPort | None = None,
        on_progress: Progress = None,
        stop_check: StopCheck = None,
    ) -> Path:
        """Build seed pages, then independently plan and rewrite each page."""
    
        source_path = Path(source_path).resolve()
        source_bytes = source_path.read_bytes()
        source_text = source_bytes.decode("utf-8")
        normalized = normalize_source(source_text)
        lines = split_source_lines(normalized)
        config = config or WikiConfig()
        model = model or ChatModelPort(config)
        slug = slugify(config.document_slug or source_path.stem, fallback="document").casefold()
        run_root = (
            Path(config.run_dir).resolve()
            if config.run_dir
            else Path(config.output_root) / f"{slug}-{sha256_text(source_text)[:12]}"
        )
        source_root = run_root / "source"
        state_root = run_root / "state"
        work_root = run_root / "work"
        wiki_root = run_root / "wiki"
        for directory in (source_root, state_root, work_root):
            directory.mkdir(parents=True, exist_ok=True)
        wiki_root.mkdir(parents=True, exist_ok=True)
        source_snapshot_path = (source_root / "original.md").resolve()
        write_text_atomic(source_snapshot_path, source_text)
    
        source_hash = sha256_text(source_text)
        plan_path = state_root / "plan.json"
        pages = _load_seed_plan(
            plan_path,
            source_sha256=source_hash,
            source_line_count=len(lines),
            prompt_version=SEED_PLAN_VERSION,
        ) if config.resume else None
        if pages is None and config.require_resume:
            raise ResumeUnavailable(f"stored seed plan for {source_path.name} could not be resumed")
        resumed_seed_plan = pages is not None
        if pages is not None:
            _emit(on_progress, "seed", "resumed", pages=len(pages), source_lines=len(lines))
        else:
            _emit(on_progress, "seed", "start", source_lines=len(lines))
            if wiki_root.exists():
                shutil.rmtree(wiki_root)
            wiki_root.mkdir(parents=True, exist_ok=True)
            from ..formats import structural_seed_plan
            from .document_map import validate_seed_plan
    
            structural = await structural_seed_plan(
                lines,
                kind=config.source_kind,
                config=config,
                model=model,
                on_progress=on_progress,
                stop_check=stop_check,
            )
            seed_plan = None
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
                observations = await observe_document(
                    source_text,
                    model=model,
                    config=config,
                    document=document_id(normalized),
                    checkpoint_dir=work_root / "observations" / "checkpoints",
                    live_output_dir=work_root / "observations" / "live",
                    on_progress=on_progress,
                    stop_check=stop_check,
                )
                seed_plan = await build_seed_plan(
                    observations,
                    lines=lines,
                    model=model,
                    config=config,
                    checkpoint_dir=work_root / "planning",
                    stop_check=stop_check,
                    on_progress=on_progress,
                )
            _emit(on_progress, "seed", "structural" if structural is not None and seed_plan is not None else "llm", pages=len(seed_plan.pages))
            pages = _plan_pages(seed_plan)
            _verify_ranges(pages, len(lines))
    
        from ..formats.context import summarize_hierarchy
    
        parents: dict[str, str] = {}
        if any(page.path for page in pages):
            _emit(on_progress, "context", "start", parents=len({tuple(page.path[:-1]) for page in pages}))
            parents = await summarize_hierarchy(
                pages,
                lines,
                model=model,
                config=config,
                checkpoint=state_root / "context.json",
                stop_check=stop_check,
            )
            _emit(on_progress, "context", "done")
        units = extract_image_units(lines) + block_units(lines)
        seed_root = (work_root / "seeds").resolve()
        if not resumed_seed_plan:
            if seed_root.exists():
                shutil.rmtree(seed_root)
            page_state_root = state_root / "pages"
            if page_state_root.exists():
                shutil.rmtree(page_state_root)
            for obsolete in (
                work_root / "chunks",
                work_root / "map",
                source_root / "slices",
            ):
                if obsolete.exists():
                    shutil.rmtree(obsolete)
            for pattern in ("page-*", "research-*"):
                for old_attempt in work_root.glob(pattern):
                    if old_attempt.is_dir():
                        shutil.rmtree(old_attempt)
        _write_reference_seeds(pages, lines, units, seed_root)
        plan_json = {
            "source": str(source_path),
            "source_snapshot": str(source_snapshot_path),
            "source_sha256": sha256_text(source_text),
            "source_line_count": len(lines),
            "prompt_version": SEED_PLAN_VERSION,
            "pages": [
                {
                    "number": page.number,
                    "title": page.title,
                    "chapter": page.chapter,
                    "path": page.path,
                    "summary": page.summary,
                    "filename": page.filename,
                    "owner_ranges": _ranges_json(page.owner_ranges),
                    "reference_ranges": _ranges_json(page.reference_ranges),
                    "provenance": _page_provenance(
                        page,
                        pages,
                        source_path=source_path,
                        source_snapshot_path=source_snapshot_path,
                        source_sha256=source_hash,
                    ),
                }
                for page in pages
            ],
        }
        write_json_atomic(plan_path, plan_json)
        write_text_atomic(wiki_root / "index.md", _index_text(source_path.stem, pages))
        _emit(on_progress, "seed", "done", pages=len(pages), images=len(units))
    
        results = await _rewrite_all(
            pages,
            lines=lines,
            units=units,
            model=model,
            config=config,
            work_root=work_root,
            seed_root=seed_root,
            wiki_root=wiki_root,
            state_root=state_root,
            source_path=source_path,
            source_snapshot_path=source_snapshot_path,
            source_sha256=source_hash,
            source_line_count=len(lines),
            stop_check=stop_check,
            on_progress=on_progress,
            parents=parents,
        )
        for item, page in zip(plan_json["pages"], pages):
            item["reference_ranges"] = _ranges_json(page.reference_ranges)
            item["provenance"] = _page_provenance(
                page,
                pages,
                source_path=source_path,
                source_snapshot_path=source_snapshot_path,
                source_sha256=source_hash,
            )
        write_json_atomic(plan_path, plan_json)
        write_text_atomic(wiki_root / "index.md", _index_text(source_path.stem, pages))
        manifest = _manifest(
            source_path=source_path,
            source_snapshot_path=source_snapshot_path,
            source_text=source_text,
            pages=pages,
            results=results,
        )
        write_json_atomic(state_root / "manifest.json", manifest)
        (wiki_root / "manifest.json").unlink(missing_ok=True)
        flagged = [item for item in results if item.verbatim_sections]
        if flagged:
            review = [
                "# 人手による確認が必要です",
                "",
                "書き換え時に情報が欠落したため、次の節は原文のまま公開されています。"
                "内容を確認してください。",
                "",
            ]
            for item in flagged:
                review.append(f"- `{item.page.filename}`")
                review.extend(f"  - {note}" for note in item.verbatim_sections)
            write_text_atomic(wiki_root / "_review.md", "\n".join(review) + "\n")
        else:
            (wiki_root / "_review.md").unlink(missing_ok=True)
    
        write_json_atomic(
            state_root / "run.json",
            {
                "source_sha256": sha256_text(source_text),
                "source_line_count": len(lines),
                "pages": len(pages),
                "rewritten": len(pages),
                "review_pages": len(flagged),
                "verbatim_sections": sum(len(item.verbatim_sections) for item in results),
                "published": True,
            },
        )
        _emit(on_progress, "publish", "done", output=str(wiki_root), review_pages=len(flagged))
        return run_root
            for item in proposed:
                if item.get("placement") == "inline" and str(item.get("anchor", "")).strip() not in original:
                    item["placement"] = "footer"
                    item["anchor"] = ""
            return _valid_choices(proposed, pool, inline_limit=inline_limit, footer_limit=footer_limit), candidate_ids
        except Exception:
            # A transient curator failure must not erase good links already visible to readers.
            return (current or [{"edge_id": edge.edge_id, "placement": "footer", "anchor": "", "summary": edge.summary or edge.peer_summary} for edge in pool[:3]]), candidate_ids
    
    
    async def render_pages(
        project: Any, catalog: Catalog, pages: set[str], *, model: Any, settings: Any, mode: str,
        on_progress: Progress = None,
    ) -> set[str]:
        from .prompts import REFERENCE_PLAN_VERSION
    
        concurrency = _concurrency(settings)
        jev_engine = None
        if str(getattr(settings, "wiki_linker_judge", "llm")) == "jev":
            try:
                from jev import get_engine_for
                jev_engine = get_engine_for(settings)
            except Exception as exc:
                log.warning("Jev linker engine unavailable for page curation: %s", exc)
        semaphore = asyncio.Semaphore(concurrency)
        jobs: list[tuple[str, str, Path, str, list[RenderEdge], dict[str, Any], bool]] = []
        navigation_by_doc: dict[str, dict[str, Any]] = {}
        completed = 0
        for page_rel in sorted(pages):
            doc, filename = page_rel.rsplit("/", 1)
            original_path = Path(project.wiki) / doc / "_planning" / "pages" / filename
            if not original_path.exists():
                continue
            nav = navigation_by_doc.setdefault(doc, _navigation(project, doc))
            page_state = nav.setdefault("pages", {}).get(filename, {})
            edges = [_edge_from_row(row, page_rel) for row in catalog.edges_for_page(page_rel)]
            jobs.append((page_rel, doc, original_path, strip_reader_references(original_path.read_text(encoding="utf-8")), edges, page_state, _big_document(project, doc)))
    
        async def curate(job: tuple[str, str, Path, str, list[RenderEdge], dict[str, Any], bool]):
            nonlocal completed
            page_rel, _doc, _path, original, edges, state, big = job
            curation_edges = edges if mode == "legacy" else [edge for edge in edges if edge.source not in {"use", "define"}]
            async with semaphore:
                choices, candidate_ids = await _curate_page(
                    page_rel=page_rel, original=original, edges=curation_edges,
                    current=list(state.get("references", [])),
                    previous_candidates=list(state.get("candidate_ids", [])) if state.get("version") == REFERENCE_PLAN_VERSION else [],
                    model=model, settings=settings, big_document=big, mode=mode, jev_engine=jev_engine,
                )
            completed += 1
            if on_progress:
                on_progress({"stage": "linker", "step": "page_curated", "page": page_rel, "current": completed, "total": len(jobs)})
            return choices, candidate_ids
    
        results = await asyncio.gather(*(curate(job) for job in jobs))
        touched: set[str] = set()
        for job, (choices, candidate_ids) in zip(jobs, results):
            page_rel, doc, original_path, original, edges, _state, big = job
            navigation_by_doc[doc].setdefault("pages", {})[original_path.name] = {"version": REFERENCE_PLAN_VERSION, "candidate_ids": candidate_ids, "references": choices}
            rendered = render_page(original, page_rel=page_rel, edges=edges, mode=mode, big_document=big, choices=choices, settings=settings)
            if write_if_changed(Path(project.wiki) / page_rel, rendered):
        if changed_pages:
            changed_page_rels = {
                page if page.startswith(document + "/") else f"{document}/{page}"
                for page in changed_pages
                if page.startswith(document + "/") or "/" not in page
            }
        document_changed_pages = {
            page for page in (changed_page_rels or ()) if page.startswith(document + "/")
        }
        regenerated_page_rels = {
            page if page.startswith(document + "/") else f"{document}/{page}"
            for page in (regenerated_pages or ())
            if page.startswith(document + "/") or "/" not in page
        }
        team = _team(project)
        mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
        judge = str(getattr(settings, "wiki_linker_judge", "llm"))
        if judge not in {"llm", "jev"}:
            raise ValueError("wiki_linker_judge must be llm or jev")
        if mode not in {"legacy", "neo"}:
            raise ValueError("wiki_linker_mode must be legacy or neo")
        planning = Path(project.wiki_dir(rel)) / "_planning"
        planning.mkdir(parents=True, exist_ok=True)
        run_id = "lrun-" + uuid.uuid4().hex[:20]
        source_marker = read_json(planning / "source.json", default={})
        id_seed = str(source_marker.get("id_seed") or document)
        # A document without a complete marker (first run, failed run, rebuild) gets a
        # candidate pass for every chunk, even ones the catalog already knows from
        # bootstrapping; metadata is still reused through the chunks.json cache.
        previous_marker = read_json(planning / "linker.json", default={})
        previously_complete = (
            previous_marker.get("status") == "complete"
            or previous_marker.get("resume") is True
        )
        write_json_atomic(planning / "linker.json", {"schema_version": 2, "status": "pending", "mode": mode, "run_id": run_id})
        if on_progress:
            on_progress({"stage": "linker", "step": "pending", "document": rel})
        catalog: Catalog | None = None
        jev_engine = None
        jev_fallbacks = 0
        jev_failed_chunks: set[str] = set()
        try:
            edge_version = EDGE_VERSION_JEV if judge == "jev" else EDGE_VERSION_NEO if mode == "neo" else EDGE_VERSION_LEGACY
            if judge == "jev":
                try:
                    from jev import get_engine_for
                    jev_engine = get_engine_for(settings)
                except Exception as exc:
                    log.warning("Jev linker engine unavailable; using LLM: %s", exc)
                    jev_fallbacks += 1
            catalog = Catalog.open(project.linker_database, mode=mode, edge_version=edge_version)
            with catalog.lock(project):
                catalog.sync_from_planning(project, skip_document=document)
                chunk_cache_path = planning / "chunks.json"
                incremental_scope = changed_page_rels is not None and previously_complete
                refresh_metadata = (
                    not incremental_scope
                    and model is not None
                    and read_json(chunk_cache_path, default={}).get("meta_version") != CHUNK_META_VERSION
                )
                previous_cache = chunks.cache_by_hash(chunk_cache_path)
                original_hashes = chunks.snapshot_originals(project.wiki_dir(rel))
                all_chunks: list[chunks.Chunk] = []
                for page in sorted((planning / "pages").glob("*.md")):
                    all_chunks.extend(chunks.make_chunks(document, team, page.name, page.read_text(encoding="utf-8"), id_seed=id_seed))
                old_rows = {row["chunk_id"]: row for row in catalog.chunks_for_document(document)}
                old_titles = {
                    str(row["page_rel"]): str(row["title"])
                    for row in catalog.conn.execute("SELECT page_rel,title FROM pages WHERE document=?", (document,))
                }
                old_edges_by_id: dict[str, dict[str, Any]] = {}
                old_page_rels: dict[str, str] = {}
                if incremental_scope and old_rows:
                    old_page_rels = {
                        str(row["chunk_id"]): str(row["page_rel"])
                        for row in catalog.conn.execute("SELECT chunk_id,page_rel FROM chunks")
                    }
                    ids = list(old_rows)
                    marks = ",".join("?" for _ in ids)
                    for edge in catalog.conn.execute(
                        f"SELECT * FROM edges WHERE chunk_a IN ({marks}) OR chunk_b IN ({marks}) ORDER BY edge_id",
                        [*ids, *ids],
                    ).fetchall():
                        stored = dict(edge)
                        old_edges_by_id[str(edge["edge_id"])] = stored
                for item in all_chunks:
                    if item.text_sha256 in previous_cache:
                        item.meta = previous_cache[item.text_sha256]
                    elif not refresh_metadata and item.chunk_id in old_rows and item.page_rel not in regenerated_page_rels:
                        item.meta = _row_meta(old_rows[item.chunk_id])
                        if incremental_scope:
                            item.meta = chunks.validate_meta(item.meta, item.text)
                diff = catalog.reconcile(document, all_chunks, team=team, raw_rel=rel, page_hashes=original_hashes)
                stale_ids = set(diff["new"]) | set(diff["changed"])
                if changed_page_rels is not None:
                    stale_ids.intersection_update(
                        item.chunk_id for item in all_chunks if item.page_rel in changed_page_rels
                    )
                # Regenerated chunks need global candidate rediscovery. Patched chunks
                # refresh metadata and embeddings below, but keep the incremental
                # contract of rechecking their existing visible edges only.
                fresh_ids = (
                    {
                        item.chunk_id for item in all_chunks
                        if item.page_rel in regenerated_page_rels and item.chunk_id in stale_ids
                    }
                    if incremental_scope else set()
                )
                if incremental_scope:
                    # Patched pages need fresh metadata just as regenerated pages do.
                    # Reusing the old row after the chunk text changed leaves summaries,
                    # entities, edge candidates, and embeddings stale.
                    to_describe = [
                        item for item in all_chunks
                        if item.chunk_id in stale_ids and item.text_sha256 not in previous_cache
                    ]
                else:
                    to_describe = [item for item in all_chunks if refresh_metadata or not previously_complete or item.chunk_id in stale_ids]
                reported_chunks = len(stale_ids) if incremental_scope else len(to_describe)
                if on_progress:
                    on_progress({
                        "stage": "linker", "step": "chunks", "document": rel,
                        "current": reported_chunks, "total": reported_chunks,
                        "catalog_total": len(all_chunks),
                    })
                run_dir = Path(project.state_dir(rel)) / "work" / "linker" / run_id
                meta_calls, meta_fallbacks = (0, 0)
                revised_ids: set[str] = set()
                output_language = str(getattr(settings, "wiki_output_language", "Japanese (日本語)"))
                if to_describe and model is not None:
                    before_meta = {item.chunk_id: item.meta.model_dump_json() for item in all_chunks}
                    meta_calls, meta_fallbacks = await chunks.describe_all(to_describe, model=model, output_language=output_language, concurrency=_concurrency(settings), cache=previous_cache, artifact_dir=run_dir, stop_check=stop_check, parallel=judge == "jev")
                    revised_ids = {item.chunk_id for item in all_chunks if item.meta.model_dump_json() != before_meta[item.chunk_id]}
                    if changed_page_rels is not None and not refresh_metadata:
                        to_describe = [
                            item for item in all_chunks
                                edge_rows.append(previous or {"chunk_a": item.chunk_id, "chunk_b": candidate.chunk_id,
                                                               "label": "related" if judge == "jev" else decision["label"],
                                                               "summary": "" if judge == "jev" else decision["summary"],
                                                               "source": "jev" if judge == "jev" else candidate.source,
                                                               "via": [candidate.source, *candidate.via] if judge == "jev" else candidate.via})
                        elif model is not None:
                            pending.append(candidate)
                    if pending:
                        unresolved.append((item, pending))
                edge_calls = 0
                concurrency = _concurrency(settings)
                semaphore = asyncio.Semaphore(concurrency)
                completed = 0
    
                async def filter_target(item: chunks.Chunk, pending: list[Candidate]) -> tuple[list[dict[str, Any]], int]:
                    nonlocal completed, jev_fallbacks
                    async with semaphore:
                        accepted, calls, fallbacks = await _filter_target(
                            catalog, model, item, pending, mode=mode, version=edge_version,
                            artifact_dir=run_dir, stop_check=stop_check, output_language=output_language,
                            strict=incremental_scope, judge=judge, jev_engine=jev_engine,
                            settings=settings, use_jev=item.chunk_id not in jev_failed_chunks,
                        )
                        jev_fallbacks += fallbacks
                        result = (accepted, calls)
                    completed += 1
                    if on_progress:
                        on_progress({"stage": "linker", "step": "edge_target_done", "document": rel, "current": completed, "total": len(unresolved)})
                    return result
    
                filtered = await asyncio.gather(*(filter_target(item, pending) for item, pending in unresolved))
                for (item, pending), (accepted, calls) in zip(unresolved, filtered):
                    edge_calls += calls
                    for candidate in pending:
                        row = catalog.chunk(candidate.chunk_id)
                        if row is None:
                            continue
                        matches = [edge for edge in accepted if edge["chunk_b"] == candidate.chunk_id]
                        if matches:
                            best = matches[0]
                            previous = incremental_candidate_edges.get((item.chunk_id, candidate.chunk_id))
                            if previous is not None:
                                best = previous
                            edge_rows.append(best)
                            catalog.edge_decision_put(item.text_sha256, row["text_sha256"], mode, edge_version, True, best["label"], best["summary"])
                        else:
                            catalog.edge_decision_put(item.text_sha256, row["text_sha256"], mode, edge_version, False, "", "")
                inserted_edges = 0
                for edge in edge_rows:
                    inserted_edges += int(catalog.insert_edge(edge, commit=False))
                catalog.conn.commit()
                if incremental_scope:
                    kept_edge_ids = {catalog.edge_id_for(edge) for edge in edge_rows}
                    changed_link_pages: set[str] = set()
                    for edge in relevant_edges:
                        edge_id = str(edge["edge_id"])
                        if edge_id in kept_edge_ids:
                            continue
                        source = str(edge["source"])
                        if mode == "neo" and source in {"use", "define"}:
                            user_id = str(edge["chunk_a"] if source == "use" else edge["chunk_b"])
                            if old_page_rels.get(user_id):
                                changed_link_pages.add(old_page_rels[user_id])
                        elif mode == "neo":
                            changed_link_pages.update(visible_edge_pages.get(edge_id, set()))
                        else:
                            changed_link_pages.update(
                                old_page_rels[chunk_id]
                                for chunk_id in (str(edge["chunk_a"]), str(edge["chunk_b"]))
                                if chunk_id in old_page_rels
                            )
                    touched_chunk_ids: set[str] = set()
                else:
                    changed_link_pages = set()
                    touched_chunk_ids = set(diff["peers_before"]) | revised_peers
                    for edge in edge_rows:
                        touched_chunk_ids.update((edge["chunk_a"], edge["chunk_b"]))
                if changed_page_rels is not None and not refresh_metadata:
                    pages: set[str] = {
                        item.page_rel for item in all_chunks if item.chunk_id in changed_ids
                    }
                else:
                    pages = {
                        item.page_rel
                        for item in all_chunks
                        if not previously_complete or item.chunk_id in changed_ids
                    }
                pages.update(
                    str(old_rows[chunk_id]["page_rel"])
                    for chunk_id in diff["removed"]
                    if chunk_id in old_rows
                )
                pages.update(document_changed_pages)
                pages.update(changed_link_pages)
                pages.update(filter(None, (catalog.page_of(cid) for cid in touched_chunk_ids)))
                if incremental_scope:
                    new_titles = {item.page_rel: item.title for item in all_chunks}
                    retitled = {page for page, title in new_titles.items() if page in old_titles and old_titles[page] != title}
                    if retitled:
                        retitled_ids = {item.chunk_id for item in all_chunks if item.page_rel in retitled}
                        for edge in old_edges_by_id.values():
                            for own, other in ((str(edge["chunk_a"]), str(edge["chunk_b"])), (str(edge["chunk_b"]), str(edge["chunk_a"]))):
                                if own in retitled_ids and old_page_rels.get(other):
                                    pages.add(old_page_rels[other])
                touched_docs: set[str] = set()
                if render:
                    rendered_docs = await render_pages(project, catalog, pages, model=model, settings=settings, mode=mode, on_progress=on_progress)
                    touched_docs.update(_raw_rel(catalog, doc) for doc in rendered_docs if doc != document)
                all_docs = {
                    document,
                    *{page.rsplit("/", 1)[0] for page in pages},
                    *{
                        page_rel.rsplit("/", 1)[0]
                        for edge in relevant_edges
                        for chunk_id in (str(edge["chunk_a"]), str(edge["chunk_b"]))
                        for page_rel in [old_page_rels.get(chunk_id, "")]
                        if page_rel
                    },
                }
                catalog.write_links_json(project, all_docs)
                complete = {"schema_version": 2, "status": "complete" if render else "render_pending", "mode": mode, "scope": "incremental" if incremental_scope else "full", "meta_version": CHUNK_META_VERSION, "edge_version": edge_version, "run_id": run_id, "chunks_total": len(all_chunks), "chunks_new": len(diff["new"]) + len(diff["changed"]), "meta_calls": meta_calls, "edge_calls": edge_calls, "meta_fallbacks": meta_fallbacks, "jev_fallbacks": jev_fallbacks, "edges_added": inserted_edges, "edges_removed": diff.get("edges_removed", 0), "touched_documents": sorted(touched_docs), "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                write_json_atomic(planning / "linker.json", complete)
                if on_progress:
                    on_progress({"stage": "linker", "step": "done", "document": rel, "edges": len(edge_rows)})
                return LinkResult(sorted(touched_docs), inserted_edges, int(diff.get("edges_removed", 0)), meta_calls, edge_calls, meta_fallbacks, sorted(pages), jev_fallbacks)
        except Exception as exc:
            write_json_atomic(planning / "linker.json", {"schema_version": 2, "status": "failed", "mode": mode, "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"[:500]})
            if on_progress:
                on_progress({"stage": "linker", "step": "failed", "document": rel, "error": str(exc)[:200]})
            raise
        finally:
            if catalog is not None:
                catalog.close()
    
    
    async def link_documents(
        project: Any, rels: list[str], *, model: Any, embedder: Any, settings: Any,
        on_progress: Progress = None, stop_check: StopCheck = None,
        changed_pages: set[str] | None = None,
        regenerated_pages: set[str] | None = None,
    ) -> LinkResult:
    
    async def write_tables(
        *,
        sheets: list[tuple[str, str, tuple[int, int]]],
        run_dir,
        model,
        config,
        on_progress=None,
        stop_check=None,
        generate_analyses: bool = True,
    ) -> list[dict[str, Any]]:
        from langchain_core.messages import HumanMessage
        from graph.wiki.storage import read_json, write_json_atomic, write_text_atomic
    
        docs = Path(run_dir) / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        files: list[dict[str, Any]] = []
        prepared = []
        link_renames: dict[str, str] = {}
        progress_stage = "excel-source" if config.source_kind == "xlsx" else "tabular"
        if on_progress:
            on_progress({"stage": progress_stage, "step": "start", "total": len(sheets)})
        semaphore = asyncio.Semaphore(
            max(1, int(getattr(config, "rewrite_concurrency", app_concurrency())))
        )
        completed = 0
    
        async def prepare(number: int, item: tuple[str, str, tuple[int, int]]):
            nonlocal completed
            sheet, table_text, source_range = item
            async with semaphore:
                if stop_check and stop_check():
                    raise RuntimeError("tabular cancelled")
                grid = grid_from_html(table_text, origin=_table_origin(table_text)) if table_text.lstrip().startswith("<table") else grid_from_gfm(table_text.splitlines())
                is_vba = "<!-- vba-id:" in table_text
                if on_progress:
                    on_progress({
                        "stage": progress_stage,
                        "step": "describe",
                        "current": number,
                        "total": len(sheets),
                        "kind": "vba" if is_vba else "sheet",
                        "sheet": sheet,
                    })
                regions = [] if is_vba else find_regions(grid)
                # An unchanged sheet reuses its LLM answers, so a workbook update pays only for
                # the sheets that changed. The cache lives in the run state a full rebuild deletes.
                digest = hashlib.sha256(f"{sheet}\0{table_text}".encode("utf-8")).hexdigest()[:32]
                cache = Path(config.run_dir) / "sheet-cache" / f"{digest}.json" if getattr(config, "run_dir", None) else None
                cached = read_json(cache, default={}) if cache else {}
                if cached:
                    description = VbaDescription.model_validate(cached["vba"]) if cached.get("vba") else None
                    structure = SheetStructure.model_validate(cached["structure"])
                else:
                    description = await describe_vba(
                        sheet,
                        table_text,
                        model=model,
                        language=config.output_language,
                    ) if is_vba else None
                    structure = SheetStructure(
                        summary=description.summary,
                        tables=[],
                    ) if description else SheetStructure(summary="（大きすぎるため原本のみ）", tables=[]) if not regions or "_Sparse cell view" in table_text else await decide_structure(sheet, grid, regions, model=model, config=config)
                    if cache:
                        write_json_atomic(cache, {"vba": description.model_dump() if description else None, "structure": structure.model_dump()})
                tables = []
                for spec in structure.tables:
                    columns, records = records_for(grid, spec)
                    if records:
                        tables.append((spec, columns, records, stats_for(columns, records)))
                page_title = f"マクロ-{description.title}" if description else source_page_title(sheet, is_vba=False) if config.source_kind == "xlsx" else sheet
                name = f"{number:03d}-{_slug(page_title)}.md"
                old_name = f"{number:03d}-{_slug(source_page_title(sheet, is_vba=True))}.md" if is_vba else name
                write_text_atomic(docs / name, render_table_page(page_title if is_vba else sheet, structure, tables, table_text, grid))
                completed += 1
                if on_progress:
                    on_progress({
                        "stage": progress_stage,
                        "step": "done",
                        "current": completed,
                        "total": len(sheets),
                        "kind": "vba" if is_vba else "sheet",
                        "sheet": sheet,
                        "filename": name,
                    })
                return (
                    {"filename": name, "title": page_title if is_vba else sheet, "kind": "vba" if is_vba else "table", "source_ranges": [list(source_range)], "summary": structure.summary},
                    (sheet, source_range, name, tables),
                    (old_name, name) if old_name != name else None,
                )
    
        results = await asyncio.gather(*(prepare(number, item) for number, item in enumerate(sheets, 1)))
        for file, prepared_item, rename in results:
            files.append(file)
            prepared.append(prepared_item)
            if rename:
                link_renames[rename[0]] = rename[1]
    
        if link_renames:
            for path in docs.glob("*.md"):
                body = path.read_text(encoding="utf-8")
                rewritten = body
                for old_name, new_name in link_renames.items():
                    rewritten = rewritten.replace(f"({old_name})", f"({new_name})")
                if rewritten != body:
                    write_text_atomic(path, rewritten)
    
        number = len(prepared)
        for sheet, source_range, name, tables in prepared:
            if not generate_analyses:
                continue
            for spec, columns, records, stats in tables:
                slices = [records] if config.tabular_slice_records <= 0 else [records[i:i + config.tabular_slice_records] for i in range(0, len(records), config.tabular_slice_records)]
                targets = [("分析", records if len(records) <= 60 else records[:30] + records[-10:])]
                if len(slices) > 1:
                    targets += [(str(i + 1), chunk) for i, chunk in enumerate(slices)]
                for suffix, subset in targets:
                    feedback: list[str] = []
                    text = ""
                    for _ in range(3):
                        text = await model.text([HumanMessage(content=analysis_prompt(sheet, spec, columns, subset, stats, language=config.output_language, feedback=feedback))])
                        feedback = check_citations(text, subset)
                        if not feedback:
                            break
                    if feedback:
                        text = "> 引用チェックに失敗したため、統計のみを掲載します。\n\n" + json.dumps(stats, ensure_ascii=False, indent=1)
                    number += 1
                    prefix = "解説" if config.source_kind == "xlsx" else "series"
                    filename = f"{number:03d}-{prefix}-{_slug(sheet)}-{_slug(spec.title)}-{suffix}.md"
                    write_text_atomic(docs / filename, f"# {spec.title} — {suffix}\n\n元の表: [{sheet}]({name})（{spec.title}）\n\n{text}\n")
                    files.append({"filename": filename, "title": f"{spec.title} — {suffix}", "kind": "analysis", "source_ranges": [list(source_range)], "summary": text.strip().splitlines()[0][:120] if text.strip() else ""})
            if on_progress:
                on_progress({"stage": "tabular", "sheet": sheet, "tables": len(tables)})
        if on_progress:
            on_progress({"stage": progress_stage, "step": "complete", "current": len(sheets), "total": len(sheets)})
        planning = Path(run_dir) / "_planning"
        planning.mkdir(exist_ok=True)
        write_json_atomic(planning / "manifest.json", {"planning": {"ingest_mode": "wiki", "strategy": "tabular"}, "files": files})
        write_json_atomic(planning / "coverage.json", {"files": [{"title": item["title"], "filename": re.sub(r"^\d+-", "", item["filename"]), "summary": item["summary"], "header": "VBA" if item["kind"] == "vba" else "表", "source_start": item["source_ranges"][0][0], "source_end": item["source_ranges"][0][1]} for item in files]})
        write_json_atomic(planning / "metadata.json", {"files": [{"name": re.sub(r"^\d+-", "", item["filename"]), "header": "VBA" if item["kind"] == "vba" else "表"} for item in files]})
        return files
    
    
    def _slug(text: str) -> str:
        from graph.wiki.ids import slugify
    
        return slugify(text, fallback="シート")
    
    
    def source_page_title(sheet: str, *, is_vba: bool) -> str:
        title = re.sub(r"-part(\d+)$", r"-部分\1", sheet, flags=re.IGNORECASE)
        if is_vba:
            return re.sub(r"^vba-", "マクロ-", title, flags=re.IGNORECASE)
        return f"シート-{title}"
        )
    
    
    async def _append_story(
        source_path: Path,
        sheets: list[tuple[str, str, tuple[int, int]]],
        files: list[dict[str, Any]],
        *,
        run_dir: Path,
        model: Any,
        config: Any,
        on_progress=None,
        stop_check=None,
    ) -> None:
        from graph.wiki.pipeline import run_pipeline
        from graph.wiki.storage import read_json, write_text_atomic
    
        story_text, spans = _story_source(sheets)
        if not spans:
            _write_planning(run_dir, files, source_name=source_path.name)
            return
        if on_progress:
            on_progress({
                "stage": "excel-story",
                "step": "start",
                "current": 0,
                "total": len(spans),
                "parts": len(spans),
            })
        for span, item in zip(spans, (item for item in files if item["kind"] == "table")):
            span["filename"] = item["filename"]
        state_root = Path(config.run_dir or (run_dir / "wiki-state"))
        story_source = state_root / "excel-story-source.md"
        story_run = state_root / "excel-story"
        write_text_atomic(story_source, story_text)
        story_config = config.model_copy(
            update={
                "run_dir": str(story_run),
                "source_kind": "xlsx",
                "document_slug": "ワークブック解説",
                "window_target_lines": 1,
                "window_overlap_lines": 0,
                "page_target_lines": 1,
            }
        )
        await run_pipeline(
            story_source,
            config=story_config,
            model=model,
            on_progress=on_progress,
            stop_check=stop_check,
        )
        plan = read_json(story_run / "state" / "plan.json")
        if on_progress:
            on_progress({
                "stage": "excel-story",
                "step": "planned",
                "parts": len(spans),
                "pages": len(plan["pages"]),
            })
        offset = len(files)
        titles = {
            page["filename"]: _replace_line_references(page["title"], spans)
            for page in plan["pages"]
        }
        renames = {
            page["filename"]: f"{offset + number:03d}-解説-{_slug(titles[page['filename']])}.md"
            for number, page in enumerate(plan["pages"], 1)
        }
        for story_number, page in enumerate(plan["pages"], 1):
            old_name = page["filename"]
            new_name = renames[old_name]
            body = (story_run / "wiki" / old_name).read_text(encoding="utf-8")
            ranges = page.get("owner_ranges", [])
            used_spans = [
                span
                for start, end in ranges
                for span in spans
                if span["start"] <= end and start <= span["end"]
            ]
            references = list(dict.fromkeys(
                f"- [{cell}]({span['filename']})"
                for span in used_spans
                for cell in span["cells"]
            ))
            if references:
                first_line, separator, rest = body.partition("\n")
                body = "\n".join([
                    first_line,
                    "",
                    "## 参照セル",
                    "",
                    *references,
                    "",
                    rest if separator else "",
                ]).rstrip() + "\n"
            for old_link, new_link in renames.items():
                body = body.replace(f"({old_link})", f"({new_link})")
            body = _replace_line_references(body, spans)
            write_text_atomic(run_dir / "docs" / new_name, body)
            files.append(
                {
                    "filename": new_name,
                    "title": titles[old_name],
                    "kind": "story",
                    "source_cells": _cells_for_ranges(ranges, spans),
                    "summary": _replace_line_references(page.get("summary", ""), spans),
                }
            )
            if on_progress:
                on_progress({
                    "stage": "excel-story",
                    "step": "page",
                    "current": story_number,
                    "total": len(plan["pages"]),
                    "page": titles[old_name],
                })
        _write_planning(run_dir, files, source_name=source_path.name)
        if on_progress:
            on_progress({
                "stage": "excel-story",
                "step": "complete",
                "current": len(plan["pages"]),
                "total": len(plan["pages"]),
            })
    
    ✓ • 11ms

## Activity

    $ sed -n '600,735p' graph/wiki/pipeline.py; sed -n '930,1125p' graph/wiki/pipeline.py; sed -n '1170,1340p' graph/wiki/pipeline.py; sed -n '1,130p' graph/formats/__init__.py; sed -n '1,80p' graph/wiki/pipeline.py
                if item not in adjacent
            ),
            key=lambda entry: (entry[0], entry[1]),
            reverse=True,
        )
        picks = [item for score, _, item in scored[: max(0, limit)] if score > 0]
        family = [item for item in others if item.path and item.path[:-1] == page.path[:-1]]
        unique = {item.number: item for item in adjacent + family[:limit] + picks}
        return sorted(list(unique.values())[: max(0, limit)], key=lambda item: item.number)
    
    
    async def _research_references(
        page: SeedPage,
        *,
        pages: Sequence[SeedPage],
        tokens: dict[int, set[str]],
        model: ModelPort,
        config: WikiConfig,
        work_root: Path,
        seed_root: Path,
        stop_check: StopCheck,
        on_progress: Progress,
    ) -> tuple[list[_ReferenceEvidence], str]:
        """Python selects references; one structured compare call per page."""
    
        selected = _select_references(page, pages, tokens, limit=config.reference_candidates)
        if not selected:
            return [], "# 参照調査結果\n\n他のWikiページはない。\n"
    
        research_dir = work_root / f"research-{page.number:03d}"
        cached = read_json(research_dir / "references.json", default={})
        if (
            isinstance(cached.get("useful_facts"), list)
            and "no_useful_information_reason" in cached
        ):
            result = ReferenceResearchResult.model_validate(cached)
            if _reference_validation_error(result, selected) is None:
                reason = result.no_useful_information_reason.strip()
                evidence = [
                    _ReferenceEvidence(
                        page=candidate,
                        facts=_valid_reference_facts(result.useful_facts, candidate, page),
                        no_useful_information_reason=reason,
                    )
                    for candidate in selected
                ]
                research = _render_reference_research(page, evidence, seed_root=seed_root)
                write_text_atomic(research_dir / "reference-research.md", research)
                _emit(on_progress, "research", "resumed", page=page.title)
                return evidence, research
    
        research_dir = clean_workdir(research_dir)
        target_summary = page.summary.strip() or page.title
        references = "\n\n".join(
            (
                f"--- 参照ページ {candidate.number:03d} {candidate.title}"
                f"（原文 {_ranges_text(candidate.owner_ranges)}行） ---\n"
                f"ページ要約: {candidate.summary}\n"
            )
            for candidate in selected
        )
        _emit(
            on_progress,
            "research",
            "selected",
            page=page.title,
            candidates=len(selected),
            references=[item.number for item in selected],
            context_characters=len(target_summary) + len(references),
            context_bytes=len((target_summary + references).encode("utf-8")),
        )
        prompt = reference_research_prompt(
            target_number=page.number,
            target_title=page.title,
            target_ranges=_ranges_text(page.owner_ranges),
            target_summary=target_summary,
            references=references,
            output_language=config.output_language,
        )
        result, attempts, error = await _structured_with_artifacts(
            schema=ReferenceResearchResult,
            prompt=prompt,
            model=model,
            output_dir=research_dir,
            stem="references",
            attempts=config.reference_attempts,
            max_output_tokens=config.reference_max_output_tokens,
            stop_check=stop_check,
            prompt_factory=lambda feedback: reference_research_prompt(
                target_number=page.number,
                target_title=page.title,
                target_ranges=_ranges_text(page.owner_ranges),
                target_summary=target_summary,
                references=references,
                output_language=config.output_language,
                last_error=feedback,
            ),
            validator=lambda candidate: _reference_validation_error(candidate, selected),
            retry_temperature=getattr(config, "retry_temperature", 0.7),
        )
        reason = (
            result.no_useful_information_reason.strip()
            if result is not None
            else f"調査呼び出し失敗: {error}"
        )
        evidence: list[_ReferenceEvidence] = []
        for current, candidate in enumerate(selected, start=1):
            # _valid_reference_facts keeps only facts inside this candidate's range.
            facts = _valid_reference_facts(result.useful_facts, candidate, page) if result else []
            evidence.append(
                _ReferenceEvidence(
                    page=candidate, facts=facts, no_useful_information_reason=reason
                )
            )
            _emit(
                on_progress,
                "research",
                "reference_done",
                page=page.title,
                reference=candidate.title,
                current=current,
                total=len(selected),
                facts=len(facts),
                attempts=attempts,
                error=error,
            )
    
        research = _render_reference_research(page, evidence, seed_root=seed_root)
        write_text_atomic(research_dir / "reference-research.md", research)
        return evidence, research
    
    
    def _judge_feedback(result: PageJudgeResult) -> list[str]:
        feedback: list[str] = []
        for omission in result.missing_important_information:
            location = (
        units: Sequence[ImageUnit],
        model: ModelPort,
        config: WikiConfig,
        task_dir: Path,
        stop_check: StopCheck,
        on_progress: Progress,
        context: str = "",
    ) -> _SectionResult:
        """Write one section until Python's lossless checks and the judge are satisfied."""
    
        # ponytail: markers plus the section judge cover enrichment; add a dedicated judge if not.
        section_units = [
            unit for unit in units if start <= unit.source_start and unit.source_end <= end
        ]
        placeholders = [unit.placeholder for unit in section_units]
        numbered = _numbered_source(lines, [(start, end)], section_units)
        source_text = _prompt_safe(slice_text(list(lines), start, end), section_units)
        facts_text = _facts_text(facts)
        stem = f"section-{index:02d}"
        candidates: list[_SectionCandidate] = []
        feedback: list[str] = []
    
        for attempt in range(1, max(1, config.write_attempts) + 1):
            if stop_check and stop_check():
                raise asyncio.CancelledError("page writing cancelled")
            prompt = section_write_prompt(
                page_title=page.title,
                page_summary=page.summary,
                index=index,
                count=count,
                source_start=start,
                source_end=end,
                numbered_section=numbered,
                facts_text=facts_text,
                image_context=_image_context(section_units, lines),
                output_language=config.output_language,
                feedback=feedback,
                context=context,
                code_identifiers=(
                    []
                    if config.source_kind in {"csv", "xlsx"}
                    else sorted(code_tokens(source_text))
                ),
            )
            rendered_prompt = prompt.render()
            cached_prompt = task_dir / f"{stem}-attempt-{attempt:02d}-prompt.md"
            cached_draft = task_dir / f"{stem}-attempt-{attempt:02d}.md"
            cached_judge = task_dir / f"{stem}-judge-{attempt:02d}.json"
            if (
                attempt == 1
                and cached_prompt.exists()
                and cached_draft.exists()
                and cached_prompt.read_text(encoding="utf-8") == rendered_prompt
            ):
                draft = cached_draft.read_text(encoding="utf-8")
                errors = check_section(
                    draft,
                    lines=lines,
                    source_text=source_text,
                    block_ranges=(),
                    placeholders=placeholders,
                    facts=facts,
                    check_identifiers=config.source_kind not in {"csv", "xlsx"},
                )
                judgment = read_json(cached_judge, default={})
                if not errors and judgment and not judgment.get("missing_important_information"):
                    _emit(
                        on_progress,
                        "write",
                        "section_resumed",
                        page=page.title,
                        section=index,
                    )
                    return _SectionResult(
                        markdown=draft,
                        attempts=0,
                        score=int(judgment.get("coverage_score", 0)),
                    )
            write_text_atomic(
                cached_prompt, rendered_prompt
            )
            try:
                raw = await model.text(
                    prompt.messages(), max_output_tokens=config.write_max_output_tokens
                )
            except Exception as exc:  # noqa: BLE001 - bounded retry with evidence
                feedback = [f"前回の呼び出しが失敗した: {type(exc).__name__}: {exc}"[:500]]
                write_text_atomic(
                    task_dir / f"{stem}-attempt-{attempt:02d}-error.txt", feedback[0] + "\n"
                )
                continue
            draft = normalize_draft(raw)
            # Gemma sometimes wraps the image token in inline code (`[[NEO-IMAGE:...]]`),
            # which survives restoration as a base64 blob inside backticks and renders
            # broken. Unwrap the token before any placeholder handling.
            draft = re.sub(r"`+(\[\[NEO-IMAGE:[A-Za-z0-9_-]+\]\])`+", r"\1", draft)
            draft = PLACEHOLDER_RE.sub(
                lambda match: match.group(0) if match.group(0) in placeholders else "",
                draft,
            )
            draft = _preserve_image_placeholders(draft, section_units, lines)
            write_text_atomic(task_dir / f"{stem}-attempt-{attempt:02d}.md", draft)
            errors = check_section(
                draft,
                lines=lines,
                source_text=source_text,
                block_ranges=(),
                placeholders=placeholders,
                facts=facts,
                check_identifiers=config.source_kind not in {"csv", "xlsx"},
            )
            if errors:
                candidates.append(_SectionCandidate(draft, attempt, errors=errors))
                feedback = errors
                _emit(
                    on_progress, "write", "section_retry",
                    page=page.title, section=index, attempt=attempt,
                    error="; ".join(errors)[:300],
                )
                continue
            judge_original = numbered
            if facts:
                judge_original += "\n\n--- 他ページから追加した事実（必ず反映） ---\n" + facts_text
            judgment, _judge_attempts, judge_error = await _structured_with_artifacts(
                schema=PageJudgeResult,
                prompt=page_judge_prompt(
                    page_title=f"{page.title}（節 {index}/{count}）",
                    owner_ranges=f"{start}-{end}",
                    numbered_original=judge_original,
                    candidate=draft,
                    output_language=config.output_language,
                ),
                model=model,
                output_dir=task_dir,
                stem=f"{stem}-judge-{attempt:02d}",
                attempts=config.judge_attempts,
                max_output_tokens=config.judge_max_output_tokens,
                stop_check=stop_check,
                retry_temperature=getattr(config, "retry_temperature", 0.7),
            )
            if judgment is None:
                candidates.append(_SectionCandidate(draft, attempt, errors=[]))
                _emit(on_progress, "write", "judge_unavailable",
                      page=page.title, section=index, attempt=attempt, error=judge_error)
                break
            missing = _judge_feedback(judgment)
            candidates.append(
                _SectionCandidate(draft, attempt, score=judgment.coverage_score, missing=missing)
            )
            _emit(
                on_progress, "write", "section_judged",
                page=page.title, section=index, attempt=attempt,
                score=judgment.coverage_score, missing=len(missing),
            )
            if not missing:
                break
            feedback = missing
    
        clean = [item for item in candidates if not item.errors]
        if clean:
            best = max(
                clean,
                key=lambda item: (
                    not item.missing,
                    item.score if item.score is not None else 0,
                    item.attempt,
                ),
            )
            return _SectionResult(
                markdown=best.markdown,
                attempts=len(candidates),
                score=best.score,
                missing=best.missing,
            )
        errors = candidates[-1].errors if candidates else list(feedback)
        _emit(on_progress, "write", "section_verbatim",
              page=page.title, section=index, error="; ".join(errors)[:300])
        return _SectionResult(
            markdown=_verbatim_section(lines, start, end, section_units),
            attempts=len(candidates),
            errors=errors,
            verbatim=True,
        )
    
    
    async def _write_intro(
        page: SeedPage,
        body: str,
        *,
        model: ModelPort,
        config: WikiConfig,
        task_dir: Path,
        stop_check: StopCheck,
        context: str = "",
    ) -> str:
        """One lead paragraph. Anything it cannot justify from the body is dropped."""
        tokens: dict[int, set[str]],
        model: ModelPort,
        config: WikiConfig,
        work_root: Path,
        seed_root: Path,
        source_line_count: int,
        stop_check: StopCheck,
        on_progress: Progress,
        parents: dict[str, str] | None = None,
    ) -> RewriteResult:
        from ..formats.context import context_block
    
        if len(page.owner_ranges) != 1:
            raise PipelineError(f"page {page.number} must own one contiguous range")
        start, end = page.owner_ranges[0]
        page_units = _page_units(page, units)
        task_dir = work_root / f"page-{page.number:03d}"
        task_dir.mkdir(parents=True, exist_ok=True)
    
        evidence, _research = await _research_references(
            page, pages=pages, tokens=tokens, model=model,
            config=config, work_root=work_root, seed_root=seed_root,
            stop_check=stop_check, on_progress=on_progress,
        )
        facts = [fact for item in evidence for fact in item.facts]
        # ponytail: sections stay in source order; add reordering only if a smoke run needs it.
        sections = split_sections(
            lines, start, end,
            target=config.section_target_lines, min_lines=config.section_min_lines,
        )
        buckets = assign_facts(facts, sections)
    
        drafts: list[str] = []
        scores: list[int] = []
        missing: list[str] = []
        verbatim: list[str] = []
        attempts = 0
        for index, ((s, e), section_facts) in enumerate(zip(sections, buckets), start=1):
            result = await _write_section(
                page, s, e, index=index, count=len(sections), facts=section_facts,
                lines=lines, units=units, model=model, config=config, task_dir=task_dir,
                stop_check=stop_check, on_progress=on_progress,
                context=context_block(page, pages, parents or {}),
            )
            drafts.append(result.markdown.rstrip())
            attempts += result.attempts
            if result.score is not None:
                scores.append(result.score)
            missing.extend(f"原文 {s}-{e}行: {item}" for item in result.missing)
            if result.verbatim:
                verbatim.append(f"原文 {s}-{e}行: " + "; ".join(result.errors))
    
        body = "\n\n".join(drafts)
        intro = await _write_intro(
            page, body, model=model, config=config, task_dir=task_dir,
            stop_check=stop_check, context=context_block(page, pages, parents or {}),
        )
        markdown = f"# {page.title}\n\n{intro.rstrip()}\n\n{body}\n"
        markdown = link_titles(
            markdown,
            [(item.title, item.filename) for item in pages if item.number != page.number],
        )
        markdown += _nav_footer(page, pages)
        restored, unresolved = restore_images(markdown, page_units)
        if unresolved:
            raise PipelineError(f"page {page.number} has unresolved image placeholders: {unresolved}")
        page.reference_ranges = _merge_ranges([
            (fact.source_start, fact.source_end) for fact in facts
            if 1 <= fact.source_start <= fact.source_end <= source_line_count
            and not any(start <= fact.source_start and fact.source_end <= end for start, end in page.owner_ranges)
        ])
        restored = strip_reader_references(restored)
        return RewriteResult(
            page=page,
            markdown=restored,
            attempts=attempts,
            judge_score=min(scores) if scores else None,
            missing_important_information=missing,
            verbatim_sections=verbatim,
        )
    
    
    async def _rewrite_all(
        pages: Sequence[SeedPage],
        *,
        lines: list[str],
        units: Sequence[ImageUnit],
        model: ModelPort,
        config: WikiConfig,
        work_root: Path,
        seed_root: Path,
        wiki_root: Path,
        state_root: Path,
        source_path: Path,
        source_snapshot_path: Path,
        source_sha256: str,
        source_line_count: int,
        stop_check: StopCheck,
        on_progress: Progress,
        parents: dict[str, str] | None = None,
    ) -> list[RewriteResult]:
        semaphore = asyncio.Semaphore(max(1, config.rewrite_concurrency))
        tokens = {
            item.number: word_tokens(
                _prompt_safe(_slice_ranges(lines, item.owner_ranges), _page_units(item, units))
            )
            for item in pages
        }
    
        results: list[RewriteResult] = []
        pending: list[SeedPage] = []
        for page in pages:
            output_path = wiki_root / page.filename
            state_path = _page_state_path(state_root, page)
            resumed = (
                _resume_rewritten_page(
                    output_path, state_path, page,
                    rewrite_version=REWRITE_PROMPT_VERSION,
                    source_line_count=source_line_count,
                )
                if config.resume
                else None
            )
            if resumed is not None:
                results.append(resumed)
                _emit(on_progress, "rewrite", "page_resumed",
                      current=len(results), total=len(pages), page=page.title)
            else:
                output_path.unlink(missing_ok=True)
                state_path.unlink(missing_ok=True)
                pending.append(page)
    
        async def one(page: SeedPage) -> RewriteResult:
            async with semaphore:
                return await _rewrite_page(
                    page, pages=pages, lines=lines, units=units, tokens=tokens,
                    model=model, config=config, work_root=work_root, seed_root=seed_root,
                    source_line_count=source_line_count,
                    stop_check=stop_check, on_progress=on_progress,
                    parents=parents,
                )
    
        for completed, task in enumerate(
            asyncio.as_completed([one(page) for page in pending]), start=len(results) + 1
        ):
            result = await task
            results.append(result)
            write_text_atomic(wiki_root / result.page.filename, result.markdown)
            _write_page_state(
                _page_state_path(state_root, result.page), result,
                rewrite_version=REWRITE_PROMPT_VERSION, pages=pages,
                source_path=source_path, source_snapshot_path=source_snapshot_path,
                source_sha256=source_sha256,
            )
            _emit(
                on_progress, "rewrite", "page_done",
                current=completed, total=len(pages), page=result.page.title,
                attempts=result.attempts, score=result.judge_score,
                verbatim_sections=len(result.verbatim_sections),
            )
        return sorted(results, key=lambda item: item.page.number)
    
    
    def _index_text(title: str, pages: Sequence[SeedPage]) -> str:
        lines = [f"# {title}", "", "ページは原文での登場順に並んでいます。", ""]
        seen: set[tuple[str, ...]] = set()
        for page in pages:
            parent = tuple(page.path[:-1])
            if page.path and parent not in seen:
                lines.extend([f"## {' › '.join(parent) or page.path[0]}", ""])
                seen.add(parent)
    """Format-specific wiki planning and tabular output."""
    
    from __future__ import annotations
    
    from pathlib import PurePosixPath
    from typing import Any
    
    KINDS = {"docx", "pptx", "xlsx", "csv", "pdf", "md"}
    TABULAR = {"xlsx", "csv"}
    
    
    def kind_of(document_name: str) -> str:
        stem = PurePosixPath(document_name).stem
        _base, sep, ext = stem.rpartition("_")
        ext = ext.lower()
        aliases = {"xlsm": "xlsx", "xls": "xlsx", "doc": "docx"}
        return aliases.get(ext, ext) if sep and (ext in KINDS or ext in aliases) else "md"
    
    
    def is_tabular(kind: str) -> bool:
        return kind in TABULAR
    
    
    def supports_page_updates(kind: str) -> bool:
        """Formats whose wiki pages can be patched or regenerated one at a time."""
    
        return not is_tabular(kind)
    
    
    async def structural_seed_plan(
        lines: list[str], *, kind: str, config: Any, model: Any,
        on_progress=None, stop_check=None,
    ):
        if kind == "docx":
            from . import docx
    
            return docx.plan(lines, config=config)
        if kind == "pptx":
            from . import pptx
    
            return await pptx.plan(
                lines, config=config, model=model, on_progress=on_progress, stop_check=stop_check
            )
        if kind == "pdf":
            from . import pdf
    
            return pdf.plan(lines, config=config)
        return None
    """The wiki pipeline: deterministic partition, section-wise lossless rewriting.
    
    1. Overlapping 250-line windows are described without assigning ownership.
    2. Regional and document planners compile one exact sequential seed partition.
    3. Python picks references (adjacent + shared vocabulary); one structured
       compare call per page collects facts to import from all of them.
    4. Python cuts each page into sections; fences, tables and images become
       placeholder tokens the model must place; the model rewrites one section at
       a time as plain Markdown; Python restores the tokens and checks identifiers
       and imported facts survived before a judge looks for semantic omissions.
    5. Python writes the title, one model-written intro, links and navigation.
    
    There is no CLI agent and no tool-calling agent. Everything the model needs
    is inside the prompt; every decision about what survives is made in Python.
    """
    
    from __future__ import annotations
    
    import asyncio
    import json
    import re
    import shutil
    from dataclasses import dataclass, field
    from pathlib import Path
    from typing import Any, Callable, Sequence
    
    from .config import REWRITE_PROMPT_VERSION, SEED_PLAN_VERSION, WikiConfig
    from .document_map import build_seed_plan
    from .markdown_blocks import build_block_index
    from .ids import document_id, slugify
    from .images import ImageUnit, block_units, extract_image_units, restore_images
    from .model import ChatModelPort, ModelPort
    from .page import (
        PLACEHOLDER_RE,
        REFERENCE_MARKER_RE,
        assign_facts,
        check_section,
        code_tokens,
        demote_h1,
        link_titles,
        normalize_draft,
        split_sections,
        strip_reader_references,
        word_tokens,
    )
    from .prompts import (
        intro_prompt,
        page_judge_prompt,
        reference_research_prompt,
        section_write_prompt,
    )
    from .schemas import CompiledSeedPlan
    from .storage import (
        clean_workdir,
        normalize_source,
        read_json,
        sha256_text,
        slice_text,
        split_source_lines,
        write_json_atomic,
        write_text_atomic,
    )
    from .wire import (
        PageJudgeResult,
        ReferenceFact,
        ReferenceResearchResult,
    )
    from .windows import observe_document
    
    
    Progress = Callable[[dict[str, Any]], None] | None
    StopCheck = Callable[[], bool] | None
    
    
    class PipelineError(RuntimeError):
        """A deterministic seed or publication invariant failed."""
    
    
    class ResumeUnavailable(PipelineError):
        """The caller required the stored seed plan, but it no longer matches."""
    ✓ • 0ms

## Activity

    $ rg -n 'def (observe_document|build_seed_plan)|_emit\(|on_progress' graph/wiki/windows.py graph/wiki/document_map.py graph/formats/pptx.py graph/formats/pdf.py graph/formats/docx.py | head -240; sed -n '430,610p' graph/wiki/document_map.py; sed -n '320,430p' graph/wiki/windows.py
    graph/wiki/document_map.py:376:    on_progress: Callable[[dict], None] | None,
    graph/wiki/document_map.py:474:        if on_progress:
    graph/wiki/document_map.py:475:            on_progress(
    graph/wiki/document_map.py:513:    on_progress: Callable[[dict], None] | None,
    graph/wiki/document_map.py:529:                if on_progress:
    graph/wiki/document_map.py:530:                    on_progress({"stage": "plan", "step": "semantic", "cached": True})
    graph/wiki/document_map.py:562:                if on_progress:
    graph/wiki/document_map.py:563:                    on_progress(
    graph/wiki/document_map.py:575:    if on_progress:
    graph/wiki/document_map.py:576:        on_progress(
    graph/wiki/document_map.py:591:    on_progress: Callable[[dict], None] | None,
    graph/wiki/document_map.py:618:                if on_progress:
    graph/wiki/document_map.py:619:                    on_progress(
    graph/wiki/document_map.py:689:                if on_progress:
    graph/wiki/document_map.py:690:                    on_progress(
    graph/wiki/document_map.py:716:        if on_progress:
    graph/wiki/document_map.py:717:            on_progress(
    graph/wiki/document_map.py:730:async def build_seed_plan(
    graph/wiki/document_map.py:738:    on_progress: Callable[[dict], None] | None = None,
    graph/wiki/document_map.py:753:        on_progress=on_progress,
    graph/wiki/document_map.py:762:        on_progress=on_progress,
    graph/wiki/document_map.py:772:        on_progress=on_progress,
    graph/wiki/windows.py:303:async def observe_document(
    graph/wiki/windows.py:311:    on_progress: Callable[[dict[str, Any]], None] | None = None,
    graph/wiki/windows.py:356:        if on_progress:
    graph/wiki/windows.py:358:            on_progress(
    graph/wiki/windows.py:379:    if on_progress:
    graph/wiki/windows.py:380:        on_progress(
    graph/formats/pptx.py:128:async def plan(lines: Sequence[str], *, config: Any, model: Any, on_progress=None, stop_check=None) -> CompiledSeedPlan | None:
                        source_end=source_end,
                        window_reports=material,
                        previous_region=previous,
                        output_language=config.output_language,
                        last_error=last_error or None,
                        page_target_lines=config.page_target_lines,
                    )
                    if task_root:
                        write_text_atomic(task_root / f"attempt-{attempt:02d}-prompt.md", prompt.render())
                    try:
                        raw = await model.structured(
                            RegionalPlan,
                            prompt.messages(),
                            max_output_tokens=config.map_max_output_tokens,
                            temperature=config.retry_temperature if last_error else config.temperature,
                        )
                        candidate = raw if isinstance(raw, RegionalPlan) else RegionalPlan.model_validate(raw)
                        if task_root:
                            write_json_atomic(task_root / f"attempt-{attempt:02d}-response.json", candidate)
                        checked, error = validate_regional_plan(
                            candidate,
                            source_start=source_start,
                            source_end=source_end,
                        )
                        if error is None and checked is not None:
                            result = RegionalReport(
                                ordinal=ordinal,
                                source_start=source_start,
                                source_end=source_end,
                                summary=checked.summary,
                                pages=checked.pages,
                            )
                            break
                        last_error = error or "invalid regional plan"
                    except Exception as exc:  # noqa: BLE001 - retry includes feedback
                        last_error = f"{type(exc).__name__}: {exc}"[:1000]
                    if task_root:
                        write_text_atomic(task_root / f"attempt-{attempt:02d}-error.txt", last_error + "\n")
    
            if result is None:
                result = _regional_fallback(reports, ordinal=ordinal)
            if result_path:
                write_json_atomic(result_path, result)
            regions.append(result)
            if on_progress:
                on_progress(
                    {
                        "stage": "plan",
                        "step": "region",
                        "current": ordinal,
                        "total": len(batches),
                        "source_start": source_start,
                        "source_end": source_end,
                        "pages": len(result.pages),
                        "cached": cached,
                        "fallback": bool(last_error and result.summary.startswith("地域モデル")),
                    }
                )
        return regions
    
    
    def _fallback_semantic_plan(regions: Sequence[RegionalReport]) -> SemanticPlan:
        lines = [
            "地域候補を原文順に照合し、重複観察を統合する。",
            "各行を一つだけの連続シードへ割り当てる。",
            "反復形式の具体的エンティティは一件ずつ独立ページにする。",
        ]
        for region in regions:
            for page in region.pages:
                lines.append(
                    f"- {page.source_start}-{page.source_end}: {page.title} — {page.scope}"
                )
        return SemanticPlan(plan="\n".join(lines))
    
    
    async def _build_semantic_plan(
        regions: Sequence[RegionalReport],
        *,
        source_line_count: int,
        model,
        config: WikiConfig,
        checkpoint_root: Path | None,
        stop_check: StopCheck,
        on_progress: Callable[[dict], None] | None,
    ) -> SemanticPlan:
        material = _regional_reports_text(regions)
        key = hash_of(
            {
                "observation_prompt_version": config.prompt_version,
                "seed_plan_version": SEED_PLAN_VERSION,
                "regions": [item.model_dump(mode="json") for item in regions],
            }
        )
        task_root = checkpoint_root / "semantic" / key[:12] if checkpoint_root else None
        result_path = task_root / "result.json" if task_root else None
        if config.resume and result_path and result_path.exists():
            try:
                cached = SemanticPlan.model_validate(read_json(result_path))
                if cached.plan.strip():
                    if on_progress:
                        on_progress({"stage": "plan", "step": "semantic", "cached": True})
                    return cached
            except (OSError, TypeError, ValueError):
                pass
        if task_root:
            task_root.mkdir(parents=True, exist_ok=True)
        last_error = ""
        for attempt in range(1, max(1, config.planner_attempts) + 1):
            if stop_check and stop_check():
                raise asyncio.CancelledError("semantic seed planning cancelled")
            prompt = semantic_plan_prompt(
                source_line_count=source_line_count,
                regional_reports=material,
                output_language=config.output_language,
                last_error=last_error or None,
                page_target_lines=config.page_target_lines,
            )
            if task_root:
                write_text_atomic(task_root / f"attempt-{attempt:02d}-prompt.md", prompt.render())
            try:
                raw = await model.structured(
                    SemanticPlan,
                    prompt.messages(),
                    max_output_tokens=config.map_max_output_tokens,
                    temperature=config.retry_temperature if last_error else config.temperature,
                )
                result = raw if isinstance(raw, SemanticPlan) else SemanticPlan.model_validate(raw)
                if task_root:
                    write_json_atomic(task_root / f"attempt-{attempt:02d}-response.json", result)
                if result.plan.strip():
                    if result_path:
                        write_json_atomic(result_path, result)
                    if on_progress:
                        on_progress(
                            {"stage": "plan", "step": "semantic", "attempt": attempt}
                        )
                    return result
                last_error = "semantic plan is empty"
            except Exception as exc:  # noqa: BLE001 - bounded semantic retry
                last_error = f"{type(exc).__name__}: {exc}"[:1000]
            if task_root:
                write_text_atomic(task_root / f"attempt-{attempt:02d}-error.txt", last_error + "\n")
        result = _fallback_semantic_plan(regions)
        if result_path:
            write_json_atomic(result_path, result)
        if on_progress:
            on_progress(
                {"stage": "plan", "step": "semantic", "fallback": True, "error": last_error}
            )
        return result
    
    
    async def _compile_seed_plan(
        semantic: SemanticPlan,
        regions: Sequence[RegionalReport],
        *,
        lines: Sequence[str],
        model,
        config: WikiConfig,
        checkpoint_root: Path | None,
        stop_check: StopCheck,
        on_progress: Callable[[dict], None] | None,
    ) -> CompiledSeedPlan:
        source_line_count = len(lines)
        block_index = build_block_index(list(lines))
        material = _regional_reports_text(regions)
        key = hash_of(
            {
                "observation_prompt_version": config.prompt_version,
                "seed_plan_compile_version": SEED_PLAN_COMPILE_VERSION,
                "semantic": semantic.model_dump(mode="json"),
                "regions": [item.model_dump(mode="json") for item in regions],
                "source_line_count": source_line_count,
            }
        )
        task_root = checkpoint_root / "compile" / key[:12] if checkpoint_root else None
        result_path = task_root / "result.json" if task_root else None
        if config.resume and result_path and result_path.exists():
            try:
                cached = CompiledSeedPlan.model_validate(read_json(result_path))
                checked, error = validate_seed_plan(
            return ObservationSet(
                document_id=document,
                source_sha256=sha256_text(source_text),
                normalized_source_sha256=sha256_text(text),
                source_line_count=0,
            )
        lines = split_source_lines(text)
        units = extract_image_units(lines)
        windows = overlapping_windows(
            len(lines),
            target=config.window_target_lines,
            overlap=config.window_overlap_lines,
        )
        checkpoint_root = Path(checkpoint_dir) if checkpoint_dir else None
        live_root = Path(live_output_dir) if live_output_dir else None
        semaphore = asyncio.Semaphore(max(1, config.planner_concurrency))
        completed = 0
    
        async def one(ordinal: int, start: int, end: int) -> WindowReport:
            nonlocal completed
            async with semaphore:
                report, cached = await _observe_one(
                    ordinal=ordinal,
                    total=len(windows),
                    source_start=start,
                    source_end=end,
                    lines=lines,
                    units=units,
                    document=document,
                    model=model,
                    config=config,
                    checkpoint_root=checkpoint_root,
                    live_root=live_root,
                    stop_check=stop_check,
                )
            completed += 1
            if on_progress:
                stem = f"window-{ordinal:06d}-lines-{start:06d}-{end:06d}.md"
                on_progress(
                    {
                        "stage": "observe",
                        "step": "window",
                        "current": completed,
                        "total": len(windows),
                        "window": ordinal,
                        "source_start": start,
                        "source_end": end,
                        "observations": len(report.observations),
                        "fallback": report.mechanical,
                        "cached": cached,
                        "live_output": str(live_root / stem) if live_root else "",
                    }
                )
            return report
    
        reports = await asyncio.gather(
            *(one(position, start, end) for position, (start, end) in enumerate(windows, 1))
        )
        reports.sort(key=lambda item: item.ordinal)
        if on_progress:
            on_progress(
                {
                    "stage": "observe",
                    "step": "done",
                    "windows": len(reports),
                    "observations": sum(len(item.observations) for item in reports),
                    "images": len(units),
                    "mechanical_fallbacks": sum(item.mechanical for item in reports),
                }
            )
        return ObservationSet(
            document_id=document,
            source_sha256=sha256_text(source_text),
            normalized_source_sha256=sha256_text(text),
            source_line_count=len(lines),
            windows=reports,
            images=image_records(units),
        )
    ✓ • 0ms

## Activity

    $ sed -n '730,790p' graph/wiki/document_map.py; sed -n '300,390p' graph/wiki/windows.py; sed -n '1,120p' publisher/queue.py; rg -n 'def scan|return .*added|added.*changed|Scan' publisher/queue.py publisher/scanner.py
    async def build_seed_plan(
        observations: ObservationSet,
        *,
        lines: Sequence[str],
        model,
        config: WikiConfig,
        checkpoint_dir: Path | str | None = None,
        stop_check: StopCheck = None,
        on_progress: Callable[[dict], None] | None = None,
    ) -> CompiledSeedPlan:
        """Build regional maps, semantic advice, then the exact seed partition."""
    
        if not lines:
            return CompiledSeedPlan()
        checkpoint_root = Path(checkpoint_dir) if checkpoint_dir else None
        if checkpoint_root:
            checkpoint_root.mkdir(parents=True, exist_ok=True)
        regions = await _build_regions(
            observations,
            model=model,
            config=config,
            checkpoint_root=checkpoint_root,
            stop_check=stop_check,
            on_progress=on_progress,
        )
        semantic = await _build_semantic_plan(
            regions,
            source_line_count=len(lines),
            model=model,
            config=config,
            checkpoint_root=checkpoint_root,
            stop_check=stop_check,
            on_progress=on_progress,
        )
        return await _compile_seed_plan(
            semantic,
            regions,
            lines=lines,
            model=model,
            config=config,
            checkpoint_root=checkpoint_root,
            stop_check=stop_check,
            on_progress=on_progress,
        )
        return report, False
    
    
    async def observe_document(
        source_text: str,
        *,
        model: ModelPort,
        config: WikiConfig | None = None,
        document: str | None = None,
        checkpoint_dir: Path | str | None = None,
        live_output_dir: Path | str | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        stop_check: StopCheck = None,
    ) -> ObservationSet:
        """Inventory independent overlapping windows, four at a time by default."""
    
        config = config or WikiConfig()
        text = normalize_source(source_text)
        document = document or document_id(text)
        if not text:
            return ObservationSet(
                document_id=document,
                source_sha256=sha256_text(source_text),
                normalized_source_sha256=sha256_text(text),
                source_line_count=0,
            )
        lines = split_source_lines(text)
        units = extract_image_units(lines)
        windows = overlapping_windows(
            len(lines),
            target=config.window_target_lines,
            overlap=config.window_overlap_lines,
        )
        checkpoint_root = Path(checkpoint_dir) if checkpoint_dir else None
        live_root = Path(live_output_dir) if live_output_dir else None
        semaphore = asyncio.Semaphore(max(1, config.planner_concurrency))
        completed = 0
    
        async def one(ordinal: int, start: int, end: int) -> WindowReport:
            nonlocal completed
            async with semaphore:
                report, cached = await _observe_one(
                    ordinal=ordinal,
                    total=len(windows),
                    source_start=start,
                    source_end=end,
                    lines=lines,
                    units=units,
                    document=document,
                    model=model,
                    config=config,
                    checkpoint_root=checkpoint_root,
                    live_root=live_root,
                    stop_check=stop_check,
                )
            completed += 1
            if on_progress:
                stem = f"window-{ordinal:06d}-lines-{start:06d}-{end:06d}.md"
                on_progress(
                    {
                        "stage": "observe",
                        "step": "window",
                        "current": completed,
                        "total": len(windows),
                        "window": ordinal,
                        "source_start": start,
                        "source_end": end,
                        "observations": len(report.observations),
                        "fallback": report.mechanical,
                        "cached": cached,
                        "live_output": str(live_root / stem) if live_root else "",
                    }
                )
            return report
    
        reports = await asyncio.gather(
            *(one(position, start, end) for position, (start, end) in enumerate(windows, 1))
        )
        reports.sort(key=lambda item: item.ordinal)
        if on_progress:
            on_progress(
                {
                    "stage": "observe",
                    "step": "done",
                    "windows": len(reports),
                    "observations": sum(len(item.observations) for item in reports),
                    "images": len(units),
                    "mechanical_fallbacks": sum(item.mechanical for item in reports),
                }
            )
        return ObservationSet(
    """Persistent, coalescing work queue for immutable mount snapshots."""
    
    from __future__ import annotations
    
    import copy
    import hashlib
    import io
    import sqlite3
    import subprocess
    import threading
    import time
    import uuid
    from contextlib import contextmanager
    from dataclasses import dataclass
    from difflib import SequenceMatcher
    from pathlib import Path
    from typing import Any, Literal
    
    from docx import Document
    from docx.table import Table
    
    from graph.workspace.project import Project, assert_unique_generated_paths, open_project, raw_name_for
    from graph.wiki.incremental import line_hunks
    from graph.wiki.storage import read_json
    
    from .history import (
        amend_candidate, candidate, candidate_is_clean, candidate_project, commit_candidate, ensure_repository, last_good, list_candidate_ids, promote,
        prune_candidates, read_blob, remove_candidate, reopen_candidate, restore_last_good, resumed_candidate, stage_blob,
    )
    from .ledger import load_ledger
    from .scanner import IGNORED_DIRS, IGNORED_NAMES, SUPPORTED, _inside
    
    SMALL_DOCUMENT_LINES = 1000
    SMALL_DOCUMENT_RATIO = 0.25
    LARGE_DOCUMENT_RATIO = 0.10
    MOVE_SIMILARITY = 0.90
    
    
    @dataclass(frozen=True)
    class Job:
        rel: str
        raw_rel: str
        operation: str
        lane: str
        version: int
        token: str
        source_id: str = ""
        from_rel: str = ""
        target_blob_oid: str = ""
        target_sha256: str = ""
        classification: str = "none"
        base_commit: str = ""
    
    
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    
    
    def _add_columns(conn: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
        present = _columns(conn, table)
        for name, definition in definitions.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    
    
    @contextmanager
    def _connect(project: Project):
        conn = sqlite3.connect(project.queue_database, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sources (
                    rel TEXT PRIMARY KEY,
                    raw_rel TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    source_id TEXT NOT NULL DEFAULT '',
                    source_sha256 TEXT NOT NULL DEFAULT '',
                    blob_oid TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    rel TEXT PRIMARY KEY,
                    raw_rel TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    lane TEXT NOT NULL CHECK(lane IN ('fast','slow')),
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','failed')),
                    token TEXT NOT NULL DEFAULT '',
                    available_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    source_id TEXT NOT NULL DEFAULT '',
                    from_rel TEXT NOT NULL DEFAULT '',
                    target_blob_oid TEXT NOT NULL DEFAULT '',
                    target_sha256 TEXT NOT NULL DEFAULT '',
                    classification TEXT NOT NULL DEFAULT 'none',
                    base_commit TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, lane, available_at, created_at);
                CREATE TABLE IF NOT EXISTS transactions (
                    operation_id TEXT PRIMARY KEY,
                    candidate_commit TEXT NOT NULL DEFAULT '',
                    base_commit TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    started_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transaction_revisions (
                    operation_id TEXT NOT NULL,
                    page_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    PRIMARY KEY(operation_id,page_id,revision_id)
                );
                """
            )
            _add_columns(conn, "sources", {
                "source_id": "TEXT NOT NULL DEFAULT ''",
                "source_sha256": "TEXT NOT NULL DEFAULT ''",
    publisher/scanner.py:24:class Scan:
    publisher/scanner.py:39:def scan_mount(root: Path, previous: dict[str, dict[str, object]] | None = None) -> Scan:
    publisher/scanner.py:61:    return Scan(files, added, changed, deleted)
    publisher/scanner.py:64:__all__ = ["Scan", "SourceFile", "SUPPORTED", "scan_mount"]
    publisher/queue.py:261:def scan(
    publisher/queue.py:1014:            if row.get("status") in {"added", "changed", "deleted", "moved"}
    publisher/queue.py:1090:    """Scan cheaply while the foreground worker runs long jobs."""
    publisher/queue.py:1105:    def scan_loop() -> None:
    ✓ • 0ms

## Activity

    $ sed -n '240,330p' publisher/queue.py; sed -n '840,1025p' publisher/queue.py; sed -n '1,95p' publisher/scanner.py
            conn.execute(statement, values[:2] + ("update",) + values[3:])
    
    
    def _source_identity(
        project: Project,
        ledger: Any,
        rel: str,
        previous: sqlite3.Row | None = None,
        digest: str = "",
    ) -> str:
        identities = read_json(project.metadata / "source-identities.json", default={})
        tombstone = dict((identities.get("tombstones") or {}).get(rel) or {})
        restored = str(tombstone.get("source_id") or "") if digest and tombstone.get("source_sha256") == digest else ""
        return str(
            (previous["source_id"] if previous is not None and previous["source_id"] else "")
            or ledger.sources.get(rel, {}).get("source_id")
            or restored
            or uuid.uuid4()
        )
    
    
    def scan(
        settings: Any,
        *,
        only: list[str] | None = None,
        settle_seconds: float = 10.0,
        force: bool = False,
        verify_content: bool = False,
    ) -> dict[str, Any]:
        """Stat the mount, stage changed bytes once, and coalesce desired state."""
        project = open_project(settings)
        base_commit = ensure_repository(project)
        current = _snapshot(project.mount)
        wanted = {item.strip().lstrip("/") for item in only or ()}
        if wanted:
            current = {rel: row for rel, row in current.items() if rel in wanted}
        now = time.time()
        result: dict[str, Any] = {"added": [], "updated": [], "deleted": [], "cancelled": [], "moved": [], "classification": {}}
        ledger = load_ledger(project.metadata / "pipeline.json")
        with _connect(project) as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = {
                str(row["rel"]): row
                for row in conn.execute("SELECT * FROM sources")
                if not wanted or str(row["rel"]) in wanted
            }
            previous_ids = {str(row["source_id"]) for row in previous.values() if row["source_id"]}
            for rel, source in ledger.sources.items():
                if (
                    rel in previous or (wanted and rel not in wanted)
                    or str(source.get("source_id") or "") in previous_ids
                ):
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO sources
                       (rel,raw_rel,size,mtime_ns,source_id,source_sha256,blob_oid) VALUES(?,?,?,?,?,?,?)""",
                    (
                        rel, str(source.get("raw_rel") or raw_name_for(Path(rel).name)),
                        int(source.get("size") or 0), int(source.get("mtime_ns") or 0),
                        str(source.get("source_id") or ""), str(source.get("source_sha256") or ""),
                        str(source.get("source_blob_oid") or ""),
                    ),
                )
                previous[rel] = conn.execute("SELECT * FROM sources WHERE rel=?", (rel,)).fetchone()
            ledger_rel_by_id = {
                str(source.get("source_id")): rel
                for rel, source in ledger.sources.items()
                if source.get("source_id")
            }
            for rel, row in list(previous.items()):
                source = ledger.sources.get(rel, {})
                if source and (not row["source_id"] or not row["source_sha256"] or not row["blob_oid"]):
                    conn.execute(
                        "UPDATE sources SET source_id=?,source_sha256=?,blob_oid=? WHERE rel=?",
                        (
                            str(source.get("source_id") or ""), str(source.get("source_sha256") or ""),
                            str(source.get("source_blob_oid") or ""), rel,
                        ),
                    )
                    previous[rel] = conn.execute("SELECT * FROM sources WHERE rel=?", (rel,)).fetchone()
            queued = {str(row[0]) for row in conn.execute("SELECT rel FROM jobs")}
            changed_paths = {
                rel for rel, (_raw, size, mtime_ns) in current.items()
                if rel not in previous or force or verify_content
                or (int(previous[rel]["size"]), int(previous[rel]["mtime_ns"])) != (size, mtime_ns)
            }
            staged: dict[str, Any] = {}
            for rel in sorted(changed_paths):
                try:
                    staged[rel] = stage_blob(project, project.mount / rel)
                except (FileNotFoundError, OSError, RuntimeError):
    
        With ``continue_run`` (``--continue``) an interrupted building/prepared
        candidate is reused with its LLM checkpoints intact; otherwise leftover
        worktrees are discarded up front.  Deletion is deferred: the worker keeps
        its worktree on exit (``keep=True``) and the next startup decides.
        """
        from .pipeline import delete_sources, move_sources, restore_publication, sync_once
    
        project = open_project(settings)
        ensure_repository(project)
        resumable_id: str | None = None
        resumable_commit = ""
        if continue_run:
            resumable = _resumable_transaction(project)
            if resumable is None and _adopt_resumable_orphan(project) is not None:
                resumable = _resumable_transaction(project)
            if resumable is not None:
                resumable_id = str(resumable["operation_id"])
                resumable_commit = str(resumable["candidate_commit"] or resumable["base_commit"])
            _prune_orphan_candidates(project, keep=resumable_id)
        else:
            prune_candidates(project)
        with _connect(project) as conn:
            unfinished = conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone() is not None
        if unfinished:
            recover(project, settings, preserve_operation_id=resumable_id)
            if resumable_id is not None:
                refreshed = _resumable_transaction(project)
                if refreshed is None or str(refreshed["operation_id"]) != resumable_id:
                    resumable_id = None
                    resumable_commit = ""
                else:
                    resumable_commit = str(refreshed["candidate_commit"] or refreshed["base_commit"])
        jobs = claim(project, "fast")
        if not jobs:
            with _connect(project) as conn:
                if conn.execute("SELECT 1 FROM jobs WHERE lane='fast' AND status='failed' LIMIT 1").fetchone():
                    return None
            jobs = claim(project, "slow")
        if not jobs:
            return None
        if on_event is not None:
            on_event({"stage": "queue-claim", "lane": jobs[0].lane, "paths": [job.rel for job in jobs]})
        reuse = resumable_id is not None
        if reuse:
            assert resumable_id is not None
            operation_id = resumable_id
            base = last_good(project)
            if on_event is not None:
                on_event({"stage": "queue-resume", "operation_id": operation_id, "base_commit": base})
        else:
            operation_id = "op-" + uuid.uuid4().hex
            base = last_good(project)
            _transaction(project, operation_id, base, "building")
        is_current = lambda: supersession(project, jobs) == "continue"
        result: dict[str, Any]
        publishing = False
        try:
            if reuse:
                assert resumable_id is not None
                staged_ctx = resumed_candidate(project, operation_id, resumable_commit or base)
            else:
                staged_ctx = candidate(project, operation_id, keep=True)
            with staged_ctx as staged:
                for job in jobs:
                    if job.target_blob_oid:
                        target = staged.mount / job.rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(read_blob(project, job.target_blob_oid))
                staged_settings = _candidate_settings(settings, staged)
                prepared_commit = ""
    
                def prepare_publish() -> None:
                    nonlocal prepared_commit, publishing
                    if prepared_commit:
                        return
                    prepared_commit = commit_candidate(
                        staged, f"publish {operation_id}", _commit_metadata(staged, jobs, base, operation_id)
                    )
                    _transaction(project, operation_id, base, "prepared", prepared_commit)
                    if on_event:
                        on_event({"stage": "history", "step": "candidate", "base_commit": base, "commit": prepared_commit})
    
                def begin_publish() -> None:
                    nonlocal publishing
                    if publishing:
                        return
                    _transaction(project, operation_id, base, "publishing", prepared_commit)
                    publishing = True
    
                record_revision = lambda page: _record_revision(project, operation_id, page)
                live_ledger = load_ledger(project.metadata / "pipeline.json")
                details: dict[str, dict[str, Any]] = {}
                for job in jobs:
                    decision = {"kind": job.classification, "ratio": 0.0, "hunks": 0, "reason": "queued"}
                    previous = live_ledger.sources.get(job.from_rel or job.rel, {})
                    old_oid = str(previous.get("source_blob_oid") or "")
                    if job.classification != "forced" and old_oid and job.target_blob_oid and job.operation != "move":
                        decision = _classification(
                            read_blob(project, old_oid), read_blob(project, job.target_blob_oid), Path(job.rel).suffix.lower(),
                        )
                    if on_event and decision["kind"] != "none":
                        on_event({"stage": "diff-classified", "path": job.rel, "decision": decision["kind"],
                                  "ratio": decision["ratio"], "hunks": decision["hunks"], "reason": decision["reason"]})
                    details[job.rel] = {
                        "source_id": job.source_id,
                        "source_blob_oid": job.target_blob_oid,
                        "source_sha256": job.target_sha256,
                        "classification": decision["kind"],
                        "from_rel": job.from_rel,
                    }
                if jobs[0].lane == "fast":
                    result = delete_sources(staged_settings, {job.rel: job.raw_rel for job in jobs},
                                            should_continue=is_current,
                                            prepare_publish=prepare_publish, begin_publish=begin_publish,
                                            on_revision=record_revision)
                else:
                    move_jobs = [job for job in jobs if job.operation == "move"]
                    normal_jobs = [
                        job for job in jobs
                        if job.operation != "move"
                        or job.target_sha256 != str(live_ledger.sources.get(job.from_rel, {}).get("source_sha256") or "")
                    ]
                    result = {"run_id": operation_id, "done": [], "failures": [], "cancelled": False}
                    if move_jobs:
                        moved = move_sources(staged_settings, move_jobs, should_continue=is_current,
                                             on_progress=on_event, prepare_publish=prepare_publish,
                                             begin_publish=begin_publish, on_revision=record_revision)
                        result["done"].extend(moved["done"])
                        result["failures"].extend(moved["failures"])
                        result["cancelled"] = moved["cancelled"]
                        result.setdefault("index_paths", []).extend(moved.get("index_paths") or [])
                    if normal_jobs and not result["failures"] and not result["cancelled"]:
                        synced = sync_once(
                            staged_settings, only=[job.rel for job in normal_jobs], force=True, resume=True,
                            should_continue=is_current, include_pending=True, on_progress=on_event,
                            source_details=details,
                            prepare_publish=prepare_publish,
                            begin_publish=begin_publish,
                            on_revision=record_revision,
                        )
                        result["run_id"] = synced["run_id"]
                        result["done"].extend(synced["done"])
                        result["failures"].extend(synced["failures"])
                        result["cancelled"] = synced["cancelled"]
                        result.setdefault("index_paths", []).extend(synced.get("index_paths") or [])
                    result["index_paths"] = sorted(set(result.get("index_paths") or []))
                if not result.get("cancelled") and not result.get("failures"):
                    commit = amend_candidate(staged) if prepared_commit else commit_candidate(
                        staged, f"publish {operation_id}", _commit_metadata(staged, jobs, base, operation_id)
                    )
                    _transaction(project, operation_id, base, "publishing", commit)
                    promote(project, staged, commit)
                    if on_event:
                        on_event({"stage": "history", "step": "promote", "base_commit": base, "commit": commit})
                elif result.get("failures") and publishing:
                    _transaction(project, operation_id, base, "restoring", prepared_commit)
                    restored = restore_publication(
                        settings, staged, jobs, known_revisions=_transaction_revisions(project, operation_id)
                    )
                    _transaction(project, operation_id, base, "restored", restored)
                    if on_event:
                        on_event({"stage": "history", "step": "rollback", "base_commit": base})
        except Exception as exc:
            prefix = "recovery required: " if publishing else ""
            finish(project, jobs, error=f"{prefix}{type(exc).__name__}: {exc}")
            if not publishing:
                _finish_transaction(project, operation_id)
            return {"lane": jobs[0].lane, "jobs": len(jobs), "failures": [f"{type(exc).__name__}: {exc}"], "cancelled": False}
        if result.get("cancelled"):
            finish(project, jobs, error="superseded by a newer mount event", retry=True)
        else:
            completed = {
                str(row.get("path")) for row in result.get("done", [])
                if row.get("status") in {"added", "changed", "deleted", "moved"}
            }
            omitted = sorted(job.rel for job in jobs if job.rel not in completed)
            if omitted:
                result.setdefault("failures", []).append(f"pipeline omitted claimed paths: {omitted}")
        if result.get("cancelled"):
            pass
        elif result.get("failures"):
            finish(project, jobs, error="; ".join(result["failures"]))
        else:
            finish(project, jobs)
        _finish_transaction(project, operation_id)
    """Content-addressed mount scanner."""
    
    from __future__ import annotations
    
    import hashlib
    from dataclasses import dataclass
    from pathlib import Path
    
    SUPPORTED = {".md", ".docx", ".doc", ".pdf", ".pptx", ".xlsx", ".xlsm", ".xls", ".csv"}
    IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
    IGNORED_DIRS = {".git", ".hg", ".svn"}
    
    
    @dataclass(frozen=True)
    class SourceFile:
        rel: str
        source_sha256: str
        size: int
        mtime_ns: int
        parser: str
    
    
    @dataclass(frozen=True)
    class Scan:
        files: dict[str, SourceFile]
        added: list[str]
        changed: list[str]
        deleted: list[str]
    
    
    def _inside(path: Path, root: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
        except ValueError:
            return False
        return True
    
    
    def scan_mount(root: Path, previous: dict[str, dict[str, object]] | None = None) -> Scan:
        root = Path(root)
        previous = previous or {}
        files: dict[str, SourceFile] = {}
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if any(part in IGNORED_DIRS for part in path.parts):
                    continue
                if path.name in IGNORED_NAMES or path.name.startswith("~$") or not path.is_file() or not _inside(path, root):
                    continue
                suffix = path.suffix.lower()
                if suffix not in SUPPORTED:
                    continue
                stat = path.stat()
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                rel = path.relative_to(root).as_posix()
                files[rel] = SourceFile(rel, digest, stat.st_size, stat.st_mtime_ns, "md" if suffix == ".md" else suffix[1:])
        current = set(files)
        known = set(previous)
        added = sorted(current - known)
        changed = sorted(rel for rel in current & known if str(previous[rel].get("source_sha256", "")) != files[rel].source_sha256)
        deleted = sorted(known - current)
        return Scan(files, added, changed, deleted)
    
    
    __all__ = ["Scan", "SourceFile", "SUPPORTED", "scan_mount"]
    ✓ • 3ms

## Activity

    $ sed -n '330,470p' publisher/queue.py; sed -n '470,560p' publisher/queue.py; sed -n '560,650p' publisher/queue.py
                except (FileNotFoundError, OSError, RuntimeError):
                    continue
    
            disappeared = set(previous) - set(current)
            appeared = set(current) - set(previous)
            old_by_hash: dict[str, list[str]] = {}
            new_by_hash: dict[str, list[str]] = {}
            for rel in disappeared:
                digest = str(previous[rel]["source_sha256"] or ledger.sources.get(rel, {}).get("source_sha256") or "")
                if digest:
                    old_by_hash.setdefault(digest, []).append(rel)
            for rel in appeared:
                if rel in staged:
                    new_by_hash.setdefault(staged[rel].sha256, []).append(rel)
            moves: dict[str, str] = {}
            for digest in set(old_by_hash) & set(new_by_hash):
                if len(old_by_hash[digest]) == len(new_by_hash[digest]) == 1:
                    old_rel, new_rel = old_by_hash[digest][0], new_by_hash[digest][0]
                    source_id = str(previous[old_rel]["source_id"] or "")
                    if source_id in ledger_rel_by_id and Path(old_rel).suffix.lower() == Path(new_rel).suffix.lower():
                        moves[old_rel] = new_rel
            fuzzy: dict[str, list[str]] = {}
            reverse_fuzzy: dict[str, list[str]] = {}
            for old_rel in disappeared - set(moves):
                old_oid = str(previous[old_rel]["blob_oid"] or ledger.sources.get(old_rel, {}).get("source_blob_oid") or "")
                source_id = str(previous[old_rel]["source_id"] or "")
                if source_id not in ledger_rel_by_id or Path(old_rel).suffix.lower() != ".docx" or not old_oid:
                    continue
                old_payload = read_blob(project, old_oid)
                for new_rel in appeared - set(moves.values()):
                    if Path(new_rel).suffix.lower() != ".docx" or new_rel not in staged:
                        continue
                    if _docx_similarity(old_payload, read_blob(project, staged[new_rel].oid)) >= MOVE_SIMILARITY:
                        fuzzy.setdefault(old_rel, []).append(new_rel)
                        reverse_fuzzy.setdefault(new_rel, []).append(old_rel)
            for old_rel, candidates in fuzzy.items():
                if len(candidates) == 1 and len(reverse_fuzzy.get(candidates[0], [])) == 1:
                    moves[old_rel] = candidates[0]
            for old_rel, new_rel in sorted(moves.items()):
                old = previous[old_rel]
                blob = staged[new_rel]
                source_id = _source_identity(project, ledger, old_rel, old, blob.sha256)
                origin = ledger_rel_by_id.get(source_id, old_rel)
                base_source = ledger.sources.get(origin, {})
                raw_rel = current[new_rel][0]
                conn.execute("DELETE FROM sources WHERE rel=?", (old_rel,))
                conn.execute("DELETE FROM jobs WHERE rel=? OR (source_id<>'' AND source_id=?)", (old_rel, source_id))
                conn.execute(
                    "INSERT OR REPLACE INTO sources(rel,raw_rel,size,mtime_ns,source_id,source_sha256,blob_oid) VALUES(?,?,?,?,?,?,?)",
                    (new_rel, raw_rel, current[new_rel][1], current[new_rel][2], source_id, blob.sha256, blob.oid),
                )
                old_oid = str(base_source.get("source_blob_oid") or old["blob_oid"] or "")
                decision = _classification(
                    read_blob(project, old_oid), read_blob(project, blob.oid), Path(new_rel).suffix.lower(),
                )
                if new_rel == origin:
                    if blob.sha256 == str(base_source.get("source_sha256") or ""):
                        result["cancelled"].append(old_rel)
                    else:
                        _enqueue(conn, new_rel, raw_rel, "update", now, settle_seconds, source_id=source_id,
                                 target_blob_oid=blob.oid, target_sha256=blob.sha256,
                                 classification=str(decision["kind"]), base_commit=base_commit)
                        result["updated"].append(new_rel)
                        result["classification"][new_rel] = decision
                    continue
                _enqueue(conn, new_rel, raw_rel, "move", now, settle_seconds, source_id=source_id,
                         from_rel=origin, target_blob_oid=blob.oid, target_sha256=blob.sha256,
                         classification=str(decision["kind"]), base_commit=base_commit)
                result["moved"].append({"from": old_rel, "to": new_rel})
                if blob.sha256 != str(old["source_sha256"] or ""):
                    result["classification"][new_rel] = decision
    
            moved_old, moved_new = set(moves), set(moves.values())
            for rel, (raw_rel, size, mtime_ns) in current.items():
                if rel in moved_new:
                    continue
                old = previous.get(rel)
                blob = staged.get(rel)
                content_changed = bool(blob and blob.sha256 != str(old["source_sha256"] if old is not None else ""))
                changed = old is None or force or content_changed
                if changed and blob is None:
                    continue
                if changed:
                    source_id = _source_identity(project, ledger, rel, old, blob.sha256)
                    origin = ledger_rel_by_id.get(source_id, "")
                    operation = "move" if origin and origin != rel else "update" if old is not None else "add"
                    classification = {"kind": "none", "ratio": 0.0, "hunks": 0, "reason": "new"}
                    if force and old is not None:
                        classification = {"kind": "forced", "ratio": 0.0, "hunks": 0, "reason": "forced"}
                        result["classification"][rel] = classification
                    elif operation in {"update", "move"} or old is not None:
                        active = conn.execute("SELECT target_blob_oid,status FROM jobs WHERE rel=?", (rel,)).fetchone()
                        old_oid = str(
                            (active["target_blob_oid"] if active is not None and active["status"] == "running" else "")
                            or ledger.sources.get(origin or rel, {}).get("source_blob_oid")
                            or (old["blob_oid"] if old is not None else "")
                        )
                        classification = _classification(
                            read_blob(project, old_oid), read_blob(project, blob.oid), Path(rel).suffix.lower(),
                        )
                        result["classification"][rel] = classification
                    _enqueue(conn, rel, raw_rel, operation, now, settle_seconds, source_id=source_id,
                             from_rel=origin if operation == "move" else "",
                             target_blob_oid=blob.oid, target_sha256=blob.sha256,
                             classification=str(classification["kind"]), base_commit=base_commit)
                    result["updated" if old is not None else "added"].append(rel)
                    conn.execute(
                        "INSERT OR REPLACE INTO sources(rel,raw_rel,size,mtime_ns,source_id,source_sha256,blob_oid) VALUES(?,?,?,?,?,?,?)",
                        (rel, raw_rel, size, mtime_ns, source_id, blob.sha256, blob.oid),
                    )
                else:
                    if blob is not None:
                        conn.execute(
                            "UPDATE sources SET size=?,mtime_ns=?,source_sha256=?,blob_oid=? WHERE rel=?",
                            (size, mtime_ns, blob.sha256, blob.oid, rel),
                        )
                    if rel in queued:
                        continue
                    source = ledger.sources.get(rel, {})
                    if source and (not old["source_id"] or not old["source_sha256"] or not old["blob_oid"]):
                        conn.execute(
                            "UPDATE sources SET source_id=?,source_sha256=?,blob_oid=? WHERE rel=?",
                            (
                                str(source.get("source_id") or ""),
                                str(source.get("source_sha256") or ""),
                                str(source.get("source_blob_oid") or ""),
                                rel,
                            ),
                        )
                    document = project.wiki_dir(raw_rel).relative_to(project.wiki).as_posix()
                    incomplete = (
                        not source or bool(source.get("last_error")) or not project.raw_file(raw_rel).exists()
                        or not project.wiki_dir(raw_rel).exists() or document not in ledger.published_documents
                    )
                    if incomplete:
                        blob_oid = str(old["blob_oid"] or "")
                        digest = str(old["source_sha256"] or "")
                        if not blob_oid:
                            try:
                                staged_blob = stage_blob(project, project.mount / rel)
                            except (FileNotFoundError, OSError, RuntimeError):
                            except (FileNotFoundError, OSError, RuntimeError):
                                continue
                            blob_oid, digest = staged_blob.oid, staged_blob.sha256
                        operation = "update" if source else "add"
                        _enqueue(conn, rel, raw_rel, operation, now, settle_seconds,
                                 source_id=_source_identity(project, ledger, rel, old, digest), target_blob_oid=blob_oid,
                                 target_sha256=digest, base_commit=base_commit)
                        result["updated" if source else "added"].append(rel)
    
            for rel in sorted((set(previous) - set(current)) - moved_old):
                old = previous[rel]
                source_id = _source_identity(project, ledger, rel, old)
                origin = ledger_rel_by_id.get(source_id, rel)
                raw_rel = str(ledger.sources.get(origin, {}).get("raw_rel") or old["raw_rel"])
                conn.execute("DELETE FROM sources WHERE rel=?", (rel,))
                job = conn.execute("SELECT operation,lane FROM jobs WHERE rel=?", (rel,)).fetchone()
                known = origin in ledger.sources or any(
                    str(row.get("raw_rel", "")) == raw_rel for row in ledger.published_documents.values()
                )
                if job is not None and str(job["lane"]) == "slow" and not known:
                    conn.execute("DELETE FROM jobs WHERE rel=?", (rel,))
                    result["cancelled"].append(rel)
                elif known:
                    conn.execute("DELETE FROM jobs WHERE rel=? OR (source_id<>'' AND source_id=?)", (rel, source_id))
                    _enqueue(conn, origin, raw_rel, "delete", now, settle_seconds,
                             source_id=source_id, base_commit=base_commit)
                    result["deleted"].append(rel)
        return result
    
    
    def _job_from_row(row: sqlite3.Row) -> Job:
        operation = "move" if row["from_rel"] else str(row["operation"])
        return Job(
            str(row["rel"]), str(row["raw_rel"]), operation, str(row["lane"]), int(row["version"]),
            str(row["token"]), str(row["source_id"]), str(row["from_rel"]), str(row["target_blob_oid"]),
            str(row["target_sha256"]), str(row["classification"]), str(row["base_commit"]),
        )
    
    
    def _resumable_transaction(project: Project) -> dict[str, Any] | None:
        """Oldest interrupted building/prepared transaction usable for --continue.
    
        A transaction qualifies only when its worktree directory still exists on
        disk and its base is still the current last-good (no promotion happened
        since the interruption).
        """
        try:
            base = last_good(project)
        except Exception:
            return None
        with _connect(project) as conn:
            rows = list(conn.execute("SELECT * FROM transactions ORDER BY started_at"))
        for row in rows:
            data = dict(row)
            if str(data.get("phase") or "") not in {"building", "prepared"}:
                continue
            if str(data.get("base_commit") or "") != base:
                continue
            try:
                exists = candidate_project(project, str(data.get("operation_id") or "")).root.exists()
            except (ValueError, OSError):
                continue
            if exists:
                return data
        return None
    
    
    def _adopt_resumable_orphan(project: Project) -> str | None:
        """Recover a dirty same-base candidate whose transaction row was lost.
    
        Older workers removed the transaction row after a pre-publication pipeline
        failure even though ``keep=True`` retained the worktree.  On an explicit
        ``--continue``, adopt the newest such worktree when work for the same base
        is still pending.  Successful candidates are clean and stale-base
        candidates are rejected, so neither is mistaken for resumable progress.
        """
        base = last_good(project)
        with _connect(project) as conn:
            if conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone() is not None:
                return None
            pending = conn.execute(
                """SELECT 1 FROM jobs
                   WHERE status IN ('queued','running','failed') AND base_commit=? LIMIT 1""",
                (base,),
            ).fetchone()
        if pending is None:
            return None
        candidates: list[tuple[int, str]] = []
        for operation_id in list_candidate_ids(project):
            try:
                staged = candidate_project(project, operation_id)
                staged = candidate_project(project, operation_id)
                head = subprocess.run(
                    ["git", "-C", str(staged.root), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if head != base or candidate_is_clean(staged, base):
                    continue
                candidates.append((staged.root.stat().st_mtime_ns, operation_id))
            except (OSError, subprocess.CalledProcessError, ValueError):
                continue
        if not candidates:
            return None
        operation_id = max(candidates)[1]
        _transaction(project, operation_id, base, "building")
        return operation_id
    
    
    def _prune_orphan_candidates(project: Project, *, keep: str | None = None) -> None:
        """Remove candidate dirs that no transaction row references.
    
        Successful batches leave their kept worktree behind (deferred deletion);
        the next iteration discards them here.  The resumable ``keep`` id and any
        dir still referenced by the transactions table are preserved.
        """
        with _connect(project) as conn:
            live = {str(row[0]) for row in conn.execute("SELECT operation_id FROM transactions")}
        for operation_id in list_candidate_ids(project):
            if operation_id == keep or operation_id in live:
                continue
            try:
                remove_candidate(project, operation_id)
            except (ValueError, OSError):
                continue
    
    
    def resumable_operation_id(project: Project, *, adopt_orphan: bool = False) -> str | None:
        """Operation id of the kept candidate ``--continue`` would resume, if any."""
        row = _resumable_transaction(project)
        if row is None and adopt_orphan and _adopt_resumable_orphan(project) is not None:
            row = _resumable_transaction(project)
        return str(row["operation_id"]) if row is not None else None
    
    
    def recover(project: Project, settings: Any | None = None, *, preserve_operation_id: str | None = None) -> int:
        """Restore interrupted publication, then return claimed work to the queue."""
        ensure_repository(project)
        with _connect(project) as conn:
            transactions = list(conn.execute("SELECT * FROM transactions ORDER BY started_at"))
            jobs = [_job_from_row(row) for row in conn.execute("SELECT * FROM jobs")]
        if any(str(row["phase"]) == "publishing" for row in transactions) and settings is None:
            raise RuntimeError("publisher settings are required to recover an interrupted GROWI publication")
        if transactions:
            from .pipeline import candidate_publication_complete, restore_publication
    
            for transaction in transactions:
                operation_id = str(transaction["operation_id"])
                commit = str(transaction["candidate_commit"] or "")
                phase = str(transaction["phase"])
                staged = candidate_project(project, operation_id)
                if phase == "restored":
                    if not commit or last_good(project) != commit:
                        raise RuntimeError(f"cannot recover publication {operation_id}: restored commit is missing")
                    _finish_transaction(project, operation_id)
                    continue
                if phase == "restoring" and last_good(project) != str(transaction["base_commit"]):
                    if not candidate_publication_complete(settings, project):
                        raise RuntimeError(f"cannot recover publication {operation_id}: restored state is incomplete")
                    _finish_transaction(project, operation_id)
                    continue
                if commit and last_good(project) == commit:
                    if staged.root.exists():
                        promote(project, staged, commit)
                    else:
                        restore_last_good(project)
                    with _connect(project) as conn:
                        conn.execute(
                            "DELETE FROM jobs WHERE status='running' AND base_commit=?",
                            (str(transaction["base_commit"]),),
                        )
                    _finish_transaction(project, operation_id)
                    continue
                if last_good(project) != str(transaction["base_commit"]):
                    raise RuntimeError(f"cannot recover publication {operation_id}: last-good changed")
                if phase in {"publishing", "restoring"}:
                    if not staged.root.exists():
                        if not commit:
                            raise RuntimeError(f"cannot recover publication {operation_id}: candidate commit is missing")
                        staged = reopen_candidate(project, operation_id, commit)
                    if commit and candidate_is_clean(staged, commit) and candidate_publication_complete(settings, staged):
    ✓ • 0ms

## Activity

    $ rg -n 'describe_all|edge_target|on_progress|step.*chunk|entity|metadata' graph/linker graph/linker/service.py graph/workspace/writer.py | head -260; sed -n '535,735p' graph/linker/service.py; sed -n '1,220p' graph/linker/chunks.py
    graph/workspace/writer.py:95:        image_identity_counts,
    graph/workspace/writer.py:206:                    for identity, count in current.items():
    graph/workspace/writer.py:207:                        required[identity] = max(required[identity], count)
    graph/workspace/writer.py:208:                final_counts = image_identity_counts(final)
    graph/workspace/writer.py:216:                for identity in checked:
    graph/workspace/writer.py:217:                    expected = required[identity]
    graph/workspace/writer.py:218:                    actual = final_counts.get(identity, 0)
    graph/workspace/writer.py:221:                            f"image {labels.get(identity, identity[:12])} count must be "
    graph/workspace/writer.py:325:    on_progress: Progress = None,
    graph/workspace/writer.py:342:            on_progress=on_progress,
    graph/workspace/writer.py:358:    on_progress: Progress = None,
    graph/workspace/writer.py:379:        run_async_blocking(runner(source_path, run_dir=out_dir, model=model, config=config, on_progress=on_progress, stop_check=stop_check))
    graph/workspace/writer.py:389:            on_progress=on_progress,
    graph/workspace/writer.py:418:        on_progress=on_progress,
    graph/workspace/writer.py:492:    on_progress: Progress = None,
    graph/workspace/writer.py:495:    identity_seed: str | None = None,
    graph/workspace/writer.py:567:    if on_progress:
    graph/workspace/writer.py:568:        on_progress(decision_event())
    graph/workspace/writer.py:599:                    state_dir=state_root, on_progress=on_progress, stop_check=stop_check,
    graph/workspace/writer.py:616:                if on_progress:
    graph/workspace/writer.py:617:                    on_progress({"stage": "wiki", "step": "patch_escalated", "file": rel, "pages": sorted(failed), "errors": failed})
    graph/workspace/writer.py:625:                        state_dir=state_root, on_progress=on_progress, stop_check=stop_check,
    graph/workspace/writer.py:636:            } and on_progress:
    graph/workspace/writer.py:637:                on_progress(decision_event())
    graph/workspace/writer.py:644:                state_dir=state_root, on_progress=on_progress, stop_check=stop_check,
    graph/workspace/writer.py:655:        write_source_stamp(target, project.raw_file(rel), rel, identity_seed=identity_seed)
    graph/workspace/writer.py:682:        if (overlay.conflicts or overlay.orphaned) and on_progress:
    graph/workspace/writer.py:683:            on_progress({"stage": "wiki", "step": "human_overlay", "file": rel,
    graph/workspace/writer.py:699:    on_progress: Progress = None, stop_check: StopCheck = None,
    graph/workspace/writer.py:704:        on_progress=on_progress, stop_check=stop_check,
    graph/workspace/writer.py:708:        on_progress=on_progress, stop_check=stop_check,
    graph/workspace/writer.py:720:    on_progress: Progress = None,
    graph/workspace/writer.py:748:            if on_progress:
    graph/workspace/writer.py:749:                on_progress({"stage": "linker", "step": "embedder_unavailable", "error": str(exc)[:200]})
    graph/workspace/writer.py:751:    result = run_async_blocking(link_document(project, rel, model=model, embedder=embedder, settings=settings, on_progress=on_progress, stop_check=stop_check))
    graph/workspace/writer.py:760:    on_progress: Progress = None, stop_check: StopCheck = None,
    graph/workspace/writer.py:775:    model = llm if hasattr(llm, "structured") else ChatModelPort(wiki_config(settings, run_dir=project.metadata / "state" / "linker"), llm=llm) if llm is not None else None
    graph/workspace/writer.py:779:        on_progress=on_progress, stop_check=stop_check, changed_pages=changed_pages,
    graph/workspace/writer.py:791:def write_source_stamp(target: Path, raw_file: Path, rel: str, *, identity_seed: str | None = None) -> None:
    graph/workspace/writer.py:795:    tmp.write_text(json.dumps({"raw": rel, "sha256": _sha256_file(raw_file), "id_seed": identity_seed or rel}), encoding="utf-8")
    graph/workspace/writer.py:833:    for planning in sorted(project.wiki.rglob("_planning/metadata.json")):
    graph/linker/service.py:80:    return _meta_from_json({"summary": row["summary"], "keywords": json.loads(row["keywords_json"] or "[]"), "entity": row["entity"], "claims": json.loads(row["claims_json"] or "[]"), "bridge_probe": row["bridge_probe"], "entities": json.loads(row["entities_json"] or "[]"), "behaviours": json.loads(row["behaviours_json"] or "[]")})
    graph/linker/service.py:96:def _neo_entity_edge_is_valid(catalog: Catalog, edge: dict[str, Any]) -> bool:
    graph/linker/service.py:97:    """Check a stored deterministic entity edge against current chunk metadata."""
    graph/linker/service.py:111:            catalog.canonical(str(row["team"]), chunks.normalize_name(str(entity.get("name", "")))) == catalog.canonical(str(row["team"]), name)
    graph/linker/service.py:112:            and entity.get("role") == role
    graph/linker/service.py:113:            for entity in json.loads(row["entities_json"] or "[]")
    graph/linker/service.py:242:    on_progress: Progress = None,
    graph/linker/service.py:280:        if on_progress:
    graph/linker/service.py:281:            on_progress({"stage": "linker", "step": "page_curated", "page": page_rel, "current": completed, "total": len(jobs)})
    graph/linker/service.py:309:        entity_candidates = [candidate for candidate in candidates_ if candidate.source in {"use", "define"}]
    graph/linker/service.py:311:        selected = entity_candidates + behaviour_candidates[:edge_candidates]
    graph/linker/service.py:367:        entity_edges = [edge for edge in accepted if edge["source"] in {"use", "define"}]
    graph/linker/service.py:369:        return entity_edges + behaviour_edges[:edges_per_target], calls
    graph/linker/service.py:393:    on_progress: Progress = None, stop_check: StopCheck = None, render: bool = True,
    graph/linker/service.py:428:    # bootstrapping; metadata is still reused through the chunks.json cache.
    graph/linker/service.py:435:    if on_progress:
    graph/linker/service.py:436:        on_progress({"stage": "linker", "step": "pending", "document": rel})
    graph/linker/service.py:455:            refresh_metadata = (
    graph/linker/service.py:488:                elif not refresh_metadata and item.chunk_id in old_rows and item.page_rel not in regenerated_page_rels:
    graph/linker/service.py:499:            # refresh metadata and embeddings below, but keep the incremental
    graph/linker/service.py:509:                # Patched pages need fresh metadata just as regenerated pages do.
    graph/linker/service.py:517:                to_describe = [item for item in all_chunks if refresh_metadata or not previously_complete or item.chunk_id in stale_ids]
    graph/linker/service.py:519:            if on_progress:
    graph/linker/service.py:520:                on_progress({
    graph/linker/service.py:521:                    "stage": "linker", "step": "chunks", "document": rel,
    graph/linker/service.py:531:                meta_calls, meta_fallbacks = await chunks.describe_all(to_describe, model=model, output_language=output_language, concurrency=_concurrency(settings), cache=previous_cache, artifact_dir=run_dir, stop_check=stop_check, parallel=judge == "jev")
    graph/linker/service.py:533:                if changed_page_rels is not None and not refresh_metadata:
    graph/linker/service.py:542:                previous_roles = {item.chunk_id: [entity.role for entity in item.entities] for item in all_chunks}
    graph/linker/service.py:547:                                   if previous_roles[item.chunk_id] != [entity.role for entity in item.entities])
    graph/linker/service.py:553:            known_team_names = {name_norm for name_norm, _name in catalog.entity_names(team)}
    graph/linker/service.py:558:                    names = sorted({(chunks.normalize_name(entity.name), entity.name)
    graph/linker/service.py:559:                                    for chunk in all_chunks for entity in chunk.entities
    graph/linker/service.py:560:                                    if chunks.normalize_name(entity.name) not in known_team_names})
    graph/linker/service.py:570:            metadata_edges_removed = catalog.delete_edges_for(revised_ids)
    graph/linker/service.py:571:            diff["edges_removed"] = int(diff.get("edges_removed", 0)) + metadata_edges_removed
    graph/linker/service.py:620:                        if _neo_entity_edge_is_valid(catalog, edge):
    graph/linker/service.py:699:            if incremental_scope and on_progress:
    graph/linker/service.py:700:                on_progress({
    graph/linker/service.py:746:                if on_progress:
    graph/linker/service.py:747:                    on_progress({"stage": "linker", "step": "edge_target_done", "document": rel, "current": completed, "total": len(unresolved)})
    graph/linker/service.py:797:            if changed_page_rels is not None and not refresh_metadata:
    graph/linker/service.py:826:                rendered_docs = await render_pages(project, catalog, pages, model=model, settings=settings, mode=mode, on_progress=on_progress)
    graph/linker/service.py:842:            if on_progress:
    graph/linker/service.py:843:                on_progress({"stage": "linker", "step": "done", "document": rel, "edges": len(edge_rows)})
    graph/linker/service.py:847:        if on_progress:
    graph/linker/service.py:848:            on_progress({"stage": "linker", "step": "failed", "document": rel, "error": str(exc)[:200]})
    graph/linker/service.py:857:    on_progress: Progress = None, stop_check: StopCheck = None,
    graph/linker/service.py:867:            on_progress=on_progress, stop_check=stop_check, render=False,
    graph/linker/service.py:883:                touched_docs = await render_pages(project, catalog, pages, model=model, settings=settings, mode=mode, on_progress=on_progress)
    graph/linker/service.py:80:    return _meta_from_json({"summary": row["summary"], "keywords": json.loads(row["keywords_json"] or "[]"), "entity": row["entity"], "claims": json.loads(row["claims_json"] or "[]"), "bridge_probe": row["bridge_probe"], "entities": json.loads(row["entities_json"] or "[]"), "behaviours": json.loads(row["behaviours_json"] or "[]")})
    graph/linker/service.py:96:def _neo_entity_edge_is_valid(catalog: Catalog, edge: dict[str, Any]) -> bool:
    graph/linker/service.py:97:    """Check a stored deterministic entity edge against current chunk metadata."""
    graph/linker/service.py:111:            catalog.canonical(str(row["team"]), chunks.normalize_name(str(entity.get("name", "")))) == catalog.canonical(str(row["team"]), name)
    graph/linker/service.py:112:            and entity.get("role") == role
    graph/linker/service.py:113:            for entity in json.loads(row["entities_json"] or "[]")
    graph/linker/service.py:242:    on_progress: Progress = None,
    graph/linker/service.py:280:        if on_progress:
    graph/linker/service.py:281:            on_progress({"stage": "linker", "step": "page_curated", "page": page_rel, "current": completed, "total": len(jobs)})
    graph/linker/service.py:309:        entity_candidates = [candidate for candidate in candidates_ if candidate.source in {"use", "define"}]
    graph/linker/service.py:311:        selected = entity_candidates + behaviour_candidates[:edge_candidates]
    graph/linker/service.py:367:        entity_edges = [edge for edge in accepted if edge["source"] in {"use", "define"}]
    graph/linker/service.py:369:        return entity_edges + behaviour_edges[:edges_per_target], calls
    graph/linker/service.py:393:    on_progress: Progress = None, stop_check: StopCheck = None, render: bool = True,
    graph/linker/service.py:428:    # bootstrapping; metadata is still reused through the chunks.json cache.
    graph/linker/service.py:435:    if on_progress:
    graph/linker/service.py:436:        on_progress({"stage": "linker", "step": "pending", "document": rel})
    graph/linker/service.py:455:            refresh_metadata = (
    graph/linker/service.py:488:                elif not refresh_metadata and item.chunk_id in old_rows and item.page_rel not in regenerated_page_rels:
    graph/linker/service.py:499:            # refresh metadata and embeddings below, but keep the incremental
    graph/linker/service.py:509:                # Patched pages need fresh metadata just as regenerated pages do.
    graph/linker/service.py:517:                to_describe = [item for item in all_chunks if refresh_metadata or not previously_complete or item.chunk_id in stale_ids]
    graph/linker/service.py:519:            if on_progress:
    graph/linker/service.py:520:                on_progress({
    graph/linker/service.py:521:                    "stage": "linker", "step": "chunks", "document": rel,
    graph/linker/service.py:531:                meta_calls, meta_fallbacks = await chunks.describe_all(to_describe, model=model, output_language=output_language, concurrency=_concurrency(settings), cache=previous_cache, artifact_dir=run_dir, stop_check=stop_check, parallel=judge == "jev")
    graph/linker/service.py:533:                if changed_page_rels is not None and not refresh_metadata:
    graph/linker/service.py:542:                previous_roles = {item.chunk_id: [entity.role for entity in item.entities] for item in all_chunks}
    graph/linker/service.py:547:                                   if previous_roles[item.chunk_id] != [entity.role for entity in item.entities])
    graph/linker/service.py:553:            known_team_names = {name_norm for name_norm, _name in catalog.entity_names(team)}
    graph/linker/service.py:558:                    names = sorted({(chunks.normalize_name(entity.name), entity.name)
    graph/linker/service.py:559:                                    for chunk in all_chunks for entity in chunk.entities
    graph/linker/service.py:560:                                    if chunks.normalize_name(entity.name) not in known_team_names})
    graph/linker/service.py:570:            metadata_edges_removed = catalog.delete_edges_for(revised_ids)
    graph/linker/service.py:571:            diff["edges_removed"] = int(diff.get("edges_removed", 0)) + metadata_edges_removed
    graph/linker/service.py:620:                        if _neo_entity_edge_is_valid(catalog, edge):
    graph/linker/service.py:699:            if incremental_scope and on_progress:
    graph/linker/service.py:700:                on_progress({
    graph/linker/service.py:746:                if on_progress:
    graph/linker/service.py:747:                    on_progress({"stage": "linker", "step": "edge_target_done", "document": rel, "current": completed, "total": len(unresolved)})
    graph/linker/service.py:797:            if changed_page_rels is not None and not refresh_metadata:
    graph/linker/service.py:826:                rendered_docs = await render_pages(project, catalog, pages, model=model, settings=settings, mode=mode, on_progress=on_progress)
    graph/linker/service.py:842:            if on_progress:
    graph/linker/service.py:843:                on_progress({"stage": "linker", "step": "done", "document": rel, "edges": len(edge_rows)})
    graph/linker/service.py:847:        if on_progress:
    graph/linker/service.py:848:            on_progress({"stage": "linker", "step": "failed", "document": rel, "error": str(exc)[:200]})
    graph/linker/service.py:857:    on_progress: Progress = None, stop_check: StopCheck = None,
    graph/linker/service.py:867:            on_progress=on_progress, stop_check=stop_check, render=False,
    graph/linker/service.py:883:                touched_docs = await render_pages(project, catalog, pages, model=model, settings=settings, mode=mode, on_progress=on_progress)
    graph/linker/jev_judge.py:49:        entities = [entity for entity in item.entities if entity.role in {"defines", "uses"}]
    graph/linker/jev_judge.py:53:            for entity in entities:
    graph/linker/jev_judge.py:54:                entity.role = "uses"
    graph/linker/jev_judge.py:57:        questions = [JevQuestion(JEV_ROLE_QUESTION.format(name=entity.name), key=str(index)) for index, entity in enumerate(entities)]
    graph/linker/jev_judge.py:60:            for entity, result in zip(entities, results):
    graph/linker/jev_judge.py:61:                entity.role = "defines" if _p(result) >= settings.wiki_linker_role_threshold else "uses"
    graph/linker/jev_judge.py:72:    existing = catalog.entity_names(team)
    graph/linker/jev_judge.py:114:    ids = catalog.entity_chunks(team, name_norm, role="defines")
    graph/linker/jev_judge.py:116:        ids = catalog.entity_chunks(team, name_norm)
    graph/linker/__main__.py:21:        for path in project.wiki.rglob("_planning/metadata.json")
    graph/linker/__main__.py:44:    cfg = wiki_config(settings, run_dir=project.metadata / "state" / document)
    graph/linker/wire.py:24:    entity: str = ""
    graph/linker/neo.py:1:"""Deterministic entity/behaviour candidate discovery for neo mode."""
    graph/linker/neo.py:40:def _entity_names(catalog: Catalog, chunk_id: str) -> set[str]:
    graph/linker/neo.py:59:                selected.append(Candidate(cid, "name_match", [entity.name])); selected_ids.add(cid)
    graph/linker/neo.py:62:    for entity in chunk.entities:
    graph/linker/neo.py:63:        name = normalize_name(entity.name)
    graph/linker/neo.py:65:        if entity.role == "uses":
    graph/linker/neo.py:66:            definers = local(catalog.entity_chunks(team, canon, role="defines", exclude_page=chunk.page_rel))
    graph/linker/neo.py:68:                selected.append(Candidate(definers[0], "use", [entity.name], True, "defines", f"「{entity.name}」の定義")); selected_ids.add(definers[0])
    graph/linker/neo.py:71:                    selected.append(Candidate(definer, "use", [entity.name])); selected_ids.add(definer)
    graph/linker/neo.py:73:            users = local(catalog.entity_chunks(team, canon, role="uses", exclude_page=chunk.page_rel))
    graph/linker/neo.py:75:                selected.append(Candidate(user, "define", [entity.name], True, "uses", f"「{entity.name}」を使用")); selected_ids.add(user)
    graph/linker/neo.py:77:                for definer in catalog.entity_chunks(team, canon, role="defines", exclude_page=chunk.page_rel):
    graph/linker/neo.py:79:                        selected.append(Candidate(definer, "define_define", [entity.name])); selected_ids.add(definer)
    graph/linker/neo.py:80:    entity_names = {normalize_name(item.name) for item in chunk.entities}
    graph/linker/neo.py:87:    for cid, _score in catalog.behaviour_chunks(team, entity_names, exclude_page=chunk.page_rel)[:caps[0]]:
    graph/linker/neo.py:88:        if cid not in obvious and cid not in selected_ids and (not judge or not (_entity_names(catalog, cid) & entity_names)):
    graph/linker/neo.py:89:            selected.append(Candidate(cid, "hop1", [next(iter(entity_names))] if entity_names else [])); selected_ids.add(cid)
    graph/linker/neo.py:92:    for e1 in entity_names:
    graph/linker/neo.py:95:            if not e2 or e2 in entity_names:
    graph/linker/neo.py:99:                if catalog.page_of(cid) != chunk.page_rel and cid not in obvious and cid not in selected_ids and not (_entity_names(catalog, cid) & entity_names):
    graph/linker/neo.py:109:    for e1 in entity_names:
    graph/linker/neo.py:112:            if not e2 or e2 in entity_names:
    graph/linker/neo.py:116:                if not e3 or e3 in entity_names or e3 == e2:
    graph/linker/neo.py:120:                    if catalog.page_of(cid) != chunk.page_rel and cid not in obvious and cid not in selected_ids and not (_entity_names(catalog, cid) & entity_names):
    graph/linker/legacy.py:58:    if chunk.entity.strip():
    graph/linker/legacy.py:59:        for cid in catalog.chunks_with_entity(chunk.entity, team, exclude_page=chunk.page_rel):
    graph/linker/chunks.py:1:"""H2 chunking and one-call metadata extraction."""
    graph/linker/chunks.py:23:# Bounds for one chunk-metadata call: temperature 0 loops on long lists, and without a
    graph/linker/chunks.py:95:    def entity(self) -> str:
    graph/linker/chunks.py:96:        return self.meta.entity
    graph/linker/chunks.py:202:    entity_names = {normalize_name(item.name): item.name for item in entities}
    graph/linker/chunks.py:206:        subject = entity_names.get(normalize_name(item.subject))
    graph/linker/chunks.py:210:        obj = entity_names.get(normalize_name(item.object), "")
    graph/linker/chunks.py:217:        summary=collapse(meta.summary, 1000), keywords=keywords, entity=collapse(meta.entity, 1000),
    graph/linker/chunks.py:285:def _apply_entity_replacements(processed: list[Chunk], entities: list[ChunkEntity]) -> set[str]:
    graph/linker/chunks.py:287:    for entity in entities:
    graph/linker/chunks.py:288:        for old_name in entity.replaces:
    graph/linker/chunks.py:289:            replacements.setdefault(normalize_name(old_name), []).append(entity)
    graph/linker/chunks.py:296:        for entity in chunk.entities:
    graph/linker/chunks.py:297:            choices = replacements.get(normalize_name(entity.name))
    graph/linker/chunks.py:299:                revised.append(entity)
    graph/linker/chunks.py:303:                choice.model_copy(update={"role": entity.role, "replaces": []})
    graph/linker/chunks.py:312:async def describe_all(chunks: list[Chunk], *, model: Any, output_language: str, concurrency: int, cache: dict[str, ChunkMeta] | None = None, artifact_dir: Path | None = None, stop_check: Callable[[], bool] | None = None, parallel: bool = False) -> tuple[int, int]:
    graph/linker/chunks.py:352:            _apply_entity_replacements(processed, item.entities)
    graph/linker/chunks.py:371:                known_entities=[{"name": entity.name, "kind": entity.kind} for entity in registry.values()],
    graph/linker/chunks.py:387:        replaced = {normalize_name(name) for entity in item.entities for name in entity.replaces}
    graph/linker/chunks.py:388:        _apply_entity_replacements(processed, item.entities)
    graph/linker/chunks.py:391:        for entity in item.entities:
    graph/linker/chunks.py:392:            registry[normalize_name(entity.name)] = entity
    graph/linker/chunks.py:397:__all__ = ["Chunk", "RawChunk", "cache_by_hash", "chunk_id", "describe_all", "make_chunks", "meta_text", "model_text", "normalize_name", "snapshot_originals", "split_page", "to_json", "validate_meta"]
    graph/linker/prompts.py:60:        "- 後の記述から既知エンティティが誤り・複合名だったと判明した場合、正しい各 entity の"
    graph/linker/prompts.py:70:        "# entity\nこの節の主要なエンティティまたはトピックを1つ、本文の表記のまま短く書く。\n\n"
    graph/linker/render.py:12:from graph.wiki.page import link_entity_mentions, link_titles, strip_reader_references
    graph/linker/render.py:100:    """True when, seen from this page, the peer chunk defines the entity in ``via``."""
    graph/linker/render.py:107:        entity = edge.via[0]
    graph/linker/render.py:109:            return "", "defines", f"「{entity}」の定義"
    graph/linker/render.py:110:        return "", "uses", f"「{entity}」を使用"
    graph/linker/render.py:145:    entity_paths: set[str] = set()
    graph/linker/render.py:148:        entity_targets: list[tuple[str, str]] = []
    graph/linker/render.py:152:            entity = edge.via[0].strip()
    graph/linker/render.py:153:            key = entity.casefold()
    graph/linker/render.py:154:            if not entity or key in seen_entities:
    graph/linker/render.py:156:            entity_targets.append((entity, relative_link(page_rel, edge.peer_page_rel)))
    graph/linker/render.py:158:        for entity, path in sorted(entity_targets, key=lambda target: -len(target[0])):
    graph/linker/render.py:159:            linked = link_entity_mentions(body, entity, path)
    graph/linker/render.py:160:            if linked != body or f"[{entity}]({path})" in body:
    graph/linker/render.py:162:                entity_paths.add(path)
    graph/linker/render.py:197:    seen_paths: set[str] = set(inline_paths) | entity_paths
    graph/linker/catalog.py:89:              entity TEXT NOT NULL, claims_json TEXT NOT NULL, bridge_probe TEXT NOT NULL,
    graph/linker/catalog.py:97:            CREATE TABLE IF NOT EXISTS entity_canon (team TEXT NOT NULL, name_norm TEXT NOT NULL, canon TEXT NOT NULL, PRIMARY KEY(team,name_norm));
    graph/linker/catalog.py:143:        path = Path(project.metadata) / "wiki-linker.lock"
    graph/linker/catalog.py:217:                """INSERT INTO chunks(chunk_id,page_rel,document,team,ordinal,heading,line_start,line_end,text_sha256,body,summary,keywords_json,entity,claims_json,bridge_probe,entities_json,behaviours_json,vectors_ready)
    graph/linker/catalog.py:218:                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chunk_id) DO UPDATE SET page_rel=excluded.page_rel,document=excluded.document,team=excluded.team,ordinal=excluded.ordinal,heading=excluded.heading,line_start=excluded.line_start,line_end=excluded.line_end,text_sha256=excluded.text_sha256,body=excluded.body,summary=excluded.summary,keywords_json=excluded.keywords_json,entity=excluded.entity,claims_json=excluded.claims_json,bridge_probe=excluded.bridge_probe,entities_json=excluded.entities_json,behaviours_json=excluded.behaviours_json,vectors_ready=CASE WHEN chunks.text_sha256=excluded.text_sha256 THEN chunks.vectors_ready ELSE 0 END""",
    graph/linker/catalog.py:219:                (item.chunk_id, item.page_rel, item.document, item.team, item.ordinal, item.heading, item.line_start, item.line_end, item.text_sha256, item.model_text, item.summary, json.dumps(item.keywords, ensure_ascii=False), item.entity, json.dumps(item.claims, ensure_ascii=False), item.bridge_probe, json.dumps([x.model_dump(mode="json") for x in item.entities], ensure_ascii=False), json.dumps([x.model_dump(mode="json") for x in item.behaviours], ensure_ascii=False), 0),
    graph/linker/catalog.py:224:            for entity in item.entities:
    graph/linker/catalog.py:225:                self.conn.execute("INSERT OR IGNORE INTO entities(name_norm,name,kind,role,chunk_id,team) VALUES(?,?,?,?,?,?)", (normalize_name(entity.name), entity.name, entity.kind, entity.role, item.chunk_id, item.team))
    graph/linker/catalog.py:357:    def chunks_with_entity(self, entity: str, team: str, exclude_page: str = "") -> list[str]:
    graph/linker/catalog.py:358:        rows = self.conn.execute("SELECT c.chunk_id FROM chunks c WHERE c.team=? AND lower(c.entity)=lower(?) AND c.page_rel<>? ORDER BY c.chunk_id", (team, entity, exclude_page)).fetchall()
    graph/linker/catalog.py:361:    def entity_chunks(self, team: str, name_norm: str, *, role: str | None = None, exclude_page: str = "") -> list[str]:
    graph/linker/catalog.py:362:        sql = "SELECT e.chunk_id FROM entities e JOIN chunks c ON c.chunk_id=e.chunk_id LEFT JOIN entity_canon ec ON ec.team=e.team AND ec.name_norm=e.name_norm WHERE e.team=? AND COALESCE(ec.canon,e.name_norm)=? AND c.page_rel<>?"
    graph/linker/catalog.py:370:        row = self.conn.execute("SELECT canon FROM entity_canon WHERE team=? AND name_norm=?", (team, name_norm)).fetchone()
    graph/linker/catalog.py:374:        self.conn.execute("DELETE FROM entity_canon WHERE team=?", (team,))
    graph/linker/catalog.py:375:        self.conn.executemany("INSERT INTO entity_canon(team,name_norm,canon) VALUES(?,?,?)",
    graph/linker/catalog.py:378:    def entity_names(self, team: str) -> list[tuple[str, str]]:
    graph/linker/catalog.py:415:        identity = sorted((a, b))
    graph/linker/catalog.py:417:            identity.append(normalize_name(str(via[0])))
    graph/linker/catalog.py:418:        return "ledge-" + short_hash("\0".join(identity), 20)
                            item for item in all_chunks
                            if item.chunk_id in stale_ids or item.chunk_id in revised_ids
                        ]
                    else:
                        to_describe = [item for item in all_chunks if item.chunk_id in stale_ids or item.chunk_id in revised_ids or not previously_complete]
                if judge == "jev" and jev_engine is not None:
                    from .jev_judge import check_roles
                    previous_roles = {item.chunk_id: [entity.role for entity in item.entities] for item in all_chunks}
                    jev_fallbacks += await check_roles(jev_engine, all_chunks, settings)
                    jev_failed_chunks.update(item.chunk_id for item in all_chunks
                                             if item.entities and item.meta.role_judge != "jev-1")
                    revised_ids.update(item.chunk_id for item in all_chunks
                                       if previous_roles[item.chunk_id] != [entity.role for entity in item.entities])
                chunk_data = chunks.to_json(document, team, all_chunks, id_seed=id_seed)
                chunk_data["raw_rel"] = rel
                for page in chunk_data["pages"]:
                    page["original_sha256"] = original_hashes.get(page["filename"], "")
                write_json_atomic(planning / "chunks.json", chunk_data)
                known_team_names = {name_norm for name_norm, _name in catalog.entity_names(team)}
                catalog.upsert_chunks(all_chunks)
                if judge == "jev" and jev_engine is not None:
                    from .jev_judge import resolve_aliases
                    try:
                        names = sorted({(chunks.normalize_name(entity.name), entity.name)
                                        for chunk in all_chunks for entity in chunk.entities
                                        if chunks.normalize_name(entity.name) not in known_team_names})
                        await resolve_aliases(catalog, jev_engine, team, names, settings)
                    except Exception as exc:
                        jev_fallbacks += 1
                        jev_failed_chunks.update(item.chunk_id for item in all_chunks)
                        log.warning("Jev alias resolution failed for %s: %s", document, exc)
                # A rebuilt catalog has no edges for this document yet; links.json (kept
                # across republish) restores the ones whose endpoint text is unchanged.
                catalog.restore_edges(planning / "links.json")
                revised_peers = {peer for chunk_id in revised_ids for peer in catalog.edge_peers(chunk_id)}
                metadata_edges_removed = catalog.delete_edges_for(revised_ids)
                diff["edges_removed"] = int(diff.get("edges_removed", 0)) + metadata_edges_removed
                if not incremental_scope:
                    catalog.embed_pending(embedder, team=team)
                elif stale_ids:
                    catalog.embed_pending(embedder, team=team, chunk_ids=stale_ids)
                changed_ids = stale_ids | revised_ids
                affected_ids = changed_ids | set(diff["removed"])
                relevant_edges = [
                    edge for edge in old_edges_by_id.values()
                    if str(edge["chunk_a"]) in affected_ids or str(edge["chunk_b"]) in affected_ids
                ]
                visible_edge_pages: dict[str, set[str]] = {}
                if incremental_scope and mode == "neo":
                    edge_documents = {
                        page_rel.rsplit("/", 1)[0]
                        for edge in relevant_edges
                        for chunk_id in (str(edge["chunk_a"]), str(edge["chunk_b"]))
                        for page_rel in [old_page_rels.get(chunk_id, "")]
                        if page_rel
                    }
                    navigation_by_document = {
                        doc: _navigation(project, doc) for doc in edge_documents
                    }
                    for edge in relevant_edges:
                        edge_id = str(edge["edge_id"])
                        for chunk_id in (str(edge["chunk_a"]), str(edge["chunk_b"])):
                            page_rel = old_page_rels.get(chunk_id, "")
                            if not page_rel:
                                continue
                            doc, filename = page_rel.rsplit("/", 1)
                            state = navigation_by_document.get(doc, {}).get("pages", {}).get(filename, {})
                            if any(str(choice.get("edge_id")) == edge_id for choice in state.get("references", [])):
                                visible_edge_pages.setdefault(edge_id, set()).add(page_rel)
    
                edge_rows: list[dict[str, Any]] = []
                incremental_candidate_edges: dict[tuple[str, str], dict[str, Any]] = {}
                candidates_for: list[tuple[Any, list[Candidate]]] = []
                if incremental_scope:
                    grouped: dict[str, tuple[Any, list[Candidate]]] = {}
                    all_by_id = {item.chunk_id: item for item in all_chunks}
                    for edge in relevant_edges:
                        if stop_check and stop_check():
                            raise LinkerCancelled("cancelled during candidates")
                        if str(edge["chunk_a"]) in fresh_ids or str(edge["chunk_b"]) in fresh_ids:
                            continue  # regenerated text: rediscovered below or dropped
                        if catalog.chunk(str(edge["chunk_a"])) is None or catalog.chunk(str(edge["chunk_b"])) is None:
                            continue
                        source = str(edge["source"])
                        if mode == "neo" and source in {"use", "define"}:
                            if _neo_entity_edge_is_valid(catalog, edge):
                                edge_rows.append(edge)
                            continue
                        edge_id = str(edge["edge_id"])
                        # Neo behaviour edges that are not selected on either page are
                        # catalog-only candidates. Keep them without spending an LLM call.
                        if mode == "neo" and edge_id not in visible_edge_pages:
                            edge_rows.append(edge)
                            continue
                        target_id = str(edge["chunk_a"])
                        candidate_id = str(edge["chunk_b"])
                        target = all_by_id.get(target_id)
                        if target is None:
                            target_row = catalog.chunk(target_id)
                            if target_row is None:
                                continue
                            target = _row_chunk(target_row)
                        if catalog.chunk(candidate_id) is None:
                            continue
                        if model is None:
                            edge_rows.append(edge)
                            continue
                        group = grouped.setdefault(target_id, (target, []))[1]
                        group.append(Candidate(
                            candidate_id,
                            source,
                            json.loads(edge["via_json"] or "[]"),
                            label=str(edge["label"]),
                            summary=str(edge["summary"]),
                        ))
                        incremental_candidate_edges[(target_id, candidate_id)] = edge
                    candidates_for = list(grouped.values())
                    for item in all_chunks:
                        if item.chunk_id not in fresh_ids:
                            continue
                        if stop_check and stop_check():
                            raise LinkerCancelled("cancelled during candidates")
                        if mode == "neo":
                            from .neo import candidates as find_candidates
                            found = find_candidates(catalog, item, team=team, settings=settings, judge=judge == "jev")
                        else:
                            from .legacy import candidates as find_candidates
                            found = find_candidates(catalog, item, team=team)
                        candidates_for.append((item, found))
                else:
                    for item in to_describe:
                        if stop_check and stop_check():
                            raise LinkerCancelled("cancelled during candidates")
                        if mode == "neo":
                            from .neo import candidates as find_candidates
                            found = find_candidates(catalog, item, team=team, settings=settings, judge=judge == "jev")
                        else:
                            from .legacy import candidates as find_candidates
                            found = find_candidates(catalog, item, team=team)
                        candidates_for.append((item, found))
                if judge == "jev" and jev_engine is not None and mode == "neo":
                    from .jev_judge import primary_definer
                    for item, found in candidates_for:
                        by_name = {}
                        for candidate in found:
                            if candidate.source == "use" and not candidate.programmatic:
                                by_name.setdefault(catalog.canonical(team, chunks.normalize_name(candidate.via[0])), []).append(candidate)
                        for canon, group in by_name.items():
                            if len(group) < 2:
                                continue
                            try:
                                chosen = await primary_definer(catalog, jev_engine, team, canon,
                                                               [candidate.chunk_id for candidate in group], settings)
                                for candidate in group:
                                    if candidate.chunk_id == chosen:
                                        candidate.programmatic = True
                                        candidate.label = "defines"
                                        candidate.summary = f"「{candidate.via[0]}」の定義"
                                    else:
                                        found.remove(candidate)
                            except Exception as exc:
                                jev_fallbacks += 1
                                jev_failed_chunks.add(item.chunk_id)
                                log.warning("Jev primary definer failed for %s: %s", canon, exc)
                if incremental_scope and on_progress:
                    on_progress({
                        "stage": "linker", "step": "incremental_scope", "document": rel,
                        "changed_chunks": len(changed_ids),
                        "existing_edges": len(relevant_edges),
                        "checked_edges": sum(len(found) for _item, found in candidates_for),
                    })
                unresolved: list[tuple[chunks.Chunk, list[Candidate]]] = []
                for item, found in candidates_for:
                    pending: list[Candidate] = []
                    for candidate in found:
                        row = catalog.chunk(candidate.chunk_id)
                        if row is None:
                            continue
                        if candidate.programmatic:
                            edge_rows.append({"chunk_a": item.chunk_id, "chunk_b": candidate.chunk_id, "label": candidate.label or "related", "summary": candidate.summary, "source": candidate.source, "via": candidate.via, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                            continue
                        decision = catalog.edge_decision_get(item.text_sha256, row["text_sha256"], mode, edge_version)
                        if decision:
                            if decision["accepted"]:
                                previous = incremental_candidate_edges.get((item.chunk_id, candidate.chunk_id))
                                edge_rows.append(previous or {"chunk_a": item.chunk_id, "chunk_b": candidate.chunk_id,
                                                               "label": "related" if judge == "jev" else decision["label"],
                                                               "summary": "" if judge == "jev" else decision["summary"],
                                                               "source": "jev" if judge == "jev" else candidate.source,
                                                               "via": [candidate.source, *candidate.via] if judge == "jev" else candidate.via})
                        elif model is not None:
                            pending.append(candidate)
                    if pending:
                        unresolved.append((item, pending))
                edge_calls = 0
                concurrency = _concurrency(settings)
                semaphore = asyncio.Semaphore(concurrency)
                completed = 0
    
                async def filter_target(item: chunks.Chunk, pending: list[Candidate]) -> tuple[list[dict[str, Any]], int]:
                    nonlocal completed, jev_fallbacks
    """H2 chunking and one-call metadata extraction."""
    
    from __future__ import annotations
    
    import asyncio
    import json
    import re
    import shutil
    import unicodedata
    from dataclasses import dataclass, field
    from pathlib import Path
    from typing import Any, Callable
    
    from graph.clients.chat import extract_json_from_text
    from graph.common.hashing import short_hash
    from graph.common.markdown import strip_big_tables, strip_image_media
    from graph.wiki.page import fence_flags, strip_reader_references
    from graph.wiki.storage import read_json, write_json_atomic, write_text_atomic
    
    from .prompts import CHUNK_META_VERSION, chunk_meta_prompt
    from .wire import ChunkBehaviour, ChunkEntity, ChunkMeta
    
    # Bounds for one chunk-metadata call: temperature 0 loops on long lists, and without a
    # token cap one call could generate until the request timeout. The cap includes thinking.
    META_MAX_TOKENS = 16384  # output only: the search fields (search_terms, claims) have no count cap
    META_TEMPERATURE = 0.7
    
    
    def _is_lead(item: "Chunk", sectioned: set[str]) -> bool:
        """The paragraph above a page's first section: the wiki's own summary of that page."""
        return not item.heading and item.page_rel in sectioned
    
    
    def _lead_meta(item: "Chunk") -> ChunkMeta:
        # Its text already is a summary, and it never defines a name (see jev_judge.check_roles),
        # so an LLM call would only re-extract names the sections below carry anyway.
        body = " ".join(line for line in item.model_text.splitlines() if line.strip() and not line.startswith("#"))
        return ChunkMeta(summary=body or item.title)
    
    
    async def _describe(model: Any, messages: list[Any]) -> ChunkMeta:
        """One plain JSON call; the schema is already in the system prompt.
    
        model.structured first sends a strict-schema request with thinking off, and on real
        chunks that request ran to the token cap every time before its plain-JSON fallback
        answered. Going straight to the plain call halves the calls per chunk.
        """
        error: Exception | None = None
        for _attempt in range(2):
            try:
                raw = await model.text(messages, max_output_tokens=META_MAX_TOKENS, temperature=META_TEMPERATURE)
                return ChunkMeta.model_validate(extract_json_from_text(raw))
            except Exception as exc:  # noqa: BLE001 - one retry, then the caller's fallback
                error = exc
        raise error
    
    def normalize_name(value: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())
    
    
    @dataclass
    class RawChunk:
        ordinal: int
        heading: str
        line_start: int
        line_end: int
        text: str
    
    
    @dataclass
    class Chunk:
        chunk_id: str
        document: str
        team: str
        page_rel: str
        filename: str
        title: str
        ordinal: int
        heading: str
        line_start: int
        line_end: int
        text: str
        text_sha256: str
        meta: ChunkMeta = field(default_factory=ChunkMeta)
    
        @property
        def summary(self) -> str:
            return self.meta.summary
    
        @property
        def keywords(self) -> list[str]:
            return self.meta.keywords
    
        @property
        def entity(self) -> str:
            return self.meta.entity
    
        @property
        def claims(self) -> list[str]:
            return self.meta.claims
    
        @property
        def bridge_probe(self) -> str:
            return self.meta.bridge_probe
    
        @property
        def entities(self) -> list[ChunkEntity]:
            return self.meta.entities
    
        @property
        def behaviours(self) -> list[ChunkBehaviour]:
            return self.meta.behaviours
    
        @property
        def model_text(self) -> str:
            return model_text(self)
    
    
    def split_page(text: str) -> list[RawChunk]:
        lines = text.splitlines()
        flags = fence_flags(lines)
        starts = [i for i, line in enumerate(lines) if not flags[i] and line.startswith("## ")]
        bounds = [0] + starts + [len(lines)]
        chunks: list[RawChunk] = []
        for s, e in zip(bounds, bounds[1:]):
            body = "\n".join(lines[s:e]).strip("\n")
            if not body.strip():
                continue
            heading = lines[s][3:].strip() if s in starts else ""
            chunks.append(RawChunk(len(chunks), heading, s + 1, e, body))
        return chunks
    
    
    def _without_nav(text: str) -> str:
        marker = text.rfind("\n---\n")
        if marker >= 0 and ("前のページ" in text[marker:] or "次のページ" in text[marker:]):
            text = text[:marker]
        return text
    
    
    def model_text(chunk: RawChunk | Chunk | str) -> str:
        text = chunk if isinstance(chunk, str) else chunk.text
        return strip_big_tables(strip_image_media(_without_nav(text)))[:12000]
    
    
    def meta_text(chunk: RawChunk | Chunk | str) -> str:
        """The whole section for chunk_meta: search terms often live in big parameter tables."""
        text = chunk if isinstance(chunk, str) else chunk.text
        return strip_image_media(_without_nav(text))
    
    
    def chunk_id(id_seed: str, filename: str, ordinal: int) -> str:
        return "lchunk-" + short_hash(f"{id_seed}\0{filename}\0{ordinal}", 20)
    
    
    def make_chunks(document: str, team: str, filename: str, text: str, *, id_seed: str | None = None) -> list[Chunk]:
        text = strip_reader_references(text)
        title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ") and line[2:].strip()), Path(filename).stem)
        page_rel = f"{document}/{filename}"
        return [
            Chunk(
                chunk_id=chunk_id(id_seed or document, filename, raw.ordinal), document=document, team=team,
                page_rel=page_rel, filename=filename, title=title, ordinal=raw.ordinal,
                heading=raw.heading, line_start=raw.line_start, line_end=raw.line_end,
                text=raw.text, text_sha256=short_hash(raw.text, 64),
            )
            for raw in split_page(text)
        ]
    
    
    def validate_meta(meta: ChunkMeta, text: str) -> ChunkMeta:
        collapse = lambda value, limit: " ".join((value or "").split())[:limit]
        keywords: list[str] = []
        seen: set[str] = set()
        for value in meta.keywords:
            value = " ".join((value or "").strip().split())
            key = value.casefold()
            if value and key not in seen:
                seen.add(key); keywords.append(value)
            if len(keywords) == 12:
                break
        claims = _unique(collapse(value, 1000) for value in meta.claims)
        entities: list[ChunkEntity] = []
        seen_names: set[str] = set()
        for item in meta.entities:
            name = (item.name or "").strip()
            if len(name) < 2 or not item.kind.strip() or name not in text:
                continue
            key = normalize_name(name)
            if key in seen_names:
                continue
            seen_names.add(key)
            replaces: list[str] = []
            seen_replacements: set[str] = set()
            for old_name in item.replaces:
                old_name = (old_name or "").strip()
                key = normalize_name(old_name)
                if old_name and key != normalize_name(name) and key not in seen_replacements:
                    seen_replacements.add(key)
                    replaces.append(old_name)
            entities.append(item.model_copy(update={"name": name, "kind": item.kind.strip(), "replaces": replaces}))
        entity_names = {normalize_name(item.name): item.name for item in entities}
        behaviours: list[ChunkBehaviour] = []
        seen_behaviours: set[tuple[str, str, str]] = set()
        for item in meta.behaviours:
            subject = entity_names.get(normalize_name(item.subject))
            action = " ".join((item.action or "").split())[:60]
            if not subject or not action:
                continue
            obj = entity_names.get(normalize_name(item.object), "")
            key = (normalize_name(subject), normalize_name(action), normalize_name(obj))
            if key in seen_behaviours:
                continue
            seen_behaviours.add(key)
            behaviours.append(ChunkBehaviour(subject=subject, action=action, object=obj))
        return ChunkMeta(
            summary=collapse(meta.summary, 1000), keywords=keywords, entity=collapse(meta.entity, 1000),
            claims=claims, bridge_probe=collapse(meta.bridge_probe, 1000), entities=entities,
            behaviours=behaviours, role_judge=meta.role_judge, kind=collapse(meta.kind, 1000),
            points=_unique(collapse(value, 1000) for value in meta.points),
    ✓ • 0ms

## Activity

    $ sed -n '470,705p' graph/workspace/writer.py; sed -n '700,815p' graph/workspace/writer.py; rg -n 'def write_wiki_pages|write_wiki_pages\(' graph/workspace/writer.py publisher/pipeline.py
            if path.name in names
        }
    
    
    def _human_edited(state_root: Path) -> list[str]:
        from graph.wiki.storage import read_json
    
        names = []
        for path in sorted((Path(state_root) / "state" / "pages").glob("*.json")):
            state = read_json(path, default={})
            if state.get("human_edited") and state.get("filename"):
                names.append(str(state["filename"]))
        return names
    
    def write_wiki_pages(
        project: Any,
        rel: str,
        *,
        mode: str,
        settings: Any,
        llm: Any,
        embedder: Any,
        on_progress: Progress = None,
        stop_check: StopCheck = None,
        resume: bool = True,
        identity_seed: str | None = None,
    ) -> WriteResult:
        from graph.formats import kind_of, supports_page_updates
        from graph.formats.xlsx import decide_update as decide_workbook
        from graph.wiki.export import export_ingest_layout
        from graph.wiki.incremental import FULL_MIN_REGEN_SHARE, UpdateDecision, apply_update, decide_update, drop_pages
        from graph.wiki.pipeline import ResumeUnavailable
        from graph.wiki.storage import read_json, write_json_atomic, write_text_atomic
    
        kind = kind_of(rel)
        state_root = Path(project.state_dir(rel))
        old_source = state_root / "source" / "original.md"
        new_text = project.raw_file(rel).read_text(encoding="utf-8")
        old_text = ""
        workbook = None
        if not resume:
            decision = UpdateDecision(tier=3, reason="forced")
        elif mode == "wiki" and kind == "xlsx" and (workbook := decide_workbook(state_root, new_text, project.wiki_dir(rel))):
            decision = workbook[0]
        elif mode != "wiki" or not supports_page_updates(kind):
            decision = UpdateDecision(tier=3, reason="format-full-only")
        elif not old_source.exists() or not (state_root / "state" / "plan.json").exists():
            decision = UpdateDecision(tier=3, reason="no-previous-state")
        else:
            old_text = old_source.read_text(encoding="utf-8")
            if not _wiki_run_complete(state_root):
                decision = UpdateDecision(
                    tier=3,
                    reason=(
                        "interrupted-initial-build"
                        if old_text == new_text
                        else "incomplete-previous-state"
                    ),
                )
            else:
                decision = decide_update(
                    state_root, old_text, new_text, kind=kind,
                    structure_target_lines=int(getattr(settings, "structure_target_lines", 250)),
                    structure_min_lines=int(getattr(settings, "structure_min_lines", 40)),
                    pdf_use_headings=bool(getattr(settings, "pdf_use_headings", False)),
                )
    
        wiki_document = Path(project.wiki_dir(rel)).relative_to(project.wiki)
        # Old reverse-sync versions contaminated generator state. Save complete
        # legacy pages before discarding that state, then rebuild a pure ancestor.
        legacy_pages = _human_edited(state_root)
        if legacy_pages:
            from publisher.human_changes import HumanStore
    
            store = HumanStore(project)
            for name in legacy_pages:
                page = project.wiki_dir(rel) / name
                if not page.exists():
                    page = state_root / "wiki" / name
                store.pin_legacy(rel, (wiki_document / name).as_posix(), page.read_text(encoding="utf-8"))
            decision = UpdateDecision(tier=3, reason="legacy-human-state")
            workbook = None
        else:
            from publisher.human_changes import HumanStore
    
            if HumanStore(project).document(rel).get("requires_pure_rebuild"):
                decision = UpdateDecision(tier=3, reason="legacy-missing-ancestor")
                workbook = None
    
        def decision_event() -> dict[str, Any]:
            pages = set(decision.patch) | decision.regenerate | set(decision.retitle)
            return {
                "stage": "wiki", "step": "update_decision", "file": rel,
                **decision.summary(),
                "changed_pages": sorted((wiki_document / name).as_posix() for name in pages),
            }
    
        if on_progress:
            on_progress(decision_event())
        # A stopped first build has already written its source snapshot and may
        # contain expensive observation/planning/rewrite checkpoints even though
        # plan.json does not exist yet.  That is different from an ordinary full
        # rebuild: keep the run directory and let run_pipeline validate/reuse its
        # input-keyed checkpoints.  Previously the tier-3 path deleted state_root
        # here, so queue --continue restarted an interrupted initial build from
        # zero before the wiki pipeline ever saw resume=True.
        resume_initial_build = (
            resume
            and decision.reason in {"no-previous-state", "interrupted-initial-build"}
            and old_source.exists()
            and old_source.read_text(encoding="utf-8") == new_text
        )
        names = _plan_filenames(state_root)
        before = _page_hashes(state_root / "wiki", names)
        human: list[str] = []
        work = project.work_dir(rel)
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        try:
            out_dir: Path | None = None
            if workbook is not None and decision.tier in (0, 2):
                # A workbook keeps state only for its 解説 run: sheet pages are re-rendered and
                # the run resumes every 解説 page except the dropped ones. Unchanged: keep all.
                if decision.reason != "unchanged":
                    human = apply_update(state_root / "excel-story", decision, workbook[1])
                    out_dir = build_wiki_output(
                        source_path=project.raw_file(rel), document_name=rel, out_dir=work / "out",
                        mode=mode, settings=settings, llm=llm, embedder=embedder,
                        state_dir=state_root, on_progress=on_progress, stop_check=stop_check,
                        source_kind=kind, resume=True,
                    ).out_dir
            elif decision.tier in (0, 1, 2):
                human = apply_update(state_root, decision, new_text)
                write_text_atomic(old_source, new_text)
                failed: dict[str, str] = {}
                if decision.patch:
                    _changed, failed = _apply_incremental_edits(
                        state_root, old_text, new_text, decision,
                        settings=settings, llm=llm, stop_check=stop_check,
                    )
                if failed:
                    plan_pages = read_json(state_root / "state" / "plan.json")["pages"]
                    human += drop_pages(state_root, plan_pages, set(failed), research=set())
                    decision.regenerate |= set(failed)
                    decision.tier, decision.reason = 2, "patch-escalated"
                    if on_progress:
                        on_progress({"stage": "wiki", "step": "patch_escalated", "file": rel, "pages": sorted(failed), "errors": failed})
                if len(decision.regenerate) > FULL_MIN_REGEN_SHARE * max(len(names), 1):
                    decision.tier, decision.reason = 3, "escalated-most-pages"
                elif decision.regenerate:
                    try:
                        out_dir = build_wiki_output(
                            source_path=project.raw_file(rel), document_name=rel, out_dir=work / "out",
                            mode=mode, settings=settings, llm=llm, embedder=embedder,
                            state_dir=state_root, on_progress=on_progress, stop_check=stop_check,
                            source_kind=kind, resume=True, require_resume=True,
                        ).out_dir
                    except ResumeUnavailable:
                        decision = UpdateDecision(tier=3, reason="resume-failed")
                else:
                    out_dir = export_ingest_layout(state_root, work / "out", document_name=rel)
            if decision.tier == 3:
                if decision.reason not in {
                    "forced", "format-full-only", "no-previous-state",
                    "interrupted-initial-build", "incomplete-previous-state",
                } and on_progress:
                    on_progress(decision_event())
                human = sorted(set(human) | set(_human_edited(state_root)))
                if not resume_initial_build:
                    shutil.rmtree(state_root, ignore_errors=True)
                out_dir = build_wiki_output(
                    source_path=project.raw_file(rel), document_name=rel, out_dir=work / "out",
                    mode=mode, settings=settings, llm=llm, embedder=embedder,
                    state_dir=state_root, on_progress=on_progress, stop_check=stop_check,
                    source_kind=kind, resume=resume_initial_build,
                ).out_dir
            # A rebuilt workbook republishes and relinks as a whole (the linker's caches make
            # unchanged sheets cheap); only its 解説 regeneration is incremental.
            rebuild = "full" if decision.tier == 3 or (workbook is not None and out_dir is not None) else "incremental"
            after = _page_hashes(state_root / "wiki", _plan_filenames(state_root)) if rebuild == "incremental" else {}
            changed_output_pages = {name for name, digest in after.items() if before.get(name) != digest}
            target = project.wiki_dir(rel)
            if out_dir is not None:
                publish_output(out_dir, target)
            write_source_stamp(target, project.raw_file(rel), rel, identity_seed=identity_seed)
            from publisher.human_changes import HumanStore, apply_generated
    
            if out_dir is not None:
                overlay = apply_generated(project, rel)
            else:
                store = HumanStore(project)
                store.ensure_generated(rel)
                overlay = store.render(rel)
            changed_output_pages.update(Path(path).name for path in overlay.changed_pages)
            marker = target / "_planning" / "linker.json"
            status = "pending" if getattr(settings, "wiki_linker_enabled", True) else "disabled"
            previous_linker = read_json(marker, default={})
            mode_name = str(getattr(settings, "wiki_linker_mode", "legacy"))
            marker_data = {"schema_version": 2, "status": status, "mode": mode_name}
            keep_linker = (
                rebuild == "incremental"
                and previous_linker.get("status") == "complete"
                and previous_linker.get("mode") == mode_name
                and not changed_output_pages
            )
            if status == "pending" and keep_linker:
                marker_data = previous_linker
            elif rebuild == "incremental" and previous_linker.get("status") == "complete" and previous_linker.get("mode") == mode_name:
                marker_data["resume"] = True
            write_json_atomic(marker, marker_data)
            document = target.relative_to(project.wiki)
            if (overlay.conflicts or overlay.orphaned) and on_progress:
                on_progress({"stage": "wiki", "step": "human_overlay", "file": rel,
                             "conflicts": overlay.conflicts, "orphaned": overlay.orphaned})
            return WriteResult(
                target=target,
                touched=[],
                rebuild=rebuild,
                changed_pages=sorted((document / name).as_posix() for name in changed_output_pages),
                tier=decision.tier,
                reason=decision.reason,
                regenerated_pages=sorted((document / name).as_posix() for name in decision.regenerate) if rebuild == "incremental" else [],
                human_edits_overwritten=[],
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)
    def write_wiki(
        project: Any, rel: str, *, mode: str, settings: Any, llm: Any, embedder: Any,
        on_progress: Progress = None, stop_check: StopCheck = None,
    ) -> WriteResult:
        """Compatibility wrapper: generate one wiki and immediately link it."""
        result = write_wiki_pages(
            project, rel, mode=mode, settings=settings, llm=llm, embedder=embedder,
            on_progress=on_progress, stop_check=stop_check,
        )
    ) -> WriteResult:
        """Compatibility wrapper: generate one wiki and immediately link it."""
        result = write_wiki_pages(
            project, rel, mode=mode, settings=settings, llm=llm, embedder=embedder,
            on_progress=on_progress, stop_check=stop_check,
        )
        touched = run_linkers(
            project, [rel], settings=settings, llm=llm, embedder=embedder,
            on_progress=on_progress, stop_check=stop_check,
        )
        return WriteResult(target=result.target, touched=touched)
    
    
    def run_linker(
        project: Any,
        rel: str,
        *,
        settings: Any,
        llm: Any,
        embedder: Any,
        on_progress: Progress = None,
        stop_check: StopCheck = None,
    ) -> list[str]:
        """Run the sole cross-document linker after pristine pages are published."""
    
        from graph.wiki.storage import write_json_atomic
    
        marker_path = project.wiki_dir(rel) / "_planning" / "linker.json"
        if not getattr(settings, "wiki_linker_enabled", True):
            # disabled mode constructs no services and never opens the catalog
            write_json_atomic(marker_path, {"schema_version": 2, "status": "disabled"})
            return []
    
        from graph.common.async_tools import run_async_blocking
        from graph.wiki.model import ChatModelPort
    
        if hasattr(llm, "structured"):
            model = llm  # already a model port (the linker only needs structured()); no services to construct
        else:
            config = wiki_config(settings, run_dir=project.state_dir(rel))
            model = ChatModelPort(config, llm=llm)
        if embedder is None:
            try:
                from graph.clients.embeddings import Embedder
    
                embedder = Embedder(settings)
            except Exception as exc:
                embedder = None
                if on_progress:
                    on_progress({"stage": "linker", "step": "embedder_unavailable", "error": str(exc)[:200]})
        from graph.linker import link_document
        result = run_async_blocking(link_document(project, rel, model=model, embedder=embedder, settings=settings, on_progress=on_progress, stop_check=stop_check))
        return result.touched_documents
    
    
    run_wiki_linker = run_linker
    
    
    def run_linkers(
        project: Any, rels: list[str], *, settings: Any, llm: Any, embedder: Any,
        on_progress: Progress = None, stop_check: StopCheck = None,
        affected_pages: set[str] | None = None,
        regenerated_pages: set[str] | None = None,
    ) -> list[str]:
        """Link a completed wiki batch and render affected pages once."""
        if not rels:
            return []
        from graph.wiki.storage import write_json_atomic
        if not getattr(settings, "wiki_linker_enabled", True):
            for rel in rels:
                write_json_atomic(project.wiki_dir(rel) / "_planning" / "linker.json", {"schema_version": 2, "status": "disabled"})
            return []
        from graph.common.async_tools import run_async_blocking
        from graph.linker import link_documents
        from graph.wiki.model import ChatModelPort
        model = llm if hasattr(llm, "structured") else ChatModelPort(wiki_config(settings, run_dir=project.metadata / "state" / "linker"), llm=llm) if llm is not None else None
        changed_pages = set(affected_pages) if affected_pages else None
        result = run_async_blocking(link_documents(
            project, rels, model=model, embedder=embedder, settings=settings,
            on_progress=on_progress, stop_check=stop_check, changed_pages=changed_pages,
            regenerated_pages=set(regenerated_pages) if regenerated_pages else None,
        ))
        if affected_pages is not None:
            affected_pages.update(result.affected_pages or [])
        return result.touched_documents
    
    
    def _sha256_file(path: Path) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    
    
    def write_source_stamp(target: Path, raw_file: Path, rel: str, *, identity_seed: str | None = None) -> None:
        planning = Path(target) / "_planning"
        planning.mkdir(exist_ok=True)
        tmp = planning / "source.json.tmp"
        tmp.write_text(json.dumps({"raw": rel, "sha256": _sha256_file(raw_file), "id_seed": identity_seed or rel}), encoding="utf-8")
        tmp.replace(planning / "source.json")
    
    
    def wiki_up_to_date(project: Any, rel: str) -> bool:
        planning = project.wiki_dir(rel) / "_planning"
        raw = project.raw_file(rel)
        current = _sha256_file(raw) if raw.exists() else ""
        for name, key in (("source.json", "sha256"), ("manifest.json", "source_sha256")):
            path = planning / name
            if not path.exists():
                continue
            try:
                if json.loads(path.read_text(encoding="utf-8")).get(key) != current:
                    return False
            except (OSError, ValueError):
                return False
            return True
        return False
    
    
    publisher/pipeline.py:918:                    result = write_wiki_pages(
    publisher/pipeline.py:1620:                result = write_wiki_pages(project, rel, mode=str(settings.ingest_mode), settings=settings, llm=model, embedder=None, on_progress=on_progress, resume=not force)
    graph/workspace/writer.py:484:def write_wiki_pages(
    graph/workspace/writer.py:702:    result = write_wiki_pages(
    ✓ • 0ms

## Activity

    $ sed -n '630,870p' graph/growi/client.py; sed -n '870,1010p' graph/growi/client.py; rg -n 'publish_documents|publish_pages' graph/growi/client.py publisher/pipeline.py
                or normalized == own_boundary
                or normalized.startswith(own_boundary.rstrip("/") + "/")
            ):
                raise PermissionError(
                    f"own-mode GROWI write outside root_path: {normalized} not under {own_boundary}"
                )
    
    
    def _marked_sections(body: str) -> dict[str, tuple[int, int]]:
        matches = list(_CHUNK_MARKER_RE.finditer(body))
        sections: dict[str, tuple[int, int]] = {}
        for index, match in enumerate(matches):
            boundary = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            end = _CHUNK_END_RE.search(body, match.end(), boundary)
            marker_id = _marker_id(match)
            sections[marker_id] = (
                match.start(),
                end.end() if end and _marker_id(end) == marker_id else boundary,
            )
        return sections
    
    
    def merge_marked_sections(existing: str, additions: str) -> str:
        """Replace the page above our stamp, keeping any text a human added below it."""
        if not _page_stamps(additions):
            raise ValueError("GROWI publish body contains no chunk markers")
        stamps = _page_stamps(existing)
        if stamps:
            tail = existing[stamps[-1].end():].strip("\n")
            return additions.rstrip() + ("\n\n" + tail + "\n" if tail else "\n")
        if existing.strip() and not _CHUNK_MARKER_RE.search(existing):
            # Somebody else's page: publish alongside it instead of overwriting it.
            return existing.rstrip() + "\n\n" + additions.rstrip() + "\n"
        # A page we published in the older chunk-marker format is wholly ours to rewrite.
        return additions
    
    
    def _complete_page(page: GrowiPage, path: str, body: str) -> GrowiPage:
        if page.path and page.body:
            return page
        return page.model_copy(update={"path": page.path or path, "body": page.body or body})
    
    
    async def publish_pages(
        client: GrowiClient,
        pages: list[dict[str, str]],
        *,
        mode: str,
        write_path: str,
        root_path: str = "/",
        known_page_ids: dict[str, str] | None = None,
        expected_pages: dict[str, dict[str, Any]] | None = None,
        on_revision: Any = None,
        on_conflict: Any = None,
        on_prepared: Any = None,
        on_confirmed: Any = None,
        on_reconcile: Any = None,
    ) -> list[GrowiPage]:
        """Resolve every page ID first, then publish stable permalink bodies."""
        current: dict[str, GrowiPage | None] = {}
        # Inspect the whole batch before creating/updating any page. The following
        # PUT still uses this exact revision, so a later race fails with HTTP 409.
        for item in pages:
            path = item["path"]
            body = item["body"]
            assert_publish_path(path, mode=mode, write_path=write_path, root_path=root_path)
            expected = (expected_pages or {}).get(item.get("local_path", ""))
            existing = await client.get_page(**({"page_id": str(expected["page_id"])} if expected else {"path": path}))
            if expected:
                if existing is None:
                    raise RuntimeError(f"GROWI page disappeared before publication: {path}")
                if existing.path != path or existing.page_id != expected.get("page_id"):
                    raise RuntimeError(f"GROWI page moved before publication: {path}")
                if not expected.get("revision_id") or existing.revision_id != expected["revision_id"]:
                    raise RuntimeError(f"GROWI page changed before publication: {path}")
                if managed_page_markdown(existing.body, str(expected.get("marker_id") or "")) is None:
                    raise RuntimeError(f"GROWI ownership marker missing before publication: {path}")
            elif existing is not None:
                stamps = _page_stamps(body)
                marker = stamps[0] if len(stamps) == 1 else None
                if marker is None or managed_page_markdown(existing.body, marker.group("id")) is None:
                    await _maybe_await(on_conflict, item, existing, "unowned_destination")
                    raise RuntimeError(f"GROWI destination is not owned by this page: {path}")
                initial = _growi_markdown(_image_fallbacks(body))
                if merge_marked_sections(existing.body, initial) != existing.body:
                    if not await _maybe_await(on_reconcile, item, existing):
                        await _maybe_await(on_conflict, item, existing, "missing_published_snapshot")
                        raise RuntimeError(f"GROWI destination has no inspected published baseline: {path}")
            current[path] = existing
        for item in pages:
            path, body = item["path"], item["body"]
            existing = current[path]
            if existing is None:
                initial_body = _growi_markdown(_image_fallbacks(body))
                await _maybe_await(on_prepared, item, None, initial_body)
                existing = _complete_page(await client.create_page(path, initial_body), path, initial_body)
                await _maybe_await(on_confirmed, item, existing, initial_body)
                await _maybe_await(on_revision, existing)
            if not existing.page_id:
                raise ValueError(f"GROWI returned no page ID for {path}")
            current[path] = existing
    
        page_ids = dict(known_page_ids or {})
        page_ids.update({
            item["local_path"]: current[item["path"]].page_id
            for item in pages
            if item.get("local_path") and current[item["path"]].page_id
        })
        results: list[GrowiPage] = []
        for item in pages:
            path = item["path"]
            existing = current[path]
            body = rewrite_page_links(item["body"], item.get("local_path", ""), page_ids)
            body = await _publish_images(client, body, existing.page_id)
            merged = merge_marked_sections(existing.body, _growi_markdown(body))
            if merged == existing.body:
                results.append(existing)
                await _maybe_await(on_revision, existing)
                continue
            await _maybe_await(on_prepared, item, existing, merged)
            try:
                page = _complete_page(
                    await client.update_page(existing.page_id, existing.revision_id, merged), path, merged
                )
            except GrowiAPIError as exc:
                if exc.status_code == 409:
                    observed = await client.get_page(page_id=existing.page_id)
                    await _maybe_await(on_conflict, item, observed, "revision_race")
                raise
            results.append(page)
            await _maybe_await(on_confirmed, item, page, merged)
            await _maybe_await(on_revision, page)
        return results
    
    
    class GrowiPublisher:
        """Publish one generated document and trash only pages marked by us."""
    
        def __init__(
            self,
            client: GrowiClient,
            connection: Any,
            *,
            human_sync_policy: Any = None,
            semantic_assistant: Any = None,
            semantic_assistant_factory: Any = None,
        ) -> None:
            from graph.config import HumanSyncPolicy
    
            self.client = client
            self.connection = connection
            self.on_revision: Any = None
            # Runtime construction always supplies Settings.  The compatibility
            # default keeps direct deterministic callers on their historic path.
            self.human_sync_policy = human_sync_policy or HumanSyncPolicy.resolve("apply")
            self.semantic_assistant = semantic_assistant
            self.semantic_assistant_factory = semantic_assistant_factory
            self.human_sync_summary: dict[str, Any] = {}
    
        def doc_path(self, project: Any, rel: str) -> str:
            folder = project.wiki_dir(rel).relative_to(project.wiki).as_posix()
            return growi_path(self.connection.write_path, folder)
    
        def page_marker_seed(self, project: Any, local_path: str) -> str:
            """The readable page identity behind a page's stamp ID (the local half of the map)."""
            document = Path(local_path).parent.as_posix()
            marker = read_json(Path(project.wiki) / document / "_planning" / "source.json", default={})
            id_seed = str(marker.get("id_seed") or document)
            return growi_path(self.connection.write_path, id_seed, Path(local_path).name)
    
        def page_marker_id(self, project: Any, local_path: str) -> str:
            seed = self.page_marker_seed(project, local_path)
            return "b" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    
        def _document_pages(self, project: Any, rel: str) -> list[dict[str, str]]:
            folder = project.wiki_dir(rel)
            doc_path = self.doc_path(project, rel)
            pages: list[dict[str, str]] = []
            for md in sorted(folder.glob("*.md")):
                name = growi_segment(md.name)
                from publisher.human_changes import map_generated
    
                body = map_generated(md.read_text(encoding="utf-8"), strip_reader_references)
                page_path = f"{doc_path}/{name}"
                local_path = md.relative_to(project.wiki).as_posix()
                page_id = self.page_marker_id(project, local_path)
                pages.append({
                    "local_path": local_path,
                    "path": page_path,
                    "body": wrap_page(body, page_id=page_id),
                })
            return pages
    
        def publish_documents(
            self,
            project: Any,
            rels: list[str],
            known_pages: dict[str, dict[str, Any]] | None = None,
            only_pages: set[str] | None = None,
            cleanup_revisions: dict[str, str] | None = None,
        ) -> dict[str, GrowiPage]:
            from publisher.human_changes import HumanStore
    
            HumanStore(project).audit()
            pages: list[dict[str, str]] = []
            document_paths: dict[str, set[str]] = {}
            for rel in dict.fromkeys(rels):
                document_pages = self._document_pages(project, rel)
                if only_pages is not None:
                    document_pages = [
                        page for page in document_pages if page["local_path"] in only_pages
                    ]
                # Newest page first: GROWI lists by last-updated, so 001 lands on top (00-目次 follows after).
                pages.extend(reversed(document_pages))
                document_paths[self.doc_path(project, rel)] = {page["path"] for page in document_pages}
            if len({page["path"] for page in pages}) != len(pages):
                raise ValueError("multiple local wiki pages resolve to the same GROWI path")
            scoped = {
                path: row for path, row in (known_pages or {}).items()
                if any(path.startswith(project.wiki_dir(rel).relative_to(project.wiki).as_posix() + "/") for rel in rels)
            }
            self.assert_known_revisions(scoped)
            def record_conflict(item: dict, observed: GrowiPage | None, reason: str) -> None:
                from publisher.human_changes import HumanStore
    
                if observed is None:
                    return
                store = HumanStore(project)
                marker = self.page_marker_id(project, item["local_path"])
                data = store.page(marker)
                data.update({"schema_version": 1, "marker_id": marker,
                             "observed_revision": observed.revision_id,
                             "observed_remote_blob": store.put(observed.body), "publication_error": reason,
                             "publication_error_attempt_id": str(data.get("prepared_attempt_id") or "")})
                attempt_id = str(data.get("prepared_attempt_id") or "")
                for attempt in data.get("attempt_history", []):
                    if attempt.get("attempt_id") == attempt_id:
                        attempt.update({"status": "rejected" if reason == "revision_race" else "failed",
                                        "error": reason, "observed_revision": observed.revision_id})
                store.save_page(data)
    
    
            def record_prepared(item: dict, inspected: GrowiPage | None, body: str) -> None:
                store = HumanStore(project)
                marker = self.page_marker_id(project, item["local_path"])
                data = store.page(marker)
                stamp = read_json(project.wiki / Path(item["local_path"]).parent / "_planning" / "source.json", default={})
                document = store.document(str(stamp["raw"])) if stamp.get("raw") else {}
                generated = document.get("pages", {}).get(Path(item["local_path"]).name, {})
                remote_blob = store.put(body)
                local_blob = store.put((project.wiki / item["local_path"]).read_text(encoding="utf-8"))
                attempt_id = "hattempt-" + uuid.uuid4().hex[:24]
                history = data.setdefault("attempt_history", [])
                previous_attempt = str(data.get("prepared_attempt_id") or "")
                for attempt in history:
                    if attempt.get("attempt_id") == previous_attempt and attempt.get("status") not in {
                        "recovered_exact", "captured_late_human", "not_landed",
                    }:
                        attempt.update({"status": "superseded", "settled_at": datetime.now(timezone.utc).isoformat()})
                history.append({
                    "attempt_id": attempt_id,
                    "status": "prepared",
                    "path": item["path"],
                    "page_id": inspected.page_id if inspected else "",
                    "revision": inspected.revision_id if inspected else "",
                    "remote_blob": remote_blob,
                    "local_blob": local_blob,
                    "generated_blob": str(generated.get("body_blob") or ""),
                    "prepared_at": datetime.now(timezone.utc).isoformat(),
                })
                data.update({"schema_version": 1, "marker_id": marker, "local_path": item["local_path"],
                             "prepared_attempt_id": attempt_id,
                             "prepared_path": item["path"], "prepared_page_id": inspected.page_id if inspected else "",
                             "prepared_revision": inspected.revision_id if inspected else "",
                             # A fresh attempt starts without a conflict, so a recorded
                             # publication_error always describes this prepared write.
                             "publication_error": "", "publication_error_attempt_id": "",
                             "prepared_remote_blob": remote_blob,
                             "prepared_local_blob": local_blob,
                             # Effective local text can contain protected human regions;
                             # recovery must use the writer's actual pure generated page.
                             "prepared_generated_blob": str(generated.get("body_blob") or "")})
                store.save_page(data)
    
            def record_confirmed(item: dict, page: GrowiPage, body: str) -> None:
                """Persist per-page success before the next page can be mutated."""
                store = HumanStore(project)
                marker = self.page_marker_id(project, item["local_path"])
                data = store.page(marker)
                attempt_id = str(data.get("prepared_attempt_id") or "")
                if not attempt_id:
                    return
                confirmation = {
                    "attempt_id": attempt_id,
                    "page_id": page.page_id,
                    "revision": page.revision_id,
                    "path": page.path,
                    "remote_blob": store.put(body),
                    "confirmed_at": datetime.now(timezone.utc).isoformat(),
                }
                data["publication_confirmation"] = confirmation
                for attempt in data.get("attempt_history", []):
                    if attempt.get("attempt_id") == attempt_id:
                        attempt.update({"status": "confirmed", **confirmation})
                store.save_page(data)
    
            def reconcile_prepared(item: dict, observed: GrowiPage) -> bool:
                """True when an unledgered page already holds exactly the body we prepared."""
                from publisher.human_changes import HumanStore
    
                return bool(HumanStore(project).prepared_match(self.page_marker_id(project, item["local_path"]), observed))
    
            results = asyncio.run(publish_pages(
                self.client,
                pages,
                mode=self.connection.mode,
                write_path=self.connection.write_path,
                root_path=self.connection.root_path,
                known_page_ids={
                    path: str(row.get("page_id"))
                    for path, row in (known_pages or {}).items()
                    if row.get("page_id")
                },
                expected_pages=scoped,
                on_revision=self.on_revision,
                on_conflict=record_conflict,
                on_prepared=record_prepared,
                on_confirmed=record_confirmed,
                on_reconcile=reconcile_prepared,
            ))
            if only_pages is None:
                for doc_path, keep in document_paths.items():
                    asyncio.run(self._trash_under(doc_path, keep=keep, expected_revisions={
                        **(cleanup_revisions or {}),
                        **{str(row["page_id"]): str(row["revision_id"]) for row in scoped.values()},
                    }, on_deleted=lambda page: self._remember_deleted(project, scoped, page)))
            return {item["local_path"]: page for item, page in zip(pages, results)}
    
        def _remember_deleted(self, project: Any, known_pages: dict[str, dict[str, Any]], page: GrowiPage) -> None:
            from publisher.human_changes import HumanStore, now
    
            item = next(((path, row) for path, row in known_pages.items() if row.get("page_id") == page.page_id), None)
            if item is None:
                return
            local_path, row = item
            store = HumanStore(project)
            marker = str(row.get("marker_id") or self.page_marker_id(project, local_path))
            data = store.page(marker)
            data.update({"schema_version": 1, "marker_id": marker, "deleted_page_id": page.page_id,
                         "deleted_revision": page.revision_id, "deleted_path": page.path,
                         "deleted_remote_blob": store.put(page.body), "deleted_at": now()})
            store.save_page(data)
    
        def discover_documents(self, project: Any, rels: list[str]) -> dict[str, GrowiPage]:
            pages = [page for rel in dict.fromkeys(rels) for page in self._document_pages(project, rel)]
    
            async def fetch() -> list[GrowiPage | None]:
                return [await self.client.get_page(path=page["path"]) for page in pages]
    
            return {
                item["local_path"]: page
                for item, page in zip(pages, asyncio.run(fetch()))
                if page is not None
            }
    
        def assert_known_revisions(self, pages: dict[str, dict[str, Any]]) -> None:
            async def check() -> None:
                for local_path, row in pages.items():
                    page_id = str(row.get("page_id") or "")
                    revision_id = str(row.get("revision_id") or "")
                    if not page_id or not revision_id:
                        raise RuntimeError(f"cannot verify GROWI revision for {local_path}")
                    current = await self.client.get_page(page_id=page_id)
                    if current is None:
                        raise RuntimeError(f"GROWI page disappeared: {local_path}")
                    if row.get("growi_path") and current.path != row["growi_path"]:
                        raise RuntimeError(f"GROWI page moved by another editor: {current.path}")
                    if row.get("marker_id") and managed_page_markdown(current.body, str(row["marker_id"])) is None:
                        raise RuntimeError(f"GROWI ownership markers were removed: {current.path}")
                    if current.revision_id != revision_id:
                        raise RuntimeError(f"GROWI page changed by another editor: {current.path}")
    
    publisher/pipeline.py:599:            pages = publisher.publish_documents(
    publisher/pipeline.py:1507:        restored_pages = publisher.publish_documents(live, base_rels, known_pages=asyncio.run(inspected_rows()), **extra_args)
    graph/growi/client.py:673:async def publish_pages(
    graph/growi/client.py:823:    def publish_documents(
    graph/growi/client.py:941:        results = asyncio.run(publish_pages(
    graph/growi/client.py:1014:        return list(self.publish_documents(project, [rel]).values())
    graph/growi/client.py:1076:        published = self.publish_documents(project, [new_rel], known_pages={
    ✓ • 19ms

## Activity

    $ rg -n 'def _progress_heartbeat|progress_heartbeat' publisher/pipeline.py; sed -n '390,470p' publisher/pipeline.py; sed -n '1060,1285p' publisher/pipeline.py
    46:def _progress_heartbeat(
    874:                with _progress_heartbeat(on_progress, stage="parse", file=rel):
    917:                with _progress_heartbeat(on_progress, stage="wiki", file=raw_rel):
    988:                with _progress_heartbeat(on_progress, stage="linker", file="batch"):
        from .activity import ActivityDetector
        from .human_changes import HumanStore
        from graph.growi.client import _page_stamps
    
        if not hasattr(publisher.client, "list_activities") or not hasattr(publisher.client, "list_all_pages"):
            pulled, failures, blocked = _capture_remote(project, ledger, publisher)
            return pulled, failures, blocked, {
                "fallback_reason": "activity_api_not_supported_by_client",
                "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
            }
    
        detector = ActivityDetector(
            project,
            endpoint=_growi_url(settings),
            boundary=str(publisher.connection.root_path),
            overlap_seconds=int(getattr(settings, "human_sync_activity_overlap_seconds", 60)),
        )
        batch = detector.poll(publisher.client, ledger.published_pages)
        inventory = None
        try:
            cursor = detector.cursor()
        except (OSError, ValueError, TypeError):
            cursor = {}
        last_inventory = str(cursor.get("last_inventory_at") or "")
        audit_seconds = int(getattr(settings, "human_sync_activity_audit_seconds", 3600))
        audit_due = not last_inventory
        if last_inventory and audit_seconds > 0:
            try:
                from datetime import datetime, timezone
    
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_inventory.replace("Z", "+00:00"))).total_seconds()
                audit_due = age >= audit_seconds
            except ValueError:
                audit_due = True
        discovery_failures: list[str] = []
        if force_inventory or batch.fallback_reason or batch.unknown_page_ids or audit_due:
            # A reset cursor must point to the beginning of the inventory window.
            # An edit that lands while the inventory is running will then remain in
            # the next activity overlap instead of being skipped by a post-scan
            # "now" timestamp.
            from datetime import datetime, timezone
    
            inventory_anchor_at = datetime.now(timezone.utc).isoformat()
            inventory = detector.inventory(publisher.client, ledger.published_pages)
            batch.selected_page_ids.update(inventory.selected_page_ids)
            store = HumanStore(project)
            for page in inventory.discovered_pages:
                stamps = _page_stamps(page.body)
                marker = stamps[0].group("id") if len(stamps) == 1 else ""
                baseline = store.page(marker) if marker else {}
                local_path = str(baseline.get("local_path") or "")
                if marker and local_path and store.prepared_match(marker, page):
                    ledger.published_pages[local_path] = {
                        "growi_path": page.path,
                        "page_id": page.page_id,
                        "revision_id": str(baseline.get("prepared_revision") or ""),
                        "marker_id": marker,
                        "marker_seed": local_path,
                    }
                    batch.selected_page_ids.add(page.page_id)
                elif marker:
                    reason = "unledgered owned page has no exact prepared publication evidence"
                    if local_path:
                        row = {"marker_id": marker, "page_id": page.page_id,
                               "growi_path": page.path, "revision_id": ""}
                        store.block_page(local_path, row, reason, page)
                    batch.metrics.setdefault("inventory_blocks", 0)
                    batch.metrics["inventory_blocks"] += 1
                    discovery_failures.append(f"inventory: {page.path}: {reason}")
            batch.metrics.update({f"inventory_{key}": value for key, value in inventory.metrics.items()})
        pulled, failures, blocked = _capture_remote(
            project, ledger, publisher, page_ids=batch.selected_page_ids
        ) if batch.selected_page_ids else ([], [], set())
        failures.extend(discovery_failures)
        if inventory is not None:
            ambiguous = [row for row in inventory.classifications
                         if row["classification"] in {"ambiguous", "duplicate_ownership"}]
            failures.extend(
                f"inventory: {row['classification']}: {row.get('path') or row.get('marker_id') or ''}"
                for row in ambiguous
            )
            raise RuntimeError("GROWI_URL is required for queued deletion")
        if on_revision is not None and hasattr(publisher, "on_revision"):
            publisher.on_revision = on_revision
        ledger_path = project.metadata / "pipeline.json"
        run_id = "del-" + uuid.uuid4().hex[:16]
        with _lock(project):
            ledger = load_ledger(ledger_path)
            _pulled, capture_failures, _blocked = _capture_remote(project, ledger, publisher, only=set(sources.values()))
            save_ledger(ledger_path, ledger)
            if capture_failures:
                return {"run_id": run_id, "done": [], "failures": capture_failures,
                        "cancelled": False, "index_paths": sorted(sources.values())}
            if getattr(settings, "wiki_linker_enabled", True):
                from graph.linker.catalog import Catalog
    
                mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
                catalog = Catalog.open(project.linker_database, mode=mode)
                try:
                    catalog.sync_from_planning(project)
                finally:
                    catalog.close()
            done, touched, failures = _remove_sources(project, ledger, sources)
            save_ledger(ledger_path, ledger)
            cancelled = should_continue is not None and not should_continue()
            if not failures and not cancelled:
                if prepare_publish is not None:
                    prepare_publish()
                failures.extend(_publish_sweep(
                    project, ledger, publisher, run_id, only=set(sources.values()) | touched,
                    settings=settings,
                    captured=True,
                    **({"begin_publish": begin_publish} if begin_publish is not None else {}),
                ))
            save_ledger(ledger_path, ledger)
        return {
            "run_id": run_id,
            "done": done,
            "failures": failures,
            "cancelled": cancelled,
            "index_paths": sorted(set(sources.values()) | touched),
        }
    
    
    def _replace_metadata_paths(value: Any, replacements: dict[str, str]) -> Any:
        if isinstance(value, dict):
            return {
                key: item if key == "id_seed" else _replace_metadata_paths(item, replacements)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [_replace_metadata_paths(item, replacements) for item in value]
        if isinstance(value, str):
            for old, new in replacements.items():
                if value == old:
                    return new
                if value.startswith(old.rstrip("/") + "/"):
                    return new.rstrip("/") + value[len(old):]
            return value
        return value
    
    
    def move_sources(
        settings: Any,
        jobs: list[Any],
        *,
        should_continue: Callable[[], bool] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        prepare_publish: Callable[[], None] | None = None,
        begin_publish: Callable[[], None] | None = None,
        on_revision: Callable[[Any], None] | None = None,
    ) -> dict[str, Any]:
        """Move generated state and GROWI pages while retaining source/page IDs."""
        project = open_project(settings)
        publisher = _publisher(settings)
        if publisher is None:
            raise RuntimeError("GROWI_URL is required for queued move")
        if on_revision is not None and hasattr(publisher, "on_revision"):
            publisher.on_revision = on_revision
        ledger_path = project.metadata / "pipeline.json"
        run_id = "move-" + uuid.uuid4().hex[:16]
        done: list[dict[str, Any]] = []
        failures: list[str] = []
        touched_raw: set[str] = set()
        with _lock(project):
            ledger = load_ledger(ledger_path)
            capture_scope = {str(ledger.sources[str(job.from_rel)].get("raw_rel") or job.raw_rel)
                             for job in jobs if str(job.from_rel) in ledger.sources}
            _pulled, capture_failures, _blocked = _capture_remote(project, ledger, publisher, only=capture_scope)
            save_ledger(ledger_path, ledger)
            if capture_failures:
                return {"run_id": run_id, "done": [], "failures": capture_failures,
                        "cancelled": False, "index_paths": sorted(capture_scope)}
            identities = _identities(project)
            moves: list[tuple[Any, str, str, str, dict[str, dict[str, Any]]]] = []
            index_paths: set[str] = set()
            try:
                for job in jobs:
                    if should_continue is not None and not should_continue():
                        return {"run_id": run_id, "done": done, "failures": failures, "cancelled": True}
                    old_rel, new_rel = str(job.from_rel), str(job.rel)
                    source = ledger.sources.get(old_rel)
                    if not source:
                        failures.append(f"{old_rel}: source identity missing for move")
                        continue
                    old_raw = str(source.get("raw_rel") or job.raw_rel)
                    new_raw = (Path(new_rel).parent / raw_name_for(Path(new_rel).name)).as_posix()
                    old_document = project.wiki_dir(old_raw).relative_to(project.wiki).as_posix()
                    new_document = project.wiki_dir(new_raw).relative_to(project.wiki).as_posix()
                    content_changed = bool(
                        job.target_sha256
                        and job.target_sha256 != str(source.get("source_sha256") or "")
                    )
                    for old_path, new_path in (
                        (project.raw_file(old_raw), project.raw_file(new_raw)),
                        (project.wiki_dir(old_raw), project.wiki_dir(new_raw)),
                        (project.state_dir(old_raw), project.state_dir(new_raw)),
                    ):
                        if old_path.exists() and old_path != new_path:
                            if new_path.exists():
                                raise FileExistsError(f"move target already exists: {new_path}")
                            new_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(old_path), str(new_path))
                            stop = (
                                project.raw if old_path == project.raw_file(old_raw)
                                else project.wiki if old_path == project.wiki_dir(old_raw)
                                else project.metadata / "state"
                            )
                            _prune_empty_parents(old_path.parent, stop)
                    replacements = {old_raw: new_raw, old_document: new_document, old_rel: new_rel}
                    planning = project.wiki_dir(new_raw) / "_planning"
                    for path in planning.glob("*.json") if planning.is_dir() else ():
                        data = read_json(path, default={})
                        if isinstance(data, dict):
                            data = _replace_metadata_paths(data, replacements)
                            if path.name in {"source.json", "chunks.json"}:
                                data["id_seed"] = str(source.get("id_seed") or old_document)
                            write_json_atomic(path, data)
                    from .human_changes import HumanStore
    
                    HumanStore(project).move(new_raw, old_document, new_document)
                    source.update({
                        "mount_rel": new_rel,
                        "raw_rel": new_raw,
                        "wiki_rel": new_rel,
                    })
                    # A rename plus an edit still has to parse the new immutable blob.
                    # Leaving the previous digest here makes sync_once see the content
                    # change and prevents its resume gate from reusing the old raw text.
                    if not content_changed:
                        source.update({
                            "source_sha256": str(job.target_sha256 or source.get("source_sha256") or ""),
                            "source_blob_oid": str(job.target_blob_oid or source.get("source_blob_oid") or ""),
                        })
                    if (project.mount / new_rel).is_file():
                        stat = (project.mount / new_rel).stat()
                        source.update({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
                    ledger.sources.pop(old_rel, None)
                    ledger.sources[new_rel] = source
                    identities["active"].pop(old_rel, None)
                    identities["active"][new_rel] = str(source["source_id"])
                    if old_document in ledger.published_documents:
                        ledger.published_documents[new_document] = ledger.published_documents.pop(old_document)
                        ledger.published_documents[new_document]["raw_rel"] = new_raw
                    old_prefix = old_document.rstrip("/") + "/"
                    moved_pages: dict[str, dict[str, Any]] = {}
                    for local_path, row in list(ledger.published_pages.items()):
                        if local_path.startswith(old_prefix):
                            moved_pages[new_document + local_path[len(old_document):]] = ledger.published_pages.pop(local_path)
                    ledger.published_pages.update(moved_pages)
                    moves.append((job, old_raw, new_raw, new_document, moved_pages))
                    index_paths.update((old_raw, new_raw))
                if failures:
                    return {"run_id": run_id, "done": done, "failures": failures, "cancelled": False}
                _assert_unique_growi_locations(project, set(_folders(project)), publisher)
                _save_identities(project, identities)
                if getattr(settings, "wiki_linker_enabled", True) and moves:
                    from graph.linker.catalog import Catalog
    
                    mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
                    catalog = Catalog.open(project.linker_database, mode=mode)
                    try:
                        catalog.sync_from_planning(project)
                    finally:
                        catalog.close()
                    touched_raw.update(run_linkers(
                        project, [new_raw for _job, _old_raw, new_raw, _document, _pages in moves],
                        settings=settings, llm=None, embedder=None, on_progress=on_progress,
                        stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                    ))
                save_ledger(ledger_path, ledger)
                if should_continue is not None and not should_continue():
                    return {"run_id": run_id, "done": done, "failures": failures, "cancelled": True}
                if prepare_publish is not None:
                    prepare_publish()
                if moves and begin_publish is not None:
                    begin_publish()
                for job, old_raw, new_raw, new_document, moved_pages in moves:
                    remote = publisher.move_document(project, old_raw, new_raw, moved_pages)
                    for local_path, page in remote.items():
                        moved_pages[local_path] = _published_page_row(project, publisher, local_path, page)
                    ledger.published_pages.update(moved_pages)
                    if new_document in ledger.published_documents:
                        ledger.published_documents[new_document]["growi_path"] = publisher.doc_path(project, new_raw)
                    save_ledger(ledger_path, ledger)
                    done.append({"path": str(job.rel), "from": str(job.from_rel), "status": "moved", "rebuild": "move"})
                if touched_raw:
                    index_paths.update(touched_raw)
                    failures.extend(_publish_sweep(
                        project, ledger, publisher, run_id, only=touched_raw, on_progress=on_progress,
                        settings=settings,
                        **({"begin_publish": begin_publish} if begin_publish is not None else {}),
                    ))
                if moves and not failures:
                    # A pure move does not need a document publish sweep, but its old/new
                    # document indexes and both ancestor trees still have to move. Indexes
                    # are derived output, so keep their failures non-transactional just as
                    # _publish_sweep does for add/update/delete.
                    from .index import build_index
    
                    try:
                        for problem in build_index(
                            settings,
                            only=sorted(index_paths),
                            locked=True,
                            ledger=ledger,
                            on_progress=on_progress,
    ✓ • 17ms

## Assistant

No code changed. I would implement this with:

- One persistent outer bar: `Documents 0/2`
- One reusable inner bar: `document-name | current-stage`
- Stage bar created with `leave=False`, so it disappears after completion.
- Completion messages emitted with `tqdm.write(...)`, not raw `print(...)`, so bars redraw cleanly.
- Bars written to stderr, preserving JSON/results on stdout.

Recommended phase split:

| Phase | Main boundary |
|---|---|
| Queue/setup | `publisher/queue.py`: scan, claim, candidate setup |
| Capture | `publisher/pipeline.py`: `_capture_remote` |
| Parser | `pipeline.py`: parse block around `_parse` |
| Wiki planner | `graph/wiki/windows.py`, `document_map.py`, `wiki/pipeline.py` seed/context |
| Wiki research | `graph/wiki/pipeline.py`: `_research_references` |
| Wiki writer + judge | `graph/wiki/pipeline.py`: `_rewrite_page` / `_write_section` |
| Linker metadata/entities | `graph/linker/service.py`: chunk creation and `chunks.describe_all` |
| Linker graph decisions | `graph/linker/service.py`: candidate discovery, edge judging, rendering |
| GROWI publish | `publisher/pipeline.py`: `_publish_sweep` and `graph/growi/client.py` |
| Index | `publisher/index.py`: `build_index` |
| History/commit | `publisher/queue.py`: candidate commit, promote, rollback |

The most important improvement over your list is separating:

1. Wiki planning from linker chunk metadata/entity extraction.
2. Linker entity extraction from linker edge decisions.
3. Publish from index generation.

Otherwise index work may make “publish” look artificially slow.

The current events already support much of this:

- Parser has start/done events.
- Writer has page progress.
- Linker has chunk, edge-target, and page-curation progress.
- Publish and index have batch progress.

A few explicit start/done events would still be needed for planner, research, linker metadata, linker decisions, and index.

One caveat: wiki research and writing currently run per-page with concurrency, so those phases overlap. I would measure their document-level wall time without changing concurrency; I would not serialize them just for measurement.

## User

do it

## Assistant

I’ll implement the two-level progress display now: one document bar plus one transient stage bar, with stage completion timing written through `tqdm.write`. I’ll add explicit phase events at the actual planner/research/linker/index boundaries, keep DEBUG logging separate, and verify that JSON output and non-TTY behavior remain safe.

## Activity

    $ sed -n '1,125p' publisher/pipeline.py; sed -n '720,810p' graph/wiki/pipeline.py; sed -n '1,35p' graph/linker/service.py; sed -n '380,550p' graph/linker/service.py; sed -n '550,760p' graph/linker/service.py; sed -n '810,855p' graph/linker/service.py; sed -n '425,475p' publisher/index.py; sed -n '245,330p' publisher/queue.py
    """One reconciliation pass for the minimal publisher."""
    
    from __future__ import annotations
    
    import asyncio
    import fcntl
    import hashlib
    import json
    import logging
    import os
    import shutil
    import tempfile
    import threading
    import time
    import uuid
    from contextlib import contextmanager
    from pathlib import Path
    from types import SimpleNamespace
    from typing import Any, Callable
    
    from graph.clients.embeddings import Embedder
    from graph.common.markdown import strip_big_tables, strip_image_media
    from graph.growi import MARKER_FORMAT, GrowiClient, GrowiPublisher, growi_path
    from graph.workspace.parser_client import UnsupportedDocument, parse_document
    from graph.workspace.project import (
        Project,
        assert_unique_generated_paths,
        open_project,
        raw_name_for,
        wiki_folder_name,
    )
    from graph.workspace.writer import links_up_to_date, run_linkers, wiki_config, wiki_up_to_date, write_wiki_pages
    from graph.wiki.model import ChatModelPort
    from graph.wiki.storage import read_json, write_json_atomic
    
    from .ledger import Ledger, load_ledger, save_ledger
    from .scanner import Scan, SourceFile, scan_mount
    
    log = logging.getLogger(__name__)
    PARSER_TIMEOUT = 7200.0
    PARSE_MIN_RATIO = 0.30  # a re-parse this much smaller than before is treated as broken
    PARSE_GATE_MIN_CHARS = 2000  # tiny documents may legitimately lose most text
    
    
    @contextmanager
    def _progress_heartbeat(
        callback: Callable[[dict[str, Any]], None] | None,
        *,
        stage: str,
        file: str,
        interval: float = 10.0,
    ):
        if callback is None:
            yield
            return
        stopped = threading.Event()
        started = time.monotonic()
    
        def pulse() -> None:
            while not stopped.wait(interval):
                callback({
                    "stage": stage,
                    "step": "waiting",
                    "file": file,
                    "elapsed_seconds": round(time.monotonic() - started),
                })
    
        thread = threading.Thread(target=pulse, name=f"{stage}-progress", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1.0)
    
    
    def _source_row(
        item: SourceFile,
        raw_rel: str,
        error: str = "",
        *,
        details: dict[str, Any] | None = None,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        details, previous = details or {}, previous or {}
        source_id = str(details.get("source_id") or previous.get("source_id") or uuid.uuid4())
        raw_path = Path(raw_rel)
        legacy_seed = (raw_path.parent / wiki_folder_name(raw_path.name)).as_posix()
        return {
            "source_id": source_id,
            "id_seed": str(previous.get("id_seed") or details.get("id_seed") or (legacy_seed if previous else source_id)),
            "mount_rel": item.rel,
            # A failed wiki/generate stage does not mean that the source bytes are
            # unknown.  Keep the digest so a later retry can reuse the parsed
            # Markdown when the source is unchanged; ``last_error`` already marks
            # the row as incomplete and is what makes the queue retry it.
            "source_sha256": item.source_sha256,
            "source_blob_oid": str(details.get("source_blob_oid") or previous.get("source_blob_oid") or ""),
            "size": item.size,
            "mtime_ns": item.mtime_ns,
            "raw_rel": raw_rel,
            "wiki_rel": item.rel,
            "parser": item.parser,
            "completed_at": "" if error else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "last_error": error,
        }
    
    
    def _identity_path(project: Project) -> Path:
        return project.metadata / "source-identities.json"
    
    
    def _identities(project: Project) -> dict[str, Any]:
        data = read_json(_identity_path(project), default={})
        return {
            "schema_version": 1,
            "active": dict(data.get("active") or {}),
            "tombstones": dict(data.get("tombstones") or {}),
        }
    
    
    def _save_identities(project: Project, identities: dict[str, Any]) -> None:
        write_json_atomic(_identity_path(project), identities)
    
    
                current=current,
                total=len(selected),
                facts=len(facts),
                attempts=attempts,
                error=error,
            )
    
        research = _render_reference_research(page, evidence, seed_root=seed_root)
        write_text_atomic(research_dir / "reference-research.md", research)
        return evidence, research
    
    
    def _judge_feedback(result: PageJudgeResult) -> list[str]:
        feedback: list[str] = []
        for omission in result.missing_important_information:
            location = (
                f"原文 {omission.source_start}-{omission.source_end}行"
                if omission.source_start > 0 and omission.source_end >= omission.source_start
                else "原文範囲未指定"
            )
            feedback.append(f"{location}: {omission.description.strip()}")
        return feedback
    
    
    def _valid_reference_ranges(value: Sequence[Sequence[int]], source_line_count: int) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        for item in value:
            if len(item) != 2:
                continue
            start, end = int(item[0]), int(item[1])
            if 1 <= start <= end <= source_line_count:
                result.append((start, end))
        return _merge_ranges(result)
    
    
    def _load_seed_plan(
        path: Path,
        *,
        source_sha256: str,
        source_line_count: int,
        prompt_version: str,
    ) -> list[SeedPage] | None:
        """Load the completed seed plan so a stopped run resumes at rewriting."""
    
        if not path.exists():
            return None
        try:
            raw = read_json(path)
            if raw.get("source_sha256") != source_sha256:
                return None
            if int(raw.get("source_line_count", 0)) != source_line_count:
                return None
            if raw.get("prompt_version") != prompt_version:
                return None
            pages = [
                SeedPage(
                    number=int(item["number"]),
                    title=str(item["title"]),
                    chapter=str(item.get("chapter", "")),
                    summary=str(item.get("summary", "")),
                    owner_ranges=_merge_ranges(
                        [(int(start), int(end)) for start, end in item["owner_ranges"]]
                    ),
                    filename=str(item["filename"]),
                    page_id=f"page-{int(item['number']):03d}",
                    reference_ranges=_valid_reference_ranges(
                        item.get("reference_ranges", []), source_line_count
                    ),
                    path=[str(value) for value in item.get("path", [])],
                )
                for item in raw["pages"]
            ]
            if not pages or [page.number for page in pages] != list(range(1, len(pages) + 1)):
                return None
            if len({page.filename for page in pages}) != len(pages):
                return None
            if any(Path(page.filename).name != page.filename for page in pages):
                return None
            _verify_ranges(pages, source_line_count)
            return pages
        except (KeyError, OSError, TypeError, ValueError, PipelineError):
            return None
    
    
    def _page_state_path(state_root: Path, page: SeedPage) -> Path:
        return state_root / "pages" / f"{page.number:03d}.json"
    
    
    def _write_page_state(
        path: Path,
        result: RewriteResult,
    """End-to-end linker orchestration."""
    
    from __future__ import annotations
    
    import asyncio
    import json
    import logging
    import time
    import uuid
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any, Callable
    
    from graph.common.hashing import short_hash
    from graph.config import app_concurrency
    from graph.wiki.page import strip_reader_references
    from graph.wiki.storage import read_json, write_json_atomic
    
    from . import chunks
    from .catalog import Catalog, LinkerModeMismatch
    from .legacy import Candidate
    from .prompts import CHUNK_META_VERSION, EDGE_VERSION_JEV, EDGE_VERSION_LEGACY, EDGE_VERSION_NEO
    from .render import (
        BIG_DOCUMENT_LINES, INTERNAL_SUMMARY_TERMS, MAX_FOOTER_ENTRIES, USEFUL_LABELS,
        RenderEdge, footer_edges, render_limits, render_page, write_if_changed,
    )
    
    Progress = Callable[[dict[str, Any]], None] | None
    StopCheck = Callable[[], bool] | None
    MAX_EDGE_CANDIDATES = 12
    MAX_EDGES_PER_TARGET = 3
    MAX_PAGE_CANDIDATES = 40
    log = logging.getLogger(__name__)
    
    
                log.warning("Jev edge judge failed for %s: %s", target.chunk_id, exc)
                accepted, calls = await _filter_groups(
                    catalog, model, target, candidates_, mode, version,
                    artifact_dir, stop_check, output_language, strict=strict, settings=settings,
                )
                return accepted, calls, 1
        accepted, calls = await _filter_groups(catalog, model, target, candidates_, mode, version,
                                               artifact_dir, stop_check, output_language, strict=strict, settings=settings)
        return accepted, calls, 0
    
    
    async def link_document(
        project: Any, rel: str, *, model: Any, embedder: Any, settings: Any,
        on_progress: Progress = None, stop_check: StopCheck = None, render: bool = True,
        changed_pages: set[str] | None = None,
        regenerated_pages: set[str] | None = None,
    ) -> LinkResult:
        started = time.monotonic()
        document = _document(project, rel)
        changed_page_rels = None
        if changed_pages:
            changed_page_rels = {
                page if page.startswith(document + "/") else f"{document}/{page}"
                for page in changed_pages
                if page.startswith(document + "/") or "/" not in page
            }
        document_changed_pages = {
            page for page in (changed_page_rels or ()) if page.startswith(document + "/")
        }
        regenerated_page_rels = {
            page if page.startswith(document + "/") else f"{document}/{page}"
            for page in (regenerated_pages or ())
            if page.startswith(document + "/") or "/" not in page
        }
        team = _team(project)
        mode = str(getattr(settings, "wiki_linker_mode", "legacy"))
        judge = str(getattr(settings, "wiki_linker_judge", "llm"))
        if judge not in {"llm", "jev"}:
            raise ValueError("wiki_linker_judge must be llm or jev")
        if mode not in {"legacy", "neo"}:
            raise ValueError("wiki_linker_mode must be legacy or neo")
        planning = Path(project.wiki_dir(rel)) / "_planning"
        planning.mkdir(parents=True, exist_ok=True)
        run_id = "lrun-" + uuid.uuid4().hex[:20]
        source_marker = read_json(planning / "source.json", default={})
        id_seed = str(source_marker.get("id_seed") or document)
        # A document without a complete marker (first run, failed run, rebuild) gets a
        # candidate pass for every chunk, even ones the catalog already knows from
        # bootstrapping; metadata is still reused through the chunks.json cache.
        previous_marker = read_json(planning / "linker.json", default={})
        previously_complete = (
            previous_marker.get("status") == "complete"
            or previous_marker.get("resume") is True
        )
        write_json_atomic(planning / "linker.json", {"schema_version": 2, "status": "pending", "mode": mode, "run_id": run_id})
        if on_progress:
            on_progress({"stage": "linker", "step": "pending", "document": rel})
        catalog: Catalog | None = None
        jev_engine = None
        jev_fallbacks = 0
        jev_failed_chunks: set[str] = set()
        try:
            edge_version = EDGE_VERSION_JEV if judge == "jev" else EDGE_VERSION_NEO if mode == "neo" else EDGE_VERSION_LEGACY
            if judge == "jev":
                try:
                    from jev import get_engine_for
                    jev_engine = get_engine_for(settings)
                except Exception as exc:
                    log.warning("Jev linker engine unavailable; using LLM: %s", exc)
                    jev_fallbacks += 1
            catalog = Catalog.open(project.linker_database, mode=mode, edge_version=edge_version)
            with catalog.lock(project):
                catalog.sync_from_planning(project, skip_document=document)
                chunk_cache_path = planning / "chunks.json"
                incremental_scope = changed_page_rels is not None and previously_complete
                refresh_metadata = (
                    not incremental_scope
                    and model is not None
                    and read_json(chunk_cache_path, default={}).get("meta_version") != CHUNK_META_VERSION
                )
                previous_cache = chunks.cache_by_hash(chunk_cache_path)
                original_hashes = chunks.snapshot_originals(project.wiki_dir(rel))
                all_chunks: list[chunks.Chunk] = []
                for page in sorted((planning / "pages").glob("*.md")):
                    all_chunks.extend(chunks.make_chunks(document, team, page.name, page.read_text(encoding="utf-8"), id_seed=id_seed))
                old_rows = {row["chunk_id"]: row for row in catalog.chunks_for_document(document)}
                old_titles = {
                    str(row["page_rel"]): str(row["title"])
                    for row in catalog.conn.execute("SELECT page_rel,title FROM pages WHERE document=?", (document,))
                }
                old_edges_by_id: dict[str, dict[str, Any]] = {}
                old_page_rels: dict[str, str] = {}
                if incremental_scope and old_rows:
                    old_page_rels = {
                        str(row["chunk_id"]): str(row["page_rel"])
                        for row in catalog.conn.execute("SELECT chunk_id,page_rel FROM chunks")
                    }
                    ids = list(old_rows)
                    marks = ",".join("?" for _ in ids)
                    for edge in catalog.conn.execute(
                        f"SELECT * FROM edges WHERE chunk_a IN ({marks}) OR chunk_b IN ({marks}) ORDER BY edge_id",
                        [*ids, *ids],
                    ).fetchall():
                        stored = dict(edge)
                        old_edges_by_id[str(edge["edge_id"])] = stored
                for item in all_chunks:
                    if item.text_sha256 in previous_cache:
                        item.meta = previous_cache[item.text_sha256]
                    elif not refresh_metadata and item.chunk_id in old_rows and item.page_rel not in regenerated_page_rels:
                        item.meta = _row_meta(old_rows[item.chunk_id])
                        if incremental_scope:
                            item.meta = chunks.validate_meta(item.meta, item.text)
                diff = catalog.reconcile(document, all_chunks, team=team, raw_rel=rel, page_hashes=original_hashes)
                stale_ids = set(diff["new"]) | set(diff["changed"])
                if changed_page_rels is not None:
                    stale_ids.intersection_update(
                        item.chunk_id for item in all_chunks if item.page_rel in changed_page_rels
                    )
                # Regenerated chunks need global candidate rediscovery. Patched chunks
                # refresh metadata and embeddings below, but keep the incremental
                # contract of rechecking their existing visible edges only.
                fresh_ids = (
                    {
                        item.chunk_id for item in all_chunks
                        if item.page_rel in regenerated_page_rels and item.chunk_id in stale_ids
                    }
                    if incremental_scope else set()
                )
                if incremental_scope:
                    # Patched pages need fresh metadata just as regenerated pages do.
                    # Reusing the old row after the chunk text changed leaves summaries,
                    # entities, edge candidates, and embeddings stale.
                    to_describe = [
                        item for item in all_chunks
                        if item.chunk_id in stale_ids and item.text_sha256 not in previous_cache
                    ]
                else:
                    to_describe = [item for item in all_chunks if refresh_metadata or not previously_complete or item.chunk_id in stale_ids]
                reported_chunks = len(stale_ids) if incremental_scope else len(to_describe)
                if on_progress:
                    on_progress({
                        "stage": "linker", "step": "chunks", "document": rel,
                        "current": reported_chunks, "total": reported_chunks,
                        "catalog_total": len(all_chunks),
                    })
                run_dir = Path(project.state_dir(rel)) / "work" / "linker" / run_id
                meta_calls, meta_fallbacks = (0, 0)
                revised_ids: set[str] = set()
                output_language = str(getattr(settings, "wiki_output_language", "Japanese (日本語)"))
                if to_describe and model is not None:
                    before_meta = {item.chunk_id: item.meta.model_dump_json() for item in all_chunks}
                    meta_calls, meta_fallbacks = await chunks.describe_all(to_describe, model=model, output_language=output_language, concurrency=_concurrency(settings), cache=previous_cache, artifact_dir=run_dir, stop_check=stop_check, parallel=judge == "jev")
                    revised_ids = {item.chunk_id for item in all_chunks if item.meta.model_dump_json() != before_meta[item.chunk_id]}
                    if changed_page_rels is not None and not refresh_metadata:
                        to_describe = [
                            item for item in all_chunks
                            if item.chunk_id in stale_ids or item.chunk_id in revised_ids
                        ]
                    else:
                        to_describe = [item for item in all_chunks if item.chunk_id in stale_ids or item.chunk_id in revised_ids or not previously_complete]
                if judge == "jev" and jev_engine is not None:
                    from .jev_judge import check_roles
                    previous_roles = {item.chunk_id: [entity.role for entity in item.entities] for item in all_chunks}
                    jev_fallbacks += await check_roles(jev_engine, all_chunks, settings)
                    jev_failed_chunks.update(item.chunk_id for item in all_chunks
                                             if item.entities and item.meta.role_judge != "jev-1")
                    revised_ids.update(item.chunk_id for item in all_chunks
                                       if previous_roles[item.chunk_id] != [entity.role for entity in item.entities])
                chunk_data = chunks.to_json(document, team, all_chunks, id_seed=id_seed)
                chunk_data["raw_rel"] = rel
                for page in chunk_data["pages"]:
                for page in chunk_data["pages"]:
                    page["original_sha256"] = original_hashes.get(page["filename"], "")
                write_json_atomic(planning / "chunks.json", chunk_data)
                known_team_names = {name_norm for name_norm, _name in catalog.entity_names(team)}
                catalog.upsert_chunks(all_chunks)
                if judge == "jev" and jev_engine is not None:
                    from .jev_judge import resolve_aliases
                    try:
                        names = sorted({(chunks.normalize_name(entity.name), entity.name)
                                        for chunk in all_chunks for entity in chunk.entities
                                        if chunks.normalize_name(entity.name) not in known_team_names})
                        await resolve_aliases(catalog, jev_engine, team, names, settings)
                    except Exception as exc:
                        jev_fallbacks += 1
                        jev_failed_chunks.update(item.chunk_id for item in all_chunks)
                        log.warning("Jev alias resolution failed for %s: %s", document, exc)
                # A rebuilt catalog has no edges for this document yet; links.json (kept
                # across republish) restores the ones whose endpoint text is unchanged.
                catalog.restore_edges(planning / "links.json")
                revised_peers = {peer for chunk_id in revised_ids for peer in catalog.edge_peers(chunk_id)}
                metadata_edges_removed = catalog.delete_edges_for(revised_ids)
                diff["edges_removed"] = int(diff.get("edges_removed", 0)) + metadata_edges_removed
                if not incremental_scope:
                    catalog.embed_pending(embedder, team=team)
                elif stale_ids:
                    catalog.embed_pending(embedder, team=team, chunk_ids=stale_ids)
                changed_ids = stale_ids | revised_ids
                affected_ids = changed_ids | set(diff["removed"])
                relevant_edges = [
                    edge for edge in old_edges_by_id.values()
                    if str(edge["chunk_a"]) in affected_ids or str(edge["chunk_b"]) in affected_ids
                ]
                visible_edge_pages: dict[str, set[str]] = {}
                if incremental_scope and mode == "neo":
                    edge_documents = {
                        page_rel.rsplit("/", 1)[0]
                        for edge in relevant_edges
                        for chunk_id in (str(edge["chunk_a"]), str(edge["chunk_b"]))
                        for page_rel in [old_page_rels.get(chunk_id, "")]
                        if page_rel
                    }
                    navigation_by_document = {
                        doc: _navigation(project, doc) for doc in edge_documents
                    }
                    for edge in relevant_edges:
                        edge_id = str(edge["edge_id"])
                        for chunk_id in (str(edge["chunk_a"]), str(edge["chunk_b"])):
                            page_rel = old_page_rels.get(chunk_id, "")
                            if not page_rel:
                                continue
                            doc, filename = page_rel.rsplit("/", 1)
                            state = navigation_by_document.get(doc, {}).get("pages", {}).get(filename, {})
                            if any(str(choice.get("edge_id")) == edge_id for choice in state.get("references", [])):
                                visible_edge_pages.setdefault(edge_id, set()).add(page_rel)
    
                edge_rows: list[dict[str, Any]] = []
                incremental_candidate_edges: dict[tuple[str, str], dict[str, Any]] = {}
                candidates_for: list[tuple[Any, list[Candidate]]] = []
                if incremental_scope:
                    grouped: dict[str, tuple[Any, list[Candidate]]] = {}
                    all_by_id = {item.chunk_id: item for item in all_chunks}
                    for edge in relevant_edges:
                        if stop_check and stop_check():
                            raise LinkerCancelled("cancelled during candidates")
                        if str(edge["chunk_a"]) in fresh_ids or str(edge["chunk_b"]) in fresh_ids:
                            continue  # regenerated text: rediscovered below or dropped
                        if catalog.chunk(str(edge["chunk_a"])) is None or catalog.chunk(str(edge["chunk_b"])) is None:
                            continue
                        source = str(edge["source"])
                        if mode == "neo" and source in {"use", "define"}:
                            if _neo_entity_edge_is_valid(catalog, edge):
                                edge_rows.append(edge)
                            continue
                        edge_id = str(edge["edge_id"])
                        # Neo behaviour edges that are not selected on either page are
                        # catalog-only candidates. Keep them without spending an LLM call.
                        if mode == "neo" and edge_id not in visible_edge_pages:
                            edge_rows.append(edge)
                            continue
                        target_id = str(edge["chunk_a"])
                        candidate_id = str(edge["chunk_b"])
                        target = all_by_id.get(target_id)
                        if target is None:
                            target_row = catalog.chunk(target_id)
                            if target_row is None:
                                continue
                            target = _row_chunk(target_row)
                        if catalog.chunk(candidate_id) is None:
                            continue
                        if model is None:
                            edge_rows.append(edge)
                            continue
                        group = grouped.setdefault(target_id, (target, []))[1]
                        group.append(Candidate(
                            candidate_id,
                            source,
                            json.loads(edge["via_json"] or "[]"),
                            label=str(edge["label"]),
                            summary=str(edge["summary"]),
                        ))
                        incremental_candidate_edges[(target_id, candidate_id)] = edge
                    candidates_for = list(grouped.values())
                    for item in all_chunks:
                        if item.chunk_id not in fresh_ids:
                            continue
                        if stop_check and stop_check():
                            raise LinkerCancelled("cancelled during candidates")
                        if mode == "neo":
                            from .neo import candidates as find_candidates
                            found = find_candidates(catalog, item, team=team, settings=settings, judge=judge == "jev")
                        else:
                            from .legacy import candidates as find_candidates
                            found = find_candidates(catalog, item, team=team)
                        candidates_for.append((item, found))
                else:
                    for item in to_describe:
                        if stop_check and stop_check():
                            raise LinkerCancelled("cancelled during candidates")
                        if mode == "neo":
                            from .neo import candidates as find_candidates
                            found = find_candidates(catalog, item, team=team, settings=settings, judge=judge == "jev")
                        else:
                            from .legacy import candidates as find_candidates
                            found = find_candidates(catalog, item, team=team)
                        candidates_for.append((item, found))
                if judge == "jev" and jev_engine is not None and mode == "neo":
                    from .jev_judge import primary_definer
                    for item, found in candidates_for:
                        by_name = {}
                        for candidate in found:
                            if candidate.source == "use" and not candidate.programmatic:
                                by_name.setdefault(catalog.canonical(team, chunks.normalize_name(candidate.via[0])), []).append(candidate)
                        for canon, group in by_name.items():
                            if len(group) < 2:
                                continue
                            try:
                                chosen = await primary_definer(catalog, jev_engine, team, canon,
                                                               [candidate.chunk_id for candidate in group], settings)
                                for candidate in group:
                                    if candidate.chunk_id == chosen:
                                        candidate.programmatic = True
                                        candidate.label = "defines"
                                        candidate.summary = f"「{candidate.via[0]}」の定義"
                                    else:
                                        found.remove(candidate)
                            except Exception as exc:
                                jev_fallbacks += 1
                                jev_failed_chunks.add(item.chunk_id)
                                log.warning("Jev primary definer failed for %s: %s", canon, exc)
                if incremental_scope and on_progress:
                    on_progress({
                        "stage": "linker", "step": "incremental_scope", "document": rel,
                        "changed_chunks": len(changed_ids),
                        "existing_edges": len(relevant_edges),
                        "checked_edges": sum(len(found) for _item, found in candidates_for),
                    })
                unresolved: list[tuple[chunks.Chunk, list[Candidate]]] = []
                for item, found in candidates_for:
                    pending: list[Candidate] = []
                    for candidate in found:
                        row = catalog.chunk(candidate.chunk_id)
                        if row is None:
                            continue
                        if candidate.programmatic:
                            edge_rows.append({"chunk_a": item.chunk_id, "chunk_b": candidate.chunk_id, "label": candidate.label or "related", "summary": candidate.summary, "source": candidate.source, "via": candidate.via, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                            continue
                        decision = catalog.edge_decision_get(item.text_sha256, row["text_sha256"], mode, edge_version)
                        if decision:
                            if decision["accepted"]:
                                previous = incremental_candidate_edges.get((item.chunk_id, candidate.chunk_id))
                                edge_rows.append(previous or {"chunk_a": item.chunk_id, "chunk_b": candidate.chunk_id,
                                                               "label": "related" if judge == "jev" else decision["label"],
                                                               "summary": "" if judge == "jev" else decision["summary"],
                                                               "source": "jev" if judge == "jev" else candidate.source,
                                                               "via": [candidate.source, *candidate.via] if judge == "jev" else candidate.via})
                        elif model is not None:
                            pending.append(candidate)
                    if pending:
                        unresolved.append((item, pending))
                edge_calls = 0
                concurrency = _concurrency(settings)
                semaphore = asyncio.Semaphore(concurrency)
                completed = 0
    
                async def filter_target(item: chunks.Chunk, pending: list[Candidate]) -> tuple[list[dict[str, Any]], int]:
                    nonlocal completed, jev_fallbacks
                    async with semaphore:
                        accepted, calls, fallbacks = await _filter_target(
                            catalog, model, item, pending, mode=mode, version=edge_version,
                            artifact_dir=run_dir, stop_check=stop_check, output_language=output_language,
                            strict=incremental_scope, judge=judge, jev_engine=jev_engine,
                            settings=settings, use_jev=item.chunk_id not in jev_failed_chunks,
                        )
                        jev_fallbacks += fallbacks
                        result = (accepted, calls)
                    completed += 1
                    if on_progress:
                        on_progress({"stage": "linker", "step": "edge_target_done", "document": rel, "current": completed, "total": len(unresolved)})
                    return result
    
                filtered = await asyncio.gather(*(filter_target(item, pending) for item, pending in unresolved))
                for (item, pending), (accepted, calls) in zip(unresolved, filtered):
                    edge_calls += calls
                    for candidate in pending:
                        row = catalog.chunk(candidate.chunk_id)
                        if row is None:
                            continue
                        matches = [edge for edge in accepted if edge["chunk_b"] == candidate.chunk_id]
                        if matches:
                            best = matches[0]
                            previous = incremental_candidate_edges.get((item.chunk_id, candidate.chunk_id))
                    if chunk_id in old_rows
                )
                pages.update(document_changed_pages)
                pages.update(changed_link_pages)
                pages.update(filter(None, (catalog.page_of(cid) for cid in touched_chunk_ids)))
                if incremental_scope:
                    new_titles = {item.page_rel: item.title for item in all_chunks}
                    retitled = {page for page, title in new_titles.items() if page in old_titles and old_titles[page] != title}
                    if retitled:
                        retitled_ids = {item.chunk_id for item in all_chunks if item.page_rel in retitled}
                        for edge in old_edges_by_id.values():
                            for own, other in ((str(edge["chunk_a"]), str(edge["chunk_b"])), (str(edge["chunk_b"]), str(edge["chunk_a"]))):
                                if own in retitled_ids and old_page_rels.get(other):
                                    pages.add(old_page_rels[other])
                touched_docs: set[str] = set()
                if render:
                    rendered_docs = await render_pages(project, catalog, pages, model=model, settings=settings, mode=mode, on_progress=on_progress)
                    touched_docs.update(_raw_rel(catalog, doc) for doc in rendered_docs if doc != document)
                all_docs = {
                    document,
                    *{page.rsplit("/", 1)[0] for page in pages},
                    *{
                        page_rel.rsplit("/", 1)[0]
                        for edge in relevant_edges
                        for chunk_id in (str(edge["chunk_a"]), str(edge["chunk_b"]))
                        for page_rel in [old_page_rels.get(chunk_id, "")]
                        if page_rel
                    },
                }
                catalog.write_links_json(project, all_docs)
                complete = {"schema_version": 2, "status": "complete" if render else "render_pending", "mode": mode, "scope": "incremental" if incremental_scope else "full", "meta_version": CHUNK_META_VERSION, "edge_version": edge_version, "run_id": run_id, "chunks_total": len(all_chunks), "chunks_new": len(diff["new"]) + len(diff["changed"]), "meta_calls": meta_calls, "edge_calls": edge_calls, "meta_fallbacks": meta_fallbacks, "jev_fallbacks": jev_fallbacks, "edges_added": inserted_edges, "edges_removed": diff.get("edges_removed", 0), "touched_documents": sorted(touched_docs), "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                write_json_atomic(planning / "linker.json", complete)
                if on_progress:
                    on_progress({"stage": "linker", "step": "done", "document": rel, "edges": len(edge_rows)})
                return LinkResult(sorted(touched_docs), inserted_edges, int(diff.get("edges_removed", 0)), meta_calls, edge_calls, meta_fallbacks, sorted(pages), jev_fallbacks)
        except Exception as exc:
            write_json_atomic(planning / "linker.json", {"schema_version": 2, "status": "failed", "mode": mode, "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"[:500]})
            if on_progress:
                on_progress({"stage": "linker", "step": "failed", "document": rel, "error": str(exc)[:200]})
            raise
        finally:
            if catalog is not None:
                catalog.close()
    
    
    async def link_documents(
                           connection: Any | None) -> str:
        if document not in tree:
            return ""
        blocks = _folder_cards(document, tree, summaries, connection)
        return "\n## サブフォルダ\n\n" + "\n".join(blocks) + "\n" if blocks else ""
    
    
    def build_index(settings: Any, *, only: list[str] | None = None, publish: bool = True,
                    on_progress: Callable[[dict[str, Any]], None] | None = None,
                    locked: bool = False, ledger: Ledger | None = None) -> dict[str, Any]:
        """Refresh document indexes and the containing folder index tree.
    
        ``only`` scopes document writes and their ancestor folder writes.
        ``locked`` and ``ledger`` let a publish sweep call this while it already holds the
        project lock, linking against the page IDs that sweep has just published.
        """
        project = open_project(settings)
        if not locked:
            with _lock(project):
                return build_index(
                    settings,
                    only=only,
                    publish=publish,
                    on_progress=on_progress,
                    locked=True,
                    ledger=ledger,
                )
        connection = _connection(settings)
        publisher = _publisher(settings) if publish else None
        if publish and publisher is None:
            raise RuntimeError("GROWI_URL is required for index (use --no-publish to only write metadata/index/)")
        run_id = "idx-" + uuid.uuid4().hex[:16]
        done: list[dict[str, Any]] = []
        failures: list[str] = []
        folders = _folders(project)
        paths = list(folders)
        tree = folder_tree(paths)
        if connection is not None:
            remote_owners: dict[str, str] = {}
            for rel in set(paths) | set(tree):
                remote_path = _index_link(connection, rel)
                previous = remote_owners.get(remote_path)
                if previous is not None and previous != rel:
                    raise ValueError(
                        f"index locations resolve to the same GROWI path {remote_path!r}: "
                        f"{previous!r}, {rel!r}"
                    )
                remote_owners[remote_path] = rel
        cards_by_document = {doc: document_cards(path) for doc, path in folders.items()}
        summaries = {doc: document_summary(doc, cards_by_document[doc]) for doc in folders}
        related = _related_documents(settings, folders, summaries, connection)
        ledger: Any,
        rel: str,
        previous: sqlite3.Row | None = None,
        digest: str = "",
    ) -> str:
        identities = read_json(project.metadata / "source-identities.json", default={})
        tombstone = dict((identities.get("tombstones") or {}).get(rel) or {})
        restored = str(tombstone.get("source_id") or "") if digest and tombstone.get("source_sha256") == digest else ""
        return str(
            (previous["source_id"] if previous is not None and previous["source_id"] else "")
            or ledger.sources.get(rel, {}).get("source_id")
            or restored
            or uuid.uuid4()
        )
    
    
    def scan(
        settings: Any,
        *,
        only: list[str] | None = None,
        settle_seconds: float = 10.0,
        force: bool = False,
        verify_content: bool = False,
    ) -> dict[str, Any]:
        """Stat the mount, stage changed bytes once, and coalesce desired state."""
        project = open_project(settings)
        base_commit = ensure_repository(project)
        current = _snapshot(project.mount)
        wanted = {item.strip().lstrip("/") for item in only or ()}
        if wanted:
            current = {rel: row for rel, row in current.items() if rel in wanted}
        now = time.time()
        result: dict[str, Any] = {"added": [], "updated": [], "deleted": [], "cancelled": [], "moved": [], "classification": {}}
        ledger = load_ledger(project.metadata / "pipeline.json")
        with _connect(project) as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = {
                str(row["rel"]): row
                for row in conn.execute("SELECT * FROM sources")
                if not wanted or str(row["rel"]) in wanted
            }
            previous_ids = {str(row["source_id"]) for row in previous.values() if row["source_id"]}
            for rel, source in ledger.sources.items():
                if (
                    rel in previous or (wanted and rel not in wanted)
                    or str(source.get("source_id") or "") in previous_ids
                ):
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO sources
                       (rel,raw_rel,size,mtime_ns,source_id,source_sha256,blob_oid) VALUES(?,?,?,?,?,?,?)""",
                    (
                        rel, str(source.get("raw_rel") or raw_name_for(Path(rel).name)),
                        int(source.get("size") or 0), int(source.get("mtime_ns") or 0),
                        str(source.get("source_id") or ""), str(source.get("source_sha256") or ""),
                        str(source.get("source_blob_oid") or ""),
                    ),
                )
                previous[rel] = conn.execute("SELECT * FROM sources WHERE rel=?", (rel,)).fetchone()
            ledger_rel_by_id = {
                str(source.get("source_id")): rel
                for rel, source in ledger.sources.items()
                if source.get("source_id")
            }
            for rel, row in list(previous.items()):
                source = ledger.sources.get(rel, {})
                if source and (not row["source_id"] or not row["source_sha256"] or not row["blob_oid"]):
                    conn.execute(
                        "UPDATE sources SET source_id=?,source_sha256=?,blob_oid=? WHERE rel=?",
                        (
                            str(source.get("source_id") or ""), str(source.get("source_sha256") or ""),
                            str(source.get("source_blob_oid") or ""), rel,
                        ),
                    )
                    previous[rel] = conn.execute("SELECT * FROM sources WHERE rel=?", (rel,)).fetchone()
            queued = {str(row[0]) for row in conn.execute("SELECT rel FROM jobs")}
            changed_paths = {
                rel for rel, (_raw, size, mtime_ns) in current.items()
                if rel not in previous or force or verify_content
                or (int(previous[rel]["size"]), int(previous[rel]["mtime_ns"])) != (size, mtime_ns)
            }
            staged: dict[str, Any] = {}
            for rel in sorted(changed_paths):
                try:
                    staged[rel] = stage_blob(project, project.mount / rel)
                except (FileNotFoundError, OSError, RuntimeError):
    ✓ • 19ms

## Activity

    $ sed -n '965,1045p' publisher/pipeline.py; sed -n '575,630p' publisher/pipeline.py; sed -n '430,485p' publisher/index.py; tail -80 publisher/index.py
                    if should_continue is not None and not should_continue():
                        cancelled = True
                        break
                    if rel not in retried:
                        retried.add(rel)
                        changed.append(rel)
                        log.warning("run=%s path=%s stage=generate retry_later error=%s: %s", run_id, rel, type(exc).__name__, exc)
                        continue
                    # Second failure: skip until the next sync.  Keep the source
                    # digest in the error row so an unchanged retry can skip parse.
                    error = f"{type(exc).__name__}: {exc}"[:500]
                    ledger.sources[rel] = _source_row(item, raw_rel, error, details=details, previous=previous_source)
                    save_ledger(ledger_path, ledger)
                    failures.append(f"{rel}: {error}")
                    log.error("run=%s path=%s stage=generate error=%s: %s", run_id, rel, type(exc).__name__, exc)
            pending_links = _pending_link_rels(project, settings, scoped_candidates)
            if should_continue is not None and not should_continue():
                cancelled = True
            if pending_links and not failures and not cancelled:
                try:
                    if on_progress:
                        on_progress({
                            "stage": "linker",
                            "step": "batch_start",
                            "current": 0,
                            "total": len(pending_links),
                            "documents": len(pending_links),
                        })
                    with _progress_heartbeat(on_progress, stage="linker", file="batch"):
                        touched = run_linkers(
                            project, pending_links, settings=settings, llm=model, embedder=embedder,
                            on_progress=on_progress,
                            stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                            affected_pages=incremental_pages if incremental_publish else None,
                            regenerated_pages=regenerated_pages if incremental_publish else None,
                        )
                    touched_raw.update(touched)
                    done.append({"path": "*", "status": "linked", "documents": len(pending_links), "touched": touched})
                    if on_progress:
                        on_progress({
                            "stage": "linker",
                            "step": "batch_done",
                            "current": len(pending_links),
                            "total": len(pending_links),
                            "touched": len(touched),
                        })
                except asyncio.CancelledError:
                    cancelled = True
                except Exception as exc:
                    if should_continue is not None and not should_continue():
                        cancelled = True
                    else:
                        failures.append(f"link: {type(exc).__name__}: {exc}")
            if should_continue is not None and not should_continue():
                cancelled = True
            publish_only = scoped_raw | set(pending_links) | touched_raw if wanted is not None else None
            if not failures and not cancelled:
                if prepare_publish is not None:
                    prepare_publish()
                publish_args = {
                    "only": publish_only,
                    "on_progress": on_progress,
                    **({"only_pages": incremental_pages} if incremental_publish else {}),
                    **({"begin_publish": begin_publish} if begin_publish is not None else {}),
                    "settings": settings,
                    "captured": True,
                }
                failures.extend(
                    _publish_sweep(project, ledger, publisher, run_id, **publish_args)
                )
            save_ledger(ledger_path, ledger)
            if history_enabled and not failures and not cancelled:
                from .history import checkpoint_live
    
                checkpoint_live(project, f"sync {run_id}")
        index_paths = None if publish_only is None else sorted(publish_only)
        return {
            "run_id": run_id,
            "scan": scan,
            "done": done,
            "failures": failures,
            except Exception as exc:
                blocked = set(folders)
                failures.append(f"pull: {type(exc).__name__}: {exc}")
        documents = [
            (document, folder, _document_raw_rel(document))
            for document, folder in sorted(folders.items())
            if document not in blocked
        ]
        not_ready = {project.wiki_dir(rel).relative_to(project.wiki).as_posix()
                     for rel in _wiki_raw_rels(project)
                     if project.wiki_dir(rel).is_dir() and (only is None or rel in only)} - set(folders) - blocked
        failures.extend(f"{document}: linker output is not ready" for document in sorted(not_ready))
        if publisher is not None:
            started = time.monotonic()
            try:
                if documents and begin_publish is not None:
                    begin_publish()
                if on_progress:
                    on_progress({
                        "stage": "growi-publish",
                        "step": "start",
                        "current": 0,
                        "total": len(documents),
                        "documents": len(documents),
                    })
                publish_args = {
                    "known_pages": known_pages,
                    **({"only_pages": only_pages} if only_pages is not None else {}),
                }
                pages = publisher.publish_documents(
                    project, [raw_rel for _, _, raw_rel in documents], **publish_args
                )
                published_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                for document, folder, raw_rel in documents:
                    ledger.published_documents[document] = {
                        "content_sha256": _content_hash(folder),
                        "growi_path": publisher.doc_path(project, raw_rel),
                        "raw_rel": raw_rel,
                        "published_at": published_at,
                    }
                updated_pages = {path: _published_page_row(project, publisher, path, page) for path, page in pages.items()}
                if only_pages is None:
                    published_prefixes = tuple(document.rstrip("/") + "/" for document, _, _ in documents)
                    ledger.published_pages = {
                        path: row for path, row in ledger.published_pages.items()
                        if not path.startswith(published_prefixes)
                    }
                ledger.published_pages.update(updated_pages)
                log.debug("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
                if on_progress:
                    on_progress({
                        "stage": "growi-publish",
                        "step": "done",
                        "current": len(documents),
                        "total": len(documents),
                        "pages": len(pages),
    
    
    def build_index(settings: Any, *, only: list[str] | None = None, publish: bool = True,
                    on_progress: Callable[[dict[str, Any]], None] | None = None,
                    locked: bool = False, ledger: Ledger | None = None) -> dict[str, Any]:
        """Refresh document indexes and the containing folder index tree.
    
        ``only`` scopes document writes and their ancestor folder writes.
        ``locked`` and ``ledger`` let a publish sweep call this while it already holds the
        project lock, linking against the page IDs that sweep has just published.
        """
        project = open_project(settings)
        if not locked:
            with _lock(project):
                return build_index(
                    settings,
                    only=only,
                    publish=publish,
                    on_progress=on_progress,
                    locked=True,
                    ledger=ledger,
                )
        connection = _connection(settings)
        publisher = _publisher(settings) if publish else None
        if publish and publisher is None:
            raise RuntimeError("GROWI_URL is required for index (use --no-publish to only write metadata/index/)")
        run_id = "idx-" + uuid.uuid4().hex[:16]
        done: list[dict[str, Any]] = []
        failures: list[str] = []
        folders = _folders(project)
        paths = list(folders)
        tree = folder_tree(paths)
        if connection is not None:
            remote_owners: dict[str, str] = {}
            for rel in set(paths) | set(tree):
                remote_path = _index_link(connection, rel)
                previous = remote_owners.get(remote_path)
                if previous is not None and previous != rel:
                    raise ValueError(
                        f"index locations resolve to the same GROWI path {remote_path!r}: "
                        f"{previous!r}, {rel!r}"
                    )
                remote_owners[remote_path] = rel
        cards_by_document = {doc: document_cards(path) for doc, path in folders.items()}
        summaries = {doc: document_summary(doc, cards_by_document[doc]) for doc in folders}
        related = _related_documents(settings, folders, summaries, connection)
        index_root = project.metadata / "index"
        scoped = set(paths) if only is None else {
            project.wiki_dir(rel.strip().lstrip("/")).relative_to(project.wiki).as_posix() for rel in only
        }
        affected = set(tree) if only is None else {""}
        for document in scoped:
            parts = Path(document).parts
            affected.update("/".join(parts[:i]) for i in range(1, len(parts)))
        # Include the scoped leaf even when it no longer exists. That is what lets a
        # delete or move remove its old document index, not only refresh its parents.
                status = "written"
                try:
                    if publisher is not None:
                        _, changed = asyncio.run(_upsert(
                            publisher.client, _index_link(connection, folder), body,
                            mode=connection.mode, write_path=connection.write_path, root_path=connection.root_path))
                        status = "indexed" if changed else "unchanged"
                except Exception as exc:
                    failures.append(f"folder {folder}: {type(exc).__name__}: {exc}")
                done.append({"folder": folder, "status": status})
                indexed += 1
                if on_progress:
                    on_progress({"stage": "index", "step": "folder", "current": indexed, "total": total, "folder": folder})
            if only is None:
                expected_rels = set(summaries) | set(tree)
                expected_local = {
                    (index_root / (rel if rel else "") / "index.md").resolve(strict=False)
                    for rel in expected_rels
                }
                removed_local = []
                for stale in sorted(index_root.rglob("index.md")) if index_root.exists() else ():
                    if stale.resolve(strict=False) in expected_local:
                        continue
                    if _delete_local_index(stale, index_root):
                        removed_local.append(stale.relative_to(index_root).as_posix())
                if removed_local:
                    done.append({"stale_local_indexes": removed_local, "status": "deleted"})
                if publisher is not None and hasattr(publisher.client, "list_all_pages"):
                    expected_remote = {_index_link(connection, rel) for rel in expected_rels}
                    try:
                        removed_remote = asyncio.run(_delete_stale_indexes(
                            publisher.client,
                            growi_path(connection.write_path),
                            expected_remote,
                        ))
                        if removed_remote:
                            done.append({"stale_growi_indexes": removed_remote, "status": "deleted"})
                    except Exception as exc:
                        failures.append(f"stale indexes: {type(exc).__name__}: {exc}")
        return {"run_id": run_id, "done": done, "failures": failures}
    
    
    def delete_index_pages(settings: Any) -> dict[str, Any]:
        """Remove every page carrying the index marker (called by `index --delete` and `reset`)."""
        project = open_project(settings)
        connection = _connection(settings)
        publisher = _publisher(settings)
        if publisher is None:
            raise RuntimeError("GROWI_URL is required")
        if hasattr(publisher.client, "list_all_pages"):
            deleted = asyncio.run(_delete_stale_indexes(
                publisher.client,
                growi_path(connection.write_path),
                set(),
            ))
        else:  # compatibility for small test/fake clients
            documents = list(_folders(project))
            tree = folder_tree(documents)
            paths = [growi_path(connection.write_path, document, INDEX_NAME) for document in documents]
            paths.extend(growi_path(connection.write_path, folder, INDEX_NAME)
                         for folder in tree if folder and folder not in documents)
            paths.append(growi_path(connection.write_path, INDEX_NAME))
            deleted = [path for path in paths if asyncio.run(_delete_if_index(publisher.client, path))]
        return {"run_id": "idx-del-" + uuid.uuid4().hex[:16], "done": [{"deleted": deleted}], "failures": []}
    
    
    def delete_document_index(publisher: Any, document: str) -> None:
        """Trash one document's index page after its wiki folder is removed.
    
        Index pages carry no chunk marker, so `delete_document` leaves them behind. This is
        derived output: a failure here is logged, never raised into the deletion that caused it.
        """
        try:
            path = growi_path(publisher.connection.write_path, document, INDEX_NAME)
            asyncio.run(_delete_if_index(publisher.client, path))
        except Exception as exc:
            log.warning("index page for %s: %s: %s", document, type(exc).__name__, exc)
    
    
    __all__ = ["FolderNode", "build_index", "data_blocks", "delete_document_index", "delete_index_pages", "document_cards", "document_records", "document_summary", "folder_summary", "folder_tree", "render_document_card", "render_document_index", "render_folder_card", "render_folder_index"]
    ✓ • 0ms

## Activity

    $ sed -n '1020,1060p' publisher/pipeline.py; sed -n '630,675p' publisher/index.py 2>/dev/null || true; rg -n 'return \{' publisher/index.py | tail -10
            publish_only = scoped_raw | set(pending_links) | touched_raw if wanted is not None else None
            if not failures and not cancelled:
                if prepare_publish is not None:
                    prepare_publish()
                publish_args = {
                    "only": publish_only,
                    "on_progress": on_progress,
                    **({"only_pages": incremental_pages} if incremental_publish else {}),
                    **({"begin_publish": begin_publish} if begin_publish is not None else {}),
                    "settings": settings,
                    "captured": True,
                }
                failures.extend(
                    _publish_sweep(project, ledger, publisher, run_id, **publish_args)
                )
            save_ledger(ledger_path, ledger)
            if history_enabled and not failures and not cancelled:
                from .history import checkpoint_live
    
                checkpoint_live(project, f"sync {run_id}")
        index_paths = None if publish_only is None else sorted(publish_only)
        return {
            "run_id": run_id,
            "scan": scan,
            "done": done,
            "failures": failures,
            "cancelled": cancelled,
            "index_paths": index_paths,
            "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
        }
    
    
    def delete_sources(
        settings: Any,
        sources: dict[str, str],
        *,
        should_continue: Callable[[], bool] | None = None,
        prepare_publish: Callable[[], None] | None = None,
        begin_publish: Callable[[], None] | None = None,
        on_revision: Callable[[Any], None] | None = None,
    ) -> dict[str, Any]:
        except Exception as exc:
            log.warning("index page for %s: %s: %s", document, type(exc).__name__, exc)
    
    
    __all__ = ["FolderNode", "build_index", "data_blocks", "delete_document_index", "delete_index_pages", "document_cards", "document_records", "document_summary", "folder_summary", "folder_tree", "render_document_card", "render_document_index", "render_folder_card", "render_folder_index"]
    155:        return {}
    229:    return {"name": Path(document).name, "pages": len(cards), "chapters": chapters,
    250:    return {"name": Path(folder).name if folder else "", "documents": len(documents),
    400:        return {"type": "child", "kind": kind, "name": summary["name"], "ref": _index_link(connection, rel),
    594:    return {"run_id": run_id, "done": done, "failures": failures}
    618:    return {"run_id": "idx-del-" + uuid.uuid4().hex[:16], "done": [{"deleted": deleted}], "failures": []}
    ✓ • 3ms

## Activity

    $ sed -n '120,150p' graph/wiki/pipeline.py; sed -n '735,760p' graph/wiki/pipeline.py; sed -n '890,960p' graph/wiki/pipeline.py; rg -n 'async def _rewrite_page|def _rewrite_page' graph/wiki/pipeline.py
        missing: list[str] = field(default_factory=list)
        errors: list[str] = field(default_factory=list)
        verbatim: bool = False
    
    
    @dataclass
    class _ReferenceEvidence:
        page: SeedPage
        facts: list[ReferenceFact] = field(default_factory=list)
        no_useful_information_reason: str = ""
    
    
    def _emit(callback: Progress, stage: str, step: str, **details: Any) -> None:
        if callback:
            callback({"stage": stage, "step": step, **details})
    
    
    def _merge_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
        """Collapse adjacent or overlapping source spans into simple ranges."""
    
        merged: list[tuple[int, int]] = []
        for start, end in sorted((int(start), int(end)) for start, end in ranges):
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged
    
    
    def _ranges_text(ranges: Sequence[tuple[int, int]]) -> str:
        return ", ".join(
            location = (
                f"原文 {omission.source_start}-{omission.source_end}行"
                if omission.source_start > 0 and omission.source_end >= omission.source_start
                else "原文範囲未指定"
            )
            feedback.append(f"{location}: {omission.description.strip()}")
        return feedback
    
    
    def _valid_reference_ranges(value: Sequence[Sequence[int]], source_line_count: int) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        for item in value:
            if len(item) != 2:
                continue
            start, end = int(item[0]), int(item[1])
            if 1 <= start <= end <= source_line_count:
                result.append((start, end))
        return _merge_ranges(result)
    
    
    def _load_seed_plan(
        path: Path,
        *,
        source_sha256: str,
        source_line_count: int,
        prompt_version: str,
                f"- 理由: {fact.reason.strip() or '単独で理解するために必要'}\n"
                f"- 出典: 原文 {fact.source_start}-{fact.source_end}行\n"
            )
        return "\n\n".join(rendered)
    
    
    def _verbatim_section(
        lines: Sequence[str], start: int, end: int, units: Sequence[ImageUnit]
    ) -> str:
        """Lossless by construction: the exact source lines, images as placeholders."""
    
        return demote_h1(_prompt_safe(slice_text(list(lines), start, end), units)).rstrip() + "\n"
    
    
    def _nav_footer(page: SeedPage, pages: Sequence[SeedPage]) -> str:
        previous = next((item for item in pages if item.number == page.number - 1), None)
        following = next((item for item in pages if item.number == page.number + 1), None)
        parts = []
        if previous:
            parts.append(f"前のページ: [{previous.title}]({previous.filename})")
        if following:
            parts.append(f"次のページ: [{following.title}]({following.filename})")
        if page.path[:-1]:
            parent = next((item for item in pages if item.path == page.path[:-1]), None)
            if parent is None:
                parent = next((item for item in pages if item.path[: len(page.path) - 1] == page.path[:-1]), None)
            if parent and parent.filename != page.filename:
                parts.insert(0, f"親: [{parent.title}]({parent.filename})")
        return ("\n---\n\n" + " ｜ ".join(parts) + "\n") if parts else ""
    
    
    async def _write_section(
        page: SeedPage,
        start: int,
        end: int,
        *,
        index: int,
        count: int,
        facts: Sequence[ReferenceFact],
        lines: Sequence[str],
        units: Sequence[ImageUnit],
        model: ModelPort,
        config: WikiConfig,
        task_dir: Path,
        stop_check: StopCheck,
        on_progress: Progress,
        context: str = "",
    ) -> _SectionResult:
        """Write one section until Python's lossless checks and the judge are satisfied."""
    
        # ponytail: markers plus the section judge cover enrichment; add a dedicated judge if not.
        section_units = [
            unit for unit in units if start <= unit.source_start and unit.source_end <= end
        ]
        placeholders = [unit.placeholder for unit in section_units]
        numbered = _numbered_source(lines, [(start, end)], section_units)
        source_text = _prompt_safe(slice_text(list(lines), start, end), section_units)
        facts_text = _facts_text(facts)
        stem = f"section-{index:02d}"
        candidates: list[_SectionCandidate] = []
        feedback: list[str] = []
    
        for attempt in range(1, max(1, config.write_attempts) + 1):
            if stop_check and stop_check():
                raise asyncio.CancelledError("page writing cancelled")
            prompt = section_write_prompt(
                page_title=page.title,
                page_summary=page.summary,
                index=index,
                count=count,
                source_start=start,
    1164:async def _rewrite_page(
    ✓ • 4ms

## Activity

    $ sed -n '1160,1245p' graph/wiki/pipeline.py; sed -n '1260,1340p' graph/wiki/pipeline.py
        write_text_atomic(task_dir / "intro.md", text + "\n")
        return text
    
    
    async def _rewrite_page(
        page: SeedPage,
        *,
        pages: Sequence[SeedPage],
        lines: list[str],
        units: Sequence[ImageUnit],
        tokens: dict[int, set[str]],
        model: ModelPort,
        config: WikiConfig,
        work_root: Path,
        seed_root: Path,
        source_line_count: int,
        stop_check: StopCheck,
        on_progress: Progress,
        parents: dict[str, str] | None = None,
    ) -> RewriteResult:
        from ..formats.context import context_block
    
        if len(page.owner_ranges) != 1:
            raise PipelineError(f"page {page.number} must own one contiguous range")
        start, end = page.owner_ranges[0]
        page_units = _page_units(page, units)
        task_dir = work_root / f"page-{page.number:03d}"
        task_dir.mkdir(parents=True, exist_ok=True)
    
        evidence, _research = await _research_references(
            page, pages=pages, tokens=tokens, model=model,
            config=config, work_root=work_root, seed_root=seed_root,
            stop_check=stop_check, on_progress=on_progress,
        )
        facts = [fact for item in evidence for fact in item.facts]
        # ponytail: sections stay in source order; add reordering only if a smoke run needs it.
        sections = split_sections(
            lines, start, end,
            target=config.section_target_lines, min_lines=config.section_min_lines,
        )
        buckets = assign_facts(facts, sections)
    
        drafts: list[str] = []
        scores: list[int] = []
        missing: list[str] = []
        verbatim: list[str] = []
        attempts = 0
        for index, ((s, e), section_facts) in enumerate(zip(sections, buckets), start=1):
            result = await _write_section(
                page, s, e, index=index, count=len(sections), facts=section_facts,
                lines=lines, units=units, model=model, config=config, task_dir=task_dir,
                stop_check=stop_check, on_progress=on_progress,
                context=context_block(page, pages, parents or {}),
            )
            drafts.append(result.markdown.rstrip())
            attempts += result.attempts
            if result.score is not None:
                scores.append(result.score)
            missing.extend(f"原文 {s}-{e}行: {item}" for item in result.missing)
            if result.verbatim:
                verbatim.append(f"原文 {s}-{e}行: " + "; ".join(result.errors))
    
        body = "\n\n".join(drafts)
        intro = await _write_intro(
            page, body, model=model, config=config, task_dir=task_dir,
            stop_check=stop_check, context=context_block(page, pages, parents or {}),
        )
        markdown = f"# {page.title}\n\n{intro.rstrip()}\n\n{body}\n"
        markdown = link_titles(
            markdown,
            [(item.title, item.filename) for item in pages if item.number != page.number],
        )
        markdown += _nav_footer(page, pages)
        restored, unresolved = restore_images(markdown, page_units)
        if unresolved:
            raise PipelineError(f"page {page.number} has unresolved image placeholders: {unresolved}")
        page.reference_ranges = _merge_ranges([
            (fact.source_start, fact.source_end) for fact in facts
            if 1 <= fact.source_start <= fact.source_end <= source_line_count
            and not any(start <= fact.source_start and fact.source_end <= end for start, end in page.owner_ranges)
        ])
        restored = strip_reader_references(restored)
        return RewriteResult(
            page=page,
            markdown=restored,
            attempts=attempts,
        seed_root: Path,
        wiki_root: Path,
        state_root: Path,
        source_path: Path,
        source_snapshot_path: Path,
        source_sha256: str,
        source_line_count: int,
        stop_check: StopCheck,
        on_progress: Progress,
        parents: dict[str, str] | None = None,
    ) -> list[RewriteResult]:
        semaphore = asyncio.Semaphore(max(1, config.rewrite_concurrency))
        tokens = {
            item.number: word_tokens(
                _prompt_safe(_slice_ranges(lines, item.owner_ranges), _page_units(item, units))
            )
            for item in pages
        }
    
        results: list[RewriteResult] = []
        pending: list[SeedPage] = []
        for page in pages:
            output_path = wiki_root / page.filename
            state_path = _page_state_path(state_root, page)
            resumed = (
                _resume_rewritten_page(
                    output_path, state_path, page,
                    rewrite_version=REWRITE_PROMPT_VERSION,
                    source_line_count=source_line_count,
                )
                if config.resume
                else None
            )
            if resumed is not None:
                results.append(resumed)
                _emit(on_progress, "rewrite", "page_resumed",
                      current=len(results), total=len(pages), page=page.title)
            else:
                output_path.unlink(missing_ok=True)
                state_path.unlink(missing_ok=True)
                pending.append(page)
    
        async def one(page: SeedPage) -> RewriteResult:
            async with semaphore:
                return await _rewrite_page(
                    page, pages=pages, lines=lines, units=units, tokens=tokens,
                    model=model, config=config, work_root=work_root, seed_root=seed_root,
                    source_line_count=source_line_count,
                    stop_check=stop_check, on_progress=on_progress,
                    parents=parents,
                )
    
        for completed, task in enumerate(
            asyncio.as_completed([one(page) for page in pending]), start=len(results) + 1
        ):
            result = await task
            results.append(result)
            write_text_atomic(wiki_root / result.page.filename, result.markdown)
            _write_page_state(
                _page_state_path(state_root, result.page), result,
                rewrite_version=REWRITE_PROMPT_VERSION, pages=pages,
                source_path=source_path, source_snapshot_path=source_snapshot_path,
                source_sha256=source_sha256,
            )
            _emit(
                on_progress, "rewrite", "page_done",
                current=completed, total=len(pages), page=result.page.title,
                attempts=result.attempts, score=result.judge_score,
                verbatim_sections=len(result.verbatim_sections),
            )
        return sorted(results, key=lambda item: item.page.number)
    
    
    def _index_text(title: str, pages: Sequence[SeedPage]) -> str:
        lines = [f"# {title}", "", "ページは原文での登場順に並んでいます。", ""]
        seen: set[tuple[str, ...]] = set()
        for page in pages:
            parent = tuple(page.path[:-1])
            if page.path and parent not in seen:
                lines.extend([f"## {' › '.join(parent) or page.path[0]}", ""])
                seen.add(parent)
    ✓ • 2ms

## Activity

    $ sed -n '735,830p' publisher/pipeline.py; sed -n '500,580p' publisher/pipeline.py
        """Reuse parsed Markdown when retrying stages for an unchanged source binary."""
    
        previous_sha256 = str(previous_source.get("source_sha256") or "")
        expected_sha256 = previous_sha256
        # Older failed rows intentionally erased source_sha256.  The queue still
        # carries the staged blob identity; only trust its hash when that identity
        # is the same one recorded with the raw Markdown.
        if (
            not expected_sha256
            and known_source_sha256
            and known_source_blob_oid
            and str(previous_source.get("source_blob_oid") or "") == known_source_blob_oid
        ):
            expected_sha256 = known_source_sha256
        return bool(
            requested_resume
            and classification != "forced"
            and expected_sha256 == item.source_sha256
            and raw_path.exists()
        )
    
    
    def sync_once(
        settings: Any,
        *,
        only: list[str] | None = None,
        force: bool = False,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        should_continue: Callable[[], bool] | None = None,
        include_pending: bool = False,
        resume: bool | None = None,
        source_details: dict[str, dict[str, Any]] | None = None,
        prepare_publish: Callable[[], None] | None = None,
        begin_publish: Callable[[], None] | None = None,
        on_revision: Callable[[Any], None] | None = None,
    ) -> dict[str, Any]:
        """One reconciliation pass, optionally scoped to mount-relative paths."""
        project = open_project(settings)
        history_enabled = (project.root / ".git").is_dir()
        ledger_path = project.metadata / "pipeline.json"
        run_id = "prun-" + uuid.uuid4().hex[:16]
        done: list[dict[str, Any]] = []
        failures: list[str] = []
        cancelled = False
        with _lock(project):
            if history_enabled:
                from .history import candidate_is_clean, last_good
    
                if not candidate_is_clean(project, last_good(project)):
                    raise RuntimeError("cannot sync from a dirty last-good working tree")
            ledger = load_ledger(ledger_path)
            scan = scan_mount(project.mount, ledger.sources)
            assert_unique_generated_paths(scan.files)
            publisher = _publisher(settings)
            if publisher is None:
                raise RuntimeError("GROWI_URL is required for sync/watch; use build for local-only output")
            if on_revision is not None and hasattr(publisher, "on_revision"):
                publisher.on_revision = on_revision
            wanted = {rel.strip().lstrip("/") for rel in only} if only is not None else None
            if wanted is not None:
                missing = wanted - set(scan.files) - set(ledger.sources)
                if missing:
                    raise FileNotFoundError(f"not under mount/: {sorted(missing)}")
            scoped_raw = {
                str(ledger.sources.get(rel, {}).get("raw_rel") or (
                    _raw_rel(scan.files[rel]) if rel in scan.files else (Path(rel).parent / raw_name_for(Path(rel).name)).as_posix()
                ))
                for rel in wanted or ()
                if rel in scan.files or rel in ledger.sources
            }
            # Preserve the actual common ancestor before parse, deletion or any tier
            # changes local pages. Late remote edits are handled by revision preflight.
            captured_pages, capture_failures, _blocked = _capture_remote(
                project, ledger, publisher, only=scoped_raw if wanted is not None else None,
            )
            save_ledger(ledger_path, ledger)
            if capture_failures:
                if history_enabled:
                    from .history import checkpoint_live
                    from .human_changes import HumanStore
    
                    HumanStore(project).audit()
                    checkpoint_live(project, f"capture blocked GROWI revision {run_id}")
                return {"run_id": run_id, "scan": scan, "done": [], "failures": capture_failures,
                        "cancelled": False, "index_paths": sorted(scoped_raw) if wanted is not None else None,
                        "human_sync": dict(getattr(publisher, "human_sync_summary", {}))}
            touched_raw: set[str] = set()
            deleted = scan.deleted if wanted is None else sorted(set(scan.deleted) & wanted)
            removed, touched, remove_failures = _remove_sources(project, ledger, {
                rel: str(ledger.sources.get(rel, {}).get("raw_rel") or _raw_rel(SourceFile(rel, "", 0, 0, "md")))
                for rel in deleted
            })
            done.extend(removed)
            touched_raw.update(touched)
            failures.extend(remove_failures)
            if deleted:
            "audit_interval_seconds": audit_seconds,
            "inventory_performed": inventory is not None,
            "authoritative_pages_fetched": len(batch.selected_page_ids),
            "classifications": inventory.classifications if inventory is not None else [],
            "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
        }
        cursor_commit = (detector, batch) if not failures and not batch.fallback_reason and batch.cursor else None
        return pulled, failures, blocked, metrics, cursor_commit
    
    
    def _published_page_row(project: Project, publisher: Any, path: str, page: Any) -> dict[str, Any]:
        row = {"growi_path": page.path, "page_id": page.page_id, "revision_id": page.revision_id,
               "marker_id": publisher.page_marker_id(project, path),
               "marker_seed": publisher.page_marker_seed(project, path) if hasattr(publisher, "page_marker_seed") else path}
        if isinstance(publisher, GrowiPublisher):
            from graph.growi.client import managed_page_markdown
            from .human_changes import HumanStore
    
            remote = managed_page_markdown(page.body, row["marker_id"])
            if remote is None:
                raise ValueError(f"published page returned no ownership marker: {path}")
            local = (project.wiki / path).read_text(encoding="utf-8")
            HumanStore(project).remember_page(path, row, remote, local, published=True)
        return row
    
    
    def _publish_sweep(
        project: Project,
        ledger: Ledger,
        publisher: GrowiPublisher | None,
        run_id: str,
        *,
        only: set[str] | None = None,
        only_pages: set[str] | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        begin_publish: Callable[[], None] | None = None,
        settings: Any | None = None,
        captured: bool = False,
    ) -> list[str]:
        from .index import build_index, delete_document_index  # index imports this module
    
        failures: list[str] = []
        if settings is not None and not getattr(settings, "wiki_linker_enabled", True):
            rels = [rel for rel in _wiki_raw_rels(project) if only is None or rel in only]
            run_linkers(project, rels, settings=settings, llm=None, embedder=None)
        folders = _folders(project)
        if publisher is not None:
            _assert_unique_growi_locations(project, set(folders), publisher)
        scoped_documents = (
            {project.wiki_dir(rel).relative_to(project.wiki).as_posix() for rel in only}
            if only is not None
            else None
        )
        if scoped_documents is not None:
            folders = {document: folder for document, folder in folders.items() if document in scoped_documents}
        blocked: set[str] = set()
        if publisher is not None:
            try:
                if not captured:
                    pulled, conflicts, blocked = _capture_remote(project, ledger, publisher, only=only)
                    failures.extend(conflicts)
                    if pulled:
                        if only_pages is not None:
                            only_pages = set(only_pages) | set(pulled)
                    if not conflicts and settings is not None:
                        pending = _pending_link_rels(project, settings, sorted(only) if only is not None else None)
                        if pending:
                            run_linkers(project, pending, settings=settings, llm=None, embedder=None, on_progress=on_progress)
                    folders = _folders(project)
                    if scoped_documents is not None:
                        folders = {doc: folder for doc, folder in folders.items() if doc in scoped_documents}
                known_pages = dict(ledger.published_pages)
                if hasattr(publisher, "assert_known_revisions"):
                    publisher.assert_known_revisions({path: row for path, row in known_pages.items()
                                                     if Path(path).parent.as_posix() in folders})
            except Exception as exc:
                blocked = set(folders)
                failures.append(f"pull: {type(exc).__name__}: {exc}")
        documents = [
            (document, folder, _document_raw_rel(document))
            for document, folder in sorted(folders.items())
    ✓ • 0ms

## Activity

    $ rg -n 'on_progress|stage.*(parse|wiki|linker|index)|page_done|queue-claim' tests | head -240; rg -n 'write_wiki_pages|run_pipeline|link_document|build_index' tests | head -160
    tests/test_human_changes.py:330:        shutil.copytree(self.project.wiki, staged.wiki, dirs_exist_ok=True)
    tests/test_human_changes.py:720:                                           mount_path=str(staged.mount), ingest_mode="wiki", wiki_linker_enabled=False)
    tests/test_mount_diff_pipeline.py:283:                settings, only=["a_md.md"], on_progress=None, locked=True
    tests/test_mount_diff_pipeline.py:328:                staged_first = staged.wiki_dir("doc_md.md") / "_planning" / "pages" / first.name
    tests/test_mount_diff_pipeline.py:330:                (staged.wiki_dir("doc_md.md") / "_planning" / "linker.json").write_text(
    tests/test_mount_diff_pipeline.py:430:        index.assert_called_once_with(settings, on_progress=None)
    tests/test_mount_diff_pipeline.py:1002:                connection = sqlite3.connect(staged.linker_database)
    tests/test_mount_diff_pipeline.py:1735:        self.assertFalse(any(event.get("stage") == "rewrite" and event.get("step") == "page_done" for event in self.events))
    tests/test_mount_diff_pipeline.py:1738:            if event.get("stage") == "wiki" and event.get("step") == "update_decision" and event.get("file") == self.raw_rel
    tests/test_mount_diff_pipeline.py:1741:        curated = sum(event.get("stage") == "linker" and event.get("step") == "page_curated" for event in self.events)
    tests/test_mount_diff_pipeline.py:1768:            if event.get("stage") == "wiki" and event.get("step") == "update_decision" and event.get("file") == self.raw_rel
    tests/test_mount_diff_pipeline.py:1771:        page_done = sum(event.get("stage") == "rewrite" and event.get("step") == "page_done" for event in self.events)
    tests/test_mount_diff_pipeline.py:1773:        self.assertEqual(page_done, regenerated)
    tests/test_human_changes.py:607:                  patch.object(pipeline, "write_wiki_pages", side_effect=generate),
    tests/test_human_changes.py:726:                      patch("publisher.index.build_index", return_value={"failures": []})):
    tests/test_human_changes.py:795:            first = writer.write_wiki_pages(project, rel, mode="wiki", settings=tiers.SETTINGS, llm=model, embedder=None)
    tests/test_human_changes.py:798:            zero = writer.write_wiki_pages(project, rel, mode="wiki", settings=tiers.SETTINGS, llm=None, embedder=None)
    tests/test_human_changes.py:807:                second = writer.write_wiki_pages(project, rel, mode="wiki", settings=tiers.SETTINGS, llm=tiers.PatchModel(fail=True), embedder=None)
    tests/test_human_changes.py:816:                third = writer.write_wiki_pages(project, rel, mode="wiki", settings=tiers.SETTINGS, llm=None, embedder=None, resume=False)
    tests/test_human_changes.py:925:              patch("publisher.index.build_index", return_value={"failures": []})):
    tests/test_update_tiers.py:419:                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
    tests/test_update_tiers.py:424:                unchanged = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
    tests/test_update_tiers.py:434:                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
    tests/test_update_tiers.py:450:                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
    tests/test_update_tiers.py:473:            result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=EditingModel(), embedder=None)
    tests/test_update_tiers.py:498:                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
    tests/test_update_tiers.py:526:                result = writer.write_wiki_pages(
    tests/test_update_tiers.py:558:                result = writer.write_wiki_pages(
    tests/test_update_tiers.py:590:                result = writer.write_wiki_pages(
    tests/test_update_tiers.py:622:                writer.write_wiki_pages(
    tests/test_update_tiers.py:645:                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=model, embedder=None)
    tests/test_update_tiers.py:666:                result = writer.write_wiki_pages(project, rel, mode="wiki", settings=SETTINGS, llm=None, embedder=None)
    tests/test_search_speedup.py:15:from publisher.index import build_index, render_document_index
    tests/test_search_speedup.py:66:        return build_index(self.settings, only=only, publish=False)
    tests/test_search_speedup.py:121:        build_index(settings, publish=False)
    tests/test_search_speedup.py:209:            result = build_index(self.settings, publish=True)
    tests/test_search_speedup.py:251:            build_index(self.settings, publish=True)
    tests/test_search_speedup.py:255:            build_index(self.settings, publish=True)
    tests/test_search_speedup.py:258:            build_index(self.settings, only=["teamB/docC.md"], publish=True)
    tests/test_mount_diff_pipeline.py:247:                patch("publisher.index.build_index", return_value={"done": [], "failures": []}),
    tests/test_mount_diff_pipeline.py:276:                patch("publisher.index.build_index", return_value={
    tests/test_mount_diff_pipeline.py:288:        from graph.linker.service import link_document
    tests/test_mount_diff_pipeline.py:322:            asyncio.run(link_document(
    tests/test_mount_diff_pipeline.py:334:                result = asyncio.run(link_document(
    tests/test_mount_diff_pipeline.py:422:            patch("publisher.index.build_index", return_value={"done": [], "failures": []}) as index,
    tests/test_mount_diff_pipeline.py:504:                result = writer.write_wiki_pages(
    tests/test_mount_diff_pipeline.py:521:                unchanged = writer.write_wiki_pages(
    tests/test_mount_diff_pipeline.py:553:        from graph.linker.service import link_document
    tests/test_mount_diff_pipeline.py:604:                result = asyncio.run(link_document(
    tests/test_mount_diff_pipeline.py:630:                rejected = asyncio.run(link_document(
    tests/test_mount_diff_pipeline.py:642:        from graph.linker.service import link_document
    tests/test_mount_diff_pipeline.py:726:                result = asyncio.run(link_document(
    tests/test_mount_diff_pipeline.py:749:            rejected = asyncio.run(link_document(
    tests/test_mount_diff_pipeline.py:766:        from graph.linker.service import link_document
    tests/test_mount_diff_pipeline.py:819:                result = asyncio.run(link_document(
    tests/test_mount_diff_pipeline.py:836:        from graph.linker.service import link_document
    tests/test_mount_diff_pipeline.py:902:            result = asyncio.run(link_document(
    tests/test_mount_diff_pipeline.py:917:        from graph.linker.service import link_document
    tests/test_mount_diff_pipeline.py:977:            result = asyncio.run(link_document(
    tests/test_mount_diff_pipeline.py:1647:        with patch.object(pipeline, "write_wiki_pages", side_effect=RuntimeError("injected generation failure")):
    ✓ • 0ms

## Activity

    file changes: Completed · 1 changes
    Add: /home/seigyo/llm-wiki/publisher/progress.py
    """Terminal progress rendering for the foreground sync command."""
    
    from __future__ import annotations
    
    import sys
    import time
    from dataclasses import dataclass
    from pathlib import Path
    from typing import Any, Iterable
    
    try:
        from tqdm import tqdm
    except ImportError:  # pragma: no cover - tqdm is an optional terminal aid
        tqdm = None
    
    
    @dataclass
    class _Phase:
        label: str
        started: float | None = None
        total: int | None = None
        current: int = 0
        completed: int = 0
        reported: bool = False
    
    
    class SyncProgress:
        """Render one document bar and one transient bar for the active phase.
    
        Progress is intentionally kept outside the pipeline.  The pipeline emits
        structured events; this class owns terminal redraws and timing only.
        """
    
        _LABELS = {
            "capture": "capture",
            "parse": "parser",
            "planner": "planner",
            "research": "wiki research",
            "writer": "wiki writer + judge",
            "linker-entities": "linker entities",
            "linker-edges": "linker main",
            "growi-publish": "publish",
            "index": "index",
        }
    
        def __init__(self, *, stream: Any = None) -> None:
            self.stream = stream or sys.stderr
            self._tty = bool(getattr(self.stream, "isatty", lambda: False)())
            self._bars_enabled = tqdm is not None and self._tty
            self._documents: set[str] = set()
            self._completed: set[str] = set()
            self._phases: dict[tuple[str, str], _Phase] = {}
            self._active_key: tuple[str, str] | None = None
            self._stage_bar: Any = None
            self._doc_bar: Any = None
            if tqdm is not None:
                self._doc_bar = tqdm(
                    total=0,
                    desc="Documents",
                    unit="doc",
                    position=0,
                    leave=True,
                    dynamic_ncols=True,
                    disable=not self._bars_enabled,
                    file=self.stream,
                )
    
        def add_documents(self, paths: Iterable[Any]) -> None:
            for path in paths:
                if isinstance(path, dict):
                    path = path.get("to") or path.get("path") or ""
                value = str(path or "")
                if not value or value in self._documents:
                    continue
                self._documents.add(value)
            if self._doc_bar is not None:
                self._doc_bar.total = len(self._documents)
                self._doc_bar.refresh()
    
        def add_scan_result(self, result: dict[str, Any]) -> None:
            moved = result.get("moved") or []
            self.add_documents([
                *result.get("added", []),
                *result.get("updated", []),
                *result.get("deleted", []),
                *(item.get("to", "") if isinstance(item, dict) else item for item in moved),
            ])
    
        def mark_completed(self, paths: Iterable[Any]) -> None:
            for path in paths:
                value = str(path or "")
                if not value:
                    continue
                self.add_documents([value])
                if value in self._completed:
                    continue
                self._completed.add(value)
                if self._doc_bar is not None:
                    self._doc_bar.update(1)
    
        def _phase_name(self, event: dict[str, Any]) -> str | None:
            stage = str(event.get("stage") or "")
            step = str(event.get("step") or "")
            if stage in self._LABELS:
                return stage
            if stage == "linker":
                if step in {"chunks", "entities"}:
                    return "linker-entities"
                if step in {"edge_target_done", "incremental_scope", "page_curated"}:
                    return "linker-edges"
            return None
    
        @staticmethod
        def _document(event: dict[str, Any]) -> str:
            value = event.get("document") or event.get("file") or "<batch>"
            value = str(value)
            if value.startswith("/"):
                return Path(value).name
            return value
    
        def _write(self, message: str) -> None:
            if tqdm is not None:
                tqdm.write(message, file=self.stream)
            else:
                print(message, file=self.stream, flush=True)
    
        def _close_visible(self) -> None:
            if self._stage_bar is not None:
                self._stage_bar.close()
                self._stage_bar = None
            self._active_key = None
    
        def _ensure_phase(
            self,
            key: tuple[str, str],
            *,
            total: int | None = None,
        ) -> _Phase:
            phase = self._phases.get(key)
            if phase is None or phase.reported:
                phase = _Phase(self._LABELS[key[1]])
                self._phases[key] = phase
            if phase.started is None:
                phase.started = time.monotonic()
            if total is not None and total > 0:
                phase.total = total
            if self._active_key != key:
                self._close_visible()
                self._active_key = key
                if tqdm is not None:
                    self._stage_bar = tqdm(
                        total=phase.total,
                        desc=f"{key[0]} | {phase.label}",
                        unit="item",
                        position=1,
                        leave=False,
                        dynamic_ncols=True,
                        disable=not self._bars_enabled,
                        file=self.stream,
                    )
            elif self._stage_bar is not None and phase.total != self._stage_bar.total:
                self._stage_bar.total = phase.total
                self._stage_bar.refresh()
            return phase
    
        def _update_bar(self, phase: _Phase, *, step: str = "") -> None:
            if self._stage_bar is None:
                return
            desired = max(self._stage_bar.n, phase.current)
            if desired > self._stage_bar.n:
                self._stage_bar.update(desired - self._stage_bar.n)
            if step:
                self._stage_bar.set_postfix_str(step[:80], refresh=False)
            self._stage_bar.refresh()
    
        def _finish(self, key: tuple[str, str], event: dict[str, Any], *, status: str = "done") -> None:
            phase = self._phases.get(key)
            if phase is None or phase.reported:
                return
            if phase.started is None:
                phase.started = time.monotonic()
            if phase.total is not None:
                phase.current = max(phase.current, phase.total)
            elapsed = time.monotonic() - phase.started
            phase.reported = True
            if self._active_key == key:
                self._update_bar(phase, step=status)
                self._close_visible()
            suffix = "" if status == "done" else f" ({status})"
            self._write(f"[{key[0]}] {phase.label}: {elapsed:.1f}s{suffix}")
    
        def on_event(self, event: dict[str, Any]) -> None:
            event = dict(event)
            if str(event.get("stage") or "") == "queue-claim":
                self.add_documents(event.get("paths", []))
                return
            phase_name = self._phase_name(event)
            if phase_name is None:
                return
            document = self._document(event)
            key = (document, phase_name)
            step = str(event.get("step") or "")
            total = event.get("total")
            total = total if isinstance(total, int) and total > 0 else None
    
            if step in {"start", "page_start"}:
                phase = self._ensure_phase(key, total=total)
                if step == "page_start" and phase.total is not None:
                    phase.total = max(phase.total, total or phase.total)
                self._update_bar(phase, step=step)
                return
    
            phase = self._ensure_phase(key, total=total)
            if step in {"page_done", "document_done"}:
                phase.completed += 1
                phase.current = max(phase.current, phase.completed)
                if total is not None:
                    phase.total = total
                self._update_bar(phase, step=step)
                if phase.total is not None and phase.completed >= phase.total:
                    self._finish(key, event)
                return
    
            current = event.get("current")
            if isinstance(current, int):
                phase.current = max(phase.current, current)
            self._update_bar(phase, step=step)
            if step in {"done", "complete", "batch_done"}:
                self._finish(key, event)
            elif step == "failed":
                self._finish(key, event, status="failed")
    
        def close(self) -> None:
            self._close_visible()
            if self._doc_bar is not None:
                self._doc_bar.close()
            if self._documents:
                self._write(
                    f"Documents: {len(self._completed)}/{len(self._documents)} processed"
                )

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/main.py
    @@ -174,2 +174,3 @@
         from publisher.queue import retry_failed, scan, work_once, worker_lock
    +    from publisher.progress import SyncProgress
     
    @@ -178,42 +179,56 @@
         combined: dict[str, Any] = {"done": [], "failures": []}
    -    stale = pipeline.republish_if_stale(settings)
    -    if stale is not None:
    -        print(f"growi endpoint or markers changed: full republish {stale['run_id']}", flush=True)
    -        combined["failures"].extend(stale["failures"])
    -    with worker_lock(project):
    -        retry_failed(project)
    -        first = True
    -        resume = bool(getattr(args, "continue_run", False))
    -        while True:
    -            scan(
    -                settings,
    -                only=args.items or None,
    -                settle_seconds=0,
    -                force=args.force and first,
    -                verify_content=True,
    -            )
    -            first = False
    -            result = work_once(settings, on_event=_progress if args.verbose else None, continue_run=resume)
    -            # Only the first batch can resume a kept worktree; later batches
    -            # in the same process are always fresh.
    -            resume = False
    -            if result is None:
    -                break
    -            combined["done"].extend(result.get("done", []))
    -            combined["failures"].extend(result.get("failures", []))
    -            if result.get("failures"):
    -                break
    -    if not combined["failures"]:
    -        # Index pages are derived output, so reconcile them against the whole wiki tree
    -        # here: a wiki built before its index exists catches up, and a page whose body
    -        # already matches GROWI is read but never rewritten.
    -        from publisher.index import build_index
    +    progress = SyncProgress()
     
    -        try:
    -            index = build_index(settings, on_progress=_progress if args.verbose else None)
    -        except Exception as exc:  # a stale table of contents must not fail a sync
    -            index = {"done": [], "failures": [f"index: {type(exc).__name__}: {exc}"]}
    -        combined["done"].extend(index["done"])
    -        combined["failures"].extend(index["failures"])
    -    return _report(combined)
    +    def on_event(event: dict[str, Any]) -> None:
    +        progress.on_event(event)
    +        if args.verbose:
    +            _progress(event)
    +
    +    try:
    +        stale = pipeline.republish_if_stale(settings)
    +        if stale is not None:
    +            print(f"growi endpoint or markers changed: full republish {stale['run_id']}", flush=True)
    +            combined["failures"].extend(stale["failures"])
    +        with worker_lock(project):
    +            retry_failed(project)
    +            first = True
    +            resume = bool(getattr(args, "continue_run", False))
    +            while True:
    +                scan_result = scan(
    +                    settings,
    +                    only=args.items or None,
    +                    settle_seconds=0,
    +                    force=args.force and first,
    +                    verify_content=True,
    +                )
    +                progress.add_scan_result(scan_result)
    +                first = False
    +                result = work_once(settings, on_event=on_event, continue_run=resume)
    +                # Only the first batch can resume a kept worktree; later batches
    +                # in the same process are always fresh.
    +                resume = False
    +                if result is None:
    +                    break
    +                paths = result.get("paths", [])
    +                progress.add_documents(paths)
    +                combined["done"].extend(result.get("done", []))
    +                combined["failures"].extend(result.get("failures", []))
    +                if result.get("failures"):
    +                    break
    +                progress.mark_completed(paths)
    +        if not combined["failures"]:
    +            # Index pages are derived output, so reconcile them against the whole wiki tree
    +            # here: a wiki built before its index exists catches up, and a page whose body
    +            # already matches GROWI is read but never rewritten.
    +            from publisher.index import build_index
    +
    +            try:
    +                index = build_index(settings, on_progress=on_event)
    +            except Exception as exc:  # a stale table of contents must not fail a sync
    +                index = {"done": [], "failures": [f"index: {type(exc).__name__}: {exc}"]}
    +            combined["done"].extend(index["done"])
    +            combined["failures"].extend(index["failures"])
    +        return _report(combined)
    +    finally:
    +        progress.close()
     

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/publisher/pipeline.py
    @@ -804,2 +804,4 @@
             }
    +        if on_progress:
    +            on_progress({"stage": "capture", "step": "start", "document": "<batch>", "total": 1})
             # Preserve the actual common ancestor before parse, deletion or any tier
    @@ -809,2 +811,7 @@
             )
    +        if on_progress:
    +            on_progress({
    +                "stage": "capture", "step": "done", "document": "<batch>",
    +                "current": 1, "total": 1, "pages": len(captured_pages),
    +            })
             save_ledger(ledger_path, ledger)

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/publisher/pipeline.py
    @@ -881,2 +881,3 @@
                             "step": "start",
    +                        "document": raw_rel,
                             "file": rel,
    @@ -920,2 +921,3 @@
                             "step": "done",
    +                        "document": raw_rel,
                             "file": rel,
    @@ -927,3 +929,9 @@
                     if on_progress:
    -                    on_progress({"stage": "wiki", "step": "start", "file": raw_rel})
    +                    on_progress({"stage": "wiki", "step": "start", "document": raw_rel, "file": raw_rel})
    +                wiki_progress = None
    +                if on_progress:
    +                    def wiki_progress(event: dict[str, Any], *, _raw_rel: str = raw_rel) -> None:
    +                        enriched = dict(event)
    +                        enriched.setdefault("document", _raw_rel)
    +                        on_progress(enriched)
                     with _progress_heartbeat(on_progress, stage="wiki", file=raw_rel):
    @@ -931,3 +939,3 @@
                             project, raw_rel, mode=str(settings.ingest_mode), settings=settings,
    -                        llm=model, embedder=embedder, on_progress=on_progress,
    +                        llm=model, embedder=embedder, on_progress=wiki_progress,
                             resume=requested_resume,
    @@ -950,2 +958,3 @@
                             "step": "done",
    +                        "document": raw_rel,
                             "file": raw_rel,

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/graph/wiki/pipeline.py
    @@ -1424,2 +1424,3 @@
         slug = slugify(config.document_slug or source_path.stem, fallback="document").casefold()
    +    _emit(on_progress, "planner", "start", source_lines=len(lines))
         run_root = (

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/graph/wiki/pipeline.py
    @@ -1568,2 +1568,3 @@
         _emit(on_progress, "seed", "done", pages=len(pages), images=len(units))
    +    _emit(on_progress, "planner", "done", current=len(pages), total=len(pages), pages=len(pages))
     

## Activity

    $ sed -n '605,700p' graph/wiki/pipeline.py
        picks = [item for score, _, item in scored[: max(0, limit)] if score > 0]
        family = [item for item in others if item.path and item.path[:-1] == page.path[:-1]]
        unique = {item.number: item for item in adjacent + family[:limit] + picks}
        return sorted(list(unique.values())[: max(0, limit)], key=lambda item: item.number)
    
    
    async def _research_references(
        page: SeedPage,
        *,
        pages: Sequence[SeedPage],
        tokens: dict[int, set[str]],
        model: ModelPort,
        config: WikiConfig,
        work_root: Path,
        seed_root: Path,
        stop_check: StopCheck,
        on_progress: Progress,
    ) -> tuple[list[_ReferenceEvidence], str]:
        """Python selects references; one structured compare call per page."""
    
        selected = _select_references(page, pages, tokens, limit=config.reference_candidates)
        if not selected:
            return [], "# 参照調査結果\n\n他のWikiページはない。\n"
    
        research_dir = work_root / f"research-{page.number:03d}"
        cached = read_json(research_dir / "references.json", default={})
        if (
            isinstance(cached.get("useful_facts"), list)
            and "no_useful_information_reason" in cached
        ):
            result = ReferenceResearchResult.model_validate(cached)
            if _reference_validation_error(result, selected) is None:
                reason = result.no_useful_information_reason.strip()
                evidence = [
                    _ReferenceEvidence(
                        page=candidate,
                        facts=_valid_reference_facts(result.useful_facts, candidate, page),
                        no_useful_information_reason=reason,
                    )
                    for candidate in selected
                ]
                research = _render_reference_research(page, evidence, seed_root=seed_root)
                write_text_atomic(research_dir / "reference-research.md", research)
                _emit(on_progress, "research", "resumed", page=page.title)
                return evidence, research
    
        research_dir = clean_workdir(research_dir)
        target_summary = page.summary.strip() or page.title
        references = "\n\n".join(
            (
                f"--- 参照ページ {candidate.number:03d} {candidate.title}"
                f"（原文 {_ranges_text(candidate.owner_ranges)}行） ---\n"
                f"ページ要約: {candidate.summary}\n"
            )
            for candidate in selected
        )
        _emit(
            on_progress,
            "research",
            "selected",
            page=page.title,
            candidates=len(selected),
            references=[item.number for item in selected],
            context_characters=len(target_summary) + len(references),
            context_bytes=len((target_summary + references).encode("utf-8")),
        )
        prompt = reference_research_prompt(
            target_number=page.number,
            target_title=page.title,
            target_ranges=_ranges_text(page.owner_ranges),
            target_summary=target_summary,
            references=references,
            output_language=config.output_language,
        )
        result, attempts, error = await _structured_with_artifacts(
            schema=ReferenceResearchResult,
            prompt=prompt,
            model=model,
            output_dir=research_dir,
            stem="references",
            attempts=config.reference_attempts,
            max_output_tokens=config.reference_max_output_tokens,
            stop_check=stop_check,
            prompt_factory=lambda feedback: reference_research_prompt(
                target_number=page.number,
                target_title=page.title,
                target_ranges=_ranges_text(page.owner_ranges),
                target_summary=target_summary,
                references=references,
                output_language=config.output_language,
                last_error=feedback,
            ),
            validator=lambda candidate: _reference_validation_error(candidate, selected),
            retry_temperature=getattr(config, "retry_temperature", 0.7),
        )
        reason = (
    ✓ • 2ms

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/graph/wiki/pipeline.py
    @@ -624,4 +624,20 @@
     
    +    _emit(
    +        on_progress,
    +        "research",
    +        "page_start",
    +        page=page.title,
    +        current=0,
    +        total=len(pages),
    +    )
         selected = _select_references(page, pages, tokens, limit=config.reference_candidates)
         if not selected:
    +        _emit(
    +            on_progress,
    +            "research",
    +            "page_done",
    +            page=page.title,
    +            total=len(pages),
    +            references=0,
    +        )
             return [], "# 参照調査結果\n\n他のWikiページはない。\n"
    @@ -648,2 +664,3 @@
                 _emit(on_progress, "research", "resumed", page=page.title)
    +            _emit(on_progress, "research", "page_done", page=page.title, total=len(pages), resumed=True)
                 return evidence, research
    @@ -728,2 +745,3 @@
         write_text_atomic(research_dir / "reference-research.md", research)
    +    _emit(on_progress, "research", "page_done", page=page.title, total=len(pages), references=len(selected))
         return evidence, research

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/graph/wiki/pipeline.py
    @@ -1312,2 +1312,4 @@
                 results.append(resumed)
    +            _emit(on_progress, "writer", "page_start", page=page.title, current=0, total=len(pages), resumed=True)
    +            _emit(on_progress, "writer", "page_done", page=page.title, total=len(pages), resumed=True)
                 _emit(on_progress, "rewrite", "page_resumed",
    @@ -1321,3 +1323,4 @@
             async with semaphore:
    -            return await _rewrite_page(
    +            _emit(on_progress, "writer", "page_start", page=page.title, current=0, total=len(pages))
    +            result = await _rewrite_page(
                     page, pages=pages, lines=lines, units=units, tokens=tokens,
    @@ -1328,2 +1331,4 @@
                 )
    +            _emit(on_progress, "writer", "page_done", page=page.title, total=len(pages))
    +            return result
     

## Activity

    $ sed -n '445,535p' graph/linker/service.py; sed -n '555,595p' graph/linker/service.py; sed -n '660,735p' graph/linker/service.py; sed -n '790,845p' graph/linker/service.py
                    from jev import get_engine_for
                    jev_engine = get_engine_for(settings)
                except Exception as exc:
                    log.warning("Jev linker engine unavailable; using LLM: %s", exc)
                    jev_fallbacks += 1
            catalog = Catalog.open(project.linker_database, mode=mode, edge_version=edge_version)
            with catalog.lock(project):
                catalog.sync_from_planning(project, skip_document=document)
                chunk_cache_path = planning / "chunks.json"
                incremental_scope = changed_page_rels is not None and previously_complete
                refresh_metadata = (
                    not incremental_scope
                    and model is not None
                    and read_json(chunk_cache_path, default={}).get("meta_version") != CHUNK_META_VERSION
                )
                previous_cache = chunks.cache_by_hash(chunk_cache_path)
                original_hashes = chunks.snapshot_originals(project.wiki_dir(rel))
                all_chunks: list[chunks.Chunk] = []
                for page in sorted((planning / "pages").glob("*.md")):
                    all_chunks.extend(chunks.make_chunks(document, team, page.name, page.read_text(encoding="utf-8"), id_seed=id_seed))
                old_rows = {row["chunk_id"]: row for row in catalog.chunks_for_document(document)}
                old_titles = {
                    str(row["page_rel"]): str(row["title"])
                    for row in catalog.conn.execute("SELECT page_rel,title FROM pages WHERE document=?", (document,))
                }
                old_edges_by_id: dict[str, dict[str, Any]] = {}
                old_page_rels: dict[str, str] = {}
                if incremental_scope and old_rows:
                    old_page_rels = {
                        str(row["chunk_id"]): str(row["page_rel"])
                        for row in catalog.conn.execute("SELECT chunk_id,page_rel FROM chunks")
                    }
                    ids = list(old_rows)
                    marks = ",".join("?" for _ in ids)
                    for edge in catalog.conn.execute(
                        f"SELECT * FROM edges WHERE chunk_a IN ({marks}) OR chunk_b IN ({marks}) ORDER BY edge_id",
                        [*ids, *ids],
                    ).fetchall():
                        stored = dict(edge)
                        old_edges_by_id[str(edge["edge_id"])] = stored
                for item in all_chunks:
                    if item.text_sha256 in previous_cache:
                        item.meta = previous_cache[item.text_sha256]
                    elif not refresh_metadata and item.chunk_id in old_rows and item.page_rel not in regenerated_page_rels:
                        item.meta = _row_meta(old_rows[item.chunk_id])
                        if incremental_scope:
                            item.meta = chunks.validate_meta(item.meta, item.text)
                diff = catalog.reconcile(document, all_chunks, team=team, raw_rel=rel, page_hashes=original_hashes)
                stale_ids = set(diff["new"]) | set(diff["changed"])
                if changed_page_rels is not None:
                    stale_ids.intersection_update(
                        item.chunk_id for item in all_chunks if item.page_rel in changed_page_rels
                    )
                # Regenerated chunks need global candidate rediscovery. Patched chunks
                # refresh metadata and embeddings below, but keep the incremental
                # contract of rechecking their existing visible edges only.
                fresh_ids = (
                    {
                        item.chunk_id for item in all_chunks
                        if item.page_rel in regenerated_page_rels and item.chunk_id in stale_ids
                    }
                    if incremental_scope else set()
                )
                if incremental_scope:
                    # Patched pages need fresh metadata just as regenerated pages do.
                    # Reusing the old row after the chunk text changed leaves summaries,
                    # entities, edge candidates, and embeddings stale.
                    to_describe = [
                        item for item in all_chunks
                        if item.chunk_id in stale_ids and item.text_sha256 not in previous_cache
                    ]
                else:
                    to_describe = [item for item in all_chunks if refresh_metadata or not previously_complete or item.chunk_id in stale_ids]
                reported_chunks = len(stale_ids) if incremental_scope else len(to_describe)
                if on_progress:
                    on_progress({
                        "stage": "linker", "step": "chunks", "document": rel,
                        "current": reported_chunks, "total": reported_chunks,
                        "catalog_total": len(all_chunks),
                    })
                run_dir = Path(project.state_dir(rel)) / "work" / "linker" / run_id
                meta_calls, meta_fallbacks = (0, 0)
                revised_ids: set[str] = set()
                output_language = str(getattr(settings, "wiki_output_language", "Japanese (日本語)"))
                if to_describe and model is not None:
                    before_meta = {item.chunk_id: item.meta.model_dump_json() for item in all_chunks}
                    meta_calls, meta_fallbacks = await chunks.describe_all(to_describe, model=model, output_language=output_language, concurrency=_concurrency(settings), cache=previous_cache, artifact_dir=run_dir, stop_check=stop_check, parallel=judge == "jev")
                    revised_ids = {item.chunk_id for item in all_chunks if item.meta.model_dump_json() != before_meta[item.chunk_id]}
                    if changed_page_rels is not None and not refresh_metadata:
                        to_describe = [
                            item for item in all_chunks
                if judge == "jev" and jev_engine is not None:
                    from .jev_judge import resolve_aliases
                    try:
                        names = sorted({(chunks.normalize_name(entity.name), entity.name)
                                        for chunk in all_chunks for entity in chunk.entities
                                        if chunks.normalize_name(entity.name) not in known_team_names})
                        await resolve_aliases(catalog, jev_engine, team, names, settings)
                    except Exception as exc:
                        jev_fallbacks += 1
                        jev_failed_chunks.update(item.chunk_id for item in all_chunks)
                        log.warning("Jev alias resolution failed for %s: %s", document, exc)
                # A rebuilt catalog has no edges for this document yet; links.json (kept
                # across republish) restores the ones whose endpoint text is unchanged.
                catalog.restore_edges(planning / "links.json")
                revised_peers = {peer for chunk_id in revised_ids for peer in catalog.edge_peers(chunk_id)}
                metadata_edges_removed = catalog.delete_edges_for(revised_ids)
                diff["edges_removed"] = int(diff.get("edges_removed", 0)) + metadata_edges_removed
                if not incremental_scope:
                    catalog.embed_pending(embedder, team=team)
                elif stale_ids:
                    catalog.embed_pending(embedder, team=team, chunk_ids=stale_ids)
                changed_ids = stale_ids | revised_ids
                affected_ids = changed_ids | set(diff["removed"])
                relevant_edges = [
                    edge for edge in old_edges_by_id.values()
                    if str(edge["chunk_a"]) in affected_ids or str(edge["chunk_b"]) in affected_ids
                ]
                visible_edge_pages: dict[str, set[str]] = {}
                if incremental_scope and mode == "neo":
                    edge_documents = {
                        page_rel.rsplit("/", 1)[0]
                        for edge in relevant_edges
                        for chunk_id in (str(edge["chunk_a"]), str(edge["chunk_b"]))
                        for page_rel in [old_page_rels.get(chunk_id, "")]
                        if page_rel
                    }
                    navigation_by_document = {
                        doc: _navigation(project, doc) for doc in edge_documents
                    }
                    for edge in relevant_edges:
                        edge_id = str(edge["edge_id"])
                        else:
                            from .legacy import candidates as find_candidates
                            found = find_candidates(catalog, item, team=team)
                        candidates_for.append((item, found))
                else:
                    for item in to_describe:
                        if stop_check and stop_check():
                            raise LinkerCancelled("cancelled during candidates")
                        if mode == "neo":
                            from .neo import candidates as find_candidates
                            found = find_candidates(catalog, item, team=team, settings=settings, judge=judge == "jev")
                        else:
                            from .legacy import candidates as find_candidates
                            found = find_candidates(catalog, item, team=team)
                        candidates_for.append((item, found))
                if judge == "jev" and jev_engine is not None and mode == "neo":
                    from .jev_judge import primary_definer
                    for item, found in candidates_for:
                        by_name = {}
                        for candidate in found:
                            if candidate.source == "use" and not candidate.programmatic:
                                by_name.setdefault(catalog.canonical(team, chunks.normalize_name(candidate.via[0])), []).append(candidate)
                        for canon, group in by_name.items():
                            if len(group) < 2:
                                continue
                            try:
                                chosen = await primary_definer(catalog, jev_engine, team, canon,
                                                               [candidate.chunk_id for candidate in group], settings)
                                for candidate in group:
                                    if candidate.chunk_id == chosen:
                                        candidate.programmatic = True
                                        candidate.label = "defines"
                                        candidate.summary = f"「{candidate.via[0]}」の定義"
                                    else:
                                        found.remove(candidate)
                            except Exception as exc:
                                jev_fallbacks += 1
                                jev_failed_chunks.add(item.chunk_id)
                                log.warning("Jev primary definer failed for %s: %s", canon, exc)
                if incremental_scope and on_progress:
                    on_progress({
                        "stage": "linker", "step": "incremental_scope", "document": rel,
                        "changed_chunks": len(changed_ids),
                        "existing_edges": len(relevant_edges),
                        "checked_edges": sum(len(found) for _item, found in candidates_for),
                    })
                unresolved: list[tuple[chunks.Chunk, list[Candidate]]] = []
                for item, found in candidates_for:
                    pending: list[Candidate] = []
                    for candidate in found:
                        row = catalog.chunk(candidate.chunk_id)
                        if row is None:
                            continue
                        if candidate.programmatic:
                            edge_rows.append({"chunk_a": item.chunk_id, "chunk_b": candidate.chunk_id, "label": candidate.label or "related", "summary": candidate.summary, "source": candidate.source, "via": candidate.via, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                            continue
                        decision = catalog.edge_decision_get(item.text_sha256, row["text_sha256"], mode, edge_version)
                        if decision:
                            if decision["accepted"]:
                                previous = incremental_candidate_edges.get((item.chunk_id, candidate.chunk_id))
                                edge_rows.append(previous or {"chunk_a": item.chunk_id, "chunk_b": candidate.chunk_id,
                                                               "label": "related" if judge == "jev" else decision["label"],
                                                               "summary": "" if judge == "jev" else decision["summary"],
                                                               "source": "jev" if judge == "jev" else candidate.source,
                                                               "via": [candidate.source, *candidate.via] if judge == "jev" else candidate.via})
                        elif model is not None:
                            pending.append(candidate)
                    if pending:
                        unresolved.append((item, pending))
                edge_calls = 0
                concurrency = _concurrency(settings)
                semaphore = asyncio.Semaphore(concurrency)
                completed = 0
    
                async def filter_target(item: chunks.Chunk, pending: list[Candidate]) -> tuple[list[dict[str, Any]], int]:
                    nonlocal completed, jev_fallbacks
                            )
                    touched_chunk_ids: set[str] = set()
                else:
                    changed_link_pages = set()
                    touched_chunk_ids = set(diff["peers_before"]) | revised_peers
                    for edge in edge_rows:
                        touched_chunk_ids.update((edge["chunk_a"], edge["chunk_b"]))
                if changed_page_rels is not None and not refresh_metadata:
                    pages: set[str] = {
                        item.page_rel for item in all_chunks if item.chunk_id in changed_ids
                    }
                else:
                    pages = {
                        item.page_rel
                        for item in all_chunks
                        if not previously_complete or item.chunk_id in changed_ids
                    }
                pages.update(
                    str(old_rows[chunk_id]["page_rel"])
                    for chunk_id in diff["removed"]
                    if chunk_id in old_rows
                )
                pages.update(document_changed_pages)
                pages.update(changed_link_pages)
                pages.update(filter(None, (catalog.page_of(cid) for cid in touched_chunk_ids)))
                if incremental_scope:
                    new_titles = {item.page_rel: item.title for item in all_chunks}
                    retitled = {page for page, title in new_titles.items() if page in old_titles and old_titles[page] != title}
                    if retitled:
                        retitled_ids = {item.chunk_id for item in all_chunks if item.page_rel in retitled}
                        for edge in old_edges_by_id.values():
                            for own, other in ((str(edge["chunk_a"]), str(edge["chunk_b"])), (str(edge["chunk_b"]), str(edge["chunk_a"]))):
                                if own in retitled_ids and old_page_rels.get(other):
                                    pages.add(old_page_rels[other])
                touched_docs: set[str] = set()
                if render:
                    rendered_docs = await render_pages(project, catalog, pages, model=model, settings=settings, mode=mode, on_progress=on_progress)
                    touched_docs.update(_raw_rel(catalog, doc) for doc in rendered_docs if doc != document)
                all_docs = {
                    document,
                    *{page.rsplit("/", 1)[0] for page in pages},
                    *{
                        page_rel.rsplit("/", 1)[0]
                        for edge in relevant_edges
                        for chunk_id in (str(edge["chunk_a"]), str(edge["chunk_b"]))
                        for page_rel in [old_page_rels.get(chunk_id, "")]
                        if page_rel
                    },
                }
                catalog.write_links_json(project, all_docs)
                complete = {"schema_version": 2, "status": "complete" if render else "render_pending", "mode": mode, "scope": "incremental" if incremental_scope else "full", "meta_version": CHUNK_META_VERSION, "edge_version": edge_version, "run_id": run_id, "chunks_total": len(all_chunks), "chunks_new": len(diff["new"]) + len(diff["changed"]), "meta_calls": meta_calls, "edge_calls": edge_calls, "meta_fallbacks": meta_fallbacks, "jev_fallbacks": jev_fallbacks, "edges_added": inserted_edges, "edges_removed": diff.get("edges_removed", 0), "touched_documents": sorted(touched_docs), "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                write_json_atomic(planning / "linker.json", complete)
                if on_progress:
                    on_progress({"stage": "linker", "step": "done", "document": rel, "edges": len(edge_rows)})
                return LinkResult(sorted(touched_docs), inserted_edges, int(diff.get("edges_removed", 0)), meta_calls, edge_calls, meta_fallbacks, sorted(pages), jev_fallbacks)
        except Exception as exc:
    ✓ • 0ms

## Activity

    $ rg -n 'edge_rows|embed_pending|edge_calls = 0|rendered_docs' graph/linker/service.py
    573:                catalog.embed_pending(embedder, team=team)
    575:                catalog.embed_pending(embedder, team=team, chunk_ids=stale_ids)
    605:            edge_rows: list[dict[str, Any]] = []
    621:                            edge_rows.append(edge)
    627:                        edge_rows.append(edge)
    640:                        edge_rows.append(edge)
    714:                        edge_rows.append({"chunk_a": item.chunk_id, "chunk_b": candidate.chunk_id, "label": candidate.label or "related", "summary": candidate.summary, "source": candidate.source, "via": candidate.via, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    720:                            edge_rows.append(previous or {"chunk_a": item.chunk_id, "chunk_b": candidate.chunk_id,
    729:            edge_calls = 0
    763:                        edge_rows.append(best)
    768:            for edge in edge_rows:
    772:                kept_edge_ids = {catalog.edge_id_for(edge) for edge in edge_rows}
    795:                for edge in edge_rows:
    826:                rendered_docs = await render_pages(project, catalog, pages, model=model, settings=settings, mode=mode, on_progress=on_progress)
    827:                touched_docs.update(_raw_rel(catalog, doc) for doc in rendered_docs if doc != document)
    843:                on_progress({"stage": "linker", "step": "done", "document": rel, "edges": len(edge_rows)})
    ✓ • 0ms

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/graph/linker/service.py
    @@ -461,2 +461,4 @@
                 original_hashes = chunks.snapshot_originals(project.wiki_dir(rel))
    +            if on_progress:
    +                on_progress({"stage": "linker-entities", "step": "start", "document": rel})
                 all_chunks: list[chunks.Chunk] = []
    @@ -518,2 +520,3 @@
                 reported_chunks = len(stale_ids) if incremental_scope else len(to_describe)
    +            metadata_total = len(to_describe)
                 if on_progress:
    @@ -575,2 +578,8 @@
                     catalog.embed_pending(embedder, team=team, chunk_ids=stale_ids)
    +            if on_progress:
    +                on_progress({
    +                    "stage": "linker-entities", "step": "done", "document": rel,
    +                    "current": metadata_total, "total": metadata_total,
    +                    "chunks": len(all_chunks), "meta_calls": meta_calls,
    +                })
                 changed_ids = stale_ids | revised_ids
    @@ -604,2 +613,4 @@
     
    +            if on_progress:
    +                on_progress({"stage": "linker-edges", "step": "start", "document": rel})
                 edge_rows: list[dict[str, Any]] = []

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/graph/linker/service.py
    @@ -740,2 +740,7 @@
                 edge_calls = 0
    +            if on_progress:
    +                on_progress({
    +                    "stage": "linker-edges", "step": "targets", "document": rel,
    +                    "current": 0, "total": len(unresolved),
    +                })
                 concurrency = _concurrency(settings)

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/graph/linker/service.py
    @@ -843,2 +843,8 @@
                     touched_docs.update(_raw_rel(catalog, doc) for doc in rendered_docs if doc != document)
    +            if on_progress:
    +                on_progress({
    +                    "stage": "linker-edges", "step": "done", "document": rel,
    +                    "current": len(unresolved), "total": len(unresolved),
    +                    "pages": len(pages), "edge_calls": edge_calls,
    +                })
                 all_docs = {

## Activity

    $ sed -n '495,535p' publisher/index.py; sed -n '585,600p' publisher/index.py
            target = str(settings.target_name).strip("/")
            # Every block is computed (cheap, local files only) so unchanged siblings keep their hashes.
            blocks = data_blocks(tree, summaries, cards_by_document, connection,
                                 lambda doc, filename: ledger.published_pages.get(f"{doc}/{filename}", {}), target)
            for document, folder in sorted(folders.items()):
                doc_path = growi_path(connection.write_path, document) if connection else f"/{document}"
                cards = cards_by_document[document]
                if document not in doc_scope:
                    continue
    
                def link_for(filename: str, _doc=document, _doc_path=doc_path) -> str:
                    row = ledger.published_pages.get(f"{_doc}/{filename}", {})
                    return f"/{row['page_id']}" if row.get("page_id") else growi_path(_doc_path, filename)
    
                body = render_document_index(Path(document).name, cards, link_for, related.get(document, []))
                body += _subfolder_section(document, tree, summaries, connection)
                body += "\n" + blocks[document]
                write_text_atomic(project.metadata / "index" / document / "index.md", body)
                status = "written"
                if publisher is not None:
                    try:
                        page, changed = asyncio.run(_upsert(publisher.client, _index_link(connection, document), body, mode=connection.mode,
                                                            write_path=connection.write_path, root_path=connection.root_path))
                        status = "indexed" if changed else "unchanged"
                    except Exception as exc:  # one document must not stop the others
                        failures.append(f"{document}: {type(exc).__name__}: {exc}")
                        continue
                indexed += 1
                done.append({"document": document, "pages": len(cards), "status": status})
                if on_progress:
                    on_progress({"stage": "index", "step": "document", "current": indexed, "total": total, "document": document})
            for folder in sorted(affected - set(summaries), key=str):
                index_path = index_root / (folder if folder else "") / "index.md"
                if folder not in tree:
                    _delete_local_index(index_path, index_root)
                    status = "deleted"
                    if publisher is not None:
                        try:
                            if asyncio.run(_delete_if_index(publisher.client, _index_link(connection, folder))):
                                status = "deleted"
                        except Exception as exc:
                        removed_remote = asyncio.run(_delete_stale_indexes(
                            publisher.client,
                            growi_path(connection.write_path),
                            expected_remote,
                        ))
                        if removed_remote:
                            done.append({"stale_growi_indexes": removed_remote, "status": "deleted"})
                    except Exception as exc:
                        failures.append(f"stale indexes: {type(exc).__name__}: {exc}")
        return {"run_id": run_id, "done": done, "failures": failures}
    
    
    def delete_index_pages(settings: Any) -> dict[str, Any]:
        """Remove every page carrying the index marker (called by `index --delete` and `reset`)."""
        project = open_project(settings)
        connection = _connection(settings)
    ✓ • 0ms

## Activity

    $ sed -n '480,502p' publisher/index.py; sed -n '565,590p' publisher/index.py
        affected = set(tree) if only is None else {""}
        for document in scoped:
            parts = Path(document).parts
            affected.update("/".join(parts[:i]) for i in range(1, len(parts)))
        # Include the scoped leaf even when it no longer exists. That is what lets a
        # delete or move remove its old document index, not only refresh its parents.
        affected.update(scoped)
        # Keep a collision document fresh when its child-folder listing changes.
        doc_scope = scoped | (set(summaries) & affected)
        if getattr(settings, "wiki_linker_judge", "llm") == "jev" and getattr(settings, "wiki_index_related_docs", False):
            doc_scope = set(paths)
        total = len(doc_scope) + len(affected)
        indexed = 0
        with contextlib.nullcontext() if locked else _lock(project):
            ledger = ledger if ledger is not None else load_ledger(project.metadata / "pipeline.json")
            target = str(settings.target_name).strip("/")
            # Every block is computed (cheap, local files only) so unchanged siblings keep their hashes.
            blocks = data_blocks(tree, summaries, cards_by_document, connection,
                                 lambda doc, filename: ledger.published_pages.get(f"{doc}/{filename}", {}), target)
            for document, folder in sorted(folders.items()):
                doc_path = growi_path(connection.write_path, document) if connection else f"/{document}"
                cards = cards_by_document[document]
                if document not in doc_scope:
                indexed += 1
                if on_progress:
                    on_progress({"stage": "index", "step": "folder", "current": indexed, "total": total, "folder": folder})
            if only is None:
                expected_rels = set(summaries) | set(tree)
                expected_local = {
                    (index_root / (rel if rel else "") / "index.md").resolve(strict=False)
                    for rel in expected_rels
                }
                removed_local = []
                for stale in sorted(index_root.rglob("index.md")) if index_root.exists() else ():
                    if stale.resolve(strict=False) in expected_local:
                        continue
                    if _delete_local_index(stale, index_root):
                        removed_local.append(stale.relative_to(index_root).as_posix())
                if removed_local:
                    done.append({"stale_local_indexes": removed_local, "status": "deleted"})
                if publisher is not None and hasattr(publisher.client, "list_all_pages"):
                    expected_remote = {_index_link(connection, rel) for rel in expected_rels}
                    try:
                        removed_remote = asyncio.run(_delete_stale_indexes(
                            publisher.client,
                            growi_path(connection.write_path),
                            expected_remote,
                        ))
                        if removed_remote:
    ✓ • 0ms

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/publisher/index.py
    @@ -492,2 +492,4 @@
         indexed = 0
    +    if on_progress:
    +        on_progress({"stage": "index", "step": "start", "current": 0, "total": total, "run_id": run_id})
         with contextlib.nullcontext() if locked else _lock(project):
    @@ -593,2 +595,4 @@
                         failures.append(f"stale indexes: {type(exc).__name__}: {exc}")
    +    if on_progress:
    +        on_progress({"stage": "index", "step": "done", "current": indexed, "total": total, "run_id": run_id})
         return {"run_id": run_id, "done": done, "failures": failures}

## Assistant

The renderer and phase markers are now in place. I’m checking syntax and event-flow integrity next; especially that existing `rewrite`/`linker` events remain intact for tests and that the new bars only consume the added phase events.

## Activity

    $ python -m py_compile main.py publisher/progress.py publisher/pipeline.py publisher/index.py graph/wiki/pipeline.py graph/linker/service.py && git diff --check
    ✓ • 92ms

## Activity

    $ git diff --stat -- main.py publisher/progress.py publisher/pipeline.py publisher/index.py graph/wiki/pipeline.py graph/linker/service.py && git diff --unified=3 -- main.py publisher/progress.py publisher/pipeline.py publisher/index.py graph/wiki/pipeline.py graph/linker/service.py
     graph/linker/service.py |  22 +++++
     graph/wiki/pipeline.py  |  27 +++++-
     main.py                 | 181 +++++++++++++++++++++++++++---------
     publisher/index.py      |   4 +
     publisher/pipeline.py   | 239 ++++++++++++++++++++++++++++++++++++++++++++----
     5 files changed, 410 insertions(+), 63 deletions(-)
    diff --git a/graph/linker/service.py b/graph/linker/service.py
    index 1311569..9863e43 100644
    --- a/graph/linker/service.py
    +++ b/graph/linker/service.py
    @@ -459,6 +459,8 @@ async def link_document(
                 )
                 previous_cache = chunks.cache_by_hash(chunk_cache_path)
                 original_hashes = chunks.snapshot_originals(project.wiki_dir(rel))
    +            if on_progress:
    +                on_progress({"stage": "linker-entities", "step": "start", "document": rel})
                 all_chunks: list[chunks.Chunk] = []
                 for page in sorted((planning / "pages").glob("*.md")):
                     all_chunks.extend(chunks.make_chunks(document, team, page.name, page.read_text(encoding="utf-8"), id_seed=id_seed))
    @@ -516,6 +518,7 @@ async def link_document(
                 else:
                     to_describe = [item for item in all_chunks if refresh_metadata or not previously_complete or item.chunk_id in stale_ids]
                 reported_chunks = len(stale_ids) if incremental_scope else len(to_describe)
    +            metadata_total = len(to_describe)
                 if on_progress:
                     on_progress({
                         "stage": "linker", "step": "chunks", "document": rel,
    @@ -573,6 +576,12 @@ async def link_document(
                     catalog.embed_pending(embedder, team=team)
                 elif stale_ids:
                     catalog.embed_pending(embedder, team=team, chunk_ids=stale_ids)
    +            if on_progress:
    +                on_progress({
    +                    "stage": "linker-entities", "step": "done", "document": rel,
    +                    "current": metadata_total, "total": metadata_total,
    +                    "chunks": len(all_chunks), "meta_calls": meta_calls,
    +                })
                 changed_ids = stale_ids | revised_ids
                 affected_ids = changed_ids | set(diff["removed"])
                 relevant_edges = [
    @@ -602,6 +611,8 @@ async def link_document(
                             if any(str(choice.get("edge_id")) == edge_id for choice in state.get("references", [])):
                                 visible_edge_pages.setdefault(edge_id, set()).add(page_rel)
    
    +            if on_progress:
    +                on_progress({"stage": "linker-edges", "step": "start", "document": rel})
                 edge_rows: list[dict[str, Any]] = []
                 incremental_candidate_edges: dict[tuple[str, str], dict[str, Any]] = {}
                 candidates_for: list[tuple[Any, list[Candidate]]] = []
    @@ -727,6 +738,11 @@ async def link_document(
                     if pending:
                         unresolved.append((item, pending))
                 edge_calls = 0
    +            if on_progress:
    +                on_progress({
    +                    "stage": "linker-edges", "step": "targets", "document": rel,
    +                    "current": 0, "total": len(unresolved),
    +                })
                 concurrency = _concurrency(settings)
                 semaphore = asyncio.Semaphore(concurrency)
                 completed = 0
    @@ -825,6 +841,12 @@ async def link_document(
                 if render:
                     rendered_docs = await render_pages(project, catalog, pages, model=model, settings=settings, mode=mode, on_progress=on_progress)
                     touched_docs.update(_raw_rel(catalog, doc) for doc in rendered_docs if doc != document)
    +            if on_progress:
    +                on_progress({
    +                    "stage": "linker-edges", "step": "done", "document": rel,
    +                    "current": len(unresolved), "total": len(unresolved),
    +                    "pages": len(pages), "edge_calls": edge_calls,
    +                })
                 all_docs = {
                     document,
                     *{page.rsplit("/", 1)[0] for page in pages},
    diff --git a/graph/wiki/pipeline.py b/graph/wiki/pipeline.py
    index 5af978b..b7cce3f 100644
    --- a/graph/wiki/pipeline.py
    +++ b/graph/wiki/pipeline.py
    @@ -622,8 +622,24 @@ async def _research_references(
     ) -> tuple[list[_ReferenceEvidence], str]:
         """Python selects references; one structured compare call per page."""
    
    +    _emit(
    +        on_progress,
    +        "research",
    +        "page_start",
    +        page=page.title,
    +        current=0,
    +        total=len(pages),
    +    )
         selected = _select_references(page, pages, tokens, limit=config.reference_candidates)
         if not selected:
    +        _emit(
    +            on_progress,
    +            "research",
    +            "page_done",
    +            page=page.title,
    +            total=len(pages),
    +            references=0,
    +        )
             return [], "# 参照調査結果\n\n他のWikiページはない。\n"
    
         research_dir = work_root / f"research-{page.number:03d}"
    @@ -646,6 +662,7 @@ async def _research_references(
                 research = _render_reference_research(page, evidence, seed_root=seed_root)
                 write_text_atomic(research_dir / "reference-research.md", research)
                 _emit(on_progress, "research", "resumed", page=page.title)
    +            _emit(on_progress, "research", "page_done", page=page.title, total=len(pages), resumed=True)
                 return evidence, research
    
         research_dir = clean_workdir(research_dir)
    @@ -726,6 +743,7 @@ async def _research_references(
    
         research = _render_reference_research(page, evidence, seed_root=seed_root)
         write_text_atomic(research_dir / "reference-research.md", research)
    +    _emit(on_progress, "research", "page_done", page=page.title, total=len(pages), references=len(selected))
         return evidence, research
    
    
    @@ -1292,6 +1310,8 @@ async def _rewrite_all(
             )
             if resumed is not None:
                 results.append(resumed)
    +            _emit(on_progress, "writer", "page_start", page=page.title, current=0, total=len(pages), resumed=True)
    +            _emit(on_progress, "writer", "page_done", page=page.title, total=len(pages), resumed=True)
                 _emit(on_progress, "rewrite", "page_resumed",
                       current=len(results), total=len(pages), page=page.title)
             else:
    @@ -1301,13 +1321,16 @@ async def _rewrite_all(
    
         async def one(page: SeedPage) -> RewriteResult:
             async with semaphore:
    -            return await _rewrite_page(
    +            _emit(on_progress, "writer", "page_start", page=page.title, current=0, total=len(pages))
    +            result = await _rewrite_page(
                     page, pages=pages, lines=lines, units=units, tokens=tokens,
                     model=model, config=config, work_root=work_root, seed_root=seed_root,
                     source_line_count=source_line_count,
                     stop_check=stop_check, on_progress=on_progress,
                     parents=parents,
                 )
    +            _emit(on_progress, "writer", "page_done", page=page.title, total=len(pages))
    +            return result
    
         for completed, task in enumerate(
             asyncio.as_completed([one(page) for page in pending]), start=len(results) + 1
    @@ -1422,6 +1445,7 @@ async def run_pipeline(
         config = config or WikiConfig()
         model = model or ChatModelPort(config)
         slug = slugify(config.document_slug or source_path.stem, fallback="document").casefold()
    +    _emit(on_progress, "planner", "start", source_lines=len(lines))
         run_root = (
             Path(config.run_dir).resolve()
             if config.run_dir
    @@ -1565,6 +1589,7 @@ async def run_pipeline(
         write_json_atomic(plan_path, plan_json)
         write_text_atomic(wiki_root / "index.md", _index_text(source_path.stem, pages))
         _emit(on_progress, "seed", "done", pages=len(pages), images=len(units))
    +    _emit(on_progress, "planner", "done", current=len(pages), total=len(pages), pages=len(pages))
    
         results = await _rewrite_all(
             pages,
    diff --git a/main.py b/main.py
    index e11114b..1430d2f 100644
    --- a/main.py
    +++ b/main.py
    @@ -37,6 +37,8 @@ from graph.workspace.project import open_project
     PROJECT_ROOT = Path(__file__).resolve().parent
     config.PROJECT_ROOT = PROJECT_ROOT
    
    +log = logging.getLogger(__name__)
    +
    
     def _settings(args: argparse.Namespace) -> Settings:
         settings = Settings.from_env(getattr(args, "project", ""))
    @@ -75,7 +77,7 @@ def _progress(event: dict[str, Any]) -> None:
             event.pop("current", None)
             event.pop("total", None)
         details = json.dumps(event, ensure_ascii=False, default=str)
    -    print(f"[{stage}] {step}{progress} {details}".rstrip(), flush=True)
    +    log.debug(f"[{stage}] {step}{progress} {details}".rstrip())
    
    
     def _report(result: dict[str, Any]) -> int:
    @@ -170,56 +172,122 @@ def cmd_check(args: argparse.Namespace) -> int:
     def cmd_sync(args: argparse.Namespace) -> int:
         from publisher import pipeline
         from publisher.queue import retry_failed, scan, work_once, worker_lock
    +    from publisher.progress import SyncProgress
    
         settings = _settings(args)
         project = open_project(settings)
         combined: dict[str, Any] = {"done": [], "failures": []}
    -    stale = pipeline.republish_if_stale(settings)
    -    if stale is not None:
    -        print(f"growi endpoint or markers changed: full republish {stale['run_id']}", flush=True)
    -        combined["failures"].extend(stale["failures"])
    -    with worker_lock(project):
    -        retry_failed(project)
    -        first = True
    -        resume = bool(getattr(args, "continue_run", False))
    -        while True:
    -            scan(
    -                settings,
    -                only=args.items or None,
    -                settle_seconds=0,
    -                force=args.force and first,
    -                verify_content=True,
    -            )
    -            first = False
    -            result = work_once(settings, on_event=_progress if args.verbose else None, continue_run=resume)
    -            # Only the first batch can resume a kept worktree; later batches
    -            # in the same process are always fresh.
    -            resume = False
    -            if result is None:
    -                break
    -            combined["done"].extend(result.get("done", []))
    -            combined["failures"].extend(result.get("failures", []))
    -            if result.get("failures"):
    -                break
    -    if not combined["failures"]:
    -        # Index pages are derived output, so reconcile them against the whole wiki tree
    -        # here: a wiki built before its index exists catches up, and a page whose body
    -        # already matches GROWI is read but never rewritten.
    -        from publisher.index import build_index
    +    progress = SyncProgress()
    
    -        try:
    -            index = build_index(settings, on_progress=_progress if args.verbose else None)
    -        except Exception as exc:  # a stale table of contents must not fail a sync
    -            index = {"done": [], "failures": [f"index: {type(exc).__name__}: {exc}"]}
    -        combined["done"].extend(index["done"])
    -        combined["failures"].extend(index["failures"])
    -    return _report(combined)
    +    def on_event(event: dict[str, Any]) -> None:
    +        progress.on_event(event)
    +        if args.verbose:
    +            _progress(event)
    +
    +    try:
    +        stale = pipeline.republish_if_stale(settings)
    +        if stale is not None:
    +            print(f"growi endpoint or markers changed: full republish {stale['run_id']}", flush=True)
    +            combined["failures"].extend(stale["failures"])
    +        with worker_lock(project):
    +            retry_failed(project)
    +            first = True
    +            resume = bool(getattr(args, "continue_run", False))
    +            while True:
    +                scan_result = scan(
    +                    settings,
    +                    only=args.items or None,
    +                    settle_seconds=0,
    +                    force=args.force and first,
    +                    verify_content=True,
    +                )
    +                progress.add_scan_result(scan_result)
    +                first = False
    +                result = work_once(settings, on_event=on_event, continue_run=resume)
    +                # Only the first batch can resume a kept worktree; later batches
    +                # in the same process are always fresh.
    +                resume = False
    +                if result is None:
    +                    break
    +                paths = result.get("paths", [])
    +                progress.add_documents(paths)
    +                combined["done"].extend(result.get("done", []))
    +                combined["failures"].extend(result.get("failures", []))
    +                if result.get("failures"):
    +                    break
    +                progress.mark_completed(paths)
    +        if not combined["failures"]:
    +            # Index pages are derived output, so reconcile them against the whole wiki tree
    +            # here: a wiki built before its index exists catches up, and a page whose body
    +            # already matches GROWI is read but never rewritten.
    +            from publisher.index import build_index
    +
    +            try:
    +                index = build_index(settings, on_progress=on_event)
    +            except Exception as exc:  # a stale table of contents must not fail a sync
    +                index = {"done": [], "failures": [f"index: {type(exc).__name__}: {exc}"]}
    +            combined["done"].extend(index["done"])
    +            combined["failures"].extend(index["failures"])
    +        return _report(combined)
    +    finally:
    +        progress.close()
    
    
     def cmd_pull(args: argparse.Namespace) -> int:
         from publisher.pipeline import pull_growi_once
    
    -    return _report(pull_growi_once(_settings(args)))
    +    settings = _settings(args)
    +    result = (
    +        pull_growi_once(settings, force_inventory=True)
    +        if getattr(args, "inventory", False)
    +        else pull_growi_once(settings)
    +    )
    +    if result.get("human_sync"):
    +        print(json.dumps({"human_sync": result["human_sync"]}, ensure_ascii=False, default=str))
    +    return _report(result)
    +
    +
    +def cmd_human(args: argparse.Namespace) -> int:
    +    from publisher.human_changes import HumanStore
    +
    +    settings = _settings(args)
    +    project = open_project(settings)
    +    store = HumanStore(project)
    +    if args.human_command == "status":
    +        print(json.dumps(store.project_summary(), ensure_ascii=False, default=str))
    +        return 0
    +    if args.human_command == "resolve":
    +        combined = ""
    +        if args.text_file:
    +            combined = Path(args.text_file).read_text(encoding="utf-8")
    +        result = store.resolve(
    +            args.edit_id,
    +            action=args.action,
    +            expected_revision=args.revision,
    +            combined_text=combined,
    +            document=args.document or "",
    +        )
    +        print(json.dumps(result, ensure_ascii=False))
    +        return 0
    +    if args.human_command == "recover-legacy":
    +        from publisher.legacy_recovery import recover_legacy_ancestor
    +
    +        result = recover_legacy_ancestor(store, args.document)
    +        print(json.dumps(result, ensure_ascii=False, default=str))
    +        return 0 if result.get("status") in {"recovered", "no_legacy_pin"} else 1
    +    if args.human_command == "live-plan":
    +        from publisher.live_verification import LiveVerificationReport
    +
    +        report = LiveVerificationReport.create(project, settings, args.path)
    +        print(json.dumps({
    +            "report": str(report.path),
    +            "resolved_boundary": report.data["disposable_path"],
    +            "endpoint": report.data["endpoint"],
    +            "confirmation_code": report.data["confirmation_code"],
    +            "status": report.data["status"],
    +        }, ensure_ascii=False))
    +        return 0
    +    raise ValueError(f"unknown human command: {args.human_command}")
    
    
     def cmd_watch(args: argparse.Namespace) -> int:
    @@ -414,7 +482,27 @@ def build_parser() -> argparse.ArgumentParser:
         build.add_argument("--force", action="store_true", help="regenerate even when the raw source is unchanged")
         build.set_defaults(fn=cmd_build)
         publish = sub.add_parser("publish", help="publish the current wiki tree only"); project_flags(publish); publish.set_defaults(fn=cmd_publish)
    -    pull = sub.add_parser("pull", help="capture GROWI edits into the local human overlay"); project_flags(pull); pull.set_defaults(fn=cmd_pull)
    +    pull = sub.add_parser("pull", help="capture GROWI edits into the local human overlay"); project_flags(pull)
    +    pull.add_argument("--inventory", action="store_true", help="force a complete read-only inventory below the configured GROWI root")
    +    pull.set_defaults(fn=cmd_pull)
    +    human = sub.add_parser("human", help="inspect and resolve durable human overlays"); project_flags(human)
    +    human_sub = human.add_subparsers(dest="human_command", required=True)
    +    human_status = human_sub.add_parser("status", help="write and print the project-wide human-sync summary")
    +    project_flags(human_status)
    +    human_resolve = human_sub.add_parser("resolve", help="apply one revision-checked operator decision")
    +    project_flags(human_resolve)
    +    human_resolve.add_argument("edit_id")
    +    human_resolve.add_argument("--action", required=True, choices=("keep-human", "accept-source", "combine", "suppress", "delete", "retry-match"))
    +    human_resolve.add_argument("--revision", required=True, help="last inspected GROWI revision")
    +    human_resolve.add_argument("--document", help="expected raw document identity")
    +    human_resolve.add_argument("--text-file", help="UTF-8 combined body for --action combine")
    +    human_recover = human_sub.add_parser("recover-legacy", help="recover a uniquely verified pure ancestor from project Git")
    +    project_flags(human_recover)
    +    human_recover.add_argument("document", help="raw-relative document path")
    +    human_live = human_sub.add_parser("live-plan", help="create a local-only redacted plan for a disposable live verification subtree")
    +    project_flags(human_live)
    +    human_live.add_argument("--path", required=True, help="confirmed disposable path below the configured project boundary")
    +    human.set_defaults(fn=cmd_human)
         index = sub.add_parser("index", help="publish per-document + root index pages for growi-search"); project_flags(index)
         index.add_argument("items", nargs="*", metavar="raw-rel", help="raw-relative paths; omit for every linked document")
         index.add_argument("--no-publish", action="store_true", help="only write metadata/index/, do not touch GROWI")
    @@ -438,8 +526,15 @@ def build_parser() -> argparse.ArgumentParser:
    
    
     def main() -> int:
    -    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
         args = build_parser().parse_args()
    +    logging.basicConfig(
    +        level="DEBUG" if getattr(args, "verbose", False) else os.environ.get("LOG_LEVEL", "INFO"),
    +        format="%(asctime)s %(levelname)s %(message)s",
    +    )
    +    if getattr(args, "verbose", False):
    +        # -v means "show our debug", not "show httpx's every socket read".
    +        for noisy in ("httpx", "httpcore", "h11", "urllib3", "openai", "langchain", "anthropic"):
    +            logging.getLogger(noisy).setLevel(logging.INFO)
         return int(args.fn(args) or 0)
    
    
    diff --git a/publisher/index.py b/publisher/index.py
    index 1b859e7..b5a3975 100644
    --- a/publisher/index.py
    +++ b/publisher/index.py
    @@ -490,6 +490,8 @@ def build_index(settings: Any, *, only: list[str] | None = None, publish: bool =
             doc_scope = set(paths)
         total = len(doc_scope) + len(affected)
         indexed = 0
    +    if on_progress:
    +        on_progress({"stage": "index", "step": "start", "current": 0, "total": total, "run_id": run_id})
         with contextlib.nullcontext() if locked else _lock(project):
             ledger = ledger if ledger is not None else load_ledger(project.metadata / "pipeline.json")
             target = str(settings.target_name).strip("/")
    @@ -591,6 +593,8 @@ def build_index(settings: Any, *, only: list[str] | None = None, publish: bool =
                             done.append({"stale_growi_indexes": removed_remote, "status": "deleted"})
                     except Exception as exc:
                         failures.append(f"stale indexes: {type(exc).__name__}: {exc}")
    +    if on_progress:
    +        on_progress({"stage": "index", "step": "done", "current": indexed, "total": total, "run_id": run_id})
         return {"run_id": run_id, "done": done, "failures": failures}
    
    
    diff --git a/publisher/pipeline.py b/publisher/pipeline.py
    index c84d2c3..3642a95 100644
    --- a/publisher/pipeline.py
    +++ b/publisher/pipeline.py
    @@ -267,7 +267,22 @@ def _publisher(settings: Any) -> GrowiPublisher | None:
             str(getattr(settings, "growi_token", "") or os.environ.get("GROWI_TOKEN", "")),
             timeout=float(getattr(settings, "growi_timeout", 30)),
         )
    -    return GrowiPublisher(client, connection)
    +    from graph.config import HumanSyncPolicy
    +
    +    policy = HumanSyncPolicy.resolve(getattr(settings, "human_sync_mode", "off"))
    +
    +    def semantic_factory(project: Project):
    +        from .human_changes import HumanStore
    +        from .human_semantic import build_runtime_semantic_assistant
    +
    +        return build_runtime_semantic_assistant(HumanStore(project), settings)
    +
    +    return GrowiPublisher(
    +        client,
    +        connection,
    +        human_sync_policy=policy,
    +        semantic_assistant_factory=semantic_factory if policy.semantic_observe else None,
    +    )
    
    
     @contextmanager
    @@ -327,7 +342,14 @@ def _pending_link_rels(project: Project, settings: Any, rels: list[str] | None =
         return [rel for rel in candidates if project.wiki_dir(rel).exists() and (force or not links_up_to_date(project, rel, mode=mode))]
    
    
    -def _capture_remote(project: Project, ledger: Ledger, publisher: Any, *, only: set[str] | None = None) -> tuple[list[str], list[str], set[str]]:
    +def _capture_remote(
    +    project: Project,
    +    ledger: Ledger,
    +    publisher: Any,
    +    *,
    +    only: set[str] | None = None,
    +    page_ids: set[str] | None = None,
    +) -> tuple[list[str], list[str], set[str]]:
         """Capture while local files still represent the previous accepted output."""
         if not hasattr(publisher, "pull_changes"):
             return [], [], set()
    @@ -341,7 +363,11 @@ def _capture_remote(project: Project, ledger: Ledger, publisher: Any, *, only: s
                 "growi_path": page.path, "page_id": page.page_id, "revision_id": page.revision_id,
                 "marker_id": publisher.page_marker_id(project, path), "marker_seed": publisher.page_marker_seed(project, path),
             } for path, page in discovered.items()})
    -    pages = {path: row for path, row in ledger.published_pages.items() if Path(path).parent.as_posix() in folders}
    +    pages = {
    +        path: row for path, row in ledger.published_pages.items()
    +        if Path(path).parent.as_posix() in folders
    +        and (page_ids is None or str(row.get("page_id") or "") in page_ids)
    +    }
         unchanged = {doc for doc, folder in folders.items()
                      if ledger.published_documents.get(doc, {}).get("content_sha256") == _content_hash(folder)}
         pulled, failures, blocked = publisher.pull_changes(project, pages, unchanged)
    @@ -351,6 +377,136 @@ def _capture_remote(project: Project, ledger: Ledger, publisher: Any, *, only: s
         return pulled, failures, blocked
    
    
    +def _capture_detected(
    +    project: Project,
    +    ledger: Ledger,
    +    publisher: Any,
    +    settings: Any,
    +    *,
    +    force_inventory: bool = False,
    +) -> tuple[list[str], list[str], set[str], dict[str, Any], tuple[Any, Any] | None]:
    +    """Use activities as hints and the normal pull path as authority."""
    +
    +    from .activity import ActivityDetector
    +    from .human_changes import HumanStore
    +    from graph.growi.client import _page_stamps
    +
    +    if not hasattr(publisher.client, "list_activities") or not hasattr(publisher.client, "list_all_pages"):
    +        pulled, failures, blocked = _capture_remote(project, ledger, publisher)
    +        return pulled, failures, blocked, {
    +            "fallback_reason": "activity_api_not_supported_by_client",
    +            "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
    +        }, None
    +
    +    detector = ActivityDetector(
    +        project,
    +        endpoint=_growi_url(settings),
    +        boundary=str(publisher.connection.root_path),
    +        overlap_seconds=int(getattr(settings, "human_sync_activity_overlap_seconds", 60)),
    +    )
    +    batch = detector.poll(publisher.client, ledger.published_pages)
    +    inventory = None
    +    try:
    +        cursor = detector.cursor()
    +    except (OSError, ValueError, TypeError):
    +        cursor = {}
    +    last_inventory = str(cursor.get("last_inventory_at") or "")
    +    audit_seconds = int(getattr(settings, "human_sync_activity_audit_seconds", 3600))
    +    audit_due = not last_inventory
    +    if last_inventory and audit_seconds > 0:
    +        try:
    +            from datetime import datetime, timezone
    +
    +            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_inventory.replace("Z", "+00:00"))).total_seconds()
    +            audit_due = age >= audit_seconds
    +        except ValueError:
    +            audit_due = True
    +    discovery_failures: list[str] = []
    +    if force_inventory or batch.fallback_reason or batch.unknown_page_ids or audit_due:
    +        # A reset cursor must point to the beginning of the inventory window.
    +        # An edit that lands while the inventory is running will then remain in
    +        # the next activity overlap instead of being skipped by a post-scan
    +        # "now" timestamp.
    +        from datetime import datetime, timezone
    +
    +        inventory_anchor_at = datetime.now(timezone.utc).isoformat()
    +        inventory = detector.inventory(publisher.client, ledger.published_pages)
    +        batch.selected_page_ids.update(inventory.selected_page_ids)
    +        store = HumanStore(project)
    +        for page in inventory.discovered_pages:
    +            stamps = _page_stamps(page.body)
    +            marker = stamps[0].group("id") if len(stamps) == 1 else ""
    +            baseline = store.page(marker) if marker else {}
    +            local_path = str(baseline.get("local_path") or "")
    +            if marker and local_path and store.prepared_match(marker, page):
    +                ledger.published_pages[local_path] = {
    +                    "growi_path": page.path,
    +                    "page_id": page.page_id,
    +                    "revision_id": str(baseline.get("prepared_revision") or ""),
    +                    "marker_id": marker,
    +                    "marker_seed": local_path,
    +                }
    +                batch.selected_page_ids.add(page.page_id)
    +            elif marker:
    +                reason = "unledgered owned page has no exact prepared publication evidence"
    +                if local_path:
    +                    row = {"marker_id": marker, "page_id": page.page_id,
    +                           "growi_path": page.path, "revision_id": ""}
    +                    store.block_page(local_path, row, reason, page)
    +                batch.metrics.setdefault("inventory_blocks", 0)
    +                batch.metrics["inventory_blocks"] += 1
    +                discovery_failures.append(f"inventory: {page.path}: {reason}")
    +        batch.metrics.update({f"inventory_{key}": value for key, value in inventory.metrics.items()})
    +    pulled, failures, blocked = _capture_remote(
    +        project, ledger, publisher, page_ids=batch.selected_page_ids
    +    ) if batch.selected_page_ids else ([], [], set())
    +    failures.extend(discovery_failures)
    +    if inventory is not None:
    +        ambiguous = [row for row in inventory.classifications
    +                     if row["classification"] in {"ambiguous", "duplicate_ownership"}]
    +        failures.extend(
    +            f"inventory: {row['classification']}: {row.get('path') or row.get('marker_id') or ''}"
    +            for row in ambiguous
    +        )
    +    reported_fallback = batch.fallback_reason
    +    if not failures:
    +        resettable = {
    +            "endpoint_changed", "malformed_cursor", "sequence_gap", "cursor_too_old",
    +            "activity_pagination_gap", "clock_skew", "malformed_activity_response",
    +            "activity_permission_gap",
    +        }
    +        if inventory is not None and batch.fallback_reason in resettable:
    +            batch.cursor = {
    +                "schema_version": 1,
    +                "endpoint_identity": detector.endpoint_identity,
    +                "last_processed_at": inventory_anchor_at,
    +                "ids_at_last_timestamp": [],
    +                "recent_ids": [],
    +                "last_sequence": None,
    +            }
    +            batch.fallback_reason = ""
    +        if not batch.cursor:
    +            batch.cursor = detector.cursor()
    +            batch.cursor["endpoint_identity"] = detector.endpoint_identity
    +        if inventory is not None:
    +            from datetime import datetime, timezone
    +
    +            batch.cursor["last_inventory_at"] = datetime.now(timezone.utc).isoformat()
    +    metrics = {
    +        **batch.metrics,
    +        "fallback_reason": reported_fallback,
    +        "forced_inventory": force_inventory,
    +        "audit_due": audit_due,
    +        "audit_interval_seconds": audit_seconds,
    +        "inventory_performed": inventory is not None,
    +        "authoritative_pages_fetched": len(batch.selected_page_ids),
    +        "classifications": inventory.classifications if inventory is not None else [],
    +        "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
    +    }
    +    cursor_commit = (detector, batch) if not failures and not batch.fallback_reason and batch.cursor else None
    +    return pulled, failures, blocked, metrics, cursor_commit
    +
    +
     def _published_page_row(project: Project, publisher: Any, path: str, page: Any) -> dict[str, Any]:
         row = {"growi_path": page.path, "page_id": page.page_id, "revision_id": page.revision_id,
                "marker_id": publisher.page_marker_id(project, path),
    @@ -464,7 +620,7 @@ def _publish_sweep(
                         if not path.startswith(published_prefixes)
                     }
                 ledger.published_pages.update(updated_pages)
    -            log.info("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
    +            log.debug("run=%s pages=%d stage=publish elapsed=%.2fs", run_id, len(pages), time.monotonic() - started)
                 if on_progress:
                     on_progress({
                         "stage": "growi-publish",
    @@ -646,11 +802,18 @@ def sync_once(
                 for rel in wanted or ()
                 if rel in scan.files or rel in ledger.sources
             }
    +        if on_progress:
    +            on_progress({"stage": "capture", "step": "start", "document": "<batch>", "total": 1})
             # Preserve the actual common ancestor before parse, deletion or any tier
             # changes local pages. Late remote edits are handled by revision preflight.
             captured_pages, capture_failures, _blocked = _capture_remote(
                 project, ledger, publisher, only=scoped_raw if wanted is not None else None,
             )
    +        if on_progress:
    +            on_progress({
    +                "stage": "capture", "step": "done", "document": "<batch>",
    +                "current": 1, "total": 1, "pages": len(captured_pages),
    +            })
             save_ledger(ledger_path, ledger)
             if capture_failures:
                 if history_enabled:
    @@ -660,7 +823,8 @@ def sync_once(
                     HumanStore(project).audit()
                     checkpoint_live(project, f"capture blocked GROWI revision {run_id}")
                 return {"run_id": run_id, "scan": scan, "done": [], "failures": capture_failures,
    -                    "cancelled": False, "index_paths": sorted(scoped_raw) if wanted is not None else None}
    +                    "cancelled": False, "index_paths": sorted(scoped_raw) if wanted is not None else None,
    +                    "human_sync": dict(getattr(publisher, "human_sync_summary", {}))}
             touched_raw: set[str] = set()
             deleted = scan.deleted if wanted is None else sorted(set(scan.deleted) & wanted)
             removed, touched, remove_failures = _remove_sources(project, ledger, {
    @@ -690,7 +854,7 @@ def sync_once(
                 if (str(getattr(settings, "embed_backend", "server")) == "off"
                         and isinstance(exc, ValueError)
                         and str(exc) == "the linker requires WIKI_EMBED_BACKEND=server"):
    -                log.info("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    +                log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
                 else:
                     log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
                 embedder = None
    @@ -710,11 +874,12 @@ def sync_once(
                     requested_resume = False
                 started = time.monotonic()
                 try:
    -                log.info("run=%s path=%s stage=parse start", run_id, rel)
    +                log.debug("run=%s path=%s stage=parse start", run_id, rel)
                     if on_progress:
                         on_progress({
                             "stage": "parse",
                             "step": "start",
    +                        "document": raw_rel,
                             "file": rel,
                             "parser": item.parser,
                             "bytes": item.size,
    @@ -737,7 +902,7 @@ def sync_once(
                         ):
                             assert previous_markdown is not None
                             markdown = previous_markdown
    -                        log.info("run=%s path=%s stage=parse resumed", run_id, rel)
    +                        log.debug("run=%s path=%s stage=parse resumed", run_id, rel)
                             if on_progress:
                                 on_progress({"stage": "parse", "step": "resumed", "file": rel})
                         else:
    @@ -754,6 +919,7 @@ def sync_once(
                         on_progress({
                             "stage": "parse",
                             "step": "done",
    +                        "document": raw_rel,
                             "file": rel,
                             "characters": len(markdown),
                             "elapsed_seconds": round(time.monotonic() - started, 1),
    @@ -761,11 +927,17 @@ def sync_once(
                     _write_raw(project.raw_file(raw_rel), markdown)
                     wiki_started = time.monotonic()
                     if on_progress:
    -                    on_progress({"stage": "wiki", "step": "start", "file": raw_rel})
    +                    on_progress({"stage": "wiki", "step": "start", "document": raw_rel, "file": raw_rel})
    +                wiki_progress = None
    +                if on_progress:
    +                    def wiki_progress(event: dict[str, Any], *, _raw_rel: str = raw_rel) -> None:
    +                        enriched = dict(event)
    +                        enriched.setdefault("document", _raw_rel)
    +                        on_progress(enriched)
                     with _progress_heartbeat(on_progress, stage="wiki", file=raw_rel):
                         result = write_wiki_pages(
                             project, raw_rel, mode=str(settings.ingest_mode), settings=settings,
    -                        llm=model, embedder=embedder, on_progress=on_progress,
    +                        llm=model, embedder=embedder, on_progress=wiki_progress,
                             resume=requested_resume,
                             stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                             identity_seed=str(
    @@ -784,6 +956,7 @@ def sync_once(
                         on_progress({
                             "stage": "wiki",
                             "step": "done",
    +                        "document": raw_rel,
                             "file": raw_rel,
                             "touched_documents": len(result.touched),
                             "elapsed_seconds": round(time.monotonic() - wiki_started, 1),
    @@ -800,7 +973,7 @@ def sync_once(
                         "reason": getattr(result, "reason", ""),
                         "human_edits_overwritten": list(getattr(result, "human_edits_overwritten", [])),
                     })
    -                log.info("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
    +                log.debug("run=%s path=%s stage=generate elapsed=%.2fs", run_id, rel, time.monotonic() - started)
                 except asyncio.CancelledError:
                     cancelled = True
                     break
    @@ -888,6 +1061,7 @@ def sync_once(
             "failures": failures,
             "cancelled": cancelled,
             "index_paths": index_paths,
    +        "human_sync": dict(getattr(publisher, "human_sync_summary", {})),
         }
    
    
    @@ -1256,7 +1430,7 @@ def restore_publication(
                     continue
                 if candidate_store.get(prepared["prepared_remote_blob"]) == page.body:
                     known_revisions.setdefault(page.page_id, set()).add(page.revision_id)
    -                log.info("human_sync event=recover_exact_prepared page=%s revision=%s", page.page_id, page.revision_id)
    +                log.debug("human_sync event=recover_exact_prepared page=%s revision=%s", page.page_id, page.revision_id)
                     return True
             return False
    
    @@ -1385,7 +1559,7 @@ def restore_publication(
         return checkpoint_live(live, "restore last-good publication")
    
    
    -def pull_growi_once(settings: Any) -> dict[str, Any]:
    +def pull_growi_once(settings: Any, *, force_inventory: bool = False) -> dict[str, Any]:
         """Pull user revisions into local wiki state without rebuilding or publishing."""
         project = open_project(settings)
         history_enabled = (project.root / ".git").is_dir()
    @@ -1402,18 +1576,44 @@ def pull_growi_once(settings: Any) -> dict[str, Any]:
         with _lock(project):
             ledger = load_ledger(ledger_path)
             try:
    -            pulled, failures, _blocked = _capture_remote(project, ledger, publisher)
    +            pulled, failures, _blocked, detector, cursor_commit = _capture_detected(
    +                project, ledger, publisher, settings, force_inventory=force_inventory
    +            )
                 save_ledger(ledger_path, ledger)
    +            from .human_changes import HumanStore
    +
    +            operator = HumanStore(project).project_summary()
    +            detector["operator"] = {key: operator[key] for key in ("counts", "unresolved", "blocked")}
    +            cursor_backup: dict[str, Any] | None = None
    +            cursor_existed = False
    +            if cursor_commit is not None:
    +                activity_detector, batch = cursor_commit
    +                cursor_existed = activity_detector.path.exists()
    +                cursor_backup = read_json(activity_detector.path) if cursor_existed else None
    +                activity_detector.commit(batch)
                 if history_enabled:
                     from .history import checkpoint_live
    -                from .human_changes import HumanStore
    
                     HumanStore(project).audit()
    -                checkpoint_live(project, f"pull GROWI {run_id}")
    +                try:
    +                    checkpoint_live(project, f"pull GROWI {run_id}")
    +                except Exception:
    +                    if cursor_commit is not None:
    +                        if cursor_existed and cursor_backup is not None:
    +                            write_json_atomic(activity_detector.path, cursor_backup)
    +                        else:
    +                            activity_detector.path.unlink(missing_ok=True)
    +                    raise
             except Exception as exc:
                 pulled = []
                 failures = [f"pull: {type(exc).__name__}: {exc}"]
    -    return {"run_id": run_id, "done": [{"status": "pulled", "path": path} for path in pulled], "failures": failures}
    +            detector = {"fallback_reason": "pull_exception"}
    +    return {
    +        "run_id": run_id,
    +        "done": [{"status": "pulled", "path": path} for path in pulled],
    +        "failures": failures,
    +        "human_sync": {**detector, **dict(getattr(publisher, "human_sync_summary", {}))},
    +    }
    
    
     def build_wiki_only(settings: Any, *, only: list[str] | None = None, force: bool = False, on_progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    @@ -1481,7 +1681,7 @@ def link_raw(settings: Any, *, only: list[str] | None = None, force: bool = Fals
                 if (str(getattr(settings, "embed_backend", "server")) == "off"
                         and isinstance(exc, ValueError)
                         and str(exc) == "the linker requires WIKI_EMBED_BACKEND=server"):
    -                log.info("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
    +                log.debug("run=%s embedder disabled (WIKI_EMBED_BACKEND=off)", run_id)
                 else:
                     log.warning("run=%s stage=embedder error=%s: %s", run_id, type(exc).__name__, exc)
                 embedder = None
    @@ -1525,7 +1725,8 @@ def publish_only(settings: Any) -> dict[str, Any]:
                 from .history import checkpoint_live
    
                 checkpoint_live(project, f"publish {run_id}" if not failures else f"publish {run_id} (incomplete)")
    -    return {"run_id": run_id, "done": [], "failures": failures}
    +    return {"run_id": run_id, "done": [], "failures": failures,
    +            "human_sync": dict(getattr(publisher, "human_sync_summary", {}))}
    
    
     def republish_if_stale(settings: Any) -> dict[str, Any] | None:
    ✓ • 10ms

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/graph/linker/service.py
    @@ -520,3 +520,3 @@
                 reported_chunks = len(stale_ids) if incremental_scope else len(to_describe)
    -            metadata_total = len(to_describe)
    +            metadata_total = max(len(to_describe), reported_chunks)
                 if on_progress:

## Activity

    $ python -c 'from publisher.progress import SyncProgress; p=SyncProgress(); p.add_documents(["a.docx", "b.docx"]); p.on_event({"stage":"parse","step":"start","document":"a.docx"}); p.on_event({"stage":"parse","step":"done","document":"a.docx","current":1,"total":1}); p.mark_completed(["a.docx"]); p.close()' && python -m py_compile main.py publisher/progress.py publisher/pipeline.py publisher/index.py graph/wiki/pipeline.py graph/linker/service.py && git diff --check
    [a.docx] parser: 0.0s
    Documents: 1/2 processed
    ✓ • 1.70s

## Assistant

The renderer passes a synthetic two-document event flow: it reports one transient parser bar, one timing line, and a single `Documents 1/2` summary without accumulating bars. I’m running the focused pipeline tests now to catch event-contract regressions.

## Activity

    $ pytest -q tests/test_mount_diff_pipeline.py tests/test_update_tiers.py
    /bin/bash: pytest: command not found
    ✗ (127) • 0ms

## Activity

    $ python -m pytest -q tests/test_mount_diff_pipeline.py tests/test_update_tiers.py
    /home/seigyo/c_repo/bhavneek/llm-wiki-neo/llm-wiki-air/.venv/bin/python: No module named pytest
    ✗ (1) • 20ms

## Activity

    $ python main.py -h >/tmp/llm-wiki-main-help.txt && sed -n '1,4p' /tmp/llm-wiki-main-help.txt && git status --short
    usage: python main.py [-h] [--project PROJECT] [--data-root DATA_ROOT] [-v]
                          {check,convert,sync,watch,queue,build,wiki,publish,pull,human,index,reset,link} ...
    
    One entry point for the publisher. `python main.py -h`.
     M .gitignore
     M README.md
     M graph/config.py
     M graph/growi/client.py
     M graph/linker/service.py
     M graph/wiki/legacy.py
     M graph/wiki/pipeline.py
     M handoff-conflicts.md
     M jev/backends/torch.py
     M jev/engine.py
     M main.py
     M publisher/human_changes.py
     M publisher/index.py
     M publisher/pipeline.py
     M tests/test_human_changes.py
    ?? configs/speed_test.ini
    ?? docs/human-semantic-calibration.json
    ?? publisher/activity.py
    ?? publisher/human_semantic.py
    ?? publisher/legacy_recovery.py
    ?? publisher/live_verification.py
    ?? publisher/progress.py
    ?? publisher/prompts/
    ?? tests/test_human_sync_rollout.py
    ✓ • 155ms

## Activity

    $ rg -n 'tqdm|dependencies|install_requires' pyproject.toml requirements*.txt setup.cfg setup.py 2>/dev/null || true; rg -n 'stage": "(planner|capture|linker-entities|linker-edges|writer)' main.py publisher graph | head -100; git diff --numstat -- publisher/progress.py
    pyproject.toml:7:dependencies = [
    publisher/pipeline.py:806:            on_progress({"stage": "capture", "step": "start", "document": "<batch>", "total": 1})
    publisher/pipeline.py:814:                "stage": "capture", "step": "done", "document": "<batch>",
    graph/linker/service.py:463:                on_progress({"stage": "linker-entities", "step": "start", "document": rel})
    graph/linker/service.py:581:                    "stage": "linker-entities", "step": "done", "document": rel,
    graph/linker/service.py:615:                on_progress({"stage": "linker-edges", "step": "start", "document": rel})
    graph/linker/service.py:743:                    "stage": "linker-edges", "step": "targets", "document": rel,
    graph/linker/service.py:846:                    "stage": "linker-edges", "step": "done", "document": rel,
    ✓ • 7ms

## Activity

    $ sed -n '1,70p' pyproject.toml; git diff --numstat -- main.py publisher/pipeline.py publisher/index.py graph/wiki/pipeline.py graph/linker/service.py
    [project]
    name = "llm-wiki"
    version = "0.1.0"
    description = "Add your description here"
    readme = "README.md"
    requires-python = ">=3.13"
    dependencies = [
      "fastapi",
      "httpx==0.28.1",
      "huggingface-hub",
      "langchain-core==1.6.3",
      "langchain-openai==1.6.2",
      "langgraph",
      "uvicorn[standard]",
      "openpyxl==3.1.5",
      "oletools==0.60.2",
      "pathspec==1.1.1",
      "pydantic==2.13.5",
      "pypdf==6.18.1",
      "python-docx==1.2.0",
      "python-dotenv==1.2.3",
      "python-pptx==1.0.2",
      "requests==2.34.2",
      "sqlite-vec==0.1.9",
      "annotated-types==0.8.0",
      "anyio==4.15.1",
      "certifi==2026.7.22",
      "cffi==2.1.1",
      "charset-normalizer==3.5.1",
      "colorclass==2.2.2",
      "cryptography==50.0.1",
      "distro==1.9.0",
      "easygui==0.98.3",
      "et-xmlfile==2.0.0",
      "h11==0.16.0",
      "httpcore==1.0.9",
      "httpcore2==2.12.0",
      "httpx2==2.12.0",
      "idna==3.19",
      "jiter==0.17.0",
      "jsonpatch==1.33",
      "jsonpointer==3.1.1",
      "langchain-protocol==0.0.19",
      "langsmith==0.12.4",
      "lxml==6.1.3",
      "msoffcrypto-tool==6.0.0",
      "numpy",
      "olefile==0.47",
      "openai==3.13.0",
      "orjson==3.12.0",
      "packaging==26.3",
      "pcodedmp==1.2.6",
      "pillow==12.3.0",
      "pycparser==3.0",
      "pydantic-core==2.46.5",
      "pyparsing==3.3.2",
      "pyyaml==6.0.3",
      "regex==2026.9.10",
      "requests-toolbelt==1.0.0",
      "sniffio==1.3.1",
      "tenacity==9.1.4",
      "tiktoken==0.14.0",
      "tokenizers>=0.21",
      "torch>=2.4",
      "transformers>=5.0",
      "truststore==0.10.4",
      "typing-extensions==4.16.0",
      "typing-inspection==0.4.4",
      "urllib3==2.7.0",
      "uuid-utils==0.17.1",
    22    0    graph/linker/service.py
    26    1    graph/wiki/pipeline.py
    138    43    main.py
    4    0    publisher/index.py
    220    19    publisher/pipeline.py
    ✓ • 0ms

## Assistant

The focused test command cannot run because this environment has no `pytest` module, but syntax, CLI help, and a synthetic renderer flow pass. I also found `tqdm` is only transitive in the project metadata, so I’m making it an explicit dependency to ensure the new progress display works in clean installs.

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/pyproject.toml
    @@ -65,2 +65,3 @@
       "transformers>=5.0",
    +  "tqdm>=4.70",
       "truststore==0.10.4",

## Activity

    $ sed -n '1188,1218p' graph/wiki/pipeline.py; sed -n '1310,1335p' graph/wiki/pipeline.py
        tokens: dict[int, set[str]],
        model: ModelPort,
        config: WikiConfig,
        work_root: Path,
        seed_root: Path,
        source_line_count: int,
        stop_check: StopCheck,
        on_progress: Progress,
        parents: dict[str, str] | None = None,
    ) -> RewriteResult:
        from ..formats.context import context_block
    
        if len(page.owner_ranges) != 1:
            raise PipelineError(f"page {page.number} must own one contiguous range")
        start, end = page.owner_ranges[0]
        page_units = _page_units(page, units)
        task_dir = work_root / f"page-{page.number:03d}"
        task_dir.mkdir(parents=True, exist_ok=True)
    
        evidence, _research = await _research_references(
            page, pages=pages, tokens=tokens, model=model,
            config=config, work_root=work_root, seed_root=seed_root,
            stop_check=stop_check, on_progress=on_progress,
        )
        facts = [fact for item in evidence for fact in item.facts]
        # ponytail: sections stay in source order; add reordering only if a smoke run needs it.
        sections = split_sections(
            lines, start, end,
            target=config.section_target_lines, min_lines=config.section_min_lines,
        )
        buckets = assign_facts(facts, sections)
            )
            if resumed is not None:
                results.append(resumed)
                _emit(on_progress, "writer", "page_start", page=page.title, current=0, total=len(pages), resumed=True)
                _emit(on_progress, "writer", "page_done", page=page.title, total=len(pages), resumed=True)
                _emit(on_progress, "rewrite", "page_resumed",
                      current=len(results), total=len(pages), page=page.title)
            else:
                output_path.unlink(missing_ok=True)
                state_path.unlink(missing_ok=True)
                pending.append(page)
    
        async def one(page: SeedPage) -> RewriteResult:
            async with semaphore:
                _emit(on_progress, "writer", "page_start", page=page.title, current=0, total=len(pages))
                result = await _rewrite_page(
                    page, pages=pages, lines=lines, units=units, tokens=tokens,
                    model=model, config=config, work_root=work_root, seed_root=seed_root,
                    source_line_count=source_line_count,
                    stop_check=stop_check, on_progress=on_progress,
                    parents=parents,
                )
                _emit(on_progress, "writer", "page_done", page=page.title, total=len(pages))
                return result
    
        for completed, task in enumerate(
    ✓ • 30ms

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/graph/wiki/pipeline.py
    @@ -1212,2 +1212,3 @@
         facts = [fact for item in evidence for fact in item.facts]
    +    _emit(on_progress, "writer", "page_start", page=page.title, current=0, total=len(pages))
         # ponytail: sections stay in source order; add reordering only if a smoke run needs it.
    @@ -1323,3 +1324,2 @@
             async with semaphore:
    -            _emit(on_progress, "writer", "page_start", page=page.title, current=0, total=len(pages))
                 result = await _rewrite_page(

## Activity

    $ python -c 'from publisher.progress import SyncProgress; p=SyncProgress(); p.add_documents(["a.docx", "b.docx"]); events=[{"stage":"planner","step":"start","document":"a.docx"},{"stage":"planner","step":"done","document":"a.docx","current":2,"total":2},{"stage":"research","step":"page_start","document":"a.docx","total":2},{"stage":"research","step":"page_done","document":"a.docx","total":2},{"stage":"research","step":"page_done","document":"a.docx","total":2},{"stage":"writer","step":"page_start","document":"a.docx","total":2},{"stage":"writer","step":"page_done","document":"a.docx","total":2},{"stage":"writer","step":"page_done","document":"a.docx","total":2}]; [p.on_event(e) for e in events]; p.mark_completed(["a.docx"]); p.close()' && python -m py_compile main.py publisher/progress.py publisher/pipeline.py publisher/index.py graph/wiki/pipeline.py graph/linker/service.py && git diff --check
    [a.docx] planner: 0.0s
    [a.docx] wiki research: 0.0s
    [a.docx] wiki writer + judge: 0.0s
    Documents: 1/2 processed
    ✓ • 1.69s

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/publisher/index.py
    @@ -458,2 +458,4 @@
         failures: list[str] = []
    +    if on_progress:
    +        on_progress({"stage": "index", "step": "start", "run_id": run_id})
         folders = _folders(project)
    @@ -493,3 +495,3 @@
         if on_progress:
    -        on_progress({"stage": "index", "step": "start", "current": 0, "total": total, "run_id": run_id})
    +        on_progress({"stage": "index", "step": "progress", "current": 0, "total": total, "run_id": run_id})
         with contextlib.nullcontext() if locked else _lock(project):

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/publisher/progress.py
    @@ -114,2 +114,4 @@
         def _document(event: dict[str, Any]) -> str:
    +        if str(event.get("stage") or "") in {"capture", "growi-publish", "index"}:
    +            return "<batch>"
             value = event.get("document") or event.get("file") or "<batch>"

## Activity

    $ rg -n '_progress_heartbeat\(' publisher/pipeline.py; sed -n '35,68p' publisher/pipeline.py; sed -n '895,945p' publisher/pipeline.py
    46:def _progress_heartbeat(
    887:                with _progress_heartbeat(on_progress, stage="parse", file=rel):
    937:                with _progress_heartbeat(on_progress, stage="wiki", file=raw_rel):
    1009:                with _progress_heartbeat(on_progress, stage="linker", file="batch"):
    
    from .ledger import Ledger, load_ledger, save_ledger
    from .scanner import Scan, SourceFile, scan_mount
    
    log = logging.getLogger(__name__)
    PARSER_TIMEOUT = 7200.0
    PARSE_MIN_RATIO = 0.30  # a re-parse this much smaller than before is treated as broken
    PARSE_GATE_MIN_CHARS = 2000  # tiny documents may legitimately lose most text
    
    
    @contextmanager
    def _progress_heartbeat(
        callback: Callable[[dict[str, Any]], None] | None,
        *,
        stage: str,
        file: str,
        interval: float = 10.0,
    ):
        if callback is None:
            yield
            return
        stopped = threading.Event()
        started = time.monotonic()
    
        def pulse() -> None:
            while not stopped.wait(interval):
                callback({
                    "stage": stage,
                    "step": "waiting",
                    "file": file,
                    "elapsed_seconds": round(time.monotonic() - started),
                })
    
        thread = threading.Thread(target=pulse, name=f"{stage}-progress", daemon=True)
                            previous_source,
                            item,
                            raw_path,
                            requested_resume=requested_resume,
                            classification=classification,
                            known_source_sha256=str(details.get("source_sha256") or ""),
                            known_source_blob_oid=str(details.get("source_blob_oid") or ""),
                        ):
                            assert previous_markdown is not None
                            markdown = previous_markdown
                            log.debug("run=%s path=%s stage=parse resumed", run_id, rel)
                            if on_progress:
                                on_progress({"stage": "parse", "step": "resumed", "file": rel})
                        else:
                            parse_args = (
                                {"previous_markdown": previous_markdown}
                                if previous_markdown is not None
                                else {}
                            )
                            markdown = _parse(item, project.mount / rel, settings, **parse_args)
                    _assert_source_unchanged(item, project.mount / rel)
                    if previous_markdown is not None and classification != "forced":
                        _check_parse_size(rel, previous_markdown, markdown)
                    if on_progress:
                        on_progress({
                            "stage": "parse",
                            "step": "done",
                            "document": raw_rel,
                            "file": rel,
                            "characters": len(markdown),
                            "elapsed_seconds": round(time.monotonic() - started, 1),
                        })
                    _write_raw(project.raw_file(raw_rel), markdown)
                    wiki_started = time.monotonic()
                    if on_progress:
                        on_progress({"stage": "wiki", "step": "start", "document": raw_rel, "file": raw_rel})
                    wiki_progress = None
                    if on_progress:
                        def wiki_progress(event: dict[str, Any], *, _raw_rel: str = raw_rel) -> None:
                            enriched = dict(event)
                            enriched.setdefault("document", _raw_rel)
                            on_progress(enriched)
                    with _progress_heartbeat(on_progress, stage="wiki", file=raw_rel):
                        result = write_wiki_pages(
                            project, raw_rel, mode=str(settings.ingest_mode), settings=settings,
                            llm=model, embedder=embedder, on_progress=wiki_progress,
                            resume=requested_resume,
                            stop_check=(lambda: not should_continue()) if should_continue is not None else None,
                            identity_seed=str(
                                previous_source.get("id_seed")
                                or ((Path(raw_rel).parent / wiki_folder_name(Path(raw_rel).name)).as_posix() if previous_source else "")
    ✓ • 14ms

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/publisher/pipeline.py
    @@ -50,2 +50,3 @@
         file: str,
    +    document: str | None = None,
         interval: float = 10.0,
    @@ -60,3 +61,3 @@
             while not stopped.wait(interval):
    -            callback({
    +            event = {
                     "stage": stage,
    @@ -65,3 +66,6 @@
                     "elapsed_seconds": round(time.monotonic() - started),
    -            })
    +            }
    +            if document:
    +                event["document"] = document
    +            callback(event)
     
    @@ -886,3 +890,3 @@
                         })
    -                with _progress_heartbeat(on_progress, stage="parse", file=rel):
    +                with _progress_heartbeat(on_progress, stage="parse", file=rel, document=raw_rel):
                         raw_path = project.raw_file(raw_rel)
    @@ -906,3 +910,3 @@
                             if on_progress:
    -                            on_progress({"stage": "parse", "step": "resumed", "file": rel})
    +                            on_progress({"stage": "parse", "step": "resumed", "document": raw_rel, "file": rel})
                         else:
    @@ -936,3 +940,3 @@
                             on_progress(enriched)
    -                with _progress_heartbeat(on_progress, stage="wiki", file=raw_rel):
    +                with _progress_heartbeat(on_progress, stage="wiki", file=raw_rel, document=raw_rel):
                         result = write_wiki_pages(

## Activity

    $ rg -n 'cmd_sync|work_once\(|scan\(' tests | head -200; rg -n 'assert.*call.*on_progress|mock.*on_progress' tests | head -100
    tests/test_update_tiers.py:745:            queue.scan(settings, settle_seconds=0)
    tests/test_update_tiers.py:746:            queue.scan(settings, settle_seconds=0, force=True)
    tests/test_mount_diff_pipeline.py:128:            queue.scan(settings, settle_seconds=0)
    tests/test_mount_diff_pipeline.py:142:            result = queue.scan(settings, settle_seconds=0)
    tests/test_mount_diff_pipeline.py:280:                result = queue.work_once(settings)
    tests/test_mount_diff_pipeline.py:424:            result = main.cmd_sync(args)
    tests/test_mount_diff_pipeline.py:1125:            queue.scan(settings, settle_seconds=0)
    tests/test_mount_diff_pipeline.py:1129:                    queue.work_once(settings)
    tests/test_mount_diff_pipeline.py:1283:    def _scan(self, **kwargs):
    tests/test_mount_diff_pipeline.py:1284:        result = queue.scan(self.settings, settle_seconds=0, **kwargs)
    tests/test_mount_diff_pipeline.py:1289:        result = queue.work_once(self.settings, on_event=events)
    tests/test_mount_diff_pipeline.py:1309:        scan = self._scan()
    tests/test_mount_diff_pipeline.py:1390:        self._scan()
    tests/test_mount_diff_pipeline.py:1408:        self._scan()
    tests/test_mount_diff_pipeline.py:1421:        self.assertEqual(self._scan()["deleted"], ["test.docx"])
    tests/test_mount_diff_pipeline.py:1433:        result = self._scan()
    tests/test_mount_diff_pipeline.py:1441:        self._scan()
    tests/test_mount_diff_pipeline.py:1444:        result = self._scan()
    tests/test_mount_diff_pipeline.py:1451:        self._scan()
    tests/test_mount_diff_pipeline.py:1456:            self.assertEqual(self._scan()["cancelled"], ["test.docx"])
    tests/test_mount_diff_pipeline.py:1469:        self._scan()
    tests/test_mount_diff_pipeline.py:1474:            scan = self._scan()
    tests/test_mount_diff_pipeline.py:1488:        self.assertEqual(self._scan()["added"], ["test.docx"])
    tests/test_mount_diff_pipeline.py:1496:        first = self._scan()
    tests/test_mount_diff_pipeline.py:1500:        second = self._scan()
    tests/test_mount_diff_pipeline.py:1512:        self._scan()
    tests/test_mount_diff_pipeline.py:1518:            scans.append(self._scan())
    tests/test_mount_diff_pipeline.py:1533:        self._scan()
    tests/test_mount_diff_pipeline.py:1539:            scans.append(self._scan())
    tests/test_mount_diff_pipeline.py:1554:        scan = self._scan()
    tests/test_mount_diff_pipeline.py:1563:        scan = self._scan()
    tests/test_mount_diff_pipeline.py:1578:        scan = self._scan()
    tests/test_mount_diff_pipeline.py:1596:        self.assertEqual(self._scan()["deleted"], ["test.docx"])
    tests/test_mount_diff_pipeline.py:1599:        self._scan()
    tests/test_mount_diff_pipeline.py:1612:            self._scan()
    tests/test_mount_diff_pipeline.py:1623:        self._scan()
    tests/test_mount_diff_pipeline.py:1634:        self._scan()
    tests/test_mount_diff_pipeline.py:1645:        self._scan()
    tests/test_mount_diff_pipeline.py:1655:        self._scan()
    tests/test_mount_diff_pipeline.py:1678:            result = self._scan(verify_content=True)
    tests/test_mount_diff_pipeline.py:1683:        self._scan()
    tests/test_mount_diff_pipeline.py:1687:            self._scan()
    tests/test_mount_diff_pipeline.py:1697:        self._scan()
    tests/test_mount_diff_pipeline.py:1702:            scans.append(self._scan())
    tests/test_mount_diff_pipeline.py:1716:        scan = self._scan(force=True)
    tests/test_mount_diff_pipeline.py:1726:        self._scan()
    tests/test_mount_diff_pipeline.py:1762:        self._scan()
    tests/test_mount_diff_pipeline.py:1786:        self._scan()
    tests/test_mount_diff_pipeline.py:430:        index.assert_called_once_with(settings, on_progress=None)
    ✓ • 1ms

## Activity

    $ sed -n '395,438p' tests/test_mount_diff_pipeline.py; sed -n '1260,1315p' tests/test_mount_diff_pipeline.py
                self.assertEqual(queue.resumable_operation_id(project), "lost-transaction")
                recovered = queue.recover(
                    project,
                    SimpleNamespace(wiki_linker_enabled=False),
                    preserve_operation_id="lost-transaction",
                )
    
                self.assertEqual(recovered, 1)
                self.assertEqual(queue.status(project)[0]["status"], "queued")
                self.assertTrue(checkpoint.exists())
    
        def test_sync_command_rescans_until_the_queue_is_drained(self) -> None:
            import main
    
            settings = SimpleNamespace(data_root="/tmp/data", target_name="test", mount_path="/tmp/mount")
            args = SimpleNamespace(items=["test.docx"], force=True, verbose=False)
            first = {"done": [{"path": "test.docx", "status": "added"}], "failures": []}
            second = {"done": [{"path": "test.docx", "status": "changed"}], "failures": []}
            project = object()
            with (
                patch.object(main, "_settings", return_value=settings),
                patch.object(main, "open_project", return_value=project),
                patch("publisher.pipeline.republish_if_stale", return_value=None),
                patch("publisher.queue.worker_lock", return_value=contextlib.nullcontext()),
                patch("publisher.queue.retry_failed") as retry,
                patch("publisher.queue.scan") as scan,
                patch("publisher.queue.work_once", side_effect=[first, second, None]) as work,
                patch("publisher.index.build_index", return_value={"done": [], "failures": []}) as index,
            ):
                result = main.cmd_sync(args)
    
            self.assertEqual(result, 0)
            retry.assert_called_once_with(project)
            self.assertEqual(work.call_count, 3)
            self.assertEqual(scan.call_count, 3)
            index.assert_called_once_with(settings, on_progress=None)
            self.assertTrue(scan.call_args_list[0].kwargs["force"])
            self.assertFalse(scan.call_args_list[1].kwargs["force"])
            self.assertTrue(all(call.kwargs["verify_content"] for call in scan.call_args_list))
    
        def test_small_paragraph_deletion_uses_model_and_scopes_linker(self) -> None:
            from graph.workspace import writer
    
            with tempfile.TemporaryDirectory() as tmp:
                    "mount_path": str(mount),
                    "target_name": target,
                    "concurrency": 1,
                    "wiki_rewrite_concurrency": 1,
                    "wiki_linker_concurrency": 1,
                    "ingest_concurrency": 1,
                    "service_max_agents": 1,
                }
            )
            self.project = open_project(self.settings)
            self.raw_rel = raw_name_for(self.source.name)
            self.events: list[dict] = []
            self.remote_may_exist = False
            self.addCleanup(self._cleanup_remote)
    
        def _cleanup_remote(self) -> None:
            if not self.remote_may_exist:
                return
            try:
                pipeline.reset_growi(self.settings)
            except Exception as exc:  # cleanup must not hide the test's real failure
                print(f"warning: failed to clean {self.settings.target_name}: {exc}")
    
        def _scan(self, **kwargs):
            result = queue.scan(self.settings, settle_seconds=0, **kwargs)
            return result
    
        def _work(self, callback=None):
            events = self.events.append if callback is None else callback
            result = queue.work_once(self.settings, on_event=events)
            if result and not result.get("failures") and any(
                row.get("status") in {"added", "changed", "deleted", "moved"}
                for row in result.get("done", [])
            ):
                self.remote_may_exist = True
            return result
    
        def _drain(self, limit: int = 8) -> list[dict]:
            results: list[dict] = []
            for _ in range(limit):
                if not queue.status(self.project):
                    return results
                result = self._work()
                self.assertIsNotNone(result, f"queue stopped with rows: {queue.status(self.project)}")
                self.assertFalse(result.get("failures"), result)
                results.append(result)
            self.fail(f"queue did not drain after {limit} jobs: {queue.status(self.project)}")
    
        def _build_initial(self):
            scan = self._scan()
            self.assertEqual(scan["added"], ["test.docx"])
            result = self._work()
            self.assertIsNotNone(result)
            self.assertFalse(result["cancelled"], result)
            self.assertFalse(result["failures"], result)
            self.remote_may_exist = True
    ✓ • 0ms

## Activity

    file changes: Completed · 2 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/main.py
    @@ -225,3 +225,4 @@
                 try:
    -                index = build_index(settings, on_progress=on_event)
    +                index_callback = on_event if args.verbose or progress.has_documents else None
    +                index = build_index(settings, on_progress=index_callback)
                 except Exception as exc:  # a stale table of contents must not fail a sync
    Update { move_path: None }: /home/seigyo/llm-wiki/publisher/progress.py
    @@ -79,2 +79,6 @@
     
    +    @property
    +    def has_documents(self) -> bool:
    +        return bool(self._documents)
    +
         def add_scan_result(self, result: dict[str, Any]) -> None:

## Activity

    $ python -m unittest tests.test_mount_diff_pipeline.MountDiffPipelineTests.test_sync_command_rescans_until_the_queue_is_drained
    E
    ======================================================================
    ERROR: MountDiffPipelineTests (unittest.loader._FailedTest.MountDiffPipelineTests)
    ----------------------------------------------------------------------
    AttributeError: module 'tests.test_mount_diff_pipeline' has no attribute 'MountDiffPipelineTests'
    
    ----------------------------------------------------------------------
    Ran 1 test in 0.000s
    
    FAILED (errors=1)
    ✗ (1) • 1.59s

## Activity

    $ rg -n '^class .*Test' tests/test_mount_diff_pipeline.py | head -30
    82:class DiffDocxFixtureTest(unittest.TestCase):
    107:class DiffPipelineSafetyTest(unittest.TestCase):
    1233:class MountDiffPipelineAcceptanceTest(unittest.TestCase):
    ✓ • 0ms

## Activity

    $ python -m unittest tests.test_mount_diff_pipeline.MountDiffPipelineAcceptanceTest.test_sync_command_rescans_until_the_queue_is_drained
    E
    ======================================================================
    ERROR: test_sync_command_rescans_until_the_queue_is_drained (unittest.loader._FailedTest.test_sync_command_rescans_until_the_queue_is_drained)
    ----------------------------------------------------------------------
    AttributeError: type object 'MountDiffPipelineAcceptanceTest' has no attribute 'test_sync_command_rescans_until_the_queue_is_drained'
    
    ----------------------------------------------------------------------
    Ran 1 test in 0.000s
    
    FAILED (errors=1)
    ✗ (1) • 1.66s

## Activity

    $ sed -n '100,135p' tests/test_mount_diff_pipeline.py; rg -n 'def test_sync_command' tests/test_mount_diff_pipeline.py
                make_minor_docx_change(minor, "minor-fixture-check")
                make_major_docx_change(major, "major-fixture-check")
                self.assertNotEqual(FIXTURE.read_bytes(), minor.read_bytes())
                self.assertNotEqual(FIXTURE.read_bytes(), major.read_bytes())
                self.assertNotEqual(minor.read_bytes(), major.read_bytes())
    
    
    class DiffPipelineSafetyTest(unittest.TestCase):
        def test_mount_raw_path_collision_fails_instead_of_overwriting_a_document(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                mount = Path(tmp)
                (mount / "same.docx").write_bytes(b"docx")
                (mount / "same_docx.md").write_text("markdown", encoding="utf-8")
    
                with self.assertRaisesRegex(ValueError, "same_docx.md"):
                    queue._snapshot(mount)
    
        def test_whole_folder_move_is_coalesced_into_identity_preserving_moves(self) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                mount = base / "mount"
                (mount / "team/sub").mkdir(parents=True)
                (mount / "team/a.md").write_text("A", encoding="utf-8")
                (mount / "team/sub/b.md").write_text("B", encoding="utf-8")
                settings = SimpleNamespace(
                    data_root=str(base / "data"), target_name="test", mount_path=str(mount)
                )
                project = open_project(settings)
                queue.scan(settings, settle_seconds=0)
                queue.finish(project, queue.claim(project, "slow"))
                with queue._connect(project) as conn:
                    observed = {str(row["rel"]): dict(row) for row in conn.execute("SELECT * FROM sources")}
                save_ledger(project.metadata / "pipeline.json", Ledger({
                    rel: {
                        "source_id": row["source_id"], "raw_rel": row["raw_rel"],
                        "source_sha256": row["source_sha256"],
    406:    def test_sync_command_rescans_until_the_queue_is_drained(self) -> None:
    ✓ • 0ms

## Activity

    $ python -m unittest tests.test_mount_diff_pipeline.DiffPipelineSafetyTest.test_sync_command_rescans_until_the_queue_is_drained
    .
    ----------------------------------------------------------------------
    Ran 1 test in 0.030s
    
    OK
    {"path": "test.docx", "status": "added"}
    {"path": "test.docx", "status": "changed"}
    ✓ • 1.73s

## Activity

    $ python -m unittest tests.test_update_tiers
    ..................................
    ----------------------------------------------------------------------
    Ran 34 tests in 1.779s
    
    OK
    ✓ • 3.41s

## Assistant

The targeted sync-command regression test and all 34 update-tier tests pass. I’m doing one final diff/status review now; the standalone `pytest` runner is unavailable, so I’m relying on the repository’s `unittest` coverage plus compile checks.

## Activity

    $ python -m unittest tests.test_mount_diff_pipeline
    F.......F.F..F......F......sssssssssssssssssssssssssss
    ======================================================================
    FAIL: test_diff_project_config_and_docx_fixture_exist (tests.test_mount_diff_pipeline.DiffDocxFixtureTest.test_diff_project_config_and_docx_fixture_exist)
    ----------------------------------------------------------------------
    Traceback (most recent call last):
      File "/home/seigyo/llm-wiki/tests/test_mount_diff_pipeline.py", line 84, in test_diff_project_config_and_docx_fixture_exist
        self.assertTrue(CONFIG.is_file(), CONFIG)
        ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^
    AssertionError: False is not true : /home/seigyo/llm-wiki/configs/diff_test_local.ini
    
    ======================================================================
    FAIL: test_incremental_linker_keeps_valid_peer_untouched_and_skips_search (tests.test_mount_diff_pipeline.DiffPipelineSafetyTest.test_incremental_linker_keeps_valid_peer_untouched_and_skips_search)
    ----------------------------------------------------------------------
    Traceback (most recent call last):
      File "/home/seigyo/llm-wiki/tests/test_mount_diff_pipeline.py", line 609, in test_incremental_linker_keeps_valid_peer_untouched_and_skips_search
        self.assertEqual(result.meta_calls, 0)
        ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^
    AssertionError: 1 != 0
    
    ======================================================================
    FAIL: test_incremental_scope_with_many_old_edges_checks_only_visible_ones (tests.test_mount_diff_pipeline.DiffPipelineSafetyTest.test_incremental_scope_with_many_old_edges_checks_only_visible_ones)
    ----------------------------------------------------------------------
    Traceback (most recent call last):
      File "/home/seigyo/llm-wiki/tests/test_mount_diff_pipeline.py", line 908, in test_incremental_scope_with_many_old_edges_checks_only_visible_ones
        self.assertEqual(
        ~~~~~~~~~~~~~~~~^
            set(result.affected_pages or []),
            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
            {"doc/001.md", f"{visible_doc}/001.md"},
            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
        )
        ^
    AssertionError: Items in the first set but not the second:
    'entity25/001.md'
    'entity14/001.md'
    'entity13/001.md'
    'entity28/001.md'
    'entity03/001.md'
    'entity22/001.md'
    'entity02/001.md'
    'entity04/001.md'
    'entity19/001.md'
    'entity16/001.md'
    'entity00/001.md'
    'entity20/001.md'
    'entity06/001.md'
    'entity23/001.md'
    'entity24/001.md'
    'entity07/001.md'
    'entity10/001.md'
    'entity17/001.md'
    'entity18/001.md'
    'entity27/001.md'
    'entity21/001.md'
    'entity11/001.md'
    'entity09/001.md'
    'entity05/001.md'
    'entity12/001.md'
    'entity26/001.md'
    'entity08/001.md'
    'entity15/001.md'
    'entity29/001.md'
    'entity01/001.md'
    
    ======================================================================
    FAIL: test_neo_incremental_linker_preserves_entity_direction_and_checks_only_visible_edges (tests.test_mount_diff_pipeline.DiffPipelineSafetyTest.test_neo_incremental_linker_preserves_entity_direction_and_checks_only_visible_edges)
    ----------------------------------------------------------------------
    Traceback (most recent call last):
      File "/home/seigyo/llm-wiki/tests/test_mount_diff_pipeline.py", line 732, in test_neo_incremental_linker_preserves_entity_direction_and_checks_only_visible_edges
        self.assertEqual(result.meta_calls, 0)
        ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^
    AssertionError: 1 != 0
    
    ======================================================================
    FAIL: test_rename_plus_edit_keeps_old_digest_until_the_new_blob_is_parsed (tests.test_mount_diff_pipeline.DiffPipelineSafetyTest.test_rename_plus_edit_keeps_old_digest_until_the_new_blob_is_parsed)
    ----------------------------------------------------------------------
    Traceback (most recent call last):
      File "/home/seigyo/llm-wiki/tests/test_mount_diff_pipeline.py", line 251, in test_rename_plus_edit_keeps_old_digest_until_the_new_blob_is_parsed
        self.assertEqual(result["failures"], [])
        ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^
    AssertionError: Lists differ: ["move: AttributeError: 'Publisher' object has no attribute 'doc_path'"] != []
    
    First list contains 1 additional elements.
    First extra element 0:
    "move: AttributeError: 'Publisher' object has no attribute 'doc_path'"
    
    - ["move: AttributeError: 'Publisher' object has no attribute 'doc_path'"]
    + []
    
    ----------------------------------------------------------------------
    Ran 54 tests in 3.879s
    
    FAILED (failures=5, skipped=27)
    {"path": "test.docx", "status": "added"}
    {"path": "test.docx", "status": "changed"}
    ✗ (1) • 5.49s

## Activity

    $ python -m py_compile main.py publisher/progress.py publisher/pipeline.py publisher/index.py graph/wiki/pipeline.py graph/linker/service.py pyproject.toml 2>&1 || true; git diff --check; git status --short; git diff --stat -- pyproject.toml main.py publisher/pipeline.py publisher/index.py graph/wiki/pipeline.py graph/linker/service.py; wc -l publisher/progress.py
      File "pyproject.toml", line 6
        requires-python = ">=3.13"
        ^^^^^^^^^^^^^^^
    SyntaxError: cannot assign to expression here. Maybe you meant '==' instead of '='?
     M .gitignore
     M README.md
     M graph/config.py
     M graph/growi/client.py
     M graph/linker/service.py
     M graph/wiki/legacy.py
     M graph/wiki/pipeline.py
     M handoff-conflicts.md
     M jev/backends/torch.py
     M jev/engine.py
     M main.py
     M publisher/human_changes.py
     M publisher/index.py
     M publisher/pipeline.py
     M pyproject.toml
     M tests/test_human_changes.py
    ?? configs/speed_test.ini
    ?? docs/human-semantic-calibration.json
    ?? publisher/activity.py
    ?? publisher/human_semantic.py
    ?? publisher/legacy_recovery.py
    ?? publisher/live_verification.py
    ?? publisher/progress.py
    ?? publisher/prompts/
    ?? tests/test_human_sync_rollout.py
     graph/linker/service.py |  22 +++++
     graph/wiki/pipeline.py  |  27 +++++-
     main.py                 | 182 ++++++++++++++++++++++++++--------
     publisher/index.py      |   6 ++
     publisher/pipeline.py   | 253 +++++++++++++++++++++++++++++++++++++++++++-----
     pyproject.toml          |   1 +
     6 files changed, 423 insertions(+), 68 deletions(-)
    246 publisher/progress.py
    ✓ • 101ms

## Activity

    $ python -c 'import tomllib; tomllib.loads(open("pyproject.toml","rb").read()); import tqdm; print(tqdm.__version__)' && python -m py_compile main.py publisher/progress.py publisher/pipeline.py publisher/index.py graph/wiki/pipeline.py graph/linker/service.py && git diff --check
    Traceback (most recent call last):
      File "<string>", line 1, in <module>
        import tomllib; tomllib.loads(open("pyproject.toml","rb").read()); import tqdm; print(tqdm.__version__)
                        ~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
      File "/home/seigyo/.local/share/uv/python/cpython-3.13.7-linux-x86_64-gnu/lib/python3.13/tomllib/_parser.py", line 74, in loads
        src = s.replace("\r\n", "\n")
    TypeError: a bytes-like object is required, not 'str'
    ✗ (1) • 39ms

## Activity

    $ python -c 'import tomllib; tomllib.load(open("pyproject.toml","rb")); import tqdm; print(tqdm.__version__)' && python -m py_compile main.py publisher/progress.py publisher/pipeline.py publisher/index.py graph/wiki/pipeline.py graph/linker/service.py && git diff --check
    4.70.1
    ✓ • 142ms

## Activity

    $ rg -n 'class SyncProgress|def cmd_sync|stage": "capture"|stage": "planner"|stage": "linker-entities"|stage": "linker-edges"|step": "start"' publisher/progress.py main.py publisher/pipeline.py graph/wiki/pipeline.py graph/linker/service.py publisher/index.py | head -80
    publisher/index.py:460:        on_progress({"stage": "index", "step": "start", "run_id": run_id})
    publisher/pipeline.py:599:                    "step": "start",
    publisher/pipeline.py:810:            on_progress({"stage": "capture", "step": "start", "document": "<batch>", "total": 1})
    publisher/pipeline.py:818:                "stage": "capture", "step": "done", "document": "<batch>",
    publisher/pipeline.py:885:                        "step": "start",
    publisher/pipeline.py:934:                    on_progress({"stage": "wiki", "step": "start", "document": raw_rel, "file": raw_rel})
    graph/linker/service.py:463:                on_progress({"stage": "linker-entities", "step": "start", "document": rel})
    graph/linker/service.py:581:                    "stage": "linker-entities", "step": "done", "document": rel,
    graph/linker/service.py:615:                on_progress({"stage": "linker-edges", "step": "start", "document": rel})
    graph/linker/service.py:743:                    "stage": "linker-edges", "step": "targets", "document": rel,
    graph/linker/service.py:846:                    "stage": "linker-edges", "step": "done", "document": rel,
    publisher/progress.py:27:class SyncProgress:
    main.py:172:def cmd_sync(args: argparse.Namespace) -> int:
    ✓ • 6ms

## Assistant

Implemented.

- Added two-level `tqdm` progress rendering in [publisher/progress.py](/home/seigyo/llm-wiki/publisher/progress.py).
- Outer bar tracks documents; inner stage bar uses `leave=False`.
- Stage completion timings use `tqdm.write`, preventing bar accumulation.
- Added phase events for capture, planner, research, writer/judge, linker entities, linker graph, publish, and index.
- Added explicit `tqdm` dependency in [pyproject.toml](/home/seigyo/llm-wiki/pyproject.toml).
- JSON output remains separate on stdout; progress uses stderr.

Validation:

- Syntax checks passed.
- TOML validation passed.
- Targeted sync regression test passed.
- All 34 update-tier tests passed.
- Broader mount suite still has 5 unrelated existing failures.

## User

Hide these http info + these warnings: (llm-wiki) [seigyo@meswkcm2582(AI)]$ .venv/bin/python main.py sync --project speed_test --continue
[<batch>] capture: 0.0s                                                                                                                                                                                 
Documents:   0%|                                                                                                                                                                 | 0/2 [00:00<?, ?doc/s]2026-10-01 14:31:27,783 INFO langchain-openai detected HTTP_PROXY, HTTPS_PROXY, http_proxy, https_proxy and no explicit `http_socket_options` / `http_client` / `http_async_client` / `openai_proxy`; skipping the custom `httpx` transport so httpx's env-proxy auto-detection applies. Pass `http_socket_options=[...]` to opt back into kernel-level TCP keepalive tuning on top of the env proxy.
[kdmパッケージ取扱説明書B改訂_pdf.md] parser: 18.0s
Documents:   0%|                                                            2026-10-01 14:32:07,121 INFO HTTP Request: POST http://10.160.144.101:51029/v1/chat/completions "HTTP/1.1 200 OK"<?, ?doc/s]
2026-10-01 14:32:13,667 INFO HTTP Request: POST http://10.160.144.101:51029/v1/chat/completions "HTTP/1.1 200 OK"

## Assistant

I’ll adjust only the third-party HTTP/client logger filters in `main.py`, preserving your sync-stage timing output and application warnings/errors. I’m first identifying the exact logger namespaces behind the two messages so the filter stays narrow.

## Activity

    $ rg -n 'no explicit `http_socket_options`|HTTP Request: POST|detected HTTP_PROXY' .venv/lib/python*/site-packages 2>/dev/null | head -40; sed -n '520,545p' main.py; python -c 'import logging; print(logging.getLogger("httpx").name, logging.getLogger("langchain_openai").name, logging.getLogger("langchain-openai").name)'
        relink = link_sub.add_parser("relink", help="(re)link one document; free when nothing changed")
        relink.add_argument("document", help="wiki folder (team/docs/Input1.docx) or raw rel path")
        rebuild = link_sub.add_parser("rebuild", help="drop the catalog and relink everything (re-pays edge calls)")
        rebuild.add_argument("--mode", choices=("legacy", "neo"), required=True)
        rebuild.add_argument("--no-edges", action="store_true", help="strip footers/inline links, keep metadata")
        link.set_defaults(fn=cmd_link)
        return parser
    
    
    def main() -> int:
        args = build_parser().parse_args()
        logging.basicConfig(
            level="DEBUG" if getattr(args, "verbose", False) else os.environ.get("LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(message)s",
        )
        if getattr(args, "verbose", False):
            # -v means "show our debug", not "show httpx's every socket read".
            for noisy in ("httpx", "httpcore", "h11", "urllib3", "openai", "langchain", "anthropic"):
                logging.getLogger(noisy).setLevel(logging.INFO)
        return int(args.fn(args) or 0)
    
    
    if __name__ == "__main__":
        raise SystemExit(main())
    httpx langchain_openai langchain-openai
    ✓ • 68ms

## Activity

    • Context compacted

## Activity

    file changes: Completed · 1 changes
    Update { move_path: None }: /home/seigyo/llm-wiki/main.py
    @@ -534,6 +534,17 @@
         )
    -    if getattr(args, "verbose", False):
    -        # -v means "show our debug", not "show httpx's every socket read".
    -        for noisy in ("httpx", "httpcore", "h11", "urllib3", "openai", "langchain", "anthropic"):
    -            logging.getLogger(noisy).setLevel(logging.INFO)
    +    # -v means "show our debug", not third-party HTTP/client chatter.
    +    # Keep client warnings out of the progress display as well; real errors
    +    # from these clients remain visible.
    +    for noisy in (
    +        "httpx",
    +        "httpcore",
    +        "h11",
    +        "urllib3",
    +        "openai",
    +        "langchain",
    +        "langchain_openai",
    +        "langchain-openai",
    +        "anthropic",
    +    ):
    +        logging.getLogger(noisy).setLevel(logging.ERROR)
         return int(args.fn(args) or 0)

## Assistant

The filter is now applied in both normal and `-v` modes, so the HTTP access lines and client-level warnings won’t interrupt the document/stage progress display. Application errors and your own stage timing output remain visible. I’m running a syntax and diff check now.

## Activity

    $ python -m py_compile main.py && git diff --check && git diff -- main.py
    diff --git a/main.py b/main.py
    index e11114b..72fa3b6 100644
    --- a/main.py
    +++ b/main.py
    @@ -37,6 +37,8 @@ from graph.workspace.project import open_project
     PROJECT_ROOT = Path(__file__).resolve().parent
     config.PROJECT_ROOT = PROJECT_ROOT
    
    +log = logging.getLogger(__name__)
    +
    
     def _settings(args: argparse.Namespace) -> Settings:
         settings = Settings.from_env(getattr(args, "project", ""))
    @@ -75,7 +77,7 @@ def _progress(event: dict[str, Any]) -> None:
             event.pop("current", None)
             event.pop("total", None)
         details = json.dumps(event, ensure_ascii=False, default=str)
    -    print(f"[{stage}] {step}{progress} {details}".rstrip(), flush=True)
    +    log.debug(f"[{stage}] {step}{progress} {details}".rstrip())
    
    
     def _report(result: dict[str, Any]) -> int:
    @@ -170,56 +172,123 @@ def cmd_check(args: argparse.Namespace) -> int:
     def cmd_sync(args: argparse.Namespace) -> int:
         from publisher import pipeline
         from publisher.queue import retry_failed, scan, work_once, worker_lock
    +    from publisher.progress import SyncProgress
    
         settings = _settings(args)
         project = open_project(settings)
         combined: dict[str, Any] = {"done": [], "failures": []}
    -    stale = pipeline.republish_if_stale(settings)
    -    if stale is not None:
    -        print(f"growi endpoint or markers changed: full republish {stale['run_id']}", flush=True)
    -        combined["failures"].extend(stale["failures"])
    -    with worker_lock(project):
    -        retry_failed(project)
    -        first = True
    -        resume = bool(getattr(args, "continue_run", False))
    -        while True:
    -            scan(
    -                settings,
    -                only=args.items or None,
    -                settle_seconds=0,
    -                force=args.force and first,
    -                verify_content=True,
    -            )
    -            first = False
    -            result = work_once(settings, on_event=_progress if args.verbose else None, continue_run=resume)
    -            # Only the first batch can resume a kept worktree; later batches
    -            # in the same process are always fresh.
    -            resume = False
    -            if result is None:
    -                break
    -            combined["done"].extend(result.get("done", []))
    -            combined["failures"].extend(result.get("failures", []))
    -            if result.get("failures"):
    -                break
    -    if not combined["failures"]:
    -        # Index pages are derived output, so reconcile them against the whole wiki tree
    -        # here: a wiki built before its index exists catches up, and a page whose body
    -        # already matches GROWI is read but never rewritten.
    -        from publisher.index import build_index
    +    progress = SyncProgress()
    
    -        try:
    -            index = build_index(settings, on_progress=_progress if args.verbose else None)
    -        except Exception as exc:  # a stale table of contents must not fail a sync
    -            index = {"done": [], "failures": [f"index: {type(exc).__name__}: {exc}"]}
    -        combined["done"].extend(index["done"])
    -        combined["failures"].extend(index["failures"])
    -    return _report(combined)
    +    def on_event(event: dict[str, Any]) -> None:
    +        progress.on_event(event)
    +        if args.verbose:
    +            _progress(event)
    +
    +    try:
    +        stale = pipeline.republish_if_stale(settings)
    +        if stale is not None:
    +            print(f"growi endpoint or markers changed: full republish {stale['run_id']}", flush=True)
    +            combined["failures"].extend(stale["failures"])
    +        with worker_lock(project):
    +            retry_failed(project)
    +            first = True
    +            resume = bool(getattr(args, "continue_run", False))
    +            while True:
    +                scan_result = scan(
    +                    settings,
    +                    only=args.items or None,
    +                    settle_seconds=0,
    +                    force=args.force and first,
    +                    verify_content=True,
    +                )
    +                progress.add_scan_result(scan_result)
    +                first = False
    +                result = work_once(settings, on_event=on_event, continue_run=resume)
    +                # Only the first batch can resume a kept worktree; later batches
    +                # in the same process are always fresh.
    +                resume = False
    +                if result is None:
    +                    break
    +                paths = result.get("paths", [])
    +                progress.add_documents(paths)
    +                combined["done"].extend(result.get("done", []))
    +                combined["failures"].extend(result.get("failures", []))
    +                if result.get("failures"):
    +                    break
    +                progress.mark_completed(paths)
    +        if not combined["failures"]:
    +            # Index pages are derived output, so reconcile them against the whole wiki tree
    +            # here: a wiki built before its index exists catches up, and a page whose body
    +            # already matches GROWI is read but never rewritten.
    +            from publisher.index import build_index
    +
    +            try:
    +                index_callback = on_event if args.verbose or progress.has_documents else None
    +                index = build_index(settings, on_progress=index_callback)
    +            except Exception as exc:  # a stale table of contents must not fail a sync
    +                index = {"done": [], "failures": [f"index: {type(exc).__name__}: {exc}"]}
    +            combined["done"].extend(index["done"])
    +            combined["failures"].extend(index["failures"])
    +        return _report(combined)
    +    finally:
    +        progress.close()
    
    
     def cmd_pull(args: argparse.Namespace) -> int:
         from publisher.pipeline import pull_growi_once
    
    -    return _report(pull_growi_once(_settings(args)))
    +    settings = _settings(args)
    +    result = (
    +        pull_growi_once(settings, force_inventory=True)
    +        if getattr(args, "inventory", False)
    +        else pull_growi_once(settings)
    +    )
    +    if result.get("human_sync"):
    +        print(json.dumps({"human_sync": result["human_sync"]}, ensure_ascii=False, default=str))
    +    return _report(result)
    +
    +
    +def cmd_human(args: argparse.Namespace) -> int:
    +    from publisher.human_changes import HumanStore
    +
    +    settings = _settings(args)
    +    project = open_project(settings)
    +    store = HumanStore(project)
    +    if args.human_command == "status":
    +        print(json.dumps(store.project_summary(), ensure_ascii=False, default=str))
    +        return 0
    +    if args.human_command == "resolve":
    +        combined = ""
    +        if args.text_file:
    +            combined = Path(args.text_file).read_text(encoding="utf-8")
    +        result = store.resolve(
    +            args.edit_id,
    +            action=args.action,
    +            expected_revision=args.revision,
    +            combined_text=combined,
    +            document=args.document or "",
    +        )
    +        print(json.dumps(result, ensure_ascii=False))
    +        return 0
    +    if args.human_command == "recover-legacy":
    +        from publisher.legacy_recovery import recover_legacy_ancestor
    +
    +        result = recover_legacy_ancestor(store, args.document)
    +        print(json.dumps(result, ensure_ascii=False, default=str))
    +        return 0 if result.get("status") in {"recovered", "no_legacy_pin"} else 1
    +    if args.human_command == "live-plan":
    +        from publisher.live_verification import LiveVerificationReport
    +
    +        report = LiveVerificationReport.create(project, settings, args.path)
    +        print(json.dumps({
    +            "report": str(report.path),
    +            "resolved_boundary": report.data["disposable_path"],
    +            "endpoint": report.data["endpoint"],
    +            "confirmation_code": report.data["confirmation_code"],
    +            "status": report.data["status"],
    +        }, ensure_ascii=False))
    +        return 0
    +    raise ValueError(f"unknown human command: {args.human_command}")
    
    
     def cmd_watch(args: argparse.Namespace) -> int:
    @@ -414,7 +483,27 @@ def build_parser() -> argparse.ArgumentParser:
         build.add_argument("--force", action="store_true", help="regenerate even when the raw source is unchanged")
         build.set_defaults(fn=cmd_build)
         publish = sub.add_parser("publish", help="publish the current wiki tree only"); project_flags(publish); publish.set_defaults(fn=cmd_publish)
    -    pull = sub.add_parser("pull", help="capture GROWI edits into the local human overlay"); project_flags(pull); pull.set_defaults(fn=cmd_pull)
    +    pull = sub.add_parser("pull", help="capture GROWI edits into the local human overlay"); project_flags(pull)
    +    pull.add_argument("--inventory", action="store_true", help="force a complete read-only inventory below the configured GROWI root")
    +    pull.set_defaults(fn=cmd_pull)
    +    human = sub.add_parser("human", help="inspect and resolve durable human overlays"); project_flags(human)
    +    human_sub = human.add_subparsers(dest="human_command", required=True)
    +    human_status = human_sub.add_parser("status", help="write and print the project-wide human-sync summary")
    +    project_flags(human_status)
    +    human_resolve = human_sub.add_parser("resolve", help="apply one revision-checked operator decision")
    +    project_flags(human_resolve)
    +    human_resolve.add_argument("edit_id")
    +    human_resolve.add_argument("--action", required=True, choices=("keep-human", "accept-source", "combine", "suppress", "delete", "retry-match"))
    +    human_resolve.add_argument("--revision", required=True, help="last inspected GROWI revision")
    +    human_resolve.add_argument("--document", help="expected raw document identity")
    +    human_resolve.add_argument("--text-file", help="UTF-8 combined body for --action combine")
    +    human_recover = human_sub.add_parser("recover-legacy", help="recover a uniquely verified pure ancestor from project Git")
    +    project_flags(human_recover)
    +    human_recover.add_argument("document", help="raw-relative document path")
    +    human_live = human_sub.add_parser("live-plan", help="create a local-only redacted plan for a disposable live verification subtree")
    +    project_flags(human_live)
    +    human_live.add_argument("--path", required=True, help="confirmed disposable path below the configured project boundary")
    +    human.set_defaults(fn=cmd_human)
         index = sub.add_parser("index", help="publish per-document + root index pages for growi-search"); project_flags(index)
         index.add_argument("items", nargs="*", metavar="raw-rel", help="raw-relative paths; omit for every linked document")
         index.add_argument("--no-publish", action="store_true", help="only write metadata/index/, do not touch GROWI")
    @@ -438,8 +527,26 @@ def build_parser() -> argparse.ArgumentParser:
    
    
     def main() -> int:
    -    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
         args = build_parser().parse_args()
    +    logging.basicConfig(
    +        level="DEBUG" if getattr(args, "verbose", False) else os.environ.get("LOG_LEVEL", "INFO"),
    +        format="%(asctime)s %(levelname)s %(message)s",
    +    )
    +    # -v means "show our debug", not third-party HTTP/client chatter.
    +    # Keep client warnings out of the progress display as well; real errors
    +    # from these clients remain visible.
    +    for noisy in (
    +        "httpx",
    +        "httpcore",
    +        "h11",
    +        "urllib3",
    +        "openai",
    +        "langchain",
    +        "langchain_openai",
    +        "langchain-openai",
    +        "anthropic",
    +    ):
    +        logging.getLogger(noisy).setLevel(logging.ERROR)
         return int(args.fn(args) or 0)
    
    
    ✓ • 40ms

## Assistant

Done. Updated [main.py](/home/seigyo/llm-wiki/main.py:532) to suppress third-party HTTP/client INFO and WARNING logs, including:

- `httpx` / `httpcore` request logs
- `langchain_openai` proxy warning
- OpenAI, Anthropic, urllib3, and related client chatter

Actual client errors remain visible, as do your application logs, tqdm bars, and stage timings.

`py_compile` and `git diff --check` pass.

## User

[D版送付用資料/#33136_予備率一定配分全般（需計画面）_r3_pdf.md] parser: 3.7s                                                                                                                                                                
Documents:   0%|                                                                                                                                                                                                    | 0/19 [08:26<?, ?doc/s]2026-10-01 17:06:47,297 DEBUG [parse] done {"document": "D版送付用資料/#33136_予備率一定配分全般（需計画面）_r3_pdf.md", "file": "D版送付用資料/#33136_予備率一定配分全般（需計画面）_r3.pdf", "characters": 15528596, "elapsed_seconds": 3.7}
2026-10-01 17:06:47,334 DEBUG [wiki] start {"document": "D版送付用資料/#33136_予備率一定配分全般（需計画面）_r3_pdf.md", "file": "D版送付用資料/#33136_予備率一定配分全般（需計画面）_r3_pdf.md"}
2026-10-01 17:06:51,104 DEBUG [wiki] update_decision {"file": "D版送付用資料/#33136_予備率一定配分全般（需計画面）_r3_pdf.md", "tier": 0, "reason": "unchanged", "hunks": 0, "patch_pages": 0, "regenerate_pages": 0, "retitle_pages": 0, "changed_pages": [], "document": "D版送付用資料/#33136_予備率一定配分全般（需計画面）_r3_pdf.md"}
2026-10-01 17:06:53,950 DEBUG human_sync event=render source=4202c2ea-57f2-4d57-81d9-107977c9e44a changed=0 conflict=0 orphaned=0
2026-10-01 17:06:53,959 DEBUG [wiki] done {"document": "D版送付用資料/#33136_予備率一定配分全般（需計画面）_r3_pdf.md", "file": "D版送付用資料/#33136_予備率一定配分全般（需計画面）_r3_pdf.md", "touched_documents": 0, "elapsed_seconds": 6.6}
2026-10-01 17:06:53,978 DEBUG run=prun-ae54b658a08e4ccc path=D版送付用資料/#33136_予備率一定配分全般（需計画面）_r3.pdf stage=generate elapsed=10.42s
2026-10-01 17:06:53,979 DEBUG run=prun-ae54b658a08e4ccc path=D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2.pdf stage=parse start
                                                                                                               2026-10-01 17:06:53,979 DEBUG [parse] start {"document": "D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2_pdf.md", "file": "D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2.pdf", "parser": "pdf", "bytes": 1205337}
2026-10-01 17:06:53,987 DEBUG run=prun-ae54b658a08e4ccc path=D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2.pdf stage=parse resumed
                                                                                                                 2026-10-01 17:06:53,987 DEBUG [parse] resumed {"document": "D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2_pdf.md", "file": "D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2.pdf"}00:00, ?item/s, resumed]
[D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2_pdf.md] parser: 1.9s                                                                                                                                                      
Documents:   0%|                                                                                                                                                                                                    | 0/19 [08:34<?, ?doc/s]2026-10-01 17:06:55,913 DEBUG [parse] done {"document": "D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2_pdf.md", "file": "D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2.pdf", "characters": 7828838, "elapsed_seconds": 1.9}
2026-10-01 17:06:55,942 DEBUG [wiki] start {"document": "D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2_pdf.md", "file": "D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2_pdf.md"}
2026-10-01 17:06:57,847 DEBUG [wiki] update_decision {"file": "D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2_pdf.md", "tier": 0, "reason": "unchanged", "hunks": 0, "patch_pages": 0, "regenerate_pages": 0, "retitle_pages": 0, "changed_pages": [], "document": "D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2_pdf.md"}
2026-10-01 17:06:59,231 DEBUG human_sync event=render source=7e6244b5-aa6b-426f-9ee6-0f1631260abc changed=0 conflict=0 orphaned=0
2026-10-01 17:06:59,238 DEBUG [wiki] done {"document": "D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2_pdf.md", "file": "D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2_pdf.md", "touched_documents": 0, "elapsed_seconds": 3.3}
2026-10-01 17:06:59,256 DEBUG run=prun-ae54b658a08e4ccc path=D版送付用資料/#33138,33195_実績最終時刻_GC時刻_GC対象コマ資料_r2.pdf stage=generate elapsed=5.28s
2026-10-01 17:06:59,257 DEBUG run=prun-ae54b658a08e4ccc path=D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画.pdf stage=parse start
                                                                                                         2026-10-01 17:06:59,257 DEBUG [parse] start {"document": "D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画_pdf.md", "file": "D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画.pdf", "parser": "pdf", "bytes": 1196841}rt]
2026-10-01 17:06:59,258 DEBUG run=prun-ae54b658a08e4ccc path=D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画.pdf stage=parse resumed
                                                                                                           2026-10-01 17:06:59,258 DEBUG [parse] resumed {"document": "D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画_pdf.md", "file": "D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画.pdf"}ser: 0item [00:00, ?item/s, resumed]
[D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画_pdf.md] parser: 0.0s                                                                                                                                                            
Documents:   0%|                                                                                                                                                                                                    | 0/19 [08:38<?, ?doc/s]2026-10-01 17:06:59,263 DEBUG [parse] done {"document": "D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画_pdf.md", "file": "D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画.pdf", "characters": 44891, "elapsed_seconds": 0.0}
2026-10-01 17:06:59,268 DEBUG [wiki] start {"document": "D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画_pdf.md", "file": "D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画_pdf.md"}
2026-10-01 17:06:59,274 DEBUG [wiki] update_decision {"file": "D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画_pdf.md", "tier": 0, "reason": "unchanged", "hunks": 0, "patch_pages": 0, "regenerate_pages": 0, "retitle_pages": 0, "changed_pages": [], "document": "D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画_pdf.md"}
2026-10-01 17:06:59,394 DEBUG human_sync event=render source=f69e0573-3037-4ad1-a5dc-8e3a5b48b028 changed=0 conflict=0 orphaned=0
2026-10-01 17:06:59,401 DEBUG [wiki] done {"document": "D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画_pdf.md", "file": "D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画_pdf.md", "touched_documents": 0, "elapsed_seconds": 0.1}
2026-10-01 17:06:59,419 DEBUG run=prun-ae54b658a08e4ccc path=D版送付用資料/M要件-266D_【27年度制度対応_工程2】_需給計画.pdf stage=generate elapsed=0.16s
2026-10-01 17:06:59,420 DEBUG run=prun-ae54b658a08e4ccc path=取引ガイド_ver.9_260401.pdf stage=parse start
                                                                      2026-10-01 17:06:59,420 DEBUG [parse] start {"document": "取引ガイド_ver.9_260401_pdf.md", "file": "取引ガイド_ver.9_260401.pdf", "parser": "pdf", "bytes": 16142497}
取引ガイド_ver.9_260401_pdf.md | parser: 0item [00:00, ?item/s, start]  2026-10-01 17:07:09,421 DEBUG [parse] waiting {"file": "取引ガイド_ver.9_260401.pdf", "elapsed_seconds": 10, "document": "取引ガイド_ver.9_260401_pdf.md"}
取引ガイド_ver.9_260401_pdf.md | parser: 0item [00:10, ?item/s, waiting]2026-10-01 17:07:19,421 DEBUG [parse] waiting {"file": "取引ガイド_ver.9_260401.pdf", "elapsed_seconds": 20, "document": "取引ガイド_ver.9_260401_pdf.md"}
取引ガイド_ver.9_260401_pdf.md | parser: 0item [00:20, ?item/s, waiting]2026-10-01 17:07:29,421 DEBUG [parse] waiting {"file": "取引ガイド_ver.9_260401.pdf", "elapsed_seconds": 30, "document": "取引ガイド_ver.9_260401_pdf.md"}
取引ガイド_ver.9_260401_pdf.md | parser: 0item [00:30, ?item/s, waiting]2026-10-01 17:07:39,422 DEBUG [parse] waiting {"file": "取引ガイド_ver.9_260401.pdf", "elapsed_seconds": 40, "document": "取引ガイド_ver.9_260401_pdf.md"}
取引ガイド_ver.9_260401_pdf.md | parser: 0item [00:40, ?item/s, waiting]2026-10-01 17:07:49,422 DEBUG [parse] waiting {"file": "取引ガイド_ver.9_260401.pdf", "elapsed_seconds": 50, "document": "取引ガイド_ver.9_260401_pdf.md"}
取引ガイド_ver.9_260401_pdf.md | parser: 0item [00:50, ?item/s, waiting]2026-10-01 17:07:59,423 DEBUG [parse] waiting {"file": "取引ガイド_ver.9_260401.pdf", "elapsed_seconds": 60, "document": "取引ガイド_ver.9_260401_pdf.md"}
取引ガイド_ver.9_260401_pdf.md | parser: 0item [01:00, ?item/s, waiting]2026-10-01 17:08:09,423 DEBUG [parse] waiting {"file": "取引ガイド_ver.9_260401.pdf", "elapsed_seconds": 70, "document": "取引ガイド_ver.9_260401_pdf.md"}
取引ガイド_ver.9_260401_pdf.md | parser: 0item [01:10, ?item/s, waiting]2026-10-01 17:08:19,423 DEBUG [parse] waiting {"file": "取引ガイド_ver.9_260401.pdf", "elapsed_seconds": 80, "document": "取引ガイド_ver.9_260401_pdf.md"}
取引ガイド_ver.9_260401_pdf.md | parser: 0item [01:20, ?item/s, waiting]2026-10-01 17:08:29,424 DEBUG [parse] waiting {"file": "取引ガイド_ver.9_260401.pdf", "elapsed_seconds": 90, "document": "取引ガイド_ver.9_260401_pdf.md"} what are these ugly ass debug logs, i cant read shit, hide file name from middle only 10 chars from beginning 10 from end and 5 ... between if chars > 25 for doc names
