# Code review: commit `6f99246` ("did 7 of the tasks hasegawa san asked for")

Reviewed against its parent `0757431`. Every finding below cites file:line so it can be
checked independently. **Nothing here has been run** — all findings are from reading the
code. Confidence is marked per finding. Verify F1 at runtime before acting on it, because
F1 and F2 decide whether F3/F4 get repaired or deleted.

Line numbers are as of commit `6f99246`.

---

## F1 — The "mentioned in answer" feature is inert. It can never fire.

**Confidence: high (code-confirmed), but confirm at runtime — see "How to verify".**

`findMentionedNodeIds` (`frontend/src/components/ChatPanel.jsx:1062`) drives the red
*"Mentioned in answer"* badges, the `mentionedCount` pill, and the sort-mentioned-to-top
behaviour in `RightDocumentRail.jsx`. It scans **only the answer's markdown prose** for
node IDs. It returns `[]` in both of the two answer paths, for two different reasons.

### Path A — shallow answers: the IDs are there, but we delete them before scanning

`graph/core.py:658` (`SHALLOW_ANSWER_PROMPT`, used at `graph/researcher.py:1538`)
instructs the model, in Japanese:

> 根拠に使ったノードのidを、本文中で**バッククォート付き**で引用してください（例：`` `node:...` ``）

That is: cite node IDs inline, **wrapped in backticks**. So the markdown does contain IDs —
as inline code spans.

Now `ChatPanel.jsx:1225`:

```js
function stripMarkdownCode(markdown) {
  return String(markdown || '')
    .replace(/```[\s\S]*?```/g, '')
    .replace(/`[^`\n]*`/g, '')      // <-- deletes every inline-code span
}
```

And `findMentionedNodeIds` calls it as its **first statement** (`ChatPanel.jsx:1063`):

```js
function findMentionedNodeIds(markdown, refs, rawById) {
  const text = stripMarkdownCode(String(markdown || ''))   // IDs are now gone
  const explicitIds = extractExplicitNodeIdsFromMarkdown(text)
  ...
```

`extractExplicitNodeIdsFromMarkdown` (`ChatPanel.jsx:1106`) does the same thing on its own
first line. Both functions strip out exactly the spans they exist to read.

### Path B — main/deep answers: there are no inline IDs to find at all

The main path uses `LEAD_AGENT_PROMPT` (`graph/core.py`, used at `researcher.py:466`),
whose entire citation instruction is:

```
- finish(answer, cited_node_ids): provide final answer
- Finish with a concise answer and cite supporting node IDs.
```

Citations come back as a **structured argument**, not embedded in the prose. That flows into
`AgentAnswer.cited_node_ids` (`graph/core.py:427-433`, populated at `researcher.py:525`) and
reaches the frontend as `answer.citedIds` / `answer.refs` (`App.jsx:248-255`). The model is
never asked to put node IDs in the answer text, so the markdown that
`findMentionedNodeIds` scans typically contains none.

### Consequence

`mentionedNodeIds` is `[]` on every answer. Therefore:

- the `onAnswerMentionedIds` callback always reports an empty list (`ChatPanel.jsx:1062`);
- `answerMentionedIdsByAnswerId` in `App.jsx:48` is always a map of empty arrays;
- `mentionedIds` reaching `RightDocumentRail` is always `[]`;
- `mentionedCount` is always `0`, so the red pill and every red-highlighted row are dead
  branches;
- the sort-mentioned-first comparator is a no-op.

That is roughly **450 lines** (ChatPanel helpers + RightDocumentRail highlighting + App.jsx
plumbing) that cannot execute their intended path.

**Note what is NOT broken:** `answer.refs` and `answer.citedIds` come from the structured
backend field and work fine. The right rail still lists cited sources. Only the
*"which of these did the answer actually name"* layer on top is dead.

### How to verify (do this first)

Add one line at `ChatPanel.jsx:1062` inside `findMentionedNodeIds`:

```js
console.log('MENTIONED', { markdown, stripped: stripMarkdownCode(markdown), result: found })
```

Ask any question in the UI. If `result` is `[]` on every answer while `markdown` visibly
contains `node:...` strings, F1 is confirmed.

### Fix options

**Option A — repair (keeps the feature).** Invert the logic: scan *only* inline code spans
instead of stripping them.

```js
const INLINE_CODE_RE = /`([^`\n]+)`/g
```

