"""POST /api/v1/route (SPEC §9) — thin router over `RoutePipeline`.

Wire shape is SPEC §9 verbatim (snake_case; scoping.md §10 makes §9
canonical, overriding the general camelCase convention), plus `no_match`.

Seams (recorded in docs/INTEGRATION_NOTES-routing.md):

* `install_routing(app, pipeline, scope_resolver=None)` — app.py is frozen for
  this track, so integration adds ONE line calling this.
* `get_scope_resolver` — dependency returning `agent_id -> ScopeFilter`.
  The integrated app installs `policy.scope.policy_scope_resolver` (backed by
  `policy.engine.evaluate`); the permissive AllowAllScope default remains only
  for an app that never installs one (logged loudly).

Integration (identity + redaction):

* `/route` authenticates with the gateway's `get_principal` and routes for the
  AUTHENTICATED agent. The body's `agent_id` (SPEC §9) may be absent or equal
  to it; a different value is identity spoofing -> 403.
* The query is passed through `execution.redaction.redact()` BEFORE the
  pipeline, so neither decision-model inputs nor the persisted
  RoutingDecisionRecord ever see secret-shaped substrings.
* After routing, the result is published to the agent's MCP sessions via
  `app.state.gateway.apply_route_threadsafe` (tools/list_changed).
* Request bodies accept snake_case (SPEC §9) and camelCase (UI). The
  response stays SPEC §9 snake_case.
* `/route/evaluate` is admin-only (gateway's `require_admin`).
* `/route/simulate` (admin-only, wave 2) routes AS a named agent under its
  live scope and budgets and returns admin diagnostics (candidates, per-stage
  prunes, policy-filtered items with reasons, budget clamps). It never
  publishes exposure, never executes, and bypasses the route cache.
* Budgets: request maxTools/maxServers may lower, never raise, the principal
  and global caps (routing.budgets); /route reports the applied values.

Decision: unknown `allowed_servers` names are a 400 listing them, never
silently ignored — dropping an unknown name could turn ["githb"] into "no
restriction", widening the caller's own request.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from pydantic.alias_generators import to_camel
from sqlalchemy import select

from mcprouter.api.deps_auth import (
    DEV_AGENT_ID,
    _dev_mode_active,
    dev_principal,
    get_principal,
    require_admin,
)
from mcprouter.eval.dataset import DatasetError, load_named
from mcprouter.eval.runner import DEFAULT_EVAL_MAX_TOOLS, case_rows, compute_metrics, run_cases
from mcprouter.eval.store import ensure_eval_table, save_eval_result
from mcprouter.execution.redaction import redact
from mcprouter.interfaces import RouteRequest, ScopeFilter, ToolCandidate
from mcprouter.models import AgentPrincipal
from mcprouter.routing.budgets import effective_budgets
from mcprouter.routing.pipeline import RoutePipeline
from mcprouter.routing.retriever import ensure_keyword_index, ensure_skill_keyword_index
from mcprouter.routing.scope import AllowAllScope, UncachedScope
from mcprouter.routing.servers import resolve_server_names
from mcprouter.routing.trace import RouteTrace

log = logging.getLogger(__name__)

ScopeResolver = Callable[[str], ScopeFilter]

router = APIRouter(prefix="/api/v1", tags=["routing"])

MAX_QUERY_CHARS = 4000
# C0 controls except tab/newline/CR, plus DEL. NUL in particular makes
# psycopg reject the decision-row insert AFTER the model already ran.
_CTRL_QUERY = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CTRL_ANY = re.compile(r"[\x00-\x1f\x7f]")

ServerName = Annotated[str, StringConstraints(min_length=1, max_length=120)]


class _Body(BaseModel):
    # Both casings: SPEC §9 snake_case and the UI's camelCase.
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RouteBody(_Body):
    query: str = Field(max_length=MAX_QUERY_CHARS)
    # Optional: identity comes from the credential. If present it must match.
    agent_id: str | None = Field(default=None, min_length=1, max_length=120)
    max_tools: int | None = Field(default=None, ge=1, le=1000)
    # Distinct-server budget; may only LOWER the principal/global caps.
    max_servers: int | None = Field(default=None, ge=1, le=1000)
    allowed_servers: list[ServerName] | None = Field(default=None, max_length=500)

    @field_validator("query")
    @classmethod
    def _query_ok(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be empty")
        if _CTRL_QUERY.search(v):
            raise ValueError("query must not contain control characters")
        return v

    @field_validator("agent_id")
    @classmethod
    def _agent_ok(cls, v: str | None) -> str | None:
        if v is not None and _CTRL_ANY.search(v):
            raise ValueError("agent_id must not contain control characters")
        return v

    @field_validator("allowed_servers")
    @classmethod
    def _servers_ok(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and any(_CTRL_ANY.search(n) for n in v):
            raise ValueError("allowed_servers names must not contain control characters")
        return v


async def _curated_validation_error(request: Request, exc: Exception) -> Response:
    """Routing paths: 422 WITHOUT echoing input (the query may carry secrets).
    Every other path keeps FastAPI's default behaviour unchanged."""
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registration guard
        raise exc
    if not request.url.path.startswith(router.prefix + "/route"):
        return await request_validation_exception_handler(request, exc)
    detail = [
        {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", ""))[:200], "type": e.get("type")}
        for e in exc.errors()[:20]
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


class RoutedToolOut(BaseModel):
    server: str
    tool: str
    score: float


class RouteResponse(BaseModel):
    request_id: str
    tools: list[RoutedToolOut]
    fallback_used: bool
    latency_ms: float
    no_match: bool
    # Effective budgets after the clamp chain (routing.budgets); null
    # max_servers_applied = no distinct-server cap anywhere.
    max_tools_applied: int
    max_servers_applied: int | None
    # Served from the route cache (authorization was re-checked on the hit).
    cached: bool


def install_routing(
    app: FastAPI, pipeline: RoutePipeline, scope_resolver: ScopeResolver | None = None
) -> None:
    ensure_keyword_index(app.state.engine)  # keyword-leg GIN index (idempotent)
    ensure_skill_keyword_index(app.state.engine)  # skills twin (idempotent)
    app.state.route_pipeline = pipeline
    app.state.route_scope_resolver = scope_resolver
    if scope_resolver is None:
        log.warning(
            "No routing scope resolver installed — /api/v1/route uses a PERMISSIVE "
            "allow-all scope. Dev only; the gateway must install a resolver."
        )
    app.add_exception_handler(RequestValidationError, _curated_validation_error)
    app.include_router(router)


def get_pipeline(request: Request) -> RoutePipeline:
    pipeline: RoutePipeline | None = getattr(request.app.state, "route_pipeline", None)
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Routing is not configured on this instance.")
    return pipeline


def _allow_all(agent_id: str) -> ScopeFilter:
    # PERMISSIVE DEV DEFAULT — see module docstring. Not an authz decision.
    return AllowAllScope()


def get_scope_resolver(request: Request) -> ScopeResolver:
    resolver: ScopeResolver | None = getattr(request.app.state, "route_scope_resolver", None)
    return resolver or _allow_all


def resolve_allowed(
    request: Request, names: list[str] | None, scope: ScopeFilter
) -> list[str] | None:
    """Out-of-scope names are reported exactly like unknown ones, so the 400
    is not an existence oracle for servers the caller may not see."""
    if names is None:
        return None
    with request.app.state.session_factory() as s:
        ids, unknown = resolve_server_names(s, names)
    known = [n for n in dict.fromkeys(names) if n not in unknown]
    visible = scope.server_ids()
    if visible is not None:
        in_scope = set(visible)
        hidden = {n for n, sid in zip(known, ids, strict=True) if sid not in in_scope}
        unknown = [n for n in dict.fromkeys(names) if n in hidden or n in unknown]
        ids = [sid for n, sid in zip(known, ids, strict=True) if n not in hidden]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Unknown server name(s) in allowed_servers; "
                "check GET /api/v1/servers for registered names.",
                "unknown_servers": unknown[:50],
            },
        )
    return ids


