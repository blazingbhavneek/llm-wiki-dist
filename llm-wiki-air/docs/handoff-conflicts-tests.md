# Handoff: test page-merge human edits end to end

Companion to `handoff-conflicts.md` (the design, the merge rules in section 2,
and the 62 cases in section 13). This file says what has been tested, how, what
is left, and exactly how to test the rest. It is written for someone who has not
seen the code: follow the steps in order and copy the commands.

Last updated: 2026-10-12, on the home PC against a local GROWI
(`http://localhost:3000`) and the `.env` chat endpoint (`gemma-4-31B`).

Work-PC rules still apply there: the approved project is
`/home/seigyo/llm-wiki/configs/conflicts_test.ini`, all live evidence comes from
`http://10.160.152.38:3000/` below `/conflicts_test`, every page touched is a
synthetic page, and private page bodies are never opened or printed.

## 1. Words used in this file

- **Source / mount**: the original document files in the configured
  `source_mount` folder. Changing a file there is a "source change".
- **Human edit**: someone saves a page in GROWI. A *real* human edit is made in the
  GROWI browser editor by a separate tester account. An *API edit* is made by
  `tools/hc_live/edit.py`; it saves the same way the editor does
  (`origin: editor`) but it is a pre-check only and never counts as real-user
  evidence (`handoff-conflicts.md` 12.5).
- **Human state**: what the pipeline remembers per document, below
  `<project>/metadata/human-sync/doc/<key>/`: `pure/` (what the generator last
  wrote), `current/` (the same pages with every human change), `doc.json`
  (Appendix name, writer guidance) and `captures/` (one record per accepted GROWI
  revision).
- **Note**: `（元文書の更新: …）` (or a quoted `> **元文書の更新:**` block). It
  appears when the human and the source changed the same fact differently: the
  human's value stays, the source's value is shown in the note.
- **Appendix**: the extra last page `NNN-付録.md`. When the source deletes text a
  human changed, the human's text moves there.
- **Marker word**: a unique word such as `HCT-05` put in every test edit, so a check
  is just "how many times does `HCT-05` appear". "Once" means 1 in GROWI, 1 in
  `current/`, 1 in the live wiki page.
- **Round**: one `sync` run plus its checks (`tools/hc_live/round.sh`).

## 2. Status

### 2.1 Offline tests

All pass on the final code (run from the repo root):

```bash
.venv/bin/python -m compileall -q common convert wiki linker index publisher runner graph
for m in test_human_changes test_human_sync_rollout test_sync_fallback test_update_tiers test_fast_repair; do
  timeout 300 .venv/bin/python -m unittest -q tests.$m
done
# 2026-10-12: 77, 24, 33, 34 and 33 tests, all OK.
```

`test_sync_fallback` used to hang on the work PC in
`test_isolated_sync_finishes_builder_pass_before_linker_phase`. A real race in
the same code path was found and fixed (two threads importing the linker modules
at once raised `_DeadlockError`, `publisher/ahead.py`). Rerun it on the work PC to
confirm. `tests/test_mount_diff_pipeline.py` has 5 failures that are older than
this work (one is a missing fixture, `configs/diff_test_local.ini`).

### 2.2 Live pre-check on the home PC (2026-10-12)

How it was run: project `configs/hc-e2e-fast.ini` (target `/hc-e2e-fast` on the
local GROWI), command `sync --fast` (linking off, pages published unlinked, the
production fast path), synthetic sources made with `tools/hc_live/canary.py`
plus three small documents. Human edits were API edits made by Haiku subagents
and by `tools/hc_live/edit.py`; GROWI-side actions (move, trash, marker damage,
foreign page) were made with the same tool; faults were injected with
`tools/hc_live/proxy.py`. **None of this counts as work-PC or real-user
evidence.**

Result: **46 of 62 passed** (74%), 6 partly done, 9 not run, 1 differs by design.
Work-PC real-user evidence: 0 of 62.

