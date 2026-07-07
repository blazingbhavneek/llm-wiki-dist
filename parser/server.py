# start this server using
# uvicorn server:app --host 0.0.0.0 --port 8888

import atexit
import hashlib
import multiprocessing as mp
import os
import queue as queue_module
import shutil
import signal
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from contextlib import asynccontextmanager
from html import escape as html_escape
from pathlib import Path
from typing import Any, Optional

import image
import pdf
import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.requests import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

CACHE_DIR = Path("./cache")
TEMP_DIR = Path("./temp")
CACHE_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)

MINERU_VENV_BIN = (
    Path(os.environ["MINERU_VENV_BIN"]) if os.environ.get("MINERU_VENV_BIN") else None
)
PUPPETEER_CONFIG_PATH = os.environ.get(
    "PUPPETEER_CONFIG_PATH", "./puppeteer-config.json"
)
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))
CACHE_RETENTION_HOURS = float(os.environ.get("CACHE_RETENTION_HOURS", "720"))
CACHE_RETENTION_SECONDS = CACHE_RETENTION_HOURS * 60 * 60

GPU_MEMORY_UTILIZATION = "0.05"
INVOKE_URL = os.environ.get("OPENAI_BASE_URL", "http://10.160.144.101:51029/v1")
API_KEY = os.environ.get("OPENAI_API_KEY", "local")
MODEL = os.environ.get("WIKI_MODEL", "gemma-4-31B")

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
}

DONE_RETENTION_SECONDS = 72 * 60 * 60

MP_CONTEXT = mp.get_context("spawn")

tasks: dict[str, dict[str, Any]] = {}

task_queue: deque[str] = deque()
current_task_id: Optional[str] = None
state_lock = threading.RLock()
scheduler_stop = threading.Event()
scheduler_thread: Optional[threading.Thread] = None
shutdown_started = False


def api_error(detail: str, retryable: bool, code: str) -> dict[str, Any]:
    return {"detail": detail, "retryable": retryable, "code": code}


class APIConfig(BaseModel):
    base_url: str = INVOKE_URL
    api_key: str = API_KEY
    model: str = MODEL
    puppeteer_config_path: Optional[str] = PUPPETEER_CONFIG_PATH


def now_ts() -> float:
    return time.time()


def cleanup_stale_processing_dirs():
    for cache_path in CACHE_DIR.iterdir():
        if not cache_path.is_dir():
            continue

        marker = cache_path / ".processing"

        if marker.exists():
            print(f"[STARTUP] Removing stale processing cache: {cache_path}")
            shutil.rmtree(cache_path, ignore_errors=True)


def kill_process_forcefully(process: mp.Process, timeout: float = 5.0):
    if process is None or process.pid is None:
        return

    pid = process.pid

    if not process.is_alive():
        process.join(timeout=0.2)
        return

    print(f"[SHUTDOWN] Terminating process PID={pid}")

    try:
        if os.name == "posix":
            try:
                pgid = os.getpgid(pid)

                # Safety: if the child has not called os.setsid() yet,
                # its pgid may still be the same as the parent server.
                # In that case, do NOT kill the whole process group.
                if pgid != os.getpgrp():
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    os.kill(pid, signal.SIGTERM)

            except ProcessLookupError:
                return
        else:
            process.terminate()

    except Exception as e:
        print(f"[WARN] SIGTERM failed for PID={pid}: {e}")

    start = time.time()

    while time.time() - start < timeout:
        if not process.is_alive():
            process.join(timeout=0.2)
            return
        time.sleep(0.1)

    if process.is_alive():
        print(f"[SHUTDOWN] Force killing process PID={pid}")

        try:
            if os.name == "posix":
                try:
                    pgid = os.getpgid(pid)

                    if pgid != os.getpgrp():
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)

                except ProcessLookupError:
                    pass
            else:
                process.kill()

        except Exception as e:
            print(f"[WARN] SIGKILL failed for PID={pid}: {e}")

    process.join(timeout=1.0)


def remove_from_queue(task_id: str):
    remaining = deque(x for x in task_queue if x != task_id)
    task_queue.clear()
    task_queue.extend(remaining)


