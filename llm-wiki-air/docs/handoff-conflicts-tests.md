# Handoff: verify page-merge human edits on the work PC and real GROWI

Companion to `handoff-conflicts.md` (the design, the 62 cases in section 13, and
the real-user procedure in 12.5–12.10). This file says what has really been
verified, what has not, and exactly how to verify the rest.

Last audited: 2026-10-09 on the work PC. The approved project is
`/home/seigyo/llm-wiki/configs/conflicts_test.ini`; all live evidence must come
from `http://10.160.152.38:3000/`, below `/conflicts_test`, using synthetic pages
edited in the GROWI browser by a separate tester account. Do not enumerate or
display the existing private wiki bodies.

## 1. The honest status

**The feature is not verified for production.** Current work-PC evidence:

- The configured chat, embedding, reranker, parser, and GROWI health/model
  endpoints returned HTTP 200. The local JEV GGUF probe passed. The GROWI root
  and `/login` both returned HTTP 200. These checks read no wiki page body.
- Metadata-only inspection found 21 source records, 21 published documents and
  260 published page records. Every recorded remote path is below
  `/conflicts_test`; none is blocked. There are no page-merge document states,
  captures, observations, or live reports, so **0 of 62 cases have work-PC live
  evidence**.
- The project is pre-populated but has no project-local `.git` repository or
  `refs/llm-wiki/last-good` yet. `human live-plan` must establish and verify the
  baseline before any remote write.
- `compileall` passed. Individually, 67 `test_human_changes`, 21
  `test_human_sync_rollout`, and 34 `test_update_tiers` tests passed (122 total).
  `test_sync_fallback` timed out after 240 seconds in
  `test_isolated_sync_finishes_builder_pass_before_linker_phase`; the eight
  preceding tests passed, but the suite has no passing result. The full suite
  was not claimed as passing.

The home-PC report remains historical only: it recorded 12 passes and 50
constraints, but all edits used the bot token/API, all model runs used a
temporary thinking cap, and only `sync --fast` on one small document ran. None
of those rows counts toward the work-PC matrix. `runner.compat_check` also still
lacks a clean-baseline pass.

## 2. What the next agent must do

Test **all 62 cases** of `handoff-conflicts.md` section 13 against the configured
real GROWI instance, plus the real-user rows U1–U10, S1–S6 and the watcher check
of 12.9. Rerun the 12 historical cases; they start as pending, not passed.

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
- Use a new synthetic source directory and bot-owned synthetic pages for all
  deliberate edits. Do not open an existing private page to select test text.
  A large test must also be synthetic unless the owner separately approves a
  private document and performs the content inspection personally.
- Stop at the first FAIL that loses human text, and report it.

## 3. Two kinds of edits, and when each is allowed

**Real GROWI edits (production path).** A separate tester account edits in the
GROWI web editor and presses save. This is required for every case where a human
edits a page (sections 5 and 13). It can be done two ways:

- the owner or a tester follows an instruction card from the agent: page path,
  exact change, marker word, then "save";
- the agent drives a real browser logged in as the tester account (for example
  a browser-automation tool), never with the bot token.

Before either path, the tester must complete the create/edit/history access probe
in `handoff-conflicts.md` 12.7 at `http://10.160.152.38:3000/`. A health check or
bot-token API call does not prove tester permissions. After the bot publishes the
synthetic canary, the tester must also successfully edit that bot-owned page;
only then may the matrix begin.

**Harness edits (API, bot token).** Allowed only for setup and for
timing-critical race cases that a person cannot hit by hand (Group F). Even
then, run each race case once with a real GROWI edit inside the window the
fault injector opens (section 4).

Source edits (changing only the synthetic file in the test mount) and the
commands (`sync`, `pull`, `watch`) are done by the operator/agent. Existing
private source and generated page bodies stay unopened.

Use the marker words, the counting script, and the per-step Git checks from
`handoff-conflicts.md` 12.8 for every case.

## 4. Fault-injection tools (no repository code changes)

All of these live below a fresh `/tmp/llm-wiki-conflicts-e2e-<run>/`, never in
the repository. They operate on the configured real GROWI instance and the
synthetic test paths only.