| # | Case | Result | How it was tested / notes |
|---|---|---|---|
| 1 | initial_multi_page_publish | pass | first `sync --fast` of canary v0: 4 pages, one marker each, ledger IDs, no human state, HEAD == last-good |
| 2 | normal_update | pass | canary v1: untouched value 40 A → 45 A shown |
| 3 | noop_repeat | pass | every repeat sync: 0 model calls, nothing changed |
| 4 | process_restart | pass (build only) | `kill -9` during a build, twice; rerun recovered. Kill during publish not done |
| 5 | human_add | pass | HCT-05: `off` blocks, `observe` blocks + records an observation, `apply` captures once |
| 6 | human_replace | pass | HCT-06: 62 → 65, stable |
| 7 | human_delete | pass | a line deleted; stayed deleted across later source updates |
| 8 | same_edit | pass | human 4 → 6 か月, then source 4 → 6: once, no note |
| 9 | disjoint_edit | pass | human line + source 17 → 18 °C in the same section: both shown |
| 10 | contradiction | pass | human 65, source 63: `65 °C（HCT-06）（元文書の更新: 63 °C）` (real model merge) |
| 11 | keep_human | pass | note deleted in GROWI; did not come back |
| 12 | accept_source | pass | human replaced their value with the source value and deleted the note |
| 13 | combine | pass | HCT-13 combination kept once |
| 14 | suppress | pass | a human-added paragraph deleted; did not return |
| 15 | tier_0 | pass | whitespace-only source change on `fifth.md` (tier 0); HCT-15 kept |
| 16 | tier_1 | pass | canary v1 (tier-1 patch) |
| 17 | tier_2 | pass | canary v2, v3 (pages regenerated) |
| 18 | tier_3 | pass | `sync --fast --force hc/canary.md`: every marker once |
| 19 | split_combine_rename_reorder | partly | section deleted and restored; repeated-line fix verified live (HCT-W3 went to the Appendix, not the wrong section). Source heading rename / reorder / split not run |
| 20 | remote_move | pass | page moved in GROWI: blocked, not recreated; moved back: unblocked (GROWI keeps the revision on a rename) |
| 21 | remote_delete | pass | page trashed: blocked, not recreated |
| 22 | marker_damage | pass | marker damaged: blocked; restored: unblocked |
| 23 | foreign_destination | pass | a hand-made page at the new document's path: publication refused in preflight, the page untouched |
| 24 | duplicate_marker | pass | a marker copied into another page: `pull --inventory` reports `duplicate_ownership`, adoption refused |
| 25 | source_rename | pass | rename (state follows), then a different file at the old name inherits nothing (needs a sync between the two) |
| 26 | bulk_delete | pass | the document with human text is blocked and kept; the one with only a human deletion is deleted and trashed |
| 27 | markdown_integrity | differs | fences, tables and human HTML comments reach GROWI; inline-code backticks are stripped in GROWI for all text (kept locally). Owner decision |
| 28 | attachments_images | pass | uploaded image + human line captured once; no spurious changes |
| 29 | conditional_409 | partly | an injected 500 gave exactly one PUT, no retry; a real 409 was not injected (offline test covers it) |
| 30 | edit_after_preflight | not run | section 7.2 |
| 31 | edit_during_generation | not run live | watch-mode fix + offline test; section 7.3 |
| 32 | lost_update | pass | proxy `drop` on the update: rerun settled it as `recovered_exact`, no human change |
| 33 | lost_create | pass | proxy `drop` on the create: adopted, no duplicate |
| 34 | partial_publish_late_edit | not run | neighbour case 62 passed; offline test; section 7.4 |
| 35 | watcher_restart | not run | section 7.5 |
| 36 | activity_cursor_restart | not run | section 7.6 |
| 37 | full_inventory_equivalence | not run | `pull --inventory` itself ran (case 24); section 7.7 |
| 38 | candidate_rollback | not run | only `watch`, `--no-isolated`, move and delete jobs publish inside a candidate; section 7.8 |
| 39 | last_good_restore | not run | section 7.9 |
| 40 | service_restart_reconcile | not run | section 7.10 |
| 41 | model_outage_fallback | pass | proxy answered 503 to every merge/verify/classify call: capture worked, no guidance, fallback note, 32 s |
| 42 | concurrent_add_same_anchor | pass | human line and a different source line at the same place: both published |
| 43 | human_delete_source_modify | pass | deletion kept, new value 950 kg shown as a note |
| 44 | human_modify_source_delete | pass | section deleted: human line in `005-付録.md`. The Appendix rename on a new page was not triggered |
| 45 | concurrent_delete_same_fact | pass | the same table row deleted on both sides: gone everywhere |
| 46 | multiple_remote_revisions_before_pull | pass | three saves (one undoing another) before one sync |
| 47 | mixed_page_capture_failure | pass | valid edit on page A + damaged marker on page B in one pull: A captured; B blocked until fixed |
| 48 | source_move_delete_with_remote_edit | pass | unpulled edit + rename: captured, then moved; + delete: captured, delete blocked |
| 49 | transport_only_remote_revision | pass | open and save without change: no human change |
| 50 | mode_transition_same_revision | pass | off → observe → apply → off → apply, each a new process |
| 51 | growi_conflict_ui_resolution | pass | keep human, accept source, combine, edit beside the note |
| 52 | remote_revision_rollback | pass | an older revision restored from GROWI history: a human change |
| 53 | idle_sync_remote_reconciliation | pass | UI edit, mount unchanged, plain sync: captured first, no build |
| 54 | human_section_placement | pass | a human section added mid-page stays in place (human reorder: offline test only) |
| 55 | conflict_survives_unrelated_edit | pass | another line edited beside a note: the note stays |
| 56 | fast_policy_human_overlay | partly | all runs used `sync --fast`; the `--fast --repair` promote step re-applied a human edit (HCT-R1) and repair skips blocked documents; `--fast --link` ran but linked nothing |
| 57 | human_revert | pass | human undid their edit; later source change applied |
| 58 | source_catches_up | partly | a false note appeared live (bug, fixed); fix verified by replaying the real inputs |
| 59 | source_removes_then_restores | pass | restored source text shown normally; Appendix entry stays |
| 60 | regeneration_without_fact_change | partly | every human change kept after a forced rebuild, but 2 fallback notes appeared (one cause fixed, one is known limitation 5) |
| 61 | structure_only_edit | partly | guidance recorded live; the guidance is lost when a rebuild renames the page (known limitation 4) |
| 62 | pull_onto_unpublished_generation | pass | publish failed with a 500, human edited, `pull`: newer local text kept + human change; next sync published both |

