# GROWI reverse-sync plan

## Decision

Use GROWI's audit-log REST API as the change index and the page REST API as the
source of truth. This makes polling proportional to the number of edits, not
the number of pages. Improve the reverse-pull code already in this repository;
do not use Slack notifications, raw MongoDB change streams, or Socket.IO
payloads as the authoritative sync channel.

The minimum useful design is:

1. Poll recent page activities every 5–10 seconds with an overlap window.
2. Fetch only the page IDs named by new activities.
3. Feed changed pages through the existing marker and conflict checks.
4. Run a path-scoped complete inventory infrequently as a recovery check.
5. Emit a structured record saying what changed and what action was taken.

Socket.IO can later reduce latency, but only as a wake-up signal followed by a
REST reconciliation. GROWI Vault is an alternative for installations already
running GROWI 8+ with the necessary infrastructure; it is not the smallest fit
for this no-Git publisher.

## What already works in this repository

This is not a greenfield feature:

- `GrowiPublisher.pull_changes()` fetches each known GROWI page, compares its
  current revision with the ledger, and imports changes.
- Only content inside the publisher's managed markers is copied back. Text a
  user adds elsewhere on the GROWI page remains remote-only.
- If both the source document and GROWI changed, the document is blocked rather
  than overwritten.
- Missing ownership markers are treated as a conflict.
- A successful pull updates the local wiki file, generator resume state,
  linker state, and stored remote revision.
- The watch worker calls this pull while idle. The default interval is 300
  seconds and `--growi-interval` already makes it configurable.

For a small wiki, an immediate improvement is:

```bash
.venv/bin/python main.py -v watch --project projectA --growi-interval 10
```

Do not use that setting for thousands of pages. It is not a guaranteed
10-second SLA, the worker polls only while its queue is idle, and the current
implementation performs one page request after another.

## What GROWI actually exposes

### REST API: supported and suitable

`GET /_api/v3/page` accepts a page ID or path and returns the page with its
revision. It uses GROWI access-token scopes and fits the authentication already
used by this project.

`GET /_api/v3/pages/list` lists accessible descendants with pagination. Its
implemented inputs are `path`, `page`, and `limit`. It does **not** implement
the `updatedAfter` or cursor arguments currently sent by
`GrowiClient.list_pages()`, so those parameters must not be relied on.

`GET /_api/v3/pages/recent` returns recently updated accessible pages, but it
has offset pagination and no durable `since` cursor. It is useful as a cheap
hint, not as the only correctness mechanism.

Sources:

