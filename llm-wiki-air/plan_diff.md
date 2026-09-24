# Mount Diff Pipeline Implementation Plan

> Update behavior (tiers, formats, .doc/.xls) is specified in plan_diff_2.md.

## 1. Goal

Implement reliable add, delete, update, rename, retry, and recovery behavior for
files observed under a project mount. Use Git as a local checkpoint store so a
failed operation can return to the last state known to match GROWI.

The implementation must be:

- safe when a mount file changes or disappears during a long build;
- deterministic: every build reads an immutable source snapshot;
- incremental for genuinely small, localized edits;
- restartable after process or machine failure;
- conservative around GROWI edits made by people;
- minimal: extend the existing scanner, queue, pipeline, writer, ledger, and
  linker instead of adding a second publishing pipeline;
- compatible with existing projects that have no Git history or source IDs;
- DOCX-capable first. PDF, PPTX, XLSX, and XLSM classification are explicitly
  deferred.

## 2. Non-goals for this change

Do not implement these in the first version:

- cross-project transactions;
- automatic merge of conflicting human changes in GROWI;
- content-aware classification for PDF, PPTX, XLSX, or XLSM;
- a custom version-control system;
- background garbage collection beyond normal `git gc` behavior;
- a new queue service or dependency;
- parallel publication of two candidate states for the same project.

For unsupported formats, retain the safe behavior: an update is a full rebuild.

## 3. Important facts about the current code

The implementer must read these functions before editing:

- `publisher/queue.py::_snapshot`, `scan`, `_enqueue`, `claim`, `current`,
  `finish`, `recover`, and `work_once`;
- `publisher/scanner.py::scan_mount`;
- `publisher/pipeline.py::_assert_source_unchanged`, `_remove_sources`,
  `_publish_sweep`, `sync_once`, and `delete_sources`;
- `graph/workspace/writer.py::write_wiki_pages`;
- `graph/wiki/incremental.py::invalidate_pages`;
- `graph/linker/chunks.py::chunk_id` and `make_chunks`;
- `graph/linker/catalog.py::reconcile` and `sync_from_planning`;
- `graph/growi/client.py::GrowiPublisher` and `publish_pages`;
- `publisher/ledger.py::Ledger`.

Current behavior that must not be lost:

- metadata scans are cheap and coalesce jobs;
- SHA-256 is authoritative during a real sync;
- raw writes are atomic;
- wiki generation supports resumable state;
- stale queue versions cannot normally finish their job row;
- linker cancellation already uses `stop_check`;
- GROWI publishing checks remote revisions before overwriting;
- deletion unlinks peer documents and republishes touched peers;
- failed generation is not published.

Current limitations this work must fix:

1. A running build reads the live mount path. A later edit makes
   `_assert_source_unchanged` abort every update, including a tiny one.
2. A delete can remove a running, unpublished job row. The build may leave raw,
   wiki, state, or ledger artifacts with no future delete job.
3. Every newer queue version cancels the active build. There is no small/large
   classification.
4. `data/<target>/` has no checkpoint or rollback boundary.
5. Rename is seen as delete plus add.
6. Linker chunk IDs contain the document path, so a rename changes IDs even
   though the document content did not change.
7. GROWI publishing is not transactional and cannot be rolled back by a local
   Git checkout alone.

## 4. State model and terminology

Use the following terms consistently in code, logs, and tests.

### Working source

The latest file currently visible in the mount. It is mutable and must never be
read directly after a job has been claimed.

### Queued target

The newest observed immutable source blob for one logical source. Repeated
events replace this target; they do not accumulate patches.

### Staged build

The immutable queued target currently being parsed and generated. It is the
equivalent of Git's index. A later mount edit cannot modify it.

### Candidate commit

A complete, validated local state produced from `last-good`. It can be
published, retried, or discarded, but it is not yet trusted as the shared
state.

### Last-good commit

The Git commit referenced by `refs/llm-wiki/last-good`. Its tracked local state
is known to have completed GROWI publication. This is the only automatic
rollback target.

### Absolute source ID

An immutable UUID assigned once to a logical source. Mount paths, raw paths,
wiki paths, GROWI page IDs, and linker rows map to this ID. A rename changes
paths, not identity.

