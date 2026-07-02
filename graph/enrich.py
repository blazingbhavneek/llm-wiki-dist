from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, ContextManager, Protocol, runtime_checkable
from .models import *

log = logging.getLogger("graph_enrich")

# DB meta key holding the count of endogenous nodes added since the last
# recluster. Owned by the worker; bumped on each "maybe_recluster" job.
RECLUSTER_COUNTER_KEY = "endo_since_recluster"


# `session_factory()` must return a context manager yielding an EnrichmentWorker.
SessionFactory = Callable[[], ContextManager[EnrichmentWorker]]


# --- queue ------------------------------------------------------------------
class EnrichmentQueue:
    """Slow-drip background worker. Thread-safe enqueue; one drain thread."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        drip_seconds: float = 3.0,
        recluster_every: int = 10,
    ) -> None:
        self._session_factory = session_factory
        self._drip_seconds = max(0.0, drip_seconds)
        self._recluster_every = max(1, recluster_every)
        self._queue: "queue.Queue[EnrichJob | None]" = queue.Queue()
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="enrichment-queue", daemon=True
        )
        self._thread.start()

    # -- public enqueue API ----------------------------------------------
    def enqueue_summary(self, node_id: str) -> None:
        self._put(EnrichJob("summary", node_id=node_id))

    def enqueue_entity_dedup(self, node_id: str) -> None:
        self._put(EnrichJob("entity_dedup", node_id=node_id))

    def enqueue_cascade(
        self, replacements: dict[str, str], stale_sources: list[str]
    ) -> None:
        if not replacements and not stale_sources:
            return
        self._put(
            EnrichJob(
                "cascade",
                replacements=dict(replacements),
                stale_sources=list(stale_sources),
            )
        )

    def note_endogenous_added(self) -> None:
        """Called once per endogenous node added on the fast path. Triggers a
        recluster only after `recluster_every` such calls."""
        self._put(EnrichJob("maybe_recluster"))

    def pending(self) -> int:
        return self._queue.qsize()

    def close(self, drain: bool = False) -> None:
        if drain:
            self._queue.join()
        self._stopping.set()
        self._queue.put(None)  # wake the drain loop

    # -- internals -------------------------------------------------------
    def _put(self, job: EnrichJob) -> None:
        if self._stopping.is_set():
            log.info("enrichment queue stopping; dropping %s", job.kind)
            return
        self._queue.put(job)

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:  # shutdown sentinel
                self._queue.task_done()
                break
            try:
                self._handle(job)
            except Exception as exc:  # noqa: BLE001 - background best-effort
                log.warning("enrichment job %s failed: %s", job.kind, exc)
            finally:
                self._queue.task_done()
            # Slow drip: keep background work strictly low priority.
            if self._drip_seconds:
                time.sleep(self._drip_seconds)

    def _handle(self, job: EnrichJob) -> None:
        with self._session_factory() as session:
            if job.kind == "summary" and job.node_id:
                session.enrich_summary(job.node_id)
            elif job.kind == "entity_dedup" and job.node_id:
                session.enrich_entity_dedup(job.node_id)
            elif job.kind == "cascade":
                session.enrich_cascade(job.replacements, job.stale_sources)
            elif job.kind == "maybe_recluster":
                self._maybe_recluster(session)
            else:
                log.info("enrichment: ignoring malformed job %r", job)

    def _maybe_recluster(self, session: EnrichmentWorker) -> None:
        raw = session.get_meta(RECLUSTER_COUNTER_KEY)
        count = int(raw) + 1 if (raw or "").isdigit() else 1
        if count >= self._recluster_every:
            log.info("enrichment: recluster threshold hit (%d) -> reclustering", count)
            session.refresh_clusters()
            count = 0
        session.set_meta(RECLUSTER_COUNTER_KEY, str(count))
