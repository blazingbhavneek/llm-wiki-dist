# Human edits and conflicts: minimal page-merge design, implementation, and validation

Last rewritten: 2026-10-08

Status: **implemented, not verified for production.** Offline tests pass. The journal model is
gone; `publisher/human_changes.py` holds `rebase()`, the state layer, pull, generate, rollback
replay, and the three model calls. Live verification so far: 12 of 62 cases, all with API edits by
the bot token and a temporary model setting, which is not production evidence.
**`handoff-conflicts-tests.md` is the plan for testing everything**, including real GROWI edits
by a separate user and the production pilot. The `data_std2` test project still has a live legacy
journal (5 edits), so its document is blocked with `legacy human journal` until migrated (4.3) or
reset.
Deviation from 4.4: a document whose generator state still carries `human_edited` sidecars
(pre-journal reverse sync) raises `legacy human state` in the writer instead of being pinned,
so nothing is rebuilt away.

Audience: the agent that implements the change, then a local agent/operator with
access to the private source subset and a disposable GROWI test subtree.

Owner priorities, in order:

1. Human information is never lost.
2. What is already in the page wins, and what came from the GROWI UI always wins.
3. Accuracy before speed. Speed matters, but second.
4. The implementation is as small as possible. Prefer an LLM call over a new
   hardcoded rule about headings, block shapes, file names, or similarity scores.
   Mechanical checks are used only where they are exact (text equality,
   uniqueness) or as safety guards on model output.

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
   consume that code. The report is an operator gate, not a runtime interlock.
6. Stop immediately on lost human text, duplicated content, an unexpected
   overwrite, delete, or move, a corrupt snapshot, an unexplained revision
   mismatch, a cursor gap, or a non-idempotent retry. Preserve the data root and
   the remote page revisions for investigation.
7. Run the focused offline tests in section 6 first. Expand only after they pass.
8. Do not put private page bodies, source text, tokens, or authenticated URLs in
   this handoff, a Git commit message, or a live report. Store hashes, revision
   IDs, page IDs, reason codes, and redacted paths instead.

## 2. The merge rules

At each sync, X is what the source document changed and Y is what humans changed
in GROWI.

1. X none, Y changed: keep Y.
2. X changed, Y none: update the wiki with X.
3. X and Y make the same change: keep it once.
4. X and Y change the same fact differently: keep Y and add X as a note,
   `（元文書の更新: …）`.
5. X and Y change different facts: both coexist.

Two additions:

- **Source deletes something a human changed.** The human's changed text moves
  to the last page of the document, `<last page number + 1>-付録.md` (Appendix;
  for example `005-付録.md` after `004-….md`), so the current document structure
  is not disturbed and the human information is kept.
- **Structure-only human edits.** Formatting, heading fixes, removed duplicate
  sentences, and reordering add no information. They become a short instruction
  for the writer ("what the human wants this page to look like"). The edit itself
  is also kept and merged like any other change (formatting differences get no
  note), so a wrong classification can never lose information (3.5).

A human change keeps being re-applied on every later sync until the human
reverts it or the source catches up to it. After that, rule 2 applies again.
Conflicts are resolved by editing the page in GROWI. There is no CLI resolver.

## 3. Design: the page is the journal

The old design stored each human edit in a separate journal with IDs, statuses,
markers, and matching rules, then pasted the edits back into each new page. The
new design stores no per-edit records. It keeps two versions of each page: what
the generator last produced, and the same page with every human change.
Comparing them recovers the human changes at every sync, so they cannot drift
from the page.

### 3.1 Where the state lives

`wiki/<document>/` belongs to the writer. `publish_output()`
(`graph/workspace/writer.py`) deletes the whole folder, `_planning/` included, on
every export (it restores only four linker files), and document deletes remove
it too. Writer state under `metadata/state/<document>/` is also deleted on a full
rebuild. Human state therefore lives under `metadata/human-sync/`, which no
writer touches and which project Git already tracks:

```
metadata/human-sync/doc/<key>/     key = HumanStore.identity(raw_rel)[0] = sha256(source_id)
  pure/<page>.md                   what the generator produced last time ("old source page")
  current/<page>.md                the page before linking, with every human change ("current page")
  doc.json                         {"source_id", "raw_rel", "appendix": "<file name>" or null,
                                    "guidance": {"<page>": ["…"]}}
  captures/<sha256(marker_id:observed_revision_id)>.json
                                   {"page", "revision", "base_blob", "human_blob", "guidance"}
```

- The key follows the source identity: a moved document keeps its state, and a
  different source at the same path starts empty (3.8).
- `current/` is the authority. Every write of it is mirrored to
  `wiki/<doc>/<page>.md` (the live page) and `wiki/<doc>/_planning/pages/<page>.md`
  (what the linker reads). A page that is blank in `current/` (a human emptied
  it) stays there as a blank file, so the deletion persists, but it is not
  written to the live folder or `_planning/pages`; the publisher then trashes the
  remote page.
- A document gets a state directory when its first human edit is accepted.
  Until then the wiki pages are exactly what they are today and no human state is
  written. The directory is never deleted automatically: its capture records are
  needed for rollback (3.8) and an unused one costs nothing.
- `captures/` is the audit trail of accepted GROWI revisions and is what candidate
  rollback replays (3.8). `*_blob` values are `HumanStore.put` snapshot hashes.

### 3.2 `rebase(old, human, new)`

One deterministic function, used by pull (3.3), generation (3.4), and rollback
(3.8). Each argument maps page file names to text for one document. It returns
the merged pages and a list of Appendix entries.

Terms: a *line-aligned* occurrence starts at a line start and covers whole lines.
*Unique in a map* means exactly one line-aligned occurrence across all pages of
that map. *Blank* means whitespace only.

1. **Changes.** For each page `p` in `old`, compare `old[p]` with `human[p]`
   (missing means unchanged) line by line, keeping line endings:
   `difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()`. Each
   non-equal opcode is one change: old lines `B` → human lines `A`. Either may be
   empty.
2. **Locate** each change against the original `new` texts:
   - **Exact:** `B` is not blank and is unique in `old` and unique in `new`. The
     location is that occurrence. This is how a change follows a section the
     source moved or reordered, including to another page.
   - **Window:** otherwise, scan `old[p]` upward from the change for the first
     line that lies in an equal opcode (unchanged by the human) and is unique,
     as a single line, in `old` and in `new`: the *before-anchor*. Scan downward
     the same way for the *after-anchor*. Both must be in the same new page and in
     order. A missing anchor may be replaced by the start or end of `new[p]`, but
     only when `p` exists in `new` and the other anchor is in `new[p]` or also
     missing. The window is the text strictly between the anchors: `old_w` in
     `old[p]`, `cur_w` in `human[p]` (anchors are equal lines, so their human
     positions are known), and `new_w` in the new page.
   - Otherwise the change has **no location**.
3. **Overlaps.** A change whose old range lies inside another change's window on
   the same old page becomes part of that window: its human text is already in
   the window's `cur_w`, and it is not located separately. If two remaining
   locations overlap in `new`, combine them into one window: the union of their
   new text, with both sides' old and human texts in order. Handle it as a
   window in step 4.
