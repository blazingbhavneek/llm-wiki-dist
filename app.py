"""
FastAPI backend with split read/write architecture.

Features:
  - Async bounded-concurrency reads via ReadGraphService
  - Serialized write queue via WriteGraphService
  - Runtime-editable settings via PATCH /api/settings
  - Streaming agent endpoint
  - Full job status visibility

Run:
    pip install fastapi "uvicorn[standard]"
    WIKI_DB=.wiki/test.sqlite uvicorn app:app --reload --port 8787
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from graph.graph import (
    ReadGraphService,
    SharedGraphRuntime,
    WriteGraphService,
    bootstrap_database,
    job_to_dict,
)
from graph.models import Settings

# ============================================================================
# lifecycle
# ============================================================================

runtime: SharedGraphRuntime | None = None
read_service: ReadGraphService | None = None
write_service: WriteGraphService | None = None
agent_runs: dict[str, asyncio.Task] = {}
agent_stop_events: dict[str, threading.Event] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    global runtime, read_service, write_service

    settings = Settings.from_env()
    runtime = SharedGraphRuntime(settings)

    # Bootstrap DB tables / vectors / clusters (blocking)
    await asyncio.to_thread(bootstrap_database, settings)

    read_service = ReadGraphService(runtime, max_reads=16, max_agents=16)
    write_service = WriteGraphService(runtime, max_queue_size=100)
    await write_service.start()

    try:
        yield
    finally:
        await write_service.stop()
        runtime.close()
        runtime = None
        read_service = None
        write_service = None


def _runtime() -> SharedGraphRuntime:
    if runtime is None:
        raise HTTPException(status_code=503, detail="runtime not ready")
    return runtime


def reads() -> ReadGraphService:
    if read_service is None:
        raise HTTPException(status_code=503, detail="read service not ready")
    return read_service


def writes() -> WriteGraphService:
    if write_service is None:
        raise HTTPException(status_code=503, detail="write service not ready")
    return write_service


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
    if job.status == "queued":
        d["position"] = writes().queue_position(job.id)
    return _dump(d)


app = FastAPI(title="LLM-Wiki API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    source_node_ids: list[str] = []
    origin: str | None = None


class DocumentBody(BaseModel):
    body: str
    title: str | None = None
    document_name: str | None = None
    source_path: str | None = None
    source_ranges: list[tuple[int, int]] | None = None


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
    return _redact(_dump(_runtime().settings))


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
        raise HTTPException(status_code=400, detail=str(exc))
    _runtime().update_settings(new)
    return _redact(_dump(new))


@app.patch("/api/settings")
async def patch_settings(payload: dict[str, Any]) -> dict:
    try:
        current_dict = _dump(_runtime().settings)
        current_dict.update(payload)
        new = Settings(**current_dict)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _runtime().update_settings(new)
    return _redact(_dump(new))


@app.post("/api/settings/reset")
async def reset_settings() -> dict:
    new = Settings.from_env()
    _runtime().update_settings(new)
    return _redact(_dump(new))


@app.post("/api/admin/resync")
async def resync() -> dict:
    try:
        job = await writes().enqueue("ensure_japanese_clusters", {})
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    return _job_response(job)


# ============================================================================
# READS
# ============================================================================


@app.get("/api/graph")
async def get_graph() -> dict:
    nodes, edges = await reads().get()
    return {"nodes": [_dump(n) for n in nodes], "edges": [_dump(e) for e in edges]}


@app.get("/api/health")
async def get_health(node_id: str | None = None) -> dict:
    stats = await reads().health(node_id)
    return _dump(stats)


@app.get("/api/node/{node_id}")
async def read_node(node_id: str) -> dict:
    node = await reads().read_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    return _dump(node)


@app.get("/api/node/{node_id}/links")
async def node_links(
    node_id: str, direction: str = "both", label: str | None = None
) -> list[dict]:
    pairs = await reads().follow_link(node_id, label=label, direction=direction)
    return [{"edge": _dump(e), "node": _dump(n)} for e, n in pairs]


@app.get("/api/search")
async def search(q: str, limit: int | None = None) -> list[dict]:
    nodes = await reads().search(q, limit)
    return [_dump(n) for n in nodes]


@app.get("/api/query")
async def query(query_type: str, value: str) -> dict:
    result = await reads().query(query_type, value)
    return _dump(result)


@app.post("/api/recon")
async def recon(payload: RecondBody) -> dict:
    # recon is read-only (it reports status without mutating)
    def work():
        db_session = __import__("graph_read_write").GraphDbSession(
            _runtime().settings, readonly=True
        )
        try:
            session = __import__("graph_read_write").GraphReadSession(
                _runtime(), db_session
            )
            return session.recon(payload.source_file)
        finally:
            db_session.close()

    return await asyncio.to_thread(work)


@app.post("/api/ask")
async def ask(payload: AskBody) -> dict:
    answer = await reads().ask(payload.question, overrides=payload.overrides)
    return _dump(answer)


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
        except Exception as exc:
            events.put({"type": "error", "message": str(exc)})
        finally:
            agent_runs.pop(run_id, None)
            agent_stop_events.pop(run_id, None)
            events.put(sentinel)

    task = asyncio.create_task(run_agent())
    agent_runs[run_id] = task
    agent_stop_events[run_id] = stop_event

    async def stream():
        yield ": connected\n\n"
        yield f"data: {json.dumps({'type': 'run', 'run_id': run_id}, ensure_ascii=False)}\n\n"
        try:
            while True:
                try:
                    event = await loop.run_in_executor(
                        None, lambda: events.get(timeout=15)
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
    task = agent_runs.get(run_id)
    stop_event = agent_stop_events.get(run_id)
    if task is None and stop_event is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    if stop_event is not None:
        stop_event.set()
    if task is not None and not task.done():
        task.cancel()
    return {"run_id": run_id, "status": "stopping"}


# ============================================================================
# WRITES (all through the queue)
# ============================================================================


async def _enqueue(type_: str, payload: dict[str, Any]) -> dict:
    try:
        job = await writes().enqueue(type_, payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
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
        },
    )


@app.post("/api/document")
async def create_document(payload: DocumentBody) -> dict:
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
    return {"pending": writes().enrich.pending()}


@app.post("/api/recluster")
async def recluster(resolution: float = 1.0) -> dict:
    return await _enqueue("recluster", {"resolution": resolution})


@app.post("/api/cascading-update")
async def cascading_update(payload: CascadingUpdateBody) -> dict:
    return await _enqueue("cascading_update", {"source_file": payload.source_file})


@app.post("/api/ingest")
async def ingest(payload: IngestBody) -> dict:
    return await _enqueue("ingest_md_output", {"path": payload.path})


# ============================================================================
# JOB STATUS
# ============================================================================


@app.get("/api/write-jobs")
async def list_write_jobs(status: str | None = None, limit: int = 100) -> list[dict]:
    jobs = writes().list_jobs(status=status, limit=limit)
    return [_job_response(j) for j in jobs]


@app.get("/api/write-jobs/{job_id}")
async def get_write_job(job_id: str) -> dict:
    job = writes().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_response(job)


@app.delete("/api/write-jobs/{job_id}")
async def cancel_write_job(job_id: str) -> dict:
    cancelled = writes().cancel_job(job_id)
    if not cancelled:
        raise HTTPException(status_code=409, detail="job not cancellable")
    return {"job_id": job_id, "status": "cancelled"}