- [single-page REST route](https://github.com/growilabs/growi/blob/f6c5b34d143e27fcf4bc09bac548b3b28eefa79d/apps/app/src/server/routes/apiv3/page/index.ts#L153-L249)
- [recent-pages route](https://github.com/growilabs/growi/blob/f6c5b34d143e27fcf4bc09bac548b3b28eefa79d/apps/app/src/server/routes/apiv3/pages/index.js#L155-L271)
- [list-pages route](https://github.com/growilabs/growi/blob/f6c5b34d143e27fcf4bc09bac548b3b28eefa79d/apps/app/src/server/routes/apiv3/pages/index.js#L635-L737)

### Audit-log REST API: best change index for a large wiki

`GET /_api/v3/activity` and `POST /_api/v3/activity/list` return activities in
newest-first order and support action filters, a limit of up to 100, and offset
pagination. Page activities contain an activity ID, timestamp, action, target
page ID, and user. GROWI records distinct actions for page creation, update,
rename, recursive rename, deletion, and recursive deletion.

This is the best native detector for thousands of pages: each poll reads only
new activity rows, then the synchronizer fetches only the affected page IDs.
The cost therefore follows the number of edits rather than total wiki size.

Requirements and limitations:

- enable audit log access with `AUDIT_LOG_ENABLED=true`;
- page create/update/rename/delete actions are in GROWI's essential action set;
  either omit the server-side action filter and filter those actions in the
  client, or explicitly add them to `AUDIT_LOG_ADDITIONAL_ACTIONS` so the API
  accepts a narrow action filter; enabling the much noisier `MEDIUM` group is
  unnecessary for this feature;
- use an admin Personal Access Token with `read:admin:auditLog` scope;
- persist the last processed activity timestamp and IDs at that timestamp;
- poll with an overlap, deduplicate by activity ID, and keep following offsets
  until the saved checkpoint is reached;
- activity retention defaults to 30 days, so a stopped consumer must recover
  before expiry or use the inventory fallback;
- verify the endpoint and scope against the deployed GROWI version before
  implementation.

The activity event identifies what changed; `/_api/v3/page?pageId=...` still
provides the authoritative current body and revision. A delete action can be
handled immediately without waiting for a missing page to be discovered.

Sources:

- [audit-log API route and authentication](https://github.com/growilabs/growi/blob/f6c5b34d143e27fcf4bc09bac548b3b28eefa79d/apps/app/src/server/routes/apiv3/activity.ts#L274-L470)
- [page activity action groups](https://github.com/growilabs/growi/blob/f6c5b34d143e27fcf4bc09bac548b3b28eefa79d/apps/app/src/interfaces/activity.ts#L480-L580)
- [activity retention configuration](https://github.com/growilabs/growi/blob/f6c5b34d143e27fcf4bc09bac548b3b28eefa79d/apps/app/src/server/service/config-manager/config-definition.ts#L529-L553)

The separate `GET /_api/v3/revisions/changes` endpoint is not a global change
feed: it intentionally returns only edits made by the authenticated user. It
cannot detect edits made by other GROWI users.

### Socket.IO: fast hint, not a sync API

GROWI uses Socket.IO, not a plain WebSocket protocol. Relevant events include
`join:page`, `page:create`, `page:update`, and `page:delete`. An update payload
can contain the page ID, revision ID, revision body, update time, and user.

It is unsuitable as the primary reverse-sync mechanism because:

- a client must join each `page:<id>` room; there is no documented global page
  change subscription;
- new pages and some path changes cannot be discovered from rooms already
  joined;
- the handshake uses GROWI's browser session/passport authentication, not the
  REST access-token parser;
- events during a disconnect are lost;
- the event contract is internal and unversioned;
- GROWI excludes sockets associated with the editing user's room, which makes
  using the publisher's own account especially fragile.

If added later, use a separate bot session, join every known page, rejoin after
reconnect, and trigger a REST fetch for the reported page. Never write the
Socket.IO body directly to disk, and always run a full scan after reconnect.

Sources:

- [event names](https://github.com/growilabs/growi/blob/f6c5b34d143e27fcf4bc09bac548b3b28eefa79d/apps/app/src/interfaces/websocket.ts#L21-L62)
- [Socket.IO session authentication and rooms](https://github.com/growilabs/growi/blob/f6c5b34d143e27fcf4bc09bac548b3b28eefa79d/apps/app/src/server/service/socket-io/socket-io.ts#L87-L170)
- [page-event broadcasting](https://github.com/growilabs/growi/blob/f6c5b34d143e27fcf4bc09bac548b3b28eefa79d/apps/app/src/server/service/system-events/sync-page-status.ts#L94-L139)
- [update payload](https://github.com/growilabs/growi/blob/f6c5b34d143e27fcf4bc09bac548b3b28eefa79d/apps/app/src/server/models/vo/s2c-message.ts#L10-L43)

### Slack/global notifications: not a generic webhook

GROWI's global notification feature can notify Slack, Mattermost, email, and
IFTTT for page create/edit/delete/move events. It is a human notification
integration: delivery and payloads are not a durable page-change API, and page
visibility rules affect which notifications are sent. It may be useful for
alerts, but not for synchronization.

Source: [GROWI external notification documentation](https://docs.growi.org/en/admin-guide/management-cookbook/external-notification.html)

### GROWI Vault: official full-tree mirror, but heavier

GROWI 8 introduced GROWI Vault, a read-only Git interface over wiki pages. It
can provide `git clone`, `fetch`, and `pull` of an ACL-filtered Markdown tree.
It requires the vault manager, shared persistent storage, and a MongoDB replica
set for change streams. It does not support pushing edits back through Git and
does not mirror every GROWI feature.

Vault is a good option if the deployment already meets those requirements and
a whole-wiki filesystem mirror is wanted. Enabling Git infrastructure only for
this publisher would add more machinery than improving its existing REST pull.

Sources:

- [GROWI Vault overview](https://docs.growi.org/en/guide/features/vault.html)
- [GROWI Vault setup](https://docs.growi.org/en/admin-guide/management-cookbook/setup-vault.html)
- [vault-manager source documentation](https://github.com/growilabs/growi/blob/f6c5b34d143e27fcf4bc09bac548b3b28eefa79d/apps/growi-vault-manager/README.md)

### Direct MongoDB watching: reject

Watching the `pages` or `revisions` collections couples this tool to private
schema details, bypasses GROWI's ACL and application semantics, requires
database credentials, and still has to reconstruct multi-document changes.
If database change streams are acceptable, use the official Vault pipeline
that GROWI maintains rather than inventing another one.

## Target design

### Detection loop

Run a lightweight detector independently of document builds:

1. Request recent activities newest first with a limit of 100, then retain page
   create/update/rename/delete actions. Use a server-side action filter when
   the GROWI configuration permits it.
2. Continue through offsets until reaching an already-processed activity.
3. Deduplicate by activity ID and discard events outside the managed page IDs
   or configured root.
4. Coalesce multiple events for the same page, retaining delete and rename
   semantics.
5. Fetch the current body only for affected, non-deleted page IDs.
6. Apply changes under the existing publisher/pipeline lock.
7. Run `/_api/v3/pages/list` for the managed root hourly or daily as a safety
   reconciliation, with the interval chosen from the required deletion SLA.

The checkpoint must be durable. Store the newest completely processed
`createdAt` and all activity IDs observed at that timestamp in the existing
ledger. An overlap makes retries and same-timestamp events harmless. If more
than 100 changes occur between polls, pagination continues until the checkpoint
is found; normal idle polls remain one small request.

If admin audit-log access is unavailable, poll `/pages/recent` with the same
overlap and deduplication approach. That fallback detects creates, edits, and
usually moves without scanning all pages, but it cannot promptly prove a
deletion. Use an hourly/daily `/pages/list` inventory to recover deletions and
any offset-pagination race. A hard low-latency deletion requirement therefore
requires the audit API or a supported custom webhook.

### Applying a detected change

Reuse `pull_changes()` and its existing ownership markers. Extend its result so
each detection produces one structured log record containing:

```text
event=update|create|delete|rename
page_id=...
old_path=...
new_path=...
old_revision=...
new_revision=...
updated_at=...
updated_by=...
result=pulled|conflict|unmanaged|ignored
added_lines=N
removed_lines=N
```

Use Python's `difflib` for line counts if useful. Do not retain another full
copy of every remote body just for reporting.

Required behavior by change type:

| Remote change | Action |
| --- | --- |
| Managed text changed, local source unchanged | Pull marked text and update generator/linker state and ledger |
| Only text outside managed markers changed | Preserve it, advance the revision baseline, log `ignored` |
| Remote and local source both changed | Block the document and log `conflict`; overwrite neither side |
| Ownership markers removed or malformed | Block and log `conflict` |
| Page deleted | Block the document; do not silently recreate it on the next publish |
| Page renamed/moved | Block and report old/new paths until an explicit ownership policy is chosen |
| New page under the root | Report `unmanaged`; do not invent a source document mapping |
| Publisher's own revision observed | Deduplicate against the revision already written to the ledger |

Deletes and renames need explicit handling because the current per-ID pull
silently skips a missing page and the local mapping still owns its old path.

## Minimal implementation sequence

1. **Add the change-index call.** In `graph/growi/client.py`, request activities
   from `/activity/list`, retain page-change actions, paginate until the durable
   checkpoint, and deduplicate by activity ID.
2. **Fetch only changed bodies.** Pass affected known page IDs to the existing
   `GrowiPublisher`; fetch their current page and revision concurrently with a
   small fixed limit. No operation should scale with total page count during
   the fast loop.
3. **Classify safely.** In `publisher/pipeline.py`, keep the existing marker and
   document-hash conflict rules; add delete, rename, new-page, and
   outside-marker result classifications.
4. **Decouple detection from long builds.** In `publisher/queue.py`, let the
   detector run on its own interval and hand coalesced page IDs to the existing
   locked apply path.
5. **Expose and observe it.** In `main.py`, add a one-shot `pull` command for
   operations/testing and expose the activity-poll and recovery-scan intervals.
   Emit the structured records above.
6. **Add rare reconciliation.** Use the real paginated `/pages/list` contract
   hourly or daily, not every few seconds. Remove reliance on the unsupported
   `updatedAfter` and cursor arguments.
7. **Only if measured latency is still inadequate**, add Socket.IO wake-ups or
   adopt Vault. Neither belongs in the first implementation.

The ledger may need a small schema migration for last-seen remote path and
update metadata. It does not need a general event-store abstraction.

## Verification

Automated tests should cover these observable cases:

- a marked remote edit is reflected locally within one detection/apply cycle;
- an outside-marker edit does not alter local generated content;
- simultaneous local and remote edits block both overwrites;
- marker removal creates a conflict;
- delete, rename, and new-page events are classified without recreation or
  automatic import;
- a publisher-created revision does not cause a loop;
- restart after missed changes is recovered by the full inventory;
- more than 100 activities between polls are paginated without loss;
- duplicate and same-timestamp activities are applied once;
- recovery pagination returns all pages under the managed root;
- 401/403/timeout responses leave the ledger unchanged and retry with bounded
  backoff.

Before enabling writes, run the detector in log-only mode against one staging
project and compare every detection with GROWI's revision history.

## Rollout and operating target

1. Record the deployed GROWI version and verify the activity API response.
2. Enable audit-log API access, verify the required page actions are returned,
   and create a least-privilege admin-scoped token for the consumer.
3. Deploy activity-based detection in log-only mode.
4. Enable application for one project, then expand after conflict behavior is
   confirmed.
5. Enable hourly or daily inventory reconciliation and test checkpoint recovery.
6. Monitor detection latency, activity backlog, REST errors, conflicts, and
   requests per minute.

Suggested initial target: p95 detection within 15 seconds while idle, guaranteed
eventual detection within the configured recovery scan, and zero automatic
overwrites when both sides changed.

## Final recommendation

Implement the audit-log detector plus targeted REST page fetches and reuse the
current pull/merge logic. It requires no GROWI fork, new dependency, or direct
database access, and its fast-path cost depends on edit volume rather than page
count. Keep a rare inventory reconciliation. Treat Socket.IO as an optional
accelerator and Vault as an infrastructure-level alternative.

## Option comparison and best choice

| Option | Reliability | Build effort | Detection speed | Main problem |
| --- | --- | --- | --- | --- |
| Existing per-page REST pull at a shorter interval | Good for known pages | Configuration only | 10–30 seconds when idle | Misses deletes, moves, and new pages; one request per known page |
| Audit-log actions plus targeted REST fetch | **Best at scale** | **Small** | 5–10 seconds | Requires enabled audit log and admin-scoped token |
| `/pages/recent` plus rare inventory | Best no-admin fallback | Small | 5–10 seconds | Does not promptly detect deletion; offset feed needs overlap |
| Path-scoped REST inventory plus targeted fetch | Reliable recovery check | Small | Depends on interval | Every scan scales with total pages |
| Socket.IO plus REST reconciliation | Very good with fallback | High | Subsecond | Session authentication, page-room subscriptions, reconnect recovery, internal API |
| Slack/global notifications | Poor for syncing | Medium | Usually fast | Human notification channel, incomplete visibility and no delivery guarantee |
| GROWI Vault | Excellent whole-tree mirror | High operational effort | Near-real-time fetches | Requires GROWI 8+, vault-manager, Git workflow, shared storage, and MongoDB replica set |
| Direct MongoDB change streams | Potentially fast but fragile | High | Subsecond | Private schema coupling, ACL/security risks, and application semantics must be rebuilt |
| Custom GROWI webhook/fork | Can be excellent | Highest | Subsecond | Fork maintenance, signing, retries, compatibility, and deployment ownership |

### Winner at thousands of pages: audit log plus targeted REST fetch

This is the best-balanced option for a large wiki because it extends code that
already exists without repeatedly listing or fetching thousands of pages:

- it uses the same supported REST API, token authentication, ledger, ownership
  markers, and conflict checks as the current publisher;
- explicit activity actions identify creates, updates, deletes, and moves;
- one normal poll reads at most 100 events, regardless of total page count;
- only changed page bodies are downloaded;
- overlap, activity-ID deduplication, and a durable checkpoint make retries
  safe, while a rare inventory repairs gaps;
- it requires no GROWI fork, browser-session emulation, new service, direct
  database credentials, or additional dependency.

The shortest implementation should modify only the existing GROWI client,
publisher pipeline, queue loop, CLI, and their tests. Do not build Socket.IO,
Vault, an event database, or a custom webhook in the first version.

### Practical order

1. **Today:** set `--growi-interval 10` for an immediate improvement with no
   code change only on small projects; do not do this across thousands of pages.
2. **First implementation:** poll audit activities, retain page-change actions,
   and fetch only the affected page bodies.
3. **Reliability completion:** classify deletion, rename, new-page, and conflict
   cases, persist the checkpoint, and run a rare recovery inventory.
4. **Without admin audit access:** use `/pages/recent` as the fast hint and
   accept that deletion detection waits for the recovery inventory.
5. **Only after measurement:** add Socket.IO if a hard subsecond requirement
   justifies its authentication and reconnect complexity.

In short: poll changes frequently, not pages. Scan every page only as an
infrequent integrity check.
