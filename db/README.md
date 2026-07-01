# db

Storage backends for the graph package.

The graph code imports `db.Database`. That alias is the switch point:

- [__init__.py](/home/seigyo/llm-wiki/db/__init__.py:1)

To try a different backend, change that one import.

## Shared Design

All backends are meant to expose the same public API used by `graph/engine.py`
and `graph/edges.py`.

That shared contract lives in:

- [base.py](/home/seigyo/llm-wiki/db/base.py:1)

The shared database shape is:

- node storage
- edge storage
- source/version tracking
- keyword search
- vector storage and vector search

So every backend needs to support the same operations:

- `upsert_node`, `get_node`, `get_all_nodes`, `get_nodes_by_document`
- `upsert_edge`, `get_all_edges`, `get_edges_for_node`
- `get_outgoing_edges`, `get_incoming_edges`
- `record_source`, `get_source`
- `keyword_search`
- `ensure_vec_tables`, `set_vector`, `vector_search`

This shared API is the part that should stay stable. The internal storage
strategy can differ.

## The 3 Backends

### 1. `RawSqliteDatabase`

File:

- [raw_sqlite.py](/home/seigyo/llm-wiki/db/raw_sqlite.py:1)

This is the current implementation that used to live under `graph/`.

Design:

- normal tables use raw `sqlite3`
- keyword search uses SQLite `FTS5`
- vector search uses `sqlite-vec`

Pros:

- already proven by the current graph tests
- single-file local database
- no new dependency beyond what the graph already used

Cons:

- most verbose to read
- lots of handwritten SQL

Use this when:

- you want the current behavior
- you want the least risk

### 2. `SQLModelDatabase`

File:

- [sqlmodel.py](/home/seigyo/llm-wiki/db/sqlmodel.py:1)

Design:

- normal relational tables use `SQLModel`
- keyword search still uses raw SQLite `FTS5`
- vector search still uses raw SQLite `sqlite-vec`

This is the “make the normal DB code easier to maintain” option.

Pros:

- node/edge/source CRUD is easier to read than raw SQL
- still keeps the current SQLite-based search setup
- smallest conceptual change from the current architecture

Cons:

- not a full removal of SQL
- still needs raw SQL for `FTS5` and `sqlite-vec`
- adds a new dependency: `sqlmodel`

Use this when:

- your main problem is maintainability of table CRUD code
- you want to keep SQLite and current search behavior

### 3. `LanceDatabase`

File:

- [lancedb.py](/home/seigyo/llm-wiki/db/lancedb.py:1)

Design:

- subclasses `SQLModelDatabase`
- keeps graph tables in SQLite/SQLModel
- keeps keyword search in SQLite `FTS5`
- replaces vector storage/search with LanceDB

So this is not a full graph-store rewrite. It is mainly a vector backend swap.

Pros:

- cleaner vector/search abstraction to compare against `sqlite-vec`
- still keeps the graph tables simple
- useful for testing whether LanceDB is easier to work with

Cons:

- now storage is split across two systems
- graph state and vector state are no longer in one engine
- adds another dependency: `lancedb`
- more moving parts than the SQLModel-only option

Use this when:

- you specifically want to compare vector-layer ergonomics
- you want to test LanceDB without rewriting graph logic

## What Is Actually Shared?

Shared by all 3:

- the public database API
- `Node` / `Edge` domain models from [graph/models.py](/home/seigyo/llm-wiki/graph/models.py:1)
- the graph engine logic in `graph/engine.py`
- the edge logic in `graph/edges.py`

Not fully shared by all 3:

- the storage internals
- where vectors live
- how CRUD is implemented

In practice:

- `RawSqliteDatabase` is the reference implementation
- `SQLModelDatabase` changes table CRUD internals
- `LanceDatabase` changes vector internals on top of `SQLModelDatabase`

## Recommended Default

If the goal is easier maintenance, the intended order is:

1. `RawSqliteDatabase` for stability
2. `SQLModelDatabase` for cleaner code
3. `LanceDatabase` only if the vector layer also feels worth changing

Today the default switch in `db/__init__.py` still points to
`RawSqliteDatabase` so existing graph behavior stays unchanged.