4. **Decide**, against the original `new`:
   - **Exact:** replace that occurrence of `B` with `A`.
   - **Window**, first matching case:
     1. `new_w == cur_w`: nothing to do (rule 3, or the source caught up).
     2. `new_w == old_w`: replace `new_w` with `cur_w` (rules 1 and 5, and pure
        insertions).
     3. Every change in the window has a non-blank `B` that occurs exactly once,
        line-aligned, in `new_w`, and those occurrences do not overlap: replace
        each with its `A`.
     4. `new_w` is blank and `old_w` is not (the source removed this part): add
        each non-blank `A` of the window's changes to the Appendix (3.6), and
        keep `new_w` as it is.
     5. Otherwise: the merge call (3.5). If it fails, replace `new_w` with
        `cur_w` followed by the source note (3.5).
   - **No location:** a non-blank `A` goes to the Appendix. A blank `A` (the
     human deleted text that can no longer be found) needs nothing; no human
     information is involved.
5. **Apply** all replacements to each new page from the last position to the
   first. Return the pages and the Appendix entries.

Required properties, each with a test: `rebase(x, x, z) == z` (no human change
means the new text unchanged) and `rebase(x, y, x) == y` (no source change means
the human version exactly), with no model call in either.

### 3.3 Pull: accept Y

For each owned page whose GROWI revision changed (the existing preflight,
ownership, revision, prepared-evidence, and mode checks stay as they are):

1. **Transport rebase**, as today: `canonical = merge(editable(previous_remote),
   editable(remote), editable(previous_local))`, then `restore_page_links`. On
   conflict, block the page as today. `previous_local` is the live page as
   published (it is read from disk at publication and stored in the page record).
2. **Remove linker output.** `L` = the Markdown links `[text](url)` (not images,
   `![…](…)`) that appear in `editable(previous_local)` but not anywhere in the
   current page (`current/<page>.md`, or `_planning/pages/<page>.md` when the
   document has no state yet). `base = unwrap(editable(previous_local), L)` and
   `human = unwrap(canonical, L)`, where `unwrap` replaces each such link with its
   text. Links a human added are not in `L`.
3. `base == human`: a transport-only or link-only revision. Record the revision;
   nothing else changes.
4. `off`/`observe`: block or observe as today. No model call.
5. `apply`:
   1. Legacy check, then bootstrap if the document has no state (4.3, 3.1): copy
      each `_planning/pages/<page>.md` (the live page if missing) into `pure/`
      and `current/`. Without state these files equal the last generator output.
   2. `rebase({page: base}, {page: human}, current pages)` → new current pages
      and Appendix entries (appended as in 3.6). Normally the current page equals `base`, so this
      returns exactly `human` with no model call. When the local generation is
      newer than the published one (a build whose publication failed), the human
      change is applied on top of the newer local pages instead of replacing them.
   3. Classification call (3.5) on the changes between `base` and `human`; add
      the resulting guidance.
   4. Write the capture record, `current/` and its mirrors, and `doc.json`. Drop
      the rewritten pages' rows from the fast inline-link manifest (3.9). Mark the
      linker pending. Record the revision as today.

The Appendix is pulled like any other page, so humans can edit or delete its
entries.

### 3.4 Generate: apply X

`apply_generated(project, raw_rel)` runs after the writer, or fast repair, has
exported the new pages with `publish_output()`. At that point the live `*.md`
files are exactly the new generator output (the folder was rebuilt).

1. Legacy check (4.3) and identity check (3.8).
2. No state: keep the new pages as they are and write them to
   `_planning/pages/` too, as the current code does. Write no state.
3. State: `rebase(pure, current pages without the Appendix, new pages)`. The
   output pages are the merged pages plus the Appendix, with the returned
   entries appended (3.6) and its name updated if N changed. Write every output
   page to `current/` and, unless blank (3.1), to the live folder and
   `_planning/pages/`. Delete `current/`, live, and `_planning/pages/` files that
   are not output pages. Replace `pure/` with the new pages. Drop rewritten pages
   from the fast inline-link manifest (3.9).
4. Return, as `changed_pages`, the paths of output pages whose text differs from
   the exported page, plus pages removed or renamed; `graph/fast/repair.py` and
   the writer's linker decision read that attribute.

When the writer did not export (`out_dir is None` in `write_wiki_pages`), the
document did not change and nothing runs. The old code re-rendered the journal
there; pull now writes the current pages directly.

### 3.5 Model calls

There are three calls. Use the existing `structured_ainvoke` and the standard
chat configuration. Thinking stays on, there are no concurrency changes, and no
prompt is truncated: an input over 30,000 characters is not sent, and that call
takes its fallback directly.

**Classify** (pull, step 5.3). Input: the changes `B → A` of one accepted
revision. Output, per change: `kind` (`content` or `structure`) and, for
`structure`, a one-line instruction in the page language. "Content" means a
reader learns something different: a value, fact, name, step, or warning was
added, changed, or removed. Everything else is "structure": formatting, heading
wording or level, moved sections, removed duplicates, table layout. Structure
instructions are appended to `guidance[page]` (exact duplicates skipped, last 20
kept). **Classification only produces guidance; it never changes how a change is
merged.** If the call fails, no guidance is added. Nothing else depends on it, so
a wrong or failed classification cannot lose information.

The writer adds `guidance[page]` as one optional block to every prompt that
writes or patches that page, looked up by the page's planned file name:
`section_write_prompt`, `intro_prompt`, and `incremental_page_edit_prompt` in
`graph/wiki/prompts.py` (callers: `graph/wiki/pipeline.py`,
`graph/workspace/writer.py`, `graph/fast/writer.py`), and the fast repair editor's
own prompt, `_writer_messages` in `graph/fast/repair.py`. With no guidance, the
prompt bytes are unchanged, so existing caches stay valid. When the writer
follows the guidance, the human's structure change is already in the new text
and `rebase` finds nothing to do. When it does not, `rebase` re-applies the
change, and the merge instruction says formatting differences get no note.

**Merge** (rebase window case 5). Input: `old_w`, `cur_w`, `new_w`, and the
page's guidance. Output: `{"edits": [{"find": "…", "replace": "…"}], "appendix": "…"}`.
The edits apply to `new_w`, so source text outside them is kept byte for byte.
The instruction is:

- apply the human's change (`old_w` → `cur_w`) to `new_w`, and keep every fact in
  `new_w`;
- if the human and `new_w` disagree on the same fact, keep the human's version
  and add the `new_w` value as `（元文書の更新: …）` (always this exact text, so
  later merges can find it);
- if they differ only in wording or formatting, keep the human's form and add no
  note;
- if `cur_w` already has a `（元文書の更新: …）` note for the same fact, replace it
  rather than adding a second;
- if `new_w` no longer contains the part the human changed, put the human's
  changed text in `appendix` instead of the page.

Before accepting a result:

1. every `find` is non-empty and occurs exactly once in `new_w`, and no two
   edits overlap;
2. every number (digit sequence) that occurs in `cur_w` but not in `old_w`
   occurs in the result or in `appendix`;
3. the **verify** call returns `true` for both checks.

**Verify.** Input: `old_w`, `cur_w`, `new_w`, the result, and `appendix`.
Output: `{"human_kept": bool, "source_kept": bool}`. `human_kept`: every piece of
information the human added or changed is in the result or `appendix`.
`source_kept`: every fact in `new_w` is in the result, as text or inside a note.

