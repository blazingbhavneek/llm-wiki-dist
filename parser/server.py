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
from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from formats import ParseOptions, ParseResult, UnsupportedFormatError, detect
from workers import Workers
from workers.mineru_api import MinerUApiService

logger = logging.getLogger("doc-parser")

load_dotenv()

# Optional URL prefix, e.g. URL_PREFIX=/aaa/bbb serves every route under
# http://host:port/aaa/bbb/... main.py writes the same value into
# frontend/src/url-prefix.js so the built SPA calls the prefixed API.
URL_PREFIX = os.environ.get("URL_PREFIX", "").strip().rstrip("/")
if URL_PREFIX and not URL_PREFIX.startswith("/"):
    URL_PREFIX = f"/{URL_PREFIX}"

HEARTBEAT_INTERVAL_S = 15.0

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
    app.state.workers = Workers()
    # Warm MinerU FastAPI service with the VLM preloaded, so PDF parsing
    # through vlm/hybrid backends skips the multi-minute vLLM cold start.
    # Runs in its own process group; killed when this server shuts down.
    mineru_api = MinerUApiService()
    app.state.mineru_api = mineru_api
    mineru_api.start()
    logger.info("workers started: external executor + GPU executor + network limiter")
    yield
    app.state.workers.shutdown()
    mineru_api.stop()


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
        elif path == "/health":
            pass  # Keep an unprefixed health check available to monitors.
        else:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await call_next(request)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/workers")
async def workers(request: Request) -> dict:
    w: Workers = request.app.state.workers
    return w.stats()


@app.post("/parse")
async def parse(
    request: Request,
    file: Annotated[UploadFile, File()],
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
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    options = ParseOptions(
        images=images,
        describe_images=describe_images,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        filename=file.filename,
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
