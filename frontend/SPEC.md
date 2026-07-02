# Frontend Fixes and Additions Spec

## Original Request

See the frontend, currently, there are a few fixes additions:

1. the left sidebar has topic button, which is useless, remove it
2. In the right sidebar, its always in the document explorer mode, always either all the documents/topics, but i want to make it tabbed, if an agent execution completes in the chat section, it should open a tab in right sidebar, which hightlight what source nodes were used to make this answer.
3. The topic button we deleted in step 1, replace that with Explorer button, which opens the explorer tab on right sidebar where we can see all the docs. I think the logic for step 2 was already done in the graph part with highlights, right? port that to step 2.
4. in the markdown editor mode, there is a cancel button, but it doesnt exit the actual markdown editor split and stays there. Also when a markdown page opens from agent answer (the expand button when chat answers) it doesnt have a back button, a lot of time users want to back to previous screen that was open, not just chat, for exmaple i do a keyword search from top bar (the bottom bar is chat search) it gives me a list, and i open a page after that, but i cant go back from there, i have to make that query again, so make a back button for these kind of tabs.
5. Also recently an agent stop button was implemented, add that stop button in UI where the start chat button was there once the agent is running.

## Frontend Readthrough Findings

Date: 2026-07-02

### Relevant Files

- `src/App.jsx`
  - Owns shell state: `leftCollapsed`, `rightOpen`, `rightMode`, `centerView`.
  - Owns the single markdown workspace: `workspace`.
  - Owns chat state: `messages`, `activeAnswerId`, saved/add-in-progress answer ids, answer write statuses.
  - `LeftSidebar` currently includes a `topics` item. Its only behavior is `handleNav('topics')`, which opens the right rail and switches `rightMode` to `topics`.
  - `RightDocumentRail` always renders `DocSidebar`; there is no right-rail tab model yet.
  - `finalizeAnswer()` builds an `answer` object with `refs` and `citedIds`, patches the assistant message, and calls `openAnswerTab(answer, false)`.
  - `openAnswerTab(answer, false)` sets `workspace` to an answer workspace, `activeAnswerId`, and `focusIds`, but does not navigate away from chat unless `expand` is true.
  - `answerIds` is derived from `workspace.kind === 'answer'`, then passed only to `GraphCanvas`.
  - `openNode()`, `openFullDoc()`, and `openAnswerTab(answer, true)` all replace the center view with markdown and do not preserve a return destination.
  - `MarkdownView` is rendered without `onCancelEdit`, so its cancel button cannot exit edit mode.

- `src/components/DocSidebar.jsx`
  - Current right rail content.
  - Has an internal `documents` / `topics` mode toggle.
  - Lists reconstructed documents and topic groups from `buildLibrary()`.
  - Highlights the currently open node only through `activeTabId === doc:${node.id}`.
  - Has no concept of answer-source highlighting.

- `src/components/ChatPanel.jsx`
  - Shows assistant answers, references, and an expand button.
  - `onViewAnswer(answer)` currently opens the answer in the center markdown workspace.
  - Reference buttons call `onOpenNode(id)`.

- `src/components/GraphCanvas.jsx`
  - Already has the source-node highlight logic mentioned in the request.
  - Accepts `answerIds` and treats them as cited/used source nodes.
  - Adds `used` class to cited nodes and `current` class to open nodes.
  - `src/index.css` styles `.gnode.used` and `.mini-node.used` in green.

- `src/components/MarkdownView.jsx`
  - Supports `onCancelEdit`, but `App.jsx` does not pass it.
  - The cancel handler only invokes `onCancelEdit`; it does not reset local state by itself.
  - Edit mode is a split view with textarea plus preview.

- `src/data/docs.js`
  - Builds the document/topic library from raw graph nodes and `follows` edges.
  - Reconstructs full documents through `reconstructDocument(doc)`.

## Implementation Plan

### 1. Replace the left-sidebar Topic button with Explorer

- Remove the `topics` navigation item from `LeftSidebar`.
- Add an `explorer` navigation item using an explorer/document icon such as `FolderTree`.
- Add or update English and Japanese strings:
  - `shell.explorer`
  - optional title/aria text for opening the explorer.