If any check fails, run the merge once more with the failure reason. If that
also fails, use the fallback: `cur_w` followed by the source note. The note is
`（元文書の更新: X）` on its own line when `new_w` is one line under 200
characters; otherwise it is `> **元文書の更新:**` followed by each line of `new_w`
prefixed with `> `. Either way both versions stay visible.

### 3.6 Appendix (`<N+1>-付録.md`)

- **Name.** N is the highest page number among the document's other output
  pages (the leading digits of `NNN-Title.md`; unnumbered pages such as `index.md` do not
  count, and N is 0 if there are none), plus one, with the same zero padding
  (three digits by default): after `004-….md` it is `005-付録.md`.
- **Identity.** The Appendix is the page named in `doc.json` `appendix`, never
  recognized by pattern. A generated page that happens to be titled 付録 is an
  ordinary page.
- **Storage.** It lives in `current/` like any page, has no `pure/` copy, and is
  left out of the generation rebase (3.4). It is carried over unchanged, then new
  entries are appended. In pull it is an ordinary page, so human edits and
  deletions are kept.
- **Entries.** New entries are only appended. An entry already present byte for
  byte is not appended again. Format:

  ```markdown
  ## <heading>

  <the human's changed text (A), verbatim>

  > 元の文書では、この部分は削除されました（元ページ: <original page file>）
  ```

  The heading is the nearest line starting with `#` at or above the change in
  the human page, without the `#` marks; if there is none, the original page file
  name. A new Appendix page starts with `# 付録`.
- **Rename.** If N changes, rename the Appendix in the same run (`current/`,
  live folder, `_planning/pages`, `doc.json`). The publisher already handles a
  renamed page: it creates the new page and trashes the old one with its expected
  revision checked (`_trash_under`). Pull runs first, so human edits to the
  Appendix are already captured; an edit landing in between fails the revision
  check and blocks.
- **Restore.** If the source later restores the removed part, the restored
  source text appears normally, and the Appendix entry stays until a human
  removes it.

### 3.7 Sync order

1. Lock and require clean last-good, as today.
2. Interrupted-operation recovery, as today.
3. **New:** pull GROWI changes (`pull_growi_once`) before any scanning, building,
   or publishing, so a UI-only edit is captured even when no source changed.
   Skip this when the endpoint or marker format changed (the case
   `republish_if_stale` handles); old page IDs must not be pulled from a newly
   configured server. Pull failures are recorded and the sync continues: the
   blocked page's document is blocked again by its own capture in step 5, and
   other documents proceed.
4. Scan, convert, and generate (`apply_generated` per document).
5. Per-document `_capture_remote` stays as the late-edit check before a
   document's publication. It uses the same pull code (3.3).
6. Link, preflight, publish, checkpoint, as today.

### 3.8 Deletes, source identity, rollback

- **Human information** means: some change between `pure` and `current` has a
  non-blank `A`, or the Appendix has entries. Pure human deletions are not human
  information.
- **Document delete.** Deleting a source document is blocked while the document
  has human information, because its Appendix is inside the folder being
  deleted. To let the delete proceed, remove or move that text in GROWI; the next
  pull captures it. This applies to bulk deletes too.
- **Document move.** Nothing new: the state key is the source identity, which a
  move keeps. `doc.json` `raw_rel` is updated on its next write.
- **Different source at the same path.** It has a new key and inherits nothing.
  If another key's `doc.json` has the same `raw_rel` and holds human information,
  block the new document the same way as a delete.
- **Candidate rollback** replaces `import_captured`, at the same call site in
  `publisher/pipeline.py` (before `restore_publication`). For every capture
  record in the candidate that live does not have, in the order they were
  captured, apply the pull computation to live: bootstrap if needed, then `rebase({page: base}, {page: human}, live
  current pages)`, using the record's blobs. Copy the record and its guidance.
  `restore_publication` then publishes live pages that keep the human change.
  Without this, a human edit captured inside a failed candidate whose page had
  already been republished would be erased when that page is restored. Do not
  copy the candidate's Appendix: its entries come from the rolled-back generation
  or from captures, and the replay already restores every captured human change,
  so copying it would duplicate them. A human edit made on the candidate's own
  Appendix page is a capture record too; replaying it onto live, where that page
  does not exist, sends the text to live's Appendix.

### 3.9 Unchanged, and the fast path

Unchanged: GROWI publication safety (ownership markers, whole-batch preflight,
compare-and-swap updates with `origin: view`, HTTP 409 aborts with no retry,
prepared/confirmed evidence, lost-response recovery, delete inspection); activity
detection and forced inventory; `human_sync_mode` (`off`, `observe`, `apply`) and
the INI-over-environment precedence; linker, index, candidate worktrees, and
last-good.

Fast path (`sync --fast`, `--fast --repair`, `--fast --link`) uses the same
hooks. Fast page writing runs inside the normal writer, which calls
`apply_generated` (`graph/workspace/writer.py`). `--fast --repair` calls
`publish_output` and then `apply_generated` directly (`graph/fast/repair.py`). The
fast inline linker writes links only into live `*.md` pages, never into
`_planning/pages`, so pull removes them as linker output (3.3 step 2).

- **Inline-link manifest.** `--fast --link` keeps
  `metadata/cache/fast-inline-links/manifest.json` and undoes its own links before
  relinking. `_undo_edits` returns `could not locate generated link` when a
  listed link and its original line are both gone, and `link_project` then stops
  the whole run. Pull and `apply_generated` rewrite live pages without those
  links, so whenever they rewrite a page they must remove that page's row from
  the manifest. Its links are no longer on the page, and the next `--fast --link`
  links it again.
- A resumed `--fast --repair` deliberately skips the GROWI pull (`runner/cli.py`).
  A human edit made meanwhile is not lost: publication preflight rejects the
  changed revision, and the next pull captures it. It appears one sync later.
- `--fast --repair` refuses to run while inline links are active; that is
  unchanged.

## 4. Implementation plan

Estimate (not a measurement): about 250 new lines, about 2,000 deleted. The new
logic goes in `publisher/human_changes.py`, so one file owns it.

### 4.1 Order of work

Build the new path beside the old one, prove it on real data, and only then
delete the old code:

1. Write `rebase()` (3.2) and its tests. It is a pure function with no wiring.
2. Run the shadow check (4.2) on real projects. The owner reviews the numbers
   before anything is wired.
3. Wire pull (3.3), generation (3.4), the model calls and writer guidance (3.5),
   the Appendix (3.6), the sync order (3.7), deletes, identity, and rollback
   (3.8), and the fast manifest (3.9).
4. Delete the old code (4.4) and its tests, and add the frozen-contract line to
   `AGENTS.md` (4.5).
5. Run the offline tests (section 6), then the live matrix (section 13).

### 4.2 Shadow check on real history

Write `tools/human_rebase_shadow.py`. Run it read-only on a copy of a real data
root, never on the original. For each document, take consecutive Git versions of
the writer's pure output (`metadata/state/<document>/wiki/*.md`, excluding
`_review.md`) where the source changed. On the older version, inject human edits: change a number, add a
paragraph mid-page, delete a sentence, rename a heading, move a section, and edit
a sentence that the newer version rewrites. Run
`rebase(older, older_with_edits, newer)` with the real model. Check:

- every injected human text is present exactly once, in the result or in an
  Appendix entry;
- `rebase(x, y, x) == y` and `rebase(x, x, z) == z` hold on every pair.

