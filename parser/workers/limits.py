"""Small asyncio-native capacity limiters shared by worker resources."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class CapacityLimiter:
    """Bound active work without blocking the asyncio event loop."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit = limit
        self.active = 0
        self.waiting = 0
        self._semaphore = asyncio.Semaphore(limit)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        self.waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            self.waiting -= 1

        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self._semaphore.release()

    def stats(self) -> dict[str, int]:
        return {
            "limit": self.limit,
            "active": self.active,
            "waiting": self.waiting,
        }