This also makes most of the surrounding machinery unnecessary — `NODE_ID_CANDIDATE_RE`,
`looksLikeExplicitNodeId`, and the hex-suffix heuristic (`ChatPanel.jsx:1127-1144`) exist
only to *guess* which bare word in free prose is a node ID. Once you read the backticked
spans, there is nothing to guess. **~300 lines → ~40.** Path B still needs
`SHALLOW_ANSWER_PROMPT`'s backtick instruction added to `LEAD_AGENT_PROMPT`, or it stays
inert for deep answers.

**Option B — delete.** Revert `RightDocumentRail.jsx` and the `App.jsx` mention plumbing to
`0757431` and drop the ChatPanel helpers. **-450 lines.** The right rail keeps working off
`citedIds`.

Recommendation: **B unless the mention-highlighting is something Hasegawa-san specifically
asked for.** It is a nice-to-have riding on ~450 lines of machinery.

---

## F2 — The citation linkifier has the same defect

**Confidence: high (code-confirmed).**

`linkifyNodeIdsInMarkdown` (`ChatPanel.jsx:948`) is supposed to turn node IDs in the answer
into clickable links. It delegates to `protectMarkdownSpecialRegions`
(`ChatPanel.jsx:969`), which deliberately **skips** replacement inside backticks:

```js
if (part.startsWith('`') && part.endsWith('`')) return part   // :976
```

Same collision as F1: in the shallow path the IDs are in backticks, so they are skipped; in
the main path there are no inline IDs to link. The linkifier renders nothing.

`rewriteCitedNodeIdsBlock` (`ChatPanel.jsx:811`) is a separate hack that regex-matches a
literal `cited_node_ids: [...]` block *in the prose*. `cited_node_ids` is a structured field
and is never concatenated into `answer.markdown` (`App.jsx:252` sets
`markdown: ans.answer`), so this only fires if the model leaks the field into its own answer
text. That may genuinely happen — but it is a workaround for a prompt leak, not a feature,
and it should be labelled as such or removed.

Resolve together with F1.

---

## F3 — `resolveCanonicalNodeId` strips the `node:` prefix on a miss, breaking click-through

**Confidence: high (code-confirmed). This is a real user-visible bug, independent of F1/F2.**

Node IDs have exactly one format. `graph/core.py:906`:

```python
return f"node:{slug(document_name or 'node', 24)}:{short_hash(body)}"
```

And `rawById` is keyed by that string verbatim — `hooks/useGraphData.js:128`:

```js
const rawById = useMemo(() => new Map(raw.nodes.map((n) => [n.id, n])), [raw])
```

Now `ChatPanel.jsx:1146`:

```js
function resolveCanonicalNodeId(id, rawById) {
  const clean = String(id || '').trim()
  if (!clean) return clean

  const withoutNode = stripNodePrefix(clean)
  const withNode = addNodePrefix(withoutNode)

  if (hasRawNode(rawById, clean)) return clean
  if (hasRawNode(rawById, withoutNode)) return withoutNode
  if (hasRawNode(rawById, withNode)) return withNode

  return withoutNode || clean        // <-- returns the PREFIX-LESS id on a miss
}
```

When a cited node is **not in the currently loaded graph slice**, all three `hasRawNode`
checks miss and the function returns the id with `node:` stripped. `openNodeFromId`
(`ChatPanel.jsx:1160`) then hands that stripped id to `onOpenNode`, which looks it up in a
map keyed by `node:...`. Guaranteed miss.

**Symptom:** clicking a citation for any node outside the loaded graph silently does
nothing. No error, no navigation.

**Fix:** on a miss, return the id unchanged. Better — see F4.

---

## F4 — The whole node-ID prefix-normalisation layer solves a problem that does not exist