Report, without printing any page text: counts per path (exact, window cases
1–5, merge accepted on the first or second attempt, fallback note, Appendix, no
location), model calls, the lines of the newer version that are missing from the
result (as hashes, for review), and run time per document.

### 4.3 Legacy journal check

The old journal for a document is `metadata/human-sync/documents/<key>.json`,
under the same key or the legacy key `sha256(id_seed or raw_rel)`, following any
`alias_of` (as `HumanStore.document` does today). If it has an edit whose status is
not `deleted` or `absorbed`, the document's pages may contain old-style human text
and markers. Pull and generation must then block the document with reason
`legacy human journal`, and never bootstrap it as having no human changes. Before
implementing, run this on every real data root, on the machine that has the real
projects:

```bash
.venv/bin/python - /absolute/data_root <<'PY'
import json, pathlib, sys
for path in pathlib.Path(sys.argv[1]).glob("*/metadata/human-sync/documents/*.json"):
    data = json.loads(path.read_text(encoding="utf-8"))
    live = [e for e in data.get("edits", []) if e.get("status") not in ("deleted", "absorbed")]
    if live:
        print(path, len(live))
PY
```

If it prints nothing, the block is only a safety net. If it prints documents, a
one-time migration is needed before `apply` is used on them:

- `pure/` comes from the journal's `pages[name].body_blob`;
- `current/` is the current `_planning/pages/<name>.md` with every
  `llm-wiki-human`/`llm-wiki-source` marker line removed;
- the text of any `99-Retained-Human-Notes*.md` page, without markers or its
  title, goes into a new Appendix;
- `98-Human-Conflicts*.md` is dropped.

A document with a `legacy_pinned` edit has no pure version: keep it blocked and
ask the owner. In this repository, only the `data_std2` test project has such
journals.

### 4.4 Delete, keep, add

**Delete**

- `publisher/human_changes.py`: `Block`, `blocks`, `conflict_text`, `wrap_edit`,
  `marker_matches`, `regions`, `substitute_markers`, `source_candidate`,
  `strip_sources`, `unquote_source`, `strip_regions`, `map_generated`,
  `OverlayResult` (replace it with a small result carrying `changed_pages`),
  `RETAINED`, `DASHBOARD`, `LegacyBaseUnavailable`, and the `HumanStore` journal
  methods `document`, `save`, `project_summary`, `resolve`, `generated`,
  `ensure_generated`, `_record`, `pin_legacy`, `import_captured` (replaced, 3.8),
  `archive`, `move`, `capture`, `_match`, `render`.
- `publisher/human_semantic.py`, `publisher/prompts/human_merge_system.txt`,
  `publisher/prompts/human_judge_system.txt`, `publisher/legacy_recovery.py`.
- CLI `human resolve` and `human recover-legacy` (`runner/cli.py`).
- `graph/workspace/writer.py`: the legacy-pin branch and the
  `requires_pure_rebuild` check (around lines 600-627), and the `out_dir is None`
  re-render branch (3.4).
- `publisher/pipeline.py`: the `archive`/`move` journal calls and
  `build_runtime_semantic_assistant`.
- `graph/growi/client.py` pull path: the semantic proposal block, the legacy-pin
  branch, `store.capture`, and `store.render`. Replace `map_generated(text, f)`
  with `f(text)`.

**Keep**

- `HumanStore`: `identity`, `put`, `get`, `validate`, `audit` (extend it to
  validate `doc/*/captures/*.json`), `page`, `save_page`, `record_observation`,
  `record_event`, `prepared_pending`, `prepared_match`, `account_prepared`,
  `settle_prepared`, `retire_unlanded`, `remember_page`, `block_page`; plus
  `editable` and `footer_span`.
- `merge`, `_changes`, `_overlaps`, and `_atomic_ranges`, only for the transport
  rebase in pull (3.3 step 1). They are not used for human merging.
- `publisher/activity.py` and `publisher/live_verification.py`.

**Add**

- `rebase()` (3.2), pull (3.3), `apply_generated()` (3.4), and the classify,
  merge, and verify calls (3.5) in `publisher/human_changes.py`.
- Writer guidance block (3.5) in the four prompt builders.
- Appendix handling (3.6).
- `_cmd_sync_isolated`: pull first (3.7).
- Delete and identity blocks, and the rollback replay (3.8).
- Fast inline-link manifest row removal (3.9).
- Legacy check (4.3).
- `human status`: print blocked pages, documents with human information,
  Appendix entry counts, and guidance counts.

### 4.5 Frozen-contract exception

The owner approved that this subsystem stops writing the journal
(`metadata/human-sync/documents/`, `semantic/`), the
`llm-wiki-human`/`llm-wiki-source` markers, and the `98-*`/`99-*` auxiliary
pages, and adds `metadata/human-sync/doc/`. Existing files are left untouched,
not migrated or deleted, except by the migration in 4.3. Add one line to
`AGENTS.md` saying so. Everything else in the frozen contract still applies.

## 5. Project paths and durable records

For `data_root=/absolute/data` and `target_name=hc-subset`, the project root is
`/absolute/data/hc-subset`. `Project.mount` references the configured absolute
`source_mount`; the mount is not copied into the project.

```
<project-root>/
  .git/
  sources/                         immutable source blobs used by project history
  raw/                             converted Markdown
  wiki/<document>/                 writer-owned output: pages and _planning/
  metadata/
    pipeline.json                  source and published page ledger
    source-identities.json         stable active IDs and tombstones
    state/<document>/              writer state
    cache/fast-inline-links/       fast inline-link manifest (3.9)
    human-sync/
      schema.json
      doc/<key>/                   human state per document (3.1)
      pages/*.json                 per-page publication/revision evidence
      snapshots/<sha256>.md        content-addressed text blobs
      observations/*.json          observe-mode records
      events/*.json                redacted decision/reason records
      activity-cursor.json
      live-reports/*.json
```

`publisher/history.py` tracks `.gitignore`, `sources/`, `raw/`, `wiki/`,
`metadata/state/`, `metadata/pipeline.json`, `metadata/source-identities.json`,
and `metadata/human-sync/`. `refs/llm-wiki/last-good` is the accepted live state.
Every command that changes durable state checkpoints it. Never fix a dirty-tree
refusal with `git reset --hard`; first find out why it is dirty, because the dirty
files may be the only copy of a captured human edit. Project Git history is also
the audit trail: every version of every current page is in it.

## 6. Focused offline tests

Rewrite `tests/test_human_changes.py` as a small set built on fake pages and a
fake model (no network). One test each:

- `rebase(x, x, z) == z` and `rebase(x, y, x) == y` with no model call;
- rules 1-5, including rule 4 through the merge call and through the fallback
  note;
- exact matching is line-aligned and requires uniqueness in both old and new
  (a short `B` that also appears elsewhere uses the window instead);
- human change survives four source updates and a restart;
- source reorders sections, and moves a section to another page: the change
  follows;
- human adds a section mid-page, renames a heading, reorders sections: positions
  are kept;
- source deletes a section, and a whole page, that a human changed: only the
  human's changed text is in the Appendix, once, and repeat runs add nothing;
- the Appendix is named last page number + 1 and is renamed when the source adds
  pages; human edits and deletions of entries are kept;