### 2.3 Bugs found by the live run (all fixed, each with a regression test)

1. A fresh project's first pull wrote the activity cursor before the human store
   existed; every later sync refused with "dirty last-good working tree".
2. Two threads importing the linker modules at once raised `_DeadlockError`.
3. After switching `off` → `observe`, the same blocked revision was not observed.
4. A blocked page was never checked again without a new revision (a page moved
   back stayed blocked; GROWI does not create a revision on a rename).
5. Deleting a paragraph containing inline code or a comment blocked the page
   ("cannot be canonicalized safely").
6. The merge call froze source changes behind notes (text the human never changed
   was treated as human text). Fixed in the merge and verify instructions, plus two
   exact rules: a window whose human change is only whitespace takes the new text;
   text the human only added next to an anchor is placed beside the new text.
7. Merges ran one window at a time; they now run up to `concurrency` at once.
8. Every document move failed on real GROWI: renaming the document page moves its
   children, so the per-page rename returned 409 `already_exists`.
9. An HTTP 500 on a publish, then a human edit on that page, blocked it forever;
   the pull now checks GROWI's revision history for the prepared body.
10. A lost create response left a ledger row with no revision, failing every
    preflight.
11. One blocked document stopped every document's publication.
12. In `watch` / `--no-isolated`, an edit saved during a long build failed the
    whole operation (the publish skipped its pull).
13. `--fast --repair` stopped for every document when any page failed its pull;
    now only that page's document is held back.
14. Earlier offline audit: wrong-section attachment in window case 3, stale
    `doc.json` path after a move, `known_pages` crash, doubled Appendix heading,
    human HTML comments stripped on publish.

## 3. What remains

In priority order:

1. **Work-PC real-user run** (section 8). Required before production. A separate
   tester edits in the GROWI browser; the real model; production's command.
2. **Owner decisions**:
   - Case 27: inline-code backticks are stripped in GROWI for all text. Keep them
     all (one line in `graph/growi/client.py:_growi_markdown`, every page
     republishes once), keep only human-written ones (about 10–15 lines), or leave.
   - Output caps are "TEMPORARILY DISABLED" in `graph/clients/chat.py`. On
     2026-10-12 the planner looped for 18 minutes on a 6-line document.
   - Projects with old `human_edited` sidecars stop with `legacy human state`.
3. **Cases not yet run** (section 7): 30, 31, 34–40, and the partly done ones 19,
   29, 44 (Appendix rename), 56, 58, 60, 61. How likely they are in daily use:
   - likely: 19 (documents get restructured), 60/61 (rebuilds, formatting edits),
     31 (edits during 5–30 minute builds), 35 (if production runs `watch`), 56 (if
     you use `--fast --link` or `--fast --repair`);
   - rare: 29, 30, 36, 58;
   - very rare (a GROWI error or crash at an exact moment; all fail closed): 34,
     37–40.
4. **Known limitations seen live** (not bugs): fallback notes can stack (design
   limitation 5); guidance is keyed by page file name and lost if a rebuild renames
   the page (limitation 4); table-of-contents pages `00-目次` keep stale page
   summaries after a small source patch (they come from the original plan); a
   page in the GROWI trash can only be restored from the GROWI UI (the API route
   rejects tokens).

## 4. Tools (`tools/hc_live/`)

All take the project from `HC_PROJECT` (a config name from `configs/`, default
`hc-e2e-fast`) and read the GROWI URL and token from that config / `.env`.

| Tool | What it does |
|---|---|
| `edit.py` | API edits on the project's pages, saved like the browser editor: `pages`, `show`, `replace`, `insert-after`, `delete`, `save-unchanged`, `history`, `restore`, `move`, `trash`, `create`. Run it with no arguments for help. Uses `HUMAN_TOKEN` if set (a separate account), else the project token |
| `count.py` | counts marker words in GROWI (`growi`), in `current/` and in the live wiki (`live`) |
| `round.sh` | sets `human_sync_mode` in the INI, runs `sync --fast` (or `$HC_SYNC`), prints exit code, time, model calls, result lines, `HEAD==last-good`, durable-dirty count and `human status`. Logs in `$HC_LOGS` (default `/tmp/hc-e2e-logs`) |
| `proxy.py` | fault proxy: delay, answer with a status, or drop the response of one chosen request |
| `canary.py` | optional synthetic HX-300 document, versions 0–3 (section 6); real documents work the same way |

