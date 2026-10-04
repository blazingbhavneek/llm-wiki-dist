"""Local Qdrant index, built and owned by growi-search from the 目次 data blocks.

One point per section window, fact, page, document and folder (payload ``level``).
Vectors: dense ``text`` / ``gist`` / ``bridge`` when an embedder is configured, and sparse
``words`` (body BM25) / ``terms`` (names, keywords, search terms); Qdrant applies the IDF.
Qdrant runs in local file mode: no server. The folder is disposable; delete it and the
sync rebuilds everything from GROWI.

Section payloads (everything but facts) are also kept in memory, so the finder can walk
neighbours and look up definitions without a query.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import unicodedata
import uuid
import zlib
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from config import Settings
from gateway import Embedder

log = logging.getLogger("growi_search_store")

COLLECTION = "wiki"
DENSE = ("text", "gist", "bridge")
SPARSE = ("words", "terms")
EMBED_BATCH = 64
UPSERT_BATCH = 256
BM25_K1 = 1.2
STATE_VERSION = 1
_NAMESPACE = uuid.UUID("5f0c1d6e-4f1b-4f55-9d0e-6b1f3c2a9e11")
_WORD_RE = re.compile(r"[a-z0-9]{2,}")
_IDENT_RE = re.compile(r"[a-z_][a-z0-9_]*_[a-z0-9_]+|[a-z]+[0-9][a-z0-9]*")
_NON_CJK_RE = re.compile(r"[a-z0-9_\s\W]+")


def norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def tokens(text: str) -> list[str]:
    """Latin/digit words, whole identifiers (mpf_mfs_open), and CJK character bigrams.

    Japanese has no spaces, so bigrams stand in for a tokenizer (same idea as the
    linker's trigram FTS); identifiers are also kept whole so exact names match exactly.
    """
    text = unicodedata.normalize("NFKC", text or "").lower()
    out = _WORD_RE.findall(text) + _IDENT_RE.findall(text)
    for run in _NON_CJK_RE.sub(" ", text).split():
        out.extend([run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)])
    return out


def sparse_vector(text: str, *, query: bool = False) -> Any | None:
    from qdrant_client import models as qm

    counts = Counter(zlib.crc32(token.encode("utf-8")) & 0x7FFFFFFF for token in tokens(text))
    if not counts:
        return None
    indices = sorted(counts)
    if query:
        values = [1.0] * len(indices)
    else:  # BM25 term saturation; Qdrant's IDF modifier supplies the other half
        values = [counts[i] * (BM25_K1 + 1) / (counts[i] + BM25_K1) for i in indices]
    return qm.SparseVector(indices=indices, values=values)


def point_id(key: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, key))


def _source_hash(spec: dict[str, Any], identity: str) -> str:
    raw = json.dumps([identity, spec.get("dense", {}), spec.get("sparse", {})], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class Store:
    def __init__(self, settings: Settings, embedder: Embedder | None) -> None:
        from qdrant_client import QdrantClient

        if not settings.store_dir:
            raise ValueError("WIKI_SEARCH_STORE_DIR is required")
        self.settings = settings
        self.embedder = embedder
        self.dir = Path(settings.store_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.identity = embedder.identity if embedder is not None else "sparse-only"
        # A different GROWI endpoint (or root) means every page id, revision and section in
        # the store belongs to another wiki, so the store is dropped like a new embedder is.
        self.source = f"{settings.growi_url}|{settings.growi_root_path}"
        self.state_path = self.dir / "state.json"
        self.state = self._load_state()
        # ponytail: local file mode is brute-force search and fine to ~100k points; switch
        # this line to QdrantClient(url=...) if the corpus outgrows it.
        self.client = QdrantClient(path=str(self.dir / "qdrant"), force_disable_check_same_thread=True)
        self._ensure_collection()
        self.catalog: dict[str, dict[str, Any]] = {}      # key -> payload (all levels but fact)
        self.page_windows: dict[str, list[str]] = {}      # page id -> section window keys, in page order
        self.doc_pages: dict[str, list[str]] = {}         # document ref -> page ids, in document order
        self.defines: dict[str, list[str]] = {}           # normalized name -> first windows of defining sections
        self.uses: dict[str, list[str]] = {}
        self._names: list[str] = []
        self._load_catalog()

    # -- persistence ---------------------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if (state.get("version") != STATE_VERSION or state.get("identity") != self.identity
                or state.get("source") != self.source):
            state = self._fresh_state()
            state["reset"] = True
        return state

    def _fresh_state(self) -> dict[str, Any]:
        return {"version": STATE_VERSION, "identity": self.identity, "blocks": {}, "tree": {},
                "source": self.source, "docs": {}, "folders": {}, "pages": {}}

    def save_state(self) -> None:
        with self.lock:
            data = json.dumps({k: v for k, v in self.state.items() if k != "reset"}, ensure_ascii=False)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(self.state_path)

    def _ensure_collection(self) -> None:
        from qdrant_client import models as qm

        with self.lock:
            exists = self.client.collection_exists(COLLECTION)
            if exists and not self.state.get("reset"):
                return
            if exists:  # a different embedder: the old vectors are not comparable
                self.client.delete_collection(COLLECTION)
            # A new collection starts empty, so the sync must start from scratch too.
            self.state = self._fresh_state()
            dense = ({name: qm.VectorParams(size=self.embedder.dim, distance=qm.Distance.COSINE) for name in DENSE}
                     if self.embedder is not None else {})
            self.client.create_collection(
                COLLECTION, vectors_config=dense,
                sparse_vectors_config={name: qm.SparseVectorParams(modifier=qm.Modifier.IDF) for name in SPARSE},
            )
        self.save_state()

    def _load_catalog(self) -> None:
        from qdrant_client import models as qm

        flt = qm.Filter(must_not=[qm.FieldCondition(key="level", match=qm.MatchValue(value="fact"))])
        offset = None
        payloads: list[dict[str, Any]] = []
        with self.lock:
            while True:
                points, offset = self.client.scroll(COLLECTION, scroll_filter=flt, limit=1000, offset=offset,
                                                    with_payload=True, with_vectors=False)
                payloads.extend(point.payload or {} for point in points)
                if offset is None:
                    break
        self._catalog_add(payloads)

    # -- in-memory catalog ---------------------------------------------------

    def _catalog_add(self, payloads: Iterable[dict[str, Any]]) -> None:
        with self.lock:
            touched_pages: set[str] = set()
            for payload in payloads:
                key = payload.get("key")
                if not key or payload.get("level") == "fact":
                    continue
                self.catalog[key] = payload
                if payload.get("level") == "section":
                    touched_pages.add(payload["page_id"])
            self._reindex_pages(touched_pages)
            self._reindex_docs()

    def _catalog_remove(self, keys: Iterable[str]) -> None:
        with self.lock:
            touched: set[str] = set()
            for key in keys:
                payload = self.catalog.pop(key, None)
                if payload and payload.get("level") == "section":
                    touched.add(payload["page_id"])
            self._reindex_pages(touched)
            self._reindex_docs()

    def _reindex_pages(self, page_ids: set[str]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {page_id: [] for page_id in page_ids}
        for payload in self.catalog.values():
            if payload.get("level") == "section" and payload.get("page_id") in grouped:
                grouped[payload["page_id"]].append(payload)
        for page_id, windows in grouped.items():
            windows.sort(key=lambda p: (p.get("ordinal", 0), p.get("window", 0)))
            if windows:
                self.page_windows[page_id] = [p["key"] for p in windows]
            else:
                self.page_windows.pop(page_id, None)

    def _reindex_docs(self) -> None:
        """Rebuild the small derived maps; called after every catalog change (ms-scale)."""
        pages = sorted((p for p in self.catalog.values() if p.get("level") == "page"),
                       key=lambda p: (p.get("document", ""), p.get("order", 0)))
        doc_pages: dict[str, list[str]] = {}
        for page in pages:
            doc_pages.setdefault(page.get("document", ""), []).append(page["page_id"])
        defines: dict[str, list[str]] = {}
        uses: dict[str, list[str]] = {}
        for payload in self.catalog.values():
            if payload.get("level") != "section" or payload.get("window", 0) != 0:
                continue
            for name in payload.get("defines", []):
                defines.setdefault(name, []).append(payload["key"])
            for name in payload.get("uses", []):
                uses.setdefault(name, []).append(payload["key"])
        self.doc_pages, self.defines, self.uses = doc_pages, defines, uses
        self._names = sorted(set(defines) | set(uses), key=len, reverse=True)

    # -- reads ---------------------------------------------------------------

    def get(self, key: str) -> dict[str, Any] | None:
        return self.catalog.get(key)

    def page(self, page_id: str) -> dict[str, Any] | None:
        return self.catalog.get(f"p|{page_id}")

    def windows_of_section(self, section: str) -> list[str]:
        """Window keys of one section (``s|page|ordinal``)."""
        page_id = section.split("|")[1] if section.count("|") >= 2 else ""
        return [key for key in self.page_windows.get(page_id, []) if key.rsplit("|", 1)[0] == section]

    def names_in(self, text: str, limit: int = 20) -> list[str]:
        """Known entity names written in ``text`` (longest first): the exact-name channel."""
        query = norm(text)
        found: list[str] = []
        for name in self._names:
            if len(name) >= 3 and name in query and not any(name in longer for longer in found):
                found.append(name)
                if len(found) >= limit:
                    break
        return found

    def counts(self) -> dict[str, int]:
        with self.lock:
            levels = Counter(p.get("level") for p in self.catalog.values())
        return {"documents": levels.get("document", 0), "pages": levels.get("page", 0),
                "sections": levels.get("section", 0)}

    def search(self, query: str, query_vector: list[float] | None, *, limit: int,
               levels: tuple[str, ...] = ("section", "fact", "page"), projects: list[str] | None = None) -> list[dict[str, Any]]:
        """Hybrid search: every dense and sparse channel, fused by reciprocal rank."""
        from qdrant_client import models as qm

        must = [qm.FieldCondition(key="level", match=qm.MatchAny(any=list(levels)))]
        if projects:
            must.append(qm.FieldCondition(key="project", match=qm.MatchAny(any=projects)))
        flt = qm.Filter(must=must)
        prefetch = []
        if query_vector is not None and self.embedder is not None:
            prefetch += [qm.Prefetch(query=query_vector, using=name, limit=limit, filter=flt) for name in DENSE]
        words = sparse_vector(query, query=True)
        if words is not None:
            prefetch += [qm.Prefetch(query=words, using=name, limit=limit, filter=flt) for name in SPARSE]
        if not prefetch:
            return []
        with self.lock:
            result = self.client.query_points(COLLECTION, prefetch=prefetch, query=qm.FusionQuery(fusion=qm.Fusion.RRF),
                                              limit=limit, with_payload=True)
        return [{**(point.payload or {}), "score": float(point.score)} for point in result.points]

    # -- writes --------------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.embedder is None or not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH):
            vectors.extend(self.embedder.embed_documents(texts[start:start + EMBED_BATCH]))
        return vectors

    def upsert(self, specs: list[dict[str, Any]]) -> int:
        """Write points; a point whose sources did not change keeps its vectors (no embed call).

        A spec is {"key", "payload", "dense": {name: text}, "sparse": {name: text}}.
        Returns the number of texts embedded.
        """
        from qdrant_client import models as qm

        if not specs:
            return 0
        for spec in specs:
            spec["payload"]["key"] = spec["key"]
            spec["payload"]["src"] = _source_hash(spec, self.identity)
        ids = [point_id(spec["key"]) for spec in specs]
        old: dict[str, Any] = {}
        with self.lock:
            for start in range(0, len(ids), 1000):
                for point in self.client.retrieve(COLLECTION, ids=ids[start:start + 1000],
                                                  with_payload=["src"], with_vectors=True):
                    old[str(point.id)] = point
        vectors: list[dict[str, Any]] = []
        pending: list[tuple[int, str, str]] = []
        for index, (spec, pid) in enumerate(zip(specs, ids)):
            previous = old.get(pid)
            if previous is not None and (previous.payload or {}).get("src") == spec["payload"]["src"]:
                vectors.append(dict(previous.vector or {}))
                continue
            vector: dict[str, Any] = {}
            for name, text in spec.get("sparse", {}).items():
                value = sparse_vector(text)
                if value is not None:
                    vector[name] = value
            if self.embedder is not None:
                pending.extend((index, name, text) for name, text in spec.get("dense", {}).items() if text.strip())
            vectors.append(vector)
        embedded = self.embed([text for _i, _name, text in pending])  # outside the lock: slow
        for (index, name, _text), value in zip(pending, embedded):
            vectors[index][name] = value
        points = [qm.PointStruct(id=pid, vector=vector, payload=spec["payload"])
                  for pid, vector, spec in zip(ids, vectors, specs)]
        with self.lock:
            for start in range(0, len(points), UPSERT_BATCH):
                self.client.upsert(COLLECTION, points=points[start:start + UPSERT_BATCH], wait=True)
        self._catalog_add(spec["payload"] for spec in specs)
        return len(pending)

    def delete(self, keys: Iterable[str]) -> None:
        from qdrant_client import models as qm

        keys = list(dict.fromkeys(keys))
        if not keys:
            return
        with self.lock:
            for start in range(0, len(keys), 1000):
                self.client.delete(COLLECTION, points_selector=qm.PointIdsList(
                    points=[point_id(key) for key in keys[start:start + 1000]]), wait=True)
        self._catalog_remove(keys)

    def close(self) -> None:
        with self.lock:
            self.client.close()
