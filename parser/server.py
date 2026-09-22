"""doc-parser HTTP server.

The request handler stays on asyncio. Each parser routes only its blocking
stages to the shared external/GPU executors and limits network calls through
the shared async semaphore. Small bounded work remains inline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from formats import ParseOptions, ParseResult, UnsupportedFormatError, detect
from formats.base import ParseProfile
from workers import Workers

logger = logging.getLogger("doc-parser")

# Keep explicit process/container environment variables authoritative while
# using a local .env file as the fallback configuration source.
load_dotenv(override=False)

# Optional URL prefix, e.g. URL_PREFIX=/aaa/bbb serves every route under
# http://host:port/aaa/bbb/... main.py writes the same value into
# frontend/src/url-prefix.js so the built SPA calls the prefixed API.
URL_PREFIX = os.environ.get("URL_PREFIX", "").strip().rstrip("/")
if URL_PREFIX and not URL_PREFIX.startswith("/"):
    URL_PREFIX = f"/{URL_PREFIX}"

HEARTBEAT_INTERVAL_S = 15.0
UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
MULTIPART_OVERHEAD_BYTES = 1024 * 1024

# Vite builds the React app here (frontend/vite.config.js -> build.outDir).
STATIC_DIR = Path(__file__).resolve().parent / "static"
ROOT_RELATIVE_ASSET_RE = re.compile(
    r'''(?P<attribute>\b(?:href|src)=["'])(?P<url>/[^"']*)''',
    re.IGNORECASE,
)


# ----------------------------------------------------------------------
# app
# ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ready = False
    app.state.workers = Workers()
    app.state.ready = True
    logger.info("workers started: external executor + GPU executor + network limiter")
    try:
        yield
    finally:
        app.state.ready = False
        app.state.workers.shutdown()


app = FastAPI(title="doc-parser", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def strip_url_prefix(request: Request, call_next):
    """Reject unprefixed paths and route prefixed requests internally."""
    path = request.scope.get("path", "")
    if URL_PREFIX:
        if path == URL_PREFIX or path.startswith(f"{URL_PREFIX}/"):
            remaining = path[len(URL_PREFIX) :]
            request.scope["path"] = remaining or "/"
            request.scope["raw_path"] = remaining.encode("utf-8")
        elif path in {"/health", "/health/live", "/health/ready"}:
            pass  # Keep an unprefixed health check available to monitors.
        else:
            return JSONResponse({"detail": "Not Found"}, status_code=404)

    # Reject obviously oversized multipart bodies before Starlette spools them
    # into the container's temporary filesystem. The chunked check in
    # ``_run_parse`` remains authoritative for clients using chunked transfer.
    if request.method == "POST" and request.scope.get("path") in {
        "/parse",
        "/parse/llm-wiki",
    }:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = 0
            if declared_length > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES:
                return JSONResponse(
                    {"detail": f"Upload exceeds the {MAX_UPLOAD_BYTES} byte limit"},
                    status_code=413,
                )
    return await call_next(request)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/health/live")
async def health_live() -> dict:
    """Cheap process liveness probe; it does not inspect dependencies."""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready(request: Request) -> Response:
    """Readiness probe used by container/orchestrator health checks."""
    workers: Workers | None = getattr(request.app.state, "workers", None)
    if not getattr(request.app.state, "ready", False) or workers is None:
        return JSONResponse({"status": "not_ready"}, status_code=503)
    if not workers.ready():
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return JSONResponse({"status": "ready"})


@app.get("/workers")
async def workers(request: Request) -> dict:
    w: Workers = request.app.state.workers
    return w.stats()


@app.post("/parse")
async def parse_generic(
    request: Request,
    file: Annotated[UploadFile, File()],
    manifest: Annotated[str | None, Form()] = None,
    images: Annotated[bool, Query()] = True,
    describe_images: Annotated[bool, Query()] = False,
    llm_base_url: Annotated[str | None, Header(alias="X-LLM-Base-URL")] = None,
    llm_api_key: Annotated[str | None, Header(alias="X-LLM-API-Key")] = None,
    llm_model: Annotated[str | None, Header(alias="X-LLM-Model")] = None,
):
    """Generic Markdown route with optional descriptions for every image."""
    if manifest:
        raise HTTPException(
            status_code=400, detail="manifest is only supported by /parse/llm-wiki"
        )
    # Generic mode describes every extracted Markdown image when requested;
    # it does not apply llm-wiki's image-selection logic.
    return await _run_parse(
        request,
        file,
        None,
        images=images,
        describe_images=describe_images,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        profile=ParseProfile.GENERIC,
    )


@app.post("/parse/llm-wiki")
async def parse_llm_wiki(
    request: Request,
    file: Annotated[UploadFile, File()],
    manifest: Annotated[str | None, Form()] = None,
    images: Annotated[
        bool,
        Query(description="on: base64 image blocks, off: descriptions only"),
    ] = True,
    describe_images: Annotated[
        bool,
        Query(description="generate LLM descriptions for extracted images"),
    ] = True,
    llm_base_url: Annotated[str | None, Header(alias="X-LLM-Base-URL")] = None,
    llm_api_key: Annotated[str | None, Header(alias="X-LLM-API-Key")] = None,
    llm_model: Annotated[str | None, Header(alias="X-LLM-Model")] = None,
):
    """llm-wiki route: full image-unit, description, and manifest behavior."""
    try:
        parsed_manifest = json.loads(manifest) if manifest else None
        if parsed_manifest is not None and not isinstance(parsed_manifest, dict):
            raise ValueError("manifest must be an object")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid manifest: {exc}") from exc

    return await _run_parse(
        request,
        file,
        parsed_manifest,
        images=images,
        describe_images=describe_images,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        profile=ParseProfile.LLM_WIKI,
    )


async def _run_parse(
    request: Request,
    file: UploadFile,
    parsed_manifest: dict | None,
    *,
    images: bool,
    describe_images: bool,
    llm_base_url: str | None,
    llm_api_key: str | None,
    llm_model: str | None,
    profile: ParseProfile,
):  # returns a JSON-serialisable dict or an SSE-style streaming Response
    data_buffer = bytearray()
    while True:
        chunk = await file.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        data_buffer.extend(chunk)
        if len(data_buffer) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds the {MAX_UPLOAD_BYTES} byte limit",
            )
    data = bytes(data_buffer)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    options = ParseOptions(
        images=images,
        describe_images=describe_images,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        filename=file.filename,
        manifest=parsed_manifest,
        profile=profile,
    )

    try:
        parser_cls = detect(data)
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    workers: Workers = request.app.state.workers
    parser = parser_cls()
    task = asyncio.create_task(parser.parse(data, options, workers))

    if parser_cls.stream_response:
        # Leading JSON whitespace acts as an invisible heartbeat. The complete
        # body remains ordinary JSON that clients can parse with response.json().
        return StreamingResponse(
            _stream_json_job(task, workers),
            media_type="application/json",
            headers={"X-Parser": parser_cls.name},
        )

    try:
        result = await task
    except asyncio.CancelledError:
        await _cancel_parse_task(task, workers)
        raise
    except Exception as exc:
        logger.exception("parse failed")
        raise HTTPException(status_code=500, detail=f"Parse failed: {exc}") from exc

    return asdict(result)


async def _cancel_parse_task(task: asyncio.Task[ParseResult], workers: Workers) -> None:
    """Cancel a parser and terminate any in-flight GPU subprocess."""
    if not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    workers.abort_gpu()


async def _stream_json_job(
    task: asyncio.Task[ParseResult], workers: Workers | None = None
) -> AsyncIterator[str]:
    """Yield whitespace heartbeats followed by one valid JSON value.

    JSON parsers ignore the leading whitespace. ``shield`` prevents each
    heartbeat timeout from cancelling the parse.
    """
    try:
        while True:
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=HEARTBEAT_INTERVAL_S,
                )
            except TimeoutError:
                yield "\n"
                continue
            except Exception as exc:  # noqa: BLE001
                yield json.dumps({"error": f"Parse failed: {exc}"})
                return

            yield json.dumps(asdict(result))
            return
    finally:
        if workers is not None and not task.done():
            await _cancel_parse_task(task, workers)


# ----------------------------------------------------------------------
# static frontend (built by ``npm run build`` in frontend/)
# ----------------------------------------------------------------------
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        index_path = STATIC_DIR / "index.html"
        if not URL_PREFIX:
            return FileResponse(index_path)

        # Existing Vite builds use root-relative asset URLs. Prefix them at
        # serving time so strict prefix enforcement does not break the UI.
        html = index_path.read_text(encoding="utf-8")

        def add_url_prefix(match: re.Match[str]) -> str:
            url = match.group("url")
            if (
                url.startswith("//")
                or url == URL_PREFIX
                or url.startswith(f"{URL_PREFIX}/")
            ):
                return match.group(0)
            return f'{match.group("attribute")}{URL_PREFIX}{url}'

        return HTMLResponse(ROOT_RELATIVE_ASSET_RE.sub(add_url_prefix, html))

    @app.get("/{path:path}", include_in_schema=False)
    async def static_file(path: str) -> FileResponse:
        candidate = (STATIC_DIR / path).resolve()
        if candidate.is_file() and STATIC_DIR.resolve() in candidate.parents:
            return FileResponse(candidate)
        # No client-side routing: unknown paths are real 404s, not the SPA shell.
        raise HTTPException(status_code=404)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000)
