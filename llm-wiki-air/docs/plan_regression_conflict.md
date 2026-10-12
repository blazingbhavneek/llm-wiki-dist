# plan_regression_conflict.md — handoff (2026-10-04)

Read this whole file before changing anything. It records what was built on
2026-10-04, how each piece is proven, what must never regress, and how to continue
the human-edit (GROWI) work. Every claim below was verified on this machine against
the real gemma endpoint unless marked otherwise.

User rules that apply to every change (from the owner, non-negotiable):

- Backward compatibility is priority #1. An unchanged source must make 0 LLM calls and
  change 0 bytes. Never bulk-migrate data. Data written by the production commit
  (`a9a7dcf`) must keep working.
- No LLM concurrency bumps (shared server). Never disable model thinking. No prompt
  truncation. The linker is a nightly job: quality over speed, do not cap verification.
- Commit only when the owner asks. Use they/them for the owner. Give short status
  updates during long runs.

---

## 0. Green-flag checklist (run this first, in `llm-wiki-air/`)

| # | Command | Expected |
|---|---|---|
| 1 | `.venv/bin/python -m compileall -q common convert wiki linker index publisher runner graph` | no output |
| 2 | `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` | `Ran 280 tests`, **exactly the 8 inherited failures** in §3.1, nothing else |
| 3 | `.venv/bin/python -m unittest tests.test_phase_compat tests.test_human_changes tests.test_human_sync_rollout tests.test_update_tiers tests.test_sync_fallback` | `OK` |
| 4 | `GROWI_TOKEN=... .venv/bin/python -m runner.compat_check --project mount-docs --data-root <a fully published root>` | `"calls": []`, sync/build/index exit `0`, tree changes only runtime files |
| 5 | Replay equivalence vs production (§3.4) | every line `OK` |

Status at hand-off: 1–5 all green (compat on `data_std2`, replay vs `a9a7dcf`).

---

## 1. Repository and commit state

```
9b647c1 will test human + fast path      <- owner commit, contains §2.1–§2.5
65916ba refactored a little bit for clean and added fast path
a9a7dcf sync with work pc                 <- PRODUCTION baseline
```

Uncommitted at hand-off (commit them together, after the green-flag checklist):

| Path | What |
|---|---|
| `publisher/human_changes.py` | two human-capture fixes (§2.7) |
| `graph/growi/client.py` | passes the pre-link page to `HumanStore.capture` (§2.7) |
| `runner/cli.py` | fast sync publishes what it built (§2.4) |
| `tests/test_human_changes.py` | `TransportSpellingMergeTest`, `test_linker_links_are_not_recorded_as_part_of_a_human_edit` |
| `tests/test_sync_fallback.py` | `StoppedBuildRecoveryTest`, three-queue and fast-publish tests |
| `tests/test_phase_compat.py` | **gitignored** (`tests/*`) — needs `git add -f` |
| `configs/mount-docs-fast.ini`, `configs/mount-docs-hc.ini` | test configs, **no token inside** |
| `tools/replay_equivalence.py`, `tools/hc_live/*` | test tooling (§3.4, §5) |
| `plan_regression_conflict.md` | this file |

Secrets: `configs/mount-docs.ini` holds the bot GROWI token; never copy it into another
file. The new configs take it from the environment:
`export GROWI_TOKEN="$(sed -n 's/^growi_token *= *//p' configs/mount-docs.ini)"`.
The human-editor token used in §5 was given in chat and is not stored anywhere; ask
the owner for it (`HUMAN_TOKEN` env var).

---

## 2. What exists now — none of this may disappear

### 2.1 Phase refactor (65916ba, kept)

- Phases `convert/ wiki/ linker/ index/ publisher/` expose `Config/Input/Result/run`;
  `runner/` composes them; `common/` holds paths, storage, settings, context, policy.
- `main.py` is a thin adapter over `runner/cli.py` (all old command names/flags work).
- Function-by-function diff of old `main.py` vs `runner/cli.py`: `cmd_sync` identical
  to production; `build/convert/index/publish` go through `runner/steps.py` /
  `publisher/phase.py` wrappers that call the same production functions.
  Cosmetic difference: `main.py publish` prints result rows as quoted strings.
