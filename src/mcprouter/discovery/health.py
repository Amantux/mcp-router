"""Server health state machine: healthy | degraded | offline (| unknown).

Deterministic transitions (pure function ``next_status``):

* probe OK and fast            -> healthy (failure streak reset)
* probe OK but slower than ``slow_ms`` -> degraded
* probe failed: streak += 1;
    - streak >= ``offline_after`` OR server was never healthy (unknown/offline)
                               -> offline
    - otherwise                -> degraded   (one blip doesn't take a server out)

The failure streak is process-local (``HealthTracker``): there is no column
for it (see docs/history/INTEGRATION_NOTES-*). After a restart the first failure of a
previously-healthy server therefore reads as ``degraded``, never worse.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

HEALTHY, DEGRADED, OFFLINE, UNKNOWN = "healthy", "degraded", "offline", "unknown"


@dataclass(frozen=True)
class HealthPolicy:
    slow_ms: float = 2000.0
    offline_after: int = 3


def next_status(
    prev: str, *, ok: bool, latency_ms: float | None, failures: int, policy: HealthPolicy
) -> str:
    if ok:
        return DEGRADED if (latency_ms or 0.0) > policy.slow_ms else HEALTHY
    if failures >= policy.offline_after or prev in (UNKNOWN, OFFLINE):
        return OFFLINE
    return DEGRADED


class HealthTracker:
    """Thread-safe failure-streak bookkeeping around ``next_status``."""

    def __init__(self, policy: HealthPolicy | None = None) -> None:
        self.policy = policy or HealthPolicy()
        self._failures: dict[str, int] = {}
        self._lock = threading.Lock()

    def success(self, server_id: str, prev: str, latency_ms: float) -> str:
        with self._lock:
            self._failures.pop(server_id, None)
        return next_status(prev, ok=True, latency_ms=latency_ms, failures=0, policy=self.policy)

    def failure(self, server_id: str, prev: str) -> str:
        with self._lock:
            n = self._failures.get(server_id, 0) + 1
            self._failures[server_id] = n
        return next_status(prev, ok=False, latency_ms=None, failures=n, policy=self.policy)

    def forget(self, server_id: str) -> None:
        with self._lock:
            self._failures.pop(server_id, None)
