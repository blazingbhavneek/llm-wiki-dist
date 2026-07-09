"""LLM-wiki graph package — four actors:

core.py        data models, settings, prompts, pure helpers (no deps)
store.py       GraphStore: SQLite persistence, thread-local connections
gateway.py     ModelGateway: chat LLM + embedder + reranker + settings
librarian.py   Librarian: ALL writes — job queue, enrichment drip, bootstrap
researcher.py  Researcher: ALL reads — hybrid search + ask() agent
"""
