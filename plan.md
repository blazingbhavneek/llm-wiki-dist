# Plan — team knowledge sync: detailed implementation spec

This file is written for an implementer model. Follow milestones in order. Every step has a **Verify**. If a verify fails: stop, write what failed into `handoff.md`, do not continue.

---

## 0. Context — what we're building and why

Developers each run a coding agent (GitHub Copilot CLI / Claude Code). When one agent discovers something durable ("bug X was caused by Y"), every other developer's agent should get that knowledge automatically.

Three pieces:

| Piece | Where | Status |
|---|---|---|
| **Wiki** (this repo) | Company server, FastAPI + sqlite at `app.py` + `graph/` | Built. Stores/searches/enriches notes of ANY kind. |
| **codegraph** | `vendor/codegraph` (TypeScript, CommonJS, MIT), already built (`npm run build` done, v1.2.0) | Vendored. Local per-repo code index: "where is X, who calls X". |
| **Bridge** | NEW — `bridge/` dir in this repo | To build. Small MCP server each dev runs; connects their agent to codegraph (local) + wiki (HTTP). |

### Hard rules (do not violate)

1. **The wiki database stays generic.** No code-specific models, columns, tables, or migrations. A "code note" is an ordinary note whose *text* mentions paths/symbols/commits. All code-awareness lives in the bridge.
2. **Never fork/edit `vendor/codegraph` source.** Import it as a library. It is ignored by this repo's git (`vendor/` in `.gitignore`).
3. **The bridge must never block on the wiki.** Wiki down → code answers still work, wiki parts silently skipped (short timeout, catch all errors).
4. Bridge code is TypeScript, CommonJS (`"module": "commonjs"`), Node >= 20 — must match codegraph's build (its tsconfig is CommonJS).

---

## 1. Reference — verified facts about existing code

Everything below was read from the actual source. Trust it; re-check only if something errors.

### 1.1 Wiki HTTP API (from `app.py`)

Base URL: configurable, dev default `http://localhost:51023` (see `frontend/src/api.js:4`). No auth currently.

**Search** — `GET /api/search?q=<urlencoded text>&limit=<n>`
Returns JSON array of node objects. Node fields (from `Node` in `graph/core.py:266`): `id`, `body`, `type` (`"endogenous"|"exogenous"|...`), `title`, `summary`, `claims` (list of strings), `keywords`, `status`, `created_at`, `updated_at`, plus source_* fields. `limit` capped at 200 server-side.

**Ask** — `POST /api/ask` body `{"question": "...", "overrides": null}`
Runs the wiki's own research agent (may take tens of seconds — use a 120 s timeout). Returns an answer dict; inspect one real response with curl before coding against it.

**Create note** — `POST /api/exogenous` body:
```json
{"body": "...markdown...", "source_node_ids": [], "origin": "user@host", "question": "short title or null"}
```
Returns a **write job**, not the node: `{"id": "...", "status": "queued", "type": "create_exogenous", ...}`.
Poll `GET /api/write-jobs/{id}` until `status` is `done` (result contains `{node, assimilating, message}`) or `failed`/`cancelled`. Poll pattern to copy: `waitForJob` in `frontend/src/api.js:19` (400–900 ms interval).
Note: `origin` is only used for title fallback server-side; it is NOT persisted as a node column — attribution must ALSO go in the note body footer (see §1.3).

**Write-job dispatch** lives in `Librarian._dispatch_job` (`graph/librarian.py:250`) — Milestone 4 adds a new job type there.

**Why no schema change is needed for search:** `Librarian._build_search_items` (`graph/librarian.py:1052`) chunks every note's title/summary/claims/body into `search_items` with FTS + vectors. Any symbol or path written in the body text is findable by hybrid search automatically.

### 1.2 codegraph as a library (from `vendor/codegraph/src/`)

