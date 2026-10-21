"""Query-side TTL cache, keyed so caching can never cross sessions
(requirements §4 statelessness-per-request) and is tied to the exact
catalog version it was computed against (requirements §3 replayability).
"""

import time
from typing import Any, Dict, Optional, Tuple


class TTLCache:
    def __init__(self, ttl_seconds: float = 30.0):
        self.ttl_seconds = ttl_seconds
        self._store: Dict[Tuple, Tuple[float, Any]] = {}
        self.hits = 0
        self.misses = 0

    def _key(self, session_id: str, query_hash: str, catalog_version: int) -> Tuple:
        return (session_id, query_hash, catalog_version)

    def get(self, session_id: str, query_hash: str, catalog_version: int) -> Optional[Any]:
        key = self._key(session_id, query_hash, catalog_version)
        hit = self._store.get(key)
        if hit is None:
            self.misses += 1
            return None
        expires_at, value = hit
        if time.monotonic() > expires_at:
            del self._store[key]
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, session_id: str, query_hash: str, catalog_version: int, value: Any) -> None:
        key = self._key(session_id, query_hash, catalog_version)
        self._store[key] = (time.monotonic() + self.ttl_seconds, value)

    def clear(self) -> None:
        self._store.clear()
        self.hits = 0
        self.misses = 0
