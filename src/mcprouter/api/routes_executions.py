"""GET /api/v1/executions — execution audit history (admin; camelCase wire).

Shape reconciles the UI's guess (docs/history/INTEGRATION_NOTES-ui.md #17):
query `agentId, outcome, limit, offset` -> `{items, total, limit, offset}`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from mcprouter.api.deps_auth import require_admin
from mcprouter.execution.history import MAX_LIMIT, list_executions

router = APIRouter(prefix="/api/v1", tags=["executions"], dependencies=[Depends(require_admin)])


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ExecutionOut(_Wire):
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
    # Wave-2 attribution (read-only): the routing decision this call followed;
    # None = unattributed (legacy rows, approval replays, admin-initiated runs).
    route_request_id: str | None = None
    # 'admin' marks an impersonated (admin-initiated) run; null/'agent' = the agent itself.
    initiated_by: str | None = None


class ExecutionPageOut(_Wire):
    items: list[ExecutionOut]
    total: int
    limit: int
    offset: int


@router.get("/executions", response_model=ExecutionPageOut, response_model_by_alias=True)
def executions(
    request: Request,
    agent_id: Annotated[str | None, Query(alias="agentId", max_length=120)] = None,
    outcome: Annotated[str | None, Query(max_length=16)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExecutionPageOut:
    with request.app.state.session_factory() as s:
        page = list_executions(s, agent_id=agent_id, outcome=outcome, limit=limit, offset=offset)
    return ExecutionPageOut(
        items=[ExecutionOut.model_validate(r, from_attributes=True) for r in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )
