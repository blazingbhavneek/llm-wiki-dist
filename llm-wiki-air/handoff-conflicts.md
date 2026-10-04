# Human-edit conflict handling handoff

## Agreed behavior

The same rules apply to Tier 1, Tier 2, and Tier 3 updates.

| Case | What happens |
| --- | --- |
| The document update does not touch the human-edited block | Keep the human block exactly as it is. |
| The document and the human made the same change | Keep one copy. There is no conflict. |
| The document changes another part of the same block | Combine both changes and keep the human information. |
| The document and the human change the same information differently | Show the human version as the main text. Add the document version in brackets: `(Updated source document says: ...)`. Mark it as a conflict. |
| A human adds completely new information | Keep it through every future update. |
| The block moves to another page | Move the human change with the block. |
| Tier 2 regenerates the page | Regenerate the page first, then reapply the stored human change. |
| Tier 3 rebuilds the whole document | Rebuild everything first, then find and reapply every stored human change. |
| The original block disappears | Move the human information to **Retained Human Notes**. Do not delete it automatically. |
| The new document now contains the human information | Keep one copy and mark the human edit as absorbed. |
| The human deletes their own change | Stop applying it. Store a deletion record so it does not return later. |
| A later document update makes an old human change relevant again | Restore it in the correct block. |
| The system cannot decide safely | Keep the human version as the main text, show the document version in brackets, and add it to the conflict list. |

> Never remove a human change automatically. Keep it, move it, combine it, or show the document's different value in brackets. Only a human can permanently delete it.

---

## Status of this document

The implementation through TODO 6 is now in the working tree (2026-10-01).
Human capture, durable overlays, partial-publication recovery, rollout modes,
observe-only semantic proposals, activity/inventory detection, operator
resolution, and byte-verified legacy recovery are implemented. TODO 7's local
preflight/report lifecycle is also implemented. The real disposable GROWI/model
matrix has not been run because no target URL and disposable subtree have yet
been explicitly confirmed; production rollout remains disabled.