## 5. Git repository layout

Initialize one Git repository at `data/<target>/.git`. Do not initialize the
repository root or share history between projects.

Track only durable, restorable state:

```text
sources/                         immutable-current source copies by source ID
raw/                             parsed Markdown
wiki/                            generated and linked wiki pages
metadata/state/                  resumable generator state
metadata/pipeline.json           source and GROWI publication ledger
metadata/source-identities.json  path/source-ID mapping and tombstones
.gitignore                       per-project runtime exclusions
```

The working copy under `sources/` uses stable names:

```text
sources/<source-id>.docx
```

The original mount-relative name belongs in the identity manifest and ledger,
not in the stable blob filename.

Never track:

```text
mount/
metadata/watch-queue.sqlite*
metadata/wiki-linker.sqlite*
metadata/*.lock
metadata/work/
metadata/candidates/
*.log
```

SQLite files are excluded because copying a live database and WAL through Git
does not guarantee a consistent database. The linker database is rebuildable
from `wiki/**/_planning`. The queue database describes current work and must
survive independently of rollback.

Configure the project repository locally, without depending on a machine-wide
Git identity:

```text
user.name  = llm-wiki
user.email = llm-wiki@local
```

Create an initial empty commit and point `refs/llm-wiki/last-good` to it for a
new project.

### Existing-project migration

On first use:

1. Create the repository and exclusions.
2. Validate that `pipeline.json`, raw files, wiki folders, and source markers
   agree. Do not bless visibly incomplete state.
3. Assign source IDs and identity seeds as described below.
4. Commit the existing durable state as `baseline`.
5. Set `refs/llm-wiki/last-good` to that commit.
6. Leave mount files untouched.

If validation fails, stop with a clear error and ask the operator to run a
normal repair sync. Never create a baseline from partially generated output.

## 6. Minimal code structure

Add one small module, `publisher/history.py`. Do not spread shell calls and Git
path rules through the queue and pipeline.

It should contain only the operations needed by this feature:

```python
ensure_repository(project) -> str
last_good(project) -> str
stage_blob(project, source_path) -> Blob
read_blob(project, oid) -> bytes
candidate(project, operation_id) -> context manager[Project]
commit_candidate(candidate_project, message, metadata) -> str
promote(project, candidate_project, commit) -> None
restore_last_good(project) -> None
```

`Blob` needs the Git object ID, SHA-256, size, and suffix. Use `subprocess.run`
with argument lists, `check=True`, captured stderr, and no shell. Keep Git
commands in this module.

Use a detached Git worktree for each candidate. Put it beside the live project,
not inside it, for example:

```text
data/.diff_test-candidates/<operation-id>/
```

Always remove the worktree in `finally`. Prune abandoned worktrees during
startup recovery. Validate every resolved candidate path before removal.

`promote` must:

1. verify the candidate commit exists;
2. atomically advance `refs/llm-wiki/last-good` only after publication succeeds;
3. update the live tracked working tree to that commit;
4. copy or rebuild the candidate linker database;
5. leave queue SQLite and locks untouched.

Do not call broad `git clean`. Remove only the known candidate directory.

## 7. Ledger and identity changes

Bump `pipeline.json` to schema version 3 while continuing to read versions 1
and 2.

Each source row must contain:

```json
{
  "source_id": "31fc...uuid...",
  "id_seed": "team/original_docx.md",
  "mount_rel": "team/current.docx",
  "raw_rel": "team/current_docx.md",
  "source_sha256": "...",
  "source_blob_oid": "...",
  "last_error": ""
}
```

- `source_id` is the absolute identity used for mapping.
- `id_seed` never changes. For migrated documents, set it to the current
  document/raw path so existing path-derived IDs can initially be preserved.
- `mount_rel` and `raw_rel` change on rename.
- `source_blob_oid` identifies the exact original source in Git.

`metadata/source-identities.json` must contain active mappings plus tombstones:

```json
{
  "schema_version": 1,
  "active": {"team/file.docx": "source-uuid"},
  "tombstones": {
    "team/file.docx": {
      "source_id": "source-uuid",
      "source_sha256": "...",
      "deleted_at": "..."
    }
  }
}
```

