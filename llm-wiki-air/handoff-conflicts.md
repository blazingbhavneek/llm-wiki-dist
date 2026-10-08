# Human edits and conflicts: current-code handoff for private-data validation

Last rewritten: 2026-10-08

Code snapshot reviewed: `a65cca8` plus the uncommitted fixes described below

Audience: a local agent/operator that can read the private source subset and the
matching disposable GROWI test subtree

This document replaces the old design diary. It describes the code that exists
now, the behavior already covered by focused offline tests, and the live work
that still has to be performed against representative documents. It is not a
claim that the live matrix has passed.

## 1. Hard safety rules for the local agent

1. Use a new absolute INI file for the test. Do not edit, delete, rename, copy,
   or sanitize any existing file in `configs/`. Existing configs point at real
   projects.
2. Use a source mount containing only the approved subset. Use a new empty
   `data_root` and a unique one-component `target_name` that maps to a disposable
   GROWI subtree.
3. Use a GROWI credential whose permissions are limited to the test instance or
   disposable subtree. Keep the publisher credential and any human-editor test
   credential out of the repository and out of reports.
4. Start with `human_sync_mode=off`, then validate `observe`, and enable `apply`
   only for the explicitly approved disposable subtree.
5. Before every write/delete/move fault-injection case, independently check the
   endpoint and resolved GROWI boundary. `human live-plan` creates a report and a
   confirmation code, but normal `sync`, `publish`, and `reset` commands do not
   consume that code. The report is an operator gate, not a universal runtime
   interlock.
6. Stop immediately on a missing/duplicated human marker, unexpected overwrite,
   unexpected delete/move, corrupt snapshot, unexplained revision mismatch,
   cursor gap, or a non-idempotent retry. Preserve the data root and the remote
   page revisions for investigation.
7. Do not run the full historical test suite as the first check. The focused
   commands in section 12 are the maintained checks for this feature and finish
   quickly. Expand testing only after the focused checks pass.
8. Do not put private page bodies, source text, tokens, or authenticated URLs in
   this handoff, a Git commit message, or a live report. Store hashes, revision
   IDs, page IDs, edit IDs, reason codes, and redacted paths instead.

## 2. Current status and this change set

The human-overlay implementation is substantial and active in the normal
pipeline. It is not a stub. The current implementation has focused offline
coverage for capture, deterministic merging, conflict display, retained notes,
source tiers 0-3, publication races, partial publication, activity detection,
semantic observe-only proposals, operator resolution, legacy recovery, and Git
candidate/last-good behavior.

Live validation on current private documents is still pending. In particular,
the current code has not been certified against the target GROWI version's
exact Markdown transport behavior, activity permissions, attachment behavior,
or representative private section rewrites.

This working change set makes two code corrections:

- `graph/workspace/writer.py`: an injected structured model is now used without
  eagerly building production endpoint configuration. The eager construction
  was introduced during the latest phase/configuration work and broke the
  human-overlay tier integration path when the caller supplied a narrow settings
  object and a complete model port.
- `runner/cli.py`: every `human` operator command now runs under the publisher
  lock, requires a clean `refs/llm-wiki/last-good` working tree, audits snapshot
  references, and checkpoints successful durable changes. Previously `human
  status`, `resolve`, `recover-legacy`, and `live-plan` could dirty tracked
  human-sync state and cause the next `sync` or `pull` to reject the project.
- `tests/test_human_sync_rollout.py`: a regression test verifies the operator
  checkpoint, idempotent status, and dirty-tree rejection behavior.

No project configuration file is part of this change set.

Focused verification performed on 2026-10-08:

- `compileall` over `common convert wiki linker index publisher runner graph`:
  passed;
- `tests.test_human_changes`: 63 tests passed in about 20 seconds;
- `tests.test_human_sync_rollout`: 25 tests passed in about 6 seconds;
- `tests.test_update_tiers`: 34 tests passed in about 3 seconds;
- fresh CLI `human live-plan` on a temporary empty project created a report,
  checkpointed it, and left `HEAD == refs/llm-wiki/last-good`.

The core suite deliberately logs injected 409, 500, lost-response, and
post-write failures while testing recovery. Those log lines are expected when
the unittest result is `OK`. No real GROWI/model/parser service was contacted by
these checks.

## 3. Architecture as it exists now

The public architecture has five phase packages:

```
convert -> wiki -> linker -> index -> publisher
                 ^                 |
                 |                 v
              runner --------> GROWI
```

`runner/` owns orchestration. `common/` owns shared paths, typed views of the
legacy settings, context, storage-neutral policy hooks, and markers. The folder
split is still a compatibility migration rather than a full internal rewrite:

- `runner/cli.py` is the command adapter.
- `runner/steps.py` composes phase entry points for local build operations.
- `common/legacy.py` still delegates several phase calls to the established
  implementation.
- `publisher/phase.py` is a thin public facade. Its `assemble` action only fills
  missing final pages conservatively.
- The production wiki writer is still `graph/workspace/writer.py`.
- The production GROWI client/publisher is still `graph/growi/client.py`.
- Human overlay ownership is in `publisher/human_changes.py`.
- Sync, pull, isolated candidate promotion, and publish ordering are in
  `publisher/pipeline.py`.

Do not move the human logic merely to make directory names look cleaner during
this validation. The frozen data contract and behavior matter more than
finishing the folder migration.

### 3.1 Actual standard sync order

For a document job that reaches `publisher.pipeline.sync_once()`, the important
order is:

1. Acquire `metadata/pipeline.lock`.
2. If the project already has Git history, require `HEAD` and durable paths to
   match `refs/llm-wiki/last-good`.
3. Scan the source mount and load `metadata/pipeline.json`.
4. Fetch/capture authoritative remote revisions before parsing or changing the
   generated pages.
5. Checkpoint a blocked remote capture when necessary so the project is not
   left dirty and unrecoverable.
6. Convert changed sources to `raw/`.
7. Update wiki generator state with tier 0, 1, 2, or 3.
8. Save the pure generated pages in the human store and render the human overlay
   into the final `wiki/` pages.
9. Link pending pages. Linker output is derived output, not human intent.
10. Preflight every page in the publication batch before the first remote write.
11. Publish serially, recording prepared and confirmed evidence per page.
12. Update ledger snapshots and checkpoint the live project/last-good ref.

#### Important current gap: an idle one-shot `sync` does not always pull GROWI

The ordering above is true only after the queue has claimed a source job.
Default CLI `sync` is queue-driven. If mount scanning finds no added, changed,
moved, deleted, failed, or forced source job, `work_once()` returns without
calling `sync_once()`. In that idle path, a one-shot `sync` currently does not
call `pull_growi_once()`. Therefore a human-only GROWI UI revision can remain
undetected when the source mount is unchanged.

Current behavior by entry point:

- A queued source update, move, or delete captures the relevant published pages
  before changing generated state or remote pages.
- `watch` calls `pull_growi_once()` while idle at `--growi-interval` (300 seconds
  by default; `0` disables it).
- Explicit `pull` performs remote reconciliation without requiring a source
  change. `pull --inventory` is the correctness-first manual check when activity
  delivery or permissions are uncertain.
- `pull_growi_once()` already writes the operator summary. `human status` is an
  explicit display/recheck command, not a prerequisite for capture.
- A no-source-change, one-shot default `sync` can currently miss a UI-only edit.

Until case `idle_sync_remote_reconciliation` in Group H is implemented, use two
separate commands and inspect the first command's exit/result before continuing:

```bash
.venv/bin/python main.py --project "$CFG" pull --inventory
.venv/bin/python main.py --project "$CFG" sync
```

In `off` or `observe`, a detected difference is expected to block the pull; do
not continue to sync as though it succeeded. In `apply`, the pull captures the
revision, writes the summary, and marks affected linking pending. The following
sync can then link/publish that protected state.

The intended final behavior is that top-level one-shot `sync` performs a remote
reconciliation even when the source queue is empty, blocks only the documents
whose capture is blocked (other documents keep syncing), processes pending
overlay/link work, and refreshes operator counts. Per-document capture inside
`sync_once()` must remain as a later revision-safety check; adding the top-level
pull is not a reason to remove it.

`publisher/ahead.py` may prepare content-addressed parse or linker metadata
caches in the background. It must not perform remote or durable project writes;
those remain in the serial loop.

Default `sync` uses isolated candidate processing. The legacy batch path is
available with `--no-isolated`, but live validation should exercise the default
first because that is the intended deployment path.

### 3.2 Authority model

There are three distinct authorities:

- Source documents own generated facts.
- Human revisions on publisher-owned GROWI pages own captured human intent, but
  only in `human_sync_mode=apply`.
- The generator/linker owns derived presentation, links, indexes, ownership
  stamps, and other managed output.

The implementation never treats the current final wiki page as a new pure
generator base. That page may already contain human spans and linker output.

Useful names when reading the code:

- `G0`: pure generated text corresponding to the last accepted publication.
- `P`: effective published local text (generated text plus human overlay and
  derived publication formatting).
- `R`: newly fetched remote GROWI text.
- `G1`: pure generated text after a later source change.
- `E1`: final local text produced by deterministically applying human intent to
  `G1`.

The pull path captures `P -> R`, records human intent against a verified pure
ancestor, and the render path later computes `G1 + intent -> E1`.

## 4. Project paths and durable records

For settings with `data_root=/absolute/data` and `target_name=hc-subset`, the
project root is `/absolute/data/hc-subset`. The external source mount is not
copied into that directory as `mount/`; `Project.mount` continues to reference
the configured absolute `source_mount`.

The relevant project tree is:

```
<project-root>/
  .git/
  sources/                         immutable source blobs used by project history
  raw/                             converted Markdown
  wiki/<document>/                 final effective pages sent to GROWI
    *.md
    _planning/source.json          raw path, source hash, stable identity seed
    _planning/manifest.json        page/source range information
    _planning/linker.json          pending|complete|failed|disabled
    _planning/pages/*.md           pre-link effective pages
  metadata/
    pipeline.json                  source and published page ledger
    source-identities.json         stable active IDs and tombstones
    state/<document>/              pure generator state and pure wiki output
    human-sync/
      schema.json
      documents/*.json             per-source human journals
      pages/*.json                 per-page publication/baseline evidence
      snapshots/<sha256>.md        immutable content-addressed text blobs
      observations/*.json          deterministic observe-mode records
      events/*.json                redacted decision/reason records
      semantic/cache/*.json
      semantic/cache-index/*.json
      semantic/observations/*.json
      activity-cursor.json
      operator-summary.json
      operator-summary.md
      live-reports/*.json
```

The frozen contract applies. Do not rename paths, JSON keys, marker strings,
schema versions, IDs, refs, prompt versions, or GROWI paths during validation.
Additive evidence is acceptable; bulk migration is not.

### 4.1 Git and last-good

`publisher/history.py` tracks these durable paths:

```
.gitignore
sources/
raw/
wiki/
metadata/state/
metadata/pipeline.json
metadata/source-identities.json
metadata/human-sync/
```

`refs/llm-wiki/last-good` is the accepted live state. Candidate worktrees start
from that ref. Human journals and snapshots are deliberately part of candidate
commit, promotion, rollback, and live checkpoint behavior.

The human CLI fix in this change set makes operator commands follow the same
rule. A command refuses to run if `HEAD` is not last-good or one of the durable
paths is dirty. It checkpoints successful changes and advances last-good.

Do not fix a dirty-tree refusal with `git reset --hard`. First identify why it
is dirty. A partial publication, blocked capture, or interrupted operator action
can contain evidence needed to recover a human edit.

### 4.2 Human document journals

`HumanStore.identity()` prefers the stable `source_id` recorded in
`pipeline.json` and the source planning stamp. Older journals keyed by the
legacy identity seed are lazily aliased/migrated when that document is touched.
A reused mount path with a new `source_id` must not inherit the previous
document's edits.

A document journal contains, among other fields:

- schema version, journal key, `source_id`, legacy IDs, raw path, and document
  identity seed;
- current pure page blob references;
- the source digest corresponding to those pages;
- edit records;
- captured revision keys and capture history;
- overlay page names and archive/recovery state.

Each edit has a stable `hedit-...` ID, operation, status, anchor, pure base blob,
human-after blob, exact human delta blobs, revision history, current target,
conflict evidence, and resolution history.

### 4.3 Page baseline records

The page record keyed by the ownership marker stores enough evidence to
distinguish bot and human revisions:

- local path, GROWI page ID/path, source identity/digest;
- accepted, observed, and published revisions;
- exact remote, effective-local, and pure-generated snapshot blobs;
- block reason and observed blocked body when capture is unsafe;
- prepared publication attempt ID, inspected page/revision/path, exact prepared
  remote/local/generated blobs, confirmation, error, and attempt history;
- legacy pin and deletion evidence when applicable.

Every `*_blob` reference is checked against a SHA-256-named file. `audit()`
fails closed when a record or referenced blob is missing/corrupt.

### 4.4 Managed markers

Human regions are visible Markdown comments:

```markdown
<!-- llm-wiki-human:hedit-<id>:start -->
human-preserved text
<!-- llm-wiki-human:hedit-<id>:end -->
```

A deterministic conflict places the updated source candidate inside the human
region:

```markdown
<!-- llm-wiki-source:hedit-<id>:start -->
source candidate text
<!-- llm-wiki-source:hedit-<id>:end -->
```

Marker parsing is fence-aware so an example inside a fenced code block is not
treated as an active marker. Duplicate, nested, partial, unbalanced, or unknown
active markers fail closed. Publication/link transforms use `map_generated()`
so protected human regions are not rewritten while generated portions are
normalized.

## 5. How capture and rendering work

### 5.1 Establishing pure generated state

After the writer exports a document, `apply_generated()` records the freshly
generated `*.md` pages before any human overlay or linking, then calls
`HumanStore.render()`.

If a legacy document has no journal yet, `ensure_generated()` accepts existing
generator state only when:

- page sidecars do not say `human_edited`;
- pure state pages exist;
- every page sidecar hash exactly matches the pure page bytes.

Otherwise it raises `LegacyBaseUnavailable`. The system pins the complete human
page instead of guessing a base.

### 5.2 Pulling a remote revision

`GrowiPublisher.pull_changes()` audits the store, fetches known pages by page
ID, and checks:

- the page still exists and is not trashed;
- page ID and expected path still match;
- exactly the expected ownership marker is present;
- source identity still matches;
- ledger revision, accepted baseline, and observed revision are coherent;
- any prepared publication outcome can be proven.

Remote Markdown is canonicalized against the exact prior remote and local
snapshots. This restores link/image transport spelling without inventing text.
If transport differences overlap ambiguously, capture blocks.

The initial baseline for an old ledger is conservative. An unchanged remote
revision can establish the baseline. A changed old page without a verified pure
ancestor becomes `legacy_pinned` in apply mode; off/observe modes block it.

### 5.3 Capturing block intent

`HumanStore.capture()` removes only explicitly managed derived regions, then
splits the old and remote pages into structural blocks. It uses
`graph.linker.chunks.split_page`; fenced code, Markdown/HTML tables, and images
are atomic ranges.

Changed blocks become durable edit records. Token-level differences are used
for equal-length rewritten lines so a human number/spelling edit is not swallowed
by a larger multi-line publication-format hunk.

When `_planning/pages/<page>.md` corresponds to the published pure generation,
capture uses it to remove linker-only changes. Links added by the linker must
not become human intent. If local generation has moved past the published
generation, this shortcut is disabled and the code keeps the larger safe human
block.

An already captured `marker:before-revision:observed-revision` key is a no-op,
which makes repeated pulls idempotent.

### 5.4 Deterministic reattachment

Rendering builds candidates from all current pure pages in the document. The
matching order is deliberately conservative:

1. A unique candidate exactly equals the old pure block or human block.
2. Otherwise require the same heading and a unique related candidate.
3. Relatedness requires one of: same title/page, a stored neighbor hash, or
   lexical continuity of at least 0.5 after heading text is excluded.
4. A same-page candidate is allowed only when it is unique and passes the same
   relatedness check.
5. No unambiguous target means `orphaned`; the full human text goes to a retained
   notes page.

There is no model-authoritative reattachment in apply mode.

### 5.5 Three-way merge outcomes

`merge(base, human, source)` is exact and deterministic:

| Condition | Result |
|---|---|
| human equals base | use source; edit is absorbed/no-op |
| source equals human | use source; status `absorbed` |
| source equals base | preserve human; status `active` |
| human changes are a subset of source changes | source already contains intent; `absorbed` |
| human and source edits are disjoint | combine both verbatim; `active` |
| edits overlap or touch the same atomic unit | keep human plus source candidate; `conflict` |

When a source later regresses after having absorbed the human fact, the journal
remains available and can become active again. `absorbed` is not a destructive
tombstone.

Every non-deleted/non-absorbed edit ID must appear exactly once across the
rendered pages. Missing or duplicate coverage raises an error.

### 5.6 Conflict and retained-note pages

Default auxiliary names are:

- `98-Human-Conflicts.md`
- `99-Retained-Human-Notes.md`

If a source page already uses one of those names, rendering chooses a numbered
variant instead of overwriting source output. The conflict page is an operator
dashboard with IDs, statuses, source IDs, revisions, match reasons, and allowed
actions. The retained page contains the complete human content that could not be
reattached safely.

Any overlay change writes both the final page and its `_planning/pages` copy and
marks the linker pending unless linking is disabled. Stale auxiliary overlay
pages are removed only after the current effective set is known.

## 6. Rollout modes

`human_sync_mode` accepts only `off`, `observe`, or `apply`. Default is `off`.

| Mode | Fetch/classify | Store redacted observation | Run semantic proposal | Accept remote difference |
|---|---:|---:|---:|---:|
| `off` | yes | event only | no | no; block |
| `observe` | yes | yes | yes, observe-only | no; block |
| `apply` | yes | deterministic capture evidence | no | yes when all checks pass |

An INI value in `[settings]` overrides the environment variable. Therefore an
existing `human_sync_mode=off` in the INI cannot be changed by exporting
`WIKI_HUMAN_SYNC_MODE=apply`. Change only the disposable test INI deliberately.

Related settings:

```ini
[settings]
human_sync_mode=off
human_sync_activity_audit_seconds=3600
human_sync_activity_overlap_seconds=60
```

The runtime always constructs `HumanSyncPolicy` from settings. A direct
`GrowiPublisher(...)` without a policy defaults to apply only for compatibility
with deterministic unit callers; that is not the CLI default.

### 6.1 Semantic observe-only path

`publisher/human_semantic.py` is advisory only and is built only in `observe`:

1. Deterministic shortlist, bounded to the current source identity and
   structurally plausible candidates.
2. Jev scorer.
3. Typed writer response.
4. Mechanical validation for protected spans, markers, fences, structured
   units, invented content, unrelated candidate leakage, and output size.
5. Independent typed judge.
6. At most two writer attempts.
7. Content/version/policy-keyed cache and redacted metrics.
8. Exact deterministic conflict fallback on any model, schema, transport,
   validation, judge, or budget failure.

Current runtime pull supplies only the current generated page as the semantic
candidate. It does not yet perform a document-wide semantic relocation search,
and a semantic proposal never changes apply-mode output. Do not describe it as
an automatic conflict resolver.

In standard policy, writer and judge are separate `ChatModelPort` instances
using the standard chat configuration. Fast policy can select its explicit
writer/judge role pair. This distinction should be checked if observe-mode model
traffic is part of the private test.

## 7. Publication, races, and lost responses

### 7.1 Ownership and preflight

GROWI pages carry publisher ownership stamps. Before a batch write,
`publish_pages()` fetches every destination and validates the whole batch:

- expected page exists when it should;
- page ID, path, revision, and ownership marker match;
- a pre-existing new destination is owned and reconcilable;
- no two local pages map to the same remote path.

Only after all pages pass preflight does the serial write loop begin. A foreign
destination is never adopted merely because its path matches.

Updates send `origin: "view"` and the inspected revision ID. HTTP 409 records a
revision-race event and aborts; there is no blind second PUT. Deletes inspect
ownership and expected revisions before trashing pages.

### 7.2 Prepared evidence

Immediately before each create/update, the store records an attempt ID and exact
prepared remote/local/pure-generated blobs. Immediately after a response, it
records page-level confirmation before proceeding to another page.

On restart:

- exact body match proves a lost response was the bot's write and adopts it;
- unchanged inspected revision proves the write did not land and retires it;
- a later human revision can be rebased only with the exact confirmation chain;
- ambiguous outcomes block instead of becoming human intent or bot intent.

A partially failed `publish` is checkpointed because some pages may already be
remote and their attempt evidence must be available to the next pull.

### 7.3 Candidate rollback

If generation/publication fails, rollback must restore old generated state but
retain newly captured human journals. `import_captured()` imports immutable
snapshots and edit/capture history without promoting the candidate's new
generated pages as the old pure base.

