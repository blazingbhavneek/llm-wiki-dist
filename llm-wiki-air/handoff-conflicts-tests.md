# Handoff: test the page-merge human-edit design completely, then in production

Companion to `handoff-conflicts.md` (the design, the 62 cases in section 13, and
the real-user procedure in 12.5–12.10). This file says what has really been
verified, what has not, and exactly how to verify the rest. Nothing here is
committed.

## 1. The honest status

**The feature is not verified for production.** What exists:

- Offline tests: 155 focused tests pass. The full suite has 289 tests with the 10
  pre-existing failures listed in section 10.
- Shadow check: 2 history pairs from one test project, 0 failing. That is too
  little to measure anything; 2 of the 3 model merges fell back to the note.
- Live report: **12 passed, 50 constrained (not run), 0 failed.** The 12 are
  `normal_update`, `noop_repeat`, `human_add`, `human_delete`, `same_edit`,
  `disjoint_edit`, `contradiction`, `tier_2`, `markdown_integrity`,
  `fast_policy_human_overlay`, `source_catches_up`, and
  `regeneration_without_fact_change`.

Even those 12 do not count as production evidence:

1. Every "human" edit was made by `lv.edit()`: an API write with the **bot's own
   token** and `origin: "view"`. No person edited anything in the GROWI editor.
   `handoff-conflicts.md` 12.5 explains why that path differs from a real user.
2. The model runs used a temporary thinking cap of 1024 tokens (now reverted),
   so every model-dependent result came from a setting production does not
   have.
3. All of it ran on one small synthetic document (`hc-test.md`), with
   `sync --fast` only.
4. `runner.compat_check` has never passed on a clean baseline.

## 2. What the next agent must do

Test **all 62 cases** of `handoff-conflicts.md` section 13, plus the real-user
rows U1–U10, S1–S6 and the watcher check of 12.9. Rerun the 12 cases already
recorded as passed, with real GROWI edits.

- A case passes only with its own recorded evidence: the hashes, revisions,
  counts and reason codes required by section 13 of the design handoff.
- "Constrained" is allowed only when the case is physically impossible in the
  test environment. It needs a precise reason code and the owner's written
  approval, and it never counts as a pass.
- A case that passes only with a temporary code or model change is a FAIL.
  Report it to the owner, with reason code `needs_workaround`. If the model loops
  without the thinking cap, that is a finding for the owner, not something to
  patch around. Thinking must stay on.
- Run the whole matrix with the sync command production uses. If production uses
  both `sync` and `sync --fast`, run it once for each.
- Include at least one large real document (owner-approved, copied into the
  disposable mount) in addition to `hc-test.md`. Record run time and the
  fallback-note rate per document.
- Stop at the first FAIL that loses human text, and report it.

## 3. Two kinds of edits, and when each is allowed

**Real GROWI edits (production path).** A separate tester account edits in the
GROWI web editor and presses save. This is required for every case where a human
edits a page (sections 5 and 13). It can be done two ways:

- the owner or a tester follows an instruction card from the agent: page path,
  exact change, marker word, then "save";
- the agent drives a real browser logged in as the tester account (for example
  a browser-automation tool), never with the bot token.

**Harness edits (`lv.edit()`, API, bot token).** Allowed only for setup and for
timing-critical race cases that a person cannot hit by hand (Group F). Even
then, run each race case once with a real GROWI edit inside the window the
fault injector opens (section 4).

Source edits (changing the file in the test mount) and the commands (`sync`,
`pull`, `watch`) are always done by the agent.

Use the marker words, the counting script, and the per-step Git checks from
`handoff-conflicts.md` 12.8 for every case.

## 4. Fault-injection tools (no repository code changes)

All of these live in the scratchpad, never in the repository.

**GROWI proxy.** A small local reverse proxy, written with the Python standard
library or `httpx` from the venv, between the pipeline and GROWI. It forwards
only to the real GROWI URL, keeps the `Authorization` header, and supports these
modes, each limited to one path and one request number:

- `delay N`: hold a request N seconds, which opens a window for a human edit;
- `status 409` or `status 500`: answer without forwarding;
- `drop`: forward the request, then close the connection without returning
  the response (a lost response).

Use a second disposable INI (`hcv-proxy.ini`) that is identical to `hcv.ini`
except `growi_url` points at the proxy. Keep gating (`lv.gate()`) against the
real GROWI URL: the live-plan confirmation code is tied to that URL.

**Process kill.** Start `sync` in the background, watch its progress output for
the stage named in the case, then `kill -9` it.

**Model outage.** A third INI whose `chat_base_url` points at a closed local
port. Do not stop the shared model server.

**Natural windows.** One document takes about 5 minutes to generate, which is
enough time for a person to save an edit in GROWI during generation.

## 5. How to produce each case

"UI" means a real GROWI edit by the tester account (section 3). "Source" means
the agent edits the file in the test mount. Pass criteria are in
`handoff-conflicts.md` section 13; they are not repeated here.

| # | Case | How to produce it |
|---|---|---|
| 1 | `initial_multi_page_publish` | New `target_name` (fresh subtree) and empty data root; first `sync`. Record it this time. |
| 2 | `normal_update` | Source: change one fact; `sync`. |
| 3 | `noop_repeat` | `sync`, `pull`, `publish` again with nothing changed. |
| 4 | `process_restart` | `kill -9` between commands and during a sync; rerun. |
| 5 | `human_add` | UI: add a paragraph. `off` → `observe` → `apply` as in 12.9 "Modes". |
| 6 | `human_replace` | UI: replace a value; `sync` twice. |
| 7 | `human_delete` | UI: delete a sentence; then a source change; then restart. |
| 8 | `same_edit` | UI: change a value; Source: make the same change; `sync`. |
| 9 | `disjoint_edit` | UI and Source change different facts in one section. |
| 10 | `contradiction` | UI and Source change the same value differently. |
| 11–14 | `keep_human`, `accept_source`, `combine`, `suppress` | UI actions on a note or a human paragraph, as described in section 13. |
| 15 | `tier_0` | `sync --force <source>` with the source unchanged; check `update_decision`. |
| 16 | `tier_1` | Source: one small edit on a page with a UI edit. |
| 17 | `tier_2` | Source: changes spanning several pages (the round-2 `NEW_FILLER` pattern). |
| 18 | `tier_3` | Source: a large rewrite, or `sync --force` that the writer reports as tier 3. |
| 19 | `split_combine_rename_reorder` | Source: split a section, merge two, rename a heading, reorder, with UI edits in each. |
| 20 | `remote_move` | UI: move an owned page in GROWI. |
| 21 | `remote_delete` | UI: delete an owned page. |
| 22 | `marker_damage` | UI: edit the ownership marker comment in the editor. |
| 23 | `foreign_destination` | UI: create a page by hand at the path the next source page will use. |
| 24 | `duplicate_marker` | UI: paste one page's ownership marker into another page. |
| 25 | `source_rename` | Source: rename the file (with a UI edit on it); then add a different file at the old name. |
| 26 | `bulk_delete` | Source: remove several files, one with a UI edit and one with only a UI deletion. |
| 27 | `markdown_integrity` | UI: an edit containing a code fence, an HTML comment, a table and inline code. |
| 28 | `attachments_images` | UI: upload an image or attachment into an edited page. |
| 29 | `conditional_409` | Proxy `status 409` on the page's update; then a real UI edit timed with proxy `delay`. |
| 30 | `edit_after_preflight` | Proxy `delay 120` on the second page's update; UI edit that page during the delay. |
| 31 | `edit_during_generation` | UI edit on the document's page while its generation runs. |
| 32 | `lost_update` | Proxy `drop` on an update, then `kill -9`; rerun. |
| 33 | `lost_create` | Proxy `drop` on a create, then `kill -9`; rerun. |
| 34 | `partial_publish_late_edit` | Proxy `status 500` on the second page; UI edit on the first page; rerun. |
| 35 | `watcher_restart` | `watch --growi-interval 60`; UI edit; restart the watcher around a poll. |
| 36 | `activity_cursor_restart` | Two UI edits within 60 s of each other; restart between polls. |
| 37 | `full_inventory_equivalence` | Two copies of the data root: `pull` on one, `pull --inventory` on the other; compare state hashes. |
| 38 | `candidate_rollback` | UI edit captured inside a sync that republishes it; proxy `status 500` on a later page in the same sync. |
| 39 | `last_good_restore` | Same failure as 38; check the restore inspected revisions; add a UI edit before the restore where the proxy delay allows. |
| 40 | `service_restart_reconcile` | Proxy `delay` after a confirmed update; `kill -9` inside that delay; rerun. |
| 41 | `model_outage_fallback` | The closed-port INI; source change on a page with a UI edit; `sync`. |
| 42 | `concurrent_add_same_anchor` | UI and Source add text at the same place: identical, then different. |
| 43 | `human_delete_source_modify` | UI deletes a fact; Source changes it. |
| 44 | `human_modify_source_delete` | UI changes a fact; Source deletes the fact, then its section, then its page; then Source adds a page (Appendix rename). |
| 45 | `concurrent_delete_same_fact` | UI and Source delete the same line, then the same section. |
| 46 | `multiple_remote_revisions_before_pull` | Three UI saves on one page (one undoes another) before one `sync`. |
| 47 | `mixed_page_capture_failure` | UI edit on page A and marker damage on page B, then one `sync`. |
| 48 | `source_move_delete_with_remote_edit` | UI edit, then Source renames (and separately deletes) that document before the next `sync`. |
| 49 | `transport_only_remote_revision` | UI: open and save without changes. |
| 50 | `mode_transition_same_revision` | One UI edit; INI mode `off` → `observe` → `apply` → `off` → `apply`, restarting between runs. |
| 51 | `growi_conflict_ui_resolution` | UI: resolve a note four ways (keep human, keep source, combine, edit around it). |
| 52 | `remote_revision_rollback` | UI: restore an older revision from the GROWI page history. |
| 53 | `idle_sync_remote_reconciliation` | UI edit, mount unchanged, plain `sync` (no `pull`). |
| 54 | `human_section_placement` | UI: add a section mid-page, rename a heading, reorder sections. |
| 55 | `conflict_survives_unrelated_edit` | UI: with a note showing, edit another line in that section. |
| 56 | `fast_policy_human_overlay` | Rerun 5, 10, 54 with `sync --fast`, `sync --fast --link`, and `sync --fast --repair` (run `sync --fast --link-reset` before the repair). |
| 57 | `human_revert` | UI: undo an earlier UI edit by editing the text back; then a source change. |
| 58 | `source_catches_up` | UI change; Source makes the same change later; then Source changes it again. |
| 59 | `source_removes_then_restores` | Source deletes a UI-edited part, then restores it. |
| 60 | `regeneration_without_fact_change` | `sync --force` that rewords UI-edited sections without changing facts. |
| 61 | `structure_only_edit` | UI: fix a heading, the formatting, and remove a duplicate sentence; then a tier-1 and a tier-3 source change; then repeat with the closed-port INI so classification fails. |
| 62 | `pull_onto_unpublished_generation` | Source change whose publish fails (proxy `status 500`); UI edit on the same document; `pull`; then a normal `sync`. |