Keep tombstones for at least the retained Git history. They let delete/re-add
preserve identity when it is clearly the same source. If several deleted files
have the same hash, do not guess; create a new identity unless the same path has
an unambiguous tombstone.

## 8. Queue schema and coalescing

Migrate the existing queue tables in place. Use `PRAGMA table_info` and
`ALTER TABLE ADD COLUMN`; do not drop user queues.

Add these fields to `jobs`:

```text
source_id         TEXT NOT NULL DEFAULT ''
from_rel          TEXT NOT NULL DEFAULT ''
target_blob_oid   TEXT NOT NULL DEFAULT ''
target_sha256     TEXT NOT NULL DEFAULT ''
classification   TEXT NOT NULL DEFAULT 'none'
base_commit       TEXT NOT NULL DEFAULT ''
```

Allow operation `move` in addition to add/update/delete.

Add `source_id`, `source_sha256`, and `blob_oid` to queue `sources` rows. The
queue's `sources` table remains an observed-mount index, not the publishing
ledger.

### Scan behavior

For a metadata-detected add or update:

1. Open and hash the file once.
2. Write its bytes into the project Git object database.
3. Re-stat it. If size or timestamp changed while reading, discard the sample
   and try it on the next scan.
4. Store the immutable blob OID and SHA-256 in the job.
5. A later event for the same source replaces those target fields and
   increments `version`.

The queue stores the latest desired file, never a chain of patches. Diffs are
recalculated from immutable versions when needed.

### Queued add followed by delete

- If the add is still `queued` and the source was never in `last-good`, delete
  the job and staged object reference. Report it as `cancelled`.
- If the add is `running`, do not delete its row blindly. Mark the running job
  superseded. The worker discards its candidate. Since the source was never in
  `last-good`, no delete candidate or GROWI call is needed.

### Running committed source followed by delete

Replace/supersede the running job with a fast `delete` job. The old worker must
see that it no longer owns the current version and discard its candidate. The
delete job starts from `last-good`.

### Repeated updates

Keep one row per `source_id`, not per current path. Replace the queued target
blob and classification. Preserve the running job's immutable target in its
claimed `Job` value. If a new event arrives, its higher queue version remains
for the next run.

## 9. DOCX change classification

The decision needed during a running build must be fast; calling the external
parser from the scan thread would make the watcher unreliable. For this first
DOCX-only version, extract comparison text locally with the already-installed
`python-docx` package.

Extract, in document order:

- non-empty body paragraphs;
- text from table cells;
- paragraph breaks and cell separators.

Normalize line endings only. Do not lowercase, remove punctuation, or collapse
all whitespace because those operations can hide real edits.

Use `difflib.SequenceMatcher(..., autojunk=False)` over lines. Calculate:

```text
changed_lines = sum(max(old_len, new_len) for every non-equal hunk)
ratio = changed_lines / max(old_line_count, new_line_count, 1)
```

Initial thresholds:

```text
document below 1,000 lines: large when ratio > 0.25
document at least 1,000 lines: large when ratio > 0.10
```

An edit is `large` regardless of ratio when top-level headings or the document
outline changed, classification failed, or the source format is not DOCX.
The number and spread of hunks do not make an otherwise small edit large: many
small factual edits should invalidate their affected wiki pages independently.

Everything else is `small`.

Keep the thresholds as named module constants initially. Do not add user
configuration until real measurements show it is needed.

Expose the decision in the scan result and queue status:

```json
{
  "classification": {
    "manual.docx": {
      "kind": "small",
      "ratio": 0.012,
      "hunks": 1,
      "reason": "localized"
    }
  }
}
```

Classification during an active build compares the active staged blob with the
newest blob. Classification immediately before processing compares
`last-good` with the newest blob. Never apply a patch calculated against an
older base.

## 10. Immutable build input

Extend `Job` with its blob information. When `work_once` claims a job:

1. Create a candidate worktree from `last-good`.
2. Materialize `target_blob_oid` to a private candidate mount path.
3. Construct a `Project` whose root is the candidate worktree and whose mount
   is that private snapshot directory.
