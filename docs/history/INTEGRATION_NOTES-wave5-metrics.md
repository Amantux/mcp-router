# Wave 5 — feedback analytics + prior (wave5/metrics-analytics)

Operator semantics: `docs/analytics.md` ("Feedback", "Usage prior").

## Wire additions (camelCase, all additive)

- `ToolFunnelOut`: `feedbackHelpful: int`, `feedbackUnhelpful: int`, `helpfulRate: float|null`.
- `OverviewOut.feedback`: `{items: int, helpfulRate: float|null, coverage: float|null}`.
- `AgentProfileOut`: `feedbackItems: int`, `helpfulRate: float|null`.
- `WastedOut`: `unhelpful: int`.

## Behaviour change

`wastedExposure` order was `surfaced desc, toolId`. It is now
`unhelpful desc, surfaced desc, selectionRate asc (null as 0), toolId`.
The filter is unchanged.

## Counting rule

Live decisions only, windowed on decision `created_at`; `source=human` always
counts; `source=agent` counts only when `route_feedback.agent_id` equals the
decision's agent. Counts only; notes never exposed. New module
`analytics/feedback_stats.py`; raw tables only (no rollup column).

## Prior

`usage_prior(..., helpful_rate=None)`: new keyword-only argument, default None
= identical to the previous formula. Weights `W_SELECTION=0.7`,
`W_FEEDBACK=0.3`, under the same cap. Still not wired into routing. A caller
must pass the feedback-derived helpfulRate itself.