@router.post("/route", response_model=RouteResponse)
def route(
    body: RouteBody,
    request: Request,
    principal: Annotated[AgentPrincipal, Depends(get_principal)],
    pipeline: Annotated[RoutePipeline, Depends(get_pipeline)],
    scope_resolver: Annotated[ScopeResolver, Depends(get_scope_resolver)],
) -> RouteResponse:
    settings = request.app.state.settings
    agent_id = principal.agent_id
    if body.agent_id is not None and body.agent_id != agent_id:
        # Never route as a body-named agent: identity comes from the credential.
        raise HTTPException(
            status_code=403, detail="agent_id does not match the authenticated agent"
        )
    scope = scope_resolver(agent_id)
    allowed_ids = resolve_allowed(request, body.allowed_servers, scope)
    # Request may lower a budget, never raise it past the principal or global cap.
    budgets = effective_budgets(
        principal,
        settings,
        requested_tools=body.max_tools,
        requested_servers=body.max_servers,
    )
    result = pipeline.route(
        RouteRequest(
            query=redact(body.query),  # model input AND the persisted decision row
            agent_id=agent_id,
            max_tools=budgets.max_tools,
            allowed_servers=allowed_ids,
            max_servers=budgets.max_servers,
        ),
        scope,
    )
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is not None:
        # Publish to the agent's MCP sessions (tools/list_changed). Sync
        # endpoint => we are on an anyio worker thread.
        try:
            gateway.apply_route_threadsafe(agent_id, result)
        except Exception as exc:  # noqa: BLE001 — publish is best effort; the route stands
            log.warning(
                "route exposure publish failed request_id=%s exc_type=%s",
                result.request_id,
                type(exc).__name__,
            )
    return RouteResponse(
        request_id=result.request_id,
        tools=[
            RoutedToolOut(server=t.server_name, tool=t.tool_name, score=t.score)
            for t in result.tools
        ],
        fallback_used=result.fallback_used,
        latency_ms=round(result.latency_ms, 3),
        no_match=result.no_match,
        max_tools_applied=budgets.max_tools,
        max_servers_applied=budgets.max_servers,
        cached=result.cached,
    )


