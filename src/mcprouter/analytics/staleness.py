"""Staleness — SUGGESTIONS ONLY. Nothing here disables, hides or deletes.

* stale tool: enabled, registered at least `stale_days` ago, and not surfaced
  by any routing decision in the last `stale_days` days (or never).
* never-routed server: enabled, registered at least `stale_days` ago, has
  tools, and none of its tools has EVER been surfaced.

"Last surfaced" is the newer of the raw decision log and the rollup table, so
it survives a future raw-log retention policy. The raw part is an all-time
scan of routing_decisions (fine at the local-first scale; revisit with
retention).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from mcprouter.analytics.window import midnight
from mcprouter.models import MCPServerRecord, MCPToolRecord, ToolStatsDaily

DEFAULT_STALE_DAYS = 30

_LAST_SURFACED_SQL = text(
    """
SELECT e.tool_id, max(d.created_at)
FROM routing_decisions d
CROSS JOIN LATERAL jsonb_array_elements_text(
    CASE WHEN jsonb_typeof(d.selected_tool_ids::jsonb) = 'array'
         THEN d.selected_tool_ids::jsonb ELSE '[]'::jsonb END
) AS e(tool_id)
GROUP BY e.tool_id
"""
)


@dataclass(frozen=True)
class StaleTool:
    tool_id: str
    tool_name: str
    server_name: str
    created_at: datetime
    last_surfaced_at: datetime | None


@dataclass(frozen=True)
class NeverRoutedServer:
    server_id: str
    server_name: str
    tool_count: int
    created_at: datetime


def last_surfaced(session: Session) -> dict[str, datetime]:
    out: dict[str, datetime] = {
        tid: ts for tid, ts in session.execute(_LAST_SURFACED_SQL).all() if ts is not None
    }
    D = ToolStatsDaily
    for tid, day in session.execute(
        select(D.tool_id, func.max(D.day)).where(D.surfaced > 0).group_by(D.tool_id)
    ).all():
        ts = midnight(day)
        if tid not in out or out[tid] < ts:
            out[tid] = ts
    return out


def stale_report(
    session: Session, now: datetime, stale_days: int = DEFAULT_STALE_DAYS
) -> tuple[list[StaleTool], list[NeverRoutedServer]]:
    cutoff = now - timedelta(days=stale_days)
    seen = last_surfaced(session)
    rows = session.execute(
        select(MCPToolRecord, MCPServerRecord)
        .join(MCPServerRecord, MCPServerRecord.id == MCPToolRecord.server_id)
        .order_by(MCPServerRecord.name, MCPToolRecord.name)
    ).all()
    stale: list[StaleTool] = []
    servers: dict[str, tuple[MCPServerRecord, int, bool]] = {}
    for tool, srv in rows:
        last = seen.get(tool.id)
        n, ever = servers.get(srv.id, (srv, 0, False))[1:]
        servers[srv.id] = (srv, n + 1, ever or last is not None)
        if tool.enabled and tool.created_at <= cutoff and (last is None or last < cutoff):
            stale.append(StaleTool(tool.id, tool.name, srv.name, tool.created_at, last))
    never = [
        NeverRoutedServer(srv.id, srv.name, n, srv.created_at)
        for srv, n, ever in servers.values()
        if srv.enabled and not ever and srv.created_at <= cutoff
    ]
    never.sort(key=lambda s: s.server_name)
    return stale, never
