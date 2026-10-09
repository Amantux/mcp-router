"""REST: agent principals, policy rules, approvals (FR-07). Thin router.

Admin-only (Bearer MCPR_ADMIN_TOKEN): principal + rule CRUD, approval list,
approve (executes exactly once), deny. Agent-authenticated: `GET /me` and
polling one's OWN approvals. camelCase on the wire.

Raw API keys leave the process exactly twice in their lifetime: in the
response that creates the principal and in the response that rotates it.
Nothing else ever returns a key or its hash.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mcprouter.api.deps import get_manager, session_factory
from mcprouter.api.deps_auth import (
    DEV_AGENT_ID,
    generate_key,
    get_principal,
    hash_key,
    require_admin,
    security_of,
)
from mcprouter.execution.manager import ApprovalError, ApprovalView
from mcprouter.execution.redaction import scrub_log
from mcprouter.generation import bump_policy
from mcprouter.models import AgentPrincipal, MCPServerRecord, PolicyRule, SkillSourceRecord

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["policy"])

AgentId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.\-]{1,120}$")]
Operation = Literal["read", "write", "execute"]
_DEFAULT_MAX_SKILLS = 3  # mirrors AgentPrincipal.max_skills column default


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PrincipalOut(_Wire):
    id: str
    agent_id: str
    enabled: bool
    max_tools: int
    max_servers: int | None  # distinct-server exposure cap; None = unlimited
    max_skills: int  # Wave 4: routed-skill exposure cap (separate from maxTools)
    created_at: datetime | None  # None only for the synthetic dev principal


class PrincipalCreated(PrincipalOut):
    api_key: str


class KeyRotated(_Wire):
    id: str
    agent_id: str
    api_key: str


class PrincipalIn(_Wire):
    agent_id: AgentId
    max_tools: int = Field(default=8, ge=1, le=64)
    max_servers: int | None = Field(default=None, ge=1, le=1000)
    max_skills: int = Field(default=3, ge=0, le=64)
    enabled: bool = True


class PrincipalPatch(_Wire):
    max_tools: int | None = Field(default=None, ge=1, le=64)
    # Explicit null clears the cap (unlimited); omitted leaves it unchanged.
    max_servers: int | None = Field(default=None, ge=1, le=1000)
    max_skills: int | None = Field(default=None, ge=0, le=64)
    enabled: bool | None = None


class RuleOut(_Wire):
    id: str
    agent_id: str
    resource_kind: (
        str  # tool | skill (skill: serverId = skill source id, toolName = skill-name glob)
    )
    server_id: str | None
    tool_name: str | None
    max_operation: str
    requires_approval: bool
    created_at: datetime


class RuleIn(_Wire):
    agent_id: AgentId
    # Immutable after create (no RulePatch field): flipping kind would silently
    # re-target serverId from a server to a skill source.
    resource_kind: Literal["tool", "skill"] = "tool"
    server_id: str | None = None
    tool_name: str | None = Field(default=None, min_length=1, max_length=200)
    max_operation: Operation = "read"
    requires_approval: bool = False


class RulePatch(_Wire):
    server_id: str | None = None
    tool_name: str | None = Field(default=None, min_length=1, max_length=200)
    max_operation: Operation | None = None
    requires_approval: bool | None = None


class ApprovalOut(_Wire):
    id: str
    agent_id: str
    tool_id: str
    status: str
    summary: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    result_preview: str | None


class ApprovalDecision(_Wire):
    approval_id: str
    status: str
    detail: str
    record_id: str | None
    result_preview: str | None = None


# ----------------------------------------------------------------- helpers
def _session(request: Request) -> Session:
    return session_factory(request)()


_manager = get_manager  # P-206: one spelling, in api/deps.py


def _p_out(p: AgentPrincipal) -> PrincipalOut:
    return PrincipalOut(
        id=p.id,
        agent_id=p.agent_id,
        enabled=p.enabled,
        max_tools=p.max_tools,
        max_servers=p.max_servers,
        # Synthetic dev principal is transient: fall back to the column default.
        max_skills=p.max_skills if p.max_skills is not None else _DEFAULT_MAX_SKILLS,
        created_at=p.created_at,
    )


def _r_out(r: PolicyRule) -> RuleOut:
    return RuleOut(
        id=r.id,
        agent_id=r.agent_id,
        resource_kind=r.resource_kind or "tool",
        server_id=r.server_id,
        tool_name=r.tool_name,
        max_operation=r.max_operation,
        requires_approval=r.requires_approval,
        created_at=r.created_at,
    )


def _a_out(v: ApprovalView) -> ApprovalOut:
    return ApprovalOut(
        id=v.id,
        agent_id=v.agent_id,
        tool_id=v.tool_id,
        status=v.status,
        summary=v.summary,
        created_at=v.created_at,
        expires_at=v.expires_at,
        decided_at=v.decided_at,
        result_preview=v.result_preview,
    )


_APPROVAL_HTTP = {"not_found": 404, "already_decided": 409, "expired": 410}


def _approval_http(exc: ApprovalError) -> HTTPException:
    return HTTPException(status_code=_APPROVAL_HTTP.get(exc.code, 409), detail=exc.message)


def _validate_rule_refs(
    s: Session, agent_id: str, server_id: str | None, kind: str = "tool"
) -> None:
    if agent_id != DEV_AGENT_ID and (
        s.scalars(select(AgentPrincipal.id).where(AgentPrincipal.agent_id == agent_id)).first()
        is None
    ):
        raise HTTPException(status_code=422, detail="agentId does not name a principal")
    if kind == "skill":
        if server_id is not None and s.get(SkillSourceRecord, server_id) is None:
            raise HTTPException(status_code=422, detail="serverId does not name a skill source")
        return
    if server_id is not None and s.get(MCPServerRecord, server_id) is None:
        raise HTTPException(status_code=422, detail="serverId does not name a server")


# -------------------------------------------------------------- principals
Admin = Depends(require_admin)


@router.get("/principals", response_model=list[PrincipalOut], dependencies=[Admin])
def list_principals(request: Request) -> list[PrincipalOut]:
    with _session(request) as s:
        rows = s.scalars(select(AgentPrincipal).order_by(AgentPrincipal.agent_id)).all()
        return [_p_out(p) for p in rows]


# D11 (A1-008): in dev mode the admin API is open only while zero principals
# exist, so creating the first one with no admin token would 403 every admin
# route forever. Refuse instead; the setup wizard shows this message.
FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN = (
    "Set MCPR_ADMIN_TOKEN before creating the first principal; creating one ends dev mode."
)


@router.post(
    "/principals",
    status_code=201,
    response_model=PrincipalCreated,
    dependencies=[Admin],
    responses={409: {"description": "agentId exists, or dev mode without MCPR_ADMIN_TOKEN"}},
)
def create_principal(body: PrincipalIn, request: Request) -> PrincipalCreated:
    config, _ = security_of(request)
    if config.admin_token_hash is None:
        # require_admin let us through without a token => dev mode is active.
        raise HTTPException(status_code=409, detail=FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN)
    key = generate_key()
    with _session(request) as s:
        p = AgentPrincipal(
            agent_id=body.agent_id,
            key_hash=hash_key(key),
            enabled=body.enabled,
            max_tools=body.max_tools,
            max_servers=body.max_servers,
            max_skills=body.max_skills,
        )
        s.add(p)
        try:
            s.commit()
        except IntegrityError:
            raise HTTPException(status_code=409, detail="agentId already exists") from None
        bump_policy()  # route cache (wave 2)
        return PrincipalCreated(**_p_out(p).model_dump(), api_key=key)


def _get_principal_row(s: Session, principal_id: str) -> AgentPrincipal:
    p = s.get(AgentPrincipal, principal_id)
    if p is None:
        raise HTTPException(status_code=404, detail="principal not found")
    return p


@router.get("/principals/{principal_id}", response_model=PrincipalOut, dependencies=[Admin])
def get_principal_by_id(principal_id: str, request: Request) -> PrincipalOut:
    with _session(request) as s:
        return _p_out(_get_principal_row(s, principal_id))


def _end_stale_streams(request: Request, agent_id: str) -> None:
    """After a committed rotate/disable/delete: close the agent's MCP streams
    opened with a credential that is no longer current (E3 P-310 hook). Sync
    routes run on an anyio worker thread, so the threadsafe entry applies."""
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is None:  # apps built without the MCP gateway (tests)
        return
    # Best effort: the credential change is already committed and per-request
    # auth refuses the revoked key regardless. A failure here must not turn a
    # successful rotate into a 500 that discards the one-time new key.
    try:
        gateway.end_stale_streams_threadsafe(agent_id)
    except Exception as exc:  # noqa: BLE001 — boundary; class name only
        log.warning(
            "policy.revoke_streams_failed agent=%s err=%s",
            scrub_log(agent_id),
            type(exc).__name__,
        )


@router.patch("/principals/{principal_id}", response_model=PrincipalOut, dependencies=[Admin])
def patch_principal(principal_id: str, body: PrincipalPatch, request: Request) -> PrincipalOut:
    with _session(request) as s:
        p = _get_principal_row(s, principal_id)
        if body.enabled is not None:
            p.enabled = body.enabled
        if body.max_tools is not None:
            p.max_tools = body.max_tools
        if "max_servers" in body.model_fields_set:
            p.max_servers = body.max_servers
        if body.max_skills is not None:
            p.max_skills = body.max_skills
        s.commit()
        bump_policy()  # route cache (wave 2)
        out = _p_out(p)
    if body.enabled is False:
        _end_stale_streams(request, out.agent_id)
    return out


@router.post(
    "/principals/{principal_id}/rotate-key", response_model=KeyRotated, dependencies=[Admin]
)
def rotate_key(principal_id: str, request: Request) -> KeyRotated:
    key = generate_key()
    with _session(request) as s:
        p = _get_principal_row(s, principal_id)
        p.key_hash = hash_key(key)
        s.commit()
        out = KeyRotated(id=p.id, agent_id=p.agent_id, api_key=key)
    _end_stale_streams(request, out.agent_id)
    return out


@router.delete("/principals/{principal_id}", status_code=204, dependencies=[Admin])
def delete_principal(principal_id: str, request: Request) -> Response:
    with _session(request) as s:
        p = _get_principal_row(s, principal_id)
        # Delete the agent's rules too: a later principal re-using this
        # agent_id must not silently inherit old grants.
        s.execute(delete(PolicyRule).where(PolicyRule.agent_id == p.agent_id))
        agent_id = p.agent_id
        s.delete(p)
        s.commit()
        bump_policy()  # route cache (wave 2)
    _end_stale_streams(request, agent_id)
    return Response(status_code=204)


# ------------------------------------------------------------------- rules
@router.get("/policy-rules", response_model=list[RuleOut], dependencies=[Admin])
def list_rules(
    request: Request, agent_id: str | None = Query(default=None, alias="agentId")
) -> list[RuleOut]:
    with _session(request) as s:
        q = select(PolicyRule).order_by(PolicyRule.created_at, PolicyRule.id)
        if agent_id is not None:
            q = q.where(PolicyRule.agent_id == agent_id)
        return [_r_out(r) for r in s.scalars(q).all()]


@router.post("/policy-rules", status_code=201, response_model=RuleOut, dependencies=[Admin])
def create_rule(body: RuleIn, request: Request) -> RuleOut:
    with _session(request) as s:
        _validate_rule_refs(s, body.agent_id, body.server_id, body.resource_kind)
        r = PolicyRule(
            agent_id=body.agent_id,
            resource_kind=body.resource_kind,
            server_id=body.server_id,
            tool_name=body.tool_name,
            max_operation=body.max_operation,
            requires_approval=body.requires_approval,
        )
        s.add(r)
        s.commit()
        bump_policy()  # route cache (wave 2)
        return _r_out(r)


def _get_rule_row(s: Session, rule_id: str) -> PolicyRule:
    r = s.get(PolicyRule, rule_id)
    if r is None:
        raise HTTPException(status_code=404, detail="rule not found")
    return r


@router.patch("/policy-rules/{rule_id}", response_model=RuleOut, dependencies=[Admin])
def patch_rule(rule_id: str, body: RulePatch, request: Request) -> RuleOut:
    with _session(request) as s:
        r = _get_rule_row(s, rule_id)
        fields = body.model_fields_set
        if "server_id" in fields:
            _validate_rule_refs(s, r.agent_id, body.server_id, r.resource_kind or "tool")
            r.server_id = body.server_id
        if "tool_name" in fields:
            r.tool_name = body.tool_name
        if body.max_operation is not None:
            r.max_operation = body.max_operation
        if body.requires_approval is not None:
            r.requires_approval = body.requires_approval
        s.commit()
        bump_policy()  # route cache (wave 2)
        return _r_out(r)


@router.delete("/policy-rules/{rule_id}", status_code=204, dependencies=[Admin])
def delete_rule(rule_id: str, request: Request) -> Response:
    with _session(request) as s:
        s.delete(_get_rule_row(s, rule_id))
        s.commit()
        bump_policy()  # route cache (wave 2)
    return Response(status_code=204)


# --------------------------------------------------------------- approvals
ApprovalStatus = Literal["pending", "executing", "executed", "failed", "denied", "expired"]
APPROVALS_MAX_LIMIT = 500


@router.get("/approvals", response_model=list[ApprovalOut], dependencies=[Admin])
async def list_approvals(
    request: Request,
    status: ApprovalStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=APPROVALS_MAX_LIMIT)] = 200,
) -> list[ApprovalOut]:
    """Newest first, at most `limit` (default 200, the old silent cap). The
    response stays a bare list; an unknown `status` is 422, not `[]`."""
    return [_a_out(v) for v in await _manager(request).list_approvals(status, limit)]


@router.post(
    "/approvals/{approval_id}/approve", response_model=ApprovalDecision, dependencies=[Admin]
)
async def approve(approval_id: str, request: Request) -> ApprovalDecision:
    mgr = _manager(request)
    try:
        res = await mgr.approve(approval_id)
    except ApprovalError as exc:
        raise _approval_http(exc) from None
    view = await mgr.get_approval(approval_id)
    return ApprovalDecision(
        approval_id=approval_id,
        status=res.status,
        detail=res.detail,
        record_id=res.record_id,
        result_preview=view.result_preview if view else None,
    )


@router.post("/approvals/{approval_id}/deny", response_model=ApprovalDecision, dependencies=[Admin])
async def deny(approval_id: str, request: Request) -> ApprovalDecision:
    try:
        await _manager(request).deny(approval_id)
    except ApprovalError as exc:
        raise _approval_http(exc) from None
    return ApprovalDecision(
        approval_id=approval_id, status="denied", detail="denied", record_id=None
    )


# ------------------------------------------------------------------- agent
@router.get("/me", response_model=PrincipalOut)
def me(principal: AgentPrincipal = Depends(get_principal)) -> PrincipalOut:
    return _p_out(principal)


@router.get("/me/approvals/{approval_id}", response_model=ApprovalOut)
async def my_approval(
    approval_id: str, request: Request, principal: AgentPrincipal = Depends(get_principal)
) -> ApprovalOut:
    view = await _manager(request).get_approval(approval_id, agent_id=principal.agent_id)
    if view is None:  # someone else's approval is indistinguishable from none
        raise HTTPException(status_code=404, detail="approval not found")
    return _a_out(view)