- Change `handleNav()` so `view === 'explorer'`:
  - opens the right sidebar,
  - activates the right-rail Explorer tab,
  - preserves the existing explorer submode (`documents` or `topics`) instead of forcing topics.
- Keep the internal `All` / `Topics` toggle inside the Explorer tab, because the user still wants all docs/topics available there.
- Remove left-sidebar active-state special casing for `topics`; the Explorer button should be active when the right rail is open and the active right tab is Explorer.

### 2. Introduce a real right-sidebar tab model

Add right-rail state in `App.jsx`:

- `rightTabs`: dynamic tabs for answer-source views.
- `activeRightTabId`: defaults to `explorer`.
- Keep `rightMode` as the Explorer tab's internal mode: `documents | topics`.

Use two tab types:

- `explorer`
  - non-closable,
  - renders `DocSidebar`,
  - contains the existing all-documents/topics explorer.
- `sources`
  - closable,
  - created when an agent answer completes,
  - keyed by answer id, for example `sources:${answer.id}`,
  - shows the source nodes used for that answer.

Refactor `RightDocumentRail` into a tabbed right rail:

- Add a compact tab strip at the top.
- Always include the Explorer tab.
- Render one tab per completed answer that has `citedIds` or `refs`.
- Provide close buttons only on source tabs.
- If the active source tab is closed, fall back to Explorer or the next available source tab.
- Keep the existing right-sidebar collapse button in the top bar working as-is.

### 3. Create the answer source-node tab content

Add a new component, likely `AnswerSourcesSidebar.jsx`, or keep it local if small:

- Props:
  - `answer`
  - `sourceNodes`
  - `activeNodeId`
  - `onOpenNode`
  - `onViewAnswer`
- Resolve source nodes from `answer.citedIds` using `rawById`.
- Fall back to `answer.refs` labels when a node is not available locally.
- Show answer title/question in the tab content header.
- Show a count of cited source nodes.
- Render each source as a selectable row/card with:
  - title,
  - source/agent badge,
  - document name if present,
  - summary/body excerpt if present,
  - a highlighted visual treatment for "used in this answer".
- Clicking a source opens that node in the center markdown workspace.
- Include a small "view answer" action that calls `openAnswerTab(answer, true)` for users who want the full answer markdown.

### 4. Port the graph highlight logic to the right source tab

The graph already defines the correct data contract:

- `answer.citedIds` is the set of source node ids.
- `GraphCanvas` receives `answerIds`.
- `GraphCanvas` marks these as `used`.

Port that concept by creating a shared derived set:

- `activeSourceIds`
  - if the active right tab is a source tab, use that answer's `citedIds`.
  - otherwise, if the current center workspace is an answer, use that answer's `citedIds`.
  - otherwise empty.

Then:

- Pass `activeSourceIds` to `GraphCanvas` as `answerIds`.
- Pass `activeSourceIds` or the active source answer to the right source tab.
- Use the same semantic naming in UI classes, for example `used`, `current`, or `source-highlight`.
- Keep graph behavior unchanged, but let the right tab highlight the exact same source set.

### 5. Open a source tab when an agent answer completes

Update `finalizeAnswer()`:

- Build the existing `answer` object as today.
- Keep patching the chat assistant message.
- Keep `openAnswerTab(answer, false)` so the answer is stored and source ids are available without forcing center navigation.
- Add `openAnswerSourcesTab(answer)` after a successful answer.
- `openAnswerSourcesTab(answer)` should:
  - upsert a `sources:${answer.id}` tab,
  - set it active,
  - open the right sidebar,
  - set `activeAnswerId`.

For answer events with no answer body but with citations:

- Prefer opening a source tab instead of auto-opening the first cited node.
- Only auto-open the first cited node if there is no usable answer-source tab data.

### 6. Fix markdown editor Cancel

Add a `cancelEdit(id)` helper in `App.jsx`:

- Find the current workspace.
- Reset `draft.title` to `doc.title`.
- Reset `draft.markdown` to `doc.markdown`.
- Set `editing: false`.
- Preserve `busy`, `refs`, `positions`, `sourceIds`, and other metadata.

Pass it into `MarkdownView`:

- `onCancelEdit={() => cancelEdit(workspace.id)}`

Expected behavior:

- In split edit mode, clicking Cancel exits the split editor.
- Unsaved edits are discarded.
- The read-only markdown preview returns.

### 7. Add back navigation for markdown workspaces

The current center area has one active `centerView`, so opening a markdown page destroys the user's visible search/list/chat context. Add a lightweight center navigation stack.

Add state:

- `centerHistory`
  - array of entries like:
    - `{ centerView: 'search' }`
    - `{ centerView: 'chat' }`
    - `{ centerView: 'graph' }`
    - `{ centerView: 'upload' }`
    - `{ centerView: 'settings' }`
    - `{ centerView: 'markdown', workspaceSnapshot }` only if we need markdown-to-markdown back later.

Add helpers:

- `captureCenterReturnPoint()`
  - captures the current center before switching to markdown.
  - does not push duplicate consecutive entries.
  - skips transient loading states.
- `openWorkspace(next, options)`
  - default pushes a return point before `setCenterView('markdown')`.
  - support `{ preserveHistory: true }` or `{ pushHistory: false }` for internal updates.
- `goBackFromWorkspace()`
  - pops the latest return point.
  - restores `centerView`.
  - restores `workspace` only if returning to a previous markdown workspace.
  - for search, the existing `searchQuery` and `searchResults` state already remains in memory, so returning to `centerView: 'search'` should show the same result list without rerunning the query.

Add UI:

- Add a Back button to `MarkdownWorkspaceFrame`.
- Use a left-arrow icon from `lucide-react`.
- Show it when `centerHistory.length > 0`.
- The existing Collapse button can still close the workspace and go to chat.

Expected flows:

- Top keyword search -> result list -> open node -> Back returns to the same search result list.
- Chat answer -> expand answer -> Back returns to chat.
- Chat reference -> open node -> Back returns to chat.
- Graph -> open node -> Back returns to graph.
- Explorer right tab -> open node while search is visible -> Back returns to search.

### 8. Keep state interactions predictable

- Closing the markdown workspace with Collapse should keep the existing behavior unless explicitly changed: close workspace and return to chat.
- Back should be different from Collapse: Back restores the previous center screen.
- Starting a new chat should clear:
  - `messages`,
  - `workspace`,
  - `activeAnswerId`,
  - source tabs created from old answers,
  - `centerHistory`,
  - and return to chat.
- Closing the right sidebar should not delete source tabs.
- Reopening the Explorer button should show the Explorer tab, not remove answer source tabs.

### 9. Suggested verification

- Run `npm run build` from `frontend/`.
- Manual checks:
  - Left sidebar shows Explorer, not Topics.
  - Explorer opens the right rail Explorer tab.
  - Explorer still supports all documents and topics.
  - Asking the agent and receiving an answer creates/activates a right source tab.
  - Source tab rows match the answer citations and open nodes correctly.
  - Graph view still highlights used source nodes for the active answer/source tab.
  - Markdown edit Cancel exits split mode and discards unsaved edits.
  - Search results survive opening a result and pressing Back.
  - Answer expand from chat can Back to chat.

### 10. Agent stop button

- Use the backend's existing stop support:
  - `/api/ask/stream` emits `{ type: "run", run_id }` at stream start.
  - `POST /api/agent-runs/{run_id}/stop` requests cancellation.
  - The stream emits `{ type: "cancelled", run_id }` when cancellation lands.
- Track the active agent run id in `App.jsx`.
- While an agent run is active, replace the chat composer send button with a stop button in the same position.
- Disable submitting another chat message while an agent run is active.
- When stop is clicked:
  - call `api.stopAgentRun(runId)`,
  - show a stopping state on the button,
  - convert the streaming assistant message into a stopped/cancelled message when the stream reports cancellation.
- If a new chat is started while an agent run is active, request stop for the active run and clear local running state.

## Assumptions

- The source nodes used by an answer are `answer.citedIds`, built from backend `cited_node_ids`.
- The existing `refs` array is display metadata; `citedIds` remains the source of truth for highlighting and lookup.
- The Explorer tab should replace the old left-sidebar Topics shortcut, but the Explorer content should still allow topic grouping.
- This plan does not restore a full multi-document center tab bar. It adds only the requested right-rail tabs and a markdown back stack.