4. Run the existing `sync_once` path against that project.

The candidate pipeline must not call `_assert_source_unchanged` against the
live mount. It may retain the assertion against the immutable candidate file.

This single change allows a small update to wait while the current staged build
finishes safely.

## 11. Update behavior while building

Replace the Boolean `current()` decision with an action:

```text
continue  current version, or newer update classified small
cancel    delete, move, or newer update classified large
```

Keep a small helper such as `supersession(project, job) -> Literal[...]` in
`queue.py`; do not pass queue SQL into graph/wiki.

### Small update arrives

1. Leave the active immutable build running.
2. Keep the new target as a queued higher version.
3. Publish and commit the active candidate normally.
4. Do not let `finish` remove the higher-version row.
5. Claim the newer version.
6. Recalculate its diff against the new `last-good` commit.
7. Run incremental generation for affected pages.
8. Publish and commit again.

### Large update arrives

1. Make `stop_check` return true.
2. Let generator/linker cancellation unwind normally.
3. Discard the candidate worktree.
4. Keep the newest target queued.
5. Start the next candidate from unchanged `last-good`.
6. Perform a full document generation.
7. Publish and promote only that candidate.

### Update arrives during GROWI publication

Do not interrupt a GROWI page batch halfway. Finish the candidate publication,
promote it, and immediately process the queued target. This may expose the old
version briefly, but it avoids a half-old/half-new remote document.

## 12. Incremental generation for localized edits

Continue using `write_wiki_pages(..., resume=True)` and
`invalidate_pages`. Do not build a second incremental engine.

The current implementation only resumes when line counts and hunk lengths are
unchanged. Extend it carefully:

1. Build a mapping for unchanged line blocks from `SequenceMatcher` opcodes.
2. Mark every page whose owner/reference ranges overlap a changed hunk.
3. Shift ranges for untouched pages using the unchanged-block mapping.
4. Invalidate the changed pages and their research/page state.
5. Update `plan.json` line count, ranges, and source hash atomically.
6. Run existing output validation.
7. If any old range cannot be mapped unambiguously, delete the run state and
   return `full`.

Return and report the actual scope:

```text
incremental  only safely mapped affected pages regenerated
full         complete document regenerated
```

Add `rebuild: "incremental" | "full"` to the source's `done` row and emit a
`diff-classified` progress event. This is operational visibility and an
acceptance-test hook, not a second state store.

## 13. Delete behavior

### Source not present in last-good

Cancel and discard its candidate. Remove any raw/wiki/state artifacts only
inside that candidate. Leave `last-good` and GROWI unchanged. No deletion
commit is necessary because the desired state equals `last-good`.

### Source present in last-good

1. Cancel any pre-publish candidate for that source.
2. Create a deletion candidate from `last-good`.
3. Call existing `remove_document` to remove graph links and collect peers.
4. Remove raw, wiki, generator state, ledger source row, and active identity
   mapping in the candidate.
5. Add an identity tombstone.
6. Re-link affected peers.
7. Commit the local deletion as a candidate commit.
8. Delete the owned GROWI document and publish affected peers.
9. Promote the candidate and advance `last-good`.

If deletion publication fails, keep `last-good` unchanged and reconcile GROWI
back to it.

## 14. Rename and move behavior

During one scan, pair a disappeared path and a new path as a move only when:

- their full source SHA-256 values are equal;
- exactly one old source and one new source match;
- the old source has an active source ID.

For rename plus content edit, use DOCX similarity only when there is one clear
candidate above a strict similarity threshold, for example 90%. Ambiguous
matches remain delete plus add. Correctness is more important than avoiding one
rebuild.

A move candidate must:

1. retain `source_id` and `id_seed`;
2. update mount/raw/wiki/state paths in the ledger and identity manifest;
3. move raw, wiki, and state trees in the candidate worktree;
4. update `_planning/source.json`, manifest metadata, and cached `raw_rel`;
5. update linker `documents`, `pages`, and chunk path columns;
6. retain chunk/node IDs;
7. rewrite local path-based links from the linker catalog;
8. move the GROWI page tree while retaining actual GROWI page IDs;
9. update `published_documents` and `published_pages` paths/revisions;
10. commit and promote the move.