def prune_old_done_tasks():
    cutoff = now_ts() - DONE_RETENTION_SECONDS
    cache_cutoff = now_ts() - CACHE_RETENTION_SECONDS

    for task_id, meta in list(tasks.items()):
        status = meta.get("status")

        if status not in {"completed", "failed"}:
            continue

        done_at = (
            meta.get("finished_at") or meta.get("done_at") or meta.get("created_at")
        )

        if done_at and done_at < cutoff:
            tasks.pop(task_id, None)

    failed_logs = CACHE_DIR / "_failed_logs"
    if failed_logs.exists():
        for log_file in failed_logs.iterdir():
            try:
                if log_file.is_file() and log_file.stat().st_mtime < cutoff:
                    log_file.unlink(missing_ok=True)
            except OSError:
                pass

    for cache_path in list(CACHE_DIR.iterdir()):
        if not cache_path.is_dir() or cache_path.name == "_failed_logs":
            continue
        if (cache_path / ".processing").exists():
            continue
        final_md = cache_path / "final.md"
        try:
            if final_md.exists() and final_md.stat().st_mtime < cache_cutoff:
                shutil.rmtree(cache_path, ignore_errors=True)
        except OSError:
            pass


def shutdown_all_tasks():
    global current_task_id
    global shutdown_started

    with state_lock:
        if shutdown_started:
            return

        shutdown_started = True
        scheduler_stop.set()

        for task_id, meta in list(tasks.items()):
            status = meta.get("status")

            if status not in {"queued", "processing"}:
                continue

            process: Optional[mp.Process] = meta.get("process")
            cache_path = Path(meta["cache_path"])
            task_dir = Path(meta["task_dir"])

            if process and process.is_alive():
                kill_process_forcefully(process)

            print(f"[SHUTDOWN] Removing partial output for task {task_id}")
            shutil.rmtree(cache_path, ignore_errors=True)
            shutil.rmtree(task_dir, ignore_errors=True)

            meta["status"] = "failed"
            meta["error"] = "Server stopped while task was not complete"
            meta["finished_at"] = now_ts()
            meta["done_at"] = now_ts()

        task_queue.clear()
        current_task_id = None


def scheduler_loop():
    while not scheduler_stop.is_set():
        try:
            with state_lock:
                if current_task_id:
                    refresh_task_status(current_task_id)

                start_next_task_if_possible()
                prune_old_done_tasks()
        except Exception:
            print("[SCHEDULER] tick failed:\n" + traceback.format_exc())

        time.sleep(0.5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler_thread

    cleanup_stale_processing_dirs()

    scheduler_stop.clear()
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()

    try:
        yield
    finally:
        shutdown_all_tasks()

        if scheduler_thread:
            scheduler_thread.join(timeout=2.0)


atexit.register(shutdown_all_tasks)


app = FastAPI(
    title="PDF to Markdown Processor",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "WIKI_CORS_ORIGINS", "http://localhost:5173"
    ).split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    print(
        f"[ERROR] unhandled error on {request.method} {request.url.path}:\n"
        + traceback.format_exc()
    )
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


def compute_pdf_hash(file_path: str) -> str:
    sha256 = hashlib.sha256()

    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)

    return sha256.hexdigest()


def find_single_markdown_file(mineru_dir: Path) -> Path:
    md_files = sorted(
        p for p in mineru_dir.rglob("*.md") if not p.name.endswith(".described.md")
    )

    if not md_files:
        raise RuntimeError("No Markdown file generated by PDF stage")

    return md_files[0]


def find_smallest_image(mineru_dir: Path) -> Optional[Path]:
    images = []

    for p in mineru_dir.rglob("*"):
        if not p.is_file():
            continue

        if p.suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        try:
            if p.stat().st_size <= 0:
                continue
        except OSError:
            continue

        images.append(p)

    if not images:
        return None

    return min(images, key=lambda p: p.stat().st_size)


