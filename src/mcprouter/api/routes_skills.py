"""REST surface for agent skills.

Integration: `app.include_router(routes_skills.router)` and set
`app.state.skill_exposure` to the gateway's `SkillExposure` (one instance,
shared with the MCP surface).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")

# --- wave-4 S3: activation + bundle ---
# Thin routes over `SkillExposure` — the ONLY implementation of skill
# visibility -> rate limit -> policy -> audit. Identity follows
# routes_execute: an agent key acts as itself (naming another agent is 403);
# the admin token MUST name agentId and acts as that agent, audited with
# initiated_by="admin" (routeRequestId is ignored for admin: an admin trial
# is not the agent selecting the skill). The routed set is derived
# server-side (`routed_skill_ids`), never taken from the client.

import functools  # noqa: E402
from typing import Annotated, Any  # noqa: E402
from urllib.parse import quote  # noqa: E402

import anyio.to_thread  # noqa: E402
from fastapi import HTTPException, Path, Query, Request, Response  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402
from pydantic.alias_generators import to_camel  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from mcprouter.api.deps_auth import get_principal, is_admin_bearer, security_of  # noqa: E402
from mcprouter.api.routes_execute import AGENT_ID_PATTERN, _principal_row  # noqa: E402
from mcprouter.execution.manager import INITIATED_BY_ADMIN  # noqa: E402
from mcprouter.gateway.skills import SkillAccessError, SkillExposure  # noqa: E402
from mcprouter.models import RoutingDecisionRecord  # noqa: E402
from mcprouter.skills.serve import resource_mime  # noqa: E402

SKILL_ID_PREFIX = "skill:"
UNKNOWN_SKILL = "Unknown skill or resource."
ADMIN_NEEDS_AGENT_SKILL = (
    "Admin requests must name the agent to act as: pass agentId. "
    "The request runs under that agent's routing and policy."
)
# code -> (status, curated message). Unknown and unrouted share ONE message so
# a caller cannot probe which skills exist; path problems look the same.
_ERRORS: dict[str, tuple[int, str]] = {
    "denied": (403, "Skill activation denied by policy."),
    "rate_limited": (429, "Too many skill activations; retry later."),
    "too_large": (413, "Skill resource exceeds the size limit."),
}
_INTERNAL = (500, "Internal error while serving the skill.")
_NOT_FOUND_CODES = frozenset(
    {"not_found", "invalid_name", "invalid_path", "not_in_manifest", "unreadable"}
)


def routed_skill_ids(factory: sessionmaker[Session], agent_id: str) -> list[str]:
    """REST visibility: skill ids in the agent's LATEST RoutingDecisionRecord
    (`selected_tool_ids` entries prefixed "skill:", prefix stripped)."""
    with factory() as s:
        row = s.scalars(
            select(RoutingDecisionRecord.selected_tool_ids)
            .where(RoutingDecisionRecord.agent_id == agent_id)
            .order_by(RoutingDecisionRecord.created_at.desc(), RoutingDecisionRecord.id.desc())
            .limit(1)
        ).one_or_none()
    return [i[len(SKILL_ID_PREFIX) :] for i in row or [] if i.startswith(SKILL_ID_PREFIX)]


def _http_error(exc: SkillAccessError) -> HTTPException:
    if exc.code in _NOT_FOUND_CODES:
        return HTTPException(status_code=404, detail=UNKNOWN_SKILL)
    status, msg = _ERRORS.get(exc.code, _INTERNAL)
    return HTTPException(status_code=status, detail=msg)


def _exposure(request: Request) -> SkillExposure:
    exp = getattr(request.app.state, "skill_exposure", None)
    if not isinstance(exp, SkillExposure):
        raise HTTPException(status_code=503, detail="skill exposure not configured")
    return exp


async def _acting_agent(request: Request, named: str | None) -> tuple[str, str | None]:
    """(agent_id, initiated_by) for this request, per the identity rules above."""
    config, _ = security_of(request)
    if is_admin_bearer(config, request.headers.get("authorization")):
        if not named:
            raise HTTPException(status_code=400, detail=ADMIN_NEEDS_AGENT_SKILL)
        row = await anyio.to_thread.run_sync(_principal_row, request, named)
        if row is None:
            raise HTTPException(status_code=404, detail="Unknown agent.")
        return row.agent_id, INITIATED_BY_ADMIN
    principal = await anyio.to_thread.run_sync(get_principal, request)  # 401 on bad key
    if named and named != principal.agent_id:
        raise HTTPException(status_code=403, detail="An agent key can only act as its own agent.")
    return principal.agent_id, None


async def _call(request: Request, fn: Any, *args: Any, **kw: Any) -> Any:
    _, factory = security_of(request)
    agent_id, initiated_by = args[0], kw.pop("initiated_by")
    routed = await anyio.to_thread.run_sync(routed_skill_ids, factory, agent_id)
    try:
        return await anyio.to_thread.run_sync(
            functools.partial(fn, *args, routed_ids=routed, initiated_by=initiated_by, **kw)
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
@router.get("/skills/bundle", tags=["skills"])
async def skills_bundle(request: Request, agent_id: _AgentQ = None) -> Response:
    exp = _exposure(request)
    agent, initiated_by = await _acting_agent(request, agent_id)
    data, skipped = await _call(request, exp.bundle, agent, initiated_by=initiated_by)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "X-Skipped-Resources": ",".join(quote(p, safe="/") for p in skipped),
            "Content-Disposition": 'attachment; filename="skills-bundle.zip"',
        },
    )


@router.post(
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
    rrid = None if initiated_by else body.route_request_id
    act = await _call(
        request, exp.activate, agent, skill_id, route_request_id=rrid, initiated_by=initiated_by
    )
    return ActivateOut(
        body=act.body,
        resources=[SkillResourceOut(**r) for r in act.resources],
        record_id=act.record_id,
    )


@router.get("/skills/{skill_id}/resources/{path:path}", tags=["skills"])
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
