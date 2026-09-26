"""FastAPI app: read-only GROWI search + page views + streaming researcher.

No database, no writes, no graph. Routes mirror the plan (§13); the frontend is
served from ./frontend/dist behind an optional WIKI_PREFIX.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import Settings
from gateway import Embedder, Reranker
from growi_client import GrowiAPIError, GrowiSearchClient
from researcher import AgentStopped, Researcher

log = logging.getLogger("growi_search")

HERE = Path(__file__).resolve().parent
FRONTEND_DIST = HERE / "frontend" / "dist"
HEARTBEAT_SECONDS = 15
SENTINEL = object()


def api_error(detail: str, retryable: bool, code: str, **extra: Any) -> dict[str, Any]:
    return {"detail": detail, "retryable": retryable, "code": code, **extra}


def growi_http_error(exc: GrowiAPIError) -> JSONResponse:
    """Map GROWI failures to the documented status/code contract."""
    if exc.status_code in (401, 403):
        return JSONResponse(status_code=502, content=api_error("GROWI authentication failed", False, "growi_auth_failed"))
    if exc.status_code == 0:
        return JSONResponse(status_code=503, content=api_error("GROWI is unreachable", True, "growi_unavailable"))
    if exc.status_code == 404:
        return JSONResponse(status_code=404, content=api_error("page not found", False, "page_not_found"))
    retryable = "not JSON" in str(exc)
    code = "growi_bad_response" if retryable else "growi_error"
    return JSONResponse(status_code=502, content=api_error(str(exc), retryable or "not JSON" in str(exc), code))


class AskRequest(BaseModel):
    question: str
    overrides: dict[str, Any] | None = None
    context: str | None = None
    cited_node_ids: list[str] | None = None


class PrefixMiddleware:
    """ASGI middleware stripping one leading WIKI_PREFIX (also serves /health
    unprefixed so process managers can probe without the mount point)."""

    def __init__(self, app: Any, prefix: str) -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket") and self.prefix:
            path = scope.get("path", "")
            if path.startswith(self.prefix + "/"):
                scope = dict(scope)
                scope["path"] = path[len(self.prefix):] or "/"
                scope["raw_path"] = scope["path"].encode("utf-8")
            elif path == self.prefix and scope["type"] == "http":
                await send({"type": "http.response.start", "status": 307,
                            "headers": [(b"location", (self.prefix + "/").encode())]})
                await send({"type": "http.response.body", "body": b""})
                return
            elif path == self.prefix:
                scope = dict(scope)
                scope["path"] = "/"
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None, transport: Any = None, researcher: Any = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.validate_strict()
        client = GrowiSearchClient(
            settings.growi_url,
            settings.growi_token,
            attachment_token=settings.growi_attachment_token,
            root_path=settings.growi_root_path,
            timeout=settings.growi_timeout,
            max_concurrency=settings.growi_concurrency,
            transport=transport,
        )
        # A test transport means offline mode: skip the live reranker probe.
        reranker = None if transport is not None else Reranker.build(settings)
        embedder = None if transport is not None else Embedder.build(settings)
        app.state.settings = settings
        app.state.client = client
        app.state.reranker = reranker
        app.state.embedder = embedder
        app.state.researcher = researcher or Researcher(client, settings, reranker, embedder)
        app.state.runs = {}
        if transport is None:  # warm the 00-目次 index map in the background (offline tests skip it)
            jev = getattr(app.state.researcher, "jev", None)
            status = (
                "disabled (set WIKI_JEV_ENABLED=1)"
                if not settings.jev_enabled
                else "configured but unavailable"
                if jev is None
                else f"enabled backend={settings.jev_backend} adapter={type(jev).__name__}"
            )
            print(f"[growi-search] Jev: {status}", flush=True)
            if settings.jev_enabled and jev is None:
                raise RuntimeError("Jev is enabled but its local model or hosted endpoint is unavailable")
            threading.Thread(target=app.state.researcher.index_map.snapshot, name="index-map-warmup", daemon=True).start()
        try:
            app.state.growi_ok = bool(await asyncio.to_thread(client.health))
        except Exception as exc:  # noqa: BLE001 - startup probe is informational
            app.state.growi_ok = False
            log.warning("GROWI health check failed at startup: %s", exc)
        log.info(
            "growi-search ready (growi=%s root=%s llm=%s reranker=%s)",
            settings.growi_url,
            settings.growi_root_path,
            settings.llm_ready,
            reranker is not None,
        )
        yield
        client.close()

    app = FastAPI(title="growi-search", lifespan=lifespan)

    # -- readiness / config --------------------------------------------------

    @app.get("/api/ready")
    async def ready() -> JSONResponse:
        st: Settings = app.state.settings
        return JSONResponse(
            {
                "ready": bool(app.state.growi_ok),
                "growi": bool(app.state.growi_ok),
                "search": bool(app.state.growi_ok),
                "llm": st.llm_ready,
                "reranker": app.state.reranker is not None,
                "embedder": app.state.embedder is not None,
                "jev": getattr(app.state.researcher, "jev", None) is not None,
                "root_path": st.growi_root_path,
            }
        )

    @app.get("/health")
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        ok = app.state.growi_ok
        return {"ok": bool(ok), "growi": "up" if ok else "down"}

    @app.get("/api/growi")
    async def growi_config() -> dict[str, Any]:
        st: Settings = app.state.settings
        # Never include the token. `enabled` keeps the frontend growiLinkFor contract.
        return {
            "enabled": True,
            "url": st.growi_url,
            "root_path": st.growi_root_path,
            "doc_parser_url": st.doc_parser_url,
        }

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return app.state.settings.public_dict()

    # -- read-only page views --------------------------------------------------

    @app.get("/api/pages/children")
    async def page_children(request: Request) -> JSONResponse:
        page_id = request.query_params.get("page_id") or ""
        path = request.query_params.get("path") or ""
        if bool(page_id) == bool(path):
            return JSONResponse(
                status_code=400,
                content=api_error("exactly one of page_id or path is required", False, "bad_request"),
            )
        researcher: Researcher = app.state.researcher
        try:
            pages = await researcher.children(page_id=page_id or None, path=path or None)
        except GrowiAPIError as exc:
            return growi_http_error(exc)
        settings = app.state.settings
        pages = [page for page in pages if page.path.rstrip("/").rsplit("/", 1)[-1] != settings.index_page_name]
        return JSONResponse({"children": [page.public_dict() for page in pages]})

    @app.get("/api/document")
    async def document(request: Request) -> JSONResponse:
        path = (request.query_params.get("path") or "").strip()
        if not path.startswith("/"):
            return JSONResponse(status_code=400, content=api_error("path is required", False, "bad_request"))
        try:
            return JSONResponse(await app.state.researcher.document_view(path))
        except GrowiAPIError as exc:
            return growi_http_error(exc)

    @app.get("/api/attachment/{attachment_id}")
    async def attachment(attachment_id: str) -> Response:
        if not re.fullmatch(r"[0-9a-fA-F]{24}", attachment_id):
            return JSONResponse(status_code=404, content=api_error("attachment not found", False, "not_found"))
        try:
            found = await asyncio.to_thread(app.state.client.fetch_attachment, attachment_id)
        except GrowiAPIError as exc:
            return growi_http_error(exc)
        if found is None:
            return JSONResponse(status_code=404, content=api_error("attachment not found", False, "not_found"))
        content, content_type = found
        return Response(content=content, media_type=content_type, headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/node/{page_id:path}")
    async def node(page_id: str) -> JSONResponse:
        researcher = app.state.researcher
        try:
            view = await researcher.node_view(page_id)
        except GrowiAPIError as exc:
            return growi_http_error(exc)
        if view is None:
            return JSONResponse(status_code=404, content=api_error("page not found", False, "page_not_found"))
        return JSONResponse(view)

    @app.get("/api/search")
    async def search(request: Request) -> JSONResponse:
        query = (request.query_params.get("q") or "").strip()
        try:
            limit = max(1, min(int(request.query_params.get("limit") or 12), 50))
        except ValueError:
            limit = 12
        if not query:
            return JSONResponse([])
        researcher = app.state.researcher
        try:
            results = await researcher.fast_search(query, limit)
        except GrowiAPIError as exc:
            return growi_http_error(exc)
        out = [
            {
                **result["node"].public_dict(),
                "score": round(float(result.get("score", 0.0)), 6),
                "evidence": result.get("evidence", []),
                "body": "",  # fast search never hydrates page bodies
            }
            for result in results
        ]
        return JSONResponse(out)

    # -- ask / agent -----------------------------------------------------------

    @app.post("/api/ask")
    async def ask(body: AskRequest) -> JSONResponse:
        researcher = app.state.researcher
        question = (body.question or "").strip()
        if not question:
            return JSONResponse(status_code=400, content=api_error("question is required", False, "bad_request"))
        try:
            researcher.validate_overrides(body.overrides)
        except ValueError as exc:
            return JSONResponse(status_code=400, content=api_error(str(exc), False, "bad_request"))
        try:
            answer = await researcher.ask(
                question, None, body.overrides, None, body.context or "", body.cited_node_ids
            )
        except GrowiAPIError as exc:
            return growi_http_error(exc)
        except RuntimeError:
            return JSONResponse(status_code=503, content=api_error("LLM is unavailable", True, "llm_unavailable"))
        return JSONResponse(
            {"question": answer.question, "answer": answer.answer, "cited_node_ids": answer.cited_node_ids,
             "cited_nodes": answer.cited_nodes, "steps": answer.steps}
        )

    @app.post("/api/ask/stream")
    async def ask_stream(body: AskRequest, request: Request) -> StreamingResponse:
        researcher = app.state.researcher
        question = (body.question or "").strip()
        if not question:
            return JSONResponse(status_code=400, content=api_error("question is required", False, "bad_request"))
        try:
            researcher.validate_overrides(body.overrides)
        except ValueError as exc:
            return JSONResponse(status_code=400, content=api_error(str(exc), False, "bad_request"))

        run_id = uuid.uuid4().hex
        stop_event = threading.Event()
        out: queue.Queue = queue.Queue()

        def emit(event: dict[str, Any]) -> None:
            out.put(event)

        async def runner() -> None:
            try:
                answer = await researcher.ask(
                    question, emit, body.overrides, stop_event, body.context or "", body.cited_node_ids
                )
                emit({"type": "answer", **answer.model_dump()})
            except AgentStopped:
                emit({"type": "cancelled"})
            except GrowiAPIError as exc:
                payload = api_error(str(exc), True, "growi_unavailable" if exc.status_code == 0 else "growi_error")
                emit({"type": "error", **payload})
            except RuntimeError:
                emit(api_error("LLM is unavailable", True, "llm_unavailable", type="error"))
            except Exception as exc:  # noqa: BLE001 - report unexpected failures as events
                log.exception("agent run failed")
                emit(api_error(str(exc), False, "agent_error", type="error"))
            finally:
                app.state.runs.pop(run_id, None)
                out.put(SENTINEL)

        app.state.runs[run_id] = (asyncio.create_task(runner()), stop_event)
        return streaming_response(out, run_id, stop_event, request)

    @app.post("/api/agent-runs/{run_id}/stop")
    async def stop_run(run_id: str) -> JSONResponse:
        entry = app.state.runs.get(run_id)
        if entry is None:
            return JSONResponse(status_code=404, content=api_error("run not found", False, "run_not_found"))
        _task, stop_event = entry
        stop_event.set()  # repeat-safe
        return JSONResponse({"stopped": True, "run_id": run_id})

    def streaming_response(out: queue.Queue, run_id: str, stop_event: threading.Event, request: Request) -> StreamingResponse:
        return StreamingResponse(
            sse_frames(out, run_id, stop_event, request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")

    if settings.prefix:
        app.add_middleware(PrefixMiddleware, prefix=settings.prefix)
    return app


def _queue_get(q: queue.Queue, timeout: float) -> Any:
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


async def sse_frames(out: queue.Queue, run_id: str, stop_event: threading.Event, is_disconnected: Callable[[], Awaitable[bool]]):
    """SSE frame generator: run event, agent events, 15s heartbeat comments,
    a final done frame; cancels the run if the client disappears."""
    loop = asyncio.get_running_loop()

    def next_frame(timeout: float) -> Any:
        return _queue_get(out, timeout)

    try:
        yield f"data: {json.dumps({'type': 'run', 'run_id': run_id}, ensure_ascii=False)}\n\n"
        while True:
            if await is_disconnected():
                stop_event.set()
            item = await loop.run_in_executor(None, next_frame, HEARTBEAT_SECONDS)
            if item is SENTINEL:
                yield 'data: {"type": "done"}\n\n'
                return
            if item is None:  # heartbeat
                yield ": ping\n\n"
                continue
            yield f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"
    finally:
        # Client gone (or stream finished): never leave the agent running.
        stop_event.set()


app = create_app()
