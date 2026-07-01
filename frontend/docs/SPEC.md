# Frontend Streamline Spec

Verbatim request from user (2026-07-02):

---

Lets streamline the screens first, you already know what we have, right? so I want this, A collapsible sidepanel on the left side (which shows the original documents we have ingested? i think we are storing inferred original doc names rightt? and we have chain linked them as well so we can fully reconstruct them if we just append them together right? so we can see a full list of docs we have, or we can group them by topic, for grouping them by topic it would be the ones in cluster names, and for each cluster name group the ones with same infered doc name together, show their previous and next links too, and by clicking view full doc i should be able to see their original full doc and their positions ok? this is collapsible left sidebar like vscode and all other desktop ui's have, then there is chatui, currently its width is limited which is fine. The right sidepanel, defaults to graph view, which is good to have, but from user's perspective its not that useful, so have a blank panel there by default which says to either open a doc from the left panel (make the left collapsible panel open by deafult) or ask the agent to make a doc, the right side panel will remain tab like (add a close tab button for newer panel, user should be able to open multiple tabs of docs together) and close them, except the graph one since we dont have a button for it, the graph one, the upload pdf one, and a settings one, the setting tab will contain ability to set llm endpoints and research depth, see the ../graph/models.py and see how user can control depth, so right panel now has settings, upload doc (add ability to add markdown docs directly too) graph and however many markdown docs user want to open, and any markdown can be edited by double clicking on it, right? and since we are rendering it from nodes we can only make change to that node right? then when we do change a markdown (either endogenous node or exogenous node) it should be added to the graph. We will discuss the cascading effect later so add that to a todo.md, what to do when endo nodes are changed and what to do when exo nodes are changed. Exo nodes are added when a user sends a AI Agent generated markdown to be added to graph, if it was uploaded then it goes as endo nodes since its source, all markdowns before sending to graph can be edited by human. Now do you understand this flow? Can you desing the UI for this? First save this message in SPEC.md in frontend folder then get to work

---

## Interpretation

### Left sidebar (collapsible, VS Code style, open by default)
- Lists ingested original documents (grouped by inferred `original_document_name`).
- Two view modes:
  - **Flat**: full list of docs.
  - **By topic**: group by `cluster` name; within each cluster group nodes sharing same `original_document_name`.
- Each doc shows its chain (prev/next links) so full doc can be reconstructed by appending nodes in order.
- "View full doc" -> opens reconstructed original full doc in a right-panel tab, showing node positions.

### Chat UI (center)
- Keep current limited width. Unchanged.

### Right panel (tabbed, VS Code style)
- Default = blank placeholder: "open a doc from left, or ask the agent to make one".
- **Persistent (non-closable) tabs**: Graph, Upload, Settings.
- **Closable tabs**: markdown docs (many open at once), each with a close (x) button.
- Graph view stays (was default; now demoted to a tab, no close button).
- **Upload tab**: PDF upload + direct markdown-doc creation.
- **Settings tab**: LLM endpoints + research depth (see `graph/models.py` Settings).

### Markdown editing
- Double-click a markdown to edit.
- Rendered from a single node -> edits apply to that one node only.
- On save, changed markdown (endo or exo node) is (re)added to the graph.
- Human can edit any markdown before it is sent to the graph.

### Node origin rules
- **Exogenous**: AI-agent-generated markdown that user sends to graph.
- **Endogenous**: uploaded source material.

### Deferred (see todo.md)
- Cascading effects when a node changes: endo-node change vs exo-node change.
