"""Execution history reads (the audit trail written by the execution manager)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mcprouter.models import ExecutionRecord, MCPServerRecord, MCPToolRecord

MAX_LIMIT = 200


@dataclass(frozen=True)
class ExecutionRow:
    id: str
    agent_id: str
    tool_id: str | None
    server_id: str | None
    tool_name: str | None
    server_name: str | None
    outcome: str
    detail: str
    latency_ms: float | None
    created_at: datetime
    route_request_id: str | None = None  # wave-2 attribution; None = unattributed
    initiated_by: str | None = None  # 'admin' = impersonated run; None/'agent' = the agent itself


@dataclass(frozen=True)
class ExecutionPage:
    items: list[ExecutionRow]
    total: int
    limit: int
    offset: int


def list_executions(
    session: Session,
    *,
    agent_id: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ExecutionPage:
    """Newest first. Tool/server names are joined for display (LEFT JOIN: the
    audit row outlives a deleted tool)."""
    conds = []
    if agent_id is not None:
        conds.append(ExecutionRecord.agent_id == agent_id)
    if outcome is not None:
        conds.append(ExecutionRecord.outcome == outcome)
    total = session.scalar(select(func.count()).select_from(ExecutionRecord).where(*conds)) or 0
    rows = session.execute(
        select(ExecutionRecord, MCPToolRecord.name, MCPServerRecord.name)
        .outerjoin(MCPToolRecord, MCPToolRecord.id == ExecutionRecord.tool_id)
        .outerjoin(MCPServerRecord, MCPServerRecord.id == ExecutionRecord.server_id)
        .where(*conds)
        .order_by(ExecutionRecord.created_at.desc(), ExecutionRecord.id)
        .limit(limit)
        .offset(offset)
    ).all()
    items = [
        ExecutionRow(
            id=r.id,
            agent_id=r.agent_id,
            tool_id=r.tool_id,
            server_id=r.server_id,
            tool_name=tool_name,
            server_name=server_name,
            outcome=r.outcome,
            detail=r.detail,
            latency_ms=r.latency_ms,
            created_at=r.created_at,
            route_request_id=r.route_request_id,
            initiated_by=r.initiated_by,
        )
        for r, tool_name, server_name in rows
    ]
    return ExecutionPage(items=items, total=total, limit=limit, offset=offset)
