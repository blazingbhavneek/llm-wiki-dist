"""Bounded, spawn-based process executor for GPU work."""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from workers.limits import CapacityLimiter

logger = logging.getLogger(__name__)


class GpuExecutor:
    """Run GPU jobs outside the API process.

    The ``spawn`` start method avoids inheriting an unsafe CUDA context.
    By default one process owns one GPU. Functions and arguments submitted
    here must be picklable.
    """

    def __init__(
        self,
        max_workers: int = 1,
        *,
        initializer=None,
        initargs: tuple = (),
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.max_workers = max_workers
        self._initializer = initializer
        self._initargs = initargs
        self._mp_context = multiprocessing.get_context("spawn")
        self._limiter = CapacityLimiter(max_workers)
        self._pool_lock = threading.Lock()
        self._closed = False
        self._pool = self._new_pool()

    def _new_pool(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=self.max_workers,
            mp_context=self._mp_context,
            initializer=self._initializer,
            initargs=self._initargs,
        )

    async def run(self, fn, *args):
        """Run a picklable callable in a GPU-owning child process."""
        async with self._limiter.slot():
            loop = asyncio.get_running_loop()
            try:
                return await loop.run_in_executor(self._pool, fn, *args)
            except BrokenProcessPool:
                # A native crash/OOM permanently breaks a ProcessPoolExecutor.
                # Replace it so the next request can recover without restarting
                # the whole API process.
                self._replace_pool()
                raise

    def ready(self) -> bool:
        """Return whether this executor can accept new work."""
        return not self._closed and not bool(getattr(self._pool, "_broken", False))

    def _replace_pool(self) -> None:
        with self._pool_lock:
            if self._closed:
                return
            old_pool = self._pool
            old_pool.shutdown(wait=False, cancel_futures=True)
            self._pool = self._new_pool()
            logger.warning("GPU process pool was replaced after a worker failure")

    @staticmethod
    def _terminate_pool(pool: ProcessPoolExecutor) -> None:
        """Stop child processes promptly during cancellation or shutdown."""
        processes = list(getattr(pool, "_processes", {}).values())
        pool.shutdown(wait=False, cancel_futures=True)
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            try:
                process.join(timeout=2)
            except (AttributeError, AssertionError):
                pass
            if process.is_alive():
                process.kill()

    def stats(self) -> dict:
        return {
            "type": "process/gpu",
            "max_workers": self.max_workers,
            **self._limiter.stats(),
        }

    def shutdown(self) -> None:
        self._closed = True
        self._terminate_pool(self._pool)

    def abort(self) -> None:
        """Stop running GPU jobs and leave a fresh pool for later requests.

        ``ProcessPoolExecutor.Future.cancel`` cannot stop a function already
        running in a child.  PDF parsing launches another subprocess inside
        that child, so on client disconnect we must terminate the worker too;
        the child-side parent-death hook then terminates MinerU itself.
        """
        if self._closed:
            return
        pool = self._pool
        self._terminate_pool(pool)
        self._pool = self._new_pool()
