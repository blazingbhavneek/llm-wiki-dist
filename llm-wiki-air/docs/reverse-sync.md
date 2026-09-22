# GROWI → wiki reverse sync (research)

Forward sync is `wiki/` → GROWI (`main.py sync|publish`). This note covers the
other direction: a user edits a page in the GROWI UI and the change should
reach the local `wiki/` state almost immediately. What to *do* with a pulled
change is a separate decision; this document only covers detection and the
existing apply path.

Verified against GROWI `8.0.5` source (`growilabs/growi` master) and a local
`7.4.2` instance (Mongo shapes checked directly; the API itself could not be
exercised because the local token is rejected).

## What already exists

`GrowiPublisher.pull_changes` (`graph/growi/client.py`) together with
`pull_growi_once` (`publisher/pipeline.py`) is a working reverse sync:

1. For every entry in `ledger.published_pages` it calls `GET /_api/v3/page?pageId=`
   and compares the GROWI `revision` with the stored `revision_id`.
2. For a changed page it writes the recovered body (chunk markers stripped,
   permalinks restored) into `wiki/<doc>/<page>.md`, mirrors it into
   `_planning/pages/` and the state sidecars, flips `_planning/linker.json`
   to `pending`, and stores the new `revision_id` in the ledger.
3. It refuses (reports a conflict, blocks the document) when the document also
   changed locally, or when the GROWI page lost its ownership markers.

`main.py watch` runs it every `GROWI_WATCH_INTERVAL_SECONDS` (default 300 s,
`publisher/queue.py::serve`). The apply side is fine; the interval is long
only because **detection is O(N) requests per pass**.

## Change-detection options

| # | Signal | Latency | Auth | Cost | Catches | Misses / caveats |
|---|---|---|---|---|---|---|
| 1 | `GET /_api/v3/pages/list?path=/<target>&limit=20` — sorted `updatedAt desc`, trash excluded; returns `_id`, `path`, `revision`, `updatedAt`, `lastUpdateUser`, `totalCount` | poll 2–5 s | existing API token (`acceptLegacy: true`) | 1 request | edits, reverts, new pages (id not in ledger) | renames made without "update metadata" (`updatedAt` unchanged); deletes only show as the page vanishing — catch with a full walk (`limit=100`, ~7 calls for 600 pages) every few minutes or when `totalCount` changes |
| 2 | `GET /_api/v3/activity?limit=50&offset=0` — audit log, `createdAt desc`; `action` (`PAGE_CREATE/UPDATE/RENAME/DELETE/REVERT/RECURSIVELY_*`), `target` (page id), `user` | poll 2–5 s | **admin** token and `AUDIT_LOG_ENABLED=true` on GROWI (not set in `growi-stack/docker-compose.yml`) | 1 request | everything, including rename and delete, with who/when | ops dependency; quirk: an absent `offset` skips the newest record, always send `offset=0`; check `AUDIT_LOG_ACTION_GROUP_SIZE` includes the page actions |
| 3 | socket.io push `page:update` / `page:delete` (`apps/app/src/server/service/system-events/sync-page-status.ts`) | instant | session cookie only (`express-session` + passport); the API token is **not** honoured — an anonymous handshake answers `Login is required to connect.` unless guest read is enabled. Log in with `POST /_api/v3/login` (`loginForm[username]`, `loginForm[password]`) and keep the cookie jar | 0 | payload `S2cMessagePageUpdated = {pageId, revisionId, revisionBody, revisionUpdateAt, revisionOrigin, remoteLastUpdateUser}` — full body, no REST follow-up | events are emitted only into room `page:<id>`: the client must `emit('join:page', {pageId})` for every ledger page and re-join after reconnect / publish; no rename event; `page:create` is unobservable (unknown id); needs `python-socketio` plus session-expiry handling |
| 4 | GROWI-initiated webhook: Admin → Slack integration (legacy) → *Incoming Webhook URL* set to our own endpoint; Admin → Notification → Global notification rule on `/<target>/*` for `pageCreate`, `pageEdit`, `pageDelete`, `pageMove` | instant | none (GROWI pushes) | 0 | create / edit / delete / **move** (old → new path) | `@slack/webhook` POSTs Slack JSON; `text` holds `<siteUrl/<pageId>\|path>` mrkdwn to parse; needs an inbound endpoint reachable from the GROWI container (work compose sets `HTTP_PROXY` → add our host to `NO_PROXY`); the legacy path is used only while no Slack App integration is configured (`slack-integration.ts::postMessage`); no retry on delivery failure |
| 5 | MongoDB change stream on `revisions` / `pages` | instant | Mongo network access + `pymongo` | 0 | everything | the v8 stack already runs a replica set (`rs0`); the local 7.4 stack does not; 27017 must be exposed; couples us to GROWI's schema |

Notes on the REST shapes:

- `pages/list` uses `findListWithDescendants` → default sort
  `{updatedAt: -1}` (`obsolete-page.js::findListFromBuilderAndViewer`),
  paginated with `page`/`limit`, `includeTrashed` only for `/trash`.
- `pages/recent` is the same query rooted at `/` (not path-scoped) with
  `includeWipPage`; strictly worse for our purpose.
- `revisions/list?pageId=&limit=&offset=` returns the revision history when a
  diff between the ledger revision and the new one is wanted.
- `page.updatedAt` is bumped on body updates and reverts; on rename only when
  the UI's "update metadata" option is chosen.
- Trashed pages keep their `_id`, get `status: "deleted"` and a `/trash/...`
  path, and drop out of `pages/list`.

## Recommendation

Option 1, with option 2 as an optional upgrade. It needs nothing from GROWI
admins, reuses the token and `GrowiClient.list_pages` we already have, and
turns the 300 s O(N) pass into a ~3 s O(1) check:

1. In `queue.serve`, every `growi_interval` (default lowered to ~3 s) call
   `list_pages(f"/{target}", page=1, limit=20)` and build
   `{page_id: revision_id}`.
2. Diff against `ledger.published_pages`. Nothing differs → done (our own
   publishes store the new revision, so they are no-ops). Something differs →
   run `pull_changes` restricted to those ids (add an optional `page_ids`
   filter so it stops fetching every page).
3. Every ~5 minutes, or whenever `totalCount` changes, do the full paginated
   walk to catch deletes, renames and new pages.

Everything downstream of detection stays in `pull_changes`, which is the hook
point for whatever should happen with a pulled change.

## Things to decide before wiring "what to do with a change"

- **Renames are reverted by forward sync.** `publish_pages` resolves pages by
  *path*, and `_trash_under` trashes every marked page not in the keep-set, so
  a UI rename is recreated at the old path and the renamed copy is trashed on
  the next publish. Either honour the rename locally (update `growi_path` in
  the ledger and the local filename) or keep the current "pipeline owns the
  path" behaviour explicitly.
- **Pages the pipeline does not own are ignored.** `00-目次` index pages and
  user-created pages under a document are not in the ledger; pull skips them
  and `main.py index` overwrites the index pages.
- **Own writes must not echo.** Publish stores the returned `revision_id`
  before releasing `_lock(project)`; the poller must take the same lock (as
  `pull_growi_once` does) so it never sees a half-published document.