The implementer must follow the ordered TODO queue in
[Remaining work and implementer protocol](#remaining-work-and-implementer-protocol).
The original queue below records the requested review gates. The 2026-10-01
request to implement TODO 2 through the end authorized one continuous offline
implementation pass. TODO 7 still retains its explicit live-target confirmation
gate because it permits remote writes and deletes.

See [Implementation and verification](#implementation-and-verification) for the
changed files, offline results, and remaining work. The design sections below
retain the original plan and its pre-implementation baseline for context.

The plan is intentionally conservative. The most important result is not a
beautiful automatic merge. It is that no source fact or human-authored fact can
silently disappear, even when the local model is weak, unavailable, or wrong.

## Short conclusion

The generator and the human changes must become two separate layers:

```text
mounted document -> pure generated base ---+
                                        +--> checked merged wiki -> linker -> GROWI
GROWI edits     -> durable human store ----+
```

The current generated state must remain document-only. Tier 1 patches, Tier 2
page regeneration, and Tier 3 full rebuilding should continue to operate on
that pure state. After generation finishes, a separate overlay step reapplies
the human changes. This is the smallest design that preserves the existing
tier logic and makes human edits durable.

The required order is:

1. Pull and record new GROWI edits.
2. Generate or patch the pure source-based wiki.
3. Rebase stored human edits onto the new generated base.
4. Run deterministic checks, scorer, writer, judge, and bounded feedback only
   where a simple merge is not enough.
5. Link the checked effective pages.
6. Verify GROWI revisions again.
7. Publish with compare-and-swap revision IDs.
8. Commit the new snapshots and human store only with the successful candidate.

The presentation rule and processing rule are different:

- Processing: build the new document version first, then apply the human layer.
- Presentation of a conflict: human text is primary; the new source text comes
  second in brackets or a clearly labelled block.

## Pre-implementation baseline: what already worked

### Existing reverse pull

`graph/growi/client.py::GrowiPublisher.pull_changes` already:

- fetches every known page by page ID;
- compares the current GROWI revision with `metadata/pipeline.json`;
- recovers only the managed body above the page ownership stamp;
- restores GROWI permalinks to local relative Markdown links;
- writes the pulled page into the visible wiki;
- writes it into `_planning/pages` and the resumable generator state;
- changes the page sidecar hash;
- sets `human_edited = true`;
- marks the linker pending;
- updates the stored GROWI revision;
- blocks a document when the local wiki and GROWI both changed;
- blocks when the ownership stamp was removed.

`watch` calls `pull_growi_once` while the queue is idle. The default interval is
300 seconds. The existing reverse-sync research in
`docs/reverse-sync-plan.md` correctly recommends an audit-log change index and
targeted REST fetches for scale.

### Existing generator safety that should be reused

The wiki pipeline already contains the right safety pattern:

- The source is partitioned into exact, contiguous owner ranges.
- Python, not the model, decides what source material must survive.
- Sections are small, bounded model tasks.
- `check_section` mechanically protects identifiers, image placeholders,
  tables, and fenced code.
- The page judge reports missing important information.
- Judge feedback is returned to the next writer attempt.
- Every prompt, draft, judge result, and error is stored as an artifact.
- Attempts are bounded.
- If all rewritten candidates fail mechanical checks, the exact source section
  is published verbatim.
- Model calls have explicit token caps and timeouts.
- Resumable state is protected by content hashes and prompt versions.

The new human merge should copy this pattern rather than making one large
"please merge these documents" model call.

### Existing update tiers

`graph/wiki/incremental.py` and `graph/workspace/writer.py` currently implement:

- Tier 0: no meaningful source change.
- Tier 1: patch the affected generated page.
- Tier 2: delete and regenerate affected pages from the stored plan.
- Tier 3: delete the generation state and rebuild the whole document.

The page ranges, reference ranges, source hashes, provenance, linker scope, and
fallback escalation are already updated correctly for source-only changes.

### Existing transaction and rollback safety

`publisher/history.py` and `publisher/queue.py` already provide:

- immutable staged source blobs;
- a per-project Git `last-good` reference;
- candidate worktrees;
- prepared/publishing/restoring transaction phases;
- recorded GROWI revisions written during a transaction;
- candidate promotion only after successful remote publication;
- recovery and rollback after a crash or partial publication;
- supersession when a newer mount event arrives.

The human store must participate in this same candidate history. A separate
ignored database would break the atomic relationship between generated pages,
human edits, the ledger, and rollback.

## Pre-implementation baseline: gaps and risks

### 1. `human_edited` is only a boolean

It does not store:

- the old generated block;
- the exact human replacement;
- whether the operation was add, replace, or delete;
- the block identity or surrounding context;
- the GROWI user/revision/time;
- whether it was absorbed, conflicted, orphaned, or explicitly deleted.

Therefore it cannot replay the human intent after a full rebuild.

### 2. Human text is mixed into generator state

The current pull overwrites `metadata/state/.../wiki/<page>.md`. A later Tier 1
starts from that mixed page. A Tier 2 drops that page. A Tier 3 deletes the
whole state directory. The writer reports `human_edits_overwritten`, but it
does not retain or reapply the edit.

Tier 1 may happen to keep unrelated human text because the patch prompt says
not to modify other content, but there is no hard invariant proving that it
survived.

### 3. Pull happens too late in a source sync

`sync_once` parses and generates changed mount files before
`_publish_sweep` calls `pull_changes`. If the mount and GROWI both changed, the
new local page has already been made before the remote change is examined.
Publication is normally blocked, but no automatic three-way resolution exists.

### 4. The HTTP 409 retry can overwrite a late human edit

`publish_pages` catches a revision conflict, reloads the remote page, calls
`merge_marked_sections`, and retries. For the current bottom-stamp format,
`merge_marked_sections` replaces everything above the stamp. A human edit made
between the preflight pull and the PUT can therefore be overwritten.

This retry must be removed. A 409 must restart capture/rebase or abort the
document. It must never be treated as a normal retry.

### 5. Missing pages are silently skipped by pull

A GROWI deletion currently produces `page is None` and is skipped. The ledger
continues to own the page, so a later publish may recreate it. Remote deletion
must instead create a blocking conflict.

### 6. Remote moves are not classified

The current pull accepts `page.path` into the ledger. A user move needs an
explicit policy; it should not silently change ownership or be moved back.

### 7. The linker footer is derived content

Human-edit detection must not mistake bot-generated link/footer changes for
human facts. The local footer has managed comments, but `_growi_markdown`
currently strips internal comments other than the ownership stamp. The new
canonicalizer must identify generated footers/navigation and either preserve
their delimiters remotely or compare them separately.

### 8. Page identity is too coarse

There is one stable ownership stamp per page. Page filenames and linker chunk
IDs can change in Tier 3. A human edit therefore also needs a block anchor and
lineage, with a safe orphan fallback when matching is uncertain.

## Non-negotiable invariants

1. The exact human-authored change is durably stored before any generated file
   or GROWI body can replace it.
2. Generator state remains a pure function of the mounted source.
3. Human changes are never written into `state_root/wiki`.
4. Tier 1, Tier 2, and Tier 3 always run before the human overlay is rendered.
5. Active human changes are applied on every update, not only once.
6. An absorbed human change remains in history and can reactivate if a later
   source version regresses.
7. Only an explicit human revision can create a deletion tombstone. A model
   may never delete, resolve, or absorb a record by itself.
8. Uncertainty preserves information: human primary, source secondary, or
   retained note. Uncertainty never discards information.
9. Every GROWI write uses the revision that was actually inspected.
10. A 409 aborts/restarts; it is never blindly overwritten.
11. Missing ownership markers, pages, or snapshots fail closed.
12. The full text is stored. Model inputs may be divided into complete blocks,
    but content is never truncated or silently skipped.
13. Tables, code, images, numbers, units, identifiers, warnings, and error
    codes receive mechanical checks.
14. The human store is committed and rolled back with the same candidate as
    the wiki and ledger.
15. Applying the same remote revision twice is idempotent.

## State model

Use these names when implementing and testing:

- `G0`: previous pure generated block.
- `H`: the durable human operation, represented by `base_before -> human_after`.
- `G1`: new pure generated block after the next mount update.
- `E1`: effective block shown to readers after rebasing H onto G1.
- `P`: exact last published managed page snapshot.
- `R`: current remote GROWI managed page.

Remote capture derives new human intent from `P -> R`. Source processing derives
`G0 -> G1`. Rebase computes `merge(G0, H, G1) -> E1`.

Do not derive a new human edit by comparing R to the newly generated G1.
Doing that loses the common ancestor and cannot distinguish source changes
from human changes.

## Durable store

### Recommended location

Use a tracked directory outside each generator run:

```text
metadata/human-sync/
  schema.json
  documents/<source_id>.json
  pages/<page-marker-id>.json
  snapshots/<sha256>.md
  artifacts/<operation-or-edit-id>/...
```

Do not put this below `metadata/state/<document>` because Tier 3 intentionally
deletes that directory. Do not use an ignored SQLite file in version 1 because
candidate promotion and Git rollback would not move it atomically.

Add `metadata/human-sync` to every durable path list in
`publisher/history.py`:

- `_stage_durable`;
- `candidate_is_clean`;
- `checkpoint_live`.

Snapshots should be content-addressed. Atomic JSON writes already exist in
`graph/wiki/storage.py`. Validate every blob hash when loading it.

### Human edit record

Each record needs at least:

```json
{
  "schema_version": 1,
  "edit_id": "hedit-...",
  "source_id": "...",
  "document_id_seed": "...",
  "operation": "add|replace|delete",
  "status": "active|absorbed|conflict|orphaned|deleted|resolved|legacy_pinned",
  "anchor": {
    "page_marker_id": "...",
    "old_local_path": "...",
    "page_title": "...",
    "heading_path": ["...", "..."],
    "ordinal": 3,
    "before_neighbor_hash": "...",
    "after_neighbor_hash": "...",
    "old_source_ranges": [[10, 24]]
  },
  "base_before_blob": "<sha256>",
  "human_after_blob": "<sha256>",
  "human_delta": [],
  "created_from_revision": "...",
  "last_seen_revision": "...",
  "created_at": "...",
  "updated_at": "...",
  "last_applied_source_sha256": "...",
  "current_target": {},
  "conflict": {}
}
```

Keep both complete block bodies, not only a patch. The patch is useful for a
deterministic three-way merge; the full bodies are necessary after a major
rewrite.

### Published page baseline

For each page store:

- page ID, marker ID, local path, and remote path;
- the last accepted GROWI revision;
- exact remote managed Markdown as GROWI stored it;
- canonical local effective Markdown before permalink/image conversion;
- hashes of both;
- source ID and source hash;
- marker/schema version;
- whether writes are blocked;
- latest observed revision separately from latest successfully published
  revision.

The ledger can contain the hashes/pointers, while the complete bodies live in
`snapshots/`. Bump the ledger schema version while continuing to read versions
1-3.

## Block model and anchors

Version 1 should be conservative and reuse current parsing:

1. Remove or separate the bot-owned link footer.
2. Keep fenced code, tables, and image units atomic using the existing Markdown
   block/fence scanners.
3. Split the editable body at H2 sections using
   `graph.linker.chunks.split_page`.
4. Treat the title/intro before the first H2 as its own block.
5. Within a changed H2, use line hunks for a more precise operation, but store
   the complete section before and after.

A block identity must not be only `filename + ordinal`. Store lineage and
match using, in order:

1. an existing stable block/edit marker;
2. exact previous block text;
3. exact heading path plus matching neighbor hashes;
4. exact block text anywhere in the same document;
5. deterministic lexical similarity using headings, identifiers, numbers,
   entity names, and text shingles;
6. a Jev same-topic score over only the top deterministic candidates;
7. otherwise `orphaned`.

Newly published pages should gradually receive hidden stable edit/conflict
markers around human-managed blocks. Do not force a global marker-format
republish. Existing pages can use structural matching; a normal successful
publish can add markers page by page.

Markers are hints, not authority. If markers are missing but text is intact,
repair them. If markers and text are both changed ambiguously, preserve the
whole remote region as a conflict or legacy-pinned block.

## Remote capture algorithm

Run this under the existing project lock, before source generation:

1. Load the ledger and human store and validate all referenced blobs.
2. Fetch only known changed page IDs when an activity detector is available.
   The first implementation may reuse the existing known-page fetch.
3. If a page is missing, record `remote_deleted`, block the document, and do
   not recreate it.
4. If its path changed, record `remote_moved` and block until policy or human
   resolution.
5. If its revision equals the accepted revision, do nothing.
6. Recover the managed body with the ownership stamp.
7. If the stamp is missing/malformed, save the complete remote body, create a
   blocking conflict, and write nothing.
8. Compare exact remote form first. This distinguishes an outside-managed-tail
   change from a managed change.
9. Canonicalize known permalinks, publisher markers, images, and derived link
   regions without losing unknown content.
10. Compare canonical P with canonical R and extract page/block operations.
11. Give every operation a deterministic ID based on page marker, baseline
    revision, observed revision, and hunk ordinal so retries cannot duplicate
    it.
12. If the human removed an already stored human addition/replacement, write a
    deletion tombstone.
13. If the human deleted generated source information, write a durable
    suppression operation. Do not simply erase the old record.
14. Save the new human records and observed remote snapshot atomically.
15. Do not write R into the generator run state.
16. Render the effective local wiki from the current pure base plus the store,
    update `_planning/pages` for linker input, and mark linking pending.

Text below the page ownership stamp may remain remote-only for backward
compatibility. If it should become durable/imported, make that a separate
explicit policy; do not silently change its meaning during this work.

## Source update and rebase algorithm

### Common flow for every tier

1. Capture remote edits first.
2. Run the existing source update unchanged to produce pure generated pages.
3. Read active, absorbed, conflicted, orphaned, and tombstoned records for the
   source ID.
4. Match each record to the new pure pages.
5. Rebase each record.
6. Render effective pages into `wiki/<document>`.
7. Write effective originals to `_planning/pages` before the linker reads
   them.
8. Return the effective changed-page set.
9. Union that set with the existing Tier 1/Tier 2 changed-page set for linker
   and selective publishing.
10. Generate a small conflict dashboard containing status and links, not
    duplicate copies of all conflicting text.

### Deterministic three-way cases

For each matched block:

- `G1 == G0`: apply `human_after` exactly.
- normalized `G1 == human_after`: mark absorbed and show one copy.
- human delta and source delta touch different ranges: apply both
  deterministically.
- both make the same exact replacement: show one copy.
- both touch the same range differently: conflict.
- no safe target: orphan.

An absorbed record stays stored. If a later G2 no longer contains the human
information, reactivate and rebase it.

### Conflict rendering

For short prose:

```markdown
Maximum temperature is 60°C. (Updated source document says: 55°C.)
```

For tables, lists, code, images, or long sections:

```markdown
### Human/GROWI version — currently preferred

<exact human block>

> **Updated source document says:**
>
> <exact new generated block>
```

Do not ask the writer to paraphrase the bracketed source value. Keep both
verbatim when accuracy is uncertain.

### Orphan rendering

If no target is safe, create or update a generated
`99-Retained-Human-Notes.md` page for that document. Include:

- the exact human text;
- old page/heading context;
- why it could not be placed;
- first and last source versions;
- a stable hidden edit ID.

Retry orphan matching on every later source update. The note remains until a
human deletes/resolves it.

## Tier-specific behavior

### Tier 0

- Pure generated base is unchanged.
- A newly pulled human edit is stored and rendered.
- No source writer runs.
- Existing active human records remain.
- If the remote revision is also unchanged, do nothing.

### Tier 1

- Run the current patch only on the pure generated page.
- If the patch does not touch a human block, copy the human block exactly.
- If source and human hunks are disjoint, combine them mechanically.
- If both produce the same result, keep one copy and mark absorbed.
- If they overlap differently, human is main and the source version is
  bracketed and recorded as a conflict.
- The Tier 1 model never receives authority to delete human records.

### Tier 2

- Regenerate the source page exactly as today.
- Match all human edits formerly attached to that page across the complete
  document, because the block may have moved.
- Reapply, absorb, conflict, or orphan each edit.
- Publish only effective pages that actually changed, plus linker peers.

### Tier 3

- Capture GROWI first.
- Preserve `metadata/human-sync`.
- Delete/rebuild only generator state.
- Match records across every new page.
- Reapply or classify every record.
- Any unmatched record goes to retained notes.
- Never publish the raw Tier 3 output before overlay validation finishes.

### Repeated updates

Every render starts from a fresh pure generated base plus the same durable
records. It never starts from the previous merged page. Therefore a human
addition is not duplicated after three or four mount updates.

### Source move/delete

- Store records by stable `source_id`, not mount path, so a recognized source
  move carries them with it.
- A mount deletion archives the human journal; it does not erase it.
- Do not automatically publish archived human text into an unrelated document.
- If the same source identity returns, offer/reapply its journal.

### GROWI delete/move

- GROWI page delete: block recreation and report it.
- GROWI page move: block move-back and report old/new paths.
- New unmanaged page under the project root: report it, but do not invent a
  source mapping.

## Weak local AI: safety pipeline

The model is an assistant, never the owner of preservation decisions.

### Layer 1: deterministic fast path

Use no model for:

- unchanged target blocks;
- exact human replay;
- exact same-result absorption;
- disjoint line hunks;
- exact block moves;
- deletion tombstones;
- safe conflict fallback.

Most updates should stop here.

Reuse:

- `line_hunks` and `BoundaryMap`;
- Markdown fence/table/image scanners;
- `code_tokens`;
- image identity helpers;
- `check_section`-style validators;
- atomic writes and content hashes.

### Layer 2: deterministic candidate shortlist

For a moved/rewritten block, rank possible new targets using:

- heading-path equality;
- neighbor hashes;
- identifiers and exact numbers/units;
- normalized token overlap;
- character shingles;
- prior source range/page relation.

Never send the whole wiki to a model. Send only the complete old block and a
small complete candidate list.

### Layer 3: Jev scorer

Use the existing Jev engine only as a scorer for the shortlist. Ask separate
yes/no questions:

1. Is this candidate about the same specific subject as the old block?
2. Does the new source already contain the information added by the human?
3. Do the human and source versions make incompatible claims?

Fail closed:

- scorer unavailable -> no semantic auto-match;
- input too long -> divide by complete paragraph/atomic block and keep all
  parts;
- close scores or low confidence -> orphan/conflict;
- never let a score create a deletion.

Initial thresholds must first run in observe mode. A suggested conservative
starting rule is top probability at least 0.90 with at least 0.15 margin over
the second candidate. Do not treat these numbers as calibrated until real
edits have been labelled.

### Layer 4: bounded merge writer

Only use a writer when deterministic merging cannot produce good readable text
but the target match is strong.

The structured input contains:

- G0 old generated block;
- exact human change and protected human spans;
- G1 new generated block;
- operation ID;
- required conflict format;
- all identifiers, numbers, units, images, tables, and code that must survive;
- feedback from the previous attempt.

The writer returns:

- classification proposal;
- exact target block ID;
- merged Markdown;
- which source/human edit IDs it claims to have applied;
- an explanation only for artifacts/debugging.

Human-authored changed spans should normally be protected verbatim. A writer
may improve connecting prose but may not rewrite the human assertion.

Use the current bounds: three writer attempts, explicit token cap, request
timeout, stored prompt/draft/error artifacts, and non-zero retry temperature.

### Layer 5: mechanical validation

Reject a candidate if any of these fail:

- every required edit ID is accounted for exactly once;
- protected human spans are present exactly;
- human-added numbers, units, identifiers, warnings, and error codes survive;
- required new-source tokens survive or appear in the bracketed source block;
- table rows and fenced code are exact;
- images occur exactly once;
- no unknown image marker appears;
- Markdown ownership/conflict markers are balanced;
- no duplicate human edit block appears;
- the candidate stays inside the selected target;
- no unsupported identifier/number is introduced.

Feedback must name the exact missing item and feed it to the next writer
attempt, as the current section writer does.

### Layer 6: independent judge

Run a small structured judge after mechanical checks. It must report:

- missing human information;
- missing new-source information;
- whether human text is visibly primary;
- whether a contradiction is clearly shown rather than hidden;
- unsupported claims;
- a coverage score.

The judge result is advisory unless it reports a problem. A clean judge may
accept; a bad/unavailable judge may never authorize deletion. Feed its exact
omissions back to the next attempt.

Jev and the LLM judge are not fully independent truth sources, so deterministic
invariants always win.

### Layer 7: guaranteed fallback

If writer, scorer, or judge fails after the bounded attempts:

1. Keep `human_after` exactly as the main block.
2. Add G1 exactly in the labelled bracket/block.
3. Mark conflict.
4. Continue safely if all markers and storage are valid.

If storage, ownership, or revision validity is not known, abort publication
instead. This separates a content-model failure, which has a safe visible
fallback, from a state-integrity failure, which must fail closed.

### Prompt-injection guard

Source documents and GROWI text are untrusted data. Prompts must explicitly
label them as quoted content, never instructions. Structured schemas, protected
spans, deterministic output validation, and the verbatim fallback are the real
protection; prompt wording alone is insufficient.

## Publication safety

Before the first PUT in a document batch:

1. Fetch every affected known page.
2. Verify page ID, path policy, ownership stamp, and expected revision.
3. If any changed since capture, abort the document before writing any page.

For each PUT:

- use the exact inspected revision ID;
- record every bot-produced revision through the existing transaction callback;
- on HTTP 409, do not merge and retry;
- fetch/save the new remote body for diagnosis, mark the batch stale, and
  restart capture/rebase on the next attempt.

After all affected pages succeed:

- store exact returned remote managed bodies;
- store canonical local effective bodies;
- update ledger revisions and snapshot hashes;
- commit/amend the candidate;
- promote it.

If publication partially succeeds, reuse the existing rollback path and its
known-revision checks. If rollback sees an unknown human revision, stop rather
than overwrite it.

A later recovery can identify a bot-written but uncommitted page by exact body
equality with the prepared effective snapshot. It must not assume every unknown
revision is human or every unknown revision is bot.

## Human resolution lifecycle

Conflict blocks need stable hidden conflict/edit IDs.

On the next GROWI edit:

- Human leaves the human version and removes the source candidate: resolve as
  keep-human; the active human overlay remains.
- Human removes the human version and keeps the source version: write a
  deletion/accept-source tombstone.
- Human edits both into one final statement: create a new human replacement
  that supersedes the old conflict.
- Human deletes a human-only addition: tombstone it.
- Human deletes the entire conflict marker structure ambiguously: save the
  complete remote block and require review; do not guess.

Generate a project conflict dashboard containing only:

- status;
- document/page link;
- heading;
- conflict/edit ID;
- first/last seen time;
- resolution state.

Keep the complete variants inline at the affected page or in retained notes,
not duplicated on the dashboard. This reduces contradictory duplicate text in
search results.

## Backward-compatible migration

Do not globally republish before capturing current remote state.

For each existing page:

1. Fetch the current page and validate its ownership stamp.
2. If revision and local content are known clean, store them as the initial P
   and effective baseline.
3. If the sidecar has `human_edited = true`, the old generated base may already
   be lost. Do not pretend the current mixed page is pure.
4. Try to recover a pre-pull generated body from committed project history only
   when exact hashes prove it.
5. Otherwise create `legacy_pinned` containing the full current managed page.
6. On the next rebuild, show that pinned human page/content as primary and the
   new generated source as a conflict or retained note until reviewed.
7. Never drop a legacy-pinned record automatically.

Roll out hidden block markers gradually on successfully published pages.
Do not bump `MARKER_FORMAT` merely to force a full republish; that current
mechanism would be too risky before the new capture path is active.

## Minimal file-level implementation plan

### New: `publisher/human_changes.py`

Keep most new logic here:

- validated store models and schema migration;
- snapshot read/write and hash checking;
- canonical page/block parsing;
- P-to-R remote delta extraction;
- edit/tombstone creation;
- anchor matching and deterministic scores;
- three-way hunk merge;
- overlay rendering;
- conflict/orphan rendering;
- writer/judge loop and safe fallback;
- structured results: changed pages, conflicts, blocked documents, events.

Keeping this in `publisher/` avoids changing the upstream generator engine.

### `graph/growi/client.py`

Make narrow changes:

- expose/factor fetching of known changed pages;
- retain existing ownership/permalink helpers;
- stop writing pulled text into generator state;
- classify missing/moved pages rather than skipping;
- remove blind 409 retry;
- optionally preserve whitelisted hidden block/footer markers;
- accept a callback/result needed to record exact returned published bodies.

Keep `pull_changes` as a compatibility wrapper during migration if tests or
callers still use it.

### `publisher/pipeline.py`

- Capture remote changes before source removal/generation.
- Apply the human overlay after `write_wiki_pages` and before `run_linkers`.
- Union overlay-changed pages with current incremental page scope.
- Replace late `pull_changes` with a revision preflight.
- Persist snapshots only after successful publication.
- Make `pull_growi_once` capture/store/render without mutating pure state.
- Return structured conflict/orphan counts in `done`.

### `publisher/ledger.py`

- Add snapshot/effective hashes, observed revision, and write-blocked state.
- Read old schema versions.
- Keep complete bodies out of the ledger.

### `publisher/history.py`

- Track `metadata/human-sync` in candidate cleanliness, staging, checkpoint,
  promotion, and restore.

### `graph/workspace/writer.py`

Avoid changing tier algorithms. Only remove the assumption that
`human_edited` in generator sidecars is the durable mechanism. The new overlay
stage lives outside this writer.

### `publisher/queue.py`

Reuse the existing transaction. Only add structured stale-revision retry
handling if needed. Do not create a parallel transaction system.

### `graph/config.py` and `main.py`

Add one rollout mode rather than many knobs:

- `human_sync_mode = off|observe|apply`, default `off` during rollout.
- Add a one-shot `pull` command after safe capture exists.

Keep matching thresholds as versioned constants until observe-mode data
calibrates them.

### Tests

Add `tests/test_human_conflicts.py` and extend current pipeline/GROWI tests.
Use fake models, fake Jev scores, and fake GROWI clients; no network.

## Implementation phases

### Phase 0 — fixtures and invariants

- Save representative generated/remote page pairs: add, replace, delete,
  same change, contradiction, moved block, rewritten document, table, code,
  image, Japanese prose, and malformed markers.
- Write table-driven expected classifications.
- Document exact conflict Markdown format.

Done when every agreed case has an input and exact expected output.

### Phase 1 — close overwrite holes

- Remove blind 409 retry.
- Classify missing and moved pages as blocking.
- Add preflight revision checks.
- Add observe-only structured remote-change results.

Done when a late human edit, deletion, move, or marker removal cannot be
overwritten by any publish path.

### Phase 2 — durable baseline and capture

- Add tracked store/snapshots and history integration.
- Baseline clean pages.
- Implement P-to-R canonical diff and idempotent human records.
- Stop reverse pull from mutating generator state.
- Add legacy-pinned migration.

Done when a GROWI edit is stored with old/human bodies and survives restart,
candidate failure, rollback, and repeated polling.

### Phase 3 — deterministic overlays

- Parse blocks.
- Implement exact replay, exact absorption, disjoint three-way merge,
  conflict fallback, tombstones, and retained notes.
- Apply overlay after every tier and before linking.
- Compute effective changed-page scope.

Done when all agreed cases work without any model call where deterministic
logic is sufficient.

### Phase 4 — scorer, writer, judge, feedback

- Add deterministic candidate shortlist.
- Add Jev scoring in observe mode.
- Add structured block writer with protected human spans.
- Add mechanical validators.
- Add structured judge and feedback loop.
- Cache decisions by G0/H/G1 hashes and prompt/scorer versions.
- Keep guaranteed verbatim conflict fallback.

Done when model/scorer failure produces the exact safe fallback and can never
lose either side.

### Phase 5 — conflict resolution and operator view

- Add stable conflict markers.
- Parse explicit human resolutions/tombstones.
- Generate conflict dashboard and retained notes.
- Add one-shot `pull` and structured progress events.

Done when a human can keep human, accept source, combine, or delete through
GROWI and the decision remains stable on later updates.

### Phase 6 — scale and recovery detector

- Implement the activity/audit-log detector from
  `docs/reverse-sync-plan.md`.
- Fetch only affected page bodies.
- Persist timestamp plus same-timestamp activity IDs.
- Add rare complete path inventory.
- Classify create/delete/rename.

Done when normal cost follows edit volume and the recovery inventory repairs
missed events.

### Phase 7 — staged rollout

1. `off`: current behavior except critical 409 protection.
2. `observe` on one project: record proposed operations and compare with GROWI
   revision history; do not alter merge output.
3. Apply only exact replay/exact same/disjoint cases.
4. Enable conflict/orphan fallback.
5. Enable scorer matching after labelled calibration.
6. Enable writer-improved merges last.

Never enable a more permissive stage until the previous stage has zero silent
loss on the labelled corpus.

## Required tests

### Store and idempotency

- same revision polled twice creates one edit;
- restart preserves records;
- invalid/missing blob blocks writes;
- atomic-write interruption retains the previous valid state;
- candidate rollback restores store and wiki together;
- candidate promotion moves them together.

### Tier behavior

- human add survives Tier 0, 1, 2, and 3;
- human replace survives four consecutive mount updates;
- Tier 1 change outside human block leaves it byte-identical;
- Tier 1 disjoint change combines both;
- Tier 1 same change leaves one copy;
- Tier 1 contradiction uses human main/source bracket;
- same cases for Tier 2 and Tier 3;
- Tier 3 page split, combine, rename, and reorder;
- unmatched edit goes to retained notes and is retried later;
- absorbed edit reactivates if source regresses.

### Deletion/resolution

- delete human addition creates tombstone;
- delete generated fact creates suppression;
- tombstoned edit never returns after multiple updates;
- keep-human/accept-source/combine resolution;
- malformed resolution markers fail closed.

### Content integrity

- exact numbers and units;
- identifiers and error codes;
- tables and fenced code;
- image identity and count;
- links/permalinks;
- Japanese headings/prose;
- large complete blocks without truncation;
- prompt-injection-looking text remains data.

### GROWI races

- remote edit before capture;
- remote edit during generation;
- remote edit after preflight;
- PUT 409;
- missing page;
- renamed page;
- marker removal;
- partial multi-page publish and rollback;
- unknown revision during rollback is never overwritten.

### Model failure

- writer timeout;
- invalid schema;
- missing protected text;
- invented number/identifier;
- judge timeout;
- Jev unavailable;
- Jev ambiguous scores;
- all attempts fail -> exact human/source fallback.

### Linker/index interaction

- linker reads effective merged pages, not pure base;
- pure generator state stays human-free;
- changed-page scope includes overlay changes;
- conflict dashboard does not duplicate full conflicting bodies in indexes;
- retained notes are published and remain linked to their document.

## Observability

Emit structured events without logging full sensitive text:

- remote revisions examined/changed;
- operations added/replaced/deleted;
- exact/disjoint/model/fallback merges;
- absorbed/conflict/orphaned/reactivated counts;
- match scores and score margins;
- writer/judge attempts and reasons;
- blocked documents;
- revision races/409s;
- rollback/recovery result.

Store full prompts/results only in local per-edit artifacts. Conflict records
must include source hash, remote revision, prompt/scorer versions, and chosen
path so a later audit can reproduce the decision.

## Decision record and rationale

This section records auditable engineering reasons, not private model
chain-of-thought.

### D1: pure base plus overlay

Chosen because it leaves the proven tier engine intact and prevents Tier 2/3
from owning human content. Mixing human text into generator state cannot
provide the required survival guarantee.

### D2: tracked JSON/blobs before SQLite

Chosen because project Git candidates already atomically commit files under
durable paths. The existing ignored SQLite databases are copied/promoted by
special code and would add another transaction problem. JSON also makes
migration and incident inspection simple.

### D3: pull before build, overlay after build

Pull-first captures the true human side before local output moves. Build-first
within the merge creates the correct new source base. Applying overlays last
protects humans across all tiers.

### D4: deterministic rules are authoritative

The local model is weaker and semantic scores are not calibrated for this new
task. Exact equality, line-hunk separation, protected spans, content hashes,
and revision checks are more reliable. Models improve readability and matching
only behind these gates.

### D5: uncertainty is visible, not destructive

Keeping human primary and source secondary may produce a temporary verbose
page, but it preserves both truths for review. Silently selecting one can cause
irrecoverable operational misinformation.

### D6: no blind 409 retry

A revision conflict is evidence that the common ancestor is stale. Retrying a
replacement merge without recapturing breaks the core three-way assumption.

### D7: disappearance is not irrelevance

A major source rewrite may intentionally remove, move, or accidentally omit
information. Only a human can decide that a stored human correction is no
longer needed. Therefore unmatched edits become retained notes.

### Rejected approaches

- Keep only `human_edited = true`: no replayable intent.
- Store only a textual diff: cannot safely rebase after a major rewrite.
- Treat the current merged page as next generation input: duplicates and
  contaminates source provenance.
- Let an LLM merge complete pages/documents: too much context and no hard
  preservation proof.
- Auto-drop a human edit when its source block disappears: violates the main
  requirement.
- Keep the store in an ignored database: unsafe across candidate rollback.
- Retry PUT 409 after refreshing and replacing managed text: can clobber a
  human edit.
- Use Socket.IO bodies as truth: events can be missed; REST revision/body must
  remain authoritative.

## Open issues to decide during implementation

1. Whether text below the ownership stamp remains remote-only or becomes an
   imported human-note region. Recommended first version: keep current
   remote-only behavior.
2. Exact long-block conflict formatting. Recommended: labelled blockquote,
   while short facts use the requested parentheses.
3. Whether remote page moves should be accepted after human confirmation or
   always moved back. Recommended: block and require explicit resolution.
4. How a user explicitly accepts source in GROWI. Recommended: stable hidden
   conflict IDs plus deterministic removal/edit interpretation; add checkboxes
   only if raw marker editing proves confusing.
5. Jev thresholds. They require observe-mode calibration; do not guess them
   into apply mode.
6. Whether conflict/retained pages should be excluded from normal search
   indexes. Recommended: dashboard has only links/status; retained content
   stays searchable but is clearly labelled non-source human information.
7. Old `human_edited` pages without a recoverable pure ancestor. Recommended:
   legacy-pin them; never manufacture a base.

## Acceptance criteria

The feature is ready only when all are true:

- A human add/replace/delete survives Tier 1, Tier 2, Tier 3, restart, and at
  least four consecutive mount updates.
- A human edit is never duplicated.
- Same changes converge to one copy.
- Different same-block changes show human primary and source secondary.
- A major rewrite cannot erase a human edit.
- Explicit human deletion stays deleted.
- Model, judge, scorer, or network failure cannot lose either side.
- A late GROWI edit and HTTP 409 cannot be overwritten.
- Page deletion/move/marker damage blocks recreation or overwrite.
- Pure generator state contains no human overlay.
- Linker and published pages use the checked effective version.
- Candidate commit, promotion, rollback, and recovery include the human store.

## Verification performed while researching

- `tests.test_update_tiers`: 34 tests passed.
- The focused existing reverse-pull/ownership tests in
  `tests.test_pipeline_scope`: 5 tests passed.
- The combined/full suite did not finish within a 90-second diagnostic timeout
  after emitting existing test-path log messages. This happened before any
  production code change and needs separate investigation; it is not evidence
  that this plan changed behavior.
- At that research stage, no production files had been edited.

## Implementation and verification

### Implemented behavior

- `publisher/human_changes.py` stores complete G0/human bodies, exact accepted
  and published remote/local snapshots, deterministic edit IDs, tombstones,
  source identity, structural anchors, and capture history under tracked
  `metadata/human-sync/`. Snapshot hashes and schemas are checked before
  publication. Journals migrate from document seeds to stable source IDs;
  reusing a path with a different source identity does not inherit its edits.
- `GrowiPublisher.pull_changes` captures edits before source processing and
  renders the effective wiki and `_planning/pages`. It does not modify pure
  generator pages or their sidecar hashes. Resumed generation uses the pure
  ancestor saved with the published page, rather than an already updated base.
- All tiers, including Tier 0, reapply the journal after source processing.
  Exact replay, same-result absorption, disjoint changes, human-primary/source-
  secondary conflicts, moved blocks, durable suppression, and retained notes
  use deterministic rules. Absorbed and orphaned edits can reactivate. Uncertain
  matches preserve complete human blocks instead of selecting a semantic target.
- Code, tables and images are atomic merge units. Numeric/sign overlap is
  treated as a conflict. Marker and footer examples inside fenced code are
  treated as content, including nested fence examples. The linker and publisher
  preserve complete human regions. Remote-only text below the ownership stamp
  keeps its code and comments.
- Explicit GROWI revisions can keep the human version, accept the raw or quoted
  source version, replace an existing correction, or delete a retained note.
  Deletion records remain in history. Ambiguous or damaged marker edits block
  publication and save the full observed body.
- Legacy pages without a provable pure ancestor are pinned in full and trigger
  a fresh source rebuild. A newly discovered revision is not proof that its
  body is clean: a mismatch with the expected local transport form is pinned.
  Repeated pre-publication edits update the pin without duplicating it.
- `98-Human-Conflicts.md` contains statuses and links; complete variants remain
  on their page or in `99-Retained-Human-Notes.md`. Auxiliary names receive a
  suffix if they collide with a source-generated page.
- Publication inspects the complete affected batch before mutation and uses
  those exact revision IDs. HTTP 409 performs no replacement retry. Missing
  pages, remote moves, changed revisions, damaged ownership, or invalid store
  data fail closed. Source moves and automatic page cleanup also use inspected
  revisions.
- The store participates in candidate cleanliness, commits, promotion and live
  checkpoints. Failed candidates restore the old source with newly captured
  human edits still applied. Recovery recognizes an unrecorded bot write only
  when its body exactly equals the saved prepared request body. Unknown human
  revisions block restoration. A missing page can be recreated during rollback
  only when a successful bot deletion was recorded; a lost deletion response
  requires review.
- `python main.py --project <project> pull` performs one capture/render/checkpoint
  pass. Existing idle watcher pulls use the same path. Linking is marked pending.
- `human_sync_mode=off|observe|apply` is validated centrally and defaults to
  `off`. Observe artifacts contain hashes and reason codes rather than page
  bodies; apply re-fetches the current revision before capture.
- Observe mode can run the bounded Jev shortlist scorer and independent
  structured writer/judge adapters. Mechanical validation remains authoritative,
  cache keys cover inputs and versions, and every failure returns the exact
  deterministic conflict artifact. Semantic output is never authoritative.
- Activity polling uses a tracked overlap cursor, deterministic pagination and
  a complete boundary inventory fallback. Cursor advancement happens only after
  the ledger/operator state is durable and is included in the Git checkpoint.
- `python main.py --project <project> human status|resolve|recover-legacy`
  exposes redacted operator status, revision-checked actions, and unique
  byte-verifiable Git ancestry recovery. `pull --inventory` forces an audit.

### Files

| File | Change |
| --- | --- |
| `publisher/human_changes.py` | New durable journal, capture, matching, merge and overlay implementation |
| `graph/growi/client.py` | Safe reverse capture, batch revision preflight, protected formatting, checked move/delete and prepared snapshots |
| `graph/workspace/writer.py` | Legacy pinning and overlay after every tier |
| `graph/linker/render.py` | Exact protection of human regions during derived linking |
| `publisher/pipeline.py` | Capture before generation/removal/move, effective publication scope, recovery and checkpoints |
| `publisher/history.py` | Track the human store with durable candidate state |
| `publisher/ledger.py` | Write schema 4; continue reading schemas 1–3 |
| `main.py` | One-shot `pull` command |
| `tests/test_human_changes.py` | Offline lifecycle, tier, migration, race and transaction regression tests |
| `tests/test_update_tiers.py` | Replace the old human-overwrite expectation with preservation/rebuild checks |
| `.gitignore`, `README.md` | Track the new tests and document the architecture and command |
| `graph/config.py` | Validated `off|observe|apply` policy plus detector audit/overlap settings |
| `publisher/activity.py` | Durable activity cursor and correctness-first boundary inventory |
| `publisher/human_semantic.py`, `publisher/prompts/` | Strict observe-only scorer/writer/judge contracts, validation, cache, prompts and runtime adapters |
| `publisher/legacy_recovery.py` | Unique Git ancestor verification and blob/commit evidence |
| `publisher/live_verification.py` | Redacted disposable-boundary plan, confirmation gate and E2E report lifecycle |
| `tests/test_human_sync_rollout.py` | Offline rollout, semantic, activity, operator, legacy and live-gate tests |

Existing changes to `.env` and the project test INIs were left intact.

### Offline verification

Run the focused coverage with:

```bash
.venv/bin/python -m unittest tests.test_human_sync_rollout tests.test_human_changes tests.test_update_tiers tests.test_growi_images tests.test_pipeline_scope.OwnershipStampTest -q
```

The focused command passed **122 tests**. It covers repeated updates/restarts,
Tier 0/1/2/3, exact generator-state
preservation, absorption/reactivation, moves, tombstones, retained-note edits,
large Japanese text and images, malformed markers, legacy migration, batch
preflight, HTTP 409, confirmed bot deletion, late human deletion/edit, prepared
body recovery, candidate commit/promotion, partial-publication rollback, rollout
modes, strict semantic fallback, activity pagination/inventory, operator actions,
verified legacy ancestry, and the live boundary gate.
Publication failure messages in the transaction tests are deliberately injected.

The 21 remaining `DiffPipelineSafetyTest` cases passed, for **143 passing
selected tests** in total. Four broader cases were
also reproduced as failures on unmodified `HEAD` in an isolated Git archive:

1. `test_incremental_linker_keeps_valid_peer_untouched_and_skips_search`
2. `test_incremental_scope_with_many_old_edges_checks_only_visible_ones`
3. `test_neo_incremental_linker_preserves_entity_direction_and_checks_only_visible_edges`
4. `test_rename_plus_edit_keeps_old_digest_until_the_new_blob_is_parsed`

Those baseline failures were not changed as part of this feature. The older,
ignored `test_pipeline_scope.py` full class also timed out; some fixtures still
expect generator-state overwrite or supply no compare-and-swap revision IDs.
Its separate ownership-stamp class passes in the focused command above.
Syntax compilation and `git diff --check` passed. No live GROWI/model services
were used and no external pages were changed during implementation.

### Remaining work and implementer protocol

The deterministic preservation core above is implemented, but the feature is
not finished. Complete the following TODOs strictly in order. Each numbered
item is a separate review phase.

For every TODO:

1. Work only on that TODO. Do not include opportunistic refactors or start the
   next item.
2. Implement both the production code and the focused offline tests for it.
3. Run the focused tests and `git diff --check`.
4. Record the files changed, commands run, results, and any remaining limitation
   under that TODO.
5. Stop and exit early. Wait for the user to ask the reviewer to inspect the
   code and tests before proceeding.

#### TODO 1 — recover standalone partial publication safely

This is the next and only task to implement now.

**Current review status: ACCEPTED FOR NOW. TODO 1 is closed.** Exact prepared
writes, late edits after the partial write, lost create/update responses, failed
sweep checkpointing, and the HTTP 409 race have focused offline coverage. The
reviewer also replaced the unsafe derived pure ancestor with the writer's actual
tracked pure generated blob. The remaining ambiguity/hard-interruption cases
listed below are explicitly deferred hardening, not a reason to reopen TODO 1.

`publish_only` can write an early page and then fail on a later page before the
returned revisions reach the ledger. Prepared request bodies are saved by
`GrowiPublisher.publish_documents`, but exact prepared-body recovery is only
used by candidate rollback. A later pull can therefore mistake an unrecorded
bot revision for a human edit, and a failed standalone publish can leave the
tracked human store dirty relative to `last-good`.

Required code:

- make standalone publication recover or reconcile exact prepared bot writes;
- never classify an exact prepared bot body as human intent;
- preserve and capture a genuinely different late human revision instead;
- handle an existing-page update and a newly created page in an existing
  document, including a lost create/update response;
- keep ledger, page baselines, prepared records, and Git checkpoint state
  consistent after success, failure, and retry;
- retain fail-closed behavior when the observed body is neither a known
  revision nor an exact prepared body.

Required tests:

- inject a two-page publish where page one succeeds and page two fails;
- retry through the real standalone `publish_only`/pull path and prove page
  one's bot body is not recorded as a human edit;
- repeat for creation of a new page in an existing document;
- simulate a lost response after the remote accepted the write;
- prove a human edit made after the partial write is not adopted as bot output
  and is never overwritten;
- prove the project is clean/checkpointed or intentionally fail-closed after
  each recoverable outcome.

Completion gate: the new focused tests pass, the existing 94-test conflict
suite still passes, and the implementer stops for review.

##### Deferred publication-recovery hardening (not a TODO 1 gate)

TODO 3 should incorporate the remaining state-machine hardening below while it
builds the atomic/recovery acceptance matrix. TODO 5 should incorporate the
unledgered-page discovery parts while it builds the full inventory. A revision
change by itself proves only that *some* write happened.

1. When preparing each create/update, persist an attempt record containing:
   marker ID, local path, remote path, inspected page ID/revision (empty only for
   create), exact prepared transport body, exact effective local body, and the
   exact pure generated page blob if one exists. Give the attempt a stable ID.
   Never manufacture a pure ancestor with `strip_regions(prepared_local)`: the
   prepared local page may already contain protected human overlays and linker
   output. Read the pure blob from the document's generated-page map.
2. Clear a prior attempt error only when a new attempt is durably recorded.
   Errors and confirmations must be scoped to the attempt ID so stale metadata
   cannot approve or reject a later attempt.
3. Add a per-page success callback at the point where create/update actually
   returns successfully. Persist the returned page ID, revision, path, and exact
   body before publishing the next page. This is recovery evidence even if a
   later page fails and the batch result never reaches the ledger. Keep existing
   callback compatibility or introduce a separate callback carrying both the
   local item and returned page.
4. If the PUT returns 409, persist `revision_race` for that attempt and the
   observed page, and mark that attempt rejected. The prepared body did not
   land. A later pull must use the last accepted published baseline to capture
   the human revision, or fail closed if that baseline is unavailable. It must
   never promote or diff against the rejected prepared body.
5. If a create/update response is lost or otherwise ambiguous, inspect remote:
   - exact path, ownership marker, page identity where known, and exact prepared
     body: adopt it as bot output;
   - same inspected revision/body: the write did not land; retire the attempt
     and keep the old baseline;
   - any different body without a confirmed successful intermediate revision:
     causality is unknown; retain all evidence and fail closed. Do not guess
     that the human revision was based on the prepared body;
   - for an unledgered create, only the exact owned prepared body may be adopted.
     A different body at that path remains preserved and blocked for review.
6. If an earlier page returned success but a later page failed, the per-page
   confirmation proves the prepared revision existed. On the next pull:
   - if remote still equals it, adopt it without a human operation;
   - if remote is a later revision, canonicalize and capture the delta using the
     prepared remote body, prepared effective local body, and saved *pure*
     generated ancestor. Only the later human delta may enter the journal.
7. Settling an attempt must update the page baseline and ledger consistently,
   retire pending prepared keys, clear only errors belonging to that attempt,
   and be idempotent across restart/repeated pull. Do not erase diagnostic
   evidence until the settled baseline is durable.
8. A failed standalone sweep must checkpoint the durable attempt/confirmation
   state so the recovery pull is not rejected as dirty. A hard interruption may
   leave an intentionally blocked state, but must never bless an unknown remote
   revision or corrupt the previous valid JSON/blob state.
9. Keep all existing ownership, path, marker, page-ID, revision preflight, and
   compare-and-swap checks. Recovery is not permission to retry blindly or
   overwrite a remote page.

##### Deferred recovery tests for TODO 3/TODO 5

Use the real `publish_only` and `pull_growi_once` orchestration with an offline
fake GROWI client. Assertions must inspect the ledger, page baseline, attempt
record, document journal, pure generated map, effective wiki page, Git
`last-good`, and remote body—not merely the returned failure list.

1. Existing page, first write succeeds and second page fails: checkpoint,
   restart objects, pull, and retry. The successful bot body creates no human
   edit; page IDs/revisions and baselines converge; prepared state retires.
2. Same scenario with a human edit after the confirmed first write: journal base
   is the prepared generated value, `human_delta` contains only the human edit,
   and a subsequent source update preserves it without conflict or stale bot
   override.
3. Exact lost update response after server acceptance: remote exact body is
   adopted once; repeated pull is a no-op.
4. Lost update response before server acceptance: remote remains at inspected
   revision; old baseline remains authoritative and retry succeeds normally.
5. Ambiguous lost response followed by a different remote body: fail closed,
   preserve remote text, do not change pure/effective state or journal, and keep
   enough attempt evidence for operator recovery.
6. **409 race regression:** during the conditional PUT, change remote from
   `40°C/AUTO` to `40°C/MANUAL`, then return 409. After pull, the rejected
   prepared `55°C` body appears nowhere in the journal base/delta or generated
   ancestor. The only human delta is `AUTO -> MANUAL`; ledger follows the human
   revision; retry never overwrites it.
7. Preflight detects a changed revision before mutation: no prepared attempt is
   incorrectly treated as landed and no other page is written.
8. Lost create response for a new page: exact owned body is reconciled to the
   discovered page ID and ledgered once. Repeat after process restart.
9. Lost create plus a different human-edited body: preserve it and fail closed;
   do not adopt, recreate, or overwrite it.
10. Path, marker, known page-ID, attempt-ID, prepared-body, and inspected-
    revision mismatches each fail closed independently.
11. Existing protected human overlay before the partial publish: recovery uses
    the saved pure generated blob, never `strip_regions(effective_local)`. The
    pre-existing human edit remains one journal operation and is not baked into
    the pure generated map or duplicated after render.
12. Successful normal publication, exact adoption, rejected 409, ambiguous
    failure, and retry each leave prepared/error/confirmation keys in the
    documented state. Repeated pull/retry must be idempotent.
13. For every recoverable outcome, assert `candidate_is_clean(project,
    last_good(project))`. For intentionally blocked outcomes, assert the block
    and evidence are checkpointed and a later pull is allowed to report the same
    block without modifying remote content.
14. Mutation checks: disabling the prepared rebase must fail the late-human
    tests; disabling the 409 rejection/causality guard must fail the 409 test;
    replacing the pure generated snapshot with stripped effective local must
    fail the pre-existing-overlay test.

Run and report:

```bash
.venv/bin/python -m unittest tests.test_human_changes.PartialPublicationTest -q
.venv/bin/python -m unittest tests.test_human_changes tests.test_update_tiers tests.test_growi_images tests.test_pipeline_scope.OwnershipStampTest -q
.venv/bin/python -m unittest tests.test_mount_diff_pipeline.DiffPipelineSafetyTest -q
git diff --check
```

The four documented `DiffPipelineSafetyTest` baseline failures may remain, but
no new failure is allowed when this deferred matrix is implemented.

#### TODO 1 completion record

Accepted behavior:

- Standalone partial publication persists exact prepared request evidence and
  checkpoints failed sweeps so the next pull can recover.
- An exact owned prepared body is adopted as bot output, not human intent.
- A late human revision after a partial bot write rebases on the prepared remote
  and effective local snapshots so the bot delta is not journaled as human.
- A recorded `revision_race` vetoes prepared adoption/rebase. The human revision
  is captured from the old accepted baseline; the rejected transport body is
  never treated as landed.
- Fresh attempts and successful publication clear attempt-scoped
  `publication_error`; normal success retires prepared keys.
- Lost create/update responses recover on an exact owned body. A different body
  on an unledgered created page is preserved and fails closed.
- Prepared recovery now records and uses the writer's actual pure generated page
  blob. It never creates a pure ancestor with
  `strip_regions(prepared_effective_local)`, which could bake an existing human
  overlay into generator state.

Files changed for TODO 1: `publisher/human_changes.py`,
`graph/growi/client.py`, `publisher/pipeline.py`, and
`tests/test_human_changes.py`. `handoff-conflicts.md` records the review.

Verified commands/results:

```bash
.venv/bin/python -m unittest tests.test_human_changes.PartialPublicationTest -q
# 8 tests passed

.venv/bin/python -m unittest tests.test_human_changes tests.test_update_tiers tests.test_growi_images tests.test_pipeline_scope.OwnershipStampTest -q
# 102 tests passed

.venv/bin/python -m unittest tests.test_mount_diff_pipeline.DiffPipelineSafetyTest -q
# 21 passed; only the same 4 documented HEAD failures remained

git diff --check
# clean
```

Accepted-for-now limitations, assigned to later TODOs:

- There is no durable per-response page confirmation or attempt ID yet. A
  non-exact advanced body without a recorded 409 remains causally ambiguous;
  TODO 3 owns explicit confirmation/ambiguous-response/hard-interruption tests.
- A process kill between remote acceptance and the outer failed-sweep checkpoint
  can leave dirty prepared state; TODO 3 owns the interruption matrix.
- A human-modified page created after a lost response but absent from the ledger
  remains fail-closed until the same document retries; TODO 5's full inventory
  owns discovery and operator reporting.
- `restore_publication` retains its separate candidate rollback evidence rules;
  unification is unnecessary unless later tests prove semantic divergence.

TODO 2 may now begin, one task only, followed by code+test review and an early
stop as required by the protocol.

#### TODO 2 — add rollout modes

This starts only after TODO 1 is reviewed and accepted. Add one enum-like
setting, `human_sync_mode = off|observe|apply`, using the repository's existing
INI/environment/CLI precedence and validation conventions. Default to `off` for
missing and legacy configuration. Reject unknown values with a useful error;
never silently coerce them to `apply`.

Required behavior by mode:

| Concern | `off` | `observe` | `apply` |
| --- | --- | --- | --- |
| Ownership/path/revision/CAS checks | active | active | active |
| Blind 409 retry | forbidden | forbidden | forbidden |
| Capture remote difference as authoritative human edit | no | no | yes |
| Record redacted observation/proposed classification | no, safety event only | yes | yes |
| Change wiki or `_planning/pages` from a new remote edit | no | no | yes |
| Mark linker pending because of a new remote edit | no | no | yes |
| Publish over an unaccepted remote difference | never | never | never |
| Apply an already-authoritative existing journal | preserve it or fail closed; never drop it | preserve it | yes |
| Model/scorer decisions | none | proposals only | deterministic paths only until TODO 4 is separately approved |

Implementation details:

1. Put parsing/validation in `graph/config.py` and expose the resolved value on
   the settings object used by sync, build, pull, watcher, and direct publish.
   Add a CLI override only if the project already has a consistent override
   pattern; otherwise document the INI/environment setting rather than adding a
   one-off flag.
2. Centralize mode decisions in a small policy object/helper. Do not scatter
   string comparisons throughout writer, pipeline, and GROWI client code.
3. Safety checks are mode-independent: ownership-marker damage, deletion,
   move, unexpected revision, 409, corrupt snapshots, and ambiguous prepared
   publication must still block in `off` and `observe`.
4. `off` must not erase or bypass an existing active journal. Choose and test
   one safe rule: continue deterministic rendering of already-authoritative
   records, or reject switching to `off` while active records exist. Document
   the choice. It may not publish pure output over previously protected human
   text.
5. `observe` stores observations separately from authoritative `edits`. Include
   page/revision IDs, content hashes, proposed operation/status/match reason,
   mode, algorithm version, and timestamp; do not duplicate full sensitive text
   outside the existing validated blob store. Repeated polling is idempotent.
6. `observe` must leave document edits, effective wiki bytes, generator state,
   sidecars, linker state, ledger accepted revision, and remote content
   unchanged. Because the remote difference remains unaccepted, publication of
   that page stays blocked.
7. `apply` retains the reviewed deterministic capture/overlay behavior. Mode
   changes do not reinterpret historical records silently. Promoting an
   observation to an authoritative edit must happen by re-fetching and checking
   the same revision, not by trusting stale proposal data.
8. Emit structured, text-redacted events including mode and decision. Surface
   counts in command results so an operator can compare observe/apply behavior.
9. Document mode semantics, default, transition behavior, and examples in
   README/config samples. Do not edit real credentials or project endpoints.

Mandatory tests:

- configuration default, all three values, invalid value, and precedence;
- the same remote human replacement through pull/sync/watcher in each mode;
- `off` and `observe` produce byte-identical wiki, pure state, sidecars, and
  ledger accepted revision, while still blocking publication;
- `observe` writes one idempotent proposal with correct hashes and no
  authoritative edit, even across restart and repeated polling;
- `apply` captures/renders exactly once;
- missing/moved/deleted/marker-damaged/409 pages block in all three modes;
- switching `apply -> off -> apply` cannot lose or duplicate an existing human
  record; test the chosen off-with-journal rule explicitly;
- `observe -> apply` revalidates the remote revision, and a changed revision
  cannot reuse the stale proposal;
- direct `publish_only`, one-shot `pull`, `sync_once`, and idle watcher all use
  the same resolved policy;
- no mode invokes a model in this TODO.

Completion gate: focused configuration, pipeline, writer, pull, watcher, and
publication tests pass; existing TODO 1 tests remain green; documentation and
handoff record actual commands/results. Stop for review before TODO 3.

#### TODO 3 — complete the deterministic acceptance-test matrix

This phase closes deterministic coverage gaps; it is not permission to add
semantic/model behavior. Prefer table-driven fixtures and actual public
pipeline/writer entry points over direct helper-only tests. Every case must
assert pure generated state, effective wiki, journal records, page baselines,
changed-page scope, and idempotency after a restart.

Required fixture corpus:

- additions before/between/after generated blocks, replacements, and deletions;
- same edit on source and human sides, disjoint edits, overlapping facts, and
  contradictions involving numbers/units/IDs;
- page split, page combine, rename, reorder, moved heading, deleted heading,
  and a complete document rewrite;
- Markdown tables, nested fences, HTML tables/comments, images, reference and
  permalink links, Japanese prose/headings, and large blocks;
- malformed/duplicated/unbalanced human, source-conflict, ownership, and footer
  markers.

Mandatory lifecycle matrix:

1. Human addition, replacement, and deletion each survive actual Tier 0, 1, 2,
   and 3 writer flows—not simulated calls to `HumanStore.generated` alone.
2. Run every deterministic same/disjoint/conflict case for Tier 1, Tier 2, and
   Tier 3. Assert exact output text and operation status.
3. Run at least four consecutive source updates plus process reconstruction;
   assert no duplicate edit IDs, regions, facts, dashboard rows, or retained
   notes.
4. Tier 3 split/combine/rename/reorder tests must verify anchor migration,
   current target, source identity, and retained fallback when matching is
   ambiguous.
5. Explicit keep-human, accept-source, combine, replacement, suppression, and
   retained-note deletion must remain stable on subsequent updates. Malformed
   resolutions fail closed with the complete observed body retained.
6. Absorbed edits reactivate if source later regresses; orphaned edits retry and
   reattach only when deterministic matching becomes unambiguous.
7. Interrupt each atomic JSON/snapshot write at the replace boundary. After
   restart, either the complete old state or complete new state is readable;
   never partial JSON, dangling blob references, or a partially advanced ledger.
8. Exercise a real multi-page publication failure and candidate rollback using
   normal orchestration. Assert remote known revisions, live/candidate Git refs,
   ledger, human store, wiki, and prepared records converge.
9. Linker consumes effective pages while pure generator pages and sidecars stay
   human-free and byte-stable. Overlay-changed pages enter incremental scope;
   unrelated pages do not.
10. Index/search tests verify the conflict dashboard contains only status/links,
    does not duplicate complete variants, while retained human notes remain
    searchable, attributed, and linked to their document.
11. Run all cases twice and after recreating `Project`, `HumanStore`, publisher,
    and fake client objects to prove persistence rather than object-memory luck.

Production fixes are allowed only when a matrix case exposes a deterministic
defect. Record each defect beside its regression test. Do not start scorer,
writer, judge, activity detector, or live-service work.

Completion gate: the complete deterministic matrix passes along with TODO 1–2
focused suites and `git diff --check`; report test counts and any known baseline
failures, then stop for review before TODO 4.

#### TODO 4 — implement Phase 4 semantic assistance

Phase 4 is an optional improvement layer for cases the deterministic engine
already preserves safely as conflict/orphan. It must never be required for
capture, publication safety, or verbatim fallback. Implement all components in
`observe` first; do not enable semantic decisions in `apply` during this TODO.

Data contracts and routing:

1. Define versioned typed records for candidate features, scorer result, writer
   request/response, validation result, judge result, feedback attempt, final
   proposal, and cache key. Validate all external/model output strictly; reject
   unknown schema versions and out-of-range/non-finite scores.
2. Build a deterministic candidate shortlist before any model call. Candidate
   features must include source identity, exact page/heading paths, block kind,
   normalized hashes, token/length ratios, neighbor hashes, exact identifiers,
   and deterministic similarity. Exclude impossible type/path/identity matches.
   Cap candidate count and record why each candidate entered the shortlist.
3. Jev receives only the bounded candidate pairs, never the whole wiki. Keep
   thresholds/margins as named versioned constants. During this phase, record
   scores and the proposed winner in observation artifacts only. Unavailable,
   malformed, tied, low-score, or low-margin results mean “no semantic match.”
4. The merge writer receives G0, exact human text H, candidate G1, structural
   metadata, and explicit protected spans. Remote text and document text are
   untrusted data, not instructions. Require a structured response containing
   the merged text and a mapping proving every protected span's disposition.
5. Mechanical validation runs before the judge and is authoritative. Verify:
   all protected human spans occur byte-for-byte exactly once unless an explicit
   tombstone authorizes deletion; numbers, units, identifiers, error codes,
   URLs, images, tables, fences, and markers are not invented/lost/duplicated;
   Markdown fences/markers remain balanced; output size is bounded; and no text
   from another candidate leaks in.
6. Use an independent structured judge call/configuration. The judge sees inputs
   plus writer output and returns pass/fail with machine-readable reasons. A
   judge cannot waive a mechanical failure or approve deletion of protected
   human content.
7. Allow a small fixed number of feedback attempts (recommended two total writer
   attempts). Feedback contains validation/judge reason codes, not free-form
   sensitive logs. No unbounded retry and no recursive agent behavior.
8. On scorer, writer, validator, judge, timeout, transport, schema, or budget
   failure, emit the existing exact deterministic conflict/retained-note output.
   Both sides remain complete and verbatim. Publication continues only if that
   deterministic result passes existing safety checks.
9. Cache by hashes of G0/H/G1 plus anchor identity, scorer/prompt/model versions,
   policy mode, and validation version. Cache only fully validated structured
   results. Corrupt, partial, old-version, or context-mismatched entries are
   ignored safely. Store blobs through the validated human store and do not log
   full sensitive text.
10. Save prompt templates and schemas as reviewed repository artifacts. Include
    explicit data delimiters and injection warnings. Do not interpolate remote
    Markdown into system/developer instructions.
11. Emit redacted metrics: shortlist size, scores/margin, attempts, reason codes,
    cache hit/miss, token/time budget, and fallback. Keep enough hashes/version
    data to reproduce a decision offline.

Mandatory tests use deterministic fake Jev/model/judge implementations—no
network and no nondeterministic assertions:

- shortlist stability, cap, impossible-candidate exclusion, and tie ordering;
- clear/high-margin, low-score, low-margin, tie, NaN/out-of-range, invalid
  schema, timeout, and unavailable Jev;
- writer success for a moved/rewritten block while preserving exact Japanese,
  numbers/units, IDs, tables, code, comments, links, and images;
- writer timeout/exception, invalid JSON/schema, truncation, duplicate protected
  text, missing protected text, invented number/ID/URL, damaged fence/marker,
  unrelated candidate leakage, and oversized output;
- judge reject/timeout/invalid schema and proof that it cannot override a
  mechanical rejection;
- feedback fixes the first attempt; all attempts fail; attempt bound enforced;
- every failure produces byte-exact deterministic fallback containing complete
  human and source variants;
- prompt-injection-looking page text is preserved as data and cannot alter tool
  choice, schema, protected spans, or attempt budget;
- cache hit avoids calls; every key/version/hash change misses; corrupt cache
  fails safely; failed/unvalidated output is never cached;
- `observe` records proposals but effective wiki, journal authority, ledger,
  linker scope, and remote pages remain unchanged;
- prove no semantic path executes in `off`, and `apply` still uses only the
  previously approved deterministic behavior.

Completion gate: all semantic components and failure tests exist, but runtime
effect remains observe-only. Produce a calibration artifact with labelled
expected matches and observed scores; do not select apply thresholds yet.
Record commands/results and stop for review before TODO 5.

#### TODO 5 — implement Phase 6 activity detection and recovery inventory

Use `docs/reverse-sync-plan.md` as the protocol reference. This optimization may
reduce reads, but it may not weaken ownership, revision, capture, or publication
safety. Keep a correctness-first full inventory path for startup, cursor loss,
periodic audit, and operator request.

Required implementation:

1. Add a versioned persisted detector cursor under tracked metadata. Store the
   server/endpoint identity, last processed timestamp, and the complete set of
   activity IDs processed at that same timestamp. Advance it only after all
   referenced pages/events are durably classified and checkpointed.
2. Page through activity results deterministically. Deduplicate IDs across
   overlapping pages/windows. On identical timestamps, resume after known IDs
   rather than adding an unsafe time epsilon. Handle clock skew, out-of-order
   events, duplicate events, pagination, empty pages, and restart mid-page.
3. Map activities to ledger-owned page IDs and document identities. Fetch bodies
   only for relevant candidates, but classify deletion, move/rename, ownership
   loss, and unknown IDs explicitly. An activity hint is never proof of content;
   the fetched page and baseline checks remain authoritative.
4. Define endpoint/cursor invalidation: endpoint change, server reset, cursor
   too old, malformed response, permission gap, or detected sequence gap forces
   a full inventory and does not silently advance the cursor.
5. Implement a complete path inventory below the configured write/root boundary.
   Compare remote ID/path/marker ownership against ledger and page baselines.
   Classify: unchanged owned, changed owned, moved owned, deleted owned, duplicate
   marker/page identity, newly discovered owned, unmanaged foreign, and ambiguous.
6. Newly discovered owned pages may be reconciled only through existing exact
   prepared/publication evidence or legacy-safe migration. Unknown/unmanaged
   pages are reported and never adopted, deleted, moved, or overwritten.
7. Repair missed events by feeding classifications into the same capture/block
   paths used by normal polling. Do not create a second merge implementation.
   Cursor and repaired ledger/store state checkpoint atomically.
8. Add bounded periodic audit and an explicit one-shot inventory command/option.
   Normal cost should scale with activity volume; audit cost and frequency must
   be visible in structured results.
9. Emit redacted counts and reason codes: events scanned/deduplicated, pages
   fetched, owned changed/moved/deleted, unmanaged, ambiguous, cursor resets,
   inventory duration, and repairs.

Mandatory offline fake-server tests:

- no activity performs no page-body reads; one owned edit fetches one page;
- multiple activities at the same timestamp, pagination split at that
  timestamp, duplicates across pages, and restart after partial processing;
- out-of-order/late event within the overlap window is processed once;
- cursor is not advanced when any page classification/checkpoint fails;
- endpoint/server identity change, expired cursor, malformed payload, sequence
  gap, permission failure, and activity API outage trigger the documented safe
  fallback without losing changes;
- create/delete/rename/move/marker damage/duplicate ownership and foreign pages
  are classified exactly and never overwritten;
- complete inventory discovers a missed owned edit and routes it through the
  same `off|observe|apply` policy;
- exact prepared lost-create page is recoverable; human-modified unledgered page
  remains blocked; unmanaged page is report-only;
- inventory boundary prevents reads/writes outside configured root;
- large inventory proves body fetches are limited to changed/ambiguous pages
  where the API metadata permits it;
- repeated detector/inventory runs are idempotent and preserve last-good/cursor
  consistency across simulated interruption.

Completion gate: normal detector and full inventory converge to identical
classifications on the fixture corpus, all prior race/mode tests stay green,
and measured fake-server call counts demonstrate edit-volume scaling. Update
the handoff and stop for review before TODO 6.

#### TODO 6 — complete operator and legacy recovery paths

Finish the human-facing recovery workflow without weakening the deterministic
store. This phase may improve presentation and recover verified history; it may
not guess missing ancestors or auto-resolve ambiguous content.

Operator view requirements:

1. Add a project-wide summary that links to each document's
   `98-Human-Conflicts.md` and retained-note page. Show counts by active,
   absorbed, conflict, orphaned, suppressed/deleted, legacy-pinned, blocked,
   and observe-only proposal status.
2. Each row has stable edit/conflict ID, project/document/page link, current
   status, first-seen source identity/hash/time, last-seen remote revision/time,
   last applied source hash/time, match/fallback reason, and available action.
   Do not duplicate full human/source bodies in the project dashboard.
3. Keep complete variants on the owned page or retained-note page. Clearly label
   human text, newer source candidate, ambiguity, and whether publication is
   blocked. Preserve exact text; the dashboard is navigation, not authority.
4. Support deterministic keep-human, accept-source, combine, delete/suppress,
   and retry-match lifecycles using stable hidden IDs. Every action must be
   revision-checked, idempotent, recorded in history, and reversible from Git.
   Malformed, duplicated, stale, or cross-document IDs fail closed.
5. Project/document dashboards and retained pages must avoid source filename
   collisions, participate in changed-page/link/index scope, and disappear only
   when no record still needs them. Search results must not show duplicate full
   conflicts from dashboards.
6. Expose structured command results/events for unresolved and blocked counts so
   watcher/CLI users know why publication stopped without reading logs.

Verified legacy ancestor recovery:

1. Search only repository-controlled Git history/state for a candidate pure
   ancestor associated with the same stable source identity, page identity, and
   source digest lineage. Never select by path alone.
2. Validate a candidate against sidecar content hash, source stamp/ID, marker
   identity, expected generated filenames, and absence of human-region/linker
   contamination. Recompute hashes from bytes; do not trust metadata alone.
3. If exactly one verified candidate exists, import its body into the validated
   snapshot store and record commit/blob/source evidence and algorithm version.
   Then replay the legacy human text through normal capture/render logic.
4. Zero or multiple valid candidates, shallow/missing history, corrupt blobs,
   identity reuse, or any contaminated candidate keeps `legacy_pinned` unchanged
   and blocked/visible. Never choose the nearest-looking text semantically.
5. Recovery is idempotent across restart and rollback. It must not rewrite Git
   history, delete the original pin/evidence, or alter unrelated documents.

Mandatory tests:

- project summary aggregation across multiple documents/projects and every
  status, with stable sorted links and no complete body duplication;
- first/last source/revision/time fields update correctly and survive restart;
- keep-human, accept-source, combine, suppress/delete, and retry-match actions
  remain stable through later source changes and repeated pulls;
- stale revision, unknown ID, duplicate ID, wrong document, damaged marker, and
  simultaneous human edit all fail closed without partial resolution;
- dashboard/retained filename collision, linker scope, publication, cleanup,
  and index/search behavior;
- unique verified historical ancestor succeeds and records exact evidence;
- wrong source identity, reused path, sidecar/hash mismatch, human-contaminated
  historical page, linker-contaminated page, two candidates, no history,
  shallow history, and corrupt blob all remain legacy-pinned;
- successful recovery followed by Tier 0–3 updates preserves the human edit;
- candidate rollback and last-good restoration return dashboard, journal,
  recovered ancestor, and wiki together.

Completion gate: an operator can find and resolve every deterministic status,
and legacy recovery succeeds only with byte-verifiable unique ancestry. Run all
prior suites, record results, and stop for review before TODO 7.

#### TODO 7 — controlled live verification and final rollout

This phase starts only after TODOs 1–6 have separate review approval. It is the
first phase allowed to contact a real GROWI/model service. Use a dedicated
throwaway project and write subtree; never production content. Obtain explicit
confirmation of the target URL and disposable path before writes/deletes, and
print/record the resolved boundary without exposing tokens.

Preflight:

1. Re-run all offline focused suites and `git diff --check`; preserve the known
   baseline-failure list separately from feature regressions.
2. Back up/export the disposable remote subtree and local project metadata.
   Record endpoint/server identity, plugin/API versions, mode, marker format,
   commit, configuration hashes, and test page IDs. Verify delete/reset is
   restricted to the disposable subtree.
3. Start with `human_sync_mode=off`; confirm no command can overwrite a seeded
   remote human difference. Then use `observe`; compare proposals with labelled
   expected operations before enabling `apply`.
4. Semantic scorer/writer remains observe-only until its labelled calibration,
   thresholds, and false-positive review are explicitly approved. Deterministic
   apply can be enabled independently.

Controlled end-to-end matrix:

- initial multi-page publish, normal update, no-op repeat, and process restart;
- human add/replace/delete, exact same edit, disjoint edit, contradiction, and
  explicit keep-human/accept-source/combine/suppress resolution;
- Tier 0, 1, 2, and 3 source updates including split/combine/rename/reorder;
- remote page rename/move, delete, ownership-marker damage, foreign page at the
  destination, and duplicate marker; all must block safely;
- source rename and bulk source/page deletion within the disposable subtree;
- tables, fenced code, Japanese text, reference/permalink links, attachments,
  and image upload/identity/count preservation;
- real conditional-write 409 using two clients, human edit after preflight, and
  human edit during generation;
- injected/transport-level lost update response and lost create response,
  partial multi-page failure, immediate late human edit, pull/retry, and watcher
  restart between failure and recovery;
- activity detector pagination/cursor restart plus forced full inventory that
  reaches the same classification;
- candidate failure, promotion, rollback, last-good restoration, and repeated
  reconciliation after service restart;
- model/Jev outage if configured: deterministic fallback remains complete and
  no remote human text is lost.

For every case record: initial local/pure/effective hashes, remote page/revision,
operation performed, structured events, final hashes/revisions/status, expected
versus actual, number of API/model calls, and cleanup result. Save redacted
artifacts under a dedicated test report location; never store tokens or full
sensitive production text.

Rollout gates:

1. `off`: safety/preflight only. Zero overwrites of seeded remote differences.
2. `observe`: run long enough to cover normal watcher restarts and labelled
   edits. Review every proposed operation; zero silent loss and acceptable false
   positive/negative rates are required.
3. deterministic `apply`: enable exact replay/absorption/disjoint and guaranteed
   conflict/orphan fallback. Monitor blocked/conflict/orphan/rollback counts.
4. activity optimization: enable only after full inventory shows no missed
   owned revisions over the observation window.
5. semantic apply, if ever desired, is a separate explicit approval after Jev
   calibration; writer-improved merges are enabled last and retain fallback.
6. Define rollback triggers before rollout: any missing/duplicated human span,
   unexpected overwrite/delete/move, cursor gap, corrupt store, non-idempotent
   retry, or unexplained ledger/remote disagreement returns the project to the
   previous mode and last-good state.

Final deliverable: a redacted E2E report with pass/fail per case, exact commands,
versions, call counts, known limitations, rollback evidence, and recommendation
for the next rollout stage. Clean up only the confirmed disposable subtree and
state whether recovery is possible. Stop for final review; do not silently
enable production projects.

### TODO 2–7 implementation record — 2026-10-01

| TODO | State | Evidence / remaining gate |
| --- | --- | --- |
| 2 — rollout modes | implemented and offline verified | Central `HumanSyncPolicy`; default `off`; INI/environment validation; redacted idempotent observations; all modes keep safety checks; observe-to-apply re-fetches the current revision. |
| 3 — deterministic matrix and publication recovery | implemented and offline verified | Stable attempt IDs/history, exact prepared blobs, per-page confirmations, 409 rejection, ambiguous-response fail-closed behavior, late-human rebase, restart/idempotency and existing Tier 0–3 matrix remain green. |
| 4 — semantic assistance | implemented observe-only and offline verified | Strict versioned contracts, deterministic bounded shortlist, Jev runtime adapter, independent structured writer/judge clients, mechanical validation, two-attempt feedback, validated cache, redacted metrics and reviewed prompt/schema artifacts. `docs/human-semantic-calibration.json` is explicitly an offline fake-Jev baseline; semantic apply has no code path. |
| 5 — activity/inventory | implemented and offline verified | Official activity response parsing, overlap/same-timestamp cursor, pagination/dedup/gap invalidation, periodic/forced full inventory, boundary enforcement and classification through the normal capture path. Cursor commit follows durable ledger/operator writes and participates in the Git checkpoint. |
| 6 — operator/legacy recovery | implemented and offline verified | Redacted project summary, stable revision-checked/idempotent resolutions, CLI status/actions, unique Git ancestry with source/sidecar/content/contamination checks, and commit/blob evidence. |
| 7 — controlled live verification | local gate implemented; live matrix pending | The report enumerates every required case, stores only redacted metadata, hashes the resolved endpoint/boundary, and refuses an unconfirmed boundary. A real URL and disposable subtree must be explicitly confirmed before any write/delete. |

Commands and results:

```text
.venv/bin/python -m unittest tests.test_human_sync_rollout tests.test_human_changes tests.test_update_tiers tests.test_growi_images tests.test_pipeline_scope.OwnershipStampTest -q
Ran 122 tests — OK

.venv/bin/python -m unittest tests.test_mount_diff_pipeline.DiffPipelineSafetyTest -q
Ran 25 tests — 21 passed; the same four documented baseline failures remained

.venv/bin/python -m compileall -q graph publisher main.py tests/test_human_sync_rollout.py tests/test_human_changes.py
passed

git diff --check
passed
```

No network/model/live GROWI call was made. To begin TODO 7, first create the
local plan with `python main.py --project <throwaway-project> human live-plan
--path /<configured-project>/disposable/<case-root>`, review the redacted
endpoint and resolved boundary, and explicitly confirm that exact URL/path.
