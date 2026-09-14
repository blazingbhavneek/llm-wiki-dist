"""A warm, supervised ``mineru-api`` service for high-accuracy GPU parsing.

The ``vlm-engine``/``hybrid-engine`` backends pull in vLLM and take several
minutes to initialise on every CLI invocation. ``mineru-api`` solves this:
started once with ``--enable-vlm-preload true`` it keeps the vision model
resident, and the ``mineru`` CLI can parse through it with ``--api-url``.

This module owns that process for the lifetime of the API server:

* spawn it in its own session (process group) so nothing we do leaks into
  the caller's signal handling and vice versa;
* poll ``/health`` in the background so server startup is not blocked by
  the (intentionally slow) model preload;
* terminate the whole process group on ``stop()`` and via an ``atexit``
  hook, plus a parent-death signal on Linux, so orphaned vLLM workers can
  keep holding GPU memory after a hard kill only until the kernel notices.

If the service is unhealthy or has died, :attr:`url` returns ``None`` and
parsers transparently fall back to a cold in-process MinerU run.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger("doc-parser.mineru-api")

TRUTHY = {"1", "true", "yes", "on"}

# Load libc at import time (not in the child) so preexec_fn below cannot
# deadlock on a dlopen lock held by another thread across fork().
try:
    _LIBC = ctypes.CDLL(None)
except Exception:  # noqa: BLE001 - non-Linux or exotic libc
    _LIBC = None


def _parent_death_signal() -> None:
    """Ask the kernel to SIGTERM this child if our process dies (Linux)."""
    if _LIBC is not None:
        _LIBC.prctl(1, signal.SIGTERM, 0, 0, 0)  # PR_SET_PDEATHSIG


def service_enabled() -> bool:
    """``MINERU_API_SERVICE`` (default true) gates the warm service."""
    return os.getenv("MINERU_API_SERVICE", "true").strip().lower() in TRUTHY


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _RotatingLog:
    """Small binary log sink with a hard cap and numbered backups."""

    def __init__(self, path: Path, max_bytes: int, backup_count: int) -> None:
        self.path = path
        self.max_bytes = max(1, max_bytes)
        self.backup_count = max(0, backup_count)
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("ab", buffering=0)
        self._size = path.stat().st_size

    def write(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self._size and self._size + len(data) > self.max_bytes:
                self._rotate()
            self._file.write(data)
            self._size += len(data)

    def _rotate(self) -> None:
        self._file.close()
        if self.backup_count:
            for index in range(self.backup_count - 1, 0, -1):
                older = self.path.with_name(f"{self.path.name}.{index}")
                newer = self.path.with_name(f"{self.path.name}.{index + 1}")
                if older.exists():
                    older.replace(newer)
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        else:
            self.path.unlink(missing_ok=True)
        self._file = self.path.open("ab", buffering=0)
        self._size = 0

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.close()


class MinerUApiService:
    """Own one ``mineru-api`` process with its VLM preloaded."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._url: str | None = None
        self._ready = threading.Event()
        self._log_path: str | None = None
        self._log: _RotatingLog | None = None
        self._log_thread: threading.Thread | None = None
        self._stopping = False
        self._cleanup_hook = lambda: self.stop()

    # ------------------------------------------------------------------
    @property
    def url(self) -> str | None:
        """Base URL of the warm service, or None when it is not usable.

        Callers treat None as "cold-start MinerU in your own process".
        """
        process = self._process
        if process is None or process.poll() is not None:
            return None
        if not self._ready.is_set():
            return None
        return self._url

    def start(self) -> bool:
        """Spawn the service and start background readiness polling.

        Returns immediately: the VLM preload is deliberately slow and the
        HTTP server must accept requests long before the model is warm.
        """
        if not service_enabled():
            logger.info("mineru-api service disabled (MINERU_API_SERVICE)")
            return False

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return True

            self._stopping = False
            self._close_log()

            host = os.getenv("MINERU_API_HOST", "127.0.0.1").strip()
            port = int(os.getenv("MINERU_API_PORT", "0") or 0) or _free_port()
            command = shlex.split(os.getenv("MINERU_API_COMMAND", "mineru-api"))
            if not command:
                logger.error("MINERU_API_COMMAND is empty; warm service off")
                return False

            environment = os.environ.copy()
            # The service output is captured through a pipe; without this,
            # Python's stdout buffering can delay logs until shutdown.
            environment["PYTHONUNBUFFERED"] = "1"
            environment["CUDA_VISIBLE_DEVICES"] = os.getenv(
                "MINERU_CUDA_VISIBLE_DEVICES",
                "1",
            )
            environment["MINERU_PROCESSING_WINDOW_SIZE"] = os.getenv(
                "MINERU_PROCESSING_WINDOW_SIZE",
                "4",
            )
            bin_dir = self._discover_bin_dir(command[0])
            if bin_dir:
                environment["PATH"] = (
                    f"{bin_dir}{os.pathsep}{environment.get('PATH', '')}"
                )

            args = [
                *command,
                "--host", host,
                "--port", str(port),
                "--enable-vlm-preload", "true",
            ]
            # This is a vLLM model option.  Pass it to the API process too;
            # setting it only on the per-document CLI would leave the warm
            # preload at vLLM's default (usually 0.9), which can OOM a shared
            # GPU before the first request is handled.
            gpu_memory = os.getenv("MINERU_GPU_MEMORY_UTILIZATION", "").strip()
            if gpu_memory:
                args.extend(["--gpu-memory-utilization", gpu_memory])
            log_path = Path(
                os.getenv("MINERU_API_LOG_PATH", "logs/mineru-api.log").strip()
                or "logs/mineru-api.log"
            )
            try:
                max_bytes = int(os.getenv("MINERU_API_LOG_MAX_BYTES", "10485760"))
                backup_count = int(os.getenv("MINERU_API_LOG_BACKUP_COUNT", "3"))
                self._log = _RotatingLog(log_path, max_bytes, backup_count)
                self._log_path = str(log_path)
            except (OSError, ValueError) as exc:
                logger.error("cannot open MinerU log %s: %s", log_path, exc)
                self._log = None
                self._log_path = None
            try:
                self._process = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE if self._log is not None else subprocess.DEVNULL,
                    stderr=(
                        subprocess.STDOUT
                        if self._log is not None
                        else subprocess.DEVNULL
                    ),
                    env=environment,
                    start_new_session=True,
                    # Only async-signal-safe prctl on an already-loaded libc,
                    # so it is fork-safe even with the readiness thread alive.
                    preexec_fn=_parent_death_signal if _LIBC else None,
                )
            except FileNotFoundError:
                logger.error("mineru-api executable not found: %s", command[0])
                self._process = None
                self._close_log()
                return False

            if self._log is not None and self._process.stdout is not None:
                self._log_thread = threading.Thread(
                    target=self._capture_output,
                    args=(self._process.stdout, self._log),
                    name="mineru-api-log",
                    daemon=True,
                )
                self._log_thread.start()

            self._url = None
            self._ready.clear()
            atexit.register(self._cleanup_hook)

            threading.Thread(
                target=self._wait_until_ready,
                args=(self._process, f"http://{host}:{port}"),
                name="mineru-api-ready",
                daemon=True,
            ).start()
            logger.info(
                "mineru-api starting (pid=%s, port=%s); VLM preload will "
                "take several minutes (log=%s)",
                self._process.pid,
                port,
                self._log_path or "disabled",
            )
            return True

    def stop(self) -> None:
        """Terminate the service's whole process group. Idempotent."""
        with self._lock:
            self._stopping = True
            process, self._process = self._process, None
            url, self._url = self._url, None
            self._ready.clear()
        if url and os.environ.get("MINERU_API_URL") == url:
            # Parsers fall back to a cold in-process run after this.
            del os.environ["MINERU_API_URL"]
        try:
            atexit.unregister(self._cleanup_hook)
        except Exception:  # noqa: BLE001
            pass
        if process is None:
            self._close_log()
            return

        logger.info("stopping mineru-api (pid=%s)", process.pid)
        if process.poll() is not None:
            self._close_log()
            return
        try:
            group = os.getpgid(process.pid)
        except ProcessLookupError:
            return
        for signal_number, grace in ((signal.SIGTERM, 15), (signal.SIGKILL, 10)):
            try:
                os.killpg(group, signal_number)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue
        logger.warning("mineru-api pid=%s refused to die", process.pid)
        self._close_log()

    @staticmethod
    def _capture_output(stream, log: _RotatingLog) -> None:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                log.write(chunk)
        except (OSError, ValueError):
            return

    def _close_log(self) -> None:
        log, self._log = self._log, None
        thread, self._log_thread = self._log_thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        if log is not None:
            log.close()

    # ------------------------------------------------------------------
    def _wait_until_ready(self, process: subprocess.Popen, url: str) -> None:
        deadline = time.monotonic() + float(
            os.getenv("MINERU_API_STARTUP_TIMEOUT_SECONDS", "900")
        )
        while time.monotonic() < deadline:
            if process.poll() is not None:
                logger.error(
                    "mineru-api exited during preload (status=%s); parsers "
                    "will cold-start MinerU instead",
                    process.returncode,
                )
                self._restart_after_exit(process)
                return
            try:
                with urllib.request.urlopen(f"{url}/health", timeout=5) as probe:
                    if probe.status == 200:
                        with self._lock:
                            # Guard against a stop()/restart() race.
                            if self._process is process:
                                self._url = url
                                self._ready.set()
                        # Spawned GPU workers inherit os.environ at submit
                        # time; this is how run_mineru finds the warm service.
                        os.environ["MINERU_API_URL"] = url
                        logger.info("mineru-api warm at %s", url)
                        threading.Thread(
                            target=self._watch_process,
                            args=(process,),
                            name="mineru-api-watch",
                            daemon=True,
                        ).start()
                        return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(2.0)
        logger.error(
            "mineru-api did not become ready within the startup timeout; "
            "parsers will cold-start MinerU instead",
        )

    def _watch_process(self, process: subprocess.Popen) -> None:
        """Restart an unexpectedly dead service without restarting doc-parser."""
        while True:
            time.sleep(2.0)
            if process.poll() is None:
                continue
            self._restart_after_exit(process)
            return

    def _restart_after_exit(self, process: subprocess.Popen) -> None:
        with self._lock:
            if self._process is not process or self._stopping:
                return
            self._process = None
            self._url = None
            self._ready.clear()
        if os.environ.get("MINERU_API_URL", "").startswith("http://127.0.0.1:"):
            os.environ.pop("MINERU_API_URL", None)
        self._close_log()
        logger.warning("mineru-api exited unexpectedly; restarting it")
        self.start()

    @staticmethod
    def _discover_bin_dir(executable: str) -> str | None:
        explicit = os.getenv("MINERU_VENV_BIN", "").strip()
        if explicit:
            return explicit
        found = shutil.which(executable)
        if found:
            return str(Path(found).parent)

        # uv-created environments are intentionally not put on the shell PATH
        # in many service managers.  Mirror the CLI discovery fallback so the
        # supervisor can still find mineru-api when launched as `uvicorn ...`.
        project_root = Path(__file__).resolve().parent.parent
        candidates = [
            *(project_root / name / "bin"
              for name in (".venv", "venv", "env", ".env")),
            Path(sys.executable).parent,
        ]
        exe_name = executable + (".exe" if os.name == "nt" else "")
        for bin_dir in candidates:
            if (bin_dir / exe_name).is_file():
                return str(bin_dir)
        return None
