"""Skills REST, agent side (S3): activation, bundle, resources (`agent_router`).

Split out of routes_skills.py (wave-6 P-207); routes_skills re-exports it.
Include `agent_router` BEFORE `routes_skills_admin.router` (so `/skills/bundle`
is not captured by `/{skill_id}`) and set `app.state.skill_exposure` to the
gateway's `SkillExposure` (one instance, shared with the MCP surface).
"""

from __future__ import annotations

import functools
from typing import Annotated, Any
from urllib.parse import quote

import anyio.to_thread
from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.acting import ADMIN_NEEDS_AGENT, act_as_agent
from mcprouter.api.deps_auth import security_of
from mcprouter.auth import AGENT_ID_PATTERN
from mcprouter.gateway.skills import (
    SKILL_ERROR_MESSAGES,
    SKILL_INTERNAL,
    SKILL_UNKNOWN,
    SkillAccessError,
    SkillExposure,
)
from mcprouter.models import SKILL_ID_PREFIX, RoutingDecisionRecord
from mcprouter.skills.serve import resource_mime

agent_router = APIRouter(prefix="/api/v1")

# --- wave-4 S3: activation + bundle ---
# Thin routes over `SkillExposure` — the ONLY implementation of skill
# visibility -> rate limit -> policy -> audit. Identity follows
# routes_execute: an agent key acts as itself (naming another agent is 403);
# the admin token MUST name agentId and acts as that agent, audited with
# initiated_by="admin" (routeRequestId is ignored for admin: an admin trial
# is not the agent selecting the skill). The routed set is derived
# server-side (`routed_skill_ids`), never taken from the client.


UNKNOWN_SKILL = SKILL_UNKNOWN
ADMIN_NEEDS_AGENT_SKILL = ADMIN_NEEDS_AGENT  # alias (P-206: one message, in api/acting.py)
# code -> HTTP status; the curated message comes from gateway.skills'
# SKILL_ERROR_MESSAGES, shared with the MCP surface. Unknown and unrouted share
# ONE message so a caller cannot probe which skills exist; path problems look
# the same.
_STATUS: dict[str, int] = {
    "denied": 403,
    "rate_limited": 429,
    "too_large": 413,
    "too_many": 413,
    "stale": 409,
    "duplicate_name": 409,
}
_ERRORS: dict[str, tuple[int, str]] = {
    code: (status, SKILL_ERROR_MESSAGES[code]) for code, status in _STATUS.items()
}
_INTERNAL = (500, SKILL_INTERNAL)
_NOT_FOUND_CODES = frozenset(
    {"not_found", "invalid_name", "invalid_path", "not_in_manifest", "unreadable"}
)


def routed_skill_ids(factory: sessionmaker[Session], agent_id: str) -> list[str]:
    """REST visibility: skill ids in the agent's LATEST RoutingDecisionRecord
    (`selected_tool_ids` entries prefixed "skill:", prefix stripped)."""
    with factory() as s:
        row = s.scalars(
            select(RoutingDecisionRecord.selected_tool_ids)
            .where(
                RoutingDecisionRecord.agent_id == agent_id,
                # Admin simulations persist rows under the agent id; they never
                # define what the agent may activate (gateway ignores them too).
                ~RoutingDecisionRecord.model_version.startswith("simulated/"),
            )
            .order_by(RoutingDecisionRecord.created_at.desc(), RoutingDecisionRecord.id.desc())
            .limit(1)
        ).one_or_none()
    return [i[len(SKILL_ID_PREFIX) :] for i in row or [] if i.startswith(SKILL_ID_PREFIX)]


def _http_error(exc: SkillAccessError) -> HTTPException:
    if exc.code in _NOT_FOUND_CODES:
        return HTTPException(status_code=404, detail=UNKNOWN_SKILL)
    status, msg = _ERRORS.get(exc.code, _INTERNAL)
    return HTTPException(status_code=status, detail=msg)


SKIPPED_HEADER_MAX = 2048  # bytes; proxies commonly reject headers past 4-8 KiB


def skipped_header(skipped: list[str]) -> str:
    """Comma-joined, percent-quoted skipped paths, capped at SKIPPED_HEADER_MAX
    bytes: whole entries only, then ",...+N" naming how many were left out."""
    parts = [quote(p, safe="/") for p in skipped]
    full = ",".join(parts)
    if len(full) <= SKIPPED_HEADER_MAX:
        return full
    reserve = len(f",...+{len(parts)}")  # worst-case suffix width
    out: list[str] = []
    size = 0
    for part in parts:
        if size + len(part) + 1 + reserve > SKIPPED_HEADER_MAX:
            break
        out.append(part)
        size += len(part) + 1
    return ",".join([*out, f"...+{len(parts) - len(out)}"])


