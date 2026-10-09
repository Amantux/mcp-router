"""In-process route cache (bounded LRU + TTL) and the query-embedding LRU.

What the route cache may skip: retrieval and decision-model calls. What it
may NEVER skip: authorization. Every hit is re-validated by the pipeline
(`RoutePipeline._revalidate`) against the CURRENT catalog eligibility and the
CURRENT scope (`ScopeFilter.permits`) before anything is returned; if any
cached tool no longer passes, the hit is discarded and the route recomputed.

Key = (agent_id, normalized query, scope fingerprint, catalog generation,
policy generation, effective max_tools, effective max_servers, allowed server
ids, decision-model name). The query is normalized by collapsing whitespace
only — case is preserved because the decision model sees case.

Not cached: fallback results (a transient model timeout must not pin a
degraded ranking for the TTL) and any scope without a `fingerprint()`
(an unknown ScopeFilter implementation cannot be keyed safely).

Prometheus: mcpr_route_cache_hits_total, mcpr_route_cache_misses_total,
mcpr_route_cache_evictions_total{reason="capacity"|"expired"}.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass

from prometheus_client import Counter

ROUTE_CACHE_HITS = Counter("mcpr_route_cache_hits", "Route cache hits (before re-validation)")
ROUTE_CACHE_MISSES = Counter("mcpr_route_cache_misses", "Route cache misses")
ROUTE_CACHE_EVICTIONS = Counter(
    "mcpr_route_cache_evictions", "Route cache evictions", labelnames=("reason",)
)

QUERY_EMBEDDING_CACHE_SIZE = 512


def normalize_query(query: str) -> str:
    return " ".join(query.split())


@dataclass(frozen=True)
class CachedTool:
    tool_id: str
    score: float
    kind: str = "tool"


@dataclass(frozen=True)
class CachedRoute:
    tools: tuple[CachedTool, ...]
    no_match: bool
    model_version: str


class _Entry:
    __slots__ = ("expires_at", "value")

    def __init__(self, value: CachedRoute, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at


class RouteCache:
    def __init__(
        self, *, size: int, ttl_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if size < 1 or ttl_s <= 0:
            raise ValueError("route cache needs size >= 1 and ttl_s > 0 (use None to disable)")
        self._size = size
        self._ttl_s = ttl_s
        self._clock = clock
        self._data: OrderedDict[Hashable, _Entry] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, size: int, ttl_s: float) -> RouteCache | None:
        """0 (either knob) disables the cache."""
        if size <= 0 or ttl_s <= 0:
            return None
        return cls(size=size, ttl_s=ttl_s)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def get(self, key: Hashable) -> CachedRoute | None:
        now = self._clock()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                ROUTE_CACHE_MISSES.inc()
                return None
            if entry.expires_at <= now:
                del self._data[key]
                ROUTE_CACHE_EVICTIONS.labels(reason="expired").inc()
                ROUTE_CACHE_MISSES.inc()
                return None
            self._data.move_to_end(key)
            ROUTE_CACHE_HITS.inc()
            return entry.value

    def put(self, key: Hashable, value: CachedRoute) -> None:
        with self._lock:
            self._data[key] = _Entry(value, self._clock() + self._ttl_s)
            self._data.move_to_end(key)
            while len(self._data) > self._size:
                self._data.popitem(last=False)
                ROUTE_CACHE_EVICTIONS.labels(reason="capacity").inc()

    def discard(self, key: Hashable) -> None:
        with self._lock:
            self._data.pop(key, None)


class QueryEmbeddingCache:
    """LRU of query vectors keyed by (text, backend name). Embeddings are a
    pure function of both, so there is nothing to invalidate; a backend
    switch simply changes the key."""

    def __init__(self, size: int = QUERY_EMBEDDING_CACHE_SIZE) -> None:
        self._size = size
        self._data: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def get_or_compute(
        self, text: str, backend: str, compute: Callable[[str], list[float]]
    ) -> list[float]:
        key = (text, backend)
        with self._lock:
            vec = self._data.get(key)
            if vec is not None:
                self._data.move_to_end(key)
                return vec
        vec = compute(text)  # outside the lock: the backend may be slow
        with self._lock:
            self._data[key] = vec
            self._data.move_to_end(key)
            while len(self._data) > self._size:
                self._data.popitem(last=False)
        return vec