The current client has no outbound page-move method. Add the smallest GROWI
client call supported by the deployed server and test it by asserting page IDs
before and after the move. Do not emulate a move by create-then-delete because
that violates the ID-preservation requirement.

### Stable linker IDs

Today `chunk_id(document, filename, ordinal)` uses a path-derived `document`.
Change it to accept an identity seed separately from the display/current path:

```python
chunk_id(id_seed, filename, ordinal)
```

Store the seed in planning metadata and `chunks.json`. Existing documents use
their old document path as the seed, preserving their current IDs. New
documents may use the source UUID. All future renames keep the same seed.

Apply the same rule anywhere legacy `make_node_id` receives a document path.
Do not globally replace IDs without a migration test.

## 15. Candidate publication and commit protocol

The safe order is:

```text
scan -> immutable blob -> queue -> candidate from last-good
     -> parse -> generate -> link -> validate
     -> create candidate Git commit
     -> pull/check GROWI revisions
     -> publish candidate
     -> advance last-good
     -> promote local working tree
     -> finish queue job
```

Creating the candidate commit before publication makes recovery deterministic,
but it does not move `last-good`.

Write a small transaction record in the queue database before remote publish:

```text
operation_id
candidate_commit
base_commit
phase: building | prepared | publishing
started_at
```

Delete it after successful promotion. This belongs in queue SQLite, not in Git.

### Failure before publishing

Discard the candidate. Keep `last-good` and GROWI unchanged. Mark the job
failed or retry it according to existing policy.

### Failure during publishing

GROWI may contain some candidate pages. Use the published-page ledger from the
base commit to compare remote page IDs and revisions:

1. If a page has a human revision not produced by either known commit, stop and
   report a conflict. Never overwrite it automatically.
2. Otherwise republish the base commit and remove candidate-only owned pages.
3. Confirm the base document set.
4. Leave `last-good` at `base_commit`.
5. Mark the candidate job failed/retryable.

### Crash recovery

At worker startup:

1. Recover `running` jobs as today.
2. Read unfinished transaction rows.
3. `building`: discard candidate and retry.
4. `prepared`: candidate was never published; discard or retry publish.
5. `publishing`: inspect remote page IDs/revisions. If all candidate revisions
   are present, finish promotion. Otherwise restore the base commit remotely.
6. Prune abandoned candidate worktrees.
7. Rebuild the linker SQLite index from committed planning files if needed.

Every recovery action must be idempotent so a second crash repeats safely.

## 16. Metadata-identical content changes

Keep normal stat scans cheap. Add a periodic content audit that hashes every
supported file and compares SHA-256 with the queue source table.

Expose this as:

```python
scan(settings, ..., verify_content=True)
```

`serve` should call it periodically, initially at the existing
`growi_interval` cadence. A content mismatch creates an update even when size
and `mtime_ns` match.

Do not hash every file on every short watcher interval.

## 17. Required scenario behavior

### 1. New file added

- scan stores its immutable blob;
- queue contains one slow add;
- build uses the blob, not the live mount;
- successful publish creates and promotes one candidate commit;
- ledger, source identity, local files, and GROWI agree.

### 1.2. Committed file deleted

- scan creates a fast delete;
- deletion candidate starts from last-good;
- local output, ledger, links, and owned GROWI pages are removed;
- deletion is committed and becomes last-good.

### 2. Add then delete before the first scan

- scan observes no file;
- no queue row, build, commit, or GROWI request is created.

### 3. Add then delete after scan but before work

- queued unpublished add is cancelled;
- its candidate is never built;
- last-good remains unchanged.

### 4. Delete during generation

- staged input remains readable;
- running candidate is cancelled and discarded;
- if absent from last-good, stop there;
- if present in last-good, enqueue and commit a real deletion.

### 5. Add then update before the first scan

- only the final bytes are staged;
- one add is built and committed.

### 6. Add then update while queued

- one job remains;
- its target blob is replaced;
- only the latest bytes are generated.

### 7. Update during generation

- small: active build completes, then the queued update is recalculated against
  the new last-good and applied incrementally;
- large: active candidate is cancelled/discarded and the newest target receives
  one full build from the unchanged last-good.

### 8. Update after completion while watching

- compare against last-good;
- small localized changes use incremental page invalidation;
- large changes use full document generation;
- successful output is published and committed.

## 18. Additional required behavior

- Pure rename/move retains source, chunk/node, and GROWI page IDs.
- Delete/re-add coalesces to final desired state when deletion is not committed.
- Rapid updates retain only the latest queued target.
- Restart retries or repairs unfinished work from its recorded phase.
- Parse and generation failures do not change last-good or GROWI.
- Partial publish failures restore the base unless a human edit causes a
  conflict.
- Periodic hash audit detects content changes hidden by identical metadata.
- Link-time updates use the same small/large policy as generation-time updates.
- Publish-time changes wait for the current publication to finish, then run.

## 19. Implementation order

Follow this order so every step leaves the old behavior runnable.

### Phase A: history foundation

1. Add `publisher/history.py`.
2. Add repository initialization and exclusions.
3. Add schema-v3 ledger reading/writing.
4. Add source IDs and ID seeds without changing existing generated IDs.
5. Add baseline migration tests.

### Phase B: immutable queue targets

1. Extend SQLite schemas in place.
2. Store changed source bytes as Git blobs.
3. Claim immutable blob data in `Job`.
4. Build from a candidate snapshot mount.
5. Ensure current add/update/delete behavior still passes existing tests.

### Phase C: candidate commits and rollback

1. Build in a detached candidate worktree.
2. Commit validated local state.
3. Add transaction phase records.
4. Publish, promote, and finish in the required order.
5. Implement startup recovery.
6. Add failure-injection tests.

### Phase D: update classification

1. Add DOCX text extraction.
2. Add pure hunk/ratio/locality classification functions.
3. Store and expose classifications.
4. Replace Boolean cancellation with continue/cancel policy.
5. Extend incremental line-range remapping.

### Phase E: rename

1. Detect exact one-to-one hash moves.
2. Add stable ID seed usage in linker generation.
3. Move local state and metadata.
4. Add the GROWI move operation.
5. Verify all relevant IDs remain unchanged.

### Phase F: audits and hardening

1. Add periodic content verification.
2. Add crash-phase recovery tests.
3. Add partial-publish restoration tests.
4. Run the full existing suite and the DOCX acceptance suite.

Do not begin rename work before immutable candidates and rollback are passing.

## 20. Test strategy

The acceptance suite is `tests/test_mount_diff_pipeline.py` and uses standard
`unittest`.

Fast fixture checks run normally. Live tests are opt-in because they call the
configured parser, LLM, embedding service, and GROWI and delete only pages under
a unique `diff-test-*` path.

Run every live case:

```bash
RUN_DIFF_INTEGRATION=1 .venv/bin/python -m unittest -v tests.test_mount_diff_pipeline.MountDiffPipelineAcceptanceTest
```

Run one case while implementing it:

```bash
RUN_DIFF_INTEGRATION=1 .venv/bin/python -m unittest -v \
  tests.test_mount_diff_pipeline.MountDiffPipelineAcceptanceTest.test_07a_small_update_during_generation_finishes_then_runs_incrementally
```

The tests use a fresh temporary local project and a unique remote GROWI prefix
per test. `data/diff_test/mount/test.docx` is the immutable fixture source.
Each test copies it before mutation.

Tests must assert outcomes, not sleeps. Mid-stage changes are injected from
existing progress callbacks. Polling, where unavoidable, must have a clear
timeout and diagnostic.

## 21. Required progress/result contract

Keep existing event names. Add only these fields/events:

- scan result: `moved` and `classification`;
- queue status: `source_id`, `from_rel`, `target_sha256`, `classification`,
  `base_commit`;
- done source row: `rebuild` (`incremental`, `full`, or `move`);
- progress event `diff-classified` with path, ratio, hunks, and decision;
- progress event `history` with step `candidate`, `rollback`, or `promote` and
  commit IDs.

Never include source content, API tokens, or full Git diffs in logs.

Use this exact shape for detected moves so callers do not have to decode a
path separator or tuple convention:

```json
{"moved": [{"from": "old/test.docx", "to": "new/test.docx"}]}
```

## 22. Acceptance checklist

Before declaring implementation complete:

- all existing unit tests pass;
- every opt-in DOCX acceptance test passes independently;
- `refs/llm-wiki/last-good` advances only after successful GROWI publication;
- cancelling a new unpublished file leaves no raw/wiki/state/ledger artifact;
- deleting a committed file produces one deletion commit;
- a small update during build produces two successful commits and no cancelled
  first build;
- a large update during build discards the first candidate and commits only the
  newest version;
- rename retains absolute source ID, linker IDs, and GROWI page IDs;
- a forced parse/build/publish failure leaves last-good unchanged;
- recovery is idempotent at every transaction phase;
- human GROWI edits are never overwritten during rollback;
- the live project Git working tree is clean after success, failure, and
  recovery;
- no queue SQLite, linker SQLite, lock, WAL, or candidate-worktree file is
  committed.

## 23. Deferred follow-up

After the DOCX behavior is proven, add format-specific fast text extractors and
the same classification contract for PPTX, XLSX/XLSM, and PDF. Do not change
the queue, history, commit, rollback, or identity model again for those formats.

# NEW


  ## DOCX

  Text edits:

  - Small word, sentence, paragraph, or number change → LLM patches only affected wiki pages.
  - Paragraph added/deleted/moved inside the same section → LLM updates that section.
  - Small table cell/row/column change → LLM updates that table only.
  - Heading changes, section moves, large additions/deletions, or failed page matching → rebuild the document and its links.
  - Formatting, spacing, page breaks, or document metadata with unchanged content → ignore.

  Image edits:

  - Image replaced in place → replace image and regenerate its description.
  - Image added → insert and describe it.
  - Image deleted → remove only its image block.
  - Same image moved → reuse its description and move the image block.
  - Crop/rotation changes meaning → regenerate description.
  - Resize-only change → update display only.
  - Caption change → normal text-edit path.
  - Mixed text and image changes → run both paths on the same affected pages.

  ## PPTX

  Text edits:

  - Text changed inside a shape, table, or chart → LLM patches that slide’s wiki content and refreshes its screenshot.
  - Speaker notes changed → patch notes-derived content only.
  - Small table/chart value change → update that slide’s text and visual description.

  Visual edits:

  - Shapes, text, or images moved/resized/reordered → retake that slide screenshot and regenerate its visual description.
  - Image added/replaced/deleted → refresh only that slide’s screenshot and description.
  - Same image moved → reuse image knowledge, but regenerate the slide description because relationships may have changed.
  - Theme/background/master change → refresh affected screenshots; do not rewrite text.
  - Animation/transition-only change → ignore.

  Slide structure:

  - Slide reordered → update order/navigation while reusing unchanged slide content.
  - Slide added → generate only the new slide content.
  - Slide deleted → remove its wiki content and links.
  - Many slides changed or slide matching fails → full rebuild.

  ## PDF

  Text edits:

  - Parse both versions and compare normalized page text.
  - Whitespace, wrapping, hyphenation, or table-format differences with the same facts → ignore.
  - Small factual text/table change → LLM patches affected wiki pages.
  - OCR noise with unchanged meaning → ignore.
  - Many changed pages or failed page matching → full rebuild.

  Visual edits:

  - Image/chart/diagram changed → rerun vision only for that page and update its description.
  - Image added/deleted → update only that page.
  - Layout moved but meaning stayed the same → ignore.
  - Layout changes relationships or meaning → update that page’s visual description.
  - Scanned-page change → compare OCR text and page image together.

  Safety:

  - Large but valid new parse → full rebuild.
  - Empty, corrupt, truncated, or failed parse → keep the last good wiki.
  - Full rebuild happens in a candidate copy; replace production only after success.

  After any small update: check only existing links touching changed pages; keep, update, or delete them. No global relinking.

  Currently, DOCX has incremental classification. PPTX and PDF are still always classified as full rebuilds.
