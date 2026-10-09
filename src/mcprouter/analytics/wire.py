"""Analytics wire models (camelCase out; documented verbatim in
docs/history/INTEGRATION_NOTES-wave2-analytics.md for the UI track)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class WindowOut(Wire):
    label: str
    start: datetime
    end: datetime


class AssumptionsOut(Wire):
    # explicit aliases: to_camel would emit "Per1K" (docs say "Per1k")
    prefill_ms_per_1k_tokens: float | None = Field(serialization_alias="prefillMsPer1kTokens")
    price_per_1k_input_tokens: float | None = Field(serialization_alias="pricePer1kInputTokens")
    estimator: str


class EconomyOut(Wire):
    served_decisions: int
    unscored_decisions: int
    stale_ref_decisions: int
    no_match_decisions: int
    exposed_tokens: int
    catalog_tokens: int
    tokens_not_sent: int
    savings: float | None
    catalog_tokens_per_decision: int | None = None
    skill_metadata_tokens: int = 0
    skill_body_tokens_exposed: int = 0
    skill_body_tokens_not_sent: int = 0
    estimator: str
    catalog_basis: str
    # ESTIMATES derived from tokens_not_sent x operator rates (never measured).
    estimated_time_saved_ms: float | None = None
    estimated_cost_saved: float | None = None
    currency: str | None = None
    assumptions: AssumptionsOut


class MeasuredOut(Wire):
    """MEASURED latencies (recorded per row), kept apart from estimates."""

    route_latency_p50_ms: float | None
    route_latency_p95_ms: float | None
    execution_latency_p50_ms: float | None
    execution_latency_p95_ms: float | None


class FunnelTotalsOut(Wire):
    surfaced: int
    selected: int
    succeeded: int
    failed: int
    selection_rate: float | None
    success_rate: float | None


class RoutingOut(Wire):
    decisions: int
    no_match: int
    no_match_rate: float | None
    fallback: int
    fallback_rate: float | None
    latency_p50_ms: float | None
    latency_p95_ms: float | None


class ExecutionsOut(Wire):
    attempts: int
    denied: int
    denial_rate: float | None
    attributed: int
    attribution_coverage: float | None
    off_funnel_selections: int


class RankPointOut(Wire):
    rank: int
    shown: int
    selected: int
    rate: float | None


class SkillsOverviewOut(Wire):
    surfaced: int  # (decision, skill) pairs surfaced
    activated: int  # of those, activated on that decision (attributed)
    activation_rate: float | None
    body_tokens_not_sent: int  # = contextEconomy.skillBodyTokensNotSent


class FeedbackOverviewOut(Wire):
    items: int  # counted route_feedback rows (live decisions, ownership-guarded)
    helpful_rate: float | None  # helpful / items; None when items == 0
    coverage: float | None  # live decisions with >=1 feedback / live decisions


class OverviewOut(Wire):
    window: WindowOut
    context_economy: EconomyOut
    funnel: FunnelTotalsOut
    routing: RoutingOut
    executions: ExecutionsOut
    position_curve: list[RankPointOut]
    catalog_drift: dict[str, int]
    skills: SkillsOverviewOut
    measured: MeasuredOut
    feedback: FeedbackOverviewOut


class ToolFunnelOut(Wire):
    tool_id: str  # tool id, or "skill:<skill id>" for a skill
    kind: Literal["tool", "skill"]  # from the id prefix; skills: name=skill, server=source
    tool_name: str | None  # None: tool no longer in the catalog
    server_name: str | None
    enabled: bool | None
    tokens: int | None  # current estimated definition tokens
    surfaced: int
    selected: int
    succeeded: int
    failed: int
    selection_rate: float | None
    success_rate: float | None
    avg_rank: float | None
    exposed_tokens: int
    feedback_helpful: int = 0
    feedback_unhelpful: int = 0
    helpful_rate: float | None = None  # None when no feedback


class ToolFunnelPageOut(Wire):
    window: WindowOut
    items: list[ToolFunnelOut]
    total: int
    limit: int
    offset: int


class CoSurfacedOut(Wire):
    tool_id: str
    kind: Literal["tool", "skill"]
    tool_name: str | None
    server_name: str | None
    co_surfaced: int
    this_selected: int
    other_selected: int


class ToolDetailOut(Wire):
    window: WindowOut
    tool: ToolFunnelOut
    position_curve: list[RankPointOut]
    co_surfaced: list[CoSurfacedOut]


class AgentProfileOut(Wire):
    agent_id: str
    decisions: int
    no_match: int
    no_match_rate: float | None
    fallback: int
    fallback_rate: float | None
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    attempts: int
    denied: int
    denial_rate: float | None
    attributed: int
    attribution_coverage: float | None
    surfaced: int
    selected: int
    skills_surfaced: int
    skills_activated: int
    skill_activation_rate: float | None
    selection_rate: float | None
    avg_surfaced_per_decision: float | None
    budget_tools: int | None = None  # not persisted per decision yet
    budget_utilization: float | None = None
    feedback_items: int = 0
    helpful_rate: float | None = None
    context_economy: EconomyOut


class AgentPageOut(Wire):
    window: WindowOut
    items: list[AgentProfileOut]


class WastedOut(Wire):
    tool_id: str
    kind: Literal["tool", "skill"]
    tool_name: str | None
    server_name: str | None
    surfaced: int
    selected: int
    selection_rate: float | None
    exposed_tokens: int
    unhelpful: int = 0


class StaleToolOut(Wire):
    tool_id: str
    tool_name: str
    server_name: str
    created_at: datetime
    last_surfaced_at: datetime | None


class NeverRoutedServerOut(Wire):
    server_id: str
    server_name: str
    tool_count: int
    created_at: datetime


class SuggestionsOut(Wire):
    window: WindowOut
    min_surfaced: int
    max_selection_rate: float
    stale_days: int
    wasted_exposure: list[WastedOut]
    stale_tools: list[StaleToolOut]
    never_routed_servers: list[NeverRoutedServerOut]


class RollupIn(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    # newest day to recompute; default = newest eligible
    day: date | None = Field(default=None, ge=date(2000, 1, 1))
    days: int = Field(default=1, ge=1, le=90)


class RollupDayOut(Wire):
    day: date
    tool_rows: int


class RollupOut(Wire):
    live_horizon: date
    days: list[RollupDayOut]