`human.py`, `make_doc.py` and `source.py` are the older home-PC tools used by
`docs/plan_regression_conflict.md`; leave them.

Typical session (copy as is; change only `HC_PROJECT`):

```bash
cd /mnt/common/Code/llm-wiki-dist/llm-wiki-air
export HC_PROJECT=<config name>        # e.g. hc-e2e-fast
E=".venv/bin/python tools/hc_live/edit.py"; C=".venv/bin/python tools/hc_live/count.py"; R=tools/hc_live/round.sh
$E pages                                   # local paths and GROWI paths
$E show '<local path from pages>'     # what the page holds now
$R apply r1                                # one sync round in apply mode
$C HCT-05 HCT-06                           # marker counts
```

## 5. Set up a test project (step by step)

The procedure works with any documents: the synthetic canary used on 2026-10-12,
or real documents the owner chooses on the work PC. Do this once per test target.
Use a new `target_name` so no other GROWI tree is touched.

1. **Check the services.** `.venv/bin/python main.py --project <any config> check`
   must show `chat 200` and `growi 200`. Non-Markdown sources (`.docx`, `.pdf`,
   `.pptx`, `.xlsx`) also need `parser 200`.
2. **Choose the documents and put them in a test mount.** One folder that holds
   only the test documents, for example `data/mount-e2e/` (home PC) or a new
   directory approved by the owner (work PC):
   - real documents: copy the chosen files in (`cp <files> <mount>/<subdir>/`);
     pick at least one document long enough to become several pages, and two that
     mention the same names if you want to test `--fast --link`;
   - synthetic: `.venv/bin/python tools/hc_live/canary.py 0 <mount>/hc/canary.md`.
3. **Write the INI** `configs/<name>.ini` (no token in it; it comes from `.env` as
   `GROWI_TOKEN`, or from the approved secret location on the work PC):

   ```ini
   [project]
   source_mount = /absolute/path/to/<mount>
   target_name = <name>
   data_root = data
   growi_url = http://localhost:3000

   [settings]
   human_sync_mode = off
   wiki_linker_enabled = false
   ```

   Add `structure_target_lines = 80` only if your documents are short and come out
   as a single page (the default 250 lines per page made the 220-line canary one
   page). `wiki_linker_enabled = false` matches `sync --fast`; drop it if
   production links.
4. **First publish:** `export HC_PROJECT=<name>` then `tools/hc_live/round.sh off r0`
   (the canary took 8 minutes; real documents take longer, roughly with their size).
   Pass (case 1): `rc=0`, `HEAD==last-good: yes  durable-dirty: 0`,
   `.venv/bin/python tools/hc_live/edit.py pages` lists one line per page, and
   `data/<name>/metadata/human-sync/doc` does not exist yet.
5. **Make a test card per document** before any edit. The wiki is written by the
   model, so its wording differs from the source. For each document write down,
   from `edit.py show <local path>` (or the files in `data/<name>/wiki/`):
   - **F1**: a line with a number or value that also appears in the source (for
     replace, conflict, same-edit, catch-up);
   - **F2**: another fact in the same section as F1 (for disjoint edits);
   - **S**: a whole section (heading and body) on a page with at least one other
     section (for "source deletes a changed section" and restore);
   - **D**: one sentence you can delete (for human delete);
   - **T**: a table cell, if the document has a table;
   - **R**: a line that appears in two sections, if there is one (for the
     wrong-section check).

   For each, also find the same fact in the source document: in the Markdown file,
   or in Word/Excel/PowerPoint for office files. A "source change" means opening
   the source in its own editor, changing that fact, and saving it back into the
   mount (same file name). Keep the file name unchanged unless the case is a
   rename.
6. **Marker words.** Every human edit adds a unique word such as `HCT-06`, so a
   check is `.venv/bin/python tools/hc_live/count.py HCT-06 --page <local path>`:
   `growi 1, current 1, live 1` is "once". Use `--page` when the target also holds
   documents you must not read.

If a sync is killed or fails, never run `git reset`; see section 10.

## 6. Watch conflicts resolve live: UI edit, then source edit

This is the full end-to-end loop for any document. Keep two windows open:

- **Window A**: the GROWI browser, logged in as the tester (on the work PC the
  separate tester account), on the page that holds F1 (its GROWI path is the
  third column of `edit.py pages`).
- **Window B**: a terminal in the repo with `export HC_PROJECT=<name>` and
  `P='<local path of that page>'` (first column of `edit.py pages`).

`<F1 value>` is the value on the page; examples from the canary run are in
brackets.

