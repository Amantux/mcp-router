"""Skills REST: admin read side (S1, `router`) + agent activation/bundle (S3, `agent_router`).

Integration: include `agent_router` BEFORE `router` (so `/skills/bundle` is not
captured by `/{skill_id}`) and set `app.state.skill_exposure` to the gateway's
`SkillExposure` (one instance, shared with the MCP surface).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.deps import session_factory
from mcprouter.api.deps_auth import require_admin
from mcprouter.generation import bump_catalog
from mcprouter.models import SkillRecord, SkillVersionRecord
from mcprouter.registry.audit import audit
from mcprouter.registry.catalog import _CLASSIFICATION_COLUMNS
from mcprouter.registry.wire import ClassificationPatchIn

router = APIRouter(prefix="/api/v1/skills", tags=["skills"], dependencies=[Depends(require_admin)])


_factory = session_factory  # P-206: one spelling, in api/deps.py


def _summary(r: SkillRecord) -> dict[str, Any]:
    return {
        "id": r.id,
        "sourceId": r.source_id,
        "name": r.name,
        "description": r.description,
        "domain": r.domain,
        "operation": r.operation,
        "categories": list(r.capabilities or []),
        "tags": list(r.tags or []),
        "requiredScopes": list(r.required_scopes or []),
        "version": r.version,
        "enabled": r.enabled,
        "available": r.available,
        "classificationReviewed": r.classification_reviewed,
        "hasScripts": r.has_scripts,
        "bodyTokensEst": r.body_tokens_est,
        "ingestFlags": r.ingest_flags,
    }


def _get(s: Session, skill_id: str) -> SkillRecord:
    rec = s.get(SkillRecord, skill_id)
    if rec is None:
        raise HTTPException(404, "skill not found")
    return rec


@router.get("")
def list_skills(
    request: Request,
    q: str | None = Query(default=None, max_length=200),
    domain: str | None = None,
    operation: str | None = None,
    source: str | None = None,
    source_id: str | None = Query(default=None, alias="sourceId"),  # D13: the UI's name
    has_scripts: bool | None = Query(default=None, alias="hasScripts"),  # D13
    enabled: bool | None = None,
    available: bool | None = None,
    reviewed: bool | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if source is not None and source_id is not None and source != source_id:
        raise HTTPException(422, "source and sourceId disagree; pass one of them")
    source = source if source is not None else source_id
    stmt = select(SkillRecord)
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        stmt = stmt.where(or_(SkillRecord.name.ilike(like), SkillRecord.description.ilike(like)))
    for col, val in (
        (SkillRecord.domain, domain),
        (SkillRecord.operation, operation),
        (SkillRecord.source_id, source),
        (SkillRecord.enabled, enabled),
        (SkillRecord.available, available),
        (SkillRecord.classification_reviewed, reviewed),
        (SkillRecord.has_scripts, has_scripts),
    ):
        if val is not None:
            stmt = stmt.where(col == val)
    with _factory(request)() as s:
        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = s.scalars(
            stmt.order_by(SkillRecord.name, SkillRecord.id).limit(limit).offset(offset)
        )
        return {
            "items": [_summary(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


@router.get("/{skill_id}")
def get_skill(skill_id: str, request: Request) -> dict[str, Any]:
    with _factory(request)() as s:
        r = _get(s, skill_id)
        versions = s.scalars(
            select(SkillVersionRecord)
            .where(SkillVersionRecord.skill_id == r.id)
            .order_by(SkillVersionRecord.version.desc())
            .limit(10)
        )
        return {
            **_summary(r),
            "frontmatter": {
                "name": r.name,
                "description": r.description,
                "license": r.license,
                "compatibility": r.compatibility,
                "metadata": r.skill_metadata,
                "allowedTools": r.allowed_tools,
            },
            "relativePath": r.relative_path,
            "resourceManifest": r.resource_manifest,
            "contentHash": r.content_hash,
            "manifestHash": r.manifest_hash,
            "recentVersions": [
                {
                    "version": v.version,
                    "changeKind": v.change_kind,
                    "recordedAt": v.recorded_at.isoformat(),
                }
                for v in versions
            ],
        }


@router.patch("/{skill_id}/classification")
def patch_classification(
    skill_id: str,
    body: ClassificationPatchIn,
    request: Request,
    admin: Annotated[str, Depends(require_admin)],
) -> dict[str, Any]:
    """Human override (mirrors the tools PATCH): wins over auto-classification
    forever, because the skill classifier skips `classification_reviewed` rows."""
    fields = body.to_fields()
    with _factory(request)() as s:
        r = _get(s, skill_id)
        for wire_name, value in fields.items():
            setattr(r, _CLASSIFICATION_COLUMNS[wire_name], value)
        r.classification_reviewed = True
        r.classification_source = "human"
        s.commit()
        audit(
            "skill.classification.override",
            actor=admin,
            skill_id=r.id,
            skill=r.name,
            fields=",".join(sorted(fields)) or "(approve)",
        )
        bump_catalog()  # route cache
        return _summary(r)


@router.get("/{skill_id}/body")
def get_body(skill_id: str, request: Request) -> dict[str, Any]:
    with _factory(request)() as s:
        r = _get(s, skill_id)
        return {"id": r.id, "body": r.body, "bodyTokensEst": r.body_tokens_est}


@router.get("/{skill_id}/versions")
def get_versions(skill_id: str, request: Request) -> list[dict[str, Any]]:
    with _factory(request)() as s:
        _get(s, skill_id)
        rows = s.scalars(
            select(SkillVersionRecord)
            .where(SkillVersionRecord.skill_id == skill_id)
            .order_by(SkillVersionRecord.version)
        )
        return [
            {
                "version": v.version,
                "changeKind": v.change_kind,
                "contentHash": v.content_hash,
                "manifestHash": v.manifest_hash,
                "recordedAt": v.recorded_at.isoformat(),
            }
            for v in rows
        ]


# ---- S3: activate / bundle (write side) goes below this line ----

agent_router = APIRouter(prefix="/api/v1")

# --- wave-4 S3: activation + bundle ---
# Thin routes over `SkillExposure` — the ONLY implementation of skill
# visibility -> rate limit -> policy -> audit. Identity follows
# routes_execute: an agent key acts as itself (naming another agent is 403);
# the admin token MUST name agentId and acts as that agent, audited with
# initiated_by="admin" (routeRequestId is ignored for admin: an admin trial
# is not the agent selecting the skill). The routed set is derived
# server-side (`routed_skill_ids`), never taken from the client.

import functools  # noqa: E402
from urllib.parse import quote  # noqa: E402

import anyio.to_thread  # noqa: E402
from fastapi import Path, Query, Response  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402
from pydantic.alias_generators import to_camel  # noqa: E402

from mcprouter.api.acting import ADMIN_NEEDS_AGENT, AGENT_ID_PATTERN, act_as_agent  # noqa: E402
from mcprouter.api.deps_auth import security_of  # noqa: E402
from mcprouter.gateway.skills import SkillAccessError, SkillExposure  # noqa: E402
from mcprouter.models import RoutingDecisionRecord  # noqa: E402
from mcprouter.skills.serve import resource_mime  # noqa: E402

SKILL_ID_PREFIX = "skill:"
UNKNOWN_SKILL = "Unknown skill or resource."
ADMIN_NEEDS_AGENT_SKILL = ADMIN_NEEDS_AGENT  # alias (P-206: one message, in api/acting.py)
# code -> (status, curated message). Unknown and unrouted share ONE message so
# a caller cannot probe which skills exist; path problems look the same.
_ERRORS: dict[str, tuple[int, str]] = {
    "denied": (403, "Skill activation denied by policy."),
    "rate_limited": (429, "Too many skill activations; retry later."),
    "too_large": (413, "Skill resource exceeds the size limit."),
    "too_many": (413, "Too many skills routed to bundle; narrow the routing."),
    "stale": (409, "Skill resource is out of date; re-index the skill."),
    "duplicate_name": (
        409,
        "Two routed skills share a name and cannot be bundled together; activate them singly.",
    ),
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