## 8. Activity detection and inventory

One-shot `pull` uses `ActivityDetector` as a hint layer. Normal full sync capture
can inspect the relevant ledger pages directly.

The activity cursor is endpoint-and-boundary keyed and stores the last timestamp,
all IDs at that timestamp, recent IDs, and optional sequence. Polling uses an
overlap window, paginates, deduplicates, and selects owned page IDs. These
conditions force a correctness-first inventory rather than cursor advancement:

- endpoint changed;
- malformed/missing cursor;
- sequence or pagination gap;
- cursor too old;
- clock skew;
- malformed activity response;
- activity API unavailable or permission gap;
- unknown page IDs;
- periodic audit is due;
- `pull --inventory` was requested.

Inventory classifies unchanged/changed/moved/deleted owned pages, newly
discovered stamped pages, unmanaged foreign pages, ambiguous pages, and
duplicate ownership markers. It rejects any result outside the configured
boundary. Newly discovered owned pages require exact prepared-publication
evidence; a marker alone is insufficient.

The cursor is committed only after durable classification/checkpoint succeeds.
If checkpointing fails, the prior cursor is restored.

## 9. Expected behavior by scenario

Use this table while interpreting both offline and live results.

Rows describe what the reader of the wiki must see, not how the code gets
there. Rows marked *current code fails* are known gaps covered by Group H.

| Scenario | Expected behavior |
|---|---|
| remote revision unchanged | no journal change |
| UI-only revision, one-shot `sync` with no source job | currently missed; run `pull --inventory` first (Group H `idle_sync_remote_reconciliation`) |
| remote differs in `off`/`observe` | that document blocks, nothing is accepted, command reports failure; `observe` also stores redacted evidence |
| same blocked revision after switching to `apply` | refetched and captured once |
| human-only add/replace/delete | one durable edit, rendered exactly once, where the human put it (*current code fails for a new or renamed section: it moves to the page bottom*) |
| human and source change different things | both changes present |
| human and source change the same thing differently (prose, table, code, image) | both versions visible; neither silently wins |
| human and source make the same change | one copy, no conflict |
| human and source delete the same thing | absent everywhere, never resurrected (*current code fails for a whole section: deleted text reappears in retained notes*) |
| human deletes, source modifies | visible conflict |
| human modifies, source deletes the text/section/page | human text stays visible (conflict or retained notes), never dropped |
| several remote revisions before one pull | latest net intent captured once |
| source moves/renames/splits content | edit follows when the target is clear; otherwise retained visibly, never attached to the wrong topic |
| linker-only or transport-only difference (including an editor re-save) | not human intent |
| remote page moved/deleted, marker damaged/duplicated, foreign page at destination | blocked; no recreate, overwrite, or adoption |
| revision changes after inspection | 409/changed-revision block, no blind retry |
| lost create/update response | exact prepared body: adopt as bot output; anything else: block |
| legacy page without a provable pure base | whole page pinned, no guessed merge |
| source identity changes at the same path | old journal not inherited |

## 10. Operator commands and recovery workflow

All examples assume:

```bash
CFG=/absolute/path/to/disposable-hc.ini
PY=.venv/bin/python
```

The `--project` selector may be a config name or an absolute INI. Use the
absolute disposable INI during private validation so there is no ambiguity.

### 10.1 Pull and inspect

```bash
$PY main.py --project "$CFG" pull
$PY main.py --project "$CFG" pull --inventory
$PY main.py --project "$CFG" human status
```

`pull` captures/classifies only; it does not regenerate or publish. `--inventory`
forces the full read-only boundary comparison before selected pages are passed
through the normal capture path.

`human status` writes and prints a redacted project summary. It includes paths,
IDs, statuses, revisions, and actions, not protected page bodies. With the
current fix it is locked and checkpointed, so the next sync remains usable.

### 10.2 Resolve one edit

Always run `pull` immediately before resolving and use the revision printed by
the latest summary. The resolver checks that revision against durable observed
state and checks local marker cardinality. It does not itself fetch GROWI again
and does not write GROWI. The later publication preflight is the final live
revision check.

```bash
$PY main.py --project "$CFG" human resolve EDIT_ID \
  --action keep-human --revision REVISION --document RAW_REL

$PY main.py --project "$CFG" build link RAW_REL
$PY main.py --project "$CFG" publish
```

Actions:

- `keep-human`: preserve the human side. For a conflict, remember the exact
  source candidate already rejected so a rerender does not immediately recreate
  the same conflict.
- `accept-source`: tombstone this edit and let current source output stand.
- `combine`: replace the human-after value with the non-empty UTF-8 contents of
  `--text-file`, then rerender.
- `suppress`: tombstone the edit. Use for an intentional suppression/deletion.
- `delete`: compatibility synonym for tombstoning; prefer `suppress` in new
  operator procedures because the intent is clearer.
- `retry-match`: clear the current target and rerun deterministic matching
  against current generated pages. It does not enable semantic apply.

Example combine:

```bash
$PY main.py --project "$CFG" human resolve EDIT_ID \
  --action combine --revision REVISION --document RAW_REL \
  --text-file /absolute/private/path/combined-section.md
```

Resolution is idempotent by resolution ID. Repeating the exact edit/action/
revision/combined-text request returns the recorded resolution.

### 10.3 Legacy recovery

```bash
$PY main.py --project "$CFG" human recover-legacy RAW_REL
```

Recovery searches project Git history and accepts a page only when source
identity, raw digest, planning stamp, pure state body, sidecar hash, lack of
human markers/links, and object evidence all agree. Every pinned edit must have
exactly one distinct verified body. Otherwise the entire recovery remains
pinned. Never manually copy a plausible old page into the journal.

### 10.4 Live report scaffold

```bash
$PY main.py --project "$CFG" human live-plan \
  --path /UNIQUE-DISPOSABLE-TARGET
```

The path must equal or be below the configured `/<target_name>` boundary. Since
`target_name` is one component and publisher writes are rooted there, normally
use the exact unique target root. Record the resulting report path and
confirmation code in the private test session, not in this repository.

The command does not run any matrix case and does not grant remote mutation
permission. A live harness must call
`verify_boundary_confirmation(growi_url, disposable_path, confirmation_code)`
immediately before each mutating/deleting case.

## 11. Known limitations and questions the live agent must answer

These are not hidden TODOs; they are explicit validation risks:

1. A heavily rewritten H2/H3 block can produce a large deterministic conflict
   even if only one fact matters. This is safe but noisy. Measure frequency on
   representative documents.
2. Section rename/split/combine can be intentionally conservative and orphan an
   edit. Verify that the retained note is complete and that no second copy is
   silently applied elsewhere.
3. Semantic observe mode currently sees one current-generated page candidate in
   runtime pull. It is not evidence that cross-page semantic relocation works.
4. Apply mode is deterministic only. Do not promote a semantic proposal into
   apply behavior during this test.
5. GROWI Markdown serialization, permalink rewriting, image upload syntax, and
   attachment behavior must be tested on the actual target version.