**Step 1: switch on capture.** Window B: `tools/hc_live/round.sh apply a0`.
Expect `rc=0`, nothing captured.

**Step 2: a human edit (rule 1, "keep Y").** Window A: change F1's value to a new
human value and add a marker (`62 °C` → `65 °C（HCT-06）`), save. Window B:

```bash
tools/hc_live/round.sh apply a1
.venv/bin/python tools/hc_live/count.py HCT-06 --page "$P"   # expect growi 1, current 1, live 1
```

`human status` now lists the document with `"human_information": true`. Reload
Window A: the page looks the same (the bot only recorded the edit).

**Step 3: the source changes the same fact differently (rule 4, a conflict).** In
the source document change F1's original value to a third value (`62 °C` →
`63 °C`) and save. Window B: `tools/hc_live/round.sh apply a2` (a few minutes: the
writer updates the page, then the merge call). Reload Window A. Expect the human
value with a note right after it: `65 °C（HCT-06）（元文書の更新: 63 °C）`. The
human value won; the source value is visible.

**Step 4: the source changes another fact in the same section (rule 5).** Change
F2 in the source, save, `round.sh apply a3`. Reload: F2 shows the new source
value; F1 and its note are unchanged.

**Step 5: resolve the conflict in GROWI (cases 11–13, 51).** Window A: delete the
note (keep the human value), save. Window B: `round.sh apply a4`, then
`round.sh apply a5`. The note must not come back. On other notes try: replace the
human value with the source value and delete the note (the page follows the source
again), or write a combination (it becomes the human value).

**Step 6: the source deletes text a human changed (Appendix).** Window A: add a
marker (`HCT-44`) to a line inside section S, save, `round.sh apply a6`. Then
delete section S from the source document, save, `round.sh apply a7`. Expect a new
last page `NNN-付録` (last page number + 1) holding the human's line under the
section heading and the note
`元の文書では、この部分は削除されました（元ページ: …）`, and S gone from its page.
Edit or delete that entry in GROWI and sync: your edit is kept, a deleted entry
does not return. Restore S in the source and sync (case 59): S appears normally
and the Appendix entry stays.

**Step 7: revert and catch-up (cases 57, 58).** Window A: undo one of your edits by
hand, save, sync. Change that fact in the source again and sync: the page follows
the source, with no note.

**Continuous mode instead of manual rounds:**

```bash
.venv/bin/python main.py --project "$HC_PROJECT" watch --growi-interval 60
```

The watcher checks the mount every 10 seconds and GROWI every 60 seconds: edit in
the browser or save a source change and the page updates within a minute or two
(plus build time). Stop it with Ctrl-C.

What to look at when a step surprises you: the round output (`ERROR` lines name the
page and reason), `human status` (blocked pages), the log in
`/tmp/hc-e2e-logs/<label>.log`, and `data/<name>/metadata/human-sync/doc/<key>/current/`.

Document-type notes: spreadsheets (`.xlsx`) are always rebuilt as a whole, so
every human change on them goes through the merge path (slower, more notes);
office documents need the parser; a source change that only moves text may come
out as a large rewrite, which is expected to cost merge calls.

The canary versions used on 2026-10-12 (`canary.py 1`, `2`, `3`) are scripted
source changes for the same steps: v1 changes F1/F2-type values on page 2 and
removes one copy of a repeated line, v2 makes a same-edit and deletes a section, v3
restores it, changes values again, deletes a table row and adds a chapter.

## 7. How to run the remaining cases

Common preparation for the cases that need a fault:

```bash
mkdir -p /tmp/hc-e2e && echo '{"rules": []}' > /tmp/hc-e2e/rules.json
.venv/bin/python tools/hc_live/proxy.py 18765 http://localhost:3000 /tmp/hc-e2e/rules.json > /tmp/hc-e2e/proxy.log 2>&1 &
PROXY="HTTP_PROXY=http://127.0.0.1:18765 http_proxy=http://127.0.0.1:18765 NO_PROXY= no_proxy="
# a round through the proxy:
env $PROXY tools/hc_live/round.sh apply LABEL
# page IDs for rules: the second column of `edit.py pages`
```

Write the rule into `/tmp/hc-e2e/rules.json` just before the round and empty it
(`{"rules": []}`) after. The proxy log shows every request and the action taken.
Stop the proxy with `kill %1` (or the PID) when done. Never point the proxy at
anything but the test GROWI.

### 7.1 Case 29: real 409

1. Pick page P (ID from `edit.py pages`). Change a fact on P in the source.
2. Rule: `{"rules": [{"method": "PUT", "path": "/_api/v3/page", "body_contains": "<P id>", "nth": 1, "action": "delay", "seconds": 60}]}`.
3. `env $PROXY tools/hc_live/round.sh apply c29 &` and watch
   `tail -f /tmp/hc-e2e/proxy.log` until a `PUT … delay` line appears.