Also run the real-user rows of 12.9 that are not in `LIVE_CASES`, especially
**U7, the stale editor save**: open the editor, let the bot publish the same
page, then save. If the bot's new value silently disappears, record FAIL
`editor_stale_save` and stop. Record U-, S- and watcher results in the
verification log (section 10).

## 6. Compatibility check on a clean baseline

`runner.compat_check` checks that existing projects behave exactly as before
(unchanged sources: 0 model calls, no changed files). It failed earlier only
because the inputs had pending work. Do it properly on the work PC:

1. Pick a real project whose last sync finished cleanly: `queue status` shows
   nothing pending and every document's linker is `complete`.
2. Run `python -m runner.compat_check --project configs/<project>.ini` on the
   commit **before** this change. It must report 0 blocked model calls and no
   changed files; otherwise the project is not a clean baseline, so pick another.
3. Run the same command on this change's commit. It must also report 0 blocked
   model calls and no changed files. Any difference is a regression to explain.

compat_check works on a copy and blocks model, parser and GROWI-write calls; it
reads the config but does not change it (AGENTS.md).

## 7. Production test from GROWI itself (after sections 2–6 pass)

Only the owner changes a real project's config (safety rule 1 in the design
handoff), and only after reviewing the full results.

1. **Back up.** Tag the project's accepted state:
   `git -C <project-root> tag pre-human-sync refs/llm-wiki/last-good`.
   GROWI page history keeps every remote revision.
