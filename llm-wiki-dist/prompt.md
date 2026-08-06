# Realtime Speaking RAG Request

Build a new endpoint for a realtime speaking LLM using the existing wiki
database format. Do not change the database or replace the existing detailed
`/api/ask` documentation flow.

The main goal is very low time to first answer. The system should work like
this:

1. Break the user's question into small, specific subqueries.
2. Organize those subqueries into dependency levels. A later level may depend
   on facts found in an earlier level.
3. Allow multiple shallow subqueries in each level and process them in
   parallel.
4. For every subquery, cast an adaptive wide search net using the existing
   keyword, vector, chunk, and reranking data. Search more broadly when the
   first results are weak or incomplete.
5. Use one fast LLM call per shallow subquery to directly produce short,
   speakable facts from the retrieved information.
6. Every fact must include the reference node IDs that support it. Do not pass
   facts with missing or invented node IDs to later levels or to the speaker.
7. Pass accepted facts and their node IDs into the next dependent level.
8. If a level does not find enough information, insert another targeted search
   level. Announce the updated plan before processing the changed order.

Expose this as a level-wise SSE endpoint:

- First stream the complete ordered plan so the speaking client knows what is
  coming.
- If the plan changes, immediately stream the complete updated order.
- Stream each completed level as soon as it is ready.
- Include clean speech text, individual referenced facts, and the combined
  reference node IDs for that level.
- Start processing the next level immediately after emitting the current one.
  Do not wait for the client to finish speaking.

This hides latency: while the user hears Level 1, the server researches Level
2. Most questions should finish within three or four logical levels, with a
small number of additional recovery levels only when necessary.

Keep the pipeline simple and fast. Do not add separate evidence-card agents,
summary agents, reducer agents, verifier agents, Mermaid diagrams, decorative
formatting, or long documentation generation. Retrieval and reranking should
do the narrowing, and the shallow LLM calls should produce the actual facts.

The system must never fill missing information with a plausible explanation.
When the database does not contain enough support, return only the supported
parts and clearly mark the result as incomplete.
