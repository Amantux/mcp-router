"""Per-agent sliding-window rate limiter (in-process; scoping.md: no Redis).

Exact sliding window: each agent keeps the timestamps of its admitted calls
in the last `window_s`; a call is admitted iff fewer than `limit` remain.
Memory per agent is bounded by `limit`. `limit <= 0` admits nothing (fail
closed — `len(q) >= limit` is always true; use a large number for
"effectively unlimited").
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol


class KeyedLimiter(Protocol):
    """What consumers need: SlidingWindowLimiter and limits.SurfaceLimiter."""

    def try_acquire(self, key: str) -> bool: ...


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def try_acquire(self, key: str) -> bool:
        now = self._clock()
        cutoff = now - self.window_s
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def is_idle(self) -> bool:
        """True when no key has a hit inside the window (prunes expired keys)."""
        cutoff = self._clock() - self.window_s
        with self._lock:
            for key in [k for k, q in self._hits.items() if not q or q[-1] <= cutoff]:
                del self._hits[key]
            return not self._hits