2. **Observe.** The owner sets `human_sync_mode=observe` in that project's INI.
   Real users edit as usual. After each sync: `human status`. Edited pages must
   be blocked and never overwritten, with an observation recorded for each.
3. **Apply.** After the owner reviews the observations, the owner sets
   `human_sync_mode=apply`.
4. **Marker check by the owner.** On one real page, add a sentence containing
   `HCT-PILOT` in the GROWI editor and save. Run the normal sync (or wait for the
   watcher). `HCT-PILOT` must appear exactly once in GROWI and once in the local
   `current/` (the counting script in 12.8). Then delete it in GROWI and sync
   again: it must be gone from both.
5. **Watch.** Check `human status` after every sync for the first week: blocked
   pages, fallback notes, and Appendix entries. Any lost human text means switching
   back to `observe` and reporting it.

## 8. Recording results without dirtying the project

The live report lives in `metadata/human-sync/live-reports/`, which project Git
tracks. Writing it makes the tree dirty, and the next `sync` or `pull` then
refuses to run. After recording, checkpoint it the normal way. Never use
`git reset`.

```python
from common.settings import load_settings
from graph.workspace.project import open_project
from publisher.history import checkpoint_live

project = open_project(load_settings("/absolute/path/hcv.ini"))
checkpoint_live(project, "live report")
```

## 9. Known code issues to fix before the affected cases

Found in review and not yet fixed. Otherwise these cases will show these bugs
instead of testing the design:

- The new GROWI pull at the start of `sync` (`runner/cli.py`,
  `_cmd_sync_isolated`) is not wrapped. A GROWI outage or a dirty tree raises and
  aborts the whole sync; design 3.7 says to record the failure and continue.
  Affects 41, 53 and any test with GROWI unreachable.
- A moved document's `doc.json` keeps its old `raw_rel` until its next write, so
  a new file placed at the old path can be blocked by mistake. Affects 25.
- A page whose GROWI revision changed but that has no published baseline (an old
  ledger) now blocks in every mode; the old code pinned it in `apply`. Operator
  path: restore the page to its published content in GROWI, `pull` (which
  records the baseline), then make the edit again.
- `legacy human state` (generator state with old `human_edited` sidecars) now
  raises in the writer instead of pinning. It needs the owner's decision.
- When two located changes overlap in the new text, the later one goes to the
  Appendix instead of being merged into one window (design 3.2 step 3). This is
  safe, since nothing is lost, but it is a deviation.

Shadow-check questions still open: why 2 of 3 model merges fell back (check the
verify reasons in the log), and the unexplained `newer_lines_missing_count` of 2
and 1.

## 10. Environment (scratchpad, not the repo)