Package name: `@colbymchenry/codegraph`, `main: dist/index.js`, no `exports` field → deep imports into `dist/` are allowed and are the intended embedding path (their issue #354).

```ts
// public entry (dist/index.js)
import { CodeGraph, MCPServer } from '@colbymchenry/codegraph';
// deep import for tool execution (dist/mcp/tools.js)
import { ToolHandler, getStaticTools, ToolResult } from '@colbymchenry/codegraph/dist/mcp/tools';
```

Verified signatures (`src/index.ts`, `src/mcp/tools.ts`):

- `CodeGraph.open(projectRoot: string, { sync?: boolean, readOnly?: boolean }): Promise<CodeGraph>` — opens an EXISTING `.codegraph/` index (line 295). Throws if not initialized — catch and tell the user to run `codegraph init`.
- `new ToolHandler(cg: CodeGraph | null)` (line 839).
- `handler.execute(toolName: string, args: Record<string, unknown>): Promise<ToolResult>` (line 1353).
- `ToolResult = { content: [{ type: 'text', text: string }], isError?: boolean }` (line 492).
- `getStaticTools(): ToolDefinition[]` — the default tool list (just `codegraph_explore`; `CODEGRAPH_MCP_TOOLS` env var widens it). `ToolDefinition` has `name`, `description`, `inputSchema` — exactly the shape MCP `tools/list` wants.
- Main tool: `codegraph_explore`, args `{ query: string, projectPath?: string }`.

Do NOT use codegraph's `MCPServer` class directly (it brings daemon/proxy machinery we don't want between us and the responses). Use `CodeGraph.open` + `ToolHandler`.

### 1.3 The refs footer — the only "protocol" we invent

When the bridge saves a note about code, it appends this plain-text footer to the note body:

```

---
wiki-refs: <repo-name> @ <full-commit-sha>
- <relative/path.py> :: <Qualified.symbol>
- <another/path.ts>
by: <user>@<hostname>
```

- `repo-name`: last path segment of `git remote get-url origin` (strip `.git`), else basename of repo root.
- One `- path :: symbol` line per ref; `:: symbol` optional.
- Parse with anchored regexes (see Milestone 3). The wiki never interprets this — it's just searchable text.

---

## Milestone 1 — bridge skeleton (codegraph passthrough)

Goal: `bridge/` is an MCP stdio server exposing `codegraph_explore`; answers come from the local index. No wiki yet.

### 1. Create `bridge/` layout

```
bridge/
  package.json
  tsconfig.json
  src/
    main.ts        # entry: parse env, start server
    server.ts      # MCP wiring (SDK server, tool registration)
    codegraph.ts   # CodeGraph.open + ToolHandler wrapper
    wiki.ts        # (Milestone 2) HTTP client
    refs.ts        # (Milestone 2/3) footer build/parse + git helpers
    trace.ts       # (Milestone 4) trace buffer + posting
  __tests__/
    refs.test.ts   # (Milestone 3)
```

`bridge/package.json`:

```json
{
  "name": "team-memory-bridge",
  "version": "0.1.0",
  "private": true,
  "main": "dist/main.js",
  "bin": { "team-memory-bridge": "dist/main.js" },
  "scripts": {
    "build": "tsc",
    "dev": "tsc --watch",
    "test": "vitest run",
    "start": "node dist/main.js"
  },
  "dependencies": {
    "@colbymchenry/codegraph": "file:../vendor/codegraph",
    "@modelcontextprotocol/sdk": "^1.0.0"
  },
  "devDependencies": {
    "@types/node": "^20.19.30",
    "typescript": "^5.0.0",
    "vitest": "^2.1.9"
  },
  "engines": { "node": ">=20.0.0 <25.0.0" }
}
```

`bridge/tsconfig.json`: copy `vendor/codegraph/tsconfig.json`'s compilerOptions, keep `"module": "commonjs"`, `"target": "ES2022"`, set `"outDir": "./dist"`, `"rootDir": "./src"`, drop `noUnusedLocals`/`noUnusedParameters` (they fight incremental work), exclude `__tests__`.

Precondition: `vendor/codegraph/dist/` exists (already built). If missing: `cd vendor/codegraph && npm install && npm run build`.