- both sides delete the same text: it appears nowhere;
- a human empties a page: it stays blank in `current/`, is not published, and
  stays deleted after a source update;
- human revert; source catches up, then changes again, and rule 2 applies;
- **full regeneration** (`publish_output` deletes the folder): every human change
  survives;
- **pull while the local generation is newer than the published one**: the newer
  local text is kept and the human change is applied on top;
- **candidate rollback** after a capture and a partial publish: live keeps the
  human change before `restore_publication`;
- merge output that drops a human number, or a `find` that is missing or not
  unique, is rejected; a `false` from verify is rejected; two rejections give the
  fallback note;
- classification: a structure edit adds guidance that reaches the four prompt
  builders; a failed classification adds none and changes nothing else;
- linker links are removed on pull and human-added links are kept; a link-only or
  transport-only revision changes nothing;
- `off`/`observe` block and make no model call;
- idle `sync` captures a UI edit with no source change;
- a document with a live legacy journal is blocked;
- a different `source_id` at the same path inherits nothing, and is blocked when
  the old key holds human information;
- deleting a document with human information is blocked; with only human
  deletions it proceeds;
- the fast inline-link manifest loses the rows of rewritten pages.

Keep and adapt the publication-safety tests (`PublicationSafetyTest`,
`PartialPublicationTest`, `GrowiClientContractTest`, `ActivityDetectorTest`).
Delete tests for removed code (journal, resolver, legacy recovery, semantic
observe).

```bash
timeout 60s .venv/bin/python -m compileall -q common convert wiki linker index publisher runner graph
timeout 120s .venv/bin/python -m unittest tests.test_human_changes tests.test_human_sync_rollout -q
```

These tests use a fake model. The shadow check (4.2) uses the real model, so
the owner sets up the model endpoint before it runs.

## 7. Publication, races, and lost responses (unchanged)

Before a batch write, `publish_pages()` fetches every destination and validates
the whole batch: expected page exists, page ID/path/revision/ownership match, a
new destination that already exists is owned and reconcilable, and no two local
pages map to one remote path. A foreign destination is never adopted because its
path matches. Updates send `origin: "view"` and the inspected revision; HTTP 409
records a race and aborts with no second PUT. Deletes inspect ownership and
expected revisions before trashing.

Before each create/update the store records an attempt ID and the exact prepared
bodies; after the response it records confirmation. On restart, an exact body
match adopts a lost response as the bot's write, an unchanged revision retires
it, and anything ambiguous blocks. A partially failed publish is checkpointed so
the next pull has the attempt evidence.

## 8. Activity detection and inventory (unchanged)

One-shot `pull` uses `ActivityDetector` as a hint. The cursor is endpoint and
boundary keyed and stores the last timestamp, the IDs at that timestamp, recent
IDs, and an optional sequence. These force a full inventory instead of advancing
the cursor: endpoint changed, missing/malformed cursor, sequence or pagination
gap, cursor too old, clock skew, malformed or unavailable activity, permission
gap, unknown page IDs, periodic audit due, or `pull --inventory`. Inventory
classifies owned pages and rejects anything outside the boundary. The cursor is
committed only after classification and checkpoint succeed.

## 9. Expected behavior by scenario

| Scenario | Expected |
|---|---|
| remote revision unchanged | nothing changes |
| UI-only edit, no source change, plain `sync` | captured before anything else (3.7) |
| human change in GROWI while `off`/`observe` | that document blocks; nothing accepted; no model call |
| same blocked revision after switching to `apply` | captured once |
| human add/replace/delete only | kept, once, where the human put it |
| human and source change different things | both present |
| human and source change the same fact differently | human value, source value in `（元文書の更新: …）` |
| human and source make the same change | once, no note; later source changes apply |
| human and source delete the same thing | absent everywhere |
| human deletes, source modifies | deletion kept, new source value as a note |
| human modifies, source deletes the text/section/page | the human's changed text in the Appendix (`<N+1>-付録.md`), once |
| human reverts their change | wiki follows the source again |
| source reorders sections or moves one to another page | human changes follow their text |
| source rewords a section without changing facts | the merge call re-applies the human change, no note |
| full regeneration (the writer rebuilds the document folder) | every human change kept |
| human only fixes formatting/headings/duplicates | kept in the page and merged without notes; guidance passed to the writer |
| several remote revisions before one pull | latest page captured once |
| linker-only or transport-only difference, editor re-save | not a human change, in every mode |
| human edit arrives while a newer local generation is unpublished | human change applied on top of the newer local page |
| a sync captures a human edit, publishes, then fails and rolls back | live keeps the human change |
| remote page moved/deleted, ownership marker damaged/duplicated, foreign page at destination | blocked; no recreate, overwrite, or adoption |
| revision changes after inspection | 409/changed-revision block, no blind retry |
| lost create/update response | exact prepared body: adopt as bot output; anything else: block |
| source document deleted while it has human information (3.8) | delete blocked until that text is removed or moved in GROWI |
| different source at the same path | nothing inherited; blocked if the old document holds human information |

## 10. Operator commands

```bash
CFG=/absolute/path/to/disposable-hc.ini
PY=.venv/bin/python
$PY main.py --project "$CFG" pull               # capture GROWI edits only
$PY main.py --project "$CFG" pull --inventory   # full read-only boundary check first
$PY main.py --project "$CFG" sync               # pulls first once 3.7 is implemented
$PY main.py --project "$CFG" human status       # blocked pages, human-change/Appendix/guidance counts
$PY main.py --project "$CFG" human live-plan --path /UNIQUE-DISPOSABLE-TARGET
```

Resolving a conflict means editing the page in GROWI: delete the
`（元文書の更新: …）` note to keep the human value, replace the human value with
the source value, or write a combination. The next pull captures the result.

`human live-plan` requires the path to equal or be below `/<target_name>`.
Record the report path and confirmation code privately. A live harness must
call `verify_boundary_confirmation(growi_url, disposable_path, confirmation_code)`
immediately before each mutating or deleting case.

## 11. Known limitations

1. Text repeated many times may have no unique anchor nearby; that change goes to
   the merge call or the fallback note.
2. A paragraph is one line, so both sides touching one paragraph costs a merge
   call and a verify call (each about 100 s on the local model), more on retry.
3. If the source rewrites a whole page, human changes on it go to the Appendix or
   get notes; a human may need to tidy up.
4. Writer guidance is keyed by the planned page file name. A page whose name
   changes loses its guidance; its human text is still kept.
5. When merges keep failing and the source keeps changing the same fact, fallback
   notes can stack. A human edit removes them.
6. Numbers are checked mechanically, but a human fact made only of words is
   protected by the verify call, which is a model. A wrong `true` there could
   accept a merge that drops it. The shadow check (4.2) measures this before
   rollout.
7. If a human moves a section and the source changes it in the same period, the
   source's version appears as a note at the old place and the moved copy keeps
   the older text.
8. A human deletion that cannot be located (its text is not unique and no
   consistent window exists, for example because its surroundings were all
   rewritten) is not re-applied, so the deleted text can reappear. No
   human-written text is lost.
9. Removing a link that the linker added is not kept; the linker adds it again.
10. GROWI Markdown serialization, permalinks, images, and attachments must be
    checked on the actual target version.
11. Activity APIs may be global or permission-restricted. A constrained case is
    not a pass; record it as constrained and prove forced inventory.

