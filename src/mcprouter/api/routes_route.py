"""POST /api/v1/route (SPEC §9) — thin router over `RoutePipeline`.

Wire shape is SPEC §9 verbatim (snake_case; scoping.md §10 makes §9
canonical, overriding the general camelCase convention), plus `no_match`.

Seams (recorded in docs/INTEGRATION_NOTES-routing.md):

* `install_routing(app, pipeline, scope_resolver=None)` — app.py is frozen for
  this track, so integration adds ONE line calling this.
* `get_scope_resolver` — dependency returning `agent_id -> ScopeFilter`.
  !!! DEFAULT IS PERMISSIVE (AllowAllScope) !!! Authenticating `agent_id` and
  resolving its PolicyRule scope is the GATEWAY track's job; until it
  installs a resolver (or overrides this dependency), any caller can route
  as any agent over the whole catalog. Routing only *ranks* — it never
  executes — and the gateway's post-ranking authorization still decides
  execution, but do not expose this default beyond localhost.

Decision: unknown `allowed_servers` names are a 400 listing them, never
silently ignored — dropping an unknown name could turn ["githb"] into "no
restriction", widening the caller's own request.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from mcprouter.eval.dataset import DatasetError, load_named
from mcprouter.eval.runner import DEFAULT_EVAL_MAX_TOOLS, case_rows, compute_metrics, run_cases
from mcprouter.eval.store import ensure_eval_table, save_eval_result
from mcprouter.interfaces import RouteRequest, ScopeFilter
from mcprouter.routing.pipeline import RoutePipeline
from mcprouter.routing.scope import AllowAllScope
from mcprouter.routing.servers import resolve_server_names

log = logging.getLogger(__name__)

ScopeResolver = Callable[[str], ScopeFilter]

router = APIRouter(prefix="/api/v1", tags=["routing"])

MAX_QUERY_CHARS = 4000


class RouteBody(BaseModel):
    query: str = Field(max_length=MAX_QUERY_CHARS)
    agent_id: str = Field(min_length=1, max_length=120)
    max_tools: int | None = Field(default=None, ge=1, le=1000)
    allowed_servers: list[str] | None = Field(default=None, max_length=500)

    @field_validator("query")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be empty")
        return v


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


def install_routing(
    app: FastAPI, pipeline: RoutePipeline, scope_resolver: ScopeResolver | None = None
) -> None:
    app.state.route_pipeline = pipeline
    app.state.route_scope_resolver = scope_resolver
    if scope_resolver is None:
        log.warning(
            "No routing scope resolver installed — /api/v1/route uses a PERMISSIVE "
            "allow-all scope. Dev only; the gateway must install a resolver."
        )
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


def resolve_allowed(request: Request, names: list[str] | None) -> list[str] | None:
    if names is None:
        return None
    with request.app.state.session_factory() as s:
        ids, unknown = resolve_server_names(s, names)
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
    pipeline: Annotated[RoutePipeline, Depends(get_pipeline)],
    scope_resolver: Annotated[ScopeResolver, Depends(get_scope_resolver)],
) -> RouteResponse:
    settings = request.app.state.settings
    allowed_ids = resolve_allowed(request, body.allowed_servers)
    result = pipeline.route(
        RouteRequest(
            query=body.query,
            agent_id=body.agent_id,
            max_tools=body.max_tools or settings.max_exposed_tools,
            allowed_servers=allowed_ids,
        ),
        scope_resolver(body.agent_id),
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
    )


# ------------------------------------------------------------ evaluation
class EvaluateBody(BaseModel):
    dataset: str = Field(min_length=1, max_length=120)
    max_tools: int = Field(default=DEFAULT_EVAL_MAX_TOOLS, ge=1, le=50)


class EvaluateResponse(BaseModel):
    id: str
    dataset: str
    model_version: str
    case_count: int
    metrics: dict[str, Any]


@router.post("/route/evaluate", response_model=EvaluateResponse)
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
        scope_resolver=scope_resolver,
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
