"""Non-blocking execution resources shared by all document formats.

There are deliberately only three constrained resources:

* ``external``: blocking converters such as Pandoc and LibreOffice;
* ``gpu``: spawn-based child processes that own GPU models;
* ``network``: an asyncio semaphore for active HTTP/LLM calls.

Small, bounded work runs inline. Formats may use multiple resources during
one parse; resources belong to stages, not to file extensions.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from workers.limits import CapacityLimiter
from workers.processes import GpuExecutor
from workers.threads import ExternalExecutor

__all__ = [
    "ExecutionTarget",
    "ExternalExecutor",
    "GpuExecutor",
    "WorkerConfig",
    "Workers",
]

T = TypeVar("T")


class ExecutionTarget(StrEnum):
    INLINE = "inline"
    EXTERNAL = "external"
    GPU = "gpu"


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Local concurrency limits for one API server process."""

    external_workers: int = 4
    gpu_workers: int = 1
    network_concurrency: int = 8


class Workers:
    """Own the server's executors and async capacity limits."""

    def __init__(
        self,
        config: WorkerConfig | None = None,
        *,
        gpu_initializer=None,
        gpu_initargs: tuple = (),
    ) -> None:
        self.config = config or WorkerConfig()
        self.external = ExternalExecutor(self.config.external_workers)
        self.gpu = GpuExecutor(
            self.config.gpu_workers,
            initializer=gpu_initializer,
            initargs=gpu_initargs,
        )
        self._network = CapacityLimiter(self.config.network_concurrency)

    async def run(self, target: ExecutionTarget | str, fn: Callable[..., T], *args) -> T:
        """Run one synchronous stage on its selected resource."""
        target = ExecutionTarget(target)
        if target is ExecutionTarget.EXTERNAL:
            return await self.run_external(fn, *args)
        if target is ExecutionTarget.GPU:
            return await self.run_gpu(fn, *args)

        result = fn(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    async def run_external(self, fn: Callable[..., T], *args) -> T:
        """Run a blocking converter/library without blocking asyncio."""
        return await self.external.run(fn, *args)

    async def run_gpu(self, fn: Callable[..., T], *args) -> T:
        """Run a picklable GPU stage in a spawn-based child process."""
        return await self.gpu.run(fn, *args)

    async def run_network(
        self,
        fn: Callable[..., Awaitable[T]],
        *args,
        **kwargs,
    ) -> T:
        """Run one async network call under the shared in-flight limit."""
        async with self._network.slot():
            return await fn(*args, **kwargs)

    @asynccontextmanager
    async def network_slot(self) -> AsyncIterator[None]:
        """Reserve a network slot for more than one await."""
        async with self._network.slot():
            yield

    @asynccontextmanager
    async def external_slot(self) -> AsyncIterator[None]:
        """Reserve external capacity for an asyncio subprocess."""
        async with self.external.slot():
            yield

    def stats(self) -> dict:
        return {
            "external": self.external.stats(),
            "gpu": self.gpu.stats(),
            "network": {
                "type": "async-semaphore/network",
                **self._network.stats(),
            },
        }

    def shutdown(self) -> None:
        self.external.shutdown()
        self.gpu.shutdown()

    def abort_gpu(self) -> None:
        """Cancel the running GPU job and reset its worker pool."""
        self.gpu.abort()