**GROWI proxy.** A small local reverse proxy, written with the Python standard
library or `httpx` from the venv, between the pipeline and GROWI. It forwards
only to the real GROWI URL, keeps the `Authorization` header, and supports these
modes, each limited to one path and one request number:

- `delay N`: hold a request N seconds, which opens a window for a human edit;
- `status 409` or `status 500`: answer without forwarding;
- `drop`: forward the request, then close the connection without returning
  the response (a lost response).

Use a second external INI (`conflicts-proxy.ini`) derived from
`configs/conflicts_test.ini`, except `growi_url` points at the proxy. Do not add
the proxy INI or credentials to the repository. Boundary verification still
uses the real GROWI URL and the exact synthetic path: the live-plan confirmation
code is tied to that URL. Verify that endpoint-change reconciliation will not
republish private documents before starting a proxy case; otherwise use a fresh
synthetic `target_name` on the same GROWI server for Group F.

**Process kill.** Start `sync` in the background, watch its progress output for
the stage named in the case, then `kill -9` it.

**Model outage.** A third external INI derived from the approved config whose
`chat_base_url` points at a closed local port. Do not stop or reconfigure the
shared work-PC model server.

**Natural windows.** Measure the synthetic large document first; do not retain
the home-PC assumption that generation takes about five minutes. If it is too
fast for a person to save, use the path-scoped delay proxy.

## 5. How to produce each case

"UI" means a real GROWI edit by the tester account (section 3). "Source" means
the agent edits the file in the test mount. Pass criteria are in
`handoff-conflicts.md` section 13; they are not repeated here.

| # | Case | How to produce it |
|---|---|---|
| 1 | `initial_multi_page_publish` | Publish a new multi-page synthetic document below the confirmed unique test path. If a completely empty project root is required, use an external INI with a fresh synthetic `target_name` on the same real GROWI server. |
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
| 37 | `full_inventory_equivalence` | Two copies of the data root: `pull` on one, `pull --inventory` on the other; compare state hashes. Because inventory can fetch all owned bodies under `/conflicts_test`, this needs explicit owner approval or a fresh synthetic `target_name` on the same real server. |
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

1. Pick a project whose last sync finished cleanly: `queue status` shows nothing
   pending and every document's linker is `complete`. The current
   `conflicts_test` copy was created with linker work pending and is not a valid
   clean baseline until that work has completed and been checkpointed.
2. Run `python -m runner.compat_check --project /absolute/path/to/project.ini`
   on the commit **before** this change. It must report 0 blocked model calls and
   no changed files; otherwise the project is not a clean baseline, so pick
   another. The CLI does not accept `configs/<name>.ini` as a relative project
   argument; use a config name or an absolute INI path.
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

project = open_project(load_settings("/home/seigyo/llm-wiki/configs/conflicts_test.ini"))
checkpoint_live(project, "live report")
```

## 9. Implementation audit and open blockers

Already implemented in this worktree:

- `LIVE_CASES` contains all 62 IDs.
- An idle isolated `sync` calls `pull_growi_once()` before scanning or building;
  `test_idle_sync_captures_a_ui_edit_before_any_scan_or_build` covers the
  successful path.
- `rebase()` combines windows that overlap in the new text; the old home-PC note
  saying the later change always went to the Appendix is obsolete.
- `_commit()` refreshes `doc.json.raw_rel` whenever human state is written.
- The page journal replacement, guidance, Appendix, rollback replay, fast-link
  invalidation, and legacy safety blocks described in the design handoff are
  present.

Still open or requiring a live decision before the affected case can pass:

- The initial GROWI pull in `_cmd_sync_isolated` is not caught. A GROWI outage or
  dirty-tree error aborts the sync instead of being recorded while unrelated
  documents continue. This affects 41 and outage variants of 53.
- A pure source move does not write human state, so `doc.json.raw_rel` can retain
  the old path until a later generation or capture. A different source created
  immediately at the old path can be blocked by mistake. This affects 25.
- A changed remote page with no inspected published baseline fails closed in all
  modes. Restore it to the published content, run `pull` to establish the
  baseline, then repeat the tester edit; do not adopt it blindly.
- `legacy human state` still raises instead of pinning. The owner must decide how
  to handle any project that contains the old `human_edited` sidecars.
- `test_sync_fallback` currently hangs in
  `test_isolated_sync_finishes_builder_pass_before_linker_phase`. Resolve or
  explain that timeout before claiming the focused regression gate passes.

Shadow-check questions remain open: why 2 of 3 historical model merges fell back,
and why the old report showed `newer_lines_missing_count` values of 2 and 1.

## 10. Work-PC environment

```bash
REPO=/home/seigyo/llm-wiki
CFG=/home/seigyo/llm-wiki/configs/conflicts_test.ini
PROJECT=/home/seigyo/llm-wiki/data_big/conflicts_test
PY=/home/seigyo/llm-wiki/.venv/bin/python
SP=/tmp/llm-wiki-conflicts-e2e-YYYYMMDD
cd "$REPO"
```

- GROWI/UI: `http://10.160.152.38:3000/`; boundary `/conflicts_test`.
- Chat/human merge/fast judge+writer: `http://10.160.144.101:51029/v1`,
  `gemma-4-31B` (no separate writer or judge endpoint is configured).