def _exposure(request: Request) -> SkillExposure:
    exp = getattr(request.app.state, "skill_exposure", None)
    if not isinstance(exp, SkillExposure):
        raise HTTPException(status_code=503, detail="skill exposure not configured")
    return exp


async def _acting_agent(request: Request, named: str | None) -> tuple[str, str | None]:
    """(agent_id, initiated_by) for this request (rules: api/acting.py)."""
    acting = await act_as_agent(request, named)
    return acting.agent_id, acting.initiated_by


async def _call(
    request: Request,
    fn: Any,
    agent_id: str,
    *args: Any,
    initiated_by: str | None,
    **kw: Any,
) -> Any:
    """Run SkillExposure `fn(agent_id, *args, routed_ids=..., initiated_by=...)`
    off the event loop, with the agent's routed set derived server-side."""
    _, factory = security_of(request)
    routed = await anyio.to_thread.run_sync(routed_skill_ids, factory, agent_id)
    try:
        return await anyio.to_thread.run_sync(
            functools.partial(
                fn, agent_id, *args, routed_ids=routed, initiated_by=initiated_by, **kw
            )
        )
    except SkillAccessError as exc:
        raise _http_error(exc) from None


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ActivateIn(_Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    agent_id: str | None = Field(default=None, pattern=AGENT_ID_PATTERN)
    route_request_id: str | None = Field(default=None, max_length=512)


class SkillResourceOut(_Wire):
    path: str | None
    size: int | None
    kind: str | None


class ActivateOut(_Wire):
    body: str
    resources: list[SkillResourceOut]
    record_id: str | None


_SkillId = Annotated[str, Path(min_length=1, max_length=64)]
_AgentQ = Annotated[str | None, Query(alias="agentId", pattern=AGENT_ID_PATTERN)]


# Registered BEFORE the /skills/{skill_id}/... routes so the static path wins.
@agent_router.get("/skills/bundle", tags=["skills"])
async def skills_bundle(request: Request, agent_id: _AgentQ = None) -> Response:
    exp = _exposure(request)
    agent, initiated_by = await _acting_agent(request, agent_id)
    data, skipped = await _call(request, exp.bundle, agent, initiated_by=initiated_by)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "X-Skipped-Resources": skipped_header(skipped),
            "Content-Disposition": 'attachment; filename="skills-bundle.zip"',
        },
    )


@agent_router.post(
    "/skills/{skill_id}/activate",
    tags=["skills"],
    response_model=ActivateOut,
    response_model_by_alias=True,
)
async def activate_skill(
    request: Request, skill_id: _SkillId, body: ActivateIn | None = None
) -> ActivateOut:
    body = body or ActivateIn()
    exp = _exposure(request)
    agent, initiated_by = await _acting_agent(request, body.agent_id)
    rrid = None
    if not initiated_by and body.route_request_id:  # agent-supplied: must be its own decision
        rrid = await anyio.to_thread.run_sync(
            exp._manager.owned_route_request_id, agent, body.route_request_id
        )
    act = await _call(
        request, exp.activate, agent, skill_id, route_request_id=rrid, initiated_by=initiated_by
    )
    return ActivateOut(
        body=act.body,
        resources=[SkillResourceOut(**r) for r in act.resources],
        record_id=act.record_id,
    )


@agent_router.get("/skills/{skill_id}/resources/{path:path}", tags=["skills"])
async def read_skill_resource(
    request: Request, skill_id: _SkillId, path: str, agent_id: _AgentQ = None
) -> Response:
    exp = _exposure(request)
    agent, initiated_by = await _acting_agent(request, agent_id)
    content = await _call(
        request, exp.read_resource, agent, skill_id, path, initiated_by=initiated_by
    )
    mime = resource_mime(content.path, content.is_text)
    data = content.text.encode() if content.text is not None else (content.blob or b"")
    headers = {"X-Content-Type-Options": "nosniff"}
    if not content.is_text or not mime.startswith("text/"):
        headers["Content-Disposition"] = "attachment"
    return Response(content=data, media_type=mime, headers=headers)