**Confidence: high (code-confirmed).**

Given F3's evidence (one canonical ID format, one map keyed by it), all of the following are
unnecessary:

| Function | File:line |
|---|---|
| `stripNodePrefix` | `ChatPanel.jsx:1215` |
| `addNodePrefix` | `ChatPanel.jsx:1219` |
| `resolveCanonicalNodeId` | `ChatPanel.jsx:1146` |
| `hasRawNode` | `ChatPanel.jsx:1204` |
| `getRawNode` | `ChatPanel.jsx:1193` |
| `normalizeNodeId`, `addNodePrefix`, `getRawNode`, `getRefByAnyId` | `RightDocumentRail.jsx` (tail) |

`buildNodeLinkCatalog` (`ChatPanel.jsx:1021`) registers **six keys per node** to cover
prefix variants that cannot occur:

```js
catalog.set(canonicalId, item)
catalog.set(originalId, item)
catalog.set(stripNodePrefix(originalId), item)
catalog.set(addNodePrefix(stripNodePrefix(originalId)), item)
catalog.set(stripNodePrefix(canonicalId), item)
catalog.set(addNodePrefix(stripNodePrefix(canonicalId)), item)
```

All of this collapses to `rawById.get(id)`. **~150 lines.**

The deep title-fallback chains ride along with it — e.g. `getNodeLabel`
(`ChatPanel.jsx:1171`) probes `node.heading`, `node.metadata.title`, `node.metadata.label`.
Grep the node shape: those fields do not exist. Two levels (`title || entity`) is the real
surface.

---

## F5 — `admin_copy_db` can produce a silently truncated copy, and ignores an existing correct implementation

**Confidence: high on the code path; the data-loss window is reasoned, not reproduced.**

The store runs in WAL mode — `graph/store.py:197`:

```python
conn.execute("PRAGMA journal_mode=WAL")
```

`app.py` knows this: `_db_sidecar_paths` enumerates `-wal` and `-shm`, and
`_unlink_db_files` deletes all three. But the copy itself, `app.py:1119`:

```python
shutil.copy2(str(source), str(target))
```

copies the **main `.sqlite` file only**. If the WAL has not been checkpointed at that
moment, the copy is missing the most recent committed transactions — a silently incomplete
backup, with no error raised. `_close_stack` (`app.py:187`) closes the tracked connections
first, and SQLite normally checkpoints on last-connection close, which narrows the window —
but see **F7**: normal traffic can reopen the DB concurrently, so "last connection" is not
guaranteed.

**The correct implementation already exists in this repo.** `GraphStore.snapshot_to()`
(`graph/store.py:597`):

> *"Consistent whole-database copy via SQLite's online backup API. Copies committed state
> (including WAL) page-by-page, so it is safe to run while other connections read the same
> file."*

It even clears stale `-wal`/`-shm` on the destination. `admin_copy_db` should call it
instead of `shutil.copy2`.

---

## F6 — `_validate_sqlite_file(final=True)` is a tautology and duplicates the schema

**Confidence: high (code-confirmed).**

`admin_upload_db`, `app.py:962-964`:

```python
_validate_sqlite_file(tmp_path, final=False)
await asyncio.to_thread(_migrate_sqlite_file, tmp_path)
_validate_sqlite_file(tmp_path, final=True)
```

`_migrate_sqlite_file` (`app.py:528`) does nothing but open `GraphStore(path,
readonly=False)`. That constructor calls `_create_core_tables()` (`store.py:124`), which
runs:

- `CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts ...` — `store.py:329`
- `CREATE TABLE IF NOT EXISTS search_items ...` — `store.py:336`
- `CREATE VIRTUAL TABLE IF NOT EXISTS search_items_fts ...` — `store.py:355`
- `ALTER TABLE nodes ADD COLUMN ...` for `source_version`, `source_material_hash`, `entity`,
  `claims_json`, `bridge_probe` — `store.py:389`

The `final=True` pass then asserts those exact tables and columns exist. They exist
*because `GraphStore` just created them one line earlier.* The check validates `GraphStore`
against itself and can never fail.