4. Within 60 s, edit P in the browser (add `HCT-29`), save.
5. Pass: the round fails with HTTP 409 for P; the proxy log has exactly one PUT for
   P (no retry); GROWI still has `HCT-29`. Empty the rules, run
   `round.sh apply c29b`: `HCT-29` once and the source change published.

### 7.2 Case 30: edit after preflight

Same as 7.1, but change facts on two pages P1 and P2 and delay the PUT of P2
(the later page). P1 publishes, the round stops at P2 with 409, and the human
revision on P2 stays. Next round: both published, the human change once.

### 7.3 Case 31: edit during generation

1. Change a fact on page P in the source (a tier-1 or tier-2 change).
2. `tools/hc_live/round.sh apply c31 &` and `tail -f /tmp/hc-e2e-logs/c31.log`
   until a `writer` line appears (the build is running).
3. Edit P in the browser (add `HCT-31`), save.
4. Pass: `rc=0`; `count.py HCT-31` gives 1/1/1 and the new source fact is shown.
5. Repeat with `HC_SYNC="sync --fast --no-isolated"` (the path `watch` uses): the
   same result. Before the 2026-10-12 fix that path failed and rebuilt again.

### 7.4 Case 34: partial publish, then a late edit

1. Change facts on pages P1 (first) and P2 (second).
2. Rule: status 500 on the PUT of P2 (`"action": "status", "status": 500`).
3. `env $PROXY round.sh apply c34`: P1 published, P2 failed.
4. Edit P1 in the browser (`HCT-34`), save. Empty the rules.
5. `round.sh apply c34b`. Pass: `HCT-34` once; P1 has the new source fact and the
   human change; P2 published; the number of files in
   `metadata/human-sync/doc/*/captures/` grew by exactly one.

### 7.5 Case 35: watcher restart

1. Start `main.py --project $HC_PROJECT watch --growi-interval 60 > /tmp/hc-e2e/watch.log 2>&1 &`.
2. Edit a page (`HCT-35`), save; wait up to 90 s; `count.py HCT-35` gives 1/1/1.
3. Note `ls metadata/human-sync/doc/*/captures | wc -l`. Stop the watcher
   (`kill <pid>`), start it again, wait two minutes.
4. Pass: the capture count is unchanged and `HCT-35` is still 1/1/1.

### 7.6 Case 36: activity cursor restart

With the watcher running, edit page A (`HCT-36a`), wait for it to be captured,
stop the watcher, edit page B (`HCT-36b`) within 60 s of the first edit, start the
watcher. Pass: both markers 1/1/1, nothing captured twice.

### 7.7 Case 37: activity pull and inventory pull agree

1. Make one browser edit (`HCT-37`) and do not sync.
2. Copy the project twice: `cp -a data/hc-e2e-fast /tmp/hc-e2e/A-hc-e2e-fast` and
   the same to `B-…`. Make two INIs, `configs/hc-e2e-A.ini` and `…-B.ini`, equal to
   the test INI except `data_root = /tmp/hc-e2e/A` (or `B`), with each copy
   renamed to `hc-e2e-fast` inside that folder.
3. `main.py --project hc-e2e-A pull` and `main.py --project hc-e2e-B pull --inventory`.
4. Pass: `git -C <copy> ls-tree -r HEAD -- wiki metadata/human-sync/doc | sha256sum`
   is the same for A and B.

### 7.8 Case 38: candidate rollback

Only `watch`, `sync --no-isolated`, and move or delete jobs publish inside a
candidate; plain `sync` publishes after promotion (a failure there is case 62).

1. Edit page P1 in the browser (`HCT-38`) and change facts on P1 and P2 in the source.
2. Rule: status 500 on the PUT of P2.
3. `env $PROXY HC_SYNC="sync --fast --no-isolated" round.sh apply c38`.
4. Pass: the round reports the failure and a rollback; GROWI's P1 shows the old
   source text plus `HCT-38` once; `count.py HCT-38` 1/1/1; capture count +1 only;
   `sqlite3 data/hc-e2e-fast/metadata/watch-queue.sqlite "select * from transactions"`
   is empty afterwards.

### 7.9 Case 39: restore meets a human edit

As 7.8, plus a second rule that delays the restore's PUT of P1 (`"nth": 2` on P1's
id, `"action": "delay", "seconds": 60`). During that delay edit P1 in the browser.
Pass: the restore refuses ("unknown GROWI revision"), the human edit stays in
GROWI, nothing is overwritten. Recovery: see section 10 ("recovery required").

### 7.10 Case 40: crash after a confirmed write

1. Change facts on P1 and P2. Rule: delay 120 s on the PUT of P2.
2. Start the round in the background; when the proxy log shows P1's PUT done and
   P2's `delay`, `kill -9` the sync (`pgrep -f "main.py --project $HC_PROJECT"`).
   The proxy still forwards P2's write after the delay (a lost response).