### 2. `src/codegraph.ts`

```ts
import { CodeGraph } from '@colbymchenry/codegraph';
import { ToolHandler, getStaticTools } from '@colbymchenry/codegraph/dist/mcp/tools';

export interface CodeIndex {
  tools: ReturnType<typeof getStaticTools>;
  execute(name: string, args: Record<string, unknown>): Promise<{ text: string; isError: boolean }>;
  projectRoot: string;
}

export async function openIndex(projectRoot: string): Promise<CodeIndex> {
  const cg = await CodeGraph.open(projectRoot, { sync: true });   // throws if no .codegraph/
  const handler = new ToolHandler(cg);
  return {
    tools: getStaticTools(),
    projectRoot,
    async execute(name, args) {
      const r = await handler.execute(name, args);
      return { text: r.content.map(c => c.text).join('\n'), isError: !!r.isError };
    },
  };
}
```

If TypeScript complains about the deep-import types, add a local `declare module '@colbymchenry/codegraph/dist/mcp/tools';` in `src/vendor.d.ts` and type things loosely — do not fight it.

### 3. `src/server.ts` + `src/main.ts`

Use `@modelcontextprotocol/sdk`: `Server` from `server/index.js`, `StdioServerTransport` from `server/stdio.js`, request schemas `ListToolsRequestSchema` / `CallToolRequestSchema` from `types.js`.

- `tools/list` → return `index.tools` mapped to `{ name, description, inputSchema }`.
- `tools/call` → `index.execute(name, args)` → return `{ content: [{ type: 'text', text }], isError }`.
- Project root: env `BRIDGE_PROJECT_ROOT`, default `process.cwd()`.
- All logging to `process.stderr` (`console.error`) — **stdout is the MCP transport, never write logs to it**.
- `main.ts`: open index, build server, connect transport, on failure print readable error to stderr and `process.exit(1)`.

### 4. Build + smoke test

```bash
cd bridge && npm install && npm run build
```

Smoke over raw stdio (run from repo root, which has a `.codegraph/` index):

```bash
printf '%s\n%s\n%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"codegraph_explore","arguments":{"query":"GraphStore transaction"}}}' \
 | BRIDGE_PROJECT_ROOT=$(pwd) node bridge/dist/main.js
```

**Verify:** the id:2 response contains `"Found"` and Python source from `graph/store.py`; nothing but JSON-RPC on stdout.

### 5. Register with an agent (manual, for the user)

Add to repo-root `.mcp.json` (create if absent) an entry the user can test with Claude Code:

```json
{ "mcpServers": { "team-memory": {
    "command": "node",
    "args": ["bridge/dist/main.js"],
    "env": { "BRIDGE_PROJECT_ROOT": "." } } } }
```

**Verify (user does this):** agent lists `codegraph_explore` from `team-memory` and answers a "who calls X" question through it.

---

## Milestone 2 — wiki read + write from the bridge

Goal: every explore answer carries relevant team notes; agent can save/ask explicitly. **Zero server changes.**

### 1. `src/wiki.ts` — HTTP client

Env: `WIKI_URL` (no default → wiki features disabled when unset; log one stderr line at startup saying so), `WIKI_TIMEOUT_MS` (default `5000` — measured live: hybrid search takes ~3 s, a 1500 ms default made the piggyback silently skip every time).

```ts
export interface WikiNote { id: string; title: string; summary: string; body: string; created_at: string; }

export class WikiClient {
  constructor(private baseUrl: string, private timeoutMs: number) {}
  private async req(path: string, init?: RequestInit, timeoutMs = this.timeoutMs): Promise<any> { /* fetch + AbortController; throw on !ok */ }

  // NEVER throws — piggyback path must not break code answers.
  async search(q: string, limit = 5): Promise<WikiNote[]> {
    try { return await this.req(`/api/search?q=${encodeURIComponent(q)}&limit=${limit}`); }
    catch (e) { console.error(`wiki search skipped: ${e}`); return []; }
  }

  async ask(question: string): Promise<string> { /* POST /api/ask, timeout 120_000, return JSON.stringify or the answer field after inspecting real response */ }

  // POST /api/exogenous → poll /api/write-jobs/{id} (500 ms interval, 180 s cap — the job
  // runs enrichment synchronously and takes ~2 min live) → return node id or throw
  async saveNote(body: string, origin: string, question: string | null): Promise<string> { /* ... */ }
}
```