`$SP` = `/tmp/claude-1000/-mnt-common-Code-llm-wiki-dist-llm-wiki-air/d42fe7bd-b92c-425f-afc3-14df1364827f/scratchpad`

- `$SP/shadow/env.sh`: exports the model endpoint (`http://localhost:51029/v1`,
  `gemma-4-31B`, `WIKI_CONCURRENCY=1`, `WIKI_REQUEST_TIMEOUT=900`). Check
  `curl -s localhost:51029/v1/models` before any model call.
- GROWI: `http://localhost:3000`. The bot token is `growi_token` in
  `configs/mount-docs.ini` (`[project]`); `lv.py token()` reads it. Never print
  it. The tester account (section 3) needs its own login and never this token.
- `$SP/live/hcv.ini`: disposable INI (`target_name = hcv-fast-1008`,
  `data_root = $SP/live/data`, `human_sync_mode = apply`, `growi_mode = attach`).
  `lv.set_mode()` edits the mode.
- `$SP/live/liveplan.json`: live plan and confirmation code. Do not paste the
  code anywhere.
- `$SP/live/src.py <round>`: writes `hc-test.md` for round 0, 1 or 2.
- `$SP/live/lv.py`: `gate()`, `run()`, `pages()`, `body()`, `local_pages()`,
  `edit()` (harness edits only, section 3), `set_mode()`.
- `tools/human_rebase_shadow.py` (repo, untracked): the shadow check. Keep its
  full-history document discovery.

Run driver calls from `$SP/live` with the repo's venv:

```bash
cd $SP/live && /mnt/common/Code/llm-wiki-dist/llm-wiki-air/.venv/bin/python -c 'import lv; print(lv.pages())'
```

Long runs: start them in the background and poll the output file.

Rules: GROWI writes only below `/hcv-fast-1008` (or a new disposable subtree),
and only after `lv.gate()`. `human_sync_mode` is changed only in the disposable
INIs. Output carries hashes, counts and reason codes, never page bodies or
secrets. Nothing is committed unless the owner asks.

## 11. Verification log so far (2026-10-08)

- `.venv/bin/python -m unittest discover -s tests`: 289 run, 10 failures, 27
  skipped. The pre-existing failures:
  `test_diff_project_config_and_docx_fixture_exist`,
  `test_incremental_linker_keeps_valid_peer_untouched_and_skips_search`,
  `test_incremental_scope_with_many_old_edges_checks_only_visible_ones`,
  `test_neo_incremental_linker_preserves_entity_direction_and_checks_only_visible_edges`,
  `test_rename_plus_edit_keeps_old_digest_until_the_new_blob_is_parsed`,
  `test_update_uses_parser_for_new_image_descriptions`,
  `test_document_index_page_cards_unchanged`, `test_root_lists_team_names_only`,
  `test_related_documents_cached_and_rendered_for_both_docs`,
  `test_first_structured_attempt_gets_token_cap_and_temperature`.
- Focused suite (`test_human_changes`, `test_human_sync_rollout`,
  `test_sync_fallback`, `test_update_tiers`): 155 tests, OK.
- `compileall`: passed.
- Shadow, real model (thinking cap on): 2 pairs, 8 model calls, 0 failing; 2 of
  3 window-5 merges fell back. Model-free rerun: 0 failing. Fast copy: no
  eligible pairs.
- `compat_check`: not a valid result (unclean inputs: 87 and 62 blocked model
  calls). Redo per section 6.
- Live round 1 (`sync --fast`, tier 1) and round 2 (tier 2): exit 0,
  `human_edits_overwritten=[]`, repeats unchanged; transport normalization
  matched except the expected navigation lines. All edits were harness edits
  with the bot token, and the thinking cap was on.
- The live report was checkpointed with `checkpoint_live` twice;
  `HEAD == refs/llm-wiki/last-good` for the disposable project.
