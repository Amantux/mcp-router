"""POST /api/v1/tools/{toolId}/execute — REST execution for the playground.

A thin route over `ExecutionManager.execute`, the ONLY path from a caller to
an upstream tool. There is no parallel path: policy, rate limit,
availability, argument validation, approval gate, audit and invocation all
happen in the manager, exactly as for MCP `tools/call`.

Identity (who the call runs as):

* **Agent key** (or the synthetic `dev` principal in dev mode) -> runs as that
  principal. An `agentId` naming a DIFFERENT agent is 403.
* **Admin token** -> MUST name `agentId` (query or body); runs as THAT
  principal under its own policy, rate limit and approval rules — the admin
  token never widens what the call may do. Every audit row of the attempt is
  annotated `[admin-initiated via REST, impersonating '<agent>']`. Missing
  agentId -> 400; unknown agent -> 404. The attempt is NOT attributed to a
  routing decision (`routeRequestId` is ignored): an admin trying a tool is
  not the agent selecting it, so it must not move the funnel.

Outcomes: every manager outcome (ok, error, timeout, denied, rate_limited,
unavailable, invalid_args, pending_approval) is a 200 with `status`; HTTP
error codes are reserved for transport/auth problems (400/401/403/404/422/503).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import anyio.to_thread
from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import select

from mcprouter.api.deps_auth import get_principal, is_admin_bearer, security_of
from mcprouter.execution.manager import ExecutionManager, ExecutionResult
from mcprouter.execution.redaction import scrub_log
from mcprouter.models import AgentPrincipal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["execution"])

AGENT_ID_PATTERN = r"^[A-Za-z0-9_.\-]{1,120}$"
ADMIN_NEEDS_AGENT = (
    "Admin requests must name the agent to run as: pass agentId (query or body). "
    "The call runs under that agent's policy."
)


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ExecuteIn(_Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    arguments: dict[str, Any] = Field(default_factory=dict)
    route_request_id: str | None = Field(default=None, max_length=36)
    agent_id: str | None = Field(default=None, pattern=AGENT_ID_PATTERN)


class ToolResultOut(_Wire):
    content: list[dict[str, Any]]
    is_error: bool
    structured_content: dict[str, Any] | None = None


class ExecuteOut(_Wire):
    status: str
    detail: str
    record_id: str | None
    approval_id: str | None = None
    errors: list[str] = Field(default_factory=list)
    result: ToolResultOut | None = None
    latency_ms: float | None = None


def _manager(request: Request) -> ExecutionManager:
    mgr = getattr(request.app.state, "execution_manager", None)
    if not isinstance(mgr, ExecutionManager):
        raise HTTPException(status_code=503, detail="execution manager not configured")
    return mgr


def _out(res: ExecutionResult) -> ExecuteOut:
    result = None
    if res.result is not None:
        result = ToolResultOut(
            content=res.result.content,
            is_error=res.result.is_error,
            structured_content=res.result.structured_content,
        )
    return ExecuteOut(
        status=res.status,
        detail=res.detail,
        record_id=res.record_id,
        approval_id=res.approval_id,
        errors=list(res.errors),
        result=result,
        latency_ms=res.latency_ms,
    )


def _principal_row(request: Request, agent_id: str) -> AgentPrincipal | None:
    _, factory = security_of(request)
    with factory() as s:
        row = s.scalars(
            select(AgentPrincipal).where(AgentPrincipal.agent_id == agent_id)
        ).one_or_none()
        if row is not None:
            s.expunge(row)
        return row


@router.post(
    "/tools/{tool_id}/execute",
    response_model=ExecuteOut,
    response_model_by_alias=True,
    response_model_exclude_none=False,
)
async def execute_tool(
    request: Request,
    tool_id: Annotated[str, Path(min_length=1, max_length=64)],
    body: ExecuteIn | None = None,
    agent_id_q: Annotated[str | None, Query(alias="agentId", pattern=AGENT_ID_PATTERN)] = None,
) -> ExecuteOut:
    body = body or ExecuteIn()
    if agent_id_q and body.agent_id and agent_id_q != body.agent_id:
        raise HTTPException(status_code=400, detail="agentId differs between query and body.")
    named = agent_id_q or body.agent_id
    config, _ = security_of(request)
    mgr = _manager(request)

    if is_admin_bearer(config, request.headers.get("authorization")):
        if not named:
            raise HTTPException(status_code=400, detail=ADMIN_NEEDS_AGENT)
        principal = await anyio.to_thread.run_sync(_principal_row, request, named)
        if principal is None:
            raise HTTPException(status_code=404, detail="Unknown agent.")
        log.info(
            "execution.admin_impersonation agent=%s tool=%s",
            scrub_log(principal.agent_id),
            scrub_log(tool_id),
        )
        res = await mgr.execute(
            principal,
            tool_id,
            body.arguments,
            audit_note=f"admin-initiated via REST, impersonating '{principal.agent_id}'",
        )
        return _out(res)

    principal = await anyio.to_thread.run_sync(get_principal, request)  # 401 on a bad credential
    if named and named != principal.agent_id:
        raise HTTPException(
            status_code=403, detail="An agent key can only execute as its own agent."
        )
    res = await mgr.execute(
        principal, tool_id, body.arguments, route_request_id=body.route_request_id
    )
    return _out(res)
