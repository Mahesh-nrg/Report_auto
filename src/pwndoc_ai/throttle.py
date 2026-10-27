"""Small in-memory limits for the routes that spend LLM money or hold a worker thread for long.

``Throttle`` allows a few requests in flight per key (usually the signed-in user) and a bounded
number per minute, optionally with a ceiling across all keys. A request over the limit gets
:class:`Busy`, which the API turns into HTTP 429. ``ResultCache`` is a tiny LRU so an expensive,
read-only answer is not recomputed while its inputs are unchanged.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from typing import Any

WINDOW_SECONDS = 60.0
MAX_KEYS = 2048  # bounded: bogus keys cannot grow the table forever


class Busy(RuntimeError):
    """The caller is over a limit; ``retry_after`` is a hint in seconds."""

    def __init__(self, message: str, retry_after: int = 5):
        super().__init__(message)
        self.retry_after = retry_after


class Throttle:
    def __init__(
        self,
        in_flight: int = 1,
        per_minute: int = 10,
        max_total: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._in_flight_limit = max(1, in_flight)
        self._per_minute = max(1, per_minute)
        self._max_total = max_total
        self._clock = clock
        self._lock = threading.Lock()
        self._active: dict[Hashable, int] = {}
        self._starts: dict[Hashable, deque[float]] = {}
        self._total = 0

    @contextmanager
    def slot(self, key: Hashable) -> Iterator[None]:
        """Hold one slot for ``key`` while the ``with`` body runs, or raise :class:`Busy`."""
        self._enter(key)
        try:
            yield
        finally:
            self._leave(key)

    def _enter(self, key: Hashable) -> None:
        now = self._clock()
        with self._lock:
            if self._active.get(key, 0) >= self._in_flight_limit:
                raise Busy("a previous request of yours is still running; wait for it to finish")
            if self._max_total is not None and self._total >= self._max_total:
                raise Busy("the server is busy with other requests; try again shortly")
            starts = self._starts.get(key)
            if starts is None:
                if len(self._starts) >= MAX_KEYS:
                    self._forget_idle(now)
                starts = self._starts.setdefault(key, deque())
            while starts and now - starts[0] >= WINDOW_SECONDS:
                starts.popleft()
            if len(starts) >= self._per_minute:
                wait = max(1, int(WINDOW_SECONDS - (now - starts[0])) + 1)
                raise Busy(f"too many requests; try again in {wait} seconds", wait)
            starts.append(now)
            self._active[key] = self._active.get(key, 0) + 1
            self._total += 1

    def _leave(self, key: Hashable) -> None:
        with self._lock:
            left = self._active.get(key, 0) - 1
            if left > 0:
                self._active[key] = left
            else:
                self._active.pop(key, None)
            self._total = max(0, self._total - 1)

    def _forget_idle(self, now: float) -> None:
        """Caller holds the lock: drop keys with nothing running and nothing recent."""
        for key in [
            k
            for k, starts in self._starts.items()
            if k not in self._active and (not starts or now - starts[-1] >= WINDOW_SECONDS)
        ]:
            del self._starts[key]


class ResultCache:
    """Least-recently-used cache of computed answers (thread-safe, bounded)."""

    def __init__(self, max_items: int = 64):
        self._max = max(1, max_items)
        self._items: OrderedDict[Hashable, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Any | None:
        with self._lock:
            if key not in self._items:
                return None
            self._items.move_to_end(key)
            return self._items[key]

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)