6. Activity APIs may be global or permission-restricted. A constrained case is
   not a pass; record it as constrained and prove forced inventory behavior.
7. Legacy pins are expected for old pages that lack a verified pure baseline.
   They can be large. Do not lower the recovery proof to make the report green.
8. `publisher/phase.py` is not yet the sole implementation boundary. Tests must
   exercise CLI/runner paths, not only phase facade functions.
9. `tools/hc_live/human.py` and `tools/hc_live/source.py` predate the current
   config/data layout and contain local defaults. Do not use them on private or
   remote data without separately reviewing/refactoring them. The supported
   interface for this handoff is the main CLI plus the GROWI UI or a private
   boundary-checked harness.
10. `LiveVerificationReport` stores case results but there is no general live
    matrix runner. The local agent must perform and record each case; pending or
    constrained cases keep the final report incomplete.
11. The operator resolver validates the last locally observed revision. It does
    not refetch remote state inside `human resolve`; run a fresh pull first and
    rely on publication preflight/409 protection afterward.
12. Watch/sync behavior depends on the target deployment's activity permissions,
    model endpoints, parser, embedder, and Jev backend. Record environmental
    constraints separately from human-overlay correctness.
13. Default one-shot isolated `sync` currently reaches remote capture only when
    a source job is claimed. With an unchanged source mount, a UI-only GROWI
    revision can be missed. Use explicit `pull --inventory` before `sync` until
    `idle_sync_remote_reconciliation` is implemented. The watcher is not affected
    when its periodic GROWI pull is enabled.

## 12. Focused offline verification

Use the configured project virtual environment. These commands do not contact
private services because the relevant clients/models are faked or injected.

### 12.1 Syntax/import check

```bash
timeout 60s .venv/bin/python -m compileall -q \
  common convert wiki linker index publisher runner graph
```

### 12.2 Core human overlay suite

```bash
timeout 60s .venv/bin/python -m unittest tests.test_human_changes -q
```

This covers ordinary capture, exact/disjoint/conflicting edits, absorbed and
reactivated edits, moves/orphans, retained notes, additions/deletions, atomic
tables/code/images, marker corruption, transport spelling, source identities,
legacy pins, sync ordering, revision races, candidate rollback, tier 0-3,
partial publication, lost responses, and linker exclusion.

### 12.3 Rollout/activity/operator suite

```bash
timeout 60s .venv/bin/python -m unittest tests.test_human_sync_rollout -q
```

This covers mode policy and INI precedence, observe/apply transitions, semantic
fallback and validation, activity/inventory behavior, operator resolution and
checkpointing, report gates, and legacy Git recovery.

### 12.4 Narrow commands by failure area

Run only the relevant method while iterating:

```bash
# Writer tier selection/update behavior used by the overlay integration
timeout 60s .venv/bin/python -m unittest tests.test_update_tiers -q

# Latest-base writer/injected-model regression and all update tiers
timeout 60s .venv/bin/python -m unittest \
  tests.test_human_changes.TierIntegrationTest.test_human_change_survives_tiers_one_two_three_and_zero -v

# Operator commands remain committed at last-good and reject dirty state
timeout 60s .venv/bin/python -m unittest \
  tests.test_human_sync_rollout.OperatorAndLiveGateTest.test_human_cli_checkpoints_summary_and_rejects_a_dirty_live_tree -v

# GROWI compare-and-swap update contract
timeout 60s .venv/bin/python -m unittest \
  tests.test_human_sync_rollout.GrowiClientContractTest.test_update_uses_view_origin_to_enforce_revision_compare_and_swap -v

# Entire batch is checked before any write
timeout 60s .venv/bin/python -m unittest \
  tests.test_human_changes.PublicationSafetyTest.test_preflight_checks_entire_batch_before_any_write -v

# A 409 never triggers a blind retry
timeout 60s .venv/bin/python -m unittest \
  tests.test_human_changes.PublicationSafetyTest.test_409_aborts_without_second_put_or_create -v

# Pull precedes generation and overlay precedes publication
timeout 60s .venv/bin/python -m unittest \
  tests.test_human_changes.PublicationSafetyTest.test_sync_captures_before_parse_and_publishes_after_overlay -v

# Lost update response exact recovery
timeout 60s .venv/bin/python -m unittest \
  tests.test_human_changes.PartialPublicationTest.test_lost_update_response_recovers_from_the_exact_prepared_body -v

# Human edit after partial write is captured
timeout 60s .venv/bin/python -m unittest \
  tests.test_human_changes.PartialPublicationTest.test_human_edit_after_a_partial_write_is_captured_and_never_overwritten -v

# Linker changes are not captured as human
timeout 60s .venv/bin/python -m unittest \
  tests.test_human_changes.PartialPublicationTest.test_linker_links_are_not_recorded_as_part_of_a_human_edit -v

# Multi-line publication spelling versus human token edit
timeout 60s .venv/bin/python -m unittest \
  tests.test_human_changes.TransportSpellingMergeTest -v

# Activity fallback and boundary safety
timeout 60s .venv/bin/python -m unittest \
  tests.test_human_sync_rollout.ActivityDetectorTest -v

# Semantic observe-only contracts
timeout 60s .venv/bin/python -m unittest \
  tests.test_human_sync_rollout.SemanticAssistanceTest -v

# Legacy recovery proof
timeout 60s .venv/bin/python -m unittest \
  tests.test_human_sync_rollout.LegacyRecoveryTest -v
```

### 12.5 Required repository checks after edits

```bash
git diff --check
git status --short
git diff --name-only
```

The expected changed files for this task are the writer fix, human CLI fix,
focused rollout test, and this handoff. Any config, `.env`, data directory,
cache, generated page, or private fixture in the repository diff is a stop
condition.

## 13. Disposable private-subset setup

### 13.1 Prepare isolated inputs

Create outside the repository:

- an approved source subset mount;
- a new empty data root;
- an INI file;
- optionally, a separate private evidence directory for hashes/screenshots and
  combined conflict text.

Use at least:

- one prose document with stable headings and numerical facts;
- one document likely to split across multiple generated pages;
- one document containing a Markdown/table equivalent, code, links, and images
  if those exist in production;
- one document that receives a realistic major source rewrite.

Do not use symlinks back into a production data root or source mount.

### 13.2 Example disposable INI

```ini
[project]
source_mount=/absolute/private-test/subset-mount
target_name=llm-wiki-hc-disposable-UNIQUE
data_root=/absolute/private-test/data
growi_url=https://test-growi.example.invalid

[settings]
human_sync_mode=off
human_sync_activity_audit_seconds=3600
human_sync_activity_overlap_seconds=60
growi_mode=attach
```

`target_name` must be exactly one folder name, not a path. With the example it
maps to `/llm-wiki-hc-disposable-UNIQUE`. `data_root` is resolved relative to the
repository when it is relative, so use an absolute value for this test.

Keep the token outside the file if possible:

```bash
export GROWI_TOKEN='publisher-test-token-from-secret-store'
```