3. Empty the rules, run `round.sh apply c40b`.
4. Pass: `rc=0`, both pages published once, no human change captured, transactions
   empty, `HEAD==last-good: yes`.

### 7.11 Partly done cases

- **19 (source restructure)**: first add browser edits in three sections
  (`HCT-19a/b/c`) and sync. Then in the source: rename a section heading, move one
  section above another, split one section into two; sync. Pass: each marker
  once, each still next to the text it was about. Record for each whether it
  followed its text, became a note, or went to the Appendix. Wrong topic = fail.
- **44 (Appendix rename)**: with an Appendix present, add a source chapter of more
  than one page (a new page) to the source and run `round.sh apply c44 --force <mount-relative source path>`.
  Pass: the Appendix is renamed to the new last number + 1, its content intact, the
  old Appendix page trashed.
- **56 (`--fast --link` / `--fast --repair`)**: use two documents that mention the
  same names (product names, codes). Run `main.py --project $HC_PROJECT sync --fast --link`,
  then `main.py --project $HC_PROJECT publish`; links appear in GROWI. Edit
  another line in the browser (`HCT-56`), run `round.sh apply c56`. Pass: `HCT-56`
  once, and `cat metadata/human-sync/doc/*/current/*.md | grep -o '](' | wc -l` is
  the same as before `--fast --link` (generated pages already contain page links;
  the inline links must not enter `current/`). Then `sync --fast --link-reset`. For repair: `sync --fast --repair --force`;
  if every page is judged clean nothing is promoted (nothing to check).
- **58 (catch-up then change)**: human sets a value, source makes the same change,
  then the source changes it again. Pass: the newest source value, no note.
- **60 (rebuild rewording)**: with human edits present,
  `round.sh apply c60 --force <mount-relative source path>`. Pass: every marker once and the
  number of `元文書の更新` is unchanged.
- **61 (structure-only edit)**: change a heading level and remove a duplicated
  sentence in the browser; sync. Pass: `human status` guidance count +1; after a
  tier-1 source change on that page the fix is still there with no note.

## 8. Work-PC real-user run (the evidence that counts)

1. Preconditions: the reviewed commit; offline tests pass (section 2.1); the legacy
   journal check of `handoff-conflicts.md` 4.3 has run on every real data root; no
   temporary model setting; the chat endpoint answers.
2. Tester account: a separate GROWI user who completes the access probe of
   `handoff-conflicts.md` 12.7 (create, edit twice, see history) below
   `/conflicts_test`. Never give the tester the bot token.
3. The documents the owner approved for testing, in a unique directory below the
   approved mount (or a synthetic `canary.py 0` file), published to a test subtree.
4. Run the "Modes" check and rows U1–U10, S1–S6 and the watcher check of
   `handoff-conflicts.md` 12.9 with the browser, using the production command
   (`sync --fast` if that is what production runs). Use section 6 above as the
   step-by-step for the S rows.
5. After every step: `count.py` for the markers, `git -C "$PROJECT" rev-parse HEAD refs/llm-wiki/last-good`
   (equal), `git -C "$PROJECT" status --porcelain` (empty), `human status`.
6. Stop at the first lost marker (a count that should be 1 is 0). Keep the data
   root and the GROWI history.
7. Record each case with `LiveVerificationReport` (section 13).

Never list or fetch every page below `/conflicts_test` there: always run
`count.py … --page <local path>`, which reads only the named pages (or fetch single
pages by ID as in `handoff-conflicts.md` 12.8).

## 9. Fault injection

- **Proxy, not a new URL.** Start `proxy.py` and run the pipeline with
  `HTTP_PROXY`/`http_proxy` pointing at it and `NO_PROXY` empty. Both the GROWI
  client and the chat client then go through it while `growi_url` stays unchanged,
  so no endpoint-change republish happens.
- **Model outage.** Rules that answer 503 to chat requests whose body contains
  `A human edited a generated wiki passage` (merge), `Check a merge.` (verify) or
  `You review edits a human made` (classify), with `"nth": 0`. The writer's calls
  still pass, so a source change is built and only the human-edit calls fail. A
  closed `chat_base_url` port would also stop the writer.
- **Process kill.** Start the round in the background, watch its log or the proxy
  log for the stage named in the case, then `kill -9` the `main.py` process (find it
  with `pgrep -f`; do not use `pkill -f` with a pattern that also matches your own
  shell command).

## 10. Troubleshooting (what was seen live)

