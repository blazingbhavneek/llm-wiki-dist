"""PDF parsing through MinerU with parallel image descriptions."""

from __future__ import annotations

import ctypes
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from client.llm import LLMClient
from formats.base import BaseParser, ParseOptions
from utils.markdown_images import embed_markdown_images
from workers import Workers

_PDF_HEADER = b"%PDF-"

try:
    _LIBC = ctypes.CDLL(None)
except Exception:  # noqa: BLE001 - non-Linux or exotic libc
    _LIBC = None


def _parent_death_signal() -> None:
    """Terminate the MinerU child if its GPU worker is forcefully killed."""
    if _LIBC is not None:
        _LIBC.prctl(1, signal.SIGTERM, 0, 0, 0)


class MineruError(RuntimeError):
    """MinerU did not produce a usable Markdown document."""


def _find_markdown(output_dir: Path, input_stem: str) -> Path:
    candidates = sorted(output_dir.rglob("*.md"))
    if not candidates:
        raise MineruError("MinerU completed without producing a Markdown file")

    preferred = [path for path in candidates if path.stem == input_stem]
    if len(preferred) == 1:
        return preferred[0]
    if len(candidates) == 1:
        return candidates[0]

    relative = ", ".join(str(path.relative_to(output_dir)) for path in candidates[:8])
    raise MineruError(f"MinerU produced multiple Markdown files: {relative}")


def discover_mineru_bin(command: list[str]) -> str | None:
    """Return a ``bin`` directory to prepend to PATH so ``command[0]`` resolves.

    Order: explicit MINERU_VENV_BIN, the current PATH, venvs in the project
    root (``.venv``, ``venv``, ...), this interpreter's own bin, then common
    venv/conda roots under the user's home and /opt. Returns None when nothing
    is found, which leaves PATH untouched so the original error message still
    applies.
    """
    explicit = os.getenv("MINERU_VENV_BIN", "").strip()
    if explicit:
        return explicit

    executable = shutil.which(command[0])
    if executable:
        return str(Path(executable).parent)

    exe_name = command[0] + (".exe" if os.name == "nt" else "")
    # This package lives in <project root>/formats, so parent.parent is the root.
    project_root = Path(__file__).resolve().parent.parent
    candidates = [
        *(project_root / name / "bin"
          for name in (".venv", "venv", "env", ".env")),
        Path(sys.executable).parent,
    ]
    home = Path.home()
    candidates.extend(
        bin_dir
        for bin_dir in (
            *sorted(home.glob("*venv*/bin")),
            *sorted(home.glob("*/.venv/bin")),
            *sorted(home.glob(".venvs/*/bin")),
            *sorted(home.glob("*conda*/envs/*/bin")),
            *sorted(Path("/opt").glob("*venv*/bin")),
        )
    )
    for bin_dir in candidates:
        if (bin_dir / exe_name).is_file():
            return str(bin_dir)
    return None


def run_mineru(pdf_path: str, output_dir: str) -> str:
    """Run MinerU in a GPU worker and return its generated Markdown path.

    This function is module-level because spawned process workers require a
    picklable callable. MinerU itself is invoked without a shell.
    """
    source = Path(pdf_path)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    command = shlex.split(os.getenv("MINERU_COMMAND", "mineru"))
    if not command:
        raise MineruError("MINERU_COMMAND is empty")

    args = [*command, "-p", str(source), "-o", str(destination)]

    # ``pipeline`` is the general-purpose backend; it cold-loads in ~1 minute and
    # ignores --gpu-memory-utilization. The vlm/hybrid backends default otherwise
    # and pull in vLLM, whose init on a shared GPU takes several minutes.
    backend = os.getenv("MINERU_BACKEND", "pipeline").strip()
    if backend:
        args.extend(["-b", backend])

    # A warm mineru-api service (started by the server lifespan with the VLM
    # preloaded) avoids the per-request vLLM cold start. When it is not up,
    # MINERU_API_URL is absent and the CLI cold-starts locally as before.
    api_url = os.getenv("MINERU_API_URL", "").strip()
    if api_url:
        args.extend(["--api-url", api_url])

    # --gpu-memory-utilization is a vLLM knob: only meaningful for vlm/hybrid.
    gpu_memory = os.getenv("MINERU_GPU_MEMORY_UTILIZATION", "").strip()
    if gpu_memory and backend not in {"", "pipeline"}:
        args.extend(["--gpu-memory-utilization", gpu_memory])

    extra_args = os.getenv("MINERU_EXTRA_ARGS", "").strip()
    if extra_args:
        args.extend(shlex.split(extra_args))

    environment = os.environ.copy()
    environment["CUDA_VISIBLE_DEVICES"] = os.getenv(
        "MINERU_CUDA_VISIBLE_DEVICES",
        "1",
    )
    environment["MINERU_PROCESSING_WINDOW_SIZE"] = os.getenv(
        "MINERU_PROCESSING_WINDOW_SIZE",
        "4",
    )
    mineru_bin = discover_mineru_bin(command)
    if mineru_bin:
        environment["PATH"] = (
            f"{mineru_bin}{os.pathsep}{environment.get('PATH', '')}"
        )

    timeout_s = float(os.getenv("MINERU_TIMEOUT_SECONDS", "1800"))
    try:
        # Own session so a kill on timeout takes the whole process group:
        # without an API URL the CLI starts a temporary local mineru-api
        # whose vLLM children would otherwise survive and hold GPU memory.
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=True,
            preexec_fn=_parent_death_signal if _LIBC else None,
        )
    except FileNotFoundError as exc:
        raise MineruError(f"MinerU executable was not found: {command[0]}") from exc

    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(process)
        process.communicate()
        raise MineruError(f"MinerU timed out after {timeout_s:g} seconds") from exc

    if process.returncode:
        details = ((stderr or "") or (stdout or "")).strip()[-4000:]
        raise MineruError(
            f"MinerU exited with status {process.returncode}: {details}"
        )

    return str(_find_markdown(destination, source.stem))


def _kill_process_group(process: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the CLI's whole process group."""
    try:
        group = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


class PdfParser(BaseParser):
    name = "pdf"
    stream_response = True

    @classmethod
    def detect(cls, data: bytes) -> bool:
        return _PDF_HEADER in data[:1024]

    async def _extract(
        self,
        data: bytes,
        image_dir: str,
        options: ParseOptions,
        workers: Workers,
    ) -> str:
        work_dir = Path(image_dir)
        pdf_path = work_dir / "document.pdf"
        output_dir = work_dir / "mineru-output"
        pdf_path.write_bytes(data)

        markdown_path = Path(
            await workers.run_gpu(
                run_mineru,
                str(pdf_path),
                str(output_dir),
            )
        )
        markdown = markdown_path.read_text(encoding="utf-8")
        client = (
            LLMClient(
                base_url=options.llm_base_url,
                api_key=options.llm_api_key,
                model=options.llm_model,
            )
            if options.describe_images
            else None
        )
        try:
            return await embed_markdown_images(
                markdown,
                markdown_path,
                output_dir,
                workers,
                client.describe_image if client is not None else None,
            )
        finally:
            if client is not None:
                await client.close()
