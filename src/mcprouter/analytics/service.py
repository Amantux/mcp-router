"""Assembles analytics responses (the routers stay thin)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from sqlalchemy.orm import Session

from mcprouter.analytics import feedback_stats as fbs
from mcprouter.analytics import funnel as fn
from mcprouter.analytics._common import CatalogMeta, meta
from mcprouter.analytics.economy import (
    CATALOG_BASIS,
    Economy,
    SavingsBasis,
    economy_by_agent,
    overall,
)
from mcprouter.analytics.profiles import (
    catalog_drift,
    execution_latency,
    off_funnel_selections,
    profiles,
)
from mcprouter.analytics.rollup import default_last_day, recompute_range
from mcprouter.analytics.staleness import stale_report
from mcprouter.analytics.tokens import ESTIMATOR, tool_token_map
from mcprouter.analytics.window import Window, live_horizon
from mcprouter.analytics.wire import (
    AgentPageOut,
    AgentProfileOut,
    AssumptionsOut,
    CoSurfacedOut,
    EconomyOut,
    ExecutionsOut,
    FeedbackOverviewOut,
    FunnelTotalsOut,
    MeasuredOut,
    NeverRoutedServerOut,
    OverviewOut,
    RankPointOut,
    RollupDayOut,
    RollupOut,
    RoutingOut,
    SkillsOverviewOut,
    StaleToolOut,
    SuggestionsOut,
    ToolDetailOut,
    ToolFunnelOut,
    ToolFunnelPageOut,
    WastedOut,
    WindowOut,
)
from mcprouter.models import SKILL_ID_PREFIX

SortKey = Literal[
    "surfaced",
    "selected",
    "succeeded",
    "failed",
    "selectionRate",
    "successRate",
    "avgRank",
    "exposedTokens",
    "toolName",
]


class ToolNotFound(LookupError):
    pass


def _window_out(w: Window) -> WindowOut:
    return WindowOut(label=w.label, start=w.start, end=w.end)


def _economy_out(e: Economy, basis: SavingsBasis, *, per_agent: bool = False) -> EconomyOut:
    configured_time = basis.prefill_ms_per_1k_tokens > 0
    configured_cost = basis.price_per_1k_input_tokens > 0
    return EconomyOut(
        estimated_time_saved_ms=basis.time_saved_ms(e.tokens_not_sent),
        estimated_cost_saved=basis.cost_saved(e.tokens_not_sent),
        currency=basis.currency if configured_cost else None,
        assumptions=AssumptionsOut(
            prefill_ms_per_1k_tokens=basis.prefill_ms_per_1k_tokens if configured_time else None,
            price_per_1k_input_tokens=basis.price_per_1k_input_tokens if configured_cost else None,
            estimator=ESTIMATOR,
        ),
        served_decisions=e.served_decisions,
        unscored_decisions=e.unscored_decisions,
        stale_ref_decisions=e.stale_ref_decisions,
        no_match_decisions=e.no_match_decisions,
        exposed_tokens=e.exposed_tokens,
        catalog_tokens=e.catalog_tokens,
        tokens_not_sent=e.tokens_not_sent,
        savings=e.savings,
        catalog_tokens_per_decision=e.catalog_tokens_per_decision if per_agent else None,
        skill_metadata_tokens=e.skill_metadata_tokens,
        skill_body_tokens_exposed=e.skill_body_tokens_exposed,
        skill_body_tokens_not_sent=e.skill_body_tokens_not_sent,
        estimator=ESTIMATOR,
        catalog_basis=CATALOG_BASIS,
    )


def _curve_out(points: list[fn.RankPoint]) -> list[RankPointOut]:
    return [
        RankPointOut(rank=p.rank, shown=p.shown, selected=p.selected, rate=p.rate) for p in points
    ]


# Old private spellings, kept as aliases (P-609 moved them to analytics/_common.py).
_Meta = CatalogMeta
_meta = meta

Kind = Literal["tool", "skill", "all"]


def kind_of(tid: str) -> Literal["tool", "skill"]:
    """Funnel ids are kind-keyed: "skill:<skill id>" vs a bare tool id."""
    return "skill" if tid.startswith(SKILL_ID_PREFIX) else "tool"


def _tool_out(
    tid: str,
    c: fn.ToolCounts,
    meta: dict[str, _Meta],
    tokens: dict[str, int],
    fb: fbs.FeedbackCounts | None = None,
) -> ToolFunnelOut:
    m = meta.get(tid)
    fb = fb or fbs.FeedbackCounts()
    return ToolFunnelOut(
        tool_id=tid,
        kind=kind_of(tid),
        tool_name=m.name if m else None,
        server_name=m.server if m else None,
        enabled=m.enabled if m else None,
        tokens=tokens.get(tid),
        surfaced=c.surfaced,
        selected=c.selected,
        succeeded=c.succeeded,
        failed=c.failed,
        selection_rate=c.selection_rate,
        success_rate=c.success_rate,
        avg_rank=c.avg_rank,
        exposed_tokens=c.exposed_tokens,
        feedback_helpful=fb.helpful,
        feedback_unhelpful=fb.unhelpful,
        helpful_rate=fb.helpful_rate,
    )


# ------------------------------------------------------------------ overview
def overview(session: Session, window: Window, basis: SavingsBasis | None = None) -> OverviewOut:
    basis = basis or SavingsBasis()
    tokens = tool_token_map(session)
    t = fn.totals(fn.merged_funnel(session, window, tokens))
    total, _ = profiles(session, window)
    econ = overall(economy_by_agent(session, window, tokens))
    x50, x95 = execution_latency(session, window)
    fb_by = fbs.feedback_by_target(session, window).values()
    fb_total = fbs.FeedbackCounts(sum(f.helpful for f in fb_by), sum(f.unhelpful for f in fb_by))
    return OverviewOut(
        window=_window_out(window),
        context_economy=_economy_out(econ, basis),
        funnel=FunnelTotalsOut(
            surfaced=t.surfaced,
            selected=t.selected,
            succeeded=t.succeeded,
            failed=t.failed,
            selection_rate=t.selection_rate,
            success_rate=t.success_rate,
        ),
        routing=RoutingOut(
            decisions=total.decisions,
            no_match=total.no_match,
            no_match_rate=total.no_match_rate,
            fallback=total.fallback,
            fallback_rate=total.fallback_rate,
            latency_p50_ms=total.latency_p50_ms,
            latency_p95_ms=total.latency_p95_ms,
        ),
        executions=ExecutionsOut(
            attempts=total.attempts,
            denied=total.denied,
            denial_rate=total.denial_rate,
            attributed=total.attributed,
            attribution_coverage=total.attribution_coverage,
            off_funnel_selections=off_funnel_selections(session, window),
        ),
        position_curve=_curve_out(fn.position_curve(session, window)),
        skills=SkillsOverviewOut(
            surfaced=total.skills_surfaced,
            activated=total.skills_activated,
            activation_rate=total.skill_activation_rate,
            body_tokens_not_sent=econ.skill_body_tokens_not_sent,
        ),
        catalog_drift=catalog_drift(session, window),
        measured=MeasuredOut(
            route_latency_p50_ms=total.latency_p50_ms,
            route_latency_p95_ms=total.latency_p95_ms,
            execution_latency_p50_ms=x50,
            execution_latency_p95_ms=x95,
        ),
        feedback=FeedbackOverviewOut(
            items=fb_total.items,
            helpful_rate=fb_total.helpful_rate,
            coverage=fbs.feedback_coverage(session, window),
        ),
    )


# --------------------------------------------------------------------- tools
def _sort_value(row: ToolFunnelOut, key: SortKey) -> float | str | None:
    return {
        "surfaced": row.surfaced,
        "selected": row.selected,
        "succeeded": row.succeeded,
        "failed": row.failed,
        "selectionRate": row.selection_rate,
        "successRate": row.success_rate,
        "avgRank": row.avg_rank,
        "exposedTokens": row.exposed_tokens,
        "toolName": (row.tool_name or "").lower(),
    }[key]


def tool_table(
    session: Session,
    window: Window,
    *,
    sort: SortKey,
    descending: bool,
    limit: int,
    offset: int,
    kind: Kind = "all",
) -> ToolFunnelPageOut:
    """Every catalog tool (zero rows included) plus removed tools that still
    have funnel data. None values sort LAST in either direction; ties break
    on tool id for stable pagination."""
    tokens = tool_token_map(session)
    funnel = fn.merged_funnel(session, window, tokens)
    meta = _meta(session)
    fb = fbs.feedback_by_target(session, window)
    rows = [
        _tool_out(tid, funnel.get(tid, fn.ToolCounts()), meta, tokens, fb.get(tid))
        for tid in sorted(set(meta) | set(funnel))
        if kind == "all" or kind_of(tid) == kind
    ]
    present = [r for r in rows if _sort_value(r, sort) is not None]
    missing = [r for r in rows if _sort_value(r, sort) is None]
    present.sort(key=lambda r: r.tool_id)
    if sort == "toolName":
        present.sort(key=lambda r: str(_sort_value(r, sort)), reverse=descending)
    else:
        present.sort(key=lambda r: float(_sort_value(r, sort) or 0.0), reverse=descending)
    ordered = present + missing
    return ToolFunnelPageOut(
        window=_window_out(window),
        items=ordered[offset : offset + limit],
        total=len(ordered),
        limit=limit,
        offset=offset,
    )


def tool_detail(session: Session, window: Window, tool_id: str) -> ToolDetailOut:
    meta = _meta(session)
    if tool_id not in meta:
        raise ToolNotFound
    tokens = tool_token_map(session)
    counts = fn.merged_funnel(session, window, tokens).get(tool_id, fn.ToolCounts())
    co = fn.co_surfaced(session, window, tool_id)
    return ToolDetailOut(
        window=_window_out(window),
        tool=_tool_out(
            tool_id, counts, meta, tokens, fbs.feedback_by_target(session, window).get(tool_id)
        ),
        position_curve=_curve_out(fn.position_curve(session, window, tool_id)),
        co_surfaced=[
            CoSurfacedOut(
                tool_id=c.tool_id,
                kind=kind_of(c.tool_id),
                tool_name=meta[c.tool_id].name if c.tool_id in meta else None,
                server_name=meta[c.tool_id].server if c.tool_id in meta else None,
                co_surfaced=c.co_surfaced,
                this_selected=c.this_selected,
                other_selected=c.other_selected,
            )
            for c in co
        ],
    )


# -------------------------------------------------------------------- agents
def agent_profiles(
    session: Session, window: Window, basis: SavingsBasis | None = None
) -> AgentPageOut:
    basis = basis or SavingsBasis()
    tokens = tool_token_map(session)
    _, per_agent = profiles(session, window)
    econ = economy_by_agent(session, window, tokens)
    fb_agent = fbs.feedback_by_agent(session, window)
    items = []
    for agent in sorted(set(per_agent) | set(econ)):
        p = per_agent.get(agent)
        if p is None:  # pragma: no cover - econ agents always routed => profiled
            continue
        items.append(
            AgentProfileOut(
                agent_id=agent,
                decisions=p.decisions,
                skills_surfaced=p.skills_surfaced,
                skills_activated=p.skills_activated,
                skill_activation_rate=p.skill_activation_rate,
                no_match=p.no_match,
                no_match_rate=p.no_match_rate,
                fallback=p.fallback,
                fallback_rate=p.fallback_rate,
                latency_p50_ms=p.latency_p50_ms,
                latency_p95_ms=p.latency_p95_ms,
                attempts=p.attempts,
                denied=p.denied,
                denial_rate=p.denial_rate,
                attributed=p.attributed,
                attribution_coverage=p.attribution_coverage,
                surfaced=p.surfaced,
                selected=p.selected,
                selection_rate=p.selection_rate,
                avg_surfaced_per_decision=p.avg_surfaced_per_decision,
                context_economy=_economy_out(econ.get(agent, Economy()), basis, per_agent=True),
                feedback_items=fb_agent.get(agent, fbs.FeedbackCounts()).items,
                helpful_rate=fb_agent.get(agent, fbs.FeedbackCounts()).helpful_rate,
            )
        )
    return AgentPageOut(window=_window_out(window), items=items)


# --------------------------------------------------------------- suggestions
def suggestions(
    session: Session,
    window: Window,
    *,
    min_surfaced: int,
    max_selection_rate: float,
    stale_days: int,
    limit: int = 50,
) -> SuggestionsOut:
    tokens = tool_token_map(session)
    meta = _meta(session)
    wasted = fn.wasted_exposure(
        fn.merged_funnel(session, window, tokens),
        min_surfaced=min_surfaced,
        max_selection_rate=max_selection_rate,
        unhelpful={t: f.unhelpful for t, f in fbs.feedback_by_target(session, window).items()},
    )[:limit]
    stale, never = stale_report(session, window.end, stale_days)
    return SuggestionsOut(
        window=_window_out(window),
        min_surfaced=min_surfaced,
        max_selection_rate=max_selection_rate,
        stale_days=stale_days,
        wasted_exposure=[
            WastedOut(
                tool_id=w.tool_id,
                kind=kind_of(w.tool_id),
                tool_name=meta[w.tool_id].name if w.tool_id in meta else None,
                server_name=meta[w.tool_id].server if w.tool_id in meta else None,
                surfaced=w.counts.surfaced,
                selected=w.counts.selected,
                selection_rate=w.counts.selection_rate,
                exposed_tokens=w.counts.exposed_tokens,
                unhelpful=w.unhelpful,
            )
            for w in wasted
        ],
        stale_tools=[
            StaleToolOut(
                tool_id=s.tool_id,
                tool_name=s.tool_name,
                server_name=s.server_name,
                created_at=s.created_at,
                last_surfaced_at=s.last_surfaced_at,
            )
            for s in stale[:limit]
        ],
        never_routed_servers=[
            NeverRoutedServerOut(
                server_id=n.server_id,
                server_name=n.server_name,
                tool_count=n.tool_count,
                created_at=n.created_at,
            )
            for n in never[:limit]
        ],
    )


# -------------------------------------------------------------------- rollup
def run_rollup(session: Session, now: datetime, day: date | None, days: int) -> RollupOut:
    last = day if day is not None else default_last_day(now)
    results = recompute_range(session, last, days, now)
    return RollupOut(
        live_horizon=live_horizon(now),
        days=[RollupDayOut(day=r.day, tool_rows=r.tool_rows) for r in results],
    )