`saveNote` polling: mirror `waitForJob` in `frontend/src/api.js:19` — statuses `queued|running` keep polling; `failed` → throw `job.error`; `done` → `job.result.node.id` (unwrap `{node, assimilating}` like `unwrapAdd` at `frontend/src/api.js:53`).

### 2. Piggyback on `codegraph_explore`

In the `tools/call` handler, after a successful codegraph result:

1. `notes = await wiki.search(args.query, 3)` (skip entirely if no `WIKI_URL`).
2. If any notes, append to the returned text:

```
\n\n── チームの知見（Wikiより） ──\n
### <title>\n<summary or first 400 chars of body>\n（保存日時 <created_at>）\n
```

All agent-LLM-visible text (tool descriptions, injected sections, tool-result errors) is Japanese — the product is for Japanese users. Protocol tokens (`wiki-refs:`, `by:`, JSON keys, node ids) stay ASCII.

3. Cap each note block at ~600 chars; never let the wiki section exceed ~2000 chars.

The wiki call runs AFTER codegraph succeeds and inside its own try/catch — failure returns the codegraph text unchanged.

### 3. Two new bridge tools

Register alongside the codegraph tools (only when `WIKI_URL` is set):

**`wiki_ask`** — inputSchema `{ question: string }`. Description: 「チームの知識Wikiに質問します。検索より低速です（サーバー側でリサーチエージェントが動きます）。ローカルのコードコンテキストだけでは足りない場合に使ってください。」
Handler: `wiki.ask(question)` → text.

**`save_note`** — inputSchema:
```json
{ "title": {"type": "string"},
  "body": {"type": "string", "description": "The discovery, markdown"},
  "refs": {"type": "array", "items": {"type": "object", "properties": {
      "path": {"type": "string"}, "symbol": {"type": "string"}}, "required": ["path"]}} }
```
Description must carry the save policy verbatim: 「恒久的で再利用可能な発見をチームWikiに保存します：根本原因、不変条件、落とし穴、意思決定。セッションの経過報告、自明な事実、コードを数秒読めば分かることは保存しないでください。コードに関する発見の場合は、関係するファイル/シンボルを refs に列挙してください。」
Handler:
1. Build footer via `refs.ts` (next step) from `refs`, current repo, `git rev-parse HEAD`.
2. `wiki.saveNote(body + footer, origin, title)` where `origin` = `` `${os.userInfo().username}@${os.hostname()}` ``.
3. Return `Saved as node <id>`.

### 4. `src/refs.ts` — footer builder + git helpers

```ts
export interface Ref { path: string; symbol?: string }
export function repoName(root: string): string   // git remote get-url origin → basename minus .git; fallback path.basename(root)
export function headSha(root: string): string | null   // git rev-parse HEAD, null on failure
export function buildFooter(refs: Ref[], repo: string, sha: string | null, origin: string): string
```

Use `child_process.execFileSync('git', [...], { cwd: root })` wrapped in try/catch. Footer format exactly as §1.3.

**Verify Milestone 2** (wiki running locally — `uvicorn app:app` or however `scripts/smoke.sh` starts it):
1. Smoke-test `save_note` over raw stdio (same printf pattern as Milestone 1, tool `save_note`, body "Test discovery: GraphStore uses thread-local connections", refs `[{"path":"graph/store.py","symbol":"GraphStore"}]`) → response contains `Saved as node`.
2. `curl 'http://localhost:51023/api/search?q=GraphStore+thread-local'` → the note appears.
3. Explore smoke test from Milestone 1 again → output now ends with `── チームの知見 ──` section containing that note.
4. Stop the wiki server, rerun explore smoke → codegraph answer still returned, no error, stderr shows "wiki search skipped".