- `AGENTS.md` documents the boundaries; keep it in sync.

### 2.2 Policy seam and isolation (the most important invariant)

- `common/policy.py:Policy` is the ONLY seam between the shared engine (`graph/`) and
  policy variants. Every hook default IS the production behaviour. Hooks:
  `cache_key, stamp, config_overrides, title, section, code_tokens, adjust_seed_plan,
  offline_seed_plan, hierarchy, mask_for_linking, neo_limits, curate_page,
  filter_target, describe_pages`, flags `research, intro, strict_judge, offline, link,
  model_table_structure, table_analyses`.
- `graph/fast/` (`policy.py`, `wiki.py`, `linker.py`) holds every fast behaviour.
- The engine must never branch on a policy name, and the standard path must never
  import `graph.fast` (it is loaded lazily by `resolve_policy("fast")`).
- Guarded by `tests/test_phase_compat.py::IsolationTest` (subprocess import check,
  regex scan of engine packages, standard-hook identity).
- To add fast behaviour: add a hook with a standard-preserving default to `Policy`,
  call it from the engine, override it in `graph/fast/policy.py`. Never edit engine
  logic for fast only.
- `WIKI_POLICY` env knob was removed on purpose: only `sync --fast` selects fast.

### 2.3 Standard (production) path

Proven equal to production `a9a7dcf` by the replay harness (§3.4): same prompt set
(incl. max tokens / temperature), identical pages, identical linker output for all 5
mount-docs documents. Intentional standard changes since production (each requested
by the owner, each with a test):

| Change | Where | Guard |
|---|---|---|
| Claim order: non-pdf (txt/md/docx/pptx/Excel/csv) smallest first, then pdfs smallest first | `publisher/queue.py:_PRIORITY`, `claim`, `queued_jobs` | `QueuePriorityTest` |
| Parse cache + parse-ahead worker | `publisher/pipeline.py:_parse/parse_cache_file`, `publisher/ahead.py:ParseAhead` | `ParseCacheTest` |
| Link-ahead worker + linker metadata cache | `publisher/ahead.py:LinkAhead`, `graph/linker/meta_cache.py`, `service.link_document` (cache read only for chunks it describes anyway) | `LinkAheadTest`, `test_three_queues_link_earlier_built_documents_while_new_ones_build` |
| Stop → publish → continue never refuses | `publisher/queue.py:recover` (building/prepared ops with moved last-good are dropped; publishing ops still refuse) | `StoppedBuildRecoveryTest` (3 cases) |
| LaTeX-in-JSON tolerant parse (only after a strict failure) | `graph/clients/chat.py:extract_json_from_text/_loads` | `LatexJsonTest` |
| Human capture fixes | §2.7 | §2.7 |

### 2.4 Fast path (`sync --fast`)

Pipeline: parse → plan → write → publish, **no linker at all**.

- Plan: model planner with `planner_max_output_tokens=12000` (4000 truncated window
  JSON + 3 extra calls per failure), pages under half the 100-line target merged into
  the smaller neighbour (`graph/fast/wiki.py:merge_small_pages`).
- Write: full rewrite + judge (strict: failed judge or missing info → verbatim
  source section), no research, no intro, heading numbers stripped, lenient identifier
  check (TeX commands in math and shouted headings are not identifiers), deterministic
  hierarchy context, math masked during link insertion.
- Spreadsheets: row-oriented structure without a model call; **xls 解説 story pages
  are kept**; csv analysis page skipped.
- Linking: `Policy.link=False` → `runner/cli._settings` sets `wiki_linker_enabled=False`
  → no link phase, no link-ahead, markers `disabled`; the isolated sync then publishes
  built docs with `publish_only(allow_unlinked=True, link_pending=False)`.
  A later standard sync links fast-built documents (a `disabled` marker is not
  "up to date" for an enabled linker).