def vision_smoke_test(api_config: dict, image_path: Path) -> bool:
    try:
        data_url = image.image_file_to_data_url(str(image_path))
        response = requests.post(
            image.normalize_invoke_url(api_config["base_url"]),
            headers={
                "Authorization": f"Bearer {api_config['api_key']}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={
                "model": api_config["model"],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Reply OK.",
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": data_url,
                                },
                            },
                        ],
                    }
                ],
                "max_tokens": 8,
                "temperature": 0.0,
                "top_p": 0.95,
                "stream": False,
                "chat_template_kwargs": {"enable_thinking": True},
            },
            timeout=30.0,
        )
        response.raise_for_status()

        _ = image.get_response_text(response.json())
        return True

    except Exception as e:
        print(f"[WARN] Vision smoke test failed: {e}")
        return False


def build_task_id(pdf_hash: str, describe_images: bool, generate_mermaid: bool) -> str:
    if not describe_images and not generate_mermaid:
        return pdf_hash

    options = (
        f"{pdf_hash}:describe_images={int(describe_images)}:"
        f"generate_mermaid={int(generate_mermaid)}"
    )
    return hashlib.sha256(options.encode("utf-8")).hexdigest()


def build_empty_image_unit(data_url: str, alt: str) -> str:
    safe_src = html_escape(data_url, quote=True)
    safe_alt = html_escape(alt or "", quote=True)

    return (
        "\x3cimage-unit\x3e\n"
        "  \x3cimage-media\x3e\n"
        f'    \x3cimg src="{safe_src}" alt="{safe_alt}"\x3e\n'
        "  \x3c/image-media\x3e\n"
        "  \x3cimage-description\x3e\n"
        "  \x3c/image-description\x3e\n"
        "\x3c/image-unit\x3e"
    )