- Embeddings: `http://10.160.144.101:51024/v1`; reranker:
  `http://10.160.144.101:51025/v1`; parser:
  `http://10.160.144.101:51027/agent/doc-parser/`.
- Link decisions use the configured local JEV GGUF scorer (`Q4_K_M`, batched).
- The INI's omitted rollout settings currently resolve to `off` and `attach`.
  Pin `human_sync_mode = off` explicitly before testing, then change only this
  approved test setting through `off` -> `observe` -> `apply`.
- The bot credential stays in the approved secret location and is never printed.
  The tester uses a separate browser login and never receives the bot token.
- `tools/human_rebase_shadow.py` remains the read-only shadow tool. Do not run it
  on the private live tree; use an approved copy.

Preflight:

```bash
$PY main.py --project "$CFG" check
$PY main.py --project "$CFG" human live-plan \
  --path /conflicts_test/human-conflicts-e2e-YYYYMMDD
$PY main.py --project "$CFG" human status
git -C "$PROJECT" rev-parse HEAD refs/llm-wiki/last-good
git -C "$PROJECT" status --porcelain
```

Use the absolute INI path or the name `conflicts_test`; the relative argument
`configs/conflicts_test.ini` is rejected. Long runs may be backgrounded and
polled, but logs contain hashes, counts, IDs and reason codes only. Evidence
helpers fetch an exact synthetic page by ID and never list all pages. No cleanup,
reset, inventory, or proxy run begins without rechecking the real endpoint and
synthetic boundary.

## 11. Verification log

Work-PC audit, 2026-10-09 (`c60392d` plus the current uncommitted feature
changes):

- `python -m compileall -q common convert wiki linker index publisher runner graph`:
  passed.
- `tests.test_human_changes`: 67 passed; expected injected 409/500/lost-response
  traces were emitted.
- `tests.test_human_sync_rollout`: 21 passed.
- `tests.test_update_tiers`: 34 passed.
- `tests.test_sync_fallback`: timed out at 240 seconds in
  `test_isolated_sync_finishes_builder_pass_before_linker_phase`; eight earlier
  tests in its verbose run passed. No focused-suite or full-suite pass is
  recorded.
- Health: chat 200, embedding 200, reranker 200, parser 200, GROWI health 200,
  local JEV probe passed; GROWI root 200 and login 200. No wiki body was read.
- Local metadata: 21 sources, 21 published documents, 260 published pages; all
  remote paths within `/conflicts_test`, 0 blocked, 0 page-merge states/captures/
  observations/live reports. No project-local Git baseline yet.
- Actual GROWI live matrix: 0/62 work-PC cases run. Tester login/edit permission
  is not yet proven; complete section 3 before changing mode or publishing a
  canary.
- `compat_check`: no valid clean-baseline result yet.

Historical home-PC evidence (not transferable): a 289-test full run reported 10
failures and 27 skips; the old focused run reported 155 passing tests; a two-pair
shadow run made 8 model calls with 2 of 3 window-5 merges falling back; and 12
live rows used bot-token harness edits plus a temporary thinking cap. Preserve
those records as history, but do not count them as work-PC, real-user, or
production evidence.
