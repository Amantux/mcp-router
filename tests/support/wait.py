"""Poll-until helper: replaces fixed ``time.sleep`` synchronisation."""

from __future__ import annotations

import time
from collections.abc import Callable


def wait_for(cond: Callable[[], object], timeout: float = 10.0, interval: float = 0.02) -> None:
    """Return as soon as ``cond()`` is truthy; ``TimeoutError`` after ``timeout`` s.

    ``cond`` is always evaluated at least once, and once more at the deadline,
    so a condition that becomes true during the last sleep is not missed."""
    deadline = time.monotonic() + timeout
    while True:
        if cond():
            return
        if time.monotonic() >= deadline:
            if cond():
                return
            raise TimeoutError(f"condition not met within {timeout}s")
        time.sleep(interval)