- Command (publishes to `/mountdocs-fast`, never `/mountdocs`):
  ```
  export GROWI_TOKEN="$(sed -n 's/^growi_token *= *//p' configs/mount-docs.ini)"
  time .venv/bin/python main.py sync --fast --project mount-docs-fast --data-root data_fast5 --timeout 900 2>&1 | tee data_fast5.log
  ```
- Guards: `FastSkipsLinkerTest`, `test_a_sync_without_linking_publishes_what_it_built`,
  and in `test_phase_compat.py`: `HeadingNumberTest`, `FastPageMergeTest`,
  `IdentifierCheckTest`, `FastLinkerMetadataTest`, `FastSheetStructureTest`,
  `HierarchyCacheTest`, `MathLinkTest`.
- Not yet measured end-to-end with the linker removed (owner ran it after hand-off).

### 2.5 Sync = three queues (both policies)

`runner/cli.py:_cmd_sync_isolated`:

1. **Parser queue** — `ParseAhead` parses queued jobs in claim order into
   `metadata/cache/parse/<key>.md` (key = source sha256 + parser URL + previous
   markdown hash + validation flag). The build's `_parse` waits for an in-flight
   parse of the same key instead of parsing twice.
2. **Builder queue** — `work_once`, one document per git-worktree candidate, promoted
   to `last_good` on success (unchanged from production).
3. **Linker queue** — `LinkAhead` starts with every built-but-unlinked document
   (e.g. from a stopped run), then each newly promoted one; writes chunk metadata +
   Jev roles into `metadata/cache/linker/<meta_version>/`. Own model concurrency, so
   worst case in flight = 2 × `WIKI_CONCURRENCY` (owner's choice; do NOT add a shared
   semaphore).
4. The serial link phase (`link_pending_isolated`: edges, render, commit, publish) runs
   after the builder queue drains — the history model allows one promotion at a time
   and rendering writes into peer documents' pages.

Caches are untracked and content-addressed; data from the production commit simply
starts with empty caches. `settings.cache_dir` is set by sync to the live project's
`metadata/cache` (candidates are separate worktrees).

Resume semantics: finished documents survive any stop; the document being built when
the run stops is rebuilt (its LLM checkpoints live in the discarded worktree); parses
and link-ahead metadata survive. Publishing anytime: stop the sync, then
`main.py publish --allow-unlinked` (built docs, no linking) or `main.py publish`
(links pending docs first, then publishes linked ones).

### 2.6 Things that are deliberately NOT supported (do not "fix" without asking)

- `sync` cannot adopt `convert`+`build` data that was never published (e.g. old
  `data/mountdocs`): `publisher/history.py:_validate_empty_unledgered_state`. Same in
  production. The owner may ask for an adoption change later.
- Mid-document resume across a stop (would need copying the kept candidate's
  `metadata/state/<doc>`).

### 2.7 Human-capture fixes (found by the live test, uncommitted)

1. **Spelling-only multi-line hunks** — GROWI holds inline code without backticks
   (`graph/growi/client.py:_growi_markdown` strips them on publish). Two adjacent
   lines differing only in that spelling formed one N-line hunk in
   `publisher/human_changes.py:_changes`, which swallowed any human token edit inside
   → "remote transport changes cannot be canonicalized safely" → document blocked.
   Fix: N→N line hunks are token-diffed line by line (exactly like 1-line hunks).
   Tests: `TransportSpellingMergeTest` (incl. same-token still conflicts).