If the INI contains a token for an isolated environment, do not print or commit
it. Never reuse one of the real project configs merely to obtain credentials.

### 13.3 Validate resolved settings without printing secrets

```bash
.venv/bin/python - "$CFG" <<'PY'
import sys
from common.settings import load_settings
s = load_settings(sys.argv[1])
print({
    "data_project": str(s.data_root) + "/" + s.target_name,
    "source_mount": s.mount_path,
    "growi_host_configured": bool(s.growi_url),
    "target_name": s.target_name,
    "growi_mode": s.growi_mode,
    "human_sync_mode": s.human_sync_mode.value,
})
PY
```

Confirm all paths and the target name manually. Do not continue if any resolved
path is a real project's data root or source mount.

### 13.4 Service, report, and build preflight

`check` makes read-only service requests but prints endpoint URLs. Run it only
in the private session:

```bash
$PY main.py --project "$CFG" check
```

Create the report scaffold while the live test data root is still empty. The
current operator command intentionally establishes and checkpoints the project
Git baseline; running it after an uncheckpointed local build would correctly
reject that dirty generated tree.

```bash
$PY main.py --project "$CFG" human live-plan --path /llm-wiki-hc-disposable-UNIQUE
```

If local output must be reviewed before any remote write, use a second preview
INI with a different empty data root and no GROWI URL:

```bash
$PY main.py --project /absolute/path/to/preview-only.ini convert
$PY main.py --project /absolute/path/to/preview-only.ini build all
```

Inspect those preview pages, then discard or retain that separate preview root
as private evidence. Do not copy its generated state into the live project.

After independently verifying the live endpoint, boundary, credential scope,
and report confirmation, use default isolated sync for the initial live build
and publication:

```bash
$PY main.py --project "$CFG" sync
$PY main.py --project "$CFG" pull --inventory
$PY main.py --project "$CFG" human status
```

Confirm that the only live data root is `<data_root>/<target_name>`, all generated
GROWI paths are below `/<target_name>`, and the live project is clean at
last-good. This sequence exercises the intended candidate pipeline from the
first publication.

## 14. Live validation matrix

The report enumerates 41 cases. For every case record:

- initial and final hashes of source, pure generated, effective local, and
  remote managed Markdown where applicable;
- GROWI page ID/path and before/after revision IDs;
- edit ID/status and reason codes;
- command/API call counts, including PUT/create/delete counts for race cases;
- expected result and actual result without copying private content;
- whether final local Git `HEAD` equals `refs/llm-wiki/last-good` and durable
  paths are clean.

Use the following procedure. “Human edit” means an edit made with a separate
test user/UI or a reviewed private harness using the page's current revision.

### Group A: baseline and idempotence

1. **`initial_multi_page_publish`** — Publish a source that produces multiple
   pages. Verify one ownership marker per page, ledger page IDs/revisions,
   generated/local/remote snapshots, no human edits, and a clean checkpoint.
2. **`normal_update`** — Change one source fact, run default `sync`, and verify
   only expected generated content changes and the remote revision advances.
3. **`noop_repeat`** — Repeat pull/sync/publish without source or remote changes.
   Hashes, edit count, and remote revisions must remain stable; an unnecessary
   empty human CLI checkpoint is also a failure.
4. **`process_restart`** — Stop between completed commands, start a fresh
   process, rerun pull/sync, and verify journals/cursor/last-good resume without
   duplicate edits.

### Group B: direct human intent

5. **`human_add`** — Add a paragraph in GROWI. Pull in `off` and `observe`
   (both must block), then switch to `apply`, pull, and verify one active edit
   and one protected region.
6. **`human_replace`** — Replace one exact fact. Pull twice. Verify the fact and
   edit ID remain byte-stable and appear once.
7. **`human_delete`** — Delete one generated fact. Verify durable suppression
   across a source regeneration and restart, without deleting adjacent content.
8. **`same_edit`** — Human and source independently make the same fact change.
   Sync and verify `absorbed`, one fact, and no conflict page entry.
9. **`disjoint_edit`** — Human and source edit different tokens/lines in the same
   block. Verify both changes are present verbatim with status active.
10. **`contradiction`** — Human and source change the same value differently.
    Verify both variants are visible, the edit is `conflict`, and source/human
    markers are balanced.

### Group C: operator resolution

For each resolution, run a fresh pull first, take the revision from `human
status`, run the resolution, link, and publish. Then pull again to prove the
result is stable.

11. **`keep_human`** — Resolve a contradiction with `keep-human`. Verify the
    source candidate is removed, human text remains, and the identical source
    version does not recreate the conflict.
12. **`accept_source`** — Resolve with `accept-source`. Verify the edit is
    tombstoned and only current source text remains.
13. **`combine`** — Resolve using a private UTF-8 text file containing an
    intentional combination. Verify that exact combined text becomes the active
    human value and no old copy survives.
14. **`suppress`** — Resolve a durable human addition/deletion with `suppress`.
    Verify it remains tombstoned after rerender and restart.

### Group D: generator tiers and structural changes

Use source variants that make the writer report the intended tier. Save the
`update_decision` progress event as evidence.

15. **`tier_0`** — No content change or the writer's tier-0 path. Verify the
    overlay is rendered without mutation or duplication.
16. **`tier_1`** — A small incremental patch to the page containing an existing
    human edit. Verify the injected/production model path used by the run and
    preservation of the edit.
17. **`tier_2`** — Regenerate a subset of pages. Verify human intent follows an
    unambiguous block or is retained, never dropped.
18. **`tier_3`** — Force/full regeneration. Verify the pure base is replaced but
    all active human edit IDs still render exactly once.
19. **`split_combine_rename_reorder`** — Exercise page split, page combine,
    heading rename, and section reorder. For each old edit, record whether it
    followed by exact/related anchor or became orphaned. Any silent attachment
    to the wrong topic is a failure; a complete retained note is an acceptable
    conservative result.

### Group E: remote integrity and ownership

20. **`remote_move`** — Move an owned page in GROWI. Pull must classify/block it;
    sync must not recreate or overwrite it silently.
21. **`remote_delete`** — Trash an owned page. Pull must block and retain deletion
    evidence. Test only in the disposable subtree.
22. **`marker_damage`** — Damage the ownership marker, or one side of a human
    marker pair: capture blocks and preserves observed evidence. Separately,
    delete a whole human marker pair but leave its text: no block, no loss, the
    text stays one active edit.
23. **`foreign_destination`** — Create an unmanaged page at a path needed by a
    new local page. Publication must fail during preflight before any page in the
    batch is written.
24. **`duplicate_marker`** — Duplicate an ownership marker on another test page.
    Forced inventory must classify duplicate ownership and refuse adoption.
25. **`source_rename`** — Rename/move the subset source while preserving its
    identity through the supported pipeline. Verify journal identity and page
    IDs/path moves behave as designed. Then test a different source reusing the
    old path and verify it does not inherit the journal.
26. **`bulk_delete`** — Remove multiple subset sources and run sync. Verify only
    inspected publisher-owned pages with expected revisions are trashed; a late
    edit blocks deletion.
