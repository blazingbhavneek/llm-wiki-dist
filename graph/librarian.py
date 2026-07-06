# region Imports

from __future__ import annotations

import asyncio
import json
import logging
import queue as queue_mod
import re
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from tqdm import tqdm

from .core import (
    CLAIM_PROMPT,
    CLUSTER_NAMER_SYSTEM,
    EDGE_PROMPT,
    ENTITY_DEDUP_PROMPT,
    KEYWORD_PROMPT,
    REGENERATE_EXOGENOUS_PROMPT,
    SUMMARY_PROMPT,
    ClaimExtraction,
    ClusterRenamePlan,
    Edge,
    EdgeSuggestions,
    EnrichJob,
    EntityMatch,
    Keywords,
    Node,
    NodeStatus,
    NodeType,
    chunk_text,
    claims_equivalent,
    dedupe,
    make_edge_id,
    make_exogenous_node_id,
    make_node_id,
    match_score,
    now_iso,
    short_hash,
    source_hash,
)

if TYPE_CHECKING:
    from .gateway import ModelGateway
    from .store import GraphStore

# endregion Imports

# region Global vars/helpers

log = logging.getLogger("graph_librarian")

# Markdown frontmatter content (content between two "---", after the second "---" starts the main body)
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

# detects Markdown filenames that start with a number followed by a dash, eg 001-introduction.md
_NUMBERED_DOC_RE = re.compile(r"^\d+-(.+\.md)$")
_CASCADE_MATCH_THRESHOLD = 0.45

# private key to change when to force a re-index
# Bump when the search_items chunking scheme changes; forces a bootstrap rebuild.
SEARCH_INDEX_VERSION = "chunk512-80-v1"

# DB meta key holding the count of endogenous nodes added since the last
# recluster. Owned by the enrichment thread.
RECLUSTER_COUNTER_KEY = "endo_since_recluster"

# possible status of a write job
WriteStatus = Literal["queued", "running", "done", "failed", "cancelled"]


@dataclass
class WriteJob:
    id: str
    type: str
    payload: dict[str, Any]
    user_id: str | None = None
    status: WriteStatus = "queued"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: Any = None
    error: str | None = None
    # Live progress for long-running jobs (chunk_and_ingest): updated in-place
    # by the worker thread, read by the /api/write-jobs pollers.
    progress: dict[str, Any] | None = None


# flatten a job object to a dict
def job_to_dict(job: WriteJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "type": job.type,
        "payload": job.payload,
        "user_id": job.user_id,
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "result": job.result,
        "error": job.error,
        "progress": job.progress,
    }


# message to show UI when assimilation is complete
ASSIMILATING_MESSAGE = "グラフへの追加が完了し、検索できるようになりました。ランキングは現在バックグラウンドで反映中です。しばらくすると検索結果の精度が向上します。"


# return object for when assimilating is complete
def _assimilating_result(node: Node) -> dict[str, Any]:
    """Wrap a fast-add node result with the UI 'still assimilating' notice."""
    return {"node": node, "assimilating": True, "message": ASSIMILATING_MESSAGE}


# endregion Global vars/helpers


