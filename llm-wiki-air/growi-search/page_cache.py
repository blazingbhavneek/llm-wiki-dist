"""Small byte and item bounded LRU for page bodies."""

from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock

from models import WikiPage


class PageCache:
    def __init__(self, ttl: int | None, max_items: int, max_bytes: int | None = None) -> None:
        self._ttl = ttl
        self._max = max_items
        self._max_bytes = max_bytes
        self._bytes = 0
        self._data: OrderedDict[tuple[str, str], tuple[float, WikiPage]] = OrderedDict()
        self._sizes: dict[tuple[str, str], int] = {}
        self._lock = Lock()

    def _remove(self, key: tuple[str, str]) -> None:
        self._data.pop(key, None)
        self._bytes -= self._sizes.pop(key, 0)

    def get(self, key: tuple[str, str]) -> WikiPage | None:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expires, page = item
            if expires is not None and time.monotonic() > expires:
                self._remove(key)
                return None
            self._data.move_to_end(key)
            return page.model_copy(deep=True)

    def put(self, key: tuple[str, str], page: WikiPage) -> None:
        size = len(page.body.encode("utf-8")) + 512
        with self._lock:
            self._remove(key)
            if self._max_bytes is not None and size > self._max_bytes:
                return
            expires = time.monotonic() + self._ttl if self._ttl is not None else None
            self._data[key] = (expires, page.model_copy(deep=True))
            self._sizes[key] = size
            self._bytes += size
            while len(self._data) > self._max or (self._max_bytes is not None and self._bytes > self._max_bytes):
                self._remove(next(iter(self._data)))

    def remove(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._remove(key)

    def size(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._sizes.clear()
            self._bytes = 0