def fallback_embed_images(markdown_file: Path, final_md_path: Path):
    with open(markdown_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    image_line_indices = set(image.find_image_line_indices(lines))
    output_lines = []

    for i, line in enumerate(lines):
        if i not in image_line_indices:
            output_lines.append(line)
            continue

        has_newline = line.endswith("\n")
        line_body = line[:-1] if has_newline else line

        def replace_match(match):
            alt = match.group("alt")
            target = match.group("target")

            try:
                resolved = image.resolve_image_path(markdown_file, target)

                if image.is_remote_url(resolved):
                    return match.group(0)

                resolved_path = Path(resolved)

                if not resolved_path.exists() or not resolved_path.is_file():
                    return match.group(0)

                data_url = image.image_file_to_data_url(str(resolved_path))
                return build_empty_image_unit(data_url, alt)

            except Exception as e:
                print(f"[WARN] Failed to embed image {target}: {e}")
                return match.group(0)

        new_line = image.IMAGE_MARKDOWN_RE.sub(replace_match, line_body)

        if has_newline:
            new_line += "\n"

        output_lines.append(new_line)

    with open(final_md_path, "w", encoding="utf-8") as f:
        f.writelines(output_lines)


def redirect_worker_output(cache_dir: str, task_id: str):
    """
    Prevent worker/subprocess logs from continuing to print into the terminal.
    Logs go to: cache/{task_id}/worker.log
    """
    try:
        log_path = Path(cache_dir) / "worker.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        log_file = open(log_path, "a", encoding="utf-8", buffering=1)

        os.dup2(log_file.fileno(), sys.stdout.fileno())
        os.dup2(log_file.fileno(), sys.stderr.fileno())

        print(f"[WORKER] Logging redirected for task {task_id}", flush=True)
    except Exception:
        pass


def worker_process(task_id: str, pdf_path: str, api_config: dict, cache_dir: str):
    import asyncio

    if os.name == "posix":
        os.setsid()

    try:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    except Exception:
        pass

    redirect_worker_output(cache_dir, task_id)

    if MINERU_VENV_BIN and MINERU_VENV_BIN.exists():
        os.environ["PATH"] = (
            f"{MINERU_VENV_BIN}{os.pathsep}{os.environ.get('PATH', '')}"
        )
        os.environ["VIRTUAL_ENV"] = str(MINERU_VENV_BIN.parent)

    cache_path = Path(cache_dir)
    mineru_dir = cache_path / "mineru"
    mineru_dir.mkdir(parents=True, exist_ok=True)

    processing_marker = cache_path / ".processing"
    processing_marker.write_text("processing", encoding="utf-8")

    final_md_path = cache_path / "final.md"
    temp_pdf_dir = Path(pdf_path).parent

    try:
        pdf.PDF_DIR = temp_pdf_dir
        pdf.OUTPUT_DIR = mineru_dir
        pdf.GPU_MEMORY_UTILIZATION = GPU_MEMORY_UTILIZATION

        expected_output_folder = mineru_dir / Path(pdf_path).stem

        if not expected_output_folder.exists():
            exit_code = pdf.main()
            if exit_code != 0:
                raise RuntimeError(f"pdf.main() exited with code {exit_code}")

        markdown_file = find_single_markdown_file(mineru_dir)
        smallest_image = find_smallest_image(mineru_dir)

        if smallest_image is None:
            print(f"[INFO] Task {task_id}: no images found")
            shutil.copyfile(markdown_file, final_md_path)
            processing_marker.unlink(missing_ok=True)
            return str(final_md_path)

        print(
            f"[INFO] Task {task_id}: smallest image is {smallest_image} "
            f"({smallest_image.stat().st_size} bytes)"
        )

        if not api_config.get("describe_images"):
            fallback_embed_images(markdown_file, final_md_path)
            processing_marker.unlink(missing_ok=True)
            return str(final_md_path)

        vision_ok = vision_smoke_test(api_config, smallest_image)

        if vision_ok:
            try:
                generate_mermaid = bool(api_config.get("generate_mermaid"))

                image.MARKDOWN_FOLDER = str(mineru_dir)
                image.OPENAI_BASE_URL = api_config["base_url"]
                image.OPENAI_API_KEY = api_config["api_key"]
                image.OPENAI_MODEL = api_config["model"]
                image.MERMAID_PUPPETEER_CONFIG_FILE = api_config.get(
                    "puppeteer_config_path",
                    PUPPETEER_CONFIG_PATH,
                )

                image.CONCURRENCY = 1
                image.FILE_CONCURRENCY = 1
                image.ENABLE_MERMAID_DIAGRAMS = generate_mermaid
                image.VALIDATE_MERMAID = generate_mermaid
                image.ENABLE_MERMAID_VISUAL_MATCH_LOOP = generate_mermaid

                asyncio.run(image.process_markdown_folder())

                described_md = markdown_file.with_name(
                    f"{markdown_file.stem}.described.md"
                )

                if not described_md.exists():
                    raise RuntimeError(
                        f"image.py completed but described file was not found: {described_md}"
                    )

                shutil.copyfile(described_md, final_md_path)
                processing_marker.unlink(missing_ok=True)
                return str(final_md_path)

            except Exception as e:
                print(f"[WARN] image.py failed, fallback enabled: {e}")

        fallback_embed_images(markdown_file, final_md_path)
        processing_marker.unlink(missing_ok=True)
        return str(final_md_path)

    finally:
        shutil.rmtree(temp_pdf_dir, ignore_errors=True)


def worker_entry(
    task_id: str,
    pdf_path: str,
    api_config: dict,
    cache_dir: str,
    result_queue,
):
    try:
        result_path = worker_process(task_id, pdf_path, api_config, cache_dir)

        result_queue.put(
            {
                "ok": True,
                "result_path": result_path,
            }
        )

    except BaseException as e:
        cache_path = Path(cache_dir)

        print(f"[ERROR] Task {task_id} failed: {e}")
        print(traceback.format_exc())

        log_src = cache_path / "worker.log"
        if log_src.exists():
            try:
                failed_logs = CACHE_DIR / "_failed_logs"
                failed_logs.mkdir(exist_ok=True)
                shutil.copyfile(log_src, failed_logs / f"{task_id}.log")
            except Exception:
                pass

        shutil.rmtree(cache_path, ignore_errors=True)

        try:
            result_queue.put(
                {
                    "ok": False,
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                }
            )
        except Exception:
            pass


def get_queue_position(task_id: str) -> Optional[int]:
    for i, queued_task_id in enumerate(task_queue):
        if queued_task_id == task_id:
            return i + 1

    return None


def public_task(task_id: str, meta: dict[str, Any], position: Optional[int] = None):
    status = meta.get("status")

    item = {
        "task_id": task_id,
        "filename": meta.get("filename"),
        "status": status,
        "queue_position": (
            position if position is not None else get_queue_position(task_id)
        ),
        "position": position,
        "created_at": meta.get("created_at"),
        "started_at": meta.get("started_at"),
        "finished_at": meta.get("finished_at"),
    }

    process = meta.get("process")

    if process is not None:
        item["pid"] = process.pid

    if meta.get("error"):
        item["error"] = meta["error"]

    if status == "failed":
        item["retryable"] = True

    if status == "completed":
        item["result_url"] = f"/result/{task_id}"

    return item


def start_next_task_if_possible():
    global current_task_id

    if current_task_id is not None:
        current_meta = tasks.get(current_task_id)

        if current_meta and current_meta.get("status") == "processing":
            return

        current_task_id = None

    while task_queue:
        next_task_id = task_queue.popleft()
        meta = tasks.get(next_task_id)

        if not meta:
            continue

        if meta.get("status") != "queued":
            continue

        process: mp.Process = meta["process"]

        try:
            process.start()
            meta["status"] = "processing"
            meta["started_at"] = now_ts()
            current_task_id = next_task_id

            print(
                f"[QUEUE] Started task {next_task_id}: "
                f"{meta.get('filename')} PID={process.pid}"
            )

        except Exception as e:
            meta["status"] = "failed"
            meta["error"] = f"Failed to start worker: {e}"
            meta["finished_at"] = now_ts()
            meta["done_at"] = now_ts()
            current_task_id = None
            print(f"[ERROR] Failed to start task {next_task_id}: {e}")

        return


def refresh_task_status(task_id: str):
    global current_task_id

    with state_lock:
        if task_id not in tasks:
            return None

        meta = tasks[task_id]
        process: Optional[mp.Process] = meta.get("process")
        result_queue = meta.get("queue")
        final_md_path = Path(meta["cache_path"]) / "final.md"

        if meta.get("status") == "queued":
            return meta

        if result_queue is not None:
            while True:
                try:
                    msg = result_queue.get_nowait()
                except (queue_module.Empty, EOFError, OSError):
                    break

                meta["last_message"] = msg

                if msg.get("ok"):
                    meta["status"] = "completed"
                    meta["finished_at"] = now_ts()
                    meta["done_at"] = now_ts()
                else:
                    meta["status"] = "failed"
                    meta["error"] = msg.get("error", "Unknown error")
                    meta["finished_at"] = now_ts()
                    meta["done_at"] = now_ts()

        if final_md_path.exists():
            if meta.get("status") != "completed":
                meta["status"] = "completed"
                meta["finished_at"] = now_ts()
                meta["done_at"] = now_ts()

        elif process and process.is_alive():
            meta["status"] = "processing"
            return meta

        elif process and process.exitcode is not None:
            if meta.get("status") not in {"completed", "failed"}:
                meta["status"] = "failed"
                meta["error"] = f"Worker exited with code {process.exitcode}"
                meta["finished_at"] = now_ts()
                meta["done_at"] = now_ts()

        elif process is None and meta.get("status") not in {"completed", "failed"}:
            meta["status"] = "failed"
            meta["error"] = "Task has no worker process"
            meta["finished_at"] = now_ts()
            meta["done_at"] = now_ts()

        if meta.get("status") in {"completed", "failed"}:
            if process:
                try:
                    process.join(timeout=0.2)
                except Exception:
                    pass

            if current_task_id == task_id:
                current_task_id = None
                start_next_task_if_possible()

        return meta


@app.post("/upload")
async def upload_pdf(
    file: UploadFile = File(...),
    base_url: str = Form(INVOKE_URL),
    api_key: str = Form(API_KEY),
    model: str = Form(MODEL),
    describe_images: bool = Form(False),
    generate_mermaid: bool = Form(False),
    puppeteer_config_path: Optional[str] = Form(PUPPETEER_CONFIG_PATH),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail=api_error("Only PDF files are supported", False, "bad_upload"),
        )

    pending_dir = TEMP_DIR / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    safe_filename = Path(file.filename).name
    pending_pdf_path = pending_dir / f"{uuid.uuid4().hex}_{safe_filename}"

    sha256 = hashlib.sha256()
    total_bytes = 0

    with open(pending_pdf_path, "wb") as f:
        while chunk := await file.read(8192):
            total_bytes += len(chunk)
            if total_bytes > MAX_UPLOAD_BYTES:
                f.close()
                pending_pdf_path.unlink(missing_ok=True)
                limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
                raise HTTPException(
                    status_code=413,
                    detail=api_error(
                        f"PDF too large (limit {limit_mb}MB)", False, "bad_upload"
                    ),
                )
            f.write(chunk)
            sha256.update(chunk)

    pdf_hash = sha256.hexdigest()
    generate_mermaid = bool(describe_images and generate_mermaid)
    task_id = build_task_id(pdf_hash, describe_images, generate_mermaid)

    cache_path = CACHE_DIR / task_id
    final_md_path = cache_path / "final.md"

    if final_md_path.exists():
        pending_pdf_path.unlink(missing_ok=True)

        with state_lock:
            ts = now_ts()

            tasks[task_id] = {
                "process": None,
                "queue": None,
                "status": "completed",
                "filename": safe_filename,
                "cache_path": str(cache_path),
                "task_dir": str(TEMP_DIR / task_id),
                "error": None,
                "created_at": ts,
                "started_at": None,
                "finished_at": ts,
                "done_at": ts,
            }

            prune_old_done_tasks()

        return {
            "task_id": task_id,
            "filename": safe_filename,
            "status": "completed",
            "message": "Returned from cache",
            "result_url": f"/result/{task_id}",
            "queue_position": None,
        }

    with state_lock:
        prune_old_done_tasks()

        if task_id in tasks:
            meta = refresh_task_status(task_id)

            if meta and meta.get("status") in {"queued", "processing"}:
                pending_pdf_path.unlink(missing_ok=True)

                return {
                    "task_id": task_id,
                    "filename": meta.get("filename"),
                    "status": meta["status"],
                    "message": "Task already running",
                    "queue_position": get_queue_position(task_id),
                }

            if meta and meta.get("status") == "completed":
                pending_pdf_path.unlink(missing_ok=True)

                return {
                    "task_id": task_id,
                    "filename": meta.get("filename") or safe_filename,
                    "status": "completed",
                    "message": "Task already completed",
                    "result_url": f"/result/{task_id}",
                    "queue_position": None,
                }

            # Failed or stale task with same hash. Replace it.
            old_meta = tasks.pop(task_id, None)
            remove_from_queue(task_id)

            if old_meta:
                shutil.rmtree(old_meta.get("task_dir", ""), ignore_errors=True)

        task_dir = TEMP_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        final_temp_pdf = task_dir / safe_filename
        shutil.move(str(pending_pdf_path), str(final_temp_pdf))

        cache_path.mkdir(parents=True, exist_ok=True)

        api_config = {
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "describe_images": describe_images,
            "generate_mermaid": generate_mermaid,
            "puppeteer_config_path": puppeteer_config_path,
        }

        result_queue = MP_CONTEXT.Queue()

        process = MP_CONTEXT.Process(
            target=worker_entry,
            args=(
                task_id,
                str(final_temp_pdf),
                api_config,
                str(cache_path),
                result_queue,
            ),
            daemon=False,
        )

        tasks[task_id] = {
            "process": process,
            "queue": result_queue,
            "status": "queued",
            "filename": safe_filename,
            "cache_path": str(cache_path),
            "task_dir": str(task_dir),
            "error": None,
            "created_at": now_ts(),
            "started_at": None,
            "finished_at": None,
            "done_at": None,
        }

        task_queue.append(task_id)

        start_next_task_if_possible()

        meta = tasks[task_id]

        return {
            "task_id": task_id,
            "filename": safe_filename,
            "status": meta["status"],
            "message": (
                "Task submitted and started"
                if meta["status"] == "processing"
                else "Task submitted to queue"
            ),
            "queue_position": get_queue_position(task_id),
        }


@app.get("/status/{task_id}")
async def get_status(task_id: str):
    final_md_path = CACHE_DIR / task_id / "final.md"

    with state_lock:
        prune_old_done_tasks()

        if task_id not in tasks:
            if final_md_path.exists():
                return {
                    "task_id": task_id,
                    "filename": None,
                    "status": "completed",
                    "message": "Available in cache",
                    "result_url": f"/result/{task_id}",
                    "queue_position": None,
                }

            raise HTTPException(
                status_code=404,
                detail=api_error("Task not found", True, "not_ready"),
            )

        meta = refresh_task_status(task_id)

        response = public_task(task_id, meta)

        if meta["status"] == "completed":
            response["result_url"] = f"/result/{task_id}"

        return response


@app.get("/queue")
async def get_queue():
    with state_lock:
        if current_task_id:
            refresh_task_status(current_task_id)

        prune_old_done_tasks()

        processing = None

        if current_task_id and current_task_id in tasks:
            meta = tasks[current_task_id]

            if meta.get("status") == "processing":
                processing = public_task(current_task_id, meta)

        queued = []

        for i, task_id in enumerate(task_queue):
            meta = tasks.get(task_id)

            if not meta:
                continue

            if meta.get("status") != "queued":
                continue

            queued.append(public_task(task_id, meta, position=i + 1))

        completed = []
        failed = []

        for task_id, meta in tasks.items():
            status = meta.get("status")

            if status == "completed":
                completed.append(public_task(task_id, meta))
            elif status == "failed":
                failed.append(public_task(task_id, meta))

        completed.sort(key=lambda x: x.get("finished_at") or 0, reverse=True)
        failed.sort(key=lambda x: x.get("finished_at") or 0, reverse=True)

        return {
            "retention_hours": DONE_RETENTION_SECONDS / 3600,
            "processing": processing,
            "queued_count": len(queued),
            "queued": queued,
            "completed_count": len(completed),
            "completed": completed,
            "failed_count": len(failed),
            "failed": failed,
        }


@app.delete("/queue/{task_id}")
async def delete_queue_item(task_id: str):
    global current_task_id

    with state_lock:
        meta = tasks.get(task_id)

        if not meta:
            remove_from_queue(task_id)

            return {
                "task_id": task_id,
                "deleted": True,
                "message": "Task was not in queue history",
            }

        status = meta.get("status")
        process: Optional[mp.Process] = meta.get("process")

        remove_from_queue(task_id)

        if status == "processing":
            if process and process.is_alive():
                kill_process_forcefully(process)

            if current_task_id == task_id:
                current_task_id = None

            shutil.rmtree(meta.get("cache_path", ""), ignore_errors=True)
            shutil.rmtree(meta.get("task_dir", ""), ignore_errors=True)

        elif status == "queued":
            shutil.rmtree(meta.get("cache_path", ""), ignore_errors=True)
            shutil.rmtree(meta.get("task_dir", ""), ignore_errors=True)

        elif status == "failed":
            shutil.rmtree(meta.get("cache_path", ""), ignore_errors=True)
            shutil.rmtree(meta.get("task_dir", ""), ignore_errors=True)

        elif status == "completed":
            # Delete from queue/history only.
            # Keep cache/{task_id}/final.md so /result/{task_id} can still work.
            shutil.rmtree(meta.get("task_dir", ""), ignore_errors=True)

        tasks.pop(task_id, None)

        start_next_task_if_possible()

        return {
            "task_id": task_id,
            "deleted": True,
            "status": status,
        }


@app.get("/result/{task_id}")
async def get_result(task_id: str):
    final_md_path = CACHE_DIR / task_id / "final.md"

    if not final_md_path.exists():
        with state_lock:
            if task_id in tasks:
                meta = refresh_task_status(task_id)

                if meta.get("status") == "failed":
                    raise HTTPException(
                        status_code=500,
                        detail=api_error(
                            meta.get("error", "Task failed"), True, "worker_failed"
                        ),
                    )

        raise HTTPException(
            status_code=404,
            detail=api_error("Result not ready or not found", True, "not_ready"),
        )

    return FileResponse(
        final_md_path,
        media_type="text/markdown",
        filename=f"{task_id}.md",
    )