# Just like a reseacher's job is to go through the relevant documents and report based on the query
# The librarian's job is to manage all the source docs, and organize them for researcher to work on them
# Since a librarian doesnt handle every book concurrently, as updating a book changes the state of the library, same way
# We have to make the changes sequential, so a state is first stabilized before adding other
class Librarian:

    # region Init, Queue and Job manager

    MAX_FINISHED_JOBS = (
        500  ## Store how many finished jobs in the queue, not just pending etc
    )

    # Sets up the Librarian's concurrency model.
    # In background/server mode, user-facing write jobs go through an asyncio queue.
    # Slow enrichment work runs separately on a background thread via a thread-safe queue.
    # A shared write lock ensures only one writer touches the graph/SQLite DB at a time.
    # In non-background/CLI mode, enrichment runs inline instead of being queued.
    # The enrichment thread can be stopped cleanly using a threading.Event signal.
    def __init__(
        self,
        gateway: ModelGateway,
        store: GraphStore,
        *,
        max_queue_size: int = 100,
        background: bool = True,
    ):
        self.gateway = gateway  # GPU Stuff, LLM/Embed/Reranker
        self.store = store  # DB Connection
        self._inline_enrichment = (
            not background
        )  # Do inline enrichment when running on background is disabled, and vice versa

        # Serializes the two writer threads (job worker + enrichment drip) so
        # each job runs as one SQLite transaction with no interleaved writes.
        # Any DB writer should lock this first, to avoid race conditions.
        # threading.Lock: a mutex; only one thread/task can hold it at a time,
        # so DB writes cannot overlap and corrupt/conflict with each other.
        self._write_lock = threading.Lock()

        # write-job queue (user-facing writes, strictly one at a time)
        # asyncio.Queue: async-friendly queue used by the event loop; write jobs wait here
        # until an async worker pulls them off with `await queue.get()`.
        self.queue: asyncio.Queue[WriteJob] = asyncio.Queue(maxsize=max_queue_size)

        # jobs dict: keeps job objects by ID so API/UI can check status/results later.
        self.jobs: dict[str, WriteJob] = {}

        # Current task
        # asyncio.Task: handle to a running async worker coroutine; not a thread,
        # just scheduled work inside the asyncio event loop.
        self.worker_task: asyncio.Task | None = None

        # slow-drip enrichment thread (background assimilation)
        # Background worker to enrich nodes with summaries etc etc, so initial addition is quick, but then those will be slowly enriched
        # with other stuff necessary for proper assimilation
        # queue.Queue: normal thread-safe queue for real Python threads; enrichment
        # jobs wait here until the enrichment thread consumes them.
        self._enrich_queue: "queue_mod.Queue[EnrichJob | None]" = queue_mod.Queue()

        # threading.Event: thread-safe stop signal; one thread calls `.set()`,
        # another checks `.is_set()` to know when to exit.
        self._enrich_stopping = threading.Event()

        # threading.Thread: real background thread that runs `_enrich_run` separately
        # from the main async server/event loop.
        self._enrich_thread: threading.Thread | None = None

        if background:
            s = self.settings

            # How many seconds to wait between enrichment jobs.
            # getattr(..., 3.0) means default to 3.0 if setting is missing.
            # max(0.0, ...) prevents negative sleep times.
            self._drip_seconds = max(0.0, float(getattr(s, "enrich_drip_seconds", 3.0)))

            # How often to refresh/recompute clusters during enrichment.
            # getattr(..., 10) means default to every 10 enrichment cycles.
            # max(1, ...) prevents invalid values like 0 or negative numbers.
            self._recluster_every = max(1, int(getattr(s, "recluster_every", 10)))

            # Create the enrichment background thread.
            # target=self._enrich_run means this thread will execute self._enrich_run().
            # daemon=True means it will not block the whole Python process from exiting.
            self._enrich_thread = threading.Thread(
                target=self._enrich_run, name="librarian-enrichment", daemon=True
            )

            # Actually start the enrichment thread.
            # After this line, _enrich_run begins running in the background.
            self._enrich_thread.start()

    # property decorator, this can be used an attribute, not a method, no need to call it
    @property
    def settings(self):
        # Live view: PATCH /api/settings replaces gateway.settings and every
        # subsequent write op sees the new values.
        return self.gateway.settings

    # This starts the queue processor, start a while loop over the queue, and if there is any job there
    # Runs it, waits for it, then marks it done
    async def start(self) -> None:
        # Starts the async job worker in the current asyncio event loop.
        # create_task schedules _worker_loop() to run in the background.
        self.worker_task = asyncio.create_task(self._worker_loop())

    # Cancels the worker that was started above
    async def stop(self) -> None:
        # Stop the async worker task if it is running.
        # cancel() requests cancellation; awaiting it lets Python clean it up properly.
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                # Expected when a task is cancelled, so we safely ignore it.
                pass

        # Signal the enrichment thread to stop.
        self._enrich_stopping.set()

        # Put None into the enrichment queue to wake the thread if it is blocked
        # waiting for work. None acts like a "shutdown sentinel".
        self._enrich_queue.put(None)

    async def enqueue(
        self, type_: str, payload: dict[str, Any], user_id: str | None = None
    ) -> WriteJob:
        # Create a new job object with a unique ID and store its request data.
        job = WriteJob(
            id=str(uuid.uuid4()), type=type_, payload=payload, user_id=user_id
        )

        # Save it in memory so callers/API can later check status/result by ID.
        self.jobs[job.id] = job

        try:
            # Add the job to the async queue without waiting.
            # If the queue is full, QueueFull is raised immediately.
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            # Mark the job as failed because it could not even enter the queue.
            job.status = "failed"
            job.error = "write queue is full"
            job.finished_at = datetime.now(timezone.utc)
            raise RuntimeError("write queue is full")

        return job

    async def _worker_loop(self) -> None:
        # Infinite background loop: waits for jobs and processes them one by one.
        # Because there is one worker loop, write jobs are serialized in queue order.
        while True:
            job = await self.queue.get()
            try:
                await self._run_job(
                    job
                )  # Get the job from the queue and put it another thread
            finally:
                # Tells asyncio.Queue that this queued item has finished processing.
                self.queue.task_done()

    # Given a job object put that in another thread
    async def _run_job(self, job: WriteJob) -> None:
        # If the job was cancelled before the worker reached it, skip it.
        if job.status == "cancelled":
            return

        # Update bookkeeping before actual execution.
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)

        try:
            # Run blocking/synchronous DB work in a separate thread so the asyncio
            # event loop does not freeze while the job is executing.
            result = await asyncio.to_thread(self._apply_job, job)

            # Store successful result and mark complete.
            job.result = result
            job.status = "done"
        except Exception as exc:
            # Convert exceptions into job failure state instead of crashing worker loop.
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            # Always stamp finish time and prune old finished jobs.
            job.finished_at = datetime.now(timezone.utc)
            self._prune_jobs()

    # Delete older job ids/status from history, we do keep about 500 of them for now for tracking etc
    def _prune_jobs(self) -> None:

        # Only finished jobs are eligible for removal.
        finished = [
            j for j in self.jobs.values() if j.status in ("done", "failed", "cancelled")
        ]

        # If we have more finished jobs than allowed, remove the oldest extras.
        overflow = len(finished) - self.MAX_FINISHED_JOBS
        if overflow <= 0:
            return

        # Oldest finished jobs come first.
        finished.sort(key=lambda j: j.finished_at or j.created_at)

        for job in finished[:overflow]:
            self.jobs.pop(job.id, None)

    def _apply_job(self, job: WriteJob) -> Any:
        # One job = one transaction: a failure rolls back every statement the
        # job made, so a crashed job can never leave half-written graph state.
        #
        # _write_lock prevents other writer threads from writing at the same time.
        # store.transaction() ensures DB changes commit/rollback as one unit. see store.py
        with self._write_lock, self.store.transaction():
            return self._dispatch_job(job)

    # Router for type of jobs, call different helper function based on what "type" of job was queued
    def _dispatch_job(self, job: WriteJob) -> Any:
        # Route the generic WriteJob to the actual graph operation based on job.type.
        # Each branch extracts payload values and calls the real method.

        if job.type == "update_node":
            return self.update_node(job.payload["node_id"], job.payload["body"])

        if job.type == "delete_node":
            self.delete_node(job.payload["node_id"])
            return {"deleted": job.payload["node_id"]}

        if job.type == "create_exogenous":
            node = self.create_exogenous_node(
                body=job.payload["body"],
                source_node_ids=job.payload.get("source_node_ids", []),
                origin=job.payload.get("origin"),
                question=job.payload.get("question"),
            )
            return _assimilating_result(node)

        if job.type == "create_document":
            node = self.create_document_node(
                body=job.payload["body"],
                title=job.payload.get("title"),
                document_name=job.payload.get("document_name"),
                source_path=job.payload.get("source_path"),
                source_ranges=job.payload.get("source_ranges"),
            )
            return _assimilating_result(node)

        if job.type == "recluster":
            mapping = self.recluster(resolution=job.payload.get("resolution", 1.0))
            return {"clusters": mapping}

        if job.type == "cascading_update":
            actions = self.cascading_update(job.payload["source_file"])
            return {"actions": actions}

        if job.type == "ingest_md_output":
            nodes = self.ingest_md_output(job.payload["path"])
            return {"ingested": len(nodes)}

        if job.type == "chunk_and_ingest":
            return self.chunk_and_ingest(job)

        if job.type == "ensure_japanese_clusters":
            mapping = self.ensure_japanese_clusters()
            return {"renamed": mapping}

        # Fail loudly if caller sends a job type this worker does not understand.
        raise ValueError(f"unknown write job type: {job.type}")

    def list_jobs(self, status: str | None = None, limit: int = 100) -> list[WriteJob]:
        # Return recent jobs, optionally filtered by status.
        # Useful for API/UI job history views.
        jobs = list(self.jobs.values())

        if status is not None:
            jobs = [j for j in jobs if j.status == status]

        # Newest jobs first.
        jobs.sort(key=lambda j: j.created_at, reverse=True)

        return jobs[:limit]

    def get_job(self, job_id: str) -> WriteJob | None:
        # Return one job by ID, or None if it does not exist.
        return self.jobs.get(job_id)

    def queue_size(self) -> int:
        # Number of jobs currently waiting in the asyncio queue.
        return self.queue.qsize()

    def queue_position(self, job_id: str) -> int | None:
        # Estimate a queued job's position among other queued jobs.
        # Position starts at 1. Returns None if the job is not currently queued.
        queued = sorted(
            [j for j in self.jobs.values() if j.status == "queued"],
            key=lambda j: j.created_at,
        )

        for index, job in enumerate(queued, start=1):
            if job.id == job_id:
                return index

        return None

    def cancel_job(self, job_id: str) -> bool:
        # Only queued jobs can be cancelled safely.
        # Running jobs are already executing, so this method does not stop them.
        job = self.jobs.get(job_id)

        if job is None or job.status != "queued":
            return False

        job.status = "cancelled"
        job.finished_at = datetime.now(timezone.utc)

        return True

    # endregion Init, Queue and Job manager

    # region Background Enrichment Queue

    def enqueue_summary(self, node_id: str) -> None:
        # Queue a background job to generate/fill the summary for this node.
        self._enrich_put(EnrichJob("summary", node_id=node_id))

    def enqueue_entity_dedup(self, node_id: str) -> None:
        # Queue a background job to check whether this node represents
        # the same real-world entity as another existing node.
        self._enrich_put(EnrichJob("entity_dedup", node_id=node_id))

    def enqueue_cascade(
        self, replacements: dict[str, str], stale_sources: list[str]
    ) -> None:
        # If nothing changed, there is no cascade work to do.
        if not replacements and not stale_sources:
            return

        # Queue a cascade job so dependent/generated nodes can be refreshed
        # after source nodes were replaced or marked stale.
        self._enrich_put(
            EnrichJob(
                "cascade",
                replacements=dict(replacements),
                stale_sources=list(stale_sources),
            )
        )

    def note_endogenous_added(self) -> None:

        # Queue a lightweight "maybe recluster" job. The actual recluster only
        # happens when enough new endogenous/source nodes have been added.
        self._enrich_put(EnrichJob("maybe_recluster"))

    def enrich_pending(self) -> int:
        # Return how many enrichment jobs are currently waiting in the thread queue.
        return self._enrich_queue.qsize()

    def _enrich_put(self, job: EnrichJob) -> None:
        # If shutdown has started, do not accept more enrichment work.
        if self._enrich_stopping.is_set():
            log.info("enrichment queue stopping; dropping %s", job.kind)
            return

        # Add the job to the thread-safe enrichment queue.
        # The enrichment thread will pick it up later.
        self._enrich_queue.put(job)

    def _enrich_run(self) -> None:
        # Main loop for the background enrichment thread.
        # It keeps pulling jobs from the enrichment queue until it receives None.
        while True:
            job = self._enrich_queue.get()

            # None is used as a shutdown sentinel to wake and stop the thread.
            if job is None:
                self._enrich_queue.task_done()
                break

            try:
                # Run the actual enrichment handler for this job.
                self._enrich_handle(job)
            except Exception as exc:  # noqa: BLE001 - background best-effort
                # Enrichment is best-effort: log failures but do not crash the thread.
                log.warning("enrichment job %s failed: %s", job.kind, exc)
            finally:
                # Tell the queue this job has finished processing.
                self._enrich_queue.task_done()

            # Slow drip: keep background work strictly low priority.
            # This prevents enrichment from aggressively using resources.
            if self._drip_seconds:
                time.sleep(self._drip_seconds)

    def _enrich_handle(self, job: EnrichJob) -> None:
        # Same discipline as write jobs: one enrichment job = one transaction,
        # rolled back wholesale if it fails partway.
        #
        # _write_lock prevents enrichment writes from overlapping with normal writes.
        # store.transaction() ensures partial enrichment changes are rolled back on error.
        with self._write_lock, self.store.transaction():
            if job.kind == "summary" and job.node_id:
                # Generate/update summary fields for this node.
                self.enrich_summary(job.node_id)

            elif job.kind == "entity_dedup" and job.node_id:
                # Check if this node duplicates an existing entity and link if needed.
                self.enrich_entity_dedup(job.node_id)

            elif job.kind == "cascade":
                # Update/regenerate dependent nodes affected by source replacements/staleness.
                self.enrich_cascade(job.replacements, job.stale_sources)

            elif job.kind == "maybe_recluster":
                # Increment recluster counter and refresh clusters only if threshold is hit.
                self._maybe_recluster()

            else:
                # Ignore jobs with missing/invalid fields instead of crashing the worker.
                log.info("enrichment: ignoring malformed job %r", job)

    def _maybe_recluster(self) -> None:
        # Read the persisted recluster counter from metadata.
        raw = self.get_meta(RECLUSTER_COUNTER_KEY)

        # Increment the counter, or start at 1 if missing/invalid.
        count = int(raw) + 1 if (raw or "").isdigit() else 1

        # Once enough source nodes have been added, recompute clusters.
        if count >= self._recluster_every:
            log.info("enrichment: recluster threshold hit (%d) -> reclustering", count)
            self.refresh_clusters()
            count = 0

        # Persist the updated counter so it survives across later calls.
        self.set_meta(RECLUSTER_COUNTER_KEY, str(count))

    # endregion Background Enrichment Queue

    # region Bootstrap / Startup Maintenance

    def bootstrap(self) -> None:
        """One-time startup: create vector tables, embed existing active nodes,
        set embed model metadata, rebuild search items, and name clusters.
        Run in the lifespan before accepting requests."""

        # Startup maintenance entry point. This prepares vectors, search index,
        # and clusters before the app starts serving normal requests.
        log.info("bootstrap.start db=%s", self.settings.database_path)

        try:
            # Ensure node/body vectors are valid for the current embedding model.
            active_nodes, reembedded = self._bootstrap_vectors()

            # Rebuild searchable chunks if vectors/index version are stale.
            self._bootstrap_search_items(active_nodes, reembedded)

            # Recompute/nickname clusters if graph changed or nodes are unclustered.
            self._bootstrap_clusters(active_nodes)
        finally:
            # Always log completion, even if one bootstrap step raises.
            log.info("bootstrap.done")

    def _bootstrap_vectors(self) -> tuple[list[Node], bool]:
        """Create vector tables if missing, re-embed active nodes if the embed
        model/dim changed or coverage is incomplete. Returns (active_nodes, reembedded).
        """

        # Ensure vector tables exist with the current embedding dimension.
        self.store.ensure_vec_tables(self.gateway.embedder.dim)

        # Current embedder info from the running model.
        current_model = self.gateway.embedder.model_name
        current_dim = self.gateway.embedder.dim

        # Previously stored embedder info from DB metadata.
        stored_model = self.store.get_meta("embed_model")
        stored_dim_raw = self.store.get_meta("embed_dim")
        stored_dim = int(stored_dim_raw) if stored_dim_raw else None

        # Only active nodes need vectors for current search/retrieval.
        active_nodes = [
            n for n in self.store.get_all_nodes() if n.status == NodeStatus.active
        ]

        # Check whether existing vectors are incompatible or incomplete.
        dim_changed = stored_dim is not None and stored_dim != current_dim
        model_changed = stored_model is not None and stored_model != current_model

        # If dimension changed, counting old vectors may be meaningless because
        # the table will be reset anyway. Otherwise, check vector coverage.
        coverage_incomplete = (
            self.store.count_vectors("vec_body") < len(active_nodes)
            if not dim_changed
            else False
        )

        log.info(
            "bootstrap.vector_status active_nodes=%d stored_model=%s current_model=%s dim_changed=%s model_changed=%s coverage_incomplete=%s",
            len(active_nodes),
            stored_model,
            current_model,
            dim_changed,
            model_changed,
            coverage_incomplete,
        )

        # Re-embed if the model changed, dimension changed, or some vectors are missing.
        reembedded = dim_changed or model_changed or coverage_incomplete
        if not reembedded:
            log.info("bootstrap.vectors_up_to_date")
            return active_nodes, reembedded

        log.info(
            "bootstrap.reembedding active_nodes=%d model=%s->%s dim=%s->%s",
            len(active_nodes),
            stored_model,
            current_model,
            stored_dim,
            current_dim,
        )

        # Clear old vector tables because they may be stale or wrong-dimensioned.
        self.store.reset_vec_tables()
        self.store.ensure_vec_tables(current_dim)

        # Recreate body and summary vectors for all active nodes.
        for node in tqdm(active_nodes, desc="bootstrap: re-embedding", unit="node"):
            try:
                self.store.set_vector(
                    node.id, "vec_body", self.gateway.embedder.embed_document(node.body)
                )

                # Only store summary vector if the node actually has summary text.
                if node.summary.strip():
                    self.store.set_vector(
                        node.id,
                        "vec_summary",
                        self.gateway.embedder.embed_document(node.summary),
                    )
            except Exception as exc:
                # Best-effort per node: one failed node should not stop all bootstrap.
                log.warning("reembed node %s failed: %s", node.id, exc)

        # Save current embedding model metadata so future bootstraps can compare.
        self.store.set_meta("embed_model", current_model)
        self.store.set_meta("embed_dim", str(current_dim))

        log.info("bootstrap.reembedding_done")
        return active_nodes, reembedded

    def _bootstrap_search_items(
        self, active_nodes: list[Node], reembedded: bool
    ) -> None:
        """Rebuild search_items + vec_search_item when the chunking scheme changed,
        when vectors were just rebuilt, or when coverage is incomplete."""

        # Stored version tells us whether the chunk/search indexing logic changed.
        stored_search_version = self.store.get_meta("search_index_version")

        # Existing vector count for search-item chunks.
        item_vec_count = self.store.count_vectors("vec_search_item")

        # Decide whether the search index is outdated or incomplete.
        search_version_changed = stored_search_version != SEARCH_INDEX_VERSION
        search_coverage_incomplete = item_vec_count < len(active_nodes)

        # Rebuild when there are active nodes and either vectors/index are stale
        # or the search item coverage is incomplete.
        rebuild_search = bool(active_nodes) and (
            reembedded or search_version_changed or search_coverage_incomplete
        )

        log.info(
            "bootstrap.search_index_status stored=%s current=%s item_vecs=%d version_changed=%s coverage_incomplete=%s rebuild=%s",
            stored_search_version,
            SEARCH_INDEX_VERSION,
            item_vec_count,
            search_version_changed,
            search_coverage_incomplete,
            rebuild_search,
        )

        if not rebuild_search:
            log.info("bootstrap.search_items_up_to_date")
            return

        log.info("bootstrap.rebuilding_search_items active_nodes=%d", len(active_nodes))

        # Rebuild searchable chunks and their vectors for each active node.
        for node in tqdm(active_nodes, desc="bootstrap: search items", unit="node"):
            try:
                self._store_search_items(node)
            except Exception as exc:
                # Best-effort per node: log and continue with the rest.
                log.warning("rebuild search items node %s failed: %s", node.id, exc)

        # Save the current search index version after rebuild completes.
        self.store.set_meta("search_index_version", SEARCH_INDEX_VERSION)

        log.info("bootstrap.search_items_rebuild_done")

    def _bootstrap_clusters(self, active_nodes: list[Node]) -> None:
        """Best-effort cluster naming. Only recluster when the graph topology
        changed since last time, or some active node still lacks a cluster."""

        # Build a simple graph signature from active node IDs and edge IDs.
        # If this signature is unchanged, the graph topology likely did not change.
        edges = self.store.get_all_edges()
        signature = source_hash(
            "|".join(
                [
                    str(len(active_nodes)),
                    *sorted(n.id for n in active_nodes),
                    str(len(edges)),
                    *sorted(e.id for e in edges),
                ]
            )
        )

        # Previously saved graph signature from last successful clustering.
        stored_signature = self.store.get_meta("cluster_signature")

        # Even if graph did not change, recluster if any active node has no cluster.
        unclustered = any(not (n.cluster or "").strip() for n in active_nodes)

        # Skip expensive reclustering when graph is unchanged and all nodes are clustered.
        if signature == stored_signature and not unclustered:
            log.info("bootstrap.graph_unchanged_skip_recluster")
            return

        try:
            log.info(
                "bootstrap.recluster_start graph_changed=%s unclustered=%s",
                signature != stored_signature,
                unclustered,
            )

            # Recompute graph communities/clusters and persist results.
            self._recluster(persist=True)

            # Make sure cluster names are Japanese/UI-friendly.
            log.info("bootstrap.ensure_japanese_clusters_start")
            self._ensure_japanese_clusters()

            # Save new graph signature only after successful recluster/naming.
            self.store.set_meta("cluster_signature", signature)

            log.info("bootstrap.japanese_cluster_naming_done")
        except Exception as exc:
            # Cluster naming is best-effort during startup; do not crash bootstrap.
            log.info("bootstrap.cluster_naming_skipped error=%s", exc, exc_info=True)

    # endregion Bootstrap / Startup Maintenance

    # region Node Mutation / Document Ingest

    def update_node(self, node_id: str, body: str) -> Node:
        # Load the current node; updating a missing node is an error.
        old = self.store.get_node(node_id)
        if not old:
            raise KeyError(f"node not found: {node_id}")

        # Recompute ID from new content. If the body changes enough, this may
        # create a new node ID instead of modifying the existing one in-place.
        new_id = (
            make_exogenous_node_id(body)
            if old.type == NodeType.exogenous
            else make_node_id(body, old.original_document_name)
        )

        # Build the replacement node while preserving original metadata where possible.
        replacement = Node(
            id=new_id,
            body=body,
            type=old.type,
            title=self._title_from_markdown(body) or old.title,
            original_document_name=old.original_document_name,
            source_path=old.source_path,
            source_ranges=old.source_ranges,
            source_version=source_hash(body),
            cluster=old.cluster,
        )

        # If ID did not change, update the same node directly.
        if replacement.id == old.id:
            old.source_version = replacement.source_version
            self._fill_derived_fields(old)
            self.store.upsert_node(old)
            return old

        # If ID changed, persist the replacement and mark the old node superseded.
        self._persist_node(replacement)
        self._supersede(old, replacement)

        # Keep the document chain (`follows`) and note provenance
        # (`reference`/`supports`) attached to the live node, not the
        # superseded one.
        self._remap_edges(old.id, replacement.id)

        # A hand edit is new information: agent notes supported by this node
        # must regenerate against the replacement or go stale.
        if not self._inline_enrichment:
            self.enqueue_cascade({old.id: replacement.id}, [])
        else:
            actions: list[str] = []
            self._cascade_dependents({old.id: replacement.id}, set(), actions)

        return replacement

    def delete_node(self, node_id: str) -> None:
        # Load node first so dependents can react before final deletion.
        node = self.store.get_node(node_id)

        if node is not None:
            # Take the node out of retrieval first, then let dependents react
            # while its `supports` edges still exist: notes regenerate from
            # their remaining sources or go stale.
            self.store.set_node_status(node_id, NodeStatus.deleted)

            actions: list[str] = []
            try:
                self._cascade_dependents({}, {node_id}, actions)
            except Exception as exc:
                # Deletion should still continue even if cascade cleanup fails.
                log.info("delete cascade failed for %s: %s", node_id, exc)

        # Remove the node from storage.
        self.store.delete_node(node_id)

    def _clean_optional_text(
        self, value: str | None, max_len: int | None = None
    ) -> str | None:
        # Normalize optional text fields: empty/whitespace-only values become None.
        if not value:
            return None

        # Collapse repeated whitespace into single spaces.
        text = " ".join(value.strip().split())

        if not text:
            return None

        # Optionally truncate long text and add an ellipsis.
        if max_len is not None and len(text) > max_len:
            return text[: max_len - 1].rstrip() + "…"

        return text

    def _exo_title_for_note(
        self,
        body: str,
        origin: str | None,
        question: str | None,
    ) -> str:
        # Clean question text before using it as a title.
        clean_question = self._clean_optional_text(question)

        # For saved agent answers, prefer the original question as title.
        # This makes future identical/similar queries much easier to retrieve.
        if clean_question:
            return clean_question

        # Otherwise infer from markdown, or fall back to origin/body-based title.
        return self._title_from_markdown(body) or self._exo_fallback_title(origin, body)

    def create_exogenous_node(
        self,
        body: str,
        source_node_ids: list[str],
        origin: str | None = None,
        question: str | None = None,
    ) -> Node:
        # Clean the question so title/identity generation is stable.
        clean_question = self._clean_optional_text(question)

        # Important:
        # Do not hash only origin/question, otherwise different answers to the same
        # question can collide. Include body too.
        identity_material = "\n\n".join(
            part
            for part in [
                origin or "",
                clean_question or "",
                body,
            ]
            if part
        )

        # Create an agent/exogenous note node.
        node = Node(
            id=make_exogenous_node_id(identity_material),
            body=body,
            type=NodeType.exogenous,
            title=self._exo_title_for_note(body, origin, clean_question),
            original_document_name=None,
            cluster="Agent Notes",
        )

        # Fill cheap metadata first, then persist so vectors/links can reference it.
        self._fill_cheap_fields(node)
        self.store.upsert_node(node)

        # Store vectors and use body vector to choose a cluster based on references.
        body_vec, _ = self._store_vectors(node)

        node.cluster = self._cluster_for_references(node.id, source_node_ids, body_vec)
        self.store.upsert_node(node)

        # Link the note to its referenced/supporting source nodes.
        self._link_references(node, source_node_ids)
        self._link_supports(node, dedupe([sid for sid in source_node_ids if sid]))

        # Queue summary generation unless running in inline/CLI mode.
        if not self._inline_enrichment:
            self.enqueue_summary(node.id)

        return node

    def create_document_node(
        self,
        body: str,
        title: str | None = None,
        document_name: str | None = None,
        source_path: str | None = None,
        source_ranges: list[tuple[int, int]] | None = None,
    ) -> Node:
        # Empty documents are invalid.
        body = body.strip()
        if not body:
            raise ValueError("document body is empty")

        # Infer document metadata when caller does not provide it.
        inferred_title = title or self._title_from_markdown(body)
        doc_name = self._document_name(
            document_name or inferred_title or f"uploaded-{short_hash(body)}.md"
        )

        line_count = max(1, len(body.splitlines()))
        ranges = source_ranges if source_ranges is not None else [(1, line_count)]
        version = source_hash(body)

        # Build the new endogenous/document node.
        node = Node(
            id=make_node_id(body, doc_name),
            body=body,
            type=NodeType.endogenous,
            title=inferred_title or doc_name,
            original_document_name=doc_name,
            source_path=source_path,
            source_ranges=ranges,
            source_version=version,
            source_material_hash=source_hash(body),
            cluster="Uploaded Documents",
        )

        # Find active existing nodes from the same document.
        active_old = [
            n
            for n in self.store.get_nodes_by_document(doc_name, active_only=True)
            if n.type == NodeType.endogenous
        ]

        # If exact same source content already exists, reuse/update it.
        for old in active_old:
            old_hash = old.source_material_hash or source_hash(old.body)
            if old_hash == node.source_material_hash:
                old.source_version = version
                old.source_material_hash = old_hash
                self.store.upsert_node(old)
                self._ingest_one(old)
                self.store.record_source(doc_name, version)
                return old

        # Prepare derived fields and revision/cascade tracking.
        self._fill_cheap_fields(node)
        replacements: dict[str, str] = {}
        stale_sources: set[str] = set()
        actions: list[str] = []

        # Backfill older metadata before trying to match old node to new node.
        backfilled_old = [self._backfill_revision_metadata(n) for n in active_old]
        best = max(
            ((old, match_score(old, node)) for old in backfilled_old),
            key=lambda item: item[1],
            default=None,
        )

        # Persist new node cheaply first; expensive enrichment can happen later.
        self._persist_node(node, cheap=True)

        matched_old_id: str | None = None
        if best is not None and best[1] >= _CASCADE_MATCH_THRESHOLD:
            # If a strong old match exists, supersede it with this new node.
            matched_old_id = best[0].id
            self._supersede(best[0], node)
            replacements[best[0].id] = node.id
            actions.append(f"superseded:{best[0].id}->{node.id}")

        # Any old active nodes not matched are now stale.
        for old in active_old:
            if old.id == matched_old_id:
                continue
            self.store.set_node_status(old.id, NodeStatus.stale)
            stale_sources.add(old.id)
            actions.append(f"stale:{old.id}")

        # Update document-level structural/source metadata.
        self._replace_structural_edges(doc_name, [])
        self.store.record_source(doc_name, version)

        # Defer the expensive graph-wide bookkeeping so the UI add returns fast.
        # The node + its semantic edges are already committed and searchable;
        # duplicate-merge, cascade regen, and reclustering catch up in the
        # background (recluster only fires every N endogenous adds).
        if not self._inline_enrichment:
            self.enqueue_cascade(replacements, list(stale_sources))
            self.enqueue_entity_dedup(node.id)
            self.note_endogenous_added()
        else:
            self._cascade_dependents(replacements, stale_sources, actions)
            self._refresh_clusters()

        return node

    def recluster(self, resolution: float = 1.0) -> dict[str, str]:
        # Public wrapper for reclustering and persisting cluster assignments.
        return self._recluster(resolution=resolution, persist=True)

    def ensure_japanese_clusters(self) -> dict[str, str]:
        # Public wrapper to ensure cluster names are Japanese/UI-friendly.
        return self._ensure_japanese_clusters()

    def ingest_md_output(self, md_output_dir: str | Path) -> list[Node]:
        # Load a markdown-output directory produced by the parser/chunker.
        out_path = Path(md_output_dir)
        if not out_path.exists():
            raise FileNotFoundError(f"input directory does not exist: {out_path}")

        nodes, structural_edges = self._load_md_output(out_path)
        if not nodes:
            return []

        # Use the first node to infer document identity/version.
        document_name = nodes[0].original_document_name or out_path.name
        version = self._source_version_for_nodes(nodes)

        if document_name and self.store.get_source(document_name):
            # Re-ingest of a known document: run the revision flow so changed
            # pages supersede their old versions (and dependents cascade)
            # instead of piling up duplicates next to stale active nodes.
            actions = self._revise_document(
                nodes, structural_edges, document_name, version
            )
            log.info("re-ingest via revision flow: %s", "; ".join(actions) or "no-op")
        else:
            # First ingest: persist each node and build semantic/dedup edges.
            edge_count = 0
            for index, node in enumerate(nodes, start=1):
                node.source_version = version
                edges = self._ingest_one(node)
                edge_count += len(edges)
                log.info(
                    "ingest %d/%d | edges so far %d | %s",
                    index,
                    len(nodes),
                    edge_count,
                    node.id,
                )

            # Replace structural document edges and remember source version.
            self._replace_structural_edges(document_name, structural_edges)
            if document_name:
                self.store.record_source(document_name, version)

            log.info(
                "ingest done: %d nodes, %d semantic/dedup edges, %d structural",
                len(nodes),
                edge_count,
                len(structural_edges),
            )

        # Best-effort clustering after ingest.
        try:
            mapping = self.recluster()
            self.ensure_japanese_clusters()
            log.info("reclustered into %d topics", len(set(mapping.values())))
        except Exception as exc:
            log.info("recluster skipped: %s", exc)

        return nodes

    def chunk_and_ingest(self, job: WriteJob) -> dict[str, Any]:
        # Big-document path: chunk an uploaded markdown body with the external
        # chunker package, then ingest the rendered pages through the normal
        # ingest_md_output path. This whole job intentionally owns the write
        # queue end-to-end (one big job); reads stay on their own connection.
        from chunker import ChunkConfig, run_chunk_pipeline

        body = job.payload["body"]
        document_name = self._document_name(
            job.payload.get("document_name")
            or job.payload.get("title")
            or f"uploaded-{short_hash(body)}.md"
        )
        config = ChunkConfig.from_env().apply_overrides(job.payload.get("options"))

        out_dir = (
            Path(self.settings.database_path).parent
            / "chunked"
            / f"{Path(document_name).stem}-{short_hash(body)}"
        )

        def on_progress(update: dict[str, Any]) -> None:
            job.progress = update

        result = run_chunk_pipeline(
            source_text=body,
            document_name=document_name,
            out_dir=out_dir,
            config=config,
            llm=self.gateway.llm,
            on_progress=on_progress,
        )

        job.progress = {"stage": "ingesting", "total": result.file_count}
        nodes = self.ingest_md_output(out_dir)

        # Inline enrichment: the job already holds the write queue, so new
        # nodes leave here fully enriched (summary + entity dedup) instead of
        # dripping through the background enrichment thread.
        for index, node in enumerate(nodes, start=1):
            job.progress = {
                "stage": "enriching",
                "current": index,
                "total": len(nodes),
            }
            self.enrich_summary(node.id)
            self.enrich_entity_dedup(node.id)

        job.progress = {"stage": "done", "total": len(nodes)}
        return {
            "chunked": True,
            "ingested": len(nodes),
            "files": result.file_count,
            "document_name": document_name,
            "out_dir": str(out_dir),
            "chunker_llm_calls": result.llm_calls,
        }

    def cascading_update(self, source_file: str | Path) -> list[str]:
        # Update an already-ingested source file and cascade changes to dependents.
        out_path = Path(source_file)
        if not out_path.exists():
            raise FileNotFoundError(f"source does not exist: {out_path}")

        nodes, structural_edges = self._load_md_output(out_path)
        if not nodes:
            return []

        # Compute document version and run shared revision logic.
        document_name = nodes[0].original_document_name or out_path.name
        version = self._source_version_for_nodes(nodes)
        actions = self._revise_document(nodes, structural_edges, document_name, version)

        # Best-effort recluster after update.
        try:
            self.recluster()
            self.ensure_japanese_clusters()
        except Exception as exc:
            log.info("recluster skipped: %s", exc)

        return actions

    def _revise_document(
        self,
        nodes: list[Node],
        structural_edges: list[Edge],
        document_name: str,
        version: str,
    ) -> list[str]:
        """Revision matching for one document: unchanged pages keep their node,
        changed pages supersede the old one, removed pages go stale, and
        dependents cascade. Shared by cascading_update and re-ingest."""

        # Cheap pass: stamp version + body hash.
        for node in nodes:
            node.source_version = version
            if not node.source_material_hash:
                node.source_material_hash = source_hash(node.body)

        # Existing active document nodes are candidates for unchanged/superseded/stale.
        active_old = [
            n
            for n in self.store.get_nodes_by_document(document_name, active_only=True)
            if n.type == NodeType.endogenous
        ]

        # If this document has no active old nodes, ingest everything as new.
        if not active_old:
            for node in nodes:
                self._persist_node(node)
            self._replace_structural_edges(document_name, structural_edges)
            self.store.record_source(document_name, version)
            return [f"ingested-new:{n.id}" for n in nodes]

        actions: list[str] = []
        replacements: dict[str, str] = {}
        stale_sources: set[str] = set()
        matched_old: set[str] = set()

        # Map old nodes by exact source hash for quick unchanged detection.
        exact_by_hash: dict[str, Node] = {}
        for old in active_old:
            exact_by_hash.setdefault(
                old.source_material_hash or source_hash(old.body), old
            )

        # First pass: exact hash matches are unchanged; others need fuzzy matching.
        pending: list[Node] = []
        for node in nodes:
            exact = exact_by_hash.get(node.source_material_hash)
            if exact and exact.id not in matched_old:
                matched_old.add(exact.id)
                actions.append(f"unchanged:{exact.id}")
            else:
                pending.append(node)

        # Fill expensive/derived fields only for nodes that may be new/changed.
        for node in pending:
            self._fill_derived_fields(node)

        # Prepare old unmatched nodes for fuzzy revision matching.
        unmatched_old = [
            self._backfill_revision_metadata(old)
            for old in active_old
            if old.id not in matched_old
        ]

        for node in pending:
            # Find best old candidate for this new node.
            candidates = [old for old in unmatched_old if old.id not in matched_old]
            best = max(
                ((c, match_score(c, node)) for c in candidates),
                key=lambda item: item[1],
                default=None,
            )

            # No good match means this is a new node.
            if best is None or best[1] < _CASCADE_MATCH_THRESHOLD:
                self._persist_node(node)
                actions.append(f"new:{node.id}")
                continue

            old = best[0]
            matched_old.add(old.id)

            # If claims are equivalent, keep old node and just treat it as remapped.
            if claims_equivalent(old, node):
                actions.append(f"remapped:{old.id}")
                continue

            # Otherwise persist new node and supersede the matched old node.
            self._persist_node(node)
            self._supersede(old, node)
            replacements[old.id] = node.id
            actions.append(f"superseded:{old.id}->{node.id}")

        # Any old node not matched by exact/fuzzy matching is stale.
        for old in active_old:
            if old.id not in matched_old:
                self.store.set_node_status(old.id, NodeStatus.stale)
                stale_sources.add(old.id)
                actions.append(f"stale:{old.id}")

        # Regenerate/stale dependent notes, update structure, and store new version.
        self._cascade_dependents(replacements, stale_sources, actions)
        self._replace_structural_edges(document_name, structural_edges)
        self.store.record_source(document_name, version)

        return actions

    def recon(self, source_file: str | Path) -> dict[str, Any]:
        """Read-only revision status for a source doc: is it new, changed, or
        unchanged relative to what was last ingested? Mutates nothing."""

        # Load source file/output and compare its computed version to stored version.
        out_path = Path(source_file)
        if not out_path.exists():
            raise FileNotFoundError(f"source does not exist: {out_path}")

        nodes, _edges = self._load_md_output(out_path)
        if not nodes:
            return {"status": "empty", "document": None, "nodes": 0}

        document_name = nodes[0].original_document_name or out_path.name
        version = self._source_version_for_nodes(nodes)
        recorded = self.store.get_source(document_name)

        # Determine whether this source is new, unchanged, or changed.
        if recorded is None:
            status = "new"
        elif recorded[0] == version:
            status = "unchanged"
        else:
            status = "changed"

        return {
            "status": status,
            "document": document_name,
            "version": version,
            "recorded_version": recorded[0] if recorded else None,
            "recorded_at": recorded[1] if recorded else None,
            "nodes": len(nodes),
        }

    def _ingest_one(self, node: Node) -> list[Edge]:
        # Skip work if node is already active and has vectors.
        existing = self.store.get_node(node.id)
        complete = (
            existing is not None
            and existing.status == NodeStatus.active
            and self.store.has_vector(node.id)
        )
        if complete:
            return []

        # Fill fields, persist node, store vectors, then build semantic links.
        self._fill_derived_fields(node)
        self.store.upsert_node(node)

        body_vec, summary_vec = self._store_vectors(node)
        edges = self._build_semantic_edges(node, body_vec, summary_vec)

        # Optionally find and link duplicate/same-entity nodes.
        if self.settings.entity_dedup:
            candidates = self._knn_candidates(node.id, body_vec, summary_vec)
            edges += self._link_entity_duplicates(node, candidates)

        return edges

    # endregion Node Mutation / Document Ingest

    # region Vector Storage / Search Items

    def _ensure_vec(self) -> None:
        # Make sure vector tables exist and match the current embedder dimension.
        self.store.ensure_vec_tables(self.gateway.embedder.dim)

    def _store_vectors(self, node: Node) -> tuple[list[float], list[float] | None]:
        # Store the main body vector, optional summary vector, and search-item vectors.
        self._ensure_vec()

        body_vec = self.gateway.embedder.embed_document(node.body)
        self.store.set_vector(node.id, "vec_body", body_vec)

        summary_vec = None
        if node.summary.strip():
            summary_vec = self.gateway.embedder.embed_document(node.summary)
            self.store.set_vector(node.id, "vec_summary", summary_vec)

        # Also rebuild chunk/title/claim search rows for this node.
        self._store_search_items(node)

        return body_vec, summary_vec

    def _build_search_items(self, node: Node) -> list[dict]:

        s = self.settings
        items: list[dict] = []

        def add(field: str, text: str, ordinal: int, start, end) -> None:
            # Normalize and skip empty evidence text.
            clean = (text or "").strip()
            if not clean:
                return

            # Each item is a searchable evidence unit tied back to the node.
            items.append(
                {
                    "id": short_hash(f"{node.id}|{field}|{ordinal}"),
                    "node_id": node.id,
                    "field": field,
                    "text": clean,
                    "ordinal": ordinal,
                    "start_char": start,
                    "end_char": end,
                    "source_path": node.source_path,
                    "source_hash": node.source_material_hash,
                }
            )

        # Add high-signal fields first.
        add("title", node.title, 0, None, None)
        add("summary", node.summary, 0, None, None)

        # Add extracted claims as separate searchable evidence rows.
        for i, claim in enumerate(node.claims):
            add("claim", claim, i, None, None)

        body = node.body or ""

        # Add larger body chunks for broad semantic recall.
        for i, (start, end, chunk) in enumerate(
            chunk_text(body, s.search_big_chunk_size, s.search_big_chunk_overlap)
        ):
            add("big_chunk", chunk, i, start, end)

        # Add smaller chunks for more precise matching.
        for i, (start, end, chunk) in enumerate(
            chunk_text(body, s.search_small_chunk_size, s.search_small_chunk_overlap)
        ):
            add("small_chunk", chunk, i, start, end)

        return items

    def _store_search_items(self, node: Node) -> None:

        try:
            self._ensure_vec()

            # Rebuild all evidence rows for this node.
            items = self._build_search_items(node)
            self.store.replace_search_items(node.id, items)

            if not items:
                return

            texts = [it["text"] for it in items]

            try:
                # Prefer batch embedding for speed.
                vectors = self.gateway.embedder.embed_documents(texts)
            except Exception as exc:
                # If batch embedding fails, fall back to one-by-one embedding.
                log.info("batch item embed failed; per-item fallback: %s", exc)
                vectors = []

                for text in texts:
                    try:
                        vectors.append(self.gateway.embedder.embed_document(text))
                    except Exception as exc2:
                        # Keep index rebuild best-effort; skip only failed item vectors.
                        log.info("item embed failed: %s", exc2)
                        vectors.append(None)

            # Store vector for each successfully embedded search item.
            for item, vector in zip(items, vectors):
                if vector is None:
                    continue

                try:
                    self.store.set_search_item_vector(item["id"], vector)
                except Exception as exc:
                    log.info("set item vector failed %s: %s", item["id"], exc)

        except Exception as exc:
            # Search indexing should not break node ingestion/update.
            log.info("store search items failed for %s: %s", node.id, exc)

    # endregion Vector Storage / Search Items

    # region Candidate Retrieval / Deduplication

    def _knn_candidates(
        self,
        node_id: str,
        body_vec: list[float],
        summary_vec: list[float] | None,
        k: int | None = None,
    ) -> list[Node]:
        # Find nearest active nodes using body vector and summary vector if available.
        if k is None:
            k = self.settings.edge_candidate_k

        ranked: list[str] = []

        probes = [("vec_body", body_vec)] + (
            [("vec_summary", summary_vec)] if summary_vec else []
        )

        # Collect unique nearest-neighbor IDs, excluding the node itself.
        for table, vector in probes:
            for candidate_id, _distance in self.store.vector_search(
                vector, table, k + 1
            ):
                if candidate_id != node_id and candidate_id not in ranked:
                    ranked.append(candidate_id)

        # Load only active candidate nodes.
        candidates = [
            node
            for node in (self.store.get_node(cid) for cid in ranked)
            if node and node.status == NodeStatus.active
        ]

        # Collapse same-as duplicates so the LLM sees fewer redundant candidates.
        return self._collapse_same_as(candidates)[:k]

    def _link_entity_duplicates(self, node: Node, candidates: list[Node]) -> list[Edge]:
        # Ask the LLM whether the new node is the same real-world entity as a candidate.
        if not candidates:
            return []

        payload = {
            "new_node": {
                "id": node.id,
                "title": node.title,
                "entity": node.entity,
                "summary": node.summary,
            },
            "candidates": [
                {"id": c.id, "title": c.title, "entity": c.entity, "summary": c.summary}
                for c in candidates
            ],
        }

        result = self.gateway.llm.complete_structured(
            ENTITY_DEDUP_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            EntityMatch,
        )

        match = (
            result
            if isinstance(result, EntityMatch)
            else EntityMatch.model_validate(result)
        )

        allowed = {c.id for c in candidates}

        # Ignore invalid, unsafe, or self matches.
        if (
            not match.is_same
            or match.target_node_id not in allowed
            or match.target_node_id == node.id
        ):
            return []

        stamp = now_iso()
        episodes = [node.id, match.target_node_id]
        edges: list[Edge] = []

        # Store same-as in both directions for easier graph traversal.
        for src, dst in (
            (node.id, match.target_node_id),
            (match.target_node_id, node.id),
        ):
            edge = Edge(
                id=make_edge_id(src, dst, "same-as"),
                source_node_id=src,
                target_node_id=dst,
                label="same-as",
                summary="Same real-world entity.",
                valid_at=stamp,
                source_episode_ids=episodes,
            )
            self.store.upsert_edge(edge)
            edges.append(edge)

        return edges

    def _collapse_same_as(self, nodes: list[Node]) -> list[Node]:
        # Keep one representative per same-as group to reduce duplicate candidates.
        kept: list[Node] = []
        seen: set[str] = set()

        for node in nodes:
            if node.id in seen:
                continue

            kept.append(node)
            group = {node.id}

            # Add all nodes directly connected by same-as edges to the seen group.
            for edge in self.store.get_edges_for_node(node.id):
                if edge.label == "same-as":
                    other = (
                        edge.target_node_id
                        if edge.source_node_id == node.id
                        else edge.source_node_id
                    )
                    group.add(other)

            seen |= group

        return kept

    # endregion Candidate Retrieval / Deduplication

    # region Derived Fields / LLM Extraction

    def _fill_derived_fields(self, node: Node) -> Node:
        # Fill all derived metadata, including summary, keywords, claims, and entity.
        if not node.source_material_hash:
            node.source_material_hash = source_hash(node.body)

        if not node.summary.strip() and node.body.strip():
            node.summary = self.gateway.llm.complete(SUMMARY_PROMPT, node.body).strip()

        if not node.keywords:
            node.keywords = self._extract_keywords(node.body)

        if not node.claims:
            extracted = self._extract_claims(node.body)
            node.entity = node.entity or extracted.entity
            node.claims = extracted.claims

        if not node.entity and node.keywords:
            node.entity = node.keywords[0]

        return node

    def _extract_keywords(self, text: str) -> list[str]:
        # Extract a small deduped keyword list from text using the LLM.
        if not text.strip():
            return []

        result = self.gateway.llm.complete_structured(
            KEYWORD_PROMPT, text[:8000], Keywords
        )

        parsed = (
            result if isinstance(result, Keywords) else Keywords.model_validate(result)
        )

        kept: list[str] = []
        seen: set[str] = set()

        # Normalize and dedupe case-insensitively.
        for kw in parsed.keywords:
            kw = kw.strip()
            if kw and kw.lower() not in seen:
                kept.append(kw)
                seen.add(kw.lower())

        return kept[:12]

    def _extract_claims(self, text: str) -> ClaimExtraction:
        # Extract entity + factual claims from text using the LLM.
        if not text.strip():
            return ClaimExtraction()

        result = self.gateway.llm.complete_structured(
            CLAIM_PROMPT, text[:12000], ClaimExtraction
        )

        parsed = (
            result
            if isinstance(result, ClaimExtraction)
            else ClaimExtraction.model_validate(result)
        )

        claims: list[str] = []
        seen: set[str] = set()

        # Normalize whitespace and dedupe claims.
        for claim in parsed.claims:
            claim = " ".join(claim.strip().split())
            if claim and claim.lower() not in seen:
                seen.add(claim.lower())
                claims.append(claim)

        return ClaimExtraction(
            entity=" ".join(parsed.entity.strip().split()), claims=claims[:20]
        )

    def _fill_cheap_fields(self, node: Node) -> Node:

        # Fill metadata needed for matching/search without doing summary generation.
        if not node.source_material_hash:
            node.source_material_hash = source_hash(node.body)

        if not node.keywords:
            node.keywords = self._extract_keywords(node.body)

        if not node.claims:
            extracted = self._extract_claims(node.body)
            node.entity = node.entity or extracted.entity
            node.claims = extracted.claims

        if not node.entity and node.keywords:
            node.entity = node.keywords[0]

        return node

    # endregion Derived Fields / LLM Extraction

    # region Semantic Edges / Node Persistence

    def _build_semantic_edges(
        self, node: Node, body_vec: list[float], summary_vec: list[float] | None
    ) -> list[Edge]:
        # Find nearby nodes, then ask the LLM what relationships should be created.
        candidates = self._knn_candidates(node.id, body_vec, summary_vec)
        if not candidates:
            return []

        payload = {
            "new_node": {
                "id": node.id,
                "title": node.title,
                "summary": node.summary,
                "keywords": node.keywords,
                "body": node.body[:4000],
            },
            "candidates": [
                {
                    "id": c.id,
                    "title": c.title,
                    "summary": c.summary,
                    "keywords": c.keywords,
                    "body": c.body[:1200],
                }
                for c in candidates
            ],
        }

        result = self.gateway.llm.complete_structured(
            EDGE_PROMPT, json.dumps(payload, ensure_ascii=False), EdgeSuggestions
        )

        parsed = (
            result
            if isinstance(result, EdgeSuggestions)
            else EdgeSuggestions.model_validate(result)
        )

        allowed = {c.id for c in candidates}
        edges: list[Edge] = []

        for suggestion in parsed.edges:
            target_id = suggestion.target_node_id

            # Only allow edges to known candidates, never to self.
            if target_id not in allowed or target_id == node.id:
                continue

            label = suggestion.label.strip() or "related"
            stamp = now_iso()

            # Contradictions invalidate previous non-contradictory edges between the pair.
            if label == "contradicts":
                self._invalidate_prior_edges(node.id, target_id, stamp)

            episodes = [node.id, target_id]

            # Store semantic edges both directions for easier graph traversal.
            for src, dst in ((node.id, target_id), (target_id, node.id)):
                edge = Edge(
                    id=make_edge_id(src, dst, label),
                    source_node_id=src,
                    target_node_id=dst,
                    label=label,
                    summary=suggestion.summary.strip(),
                    valid_at=stamp,
                    source_episode_ids=episodes,
                )
                self.store.upsert_edge(edge)
                edges.append(edge)

        return edges

    def _invalidate_prior_edges(
        self, source_id: str, target_id: str, stamp: str
    ) -> None:
        # Expire older edges between two nodes when a contradiction is added.
        for edge in self.store.get_edges_for_node(target_id):
            if {edge.source_node_id, edge.target_node_id} != {source_id, target_id}:
                continue

            if edge.label == "contradicts" or edge.invalid_at:
                continue

            edge.invalid_at = stamp
            edge.expired_at = stamp
            self.store.upsert_edge(edge)

    def _persist_node(self, node: Node, *, cheap: bool = False) -> Node:
        # Fill fields, save node, store vectors, and build semantic edges.
        (self._fill_cheap_fields if cheap else self._fill_derived_fields)(node)

        self.store.upsert_node(node)

        body_vec, summary_vec = self._store_vectors(node)
        self._build_semantic_edges(node, body_vec, summary_vec)

        return node

    def _supersede(self, old: Node, new: Node) -> None:
        # Link old -> new and new -> old so history/revision lineage is preserved.
        self.store.upsert_edge(
            Edge(
                id=make_edge_id(old.id, new.id, "superseded_by"),
                source_node_id=old.id,
                target_node_id=new.id,
                label="superseded_by",
                summary="Newer source material replaces these facts.",
            )
        )

        self.store.upsert_edge(
            Edge(
                id=make_edge_id(new.id, old.id, "supersedes"),
                source_node_id=new.id,
                target_node_id=old.id,
                label="supersedes",
                summary="Older source material replaced by this node.",
            )
        )

        # Mark old node inactive for normal retrieval.
        self.store.set_node_status(old.id, NodeStatus.superseded)

        # Superseded facts should no longer surface as evidence.
        try:
            self.store.delete_search_items(old.id)
        except Exception as exc:
            log.info("clear search items on supersede failed %s: %s", old.id, exc)

    def _remap_edges(
        self,
        old_id: str,
        new_id: str,
        labels: tuple[str, ...] = ("follows", "reference", "supports"),
    ) -> None:

        wanted = set(labels)

        # Move selected edges from old node ID to replacement node ID.
        for edge in self.store.get_edges_for_node(old_id):
            if edge.label not in wanted:
                continue

            src = new_id if edge.source_node_id == old_id else edge.source_node_id
            dst = new_id if edge.target_node_id == old_id else edge.target_node_id

            # Avoid creating self-loops.
            if src == dst:
                continue

            self.store.upsert_edge(
                Edge(
                    id=make_edge_id(src, dst, edge.label),
                    source_node_id=src,
                    target_node_id=dst,
                    label=edge.label,
                    summary=edge.summary,
                    valid_at=edge.valid_at,
                    source_episode_ids=edge.source_episode_ids,
                )
            )

            self.store.delete_edge(edge.id)

    def _link_supports(self, node: Node, source_node_ids: list[str]) -> None:
        # Create source -> derived-note support edges.
        for source_id in source_node_ids:
            if not self.store.get_node(source_id):
                continue

            self.store.upsert_edge(
                Edge(
                    id=make_edge_id(source_id, node.id, "supports"),
                    source_node_id=source_id,
                    target_node_id=node.id,
                    label="supports",
                    summary="Source node supports this derived node.",
                )
            )

    def _link_references(self, node: Node, cited_node_ids: list[str]) -> None:

        # Create derived-note -> source reference edges for UI provenance.
        for source_id in dedupe([cid for cid in cited_node_ids if cid]):
            if not self.store.get_node(source_id):
                continue

            self.store.upsert_edge(
                Edge(
                    id=make_edge_id(node.id, source_id, "reference"),
                    source_node_id=node.id,
                    target_node_id=source_id,
                    label="reference",
                    summary="Derived note references this source node.",
                )
            )

    # endregion Semantic Edges / Node Persistence

    # region Cluster Assignment Helpers

    def _cluster_for_references(
        self, node_id: str, source_node_ids: list[str], body_vec: list[float]
    ) -> str:

        # Prefer the majority cluster from cited source nodes.
        cited_clusters = [
            n.cluster
            for n in (self.store.get_node(sid) for sid in dedupe(source_node_ids))
            if n and n.status == NodeStatus.active and (n.cluster or "").strip()
        ]

        if cited_clusters:
            counts = Counter(cited_clusters)
            top = counts.most_common()
            best = top[0][1]
            tied = {c for c, freq in top if freq == best}

            if len(tied) == 1:
                return next(iter(tied))

            # Tie-break by nearest node whose cluster is one of the tied clusters.
            return (
                self._nearest_cluster(node_id, body_vec, allowed=tied)
                or sorted(tied)[0]
            )

        # If no cited clusters exist, fall back to nearest active node's cluster.
        return self._nearest_cluster(node_id, body_vec) or "Agent Notes"

    def _nearest_cluster(
        self, node_id: str, body_vec: list[float], allowed: set[str] | None = None
    ) -> str | None:
        # Return the cluster of the nearest active neighbor, optionally restricted.
        for cid, _dist in self.store.vector_search(body_vec, "vec_body", 50):
            if cid == node_id:
                continue

            nn = self.store.get_node(cid)
            if not nn or nn.status != NodeStatus.active:
                continue

            cluster = (nn.cluster or "").strip()
            if cluster and (allowed is None or cluster in allowed):
                return cluster

        return None

    # endregion Cluster Assignment Helpers

    # region Metadata / Enrichment Public Wrappers

    def get_meta(self, key: str) -> str | None:
        # Thin wrapper around store metadata read.
        return self.store.get_meta(key)

    def set_meta(self, key: str, value: str) -> None:
        # Thin wrapper around store metadata write.
        self.store.set_meta(key, value)

    def refresh_clusters(self) -> None:
        # Public wrapper to recluster and ensure cluster names are UI-friendly.
        self._refresh_clusters()

    def enrich_summary(self, node_id: str) -> None:
        # Fill summary for an active node if it does not already have one.
        node = self.store.get_node(node_id)
        if not node or node.status != NodeStatus.active or node.summary.strip():
            return

        body = node.body.strip()
        if not body:
            return

        # Generate summary and persist updated node.
        node.summary = self.gateway.llm.complete(SUMMARY_PROMPT, body).strip()
        node.updated_at = now_iso()
        self.store.upsert_node(node)

        if node.summary.strip():
            # Store summary vector and rebuild search items now that summary exists.
            self._ensure_vec()
            self.store.set_vector(
                node.id,
                "vec_summary",
                self.gateway.embedder.embed_document(node.summary),
            )
            self._store_search_items(node)

    def enrich_entity_dedup(self, node_id: str) -> None:
        # Background duplicate/entity check for an active node.
        node = self.store.get_node(node_id)
        if not node or node.status != NodeStatus.active:
            return

        body_vec = self.store.get_vector(node.id, "vec_body")
        if body_vec is None:
            body_vec = self.gateway.embedder.embed_document(node.body)

        summary_vec = self.store.get_vector(node.id, "vec_summary")

        candidates = self._knn_candidates(node.id, body_vec, summary_vec)
        self._link_entity_duplicates(node, candidates)

    def enrich_cascade(
        self, replacements: dict[str, str], stale_sources: list[str]
    ) -> None:
        # Background cascade entry point; records actions internally for debugging.
        actions: list[str] = []
        self._cascade_dependents(replacements, set(stale_sources), actions)

    # endregion Metadata / Enrichment Public Wrappers

    # region Revision Metadata / Structural Edges

    def _backfill_revision_metadata(self, node: Node) -> Node:
        # Fill missing revision fields on older nodes so matching works reliably.
        changed = False

        if not node.source_material_hash:
            node.source_material_hash = source_hash(node.body)
            changed = True

        if not node.claims:
            extracted = self._extract_claims(node.body)
            node.entity = node.entity or extracted.entity
            node.claims = extracted.claims
            changed = True

        if not node.entity and node.keywords:
            node.entity = node.keywords[0]
            changed = True

        if changed:
            self.store.upsert_node(node)

        return node

    def _source_version_for_nodes(self, nodes: list[Node]) -> str:
        # Compute one stable version hash for a whole document/source.
        if not nodes:
            return source_hash("")

        parts: list[str] = []

        for node in nodes:
            # Prefer original source file contents if available, otherwise node body.
            if node.source_path and Path(node.source_path).exists():
                parts.append(
                    Path(node.source_path).read_text(encoding="utf-8", errors="ignore")
                )
            else:
                parts.append(node.body)

        return source_hash("\n\n--- NODE BREAK ---\n\n".join(parts))

    def _replace_structural_edges(
        self, document_name: str | None, edges: list[Edge]
    ) -> None:
        # Remove old follows edges for this document, then add the new active ones.
        if document_name:
            node_ids = {
                n.id
                for n in self.store.get_nodes_by_document(document_name)
                if n.type == NodeType.endogenous
            }
            self.store.delete_edges_by_label_for_nodes("follows", node_ids)

        # Only add structural edges where both endpoints still exist and are active.
        for edge in edges:
            source = self.store.get_node(edge.source_node_id)
            target = self.store.get_node(edge.target_node_id)

            if (
                source
                and target
                and source.status == NodeStatus.active
                and target.status == NodeStatus.active
            ):
                self.store.upsert_edge(edge)

    # endregion Revision Metadata / Structural Edges

    # region Cascade Regeneration

    def _cascade_dependents(
        self, replacements: dict[str, str], stale_sources: set[str], actions: list[str]
    ) -> None:
        # Regenerate or stale exogenous notes affected by changed/stale source nodes.
        max_hops = max(0, self.settings.cascade_max_hops)
        max_nodes = max(0, self.settings.cascade_max_nodes)

        if max_hops == 0 or max_nodes == 0:
            if replacements or stale_sources:
                actions.append("cascade-skipped:disabled")
            return

        # Seed from both sides of each replacement: after a hand edit the
        # `supports` edges have been remapped onto the new node, while the
        # revision flow leaves them on the old one.
        frontier: deque[tuple[str, int]] = deque(
            (nid, 0)
            for nid in sorted(
                set(replacements) | set(replacements.values()) | set(stale_sources)
            )
        )

        visited: set[str] = set()
        processed = 0

        while frontier:
            changed_id, depth = frontier.popleft()
            target_depth = depth + 1

            if target_depth > max_hops:
                continue

            # Find exogenous notes supported by the changed source.
            for edge in self.store.get_outgoing_edges(changed_id, "supports"):
                target = self.store.get_node(edge.target_node_id)

                if (
                    not target
                    or target.status != NodeStatus.active
                    or target.type != NodeType.exogenous
                    or target.id in visited
                ):
                    continue

                if processed >= max_nodes:
                    actions.append(
                        f"cascade-cap-hit:max_nodes={max_nodes}:at={target.id}"
                    )
                    return

                visited.add(target.id)
                processed += 1

                # Rebuild support set using replacements, then regenerate if possible.
                support_nodes = self._current_support_nodes(target, replacements)
                replacement = (
                    self._regenerate_exogenous_node(target, support_nodes)
                    if support_nodes
                    else None
                )

                if replacement is None:
                    self.store.set_node_status(target.id, NodeStatus.stale)
                    actions.append(f"stale-exogenous:{target.id}")
                else:
                    replacements[target.id] = replacement.id
                    actions.append(
                        f"regenerated-exogenous:{target.id}->{replacement.id}"
                    )

                # Continue cascade outward if hop limit allows.
                if target_depth < max_hops:
                    frontier.append((target.id, target_depth))

    def _current_support_nodes(
        self, node: Node, replacements: dict[str, str]
    ) -> list[Node]:
        # Resolve current active support nodes for an exogenous note.
        support_nodes: dict[str, Node] = {}

        for edge in self.store.get_incoming_edges(node.id, "supports"):
            source_id = replacements.get(edge.source_node_id, edge.source_node_id)
            source = self.store.get_node(source_id)

            # If source was superseded, follow superseded_by to active replacement.
            if source and source.status == NodeStatus.superseded:
                for swap in self.store.get_outgoing_edges(source.id, "superseded_by"):
                    target = self.store.get_node(swap.target_node_id)
                    if target and target.status == NodeStatus.active:
                        source = target
                        break

            if source and source.status == NodeStatus.active:
                support_nodes[source.id] = source

        return self._collapse_same_as(list(support_nodes.values()))

    def _regenerate_exogenous_node(
        self, old: Node, support_nodes: list[Node]
    ) -> Node | None:
        # Regenerate an agent/exogenous note from its current support material.
        if not support_nodes:
            return None

        payload = {
            "previous_node": {
                "id": old.id,
                "title": old.title,
                "summary": old.summary,
                "body": old.body[:4000],
            },
            "current_support_material": [
                {
                    "id": n.id,
                    "title": n.title,
                    "summary": n.summary,
                    "body": n.body[:2500],
                }
                for n in support_nodes[:8]
            ],
        }

        body = self.gateway.llm.complete(
            REGENERATE_EXOGENOUS_PROMPT, json.dumps(payload, ensure_ascii=False)
        ).strip()

        if not body:
            return None

        support_ids = sorted(n.id for n in support_nodes)

        # Version ties regenerated content to support IDs and support versions.
        version = source_hash(
            "|".join(
                [
                    source_hash(body),
                    *support_ids,
                    *(n.source_version or "" for n in support_nodes),
                ]
            )
        )

        replacement = Node(
            id=make_exogenous_node_id(f"{old.id}|{version}|{body}"),
            body=body,
            type=NodeType.exogenous,
            title=old.title,
            original_document_name=old.original_document_name,
            source_version=version,
            cluster=old.cluster,
        )

        if replacement.id == old.id:
            return old

        self._persist_node(replacement)
        self._link_supports(replacement, support_ids)

        # Keep UI provenance: notes render their sources via `reference` edges.
        self._link_references(replacement, support_ids)

        self._supersede(old, replacement)

        return replacement

    # endregion Cascade Regeneration

    # region Clustering / Cluster Naming

    def _recluster(
        self, resolution: float = 1.0, seed: int = 42, persist: bool = True
    ) -> dict[str, str]:
        # Build an undirected graph from active nodes and edges, then run Louvain.
        import networkx as nx

        nodes = [n for n in self.store.get_all_nodes() if n.status == NodeStatus.active]
        node_by_id = {n.id: n for n in nodes}

        graph = nx.Graph()
        graph.add_nodes_from(node_by_id)

        # Add graph edges; repeated edges increase relationship weight.
        for edge in self.store.get_all_edges():
            src, dst = edge.source_node_id, edge.target_node_id

            if src not in node_by_id or dst not in node_by_id or src == dst:
                continue

            if graph.has_edge(src, dst):
                graph[src][dst]["weight"] += 1.0
            else:
                graph.add_edge(src, dst, weight=1.0)

        communities = nx.community.louvain_communities(
            graph, weight="weight", resolution=resolution, seed=seed
        )

        ordered = sorted(communities, key=len, reverse=True)

        per_comm: list[Counter[str]] = []
        titles: list[list[str]] = []
        doc_freq: Counter[str] = Counter()

        # Gather keyword/title evidence for each community name.
        for members in ordered:
            counts: Counter[str] = Counter()
            comm_titles: list[str] = []

            for nid in members:
                node = node_by_id.get(nid)

                if node:
                    counts.update(k.lower().strip() for k in node.keywords if k.strip())

                    if node.title:
                        comm_titles.append(node.title)

            per_comm.append(counts)
            titles.append(comm_titles)
            doc_freq.update(counts.keys())

        n_comms = max(len(ordered), 1)
        mapping: dict[str, str] = {}
        used: Counter[str] = Counter()
        used_labels: list[str] = []

        log.info("recluster.naming communities=%d", len(ordered))

        # Name each community using TF-IDF keywords and sample titles.
        for index, members in enumerate(
            tqdm(ordered, desc="recluster: naming clusters", unit="cluster")
        ):
            keywords = self._tfidf_keywords(per_comm[index], doc_freq, n_comms, k=8)
            label = self._name_cluster(keywords, titles[index][:12], used_labels)

            used[label] += 1
            if used[label] > 1:
                label = f"{label} {used[label]}"

            used_labels.append(label)

            for nid in members:
                mapping[nid] = label

        # Persist cluster labels back to nodes if requested.
        if persist:
            for node in nodes:
                new_label = mapping.get(node.id)

                if new_label and node.cluster != new_label:
                    node.cluster = new_label
                    self.store.upsert_node(node)

        return mapping

    def _tfidf_keywords(
        self, counts: Counter[str], doc_freq: Counter[str], n_comms: int, k: int = 5
    ) -> list[str]:
        # Score keywords higher if common in this cluster but rare across clusters.
        if not counts:
            return []

        import math

        scored = sorted(
            counts.items(),
            key=lambda kv: kv[1] * math.log(1 + n_comms / max(doc_freq[kv[0]], 1)),
            reverse=True,
        )

        return [kw for kw, _ in scored[:k]]

    def _name_cluster(
        self, keywords: list[str], titles: list[str], used_names: list[str]
    ) -> str:
        # Ask the LLM for one short Japanese cluster label, then validate it.
        import re

        def has_japanese(text: str) -> bool:
            return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text))

        if not keywords and not titles:
            raise SystemExit(
                "fatal: cluster naming failed: no keywords or titles were available"
            )

        user = (
            f"キーワード: {', '.join(keywords) or '(なし)'}\n"
            f"サンプルタイトル: {'; '.join(titles) or '(なし)'}\n"
            f"避けるべき既存の名前: {', '.join(used_names) or '(なし)'}\n\n"
            "必ず日本語のクラスタ名を1つだけ返してください。\n"
            "英語のみの名前は禁止です。\n"
            "少なくとも1文字以上の日本語文字、つまりひらがな、カタカナ、または漢字を含めてください。\n"
            "API名、関数名、ライブラリ名、頭字語、識別子は必要な場合のみ原文のまま残してかまいません。\n"
            "説明、引用符、句読点、箇条書きは不要です。\n\n"
            "日本語クラスタ名:"
        )

        try:
            raw = self.gateway.llm.complete(CLUSTER_NAMER_SYSTEM, user)
        except Exception as exc:
            raise SystemExit(
                f"fatal: cluster naming failed: LLM call raised {type(exc).__name__}: {exc}"
            ) from exc

        # Normalize the LLM output into a single label string.
        name = " ".join(raw.strip().strip("\"'").split())

        if not name:
            raise SystemExit(
                f"fatal: cluster naming failed: empty LLM response; "
                f"keywords={keywords!r}; titles={titles[:5]!r}"
            )

        if len(name) > 60:
            raise SystemExit(f"fatal: cluster naming failed: name too long: {name!r}")

        if len(name.split()) > 6:
            raise SystemExit(f"fatal: cluster naming failed: too many words: {name!r}")

        if name.lower() in {u.lower() for u in used_names}:
            raise SystemExit(
                f"fatal: cluster naming failed: duplicate cluster name: {name!r}"
            )

        if not has_japanese(name):
            raise SystemExit(
                "fatal: cluster naming failed: LLM returned English-only/non-Japanese name: "
                f"{name!r}; keywords={keywords!r}; titles={titles[:5]!r}"
            )

        return name

    def _ensure_japanese_clusters(self) -> dict[str, str]:
        """Ask LLM to rename clusters for Japanese UI."""

        try:
            # Collect existing non-empty cluster names from active nodes.
            nodes = [
                n
                for n in self.store.get_all_nodes()
                if getattr(n, "status", None) == NodeStatus.active
                and getattr(n, "cluster", None)
                and n.cluster.strip()
            ]

            cluster_names = sorted({n.cluster.strip() for n in nodes})
            if not cluster_names:
                return {}

            prompt = """
            You are reviewing knowledge-graph cluster labels for a Japanese user interface.
            You will receive a list of existing cluster names.
            
            Task:
            - Return ONLY the cluster names that should be renamed.
            - If a name is already good, do not include it.
            - If an English abbreviation, acronym, product name, library name, model name, or technical term is better left unchanged, do not include it.
            - If a name is awkward English and should be localized for Japanese users, provide a concise Japanese or natural Japanese-mixed replacement.
            - Preserve technical meaning.
            - Do not over-translate proper nouns.
            - New names should be short, clear, and suitable as UI cluster labels.
            - Maximum 60 characters per new name.
            - If no names need changes, return an empty renames list.
            """

            result = self.gateway.llm.complete_structured(
                prompt,
                json.dumps({"cluster_names": cluster_names}, ensure_ascii=False),
                ClusterRenamePlan,
            )

            if not isinstance(result, ClusterRenamePlan):
                return {}

            allowed_originals = set(cluster_names)
            mapping: dict[str, str] = {}

            # Validate proposed renames before applying them.
            for item in result.renames:
                old_name = (item.original_name or "").strip()
                new_name = (item.new_name or "").strip()

                if not old_name or not new_name:
                    continue

                if old_name not in allowed_originals:
                    continue

                if old_name == new_name:
                    continue

                if len(new_name) > 60:
                    continue

                mapping[old_name] = new_name

            if not mapping:
                return {}

            existing_names = set(cluster_names)
            final_mapping: dict[str, str] = {}

            # Avoid renaming into another existing cluster name accidentally.
            for old_name, new_name in mapping.items():
                would_collide = (
                    new_name in existing_names
                    and new_name != old_name
                    and new_name not in mapping
                )

                if would_collide:
                    continue

                final_mapping[old_name] = new_name

            if not final_mapping:
                return {}

            # Apply final cluster rename mapping to all active nodes.
            for node in nodes:
                old_cluster = node.cluster.strip()

                if old_cluster in final_mapping:
                    node.cluster = final_mapping[old_cluster]
                    self.store.upsert_node(node)

            return final_mapping

        except Exception as e:
            # Best-effort: cluster renaming should not break the app.
            log.warning("ensure_japanese_clusters failed: %s", e)
            return {}

    def _refresh_clusters(self) -> None:
        # Best-effort refresh: recompute clusters, then localize names.
        try:
            self.recluster()
            self.ensure_japanese_clusters()
        except Exception as exc:
            log.info("recluster skipped: %s", exc)

    # endregion Clustering / Cluster Naming

    # region Markdown Output Dispatch

    def _load_md_output(self, out_path: Path) -> tuple[list[Node], list[Edge]]:
        # Detect output format: old parser output has manifest.json, new one has docs/.
        if (out_path / "manifest.json").exists():
            return self._load_old_manifest_output(out_path)

        return self._load_new_planning_docs_output(out_path)

    # endregion Markdown Output Dispatch

    # region Old Manifest Output Loader

    def _load_old_manifest_output(
        self, out_path: Path
    ) -> tuple[list[Node], list[Edge]]:
        # Load legacy manifest metadata produced by older parser output.
        manifest = json.loads((out_path / "manifest.json").read_text(encoding="utf-8"))

        source_path = manifest.get("source")
        document_name = Path(source_path).name if source_path else out_path.name

        # Index manifest file records by markdown filename.
        by_filename = {
            r["filename"]: r for r in manifest.get("files", []) if r.get("filename")
        }

        nodes: list[Node] = []
        sections: dict[str, list[str]] = {}

        for filename, record in by_filename.items():
            leaf = out_path / filename
            if not leaf.exists():
                continue

            # Split optional frontmatter from markdown body.
            meta, body = self._split_frontmatter(
                leaf.read_text(encoding="utf-8", errors="ignore")
            )

            if not body:
                continue

            # Build an endogenous node from the legacy manifest record.
            node = Node(
                id=make_node_id(body, document_name),
                body=body,
                type=NodeType.endogenous,
                title=record.get("title") or meta.get("title", ""),
                original_document_name=document_name,
                source_path=source_path,
                source_ranges=self._parse_ranges(record.get("source_ranges"))
                or self._parse_ranges(meta.get("source_lines")),
                summary=record.get("summary") or meta.get("summary", ""),
                cluster=self._humanize(Path(filename).parent.name),
            )

            nodes.append(node)

            # Group nodes by folder/section so each section can be chained.
            sections.setdefault(str(Path(filename).parent), []).append(node.id)

        edges: list[Edge] = []

        # Create follows edges between adjacent pages in the same section.
        for node_ids in sections.values():
            edges += self._chain_edges(
                node_ids, "Adjacent page in the same source section."
            )

        return nodes, edges

    # endregion Old Manifest Output Loader

    # region New Planning Docs Output Loader

    def _load_new_planning_docs_output(
        self, out_path: Path
    ) -> tuple[list[Node], list[Edge]]:
        # New parser output stores metadata in _planning/ and markdown pages in docs/.
        planning_dir = out_path / "_planning"
        docs_dir = out_path / "docs"

        if not docs_dir.exists():
            raise FileNotFoundError(f"no docs directory found in {out_path}")

        # Planning JSON files are optional; default to empty metadata.
        metadata = self._read_json(planning_dir / "metadata.json", default={})
        coverage = self._read_json(planning_dir / "coverage.json", default={})

        document_name = (
            metadata.get("inferred_file_name")
            or metadata.get("original_file_name")
            or out_path.name
        )

        # Index planning metadata by canonical markdown name.
        metadata_by_name = {
            i.get("name"): i for i in metadata.get("files", []) if i.get("name")
        }
        coverage_by_name = {
            i.get("filename"): i for i in coverage.get("files", []) if i.get("filename")
        }

        nodes: list[Node] = []
        ordered_ids: list[str] = []

        # Sort pages numerically when filenames start with a number.
        for md_file in sorted(docs_dir.glob("*.md"), key=self._doc_sort_key):
            meta, body = self._split_frontmatter(
                md_file.read_text(encoding="utf-8", errors="ignore")
            )

            body = body.strip()
            if not body:
                continue

            canonical = self._canonical_doc_name(md_file.name)
            meta_rec = metadata_by_name.get(canonical, {})
            cov_rec = coverage_by_name.get(canonical, {})

            ranges: list[tuple[int, int]] = []

            # Prefer explicit coverage start/end from planning coverage.
            start, end = cov_rec.get("source_start"), cov_rec.get("source_end")
            if start is not None and end is not None:
                try:
                    ranges = [(int(start), int(end))]
                except (TypeError, ValueError):
                    ranges = []
            else:
                # Fall back to frontmatter source_lines if coverage is missing.
                ranges = self._parse_ranges(meta.get("source_lines"))

            # Build an endogenous node from the docs/ markdown page.
            node = Node(
                id=make_node_id(body, document_name),
                body=body,
                type=NodeType.endogenous,
                title=(
                    cov_rec.get("title")
                    or meta.get("title")
                    or meta_rec.get("header")
                    or self._title_from_markdown(body)
                    or self._humanize(canonical.removesuffix(".md"))
                ),
                original_document_name=document_name,
                source_path=str(md_file),
                source_ranges=ranges,
                summary=cov_rec.get("summary") or meta.get("summary") or "",
                cluster=cov_rec.get("header") or meta_rec.get("header") or "General",
            )

            nodes.append(node)
            ordered_ids.append(node.id)

        # Link pages in document order.
        edges = self._chain_edges(ordered_ids, "Next page in the source document.")

        return nodes, edges

    # endregion New Planning Docs Output Loader

    # region Markdown Metadata Parsing

    def _split_frontmatter(self, text: str) -> tuple[dict[str, str], str]:
        # Extract simple YAML-style frontmatter if the markdown starts with it.
        match = _FRONTMATTER_RE.match(text)
        if not match:
            return {}, text

        meta: dict[str, str] = {}

        # Parse basic key: value lines; this intentionally avoids full YAML parsing.
        for line in match.group(1).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip().strip('"')

        return meta, match.group(2).strip()

    def _parse_ranges(self, value: str | list[object] | None) -> list[tuple[int, int]]:
        # Normalize source range metadata into [(start, end), ...].
        if not value:
            return []

        # Some frontmatter stores ranges as a JSON string.
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return []

        ranges: list[tuple[int, int]] = []

        # Keep only valid 2-item numeric ranges.
        for pair in value or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                try:
                    ranges.append((int(pair[0]), int(pair[1])))
                except (TypeError, ValueError):
                    pass

        return ranges

    def _title_from_markdown(self, body: str) -> str | None:
        # Use the first markdown heading as the title.
        for line in body.splitlines():
            stripped = line.strip()

            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or None

        return None

    def _exo_fallback_title(self, origin: str | None, body: str) -> str:
        """Title for an agent note whose markdown has no leading heading: the
        originating question (origin minus the `agent:`/`human:` prefix), else
        the first non-empty line of the body."""

        # Prefer a cleaned origin label/question when available.
        if origin:
            cleaned = re.sub(r"^(agent|human):", "", origin).strip()
            if cleaned:
                return cleaned[:80]

        # Otherwise use the first non-empty body line.
        for line in body.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:80]

        return "Agent note"

    # endregion Markdown Metadata Parsing

    # region Naming / Path Helpers

    def _humanize(self, dirname: str) -> str:
        # Turn folder names like "01-api-reference" into "Api Reference".
        name = re.sub(r"^\d+-", "", dirname).replace("-", " ").strip()
        return name.title()[:80] or "General"

    def _document_name(self, value: str) -> str:
        # Normalize document names and cap length for stable storage/display.
        name = " ".join((value or "").strip().split())

        if not name:
            name = "untitled.md"

        return name[:160]

    def _read_json(self, path: Path, default: Any) -> Any:
        # Read JSON if present; otherwise return the caller-provided default.
        if not path.exists():
            return default

        return json.loads(path.read_text(encoding="utf-8"))

    def _canonical_doc_name(self, filename: str) -> str:
        # Strip numeric ordering prefix from docs filenames when present.
        match = _NUMBERED_DOC_RE.match(filename)
        return match.group(1) if match else filename

    def _doc_sort_key(self, path: Path) -> tuple[int, str]:
        # Sort "001-title.md" before non-numbered files; tie-break by filename.
        first = path.name.split("-", 1)[0]
        return (int(first), path.name) if first.isdigit() else (10**9, path.name)

    # endregion Naming / Path Helpers

    # region Structural Edge Helpers

    def _chain_edges(self, node_ids: list[str], summary: str) -> list[Edge]:
        # Create follows edges between consecutive nodes.
        return [
            Edge(
                id=make_edge_id(prev_id, next_id, "follows"),
                source_node_id=prev_id,
                target_node_id=next_id,
                label="follows",
                summary=summary,
            )
            for prev_id, next_id in zip(node_ids, node_ids[1:])
        ]

    # endregion Structural Edge Helpers
