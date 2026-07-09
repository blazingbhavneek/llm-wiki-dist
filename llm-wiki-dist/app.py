"""
FastAPI backend over four actors:

  - ModelGateway  — chat LLM + embedder + reranker + settings
  - GraphStore    — SQLite persistence (one writable, one read-only instance)
  - Librarian     — all writes: job queue + enrichment drip + bootstrap
  - Researcher    — all reads: search + ask() agent, bounded concurrency

This file is transport only: HTTP/SSE in, actor methods out.

Run:
    pip install fastapi "uvicorn[standard]"
    WIKI_DB=.wiki/wiki3.sqlite uvicorn app:app --port 51023
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.requests import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from graph.core import Settings
from graph.gateway import ModelGateway
from graph.librarian import Librarian, job_to_dict
from graph.researcher import AgentStopped, Researcher
from graph.store import GraphStore
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("app")
SSE_EXECUTOR = ThreadPoolExecutor(max_workers=64, thread_name_prefix="sse")
INGEST_ROOT = Path(os.environ.get("WIKI_INGEST_ROOT", ".")).resolve()

# ============================================================================
# lifecycle
# ============================================================================


class AgentRunRegistry:
    """Tracks in-flight streaming agent runs so /stop can cancel them."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_events: dict[str, threading.Event] = {}

    def register(self, run_id: str, task: asyncio.Task, stop: threading.Event) -> None:
        self._tasks[run_id] = task
        self._stop_events[run_id] = stop

    def remove(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        self._stop_events.pop(run_id, None)

    def stop(self, run_id: str) -> bool:
        """Signal + cancel a run. False when the run id is unknown."""
        task = self._tasks.get(run_id)
        stop_event = self._stop_events.get(run_id)
        if task is None and stop_event is None:
            return False
        if stop_event is not None:
            stop_event.set()
        if task is not None and not task.done():
            task.cancel()
        return True


gateway: ModelGateway | None = None
researcher: Researcher | None = None
librarian: Librarian | None = None
write_store: GraphStore | None = None
read_store: GraphStore | None = None
startup_stage = "starting"
startup_error: str | None = None
bootstrap_task: asyncio.Task | None = None
bootstrap_running = False
agent_runs = AgentRunRegistry()


def api_error(detail: str, retryable: bool, code: str, **extra: Any) -> dict[str, Any]:
    return {"detail": detail, "retryable": retryable, "code": code, **extra}


def _not_ready_detail() -> dict[str, Any]:
    return api_error(
        startup_error or f"server is not ready ({startup_stage})",
        True,
        "not_ready",
        stage=startup_stage,
    )


def _build_stack():
    global startup_stage

    startup_stage = "gateway"
    log.info("startup: initializing gateway")
    settings = Settings.from_env()
    new_gateway = ModelGateway(settings)

    startup_stage = "bootstrap"
    log.info("startup: opening store db=%s", settings.database_path)
    new_write_store = GraphStore(settings.database_path)
    new_librarian = Librarian(new_gateway, new_write_store, max_queue_size=100)

    log.info("startup: running bootstrap (embed/search/cluster catch-up)")
    try:
        new_librarian.bootstrap()
        new_read_store = GraphStore(settings.database_path, readonly=True)
        new_researcher = Researcher(new_gateway, new_read_store)
    except BaseException:
        try:
            new_librarian._enrich_stopping.set()
            new_librarian._enrich_queue.put(None)
        except Exception:
            pass
        new_write_store.close()
        new_gateway.close()
        raise

    return new_gateway, new_write_store, new_librarian, new_read_store, new_researcher


async def _shutdown_stack() -> None:
    global gateway, researcher, librarian, write_store, read_store

    current_librarian = librarian
    current_write_store = write_store
    current_read_store = read_store
    current_gateway = gateway

    gateway = None
    researcher = None
    librarian = None
    write_store = None
    read_store = None

    if current_librarian is not None:
        await current_librarian.stop()
    if current_write_store is not None:
        current_write_store.close()
    if current_read_store is not None:
        current_read_store.close()
    if current_gateway is not None:
        current_gateway.close()


async def _guarded_bootstrap() -> None:
    global gateway, researcher, librarian, write_store, read_store
    global startup_stage, startup_error, bootstrap_running

    if bootstrap_running:
        return

    bootstrap_running = True
    startup_error = None
    await _shutdown_stack()

    try:
        stack = await asyncio.to_thread(_build_stack)
        gateway, write_store, librarian, read_store, researcher = stack
        await librarian.start()
        startup_stage = "ready"
        startup_error = None
        log.info("startup: ready, serving requests")
    except Exception as exc:
        startup_stage = "failed"
        startup_error = f"{type(exc).__name__}: {exc}"
        log.exception("startup/bootstrap failed")
    finally:
        bootstrap_running = False


@asynccontextmanager
async def lifespan(_: FastAPI):
    global bootstrap_task, startup_stage, startup_error

    # Surface graph.* INFO logs (bootstrap / reclustering / cluster naming) that
    # otherwise stay hidden behind the root logger's WARNING default.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("graph_librarian").setLevel(logging.INFO)

    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=64, thread_name_prefix="default")
    )

    startup_stage = "starting"
    startup_error = None
    bootstrap_task = asyncio.create_task(_guarded_bootstrap())

    try:
        yield
    finally:
        if bootstrap_task and not bootstrap_task.done():
            bootstrap_task.cancel()
            try:
                await bootstrap_task
            except asyncio.CancelledError:
                pass
        await _shutdown_stack()