27. **`markdown_integrity`** — Human-edit prose containing code fences, marker
    examples, comments, tables, inline code, and footer-like text. Verify bytes
    inside the human region are preserved through link/publish/pull.
28. **`attachments_images`** — Add/use representative approved images or
    attachments. Verify the human region is preserved and publisher image
    rewriting does not corrupt the journal. Record transport differences by
    hash, not by embedding the asset in the report.

### Group F: revision and partial-publication races

These cases require a controlled proxy, instrumented client, or an approved
pause hook. Manual timing alone is weak evidence. Never run them on a shared or
production subtree.

29. **`conditional_409`** — Change the page after it is inspected but before
    update. Verify one update attempt, HTTP 409 handling, recorded observed
    revision, and no second PUT.
30. **`edit_after_preflight`** — Complete batch preflight, then edit a later page
    before its update. Verify publication stops and the human revision remains.
31. **`edit_during_generation`** — Start a slow generation, edit the previously
    published page, then allow generation to finish. The next preflight/pull must
    capture or block the late edit; it must not overwrite it.
32. **`lost_update`** — Let an update land but drop the response. Restart and
    verify exact prepared-body recovery classifies it as bot output, not human.
33. **`lost_create`** — Let create land but drop its response. Retry and verify
    the exact stamped page is reconciled without a duplicate page.
34. **`partial_publish_late_edit`** — Let an early page publish, fail a later
    page, then human-edit the early page before recovery. Verify the confirmed
    bot base and only the late human delta are captured.

### Group G: activity, rollback, and restart

35. **`watcher_restart`** — Run the watcher on the disposable project, make one
    remote edit, stop/restart around polling, and verify exactly-once capture.
36. **`activity_cursor_restart`** — Create events sharing a timestamp or inside
    the overlap window, restart, and verify ID-based dedupe without skipping a
    late event. If the activity feed is inaccessible, record constrained and
    prove forced inventory instead; do not mark pass.
37. **`full_inventory_equivalence`** — Compare ordinary activity-selected pull
    with `pull --inventory`. They must reach equivalent page/journal state for
    the same remote revisions.
38. **`candidate_rollback`** — Capture a human edit in a candidate, force a later
    generation/publication failure, and verify old generated state plus the new
    journal is restored to live state.
39. **`last_good_restore`** — There is no CLI command for this:
    `restore_publication()` runs only when a queued sync fails or is
    interrupted during publication (`publisher/queue.py`). Trigger it that way
    on the disposable project. Verify expected revisions before remote mutation
    and preserve any intervening human change by blocking.
40. **`service_restart_reconcile`** — Restart after a prepared or confirmed
    per-page write but before the overall ledger is final. Verify exact evidence
    drives recovery and durable paths finish clean.
41. **`model_outage_fallback`** — In observe mode, make the semantic scorer,
    writer, and/or judge unavailable. Verify bounded exact fallback, reason
    codes, no accepted remote difference, and no model-driven data loss. Also
    verify deterministic apply does not depend on semantic models.

The authoritative names are in `publisher/live_verification.py::LIVE_CASES`.
Before finalizing, compare all 41 report keys to that tuple. The prose grouping
is for execution order; the tuple is the source of truth. In particular, keep
`service_restart_reconcile` independent from the lost-response cases even if the
same fault-injection harness is used.

### Group H: additive matrix extensions (not implemented yet)

The current code and new reports contain only the 41 names above. The cases
below are requirements for the next pass, not passes. Until they are in
`LIVE_CASES`, record them as separate private evidence and keep the
recommendation at `off`/incomplete. Append them; never rename, remove, or
reorder the 41.

Expected results describe what the wiki reader sees. They deliberately say
nothing about headings, block shapes, file names, or match scores: any
implementation that produces the outcome passes. Every case is also checked
against these invariants:

- every current human edit appears verbatim exactly once, or the run blocks;
- when human and source disagree, both are visible and neither silently wins;
- nothing is written over a remote revision the pipeline did not inspect;
- a repeat run with no new change is a no-op;
- the run ends with `HEAD == refs/llm-wiki/last-good` and clean durable paths.

42. **`concurrent_add_same_anchor`** — Human and source each add text at the
    same place. Identical text appears once. Different text: both appear, once
    each.

43. **`human_delete_source_modify`** — Human deletes a fact and the next source
    version changes it. Visible conflict; the new source value must not quietly
    reappear as if nothing had been deleted.

44. **`human_modify_source_delete`** — Human changes a fact, then the source
    deletes the fact, its section, or its whole page (run all three). The human
    text stays visible as a conflict or retained note.

45. **`concurrent_delete_same_fact`** — Both sides delete the same text. It is
    absent everywhere, retained notes included, and never comes back. Run it
    with one line and with a whole section. *Current code fails the
    whole-section variant: the deleted text reappears in retained notes.*

46. **`multiple_remote_revisions_before_pull`** — Several GROWI revisions land
    before one pull, including two edits to the same section and one revision
    that undoes another. Only the latest net intent is captured, once.

47. **`mixed_page_capture_failure`** — One pull sees a valid edit on page A
    and a damaged or moved page B. A's capture is checkpointed. B stays blocked
    and is rechecked on every later pull until resolved, whatever the activity
    cursor does. A retry neither loses nor duplicates A.

48. **`source_move_delete_with_remote_edit`** — An unpulled human revision sits
    on a document that the same sync renames or deletes at the source. Rename:
    capture first, then move; the edit follows. Delete: while the document has
    current human edits, the delete blocks for an operator decision and the
    edited page is not trashed. Retained notes cannot help here because they
    live in the folder being deleted.

49. **`transport_only_remote_revision`** — A human opens a page in the GROWI
    editor and saves without changing anything. No human edit is created. A
    difference that cannot be shown to be transport-only blocks.

50. **`mode_transition_same_revision`** — Show one remote revision to `off`,
    `observe`, and then `apply` with no new edit, then switch
    `apply -> off -> apply`. Restart the process and reload the INI between
    runs. `off` and `observe` accept nothing; `apply` captures that revision
    once; later switches create nothing new.

51. **`growi_conflict_ui_resolution`** — A human resolves a shown conflict in
    the GROWI editor four ways: keep only their side, keep only the source
    side, write a combination, or delete the marker comments while editing.
    Also edit the conflict dashboard and retained-notes pages directly. Each
    result is captured as what the page now says, exactly once; an unclear
    result blocks.

52. **`remote_revision_rollback`** — Someone restores an older body from GROWI
    history. Treat it as a new human revision. If the restored body predates
    the last bot publication, it carries stale generated text. That text must
    not become permanent human intent: block it or show it as a conflict
    against the current source.

53. **`idle_sync_remote_reconciliation`** — After a publish, with the source
    mount unchanged, a human edits a page in GROWI and the operator runs plain
    `sync` without `pull`. Sync detects the edit before any other work. In
    `apply` it captures, relinks, and republishes that document. In `off` and
    `observe` only that document blocks; other documents still sync. A repeat
    run is a no-op with no model or parser calls. Keep the per-job capture in
    `sync_once()` and the publication compare-and-swap; this check is an extra
    layer. Endpoint/marker migration (`republish_if_stale`) needs its own path,
    because old page IDs must not be pulled from a newly configured server.