# ------------------------------------------------------------- simulation
# Admin-only. camelCase on the wire (management/UI convention), unlike the
# SPEC §9 /route response. Shape is a contract with the UI simulator:
# docs/INTEGRATION_NOTES-wave2-budgets.md §4 — add fields, never rename.
class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SimulateBody(RouteBody):
    # Required here: the admin names the agent whose scope + budgets to use.
    agent_id: str = Field(min_length=1, max_length=120)


class ToolRefOut(_Wire):
    tool_id: str
    server: str
    tool: str


class SimulatedToolOut(ToolRefOut):
    score: float


class CandidateOut(ToolRefOut):
    domain: str | None
    operation: str
    retrieval_score: float
    matched_on: list[str]


class StageOut(_Wire):
    stage: str
    before: int
    after: int
    pruned: list[ToolRefOut]
    detail: dict[str, Any]


class PolicyFilteredOut(ToolRefOut):
    operation: str
    reason: str


class BudgetClampOut(_Wire):
    budget: str
    requested: int | None
    principal: int | None
    global_cap: int | None
    applied: int | None
    clamped_by: str | None


class DiagnosticsOut(_Wire):
    candidates_considered: list[CandidateOut]
    stages: list[StageOut]
    policy_filtered: list[PolicyFilteredOut]
    budget_clamps: list[BudgetClampOut]


class SimulateResponse(_Wire):
    request_id: str
    agent_id: str
    simulated: bool
    tools: list[SimulatedToolOut]
    no_match: bool
    fallback_used: bool
    latency_ms: float
    model_version: str
    max_tools_applied: int
    max_servers_applied: int | None
    diagnostics: DiagnosticsOut


def _ref(c: ToolCandidate) -> ToolRefOut:
    return ToolRefOut(tool_id=c.tool_id, server=c.server_name, tool=c.tool_name)


def _simulation_principal(request: Request, agent_id: str) -> AgentPrincipal:
    security = request.app.state.security
    principal: AgentPrincipal | None
    with request.app.state.session_factory() as s:
        principal = s.scalars(
            select(AgentPrincipal).where(AgentPrincipal.agent_id == agent_id)
        ).one_or_none()
        if principal is None and agent_id == DEV_AGENT_ID and _dev_mode_active(s, security):
            principal = dev_principal(security)
    if principal is None:
        raise HTTPException(status_code=404, detail="agentId does not name a principal")
    return principal