Worse, `_validate_sqlite_file` (`app.py:431`) hardcodes a **second copy of the schema** —
two `required_columns` dicts, ~60 lines listing every table and column. They will silently
desync the first time anyone edits `store.py`, and the desync will surface as a spurious
"invalid sqlite" rejection of a perfectly good upload.

**Keep:** `PRAGMA integrity_check`, plus one cheap "is this an llm-wiki DB and not some
stranger's sqlite" probe *before* we ALTER it (e.g. `nodes` table exists with a `body`
column). **Drop:** both column dicts and the `final=True` call. ~80 lines → ~10.

---

## F7 — `ADMIN_DB_LOCK` does not guard the DB-stack lifecycle

**Confidence: high on the code path; not reproduced. Race, so it will be intermittent.**

Every admin handler takes the lock (`app.py:877, 920, 1016, 1093, 1184`):

```python
async with ADMIN_DB_LOCK:
```

But `_ensure_building(db)` (`app.py:206`) — called from the `db_routing` middleware on
**every normal request** (`app.py:249`, `app.py:811`) — does not:

```python
def _ensure_building(db: str) -> None:
    if db not in STACKS and db not in building:
        asyncio.create_task(_bootstrap_db(db))
```

So while `admin_upload_db` sits between `_close_stack(db)` (`app.py:980`) and
`shutil.move(...)` (`app.py:985`), any user hitting `/llm-wiki/{db}/` re-enters
`_ensure_building`, spawns `_bootstrap_db`, and **recreates the stack and the sqlite file
underneath the move**. The same window exists in delete and rename.

The lock is protecting admin-vs-admin. It needs to protect admin-vs-traffic — i.e. it
belongs on the stack lifecycle (`_ensure_building` / `_bootstrap_db` / `_close_stack`), not
just on the admin handlers.

---

## F8 — Typo in the MCP backend origin default

**Confidence: certain.**

`mcp_server.py:33`:

```python
DEFAULT_BACKEND_ORIGIN = os.environ.get(
    "MCP_BACKEND_ORIGIN", "http://locahost:8000" # TODO Make this sync with backend auto
).rstrip("/")
```

`locahost` — missing the `l`. This is the fallback whenever `MCP_BACKEND_ORIGIN` is unset,
so an unconfigured MCP server fails DNS instead of reaching the local backend. One-character
fix, unrelated to everything else here; land it on its own.

---

## F9 — Hardcoded admin password default

**Confidence: certain. Security.**

`app.py:99`:

```python
ADMIN_PASSWORD = os.environ.get("WIKI_ADMIN_PASSWORD", "seigyo@rikiseisan")
```

A real-looking password is committed to the repo as the default. Any deployment that forgets
to set `WIKI_ADMIN_PASSWORD` exposes the full admin API — create, **delete**, replace, and
rename every wiki DB — behind a password that is public in git history.

`_require_admin` (`app.py`) **already handles empty correctly**, returning 503
"admin API disabled: set WIKI_ADMIN_PASSWORD". So the fix is to default to `""`, which makes
an unconfigured deployment locked rather than publicly writable:

```python
ADMIN_PASSWORD = os.environ.get("WIKI_ADMIN_PASSWORD", "")
```

Rotate the password wherever it is currently deployed — it is in git history regardless of
what we change now.

---

## Suggested order

1. **F8, F9** — one-liners, no dependencies, land immediately. Rotate the deployed password.
2. **F1** — run the verification console.log. That answer decides F2/F3/F4: repair (~-300
   lines) or delete (~-450 lines).
3. **F3/F4** — follows from the F1 decision.
4. **F6** — self-contained, ~-70 lines.
5. **F5** — swap `shutil.copy2` for the existing `GraphStore.snapshot_to()`.
6. **F7** — the real design fix; do it last, it needs the most thought.

Rough total: **~700 lines removable**, three real defects (F1 dead feature, F3 broken
click-through, F5 truncated copies), one race (F7), one security default (F9).