def _gateway() -> ModelGateway:
    if gateway is None or startup_stage != "ready":
        raise HTTPException(status_code=503, detail=_not_ready_detail())
    return gateway


def reads() -> Researcher:
    if researcher is None or startup_stage != "ready":
        raise HTTPException(status_code=503, detail=_not_ready_detail())
    return researcher


def writes() -> Librarian:
    if librarian is None or startup_stage != "ready":
        raise HTTPException(status_code=503, detail=_not_ready_detail())
    return librarian


# ============================================================================
# helpers
# ============================================================================


def _dump(obj: Any) -> Any:
    """Recursive pydantic / dataclass serialization."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _dump(v) for k, v in asdict(obj).items()}
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, (list, tuple)):
        return [_dump(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _dump(v) for k, v in obj.items()}
    return obj


def _job_response(job):
    d = job_to_dict(job)
    # Job payloads can embed whole markdown documents; clients polling job
    # status only need type/status/result/error. Truncate long strings so
    # every poll stays cheap.
    payload = d.get("payload")
    if isinstance(payload, dict):
        d["payload"] = {
            k: (
                v[:200] + f"… ({len(v)} chars)"
                if isinstance(v, str) and len(v) > 200
                else v
            )
            for k, v in payload.items()
        }
    if job.status == "queued":
        d["position"] = writes().queue_position(job.id)
    return _dump(d)


def _path_within_ingest_root(value: str) -> str:
    path = Path(value).expanduser().resolve()
    if not path.is_relative_to(INGEST_ROOT):
        raise HTTPException(
            status_code=400,
            detail=api_error("path outside allowed ingest root", False, "bad_path"),
        )
    return str(path)


app = FastAPI(title="LLM-Wiki API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content=api_error(
            f"サーバー内部エラー: {type(exc).__name__}: {exc}",
            True,
            "internal_error",
        ),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and {"detail", "retryable", "code"} <= set(
        exc.detail
    ):
        content = exc.detail
    else:
        content = api_error(str(exc.detail), exc.status_code >= 500, "http_error")
    return JSONResponse(status_code=exc.status_code, content=content)


@app.get("/api/ready")
async def ready() -> dict[str, Any]:
    return {
        "ready": startup_stage == "ready",
        "stage": startup_stage,
        "error": startup_error,
        "retryable": startup_stage != "ready",
    }


@app.post("/api/admin/restart-bootstrap")
async def restart_bootstrap() -> dict[str, Any]:
    global bootstrap_task

    if startup_stage == "ready":
        return {"ready": True, "stage": startup_stage, "error": None}
    if bootstrap_running:
        return {"ready": False, "stage": startup_stage, "error": startup_error}

    bootstrap_task = asyncio.create_task(_guarded_bootstrap())
    return {"ready": False, "stage": "starting", "error": None}


# ============================================================================
# request models
# ============================================================================


class AskBody(BaseModel):
    question: str
    # Optional per-request tunable overrides (subagents, depth, search net,
    # chat endpoint/model/key). Omitted keys fall back to server defaults.
    overrides: dict[str, Any] | None = None


class UpdateBody(BaseModel):
    body: str


class ExogenousBody(BaseModel):
    body: str
    source_node_ids: list[str] = Field(default_factory=list)
    origin: str | None = None

    # New: original user query this note answers.
    question: str | None = None


class DocumentBody(BaseModel):
    body: str
    title: str | None = None
    document_name: str | None = None
    source_path: str | None = None
    source_ranges: list[tuple[int, int]] | None = None
    # Optional ChunkConfig overrides for the chunk-and-ingest path
    # (ablation/speed switches; unknown keys are ignored server-side).
    chunk_options: dict[str, Any] | None = None


# Documents longer than this many lines (~3 pages) always take the
# chunk-and-ingest path: split into concept pages before node creation.
CHUNK_LINE_THRESHOLD = int(os.environ.get("WIKI_CHUNK_THRESHOLD_LINES", "300"))


class RecondBody(BaseModel):
    source_file: str


class CascadingUpdateBody(BaseModel):
    source_file: str


class IngestBody(BaseModel):
    path: str


# ============================================================================
# SETTINGS (editable at runtime)
# ============================================================================


# Secret fields are server-only (compile-time config). Redact before sending
# to any client so our API keys never reach the browser.
_SECRET_KEYS = {"chat_api_key", "embed_api_key", "rerank_api_key"}


def _redact(data: dict) -> dict:
    return {k: ("" if k in _SECRET_KEYS and v else v) for k, v in data.items()}


@app.get("/api/settings")
async def get_settings() -> dict:
    return _redact(_dump(_gateway().settings))


@app.get("/api/settings/schema")
async def get_settings_schema() -> dict:
    if hasattr(Settings, "model_json_schema"):
        return Settings.model_json_schema()
    if hasattr(Settings, "schema"):
        return Settings.schema()
    return {}


@app.put("/api/settings")
async def replace_settings(payload: dict[str, Any]) -> dict:
    try:
        new = Settings(**payload)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=api_error(str(exc), False, "invalid_settings"),
        )
    _gateway().update_settings(new)
    return _redact(_dump(new))


@app.patch("/api/settings")
async def patch_settings(payload: dict[str, Any]) -> dict:
    try:
        current_dict = _dump(_gateway().settings)
        # Ignore blanked-out secrets: clients receive redacted values ("") and
        # may echo them back; an empty secret must not wipe the real key.
        clean = {k: v for k, v in payload.items() if not (k in _SECRET_KEYS and not v)}
        current_dict.update(clean)
        new = Settings(**current_dict)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=api_error(str(exc), False, "invalid_settings"),
        )
    _gateway().update_settings(new)
    return _redact(_dump(new))


@app.post("/api/settings/reset")
async def reset_settings() -> dict:
    new = Settings.from_env()
    _gateway().update_settings(new)
    return _redact(_dump(new))


@app.post("/api/admin/resync")
async def resync() -> dict:
    try:
        job = await writes().enqueue("ensure_japanese_clusters", {})
    except RuntimeError as exc:
        raise HTTPException(
            status_code=429,
            detail=api_error(str(exc), True, "queue_full"),
        )
    return _job_response(job)


# ============================================================================
# READS
# ============================================================================


@app.get("/api/graph")
async def get_graph() -> dict:
    try:
        nodes, edges = await reads().get()
        return {"nodes": [_dump(n) for n in nodes], "edges": [_dump(e) for e in edges]}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=api_error(f"graph failed: {exc}", True, "search_failed"),
        ) from exc


@app.get("/api/health")
async def get_health(node_id: str | None = None) -> dict:
    try:
        stats = await reads().health(node_id)
        return _dump(stats)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=api_error(f"health failed: {exc}", True, "search_failed"),
        ) from exc


@app.get("/api/node/{node_id}")
async def read_node(node_id: str) -> dict:
    node = await reads().read_node(node_id)
    if node is None:
        raise HTTPException(
            status_code=404,
            detail=api_error("node not found", False, "not_found"),
        )
    return _dump(node)


@app.get("/api/node/{node_id}/links")
async def node_links(
    node_id: str, direction: str = "both", label: str | None = None
) -> list[dict]:
    try:
        pairs = await reads().follow_link(node_id, label=label, direction=direction)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=api_error(str(exc), False, "bad_request"),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=api_error(f"links failed: {exc}", True, "search_failed"),
        ) from exc
    return [{"edge": _dump(e), "node": _dump(n)} for e, n in pairs]


@app.get("/api/search")
async def search(q: str, limit: int | None = None) -> list[dict]:
    try:
        if limit is not None:
            limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=api_error("limit must be an integer", False, "bad_request"),
        ) from exc
    try:
        nodes = await reads().search(q, limit)
        return [_dump(n) for n in nodes]
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=api_error(f"search failed: {exc}", True, "search_failed"),
        ) from exc


@app.get("/api/query")
async def query(query_type: str, value: str) -> dict:
    try:
        result = await reads().query(query_type, value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=api_error(str(exc), False, "bad_request"),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=api_error(f"query failed: {exc}", True, "search_failed"),
        ) from exc
    return _dump(result)


@app.post("/api/recon")
async def recon(payload: RecondBody) -> dict:
    # Read-only revision status: is this source doc new/changed/unchanged
    # relative to what was last ingested? Uses the Librarian for its doc
    # loaders but mutates nothing, so it stays off the write queue.
    source_file = _path_within_ingest_root(payload.source_file)
    if not Path(source_file).exists():
        raise HTTPException(
            status_code=404,
            detail=api_error(f"source does not exist: {source_file}", False, "bad_path"),
        )
    try:
        return await asyncio.to_thread(writes().recon, source_file)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=api_error(str(exc), False, "bad_path"),
        )


@app.post("/api/ask")
async def ask(payload: AskBody) -> dict:
    try:
        answer = await reads().ask(payload.question, overrides=payload.overrides)
        return _dump(answer)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=api_error(f"LLM request failed: {exc}", True, "llm_unavailable"),
        ) from exc


@app.post("/api/ask/stream")
async def ask_stream(payload: AskBody) -> StreamingResponse:
    loop = asyncio.get_running_loop()
    events: queue.Queue = queue.Queue()
    sentinel = object()
    run_id = str(uuid.uuid4())
    stop_event = threading.Event()

    async def run_agent():
        def emit(event: dict) -> None:
            events.put(event)

        try:
            answer = await reads().ask(
                payload.question,
                on_event=emit,
                overrides=payload.overrides,
                stop_event=stop_event,
            )
            events.put({"type": "answer", **_dump(answer)})
        except asyncio.CancelledError:
            stop_event.set()
            events.put({"type": "cancelled", "run_id": run_id})
            raise
        except AgentStopped:
            # The stop endpoint may land as AgentStopped (raised inside the
            # worker thread) instead of task cancellation; both are a clean
            # user-requested stop, not an error.
            events.put({"type": "cancelled", "run_id": run_id})
        except Exception as exc:
            events.put(
                {
                    "type": "error",
                    "message": str(exc),
                    "retryable": True,
                    "code": "agent_failed",
                }
            )
        finally:
            agent_runs.remove(run_id)
            events.put(sentinel)

    task = asyncio.create_task(run_agent())
    agent_runs.register(run_id, task, stop_event)

    async def stream():
        try:
            yield ": connected\n\n"
            yield f"data: {json.dumps({'type': 'run', 'run_id': run_id}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    event = await loop.run_in_executor(
                        SSE_EXECUTOR, lambda: events.get(timeout=15)
                    )
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                if event is sentinel:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            if not task.done():
                stop_event.set()
                task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/agent-runs/{run_id}/stop")
async def stop_agent_run(run_id: str) -> dict:
    if not agent_runs.stop(run_id):
        raise HTTPException(
            status_code=404,
            detail=api_error("agent run not found", False, "not_found"),
        )
    return {"run_id": run_id, "status": "stopping"}


# ============================================================================
# WRITES (all through the queue)
# ============================================================================


async def _enqueue(type_: str, payload: dict[str, Any]) -> dict:
    try:
        job = await writes().enqueue(type_, payload)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=429,
            detail=api_error(str(exc), True, "queue_full"),
        )
    return _job_response(job)


@app.put("/api/node/{node_id}")
async def update_node(node_id: str, payload: UpdateBody) -> dict:
    return await _enqueue("update_node", {"node_id": node_id, "body": payload.body})


@app.delete("/api/node/{node_id}")
async def delete_node(node_id: str) -> dict:
    return await _enqueue("delete_node", {"node_id": node_id})


@app.post("/api/exogenous")
async def create_exogenous(payload: ExogenousBody) -> dict:
    return await _enqueue(
        "create_exogenous",
        {
            "body": payload.body,
            "source_node_ids": payload.source_node_ids,
            "origin": payload.origin,
            "question": payload.question,
        },
    )


@app.post("/api/document")
async def create_document(payload: DocumentBody) -> dict:
    if len(payload.body.splitlines()) > CHUNK_LINE_THRESHOLD:
        return await _enqueue(
            "chunk_and_ingest",
            {
                "body": payload.body,
                "title": payload.title,
                "document_name": payload.document_name,
                "source_path": payload.source_path,
            },
        )

    return await _enqueue(
        "create_document",
        {
            "body": payload.body,
            "title": payload.title,
            "document_name": payload.document_name,
            "source_path": payload.source_path,
            "source_ranges": payload.source_ranges,
        },
    )


@app.get("/api/assimilation")
async def assimilation_status() -> dict:
    """Background enrichment backlog still pending (summary/dedup/cascade/
    recluster). UI can show a 'graph assimilating (N)' badge."""
    return {"pending": writes().enrich_pending()}


@app.post("/api/recluster")
async def recluster(resolution: float = 1.0) -> dict:
    return await _enqueue("recluster", {"resolution": resolution})


@app.post("/api/cascading-update")
async def cascading_update(payload: CascadingUpdateBody) -> dict:
    source_file = _path_within_ingest_root(payload.source_file)
    return await _enqueue("cascading_update", {"source_file": source_file})


@app.post("/api/ingest")
async def ingest(payload: IngestBody) -> dict:
    path = _path_within_ingest_root(payload.path)
    if not Path(path).exists():
        raise HTTPException(
            status_code=404,
            detail=api_error(f"path not found: {path}", False, "bad_path"),
        )
    return await _enqueue("ingest_md_output", {"path": path})


# ============================================================================
# JOB STATUS
# ============================================================================


@app.get("/api/write-jobs")
async def list_write_jobs(status: str | None = None, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    jobs = writes().list_jobs(status=status, limit=limit)
    return [_job_response(j) for j in jobs]


@app.get("/api/write-jobs/{job_id}")
async def get_write_job(job_id: str) -> dict:
    job = writes().get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=api_error("job not found", False, "not_found"),
        )
    return _job_response(job)


@app.delete("/api/write-jobs/{job_id}")
async def cancel_write_job(job_id: str) -> dict:
    job = writes().get_job(job_id)
    status = job.status if job else None
    cancelled = writes().cancel_job(job_id)
    if not cancelled:
        raise HTTPException(
            status_code=409,
            detail=api_error("job not cancellable", False, "job_not_cancellable"),
        )
    return {"job_id": job_id, "status": "cancelling" if status == "running" else "cancelled"}


DIST_DIR = Path(__file__).parent / "frontend" / "dist"

app.mount(
    "/",
    StaticFiles(directory=DIST_DIR, html=True),
    name="frontend",
)