@router.post(
    "/route/simulate",
    response_model=SimulateResponse,
    dependencies=[Depends(require_admin)],
)
def simulate(
    body: SimulateBody,
    request: Request,
    pipeline: Annotated[RoutePipeline, Depends(get_pipeline)],
    scope_resolver: Annotated[ScopeResolver, Depends(get_scope_resolver)],
) -> SimulateResponse:
    """Route AS `agentId` (its live scope and budgets) and explain the result.
    Never publishes exposure to any session, never executes, never touches
    the route cache; the decision row is marked `simulated/`."""
    settings = request.app.state.settings
    principal = _simulation_principal(request, body.agent_id)
    scope = scope_resolver(principal.agent_id)
    allowed_ids = resolve_allowed(request, body.allowed_servers, scope)
    budgets = effective_budgets(
        principal,
        settings,
        requested_tools=body.max_tools,
        requested_servers=body.max_servers,
    )
    trace = RouteTrace()
    result = pipeline.route(
        RouteRequest(
            query=redact(body.query),
            agent_id=principal.agent_id,
            max_tools=budgets.max_tools,
            allowed_servers=allowed_ids,
            max_servers=budgets.max_servers,
        ),
        scope,
        trace=trace,
    )
    return SimulateResponse(
        request_id=result.request_id,
        agent_id=principal.agent_id,
        simulated=True,
        tools=[
            SimulatedToolOut(
                tool_id=t.tool_id, server=t.server_name, tool=t.tool_name, score=t.score
            )
            for t in result.tools
        ],
        no_match=result.no_match,
        fallback_used=result.fallback_used,
        latency_ms=round(result.latency_ms, 3),
        model_version=result.model_version,
        max_tools_applied=budgets.max_tools,
        max_servers_applied=budgets.max_servers,
        diagnostics=DiagnosticsOut(
            candidates_considered=[
                CandidateOut(
                    tool_id=c.tool_id,
                    server=c.server_name,
                    tool=c.tool_name,
                    domain=c.domain,
                    operation=c.operation,
                    retrieval_score=round(c.retrieval_score, 6),
                    matched_on=list(c.matched_on),
                )
                for c in trace.candidates
            ],
            stages=[
                StageOut(
                    stage=st.stage,
                    before=st.before,
                    after=st.after,
                    pruned=[_ref(c) for c in st.pruned],
                    detail=st.detail,
                )
                for st in trace.stages
            ],
            policy_filtered=[
                PolicyFilteredOut(
                    tool_id=pf.candidate.tool_id,
                    server=pf.candidate.server_name,
                    tool=pf.candidate.tool_name,
                    operation=pf.candidate.operation,
                    reason=pf.reason,
                )
                for pf in trace.policy_filtered
            ],
            budget_clamps=[
                BudgetClampOut(
                    budget=c.budget,
                    requested=c.requested,
                    principal=c.principal,
                    global_cap=c.global_cap,
                    applied=c.applied,
                    clamped_by=c.clamped_by,
                )
                for c in budgets.clamps
            ],
        ),
    )


# ------------------------------------------------------------ evaluation
class EvaluateBody(_Body):
    dataset: str = Field(min_length=1, max_length=120)
    max_tools: int = Field(default=DEFAULT_EVAL_MAX_TOOLS, ge=1, le=50)


class EvaluateResponse(BaseModel):
    id: str
    dataset: str
    model_version: str
    case_count: int
    metrics: dict[str, Any]


@router.post(
    "/route/evaluate", response_model=EvaluateResponse, dependencies=[Depends(require_admin)]
)
def evaluate(
    body: EvaluateBody,
    request: Request,
    pipeline: Annotated[RoutePipeline, Depends(get_pipeline)],
    scope_resolver: Annotated[ScopeResolver, Depends(get_scope_resolver)],
) -> EvaluateResponse:
    """Run a named, server-shipped dataset against the LIVE pipeline + scope
    resolver and store the result. Dataset names are whitelisted (no paths)."""
    try:
        cases = load_named(body.dataset)
    except DatasetError:
        raise HTTPException(status_code=404, detail="Unknown dataset.") from None
    factory = request.app.state.session_factory
    outcomes = run_cases(
        pipeline,
        cases,
        session_factory=factory,
        # Uncached: a re-run within the TTL must measure the pipeline.
        scope_resolver=lambda agent_id: UncachedScope(scope_resolver(agent_id)),
        max_tools=body.max_tools,
    )
    metrics = compute_metrics(outcomes)
    ensure_eval_table(request.app.state.engine)
    with factory() as s:
        rid = save_eval_result(
            s,
            dataset=body.dataset,
            model_version=pipeline.model_name,
            metrics=metrics,
            cases=case_rows(outcomes),
        )
        s.commit()
    return EvaluateResponse(
        id=rid,
        dataset=body.dataset,
        model_version=pipeline.model_name,
        case_count=len(outcomes),
        metrics=metrics,
    )