54. **`human_section_placement`** — With no source change, a human adds a new
    section mid-page, renames a section, or reorders sections. After the next
    sync every section is where the human put it. *Current code fails: added or
    renamed sections move to the bottom of the page.*

55. **`conflict_survives_unrelated_edit`** — While a conflict is shown, a human
    edits a different line in the same section. The conflict stays visible with
    both versions, and only the new line is added as human intent. *Code
    reading suggests current code drops the source version and silently keeps
    the human side.*

56. **`fast_policy_human_overlay`** — Repeat `human_add`, `contradiction`, and
    `human_section_placement` with `sync --fast`, including `--fast --link` and
    `--fast --repair`. No link or other generated change is written inside a
    human region, and a later human edit there does not record linker text as
    human intent. *The fast inline linker (`graph/fast/inline_linker.py`
    `_wrap_once`) currently skips fences, headings, and tables but not human
    regions.*

Removed from the earlier draft so nobody re-adds them:
`human_edit_source_page_removed` (now part of 44), `multiple_edits_same_block`
(46 and 10), `concurrent_heading_title_change` (54 and 19),
`stale_operator_resolution` (29 and 30), `structured_atomic_conflict` (10, 27,
and 28), `absorbed_then_source_regression`, `retained_note_round_trip`, and
`auxiliary_filename_collision` (internal mechanics, offline tests only), and
`legacy_unverified_baseline_live` (needs a copied real ledger that points at
production page IDs; `LegacyRecoveryTest` covers the logic offline).

#### Implementation contract for Group H

1. Append the 15 IDs above to `LIVE_CASES`, making 56 in total. Never rename or
   remove the existing 41.
2. `finalize()` compares against `LIVE_CASES`, and keys missing from an old
   report count as pending. An old 41-case report must never finalize as
   complete. Do not rewrite old reports on open; add a missing case only when
   the operator records it.
3. Prefer the LLM path over new hardcoded rules (heading matching, block
   shapes, file-name conventions, similarity thresholds). Check model output
   mechanically: human text verbatim, markers balanced, nothing invented. When
   a check fails, fall back to a visible conflict, never to dropping text.
4. Add one focused offline test per case before running it live.
5. Record each live case separately, even when one fixture covers several.
6. Update the case count in this handoff in the same commit that appends to
   `LIVE_CASES`.
7. The `apply` rollout stays incomplete until every Group H case passes or is
   reviewed as constrained. A constrained result is not a pass.

## 15. Recording and finalizing the report

`LiveVerificationReport.record_case()` expects hashes, revisions, operation,
expected/actual summaries, call counts, and reason codes. It does not require
page bodies. A private harness can open the report and record one result after
each case:

```python
from pathlib import Path
from publisher.live_verification import LiveVerificationReport

report = LiveVerificationReport.open(Path(REPORT_PATH))
report.record_case(
    CASE_NAME,
    passed=PASSED,
    initial_hashes=INITIAL_HASHES,
    final_hashes=FINAL_HASHES,
    revisions=REVISIONS,
    operation=OPERATION_SUMMARY,
    expected=EXPECTED_SUMMARY,
    actual=ACTUAL_SUMMARY,
    calls={"get": GETS, "put": PUTS, "create": CREATES, "delete": DELETES},
    reason_codes=REASON_CODES,
)
```

If a case cannot be executed because the disposable environment deliberately
lacks a global activity permission or fault injector, use `record_constraint()`
with precise reason codes. A constrained result keeps the report incomplete.

Before `finalize()`:

1. Ensure every report case is passed, failed, or explicitly constrained; none
   may remain accidentally pending.
2. Run a forced inventory and a final pull.
3. Run `human status`; unresolved/blocked counts must be explained.
4. Check every non-deleted/non-absorbed edit marker appears exactly once.
5. Verify project `HEAD` equals `refs/llm-wiki/last-good` and durable paths are
   clean.
6. Clean up only the confirmed disposable GROWI subtree using the approved
   credential and record whether recovery remains possible.
7. Preserve the local data root/report until results are reviewed.

The report is eligible for a reviewed observe rollout only when the full matrix
passes. `apply` requires a separate decision based on the private-document
results, conflict/orphan rates, transport behavior, and operational recovery
evidence.

## 16. Triage guide

### “cannot ... dirty last-good working tree”

Do not reset. Run read-only Git status scoped to durable paths, inspect the last
command, and preserve any prepared/capture evidence. The current `human` CLI
should not cause this on success; if it does, record a regression with the
command and changed paths.

### Remote difference blocks in off/observe

This is expected. Verify the block/observation, then change only the disposable
INI to the next approved mode and rerun pull. The same revision must be refetched
and apply must clear only a rollout-policy block, not an unrelated safety block.

### `legacy_pinned`

The system could not prove a pure base. Keep the pin, rebuild through the normal
writer, or run explicit Git recovery. Do not edit snapshot JSON or relabel the
record manually.

### `orphaned`

Check the complete human text in `99-Retained-Human-Notes*.md`, the anchor path,
heading, neighbors, and current generated structure. Use `retry-match` only
after the source structure makes the target unambiguous; otherwise combine or
suppress through an explicit operator decision.

### `conflict`

Verify both exact variants and marker balance. Resolve with an explicit action.
Do not remove comments with a bulk formatter because marker damage intentionally
blocks capture.

### Remote moved/deleted/marker-damaged

Treat remote state as authoritative evidence of an intervention. Repair or
decide the remote state explicitly, then rerun inventory/pull. Do not let sync
silently recreate the page.

### Ambiguous prepared publication outcome

Preserve the page and attempt record. Compare exact body hashes, page ID/path,
prepared revision, confirmation, and observed revision. Only exact prepared
evidence may be adopted as bot output.

### Linker text appears as a human edit

Check that `_planning/pages/<page>.md` exists and corresponds to the published
generation, and compare the page record's `generated_blob`. A build that moved
past the published generation intentionally disables unsafe link subtraction.

## 17. Completion criteria for the local agent

Return all of the following to the reviewer without private bodies or secrets:

- code commit/hash tested and whether the working tree had additional changes;
- disposable INI hash and redacted resolved endpoint/boundary/data paths;
- focused offline command results;
- live report path/hash and per-case status/reason-code summary;
- counts of active, absorbed, deleted, conflict, orphaned, legacy-pinned, and
  blocked records by document type;
- conflict/orphan examples described by structure and hashes, not prose;
- activity/inventory permission findings;
- partial publication/restart evidence and API call counts;
- cleanup status and whether the disposable state is recoverable;
- a clear recommendation: remain off, proceed to observe, or request a separate
  reviewed apply rollout.

Do not claim “human editing works” based only on unit tests or one successful
replacement. The acceptance bar is that human intent is never silently lost,
generated source truth is never silently replaced by a guess, remote races fail
closed, and every recovery path leaves durable state auditable and repeatable.
