"""Per-agent exposed tool sets (FR-06). In-process (scoping.md: no Redis).

The exposure is keyed by the authenticated principal's agent_id, so the REST
`/route` call and the agent's MCP session(s) share one view: whatever the
agent's LAST route selected. It is a context-window optimization, NOT an
authorization boundary — everything read from here is re-filtered through
policy before it is shown, and calls are authorized independently by the
execution manager.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from mcprouter.models import utcnow


@dataclass(frozen=True)
class Exposure:
    tool_ids: tuple[str, ...]  # internal tool UUIDs, in route order
    request_id: str
    updated_at: datetime


class ExposureStore:
    def __init__(self) -> None:
        self._by_agent: dict[str, Exposure] = {}
        self._lock = threading.Lock()

    def get(self, agent_id: str) -> Exposure | None:
        with self._lock:
            return self._by_agent.get(agent_id)

    def set(self, agent_id: str, tool_ids: list[str], request_id: str) -> bool:
        """Store; returns True iff the visible set (ids or order) changed."""
        new = Exposure(tuple(tool_ids), request_id, utcnow())
        with self._lock:
            old = self._by_agent.get(agent_id)
            self._by_agent[agent_id] = new
        return old is None or old.tool_ids != new.tool_ids

    def clear(self, agent_id: str) -> bool:
        with self._lock:
            return self._by_agent.pop(agent_id, None) is not None