---

## Milestone 3 — staleness flagging (client-side git)

Goal: when a served note references code that changed since the note's commit, say so.

### 1. Footer parsing in `refs.ts`

```ts
export interface ParsedRefs { repo: string; sha: string | null; refs: Ref[] }
export function parseFooter(body: string): ParsedRefs | null
```

Regexes:
- `/^wiki-refs:\s*(\S+)\s*@\s*([0-9a-f]{7,40})\s*$/m` → repo, sha (group 2 optional: also accept `wiki-refs: <repo>` with no `@ sha`).
- Each following line `/^-\s+(\S+)(?:\s*::\s*(\S+))?\s*$/` until a non-matching line → path, symbol.

### 2. Staleness check

```ts
export function staleness(parsed: ParsedRefs, projectRoot: string): { stale: boolean; changed: string[] } | null
```

- Return `null` (unknown, say nothing) when: `parsed.sha` missing, `parsed.repo !== repoName(projectRoot)`, or git fails (e.g. sha not in local history — shallow clone).
- Else `execFileSync('git', ['diff', '--name-only', `${sha}..HEAD`], { cwd: projectRoot })`, intersect output lines with `parsed.refs` paths.
- Intersection empty → `{ stale: false, changed: [] }`; else `{ stale: true, changed }`.

### 3. Wire into the piggyback formatter and `wiki_ask`

For each wiki note, run `parseFooter(note.body)`; if parsed, append one line to its block:
- fresh → `✅ <sha7> 以降、参照ファイルに変更なし`
- stale → `⚠️ 古い可能性あり：<sha7> 以降に変更されたファイル: <paths> — 信頼する前に確認してください`
- null → nothing.

### 4. Tests — `bridge/__tests__/refs.test.ts` (vitest)

Pure-function tests, no git spawning: `buildFooter` → `parseFooter` round-trip; footer with no sha; body without footer → null; path line without symbol. For `staleness`, factor the git call behind an injectable `(args: string[]) => string` so tests stub it (one stubbed test: diff returns one of the ref paths → stale true).

**Verify:** `cd bridge && npm test` green. Manual: save a note referencing a file, `git commit --allow-empty` then modify+commit that file, rerun explore → note shows `⚠️ 古い可能性あり`.

---

## Milestone 4 — automatic capture (the only server change)

Goal: discoveries get captured even when nobody calls `save_note`. Bridge posts raw session context; the SERVER's LLM decides what (usually nothing) is worth keeping. Endpoint stays generic — any raw text (meeting transcript, log dump) works.

### 4a. Server: `POST /api/ingest-raw`

1. `app.py`: next to `ExogenousBody`, add `class IngestRawBody(BaseModel): text: str; origin: str | None = None`. Endpoint (place next to `create_exogenous`, `app.py:480`):

```python
@app.post("/api/ingest-raw")
async def ingest_raw(payload: IngestRawBody) -> dict:
    return await _enqueue("ingest_raw", {"text": payload.text, "origin": payload.origin})
```

2. `graph/librarian.py` `_dispatch_job` (line 247): add

```python
if job.type == "ingest_raw":
    nodes = self.ingest_raw(job.payload["text"], job.payload.get("origin"))
    return {"extracted": len(nodes)}
```