2. **Linker links recorded as human edits** — `HumanStore.capture` stored the human's
   whole block (with the linker's inline links) against the pure generated block, so
   the links became "human changes" and later LLM rewrites of those lines produced
   spurious conflicts. Fix: `capture(..., unlinked=_planning/pages/<page>)` rebases only
   the human's own change onto the pre-link block. Guard: used only while the local
   pure page equals the published ancestor (`generated_before`); after a failed
   publication (local rebuild newer than GROWI) the old behaviour is kept — required
   by `test_409_race_uses_the_old_baseline_not_the_write_that_never_landed`.
   Test: `test_linker_links_are_not_recorded_as_part_of_a_human_edit`.

---

## 3. How to verify (details)

### 3.1 Inherited failing tests (also fail on production `a9a7dcf`)

```
test_diff_project_config_and_docx_fixture_exist
test_incremental_linker_keeps_valid_peer_untouched_and_skips_search
test_incremental_scope_with_many_old_edges_checks_only_visible_ones
test_neo_incremental_linker_preserves_entity_direction_and_checks_only_visible_edges
test_rename_plus_edit_keeps_old_digest_until_the_new_blob_is_parsed
test_document_index_page_cards_unchanged
test_root_lists_team_names_only
test_related_documents_cached_and_rendered_for_both_docs
```
Any other failure is a regression. The handoff command in `handoff-conflicts.md` also
names `tests.test_growi_images` / `tests.test_pipeline_scope`; those modules do not
exist in this tree (loader errors, not failures).

### 3.2 Negative checks used (re-use them when touching these areas)

Each new test was confirmed to FAIL with the feature removed, e.g. by patching:
`graph.linker.meta_cache.load` → `{}` (LinkAhead), `pipeline._pending_link_rels` → `[]`
(linker queue start), `graph.clients.chat._loads` → `json.loads` (LaTeX), capture
`unlinked=None` (linker-links fix). Do the same for any new test you add.

### 3.3 compat_check caveat

`runner.compat_check` copies a root and blocks model/parser/GROWI writes. Run it on a
**fully published** root. `data_std` was never published (GROWI was down), so with GROWI
up it shows blocked `growi.update_page` calls — pending publication, not a regression.
`data_std2` is fully published → clean result.

### 3.4 Replay equivalence vs production (standard path)

```
R=$PWD; rm -rf /tmp/rp-base /tmp/rp-now; mkdir /tmp/rp-base /tmp/rp-now
git -C .. worktree add --detach /tmp/base a9a7dcf
(cd /tmp/base/llm-wiki-air && PYTHONPATH=. $R/.venv/bin/python $R/tools/replay_equivalence.py \
   $R/data_std/mountdocs/raw /tmp/rp-base /tmp/base.json $R/data_std/mountdocs)
PYTHONPATH=. .venv/bin/python tools/replay_equivalence.py data_std/mountdocs/raw /tmp/rp-now /tmp/now.json data_std/mountdocs
.venv/bin/python tools/replay_equivalence.py --compare /tmp/base.json /tmp/now.json
git -C .. worktree remove --force /tmp/base
```
(run from `llm-wiki-air/`). All lines must be `OK`.
pdf/xls/pptx stop at planning with the fake model in both trees (same error) — the
comparison still covers prompts up to that point; edge judging/curation are not
exercised (no edges with a fake model).

### 3.5 Real-endpoint baselines (same 5 mount-docs documents)

| Run | Total | Notes |
|---|---|---|
| standard `data_std` | 44 m 17 s | before today's changes |
| fast `data_fast` | 33 m 59 s | first fast version |
| fast `data_fast3` (rewrite+judge, page merge, 12k planner) | 29 m 29 s | writer 476 s vs 792 s std; linker 717 s |
| linker-only with prefetch on `data_fast3` copy | 577 s | vs 717 s sequential |
| standard `data_std2` with 3 queues | parse 100% hidden; linker entities ≈1–3 s per doc in link phase | link-ahead did the work during builds |

Server: `vllm` at `162.43.170.108:27027` (`/metrics` shows running/waiting); ~55 tok/s
single stream, ~150 tok/s at 3–4 in flight.

---

## 4. Data folders and configs

| Path | What |
|---|---|
| `data_std/` | standard build of mount-docs, never published |
| `data_std2/` | standard build, **published to `localhost:3000/mountdocs`**, used by the human test; has the hc test documents (§5) |
| `data_fast*/` | fast experiments (`data_fast3` complete; `data_fast3_link` linker-only rerun) |
| `data/mount` | the real test mount (untouched) |
| `data/mount-hc` | copy of the mount + human-test documents (`hc-test2.md`) |
| `configs/mount-docs.ini` | original project (has the bot token) |
| `configs/mount-docs-fast.ini` | same mount, target `mountdocs-fast`, no token |
| `configs/mount-docs-hc.ini` | `data_std2` fed from `data/mount-hc`, target `mountdocs`, no token |
| `hc_round*.log`, `hc2_round0.log` | live human-test run logs |

GROWI: local dev instance `http://localhost:3000` (app + `devcontainer-mongo-1`).
GROWI write path is always `/{target_name}` (`publisher/pipeline.py:_connection`).

---

## 5. Human-edit (GROWI) live testing — how to continue

### 5.1 Setup

```
cd llm-wiki-air
export GROWI_TOKEN="$(sed -n 's/^growi_token *= *//p' configs/mount-docs.ini)"   # bot (publisher)
export WIKI_HUMAN_SYNC_MODE=apply                                                  # default is off!
export HUMAN_TOKEN=<human editor token from the owner>                             # tools/hc_live/human.py
export HC_DOC=hc-test2.md
hc_sync() { .venv/bin/python main.py sync --project mount-docs-hc --data-root data_std2 --timeout 900; }
human() { .venv/bin/python tools/hc_live/human.py "$@"; }          # pages | show | replace | insert | delete
source_round() { .venv/bin/python tools/hc_live/source.py "$1"; }   # writes data/mount-hc/$HC_DOC
```
`tools/hc_live/make_doc.py` builds the Japanese test document (3 chapters × 3
sections, distinctive numbers); 20 filler lines per section → 2 pages (ch1+ch2, ch3).
Predict the tier before syncing:
```
.venv/bin/python - <<'EOF'
from pathlib import Path
from graph.wiki.incremental import decide_update
state = Path("data_std2/mountdocs/metadata/state/hc-test2.md")
d = decide_update(state, (state/"source"/"original.md").read_text(), Path("data/mount-hc/hc-test2.md").read_text(), kind="md")
print(d.tier, d.reason, sorted(d.regenerate), d.churn)
EOF
```
Tier rules (`graph/wiki/incremental.py`): page churn ≤ 20 % → patch (Tier 1);
> 20 % → regenerate page (Tier 2); > 50 % of pages, plan mismatch, page-too-large,
or heading-shape change (moving/deleting a section of a heading-planned doc) → Tier 3.

Inspect results: `human show <page>` (GROWI body), `metadata/human-sync/documents/*.json`
(record `status`: active/absorbed/conflict/orphaned/deleted; `base_before_blob`,
`human_after_blob`, `human_delta`), pages `98-Human-Conflicts.md`,
`99-Retained-Human-Notes.md`, and `main.py human status`.

### 5.2 State at hand-off

- `hc-test.md` (first document) was used for rounds 0–2 and then **deleted from the
  mount** (source-delete case). Its journal is archived (`archived: true`, all 6
  records kept) and its GROWI pages were removed — matches the design.
- `hc-test2.md` was added to `data/mount-hc` (round-0 content). Its round-0 sync was
  **stopped mid-write** (GPU shutdown). The next `sync` recovers the stale transaction
  (fix in §2.3) and builds/publishes it. Then run rounds 1–4 below on it — with the
  §2.7 fixes in place, which rounds 1–2 of `hc-test.md` did not fully have.

### 5.3 Results so far (`hc-test.md`)

| # | Case (handoff table) | Round | Result |
|---|---|---|---|
| 1 | update does not touch the human block | R1 Tier 1 | ✅ kept exactly |
| 2 | same change both sides | R1 | ✅ one copy, record `absorbed` |
| 3 | source changes another part of the block | R1 | ✅ combined (human “夏季は 16 °C” + source 28 °C) |
| 4 | same fact changed differently | R1 | ✅ human 65 °C primary, “Updated source document says … 55 °C”, listed in `98-Human-Conflicts` with actions |
| 5 | human adds new information | R1 | ✅ kept |
| 7 | Tier 2 regenerates the page | R2 Tier 2 | ⚠️ E-41 note reapplied (active); FL-9 edit **orphaned** → `99-Retained-Human-Notes` (gemma restructured the H2 `## 保守` into `## 定期点検`/`## 部品交換`) |
| — | source document deleted | after R2 | ✅ journal archived, pages removed |
| — | page-1 R1 edits after R2 | R2 | ⚠️ became `conflict`: cause = linker links recorded as human edits → **fixed** (§2.7 #2), not yet re-tested live |
| — | absorbed R1 edit after R2 | R2 | ⚠️ status became `deleted` (undiagnosed — see §5.5) |
| 6, 8, 9, 10, 11, 12, 13 | | | not yet run |

### 5.4 Rounds to run on `hc-test2.md` (edit `tools/hc_live/source.py:facts()` per round)

- **R0′**: `hc_sync` (builds + publishes 2 pages). `human pages` to get paths.
- **R1′ Tier 1** — human: insert after `60,000 時間` line; insert after `FW 3.2.1`
  line; `18 °C …します。` → `…します（夏季は 16 °C）。`; `高温警報: 60 °C` → `65 °C`;
  page 2 `3 か月ごとに実施します。` → `2 か月…`. Source (`source_round 1`): 26→28 °C,
  60→55 °C, 3→2 か月. Expect cases 1–5 as in §5.3.
- **R2′ Tier 2** — human: insert note after the `E-41` line; FL-9 line + `（予備は常時
  2 個保管）`. Source: rewrite C1 and C3 procedure lines (`source_round 2`, NEW_FILLER).
  Expect: page 2 regenerated, notes reapplied; **page-1 R1′ edits must stay `active`**
  (this verifies §2.7 #2 live).
- **R3′ Tier 3** — source: move section A3 (設置環境) into chapter 3, delete C2 (部品交換),
  add the human 【人間メモ】 sentence to A1 (case 10). Human, before syncing: edit A3
  (`800 kg 以上` + `（2024 年に再測定済み）`, case 6), delete their own W-3 paragraph
  (`human delete <page> "CB-7 の予備品は倉庫 W-3 に保管されている。"`, case 11).
  Implement in `source.py` with `build(..., sections={"A": ["A1","A2"], "B": [...],
  "C": ["A3","C1","C3"]})`. Expect: Tier 3 (heading shape changed), A3 edit moves to
  page 2 (6), C2 edit → retained notes (9), A1 absorbed (10), W-3 not back (11), all
  other active/conflict records reapplied after full rebuild (8).
- **R4′** — source: remove the absorbed sentence again → human note reactivated (12);
  W-3 stays deleted (11 durable).
- **Extra cases** (handoff "Required tests"): Tier 0 (human edit, no source change →
  `main.py pull` / sync renders it, no writer); human deletes generated text
  (suppression must persist across later syncs); repeated updates (no duplicated human
  text after 3+ rounds); operator actions via `main.py human resolve`
  (`keep-human|accept-source|combine|suppress|retry-match`) and editing the conflict
  block in GROWI (keep human / accept source); GROWI page deleted or moved by a person
  (must block and report, never recreate); damaged ownership marker
  (`<!-- llm-wiki-bot-ref:… -->` removed → block, save body); late human edit during a
  publish (409 path); stop mid-publish then continue (prepared-body recovery).
- Case 13 (“cannot decide safely”) cannot be forced reliably; record it if it appears
  (expected rendering = human primary + bracketed source + conflict list).

### 5.5 Open issues found (with diagnosis and what to try)

1. **Tier-2 regeneration orphans edits when gemma renames/splits H2 sections.**
   Records are whole H2 blocks; `HumanStore._match` needs exact text, same heading +
   neighbour hash, or ≥ 0.5 similarity under the same heading. A regenerated page with
   new H2 names leaves no candidate → `orphaned` (safe: retained notes). Options:
   (a) when the old heading vanished, try `related()`-style similarity across all new
   blocks of the same page and accept a unique best ≥ threshold; (b) anchor on the
   human delta's surrounding sentences instead of the whole block; (c) use the existing
   Jev same-topic scorer (observe mode only today). Keep “uncertain → retained notes”.
2. **Absorbed record turned `deleted` after Tier 2.** Not diagnosed. `merge()` returns
   `"deleted"` when `human == base`; an absorbed record rebased on a regenerated base
   may hit that path. Deleted (tombstoned) records never reactivate, which would break
   case 12. Find where render/rebase writes `status` for absorbed records and keep
   `absorbed` (not `deleted`) unless a human removed it.
3. **Spurious conflicts from LLM rewording.** Even with §2.7 #2, a human edit in a
   block that gemma rewrites (Tier 2, or a Tier-1 reference patch from research facts)
   overlaps the rewrite → conflict rendering of the whole H2 block (blockquote with the
   complete new block). Safe but noisy. Options: line-level instead of block-level
   conflict rendering; treat pure human insertions (new lines) as re-insertable after
   the nearest unchanged anchor line.
4. **Transport differences are a general hazard.** Any new publish-time transform
   (like `_growi_markdown` stripping backticks or permalink rewriting) must be either
   inverted in capture or diffed token-wise; otherwise human edits next to it block the
   document. If a new “cannot be canonicalized safely” appears: diff
   `remote_blob` vs `local_blob` of the page (`HumanStore.page(marker)`), then
   instrument `merge(editable(prev_remote), editable(now_remote), editable(prev_local))`
   and print the overlapping `(_changes)` pair (that is how §2.7 #1 was found).

### 5.6 If X happens → do Y

| Symptom | Likely cause | Do |
|---|---|---|
| `cannot recover publication op-…: last-good changed` | a `publishing` op interrupted, then last-good moved | genuine: run `main.py human status`, inspect `transactions` in `metadata/watch-queue.sqlite`; never delete a `publishing` row blindly. For `building/prepared` this no longer happens (§2.3). |
| `remote transport changes cannot be canonicalized safely` | publish-time spelling vs local spelling around a human edit | §5.5 #4 |
| `remote difference requires human_sync_mode=apply` | mode is `off` (default) | `export WIKI_HUMAN_SYNC_MODE=apply` for the test |
| human edit missing after a sync | check record status: `orphaned` → retained notes page; `deleted` → tombstone (was it really the human?); `conflict` → conflict block | §5.5 #1/#2 |
| conflict shows links or backticks as differences | derived/transport content in the record | §2.7 #2 guard (published ancestor ≠ local pure page?) |
| `GROWI destination is not owned by this page` / `no inspected published baseline` | two data roots publishing the same `target_name` | give each experiment its own config/target (see `mount-docs-fast.ini`) |
| `publisher already running` | another sync/publish holds `metadata/pipeline.lock` | wait; only one writer at a time by design |
| `cannot create Git baseline … without pipeline.json` | `convert`/`build` data never published | §2.6 (unsupported; ask the owner) |
| linker `JSONDecodeError: Invalid \escape` | LaTeX in model JSON | fixed in `extract_json_from_text`; if seen, the repaired parse also failed — inspect the raw output in the artifact dir |
| fast run slower than expected | planner truncation (check `observations/checkpoints/*/attempt-*-error.txt`), page over-division, false identifier retries (`page-*/section-*-attempt-02-prompt.md` feedback) | see §2.4 knobs |
| new test passes but you are unsure it tests anything | — | negative-check it (§3.2) |

---

## 6. Continue here

1. Run §0. Commit (owner's go-ahead), including `git add -f tests/test_phase_compat.py`.
2. `hc_sync` once (recovers the stopped R0′, publishes `hc-test2.md`).
3. Run R1′–R4′ and the extra cases (§5.4), filling the table in §5.3.
4. Fix §5.5 #2 first (it can break case 12), then #1 and #3, each with a failing test
   first and the full human suites green afterwards
   (`tests.test_human_changes tests.test_human_sync_rollout tests.test_update_tiers`).
5. Re-run §0 and the replay harness after every change in `graph/` or `publisher/`.