## 12. Disposable setup and the real-user test (work PC)

### 12.1 Inputs

Create outside the repository: an approved source subset mount, a new empty data
root, an INI file, and optionally a private evidence directory. Include at least
one prose document with stable headings and numbers, one document that splits
into several pages, one with tables, code, links, and images if production has
them, and one that gets a realistic major rewrite. Do not use symlinks into a
production data root or source mount.

### 12.2 Example INI

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

`target_name` must be one folder name; it maps to `/llm-wiki-hc-disposable-UNIQUE`.
Use an absolute `data_root`. An INI value in `[settings]` overrides
`WIKI_HUMAN_SYNC_MODE`, so change the mode only in this INI. Keep the token in
`GROWI_TOKEN` rather than the file when possible, and never print or commit it.

### 12.3 Check resolved settings without printing secrets

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

Do not continue if any path is a real project's data root or source mount.

### 12.4 Preflight and first publish

```bash
$PY main.py --project "$CFG" check        # read-only; prints endpoints, run privately
$PY main.py --project "$CFG" human live-plan --path /llm-wiki-hc-disposable-UNIQUE
$PY main.py --project "$CFG" sync
$PY main.py --project "$CFG" pull --inventory
$PY main.py --project "$CFG" human status
```

Run `live-plan` while the data root is still empty. Confirm that every GROWI path
is below `/<target_name>` and that the project is clean at last-good.

### 12.5 Real-user test on the work PC: why

Edits made through the API by a script are not enough to trust production. A
person editing in the GROWI browser editor differs in three ways that such
scripts never exercise:

- the editor saves with `origin: "editor"`, which GROWI accepts even on a stale
  revision (see the comment in `GrowiClient.update_page`), so a save from an
  editor left open can land on top of a newer bot publish;
- the editor may re-serialize the page (whitespace, tables, escaping, the final
  newline), and the publisher's transport rewrites (stripped backticks, links
  turned into `/<page_id>`, the navigation block) come back inside the saved
  body;
- the edit comes from another account, so activity detection and permissions
  follow a different path.

The procedure below uses a real browser and a second account. Every case that
involves a human edit (sections 9 and 13) must pass here at least once; a pass
from an API harness alone does not count.

### 12.6 Before starting

1. The code is the reviewed commit. Note `git rev-parse HEAD` and whether the
   working tree has other changes.
2. The offline tests (section 6) pass.
3. The legacy check (4.3) has been run on every real data root of the work PC.
4. No temporary model setting is active. This must print nothing:
   `git diff -- graph/clients/chat.py graph/wiki/model.py | grep -n THINKING_BUDGET`.
5. The model endpoint answers: `curl -s <chat_base_url>/models` lists the model.

### 12.7 Accounts and connection

Use two GROWI accounts. Never use one for both.

- **Bot account.** The pipeline connects to GROWI with `growi_url` and this
  account's access token. Issue the token in GROWI under the account's user
  settings (the API settings / access token page; the exact label depends on the
  GROWI version). Put it in the test INI as `growi_token` under `[project]`, or
  export it as `GROWI_TOKEN` (an INI value wins over the environment). Never print
  or commit it.
- **Tester account.** An ordinary user, created by a GROWI administrator (user
  management, invite). It must be able to edit pages below `/<target_name>`. It
  edits only in the browser and never receives the bot token.

The test INI is the one from 12.2. Add the endpoint settings your working
project uses, by typing their values into the new INI's `[settings]`:
`chat_base_url`, `chat_model`, `parser_base_url`, `concurrency`, and any
`wiki_linker_*` / `wiki_jev_*` keys that project sets. Do not copy or edit the
existing INI file. Then confirm the connection, read-only:

```bash
$PY main.py --project "$CFG" check
```

Use the same sync command as production (`sync` or `sync --fast`) everywhere
below. If production uses both, run the whole procedure once for each.

### 12.8 Marker words and the counting check

Every human edit in this procedure contains a unique marker word: `HCT-01`,
`HCT-02`, and so on. Then every check is just "how many times does `HCT-01`
appear", in GROWI and in the local human state, and no page text has to be read
or reported. This read-only script counts one marker:

```bash
$PY - "$CFG" HCT-01 <<'PY'
import asyncio, pathlib, sys
from common.settings import load_settings
from graph.growi.client import GrowiClient

settings, word = load_settings(sys.argv[1]), sys.argv[2]

async def remote():
    client = GrowiClient(settings.growi_url, settings.growi_token, timeout=60)
    counts = {}
    for listed in await client.list_all_pages("/" + settings.target_name):
        page = await client.get_page(page_id=listed.page_id)
        if page is not None and page.body.count(word):
            counts[page.path] = page.body.count(word)
    return counts

growi = asyncio.run(remote())
state = pathlib.Path(settings.data_root) / settings.target_name / "metadata" / "human-sync" / "doc"
local = {str(p.relative_to(state)): p.read_text(encoding="utf-8").count(word)
         for p in state.glob("*/current/*.md")}
local = {name: n for name, n in local.items() if n}
print({"growi_total": sum(growi.values()), "growi_pages": growi,
       "local_total": sum(local.values()), "local_files": local})
PY
```

"Once" in the tables below means `growi_total == 1` and `local_total == 1` after
the sync has published. After every step also check:

```bash
git -C "$PROJECT" rev-parse HEAD refs/llm-wiki/last-good   # the two hashes are equal
git -C "$PROJECT" status --porcelain                       # prints nothing
$PY main.py --project "$CFG" human status
```

`$PROJECT` is `<data_root>/<target_name>`.

### 12.9 Procedure

**Setup.** Publish the subset with `human_sync_mode=off` (12.4). `human status`
lists no documents.

**Modes.** The tester adds a sentence containing `HCT-00` to one page and saves.

