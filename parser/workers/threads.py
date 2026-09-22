"""Bounded executor for blocking external tools and libraries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from workers.limits import CapacityLimiter


class ExternalExecutor:
    """Run blocking external calls without occupying the event loop.

    The limiter is acquired before submission, so the executor's private
    work queue cannot grow without bound. Waiting coroutines yield control
    back to asyncio and do not hold a worker thread.
    """

    def __init__(self, max_workers: int = 4) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.max_workers = max_workers
        self._limiter = CapacityLimiter(max_workers)
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="doc-external",
        )

    async def run(self, fn, *args):
        """Run a blocking callable in the bounded thread executor."""
        async with self._limiter.slot():
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._pool, fn, *args)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Reserve capacity while directly using an asyncio subprocess."""
        async with self._limiter.slot():
            yield

    def stats(self) -> dict:
        return {
            "type": "thread/external",
            "max_workers": self.max_workers,
            **self._limiter.stats(),
        }

    def ready(self) -> bool:
        """Return whether this executor can accept new work."""
        return not bool(getattr(self._pool, "_shutdown", False))

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