| Symptom | Cause and what to do |
|---|---|
| `cannot pull GROWI changes into a dirty last-good working tree` | Something changed durable files outside a checkpoint. `git -C <project> status` shows them. Never `git reset`. Find the cause (the first-pull bug of 2.3 item 1 caused this before it was fixed). If you changed state by hand for a test, record it with `checkpoint_live` (section 12) |
| page blocked `remote difference requires human_sync_mode=apply` | expected in `off`/`observe`; set `apply` and sync |
| blocked `remote_moved` | move the page back in GROWI (a rename keeps the revision; the next pull unblocks it) |
| blocked `GROWI ownership markers were removed` | restore the `<!-- llm-wiki-bot-ref:… -->` line in GROWI |
| blocked `remote_deleted` | restore the page from the GROWI trash in the UI (the API route rejects tokens), or decide to remove the source |
| `ambiguous prepared publication outcome` | an unconfirmed bot write whose body is in the page history; inspect the page history before doing anything |
| `recovery required: rollback conflict … GROWI page moved during publication` | a move or publish failed midway. Make GROWI match the last accepted state (for a move: rename the document page back, which brings its children), then sync; recovery finishes and the job is retried |
| `document has human information; remove or move that text in GROWI first` | a delete of a document with human text is blocked by design; remove the human text in GROWI, sync, and the delete goes through |
| a build hangs on a tiny document | the planner looped (output caps disabled in `graph/clients/chat.py`); stop the sync and make the document larger, or restore the caps |
| `--fast --repair` reports a page failure but still runs | a page that failed the pull holds back only its document |
| `00-目次` shows old values | table-of-contents summaries are taken from the plan; not a human-edit problem |

## 11. Compatibility check on a clean baseline

`runner.compat_check` checks that existing projects behave exactly as before
(unchanged sources: 0 model calls, no changed files). On the work PC:

1. Pick a project whose last sync finished cleanly: `queue status` shows nothing
   pending and every document's linker is `complete`.
2. Run `python -m runner.compat_check --project /absolute/path/to/project.ini` on
   the commit **before** this change. It must report 0 blocked model calls and no
   changed files; otherwise pick another project.
3. Run the same on this change's commit. Any difference is a regression to explain.

## 12. Production test from GROWI itself (after sections 8 and 11 pass)

Only the owner changes a real project's config, and only after reviewing results.

1. Back up: `git -C <project-root> tag pre-human-sync refs/llm-wiki/last-good`.
2. Observe: set `human_sync_mode=observe`. After each sync, `human status`: edited
   pages blocked, never overwritten, an observation per page.
3. Apply: set `human_sync_mode=apply`.
4. On one real page add a sentence with `HCT-PILOT` in the editor, sync: once in
   GROWI and in `current/`. Delete it, sync: gone from both.
5. For the first week check `human status` after every sync. Any lost human text:
   switch back to `observe` and report it.

## 13. Recording results without dirtying the project

The live report lives in `metadata/human-sync/live-reports/` (tracked by project
Git), so writing it dirties the tree. Checkpoint it the normal way; never use
`git reset`:

```python
from common.settings import load_settings
from graph.workspace.project import open_project
from publisher.history import checkpoint_live

project = open_project(load_settings("/home/seigyo/llm-wiki/configs/conflicts_test.ini"))
checkpoint_live(project, "live report")
```

## 14. Work-PC environment

```bash
REPO=/home/seigyo/llm-wiki
CFG=/home/seigyo/llm-wiki/configs/conflicts_test.ini
PROJECT=/home/seigyo/llm-wiki/data_big/conflicts_test
PY=/home/seigyo/llm-wiki/.venv/bin/python
cd "$REPO"
```

- GROWI/UI: `http://10.160.152.38:3000/`; boundary `/conflicts_test`.
- Chat/human merge/fast judge+writer: `http://10.160.144.101:51029/v1`,
  `gemma-4-31B`. Embeddings `:51024`, reranker `:51025`, parser
  `:51027/agent/doc-parser/`.
- Pin `human_sync_mode = off` in the test INI before testing; change it only
  through `off` → `observe` → `apply`.
- The bot credential stays in the approved secret location; the tester uses a
  separate browser login.
- Preflight: `$PY main.py --project "$CFG" check`, then
  `human live-plan --path /conflicts_test/human-conflicts-e2e-YYYYMMDD`,
  `human status`, and the two Git checks of section 8 step 5.

## 15. Verification log

- **2026-10-12, home PC** (local GROWI, `sync --fast`, linking off,
  `configs/hc-e2e-fast.ini` and `configs/hc-e2e-repair.ini`; API edits, so not
  real-user evidence): 46 of 62 cases passed (section 2.2); the bugs of 2.3 were
  found and fixed; offline suites 77/24/33/34/33 OK.
- **2026-10-09, work PC**: compileall passed; 67 `test_human_changes`, 21
  `test_human_sync_rollout`, 34 `test_update_tiers` passed; `test_sync_fallback`
  timed out in `test_isolated_sync_finishes_builder_pass_before_linker_phase`.
  Health checks 200; 21 sources, 21 published documents, 260 published pages, no
  page-merge state, no project Git baseline yet. 0/62 work-PC live cases.
- Historical home-PC evidence before the page-merge redesign (12 bot-token rows with
  a temporary thinking cap) does not count toward the matrix.
