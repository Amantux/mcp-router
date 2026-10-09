"""Analytics API (wave 2) — admin only, camelCase wire, curated errors.

    GET  /api/v1/analytics/overview          headline cards
    GET  /api/v1/analytics/tools             per-tool funnel table (sortable)
    GET  /api/v1/analytics/tools/{tool_id}   funnel + position curve + co-surfacing
    GET  /api/v1/analytics/agents            per-agent profiles
    GET  /api/v1/analytics/suggestions       wasted exposure + staleness (suggestions only)
    POST /api/v1/analytics/rollup            recompute tool_stats_daily day(s)

Integration: `install_analytics(app)` (one line in app.py, after the
session factory exists). It includes the router and binds the Prometheus
funnel collector (idempotent across app re-creation).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request
from sqlalchemy.orm import Session

from mcprouter.analytics import service
from mcprouter.analytics.metrics import install_metrics
from mcprouter.analytics.rollup import RollupNotAllowed
from mcprouter.analytics.staleness import DEFAULT_STALE_DAYS
from mcprouter.analytics.window import (
    DEFAULT_WINDOW,
    WINDOW_PATTERN,
    InvalidWindow,
    Window,
    parse_window,
)
from mcprouter.analytics.wire import (
    AgentPageOut,
    OverviewOut,
    RollupIn,
    RollupOut,
    SuggestionsOut,
    ToolDetailOut,
    ToolFunnelPageOut,
)
from mcprouter.api.deps_auth import require_admin
from mcprouter.models import utcnow

router = APIRouter(
    prefix="/api/v1/analytics", tags=["analytics"], dependencies=[Depends(require_admin)]
)


def get_now() -> datetime:
    """Overridable clock (tests use app.dependency_overrides)."""
    return utcnow()


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as s:
        yield s


def get_window(
    now: Annotated[datetime, Depends(get_now)],
    window: Annotated[str, Query(pattern=WINDOW_PATTERN, max_length=5)] = DEFAULT_WINDOW,
) -> Window:
    try:
        return parse_window(window, now)
    except InvalidWindow as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


SessionDep = Annotated[Session, Depends(get_session)]
WindowDep = Annotated[Window, Depends(get_window)]


@router.get("/overview", response_model=OverviewOut)
def overview(session: SessionDep, window: WindowDep) -> OverviewOut:
    return service.overview(session, window)


@router.get("/tools", response_model=ToolFunnelPageOut)
def tools(
    session: SessionDep,
    window: WindowDep,
    sort: service.SortKey = "surfaced",
    order: Literal["asc", "desc"] = "desc",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    kind: service.Kind = "all",
) -> ToolFunnelPageOut:
    return service.tool_table(
        session,
        window,
        sort=sort,
        descending=order == "desc",
        limit=limit,
        offset=offset,
        kind=kind,
    )


@router.get("/tools/{tool_id}", response_model=ToolDetailOut)
def tool_detail(
    tool_id: Annotated[str, Path(max_length=42)], session: SessionDep, window: WindowDep
) -> ToolDetailOut:
    try:
        return service.tool_detail(session, window, tool_id)
    except service.ToolNotFound:
        raise HTTPException(status_code=404, detail="Unknown tool.") from None


@router.get("/agents", response_model=AgentPageOut)
def agents(session: SessionDep, window: WindowDep) -> AgentPageOut:
    return service.agent_profiles(session, window)


@router.get("/suggestions", response_model=SuggestionsOut)
def suggestions(
    session: SessionDep,
    window: WindowDep,
    min_surfaced: Annotated[int, Query(alias="minSurfaced", ge=1, le=100_000)] = 20,
    max_selection_rate: Annotated[float, Query(alias="maxSelectionRate", ge=0.0, le=1.0)] = 0.05,
    stale_days: Annotated[int, Query(alias="staleDays", ge=1, le=365)] = DEFAULT_STALE_DAYS,
) -> SuggestionsOut:
    return service.suggestions(
        session,
        window,
        min_surfaced=min_surfaced,
        max_selection_rate=max_selection_rate,
        stale_days=stale_days,
    )


@router.post("/rollup", response_model=RollupOut)
def rollup(
    session: SessionDep,
    now: Annotated[datetime, Depends(get_now)],
    body: RollupIn | None = None,
) -> RollupOut:
    body = body or RollupIn()
    try:
        out = service.run_rollup(session, now, body.day, body.days)
    except RollupNotAllowed as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    session.commit()
    return out


def install_analytics(app: FastAPI) -> None:
    app.include_router(router)
    install_metrics(app.state.session_factory)