3. `Librarian.ingest_raw(text, origin)` — new method near `create_exogenous_node` (`graph/librarian.py:~650`):
   - Truncate `text` to a sane cap (e.g. 60k chars, keep the tail — later context is denser).
   - Call the LLM through `self.gateway` using the SAME structured-output pattern the librarian already uses elsewhere (find an existing `self.gateway` structured call in `librarian.py` and mirror it exactly — model class in `graph/core.py`'s structured-outputs section, prompt string next to the other prompts in `core.py`).
   - New pydantic output model in `core.py`:

```python
class ExtractedDiscovery(BaseModel):
    title: str
    body: str

class DiscoveryExtraction(BaseModel):
    discoveries: list[ExtractedDiscovery] = Field(default_factory=list)
```

   - Extraction prompt (add to `core.py` prompts; Japanese, like every other prompt; tune later): *「あなたは開発者のコーディングエージェントが残した生の作業記録（トランスクリプト）を読みます。恒久的で再利用可能な発見のみを抽出してください：根本原因、不変条件、落とし穴、理由を伴う自明でない意思決定。ルール：ほとんどのトランスクリプトには恒久的な内容は何も含まれていません。その場合は空のリストを返してください。経過の説明、計画、TODOのような雑談、コードを読めば明らかな事実は決して抽出しないでください。各発見は単体で理解できなければなりません（タイトルと、関係するファイル・シンボル・コミットを本文中に明記した本文）。」*
   - For each discovery: `self.create_exogenous_node(body=d.body, source_node_ids=[], origin=origin, question=d.title)`. Return list of created nodes.
   - Wrap the LLM call so a gateway failure raises normally (job → `failed`; that's fine and visible in `/api/write-jobs`).

**Verify 4a:**
```bash
curl -s -X POST localhost:51023/api/ingest-raw -H 'Content-Type: application/json' \
  -d '{"text":"agent session: found that GraphStore commits per write because sqlite WAL ...; root cause of bug #12 was the reranker returning scores unsorted — fixed by sorting in gateway.rerank","origin":"test@pc"}'
# → job json; poll /api/write-jobs/<id> until done; expect extracted >= 1
curl -s 'localhost:51023/api/search?q=reranker+unsorted' | python -m json.tool | head
```
Also: post garbage text ("asdf hello lunch plans") → `extracted: 0`, no junk node created.

### 4b. Bridge: trace buffer + posting

`src/trace.ts`:
- In-memory array; every `tools/call` appends one entry: `[iso-time] TOOL <name> QUERY <args.query or title>\nRESULT (first 500 chars)`. (Honest limitation, noted here deliberately: an MCP server sees only its own tool traffic, not the agent's full conversation — good enough: it captures what was investigated and what came back.)
- Flush = join entries + `wiki POST /api/ingest-raw {text, origin}` fire-and-forget (catch + stderr), then clear.
- Flush triggers: every `TRACE_FLUSH_MS` (default 10 min, only if ≥ 3 entries) and on `process.on('SIGINT'|'SIGTERM')` / stdin close before exit.
- Disabled entirely when `WIKI_URL` unset or `BRIDGE_NO_TRACE=1`.

**Verify 4b:** run explore smoke a few times with `TRACE_FLUSH_MS=2000`, wait, check wiki `/api/write-jobs` shows an `ingest_raw` job.

---

## Milestone 5 — pilot readiness (small, do last)

1. `bridge/README.md`: install (node ≥20, `npm install && npm run build` in `vendor/codegraph` then `bridge/`), env vars table (`WIKI_URL`, `BRIDGE_PROJECT_ROOT`, `WIKI_TIMEOUT_MS`, `TRACE_FLUSH_MS`, `BRIDGE_NO_TRACE`, `CODEGRAPH_MCP_TOOLS`), MCP config snippets for Copilot CLI and Claude Code.
2. Reuse metric (server, tiny): in `graph/researcher.py`, where the ask router decides to reuse a cached/existing note (search for the early-exit/`reuse` path), add one `log.info("reuse_hit node=%s", node_id)` — grep-able later; no schema.
3. Leftover chores if time remains: run each `graph/cli.py` subcommand once, fix or delete broken ones; move `max_reads`/`max_agents` magic numbers into named `Settings` fields.

---

## Out of scope (do NOT build)

- No `CodeRef` model / JSON columns / code-aware indexing in the wiki DB.
- No MCP server inside the wiki (`mcp_server.py` idea is dead — bridge translates MCP↔REST).
- No auth layer yet (company LAN assumption; revisit before real pilot).
- No editing `vendor/codegraph`; no repo packers, CocoIndex, Joern/CodeQL, Zoekt.
- No server-side git, file watching, or repo clones.