1. `off`: run `sync`. The output reports that page as blocked ("requires
   human_sync_mode=apply"). `HCT-00`: growi 1, local 0. The bot did not
   overwrite the page.
2. Set `human_sync_mode=observe` in the test INI and run `sync`. Still blocked,
   and an observation is recorded.
3. Set `human_sync_mode=apply` and run `sync`. `HCT-00` once. Run `sync` again:
   nothing changes, and the output shows no model calls.

**Human edits** (`apply`). For each row the tester edits in the browser and
saves; the operator then runs `sync` (not `pull`: sync pulls first, which is
what production relies on), checks, and runs `sync` once more, which must change
nothing.

| # | The tester, in the GROWI editor | Passes when |
|---|---|---|
| U1 | changes a number in a sentence and adds `HCT-01` | once; the rest of the page is unchanged |
| U2 | edits a line that contains inline code and a link, adding `HCT-02` | once; the page is not blocked with "cannot be canonicalized safely"; the link still works after publish |
| U3 | edits a table cell, adding `HCT-03` | once; the table is intact |
| U4 | adds a new section in the middle of a page, heading `## HCT-04` | once, at the same position |
| U5 | deletes one sentence (note its first words privately) | the sentence is gone from GROWI and from `current/` |
| U6 | opens a page and saves without changing anything | `human status` and the number of files in `captures/` are unchanged |
| U7 | opens the editor on page P and leaves it open. The operator changes a value on P in the source and runs `sync`, so the bot publishes P. Then the tester adds `HCT-07` in the still-open editor and saves | `HCT-07` once, **and** the bot's new value is still on P (as text or inside a `（元文書の更新: …）` note). If the new value silently disappeared, record FAIL `editor_stale_save` and stop: this needs a design decision |
| U8 | edits another page of a document while a long `sync` is generating it, adding `HCT-08` | once after the next `sync`; never overwritten |
| U9 | after S4 below has created `NNN-付録.md`, edits one Appendix entry (add `HCT-09`) and deletes another | `HCT-09` once; the deleted entry does not return |
| U10 | deletes a `（元文書の更新: …）` note | the note does not come back while the source value stays the same |

**Source changes with human edits present.** The operator edits the source file
in the test mount, then runs `sync`. After each row every `HCT-` marker from
U1–U4 is still present exactly once, unless the row says otherwise.

| # | The operator changes the source | Passes when |
|---|---|---|
| S1 | a value the tester did not touch | the wiki shows the new value (rule 2) |
| S2 | the value the tester changed in U1, to a different value | the tester's value stays, with the new source value in `（元文書の更新: …）` (rule 4) |
| S3 | that value again, to exactly the tester's value | the value appears once, no note (rule 3) |
| S4 | deletes the section that holds `HCT-01` | `HCT-01` is in `NNN-付録.md` once and nowhere else; that page is numbered last page + 1 |
| S5 | reorders sections, and moves one section to another page | `HCT-02`, `HCT-03`, `HCT-04` follow their text, once each |
| S6 | nothing; run `sync --force <source path relative to the mount>` to force a rebuild | every marker once |

**Watcher.** Run `$PY main.py --project "$CFG" watch --growi-interval 60`. The
tester adds `HCT-11` to a page. Within about a minute it is captured once,
without anyone running `sync`. Stop the watcher and restart it: nothing is
captured twice.

### 12.10 When a row fails

Record it with `record_case(..., passed=False, reason_codes=[...])` (section 14).
Then look at these, in order:

- the `sync` output (failures name the page and reason);
- `human status` (blocked pages and their reasons);
- the marker counts: growi 1 and local 0 means the pull did not capture it;
  growi 0 and local 1 means the publish overwrote or did not publish it;
- `metadata/human-sync/doc/<key>/captures/` (one record per accepted revision);
- section 15.

Do not continue past a FAIL that lost human text (any marker count of 0 that
should be 1). Keep the data root and the GROWI page history for investigation.

## 13. Live validation matrix

For every case record: initial and final hashes of the source, old source page,
current page, and remote Markdown; GROWI page IDs and revisions; reason codes;
API call counts (GET/PUT/create/delete); model call counts; expected and actual
result without private content; and whether `HEAD == refs/llm-wiki/last-good`
with clean durable paths.

Every case is also checked against these invariants:

- every current human change appears exactly once, in the page or the Appendix;
- the rules in section 2 hold;
- nothing is written over a remote revision the pipeline did not inspect;
- a repeat run with no new change is a no-op with no model calls.

"Human edit" means an edit by a separate test user in the GROWI UI, or by a
reviewed private harness using the page's current revision. Every case that
involves a human edit must also pass at least once through the real-user
procedure in 12.9.

### Group A: baseline and idempotence

1. **`initial_multi_page_publish`** — Publish a multi-page source. One ownership
   marker per page, ledger IDs/revisions, no human state written, clean
   checkpoint.
2. **`normal_update`** — Change one source fact; only the expected content changes.
3. **`noop_repeat`** — Repeat pull/sync/publish with nothing changed; hashes and
   revisions are stable.
4. **`process_restart`** — Stop between commands and rerun; nothing duplicated.

### Group B: direct human intent

5. **`human_add`** — Add a paragraph in GROWI. Pull in `off` and `observe` (both
   block, no model call), then `apply`: the text is kept once.
6. **`human_replace`** — Replace one fact; pull twice; the value is stable and
   appears once.
7. **`human_delete`** — Delete one fact; it stays deleted across a source update
   and a restart, and the adjacent text is untouched.
8. **`same_edit`** — Human and source make the same change: once, no note.
9. **`disjoint_edit`** — Human and source change different facts in the same
   section: both present.
10. **`contradiction`** — Human and source change the same value differently:
    human value with `（元文書の更新: …）`.

### Group C: resolution in GROWI

These names are kept from the old CLI resolver. The actions are now page edits.
After each one, pull, sync, and pull again to prove the result is stable.

11. **`keep_human`** — Delete the note. The human value stays and the same
    source value does not bring the note back.
12. **`accept_source`** — Replace the human value with the source value and
    delete the note. The page follows the source from then on.
13. **`combine`** — Write a combination. It becomes the human value, once.
14. **`suppress`** — Delete a human-added paragraph. It does not return.

### Group D: generator tiers and structural changes

Save the writer's `update_decision` event as evidence.

15. **`tier_0`** — No content change: human changes render unchanged.
16. **`tier_1`** — Small patch on a page with a human change: the change and any
    structure fix are kept.
17. **`tier_2`** — Regenerate some pages: human changes follow their text or go
    to the merge call or the Appendix; nothing is dropped.
18. **`tier_3`** — Full regeneration (`publish_output` rebuilds the document
    folder): every human change is present once.
19. **`split_combine_rename_reorder`** — Page split and combine, heading rename,
    section reorder by the source. Record per change whether it followed, was
    merged, or went to the Appendix. Attaching to the wrong topic is a failure.

### Group E: remote integrity and ownership

20. **`remote_move`** — Move an owned page in GROWI: blocked, not recreated.
21. **`remote_delete`** — Trash an owned page: blocked, deletion evidence kept.
22. **`marker_damage`** — Damage or remove the ownership marker: capture blocks
    and keeps the observed evidence.
23. **`foreign_destination`** — An unmanaged page at a needed path: publication
    fails in preflight before any write.
24. **`duplicate_marker`** — Duplicate an ownership marker on another page:
    inventory reports it and refuses adoption.
25. **`source_rename`** — Rename the source; human changes move with the
    document. Then a different source at the old path inherits nothing.
26. **`bulk_delete`** — Remove several sources: only inspected owned pages with
    expected revisions are trashed. A document with human information blocks
    (3.8); one with only human deletions does not.
27. **`markdown_integrity`** — Human text containing code fences, comments,
    tables, inline code, and footer-like text survives link/publish/pull byte for
    byte.
28. **`attachments_images`** — Approved images or attachments: human text is
    preserved and image rewriting does not create human changes. Record transport
    differences by hash.

### Group F: revision and partial-publication races

These need a controlled proxy, instrumented client, or approved pause hook. Never
run them on a shared or production subtree.

29. **`conditional_409`** — Page changes after inspection, before update: one
    PUT, 409 handled, no second PUT.
30. **`edit_after_preflight`** — Edit a later page after preflight: publication
    stops and the human revision remains.
31. **`edit_during_generation`** — Edit during a slow generation: the next
    preflight/pull captures or blocks it; no overwrite.
32. **`lost_update`** — The update lands and the response is lost: restart
    recovers it as bot output, not a human change.
33. **`lost_create`** — The create lands and the response is lost: the retry
    reconciles the page with no duplicate.
34. **`partial_publish_late_edit`** — An early page publishes, a later page
    fails, then a human edits the early page: only the human's change is captured.

### Group G: activity, rollback, and restart

35. **`watcher_restart`** — Watcher running, one remote edit, restart around
    polling: captured exactly once.
36. **`activity_cursor_restart`** — Events sharing a timestamp or in the overlap
    window: deduplicated without skipping. If the feed is inaccessible, record
    constrained and prove forced inventory.
37. **`full_inventory_equivalence`** — Activity pull and `pull --inventory` reach
    the same state for the same revisions.
38. **`candidate_rollback`** — Inside one sync, capture a human edit, let that
    sync republish the page, then force a later failure in the same candidate:
    rollback replays the capture onto live before restoring (3.8), the restored
    page still has the human change, and nothing is captured twice.
39. **`last_good_restore`** — `restore_publication()` has no CLI command; it runs
    when a queued sync fails or is interrupted during publication. Trigger it that
    way: expected revisions are checked first and any intervening human change
    blocks.
40. **`service_restart_reconcile`** — Restart after a prepared or confirmed write
    but before the ledger is final: evidence drives recovery; paths end clean.
41. **`model_outage_fallback`** — Make the model unavailable during pull
    (classification) and generation (merge and verify): no guidance is added,
    windows get the fallback note, nothing is lost, and sync does not hang.

### Group H: new cases (append to `LIVE_CASES`)

42. **`concurrent_add_same_anchor`** — Human and source add text at the same
    place. Identical: once. Different: both present.
43. **`human_delete_source_modify`** — Human deletes a fact; the source changes
    it: the deletion stays and the new value is a note.
44. **`human_modify_source_delete`** — Human changes a fact; the source deletes
    the fact, its section, or its page (all three): the human's changed text is
    in the Appendix (`<N+1>-付録.md`) once and the rest of the document keeps the
    source structure. Then add a page at the source: the Appendix is renamed to
    stay last, with its content intact.
45. **`concurrent_delete_same_fact`** — Both delete the same line, and the same
    section: absent everywhere, Appendix included.
46. **`multiple_remote_revisions_before_pull`** — Several GROWI revisions,
    including one that undoes another: the latest page is captured once. Change
    the same text again in a later sync: one value, the latest.
47. **`mixed_page_capture_failure`** — One pull with a valid edit on page A and a
    damaged or moved page B: A is captured and checkpointed; B stays blocked and is
    rechecked on every pull until resolved; a retry neither loses nor duplicates A.
48. **`source_move_delete_with_remote_edit`** — An unpulled human revision on a
    document the same sync renames or deletes. Rename: captured, then moved.
    Delete: blocked for the operator; the edited page is not trashed.
49. **`transport_only_remote_revision`** — Open a page in the GROWI editor and
    save without changes: no human change.
50. **`mode_transition_same_revision`** — One remote revision seen by `off`,
    `observe`, then `apply`, then `apply -> off -> apply`, with restarts and INI
    reloads: only `apply` captures, once.
51. **`growi_conflict_ui_resolution`** — Resolve a note four ways in the
    editor: keep human, keep source, combine, or edit around it. Each result is
    captured once; a later new source value brings a new note, the old value
    does not.
52. **`remote_revision_rollback`** — Restore an older body from GROWI history:
    it is a human change under rule 1.
53. **`idle_sync_remote_reconciliation`** — After a publish, with the mount
    unchanged, edit a page in GROWI and run plain `sync`: captured before any other
    work; in `off`/`observe` only that document blocks; the repeat run is a no-op
    with no model or parser calls.
54. **`human_section_placement`** — With no source change, add a section
    mid-page, rename a heading, reorder sections: everything stays where the human
    put it.
55. **`conflict_survives_unrelated_edit`** — While a note is shown, edit
    another line in the same section: the note stays.
56. **`fast_policy_human_overlay`** — Repeat 5, 10, and 54 with `sync --fast`,
    `--fast --link`, and `--fast --repair`: no linker text is captured as a human
    change.
57. **`human_revert`** — Undo an earlier change in the editor: the wiki follows
    the source, including later source updates.
58. **`source_catches_up`** — The source makes the human's change: once, no
    note; a later source change to that fact applies normally.
59. **`source_removes_then_restores`** — The source deletes a human-changed part
    (it goes to the Appendix) and later restores it: the restored source text
    appears normally and the Appendix entry stays until a human removes it.
60. **`regeneration_without_fact_change`** — A forced or tier-3 rebuild rewords
    human-changed sections without changing facts: the human changes are
    re-applied with no note.
61. **`structure_only_edit`** — A human fixes headings, formatting, and removes
    a duplicate sentence: the page keeps it; `doc.json` gets guidance that reaches
    the writer prompt; a tier-1 patch keeps the fix; after a tier-3 rewrite the
    fix is present once with no note. Repeat with classification forced to fail:
    the fix is still kept.
62. **`pull_onto_unpublished_generation`** — Make a source change whose
    publication fails, then edit the same document in GROWI and pull: the newer
    local text stays, the human change is applied on top, and the next publish
    carries both.

#### Contract for `LIVE_CASES`

1. Append the 21 IDs above (42-62), making 62 in total. Never rename or remove
   the existing 41.
2. `finalize()` compares against `LIVE_CASES`; keys missing from an old report
   count as pending, so an old 41-case report never finalizes as complete. Do not
   rewrite old reports on open.
3. Record each live case separately, even when one fixture covers several.
4. A constrained result is not a pass. The `apply` rollout stays incomplete until
   every case passes or is reviewed as constrained.

## 14. Recording and finalizing the report

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

Use `record_constraint()` with precise reason codes when the environment
deliberately lacks a permission or fault injector. Before `finalize()`: every case
is passed, failed, or constrained; run a forced inventory and a final pull; check
`human status`; verify `HEAD == refs/llm-wiki/last-good` with clean durable paths;
clean up only the confirmed disposable subtree; keep the local data root and
report until reviewed.

## 15. Triage

- **"dirty last-good working tree"**: do not reset. Check Git status for the
  durable paths and the last command; keep any capture or prepared evidence.
- **Remote difference blocks in `off`/`observe`**: expected. Switch only the
  disposable INI to the next approved mode and pull again.
- **Human text missing**: compare `metadata/human-sync/doc/<key>/current/`,
  `pure/`, the Appendix, and the capture records by hash. Project Git history has
  every earlier version.
- **Unexpected notes**: a merge was rejected (find missing or not unique, number
  check, verify `false`) or its input was over the size limit; check the run log
  for the reason code.
- **Linker text captured as a human change**: compare the links in the page
  record's published local page with the current page (3.3 step 2).
- **Remote moved/deleted/marker damaged**: treat the remote state as an
  intervention; decide it explicitly, then rerun inventory/pull.
- **Ambiguous prepared publication**: preserve the page and attempt record; only
  exact prepared evidence may be adopted as bot output.

## 16. Completion criteria for the local agent

Return without private bodies or secrets: the tested commit and any extra working
changes; the disposable INI hash and redacted endpoint/boundary/data paths; the
offline test results; the live report path/hash and per-case status with reason
codes; counts of pages with human changes, Appendix entries, notes, and
guidance entries by document type; model call counts and fallbacks;
activity/inventory permission findings; partial-publication and restart evidence
with API call counts; cleanup status; and a recommendation: remain `off`, proceed
to `observe`, or request a reviewed `apply` rollout.

The acceptance bar: human information is never silently lost, source truth is
never silently replaced, remote races fail closed, and every recovery path leaves
durable state auditable and repeatable.
