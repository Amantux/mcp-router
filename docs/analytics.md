# Analytics — operator guide

All metrics are read-only views over `routing_decisions`, `execution_records`,
`route_feedback` and the daily rollup (`tool_stats_daily`). Every view takes a
window (`24h`, `7d`, `30d`, ...). **Simulated decisions** (`model_version`
starting `simulated/`, from admin `/route/simulate`) are excluded everywhere:
nothing was shown to an agent.

## Funnel (per tool / skill)

`surfaced -> selected -> succeeded` per funnel id (a tool id, or `skill:<id>`).

- **surfaced**: appearances in a live decision's `selectedToolIds`.
- **selected**: surfaced (decision, tool) pairs with >= 1 terminal execution
  attributed to that decision **by the agent that owns it** (a foreign agent
  quoting someone else's decision id is ignored). `selected <= surfaced`.
- **succeeded / failed**: of the selected, any `ok` / only error or timeout.
- `selectionRate`, `successRate`, `avgRank` are `null` when the denominator is 0.
- Whole UTC days older than the live horizon are read from rollups; the rest live.
- Position curve and co-surfacing are raw-table only (not rolled up).

## Context economy

- **Measured**: route latency p50/p95, execution latency p50/p95, token counts
  of the definitions actually exposed (`exposedTokens`) versus the whole
  catalog (`catalogTokens`); `tokensNotSent` = the difference.
- **Estimated** (`estimatedTimeSavedMs`, `estimatedCostSaved`, `currency`):
  `tokensNotSent` x the operator's assumptions, echoed in `assumptions`
  (`prefillMsPer1kTokens`, `pricePer1kInputTokens`, `estimator`). `null` when
  the assumption is unset. Token counts themselves are *estimates* from
  `estimator` (not a provider tokenizer) — treat savings as order-of-magnitude.
- Skills: `skillMetadataTokens` (what is exposed), `skillBodyTokensExposed`,
  `skillBodyTokensNotSent` (bodies withheld until activation). Overview
  `skills{surfaced, activated, activationRate, bodyTokensNotSent}`.

## Feedback

Written by agents (`router.feedback` MCP tool / API, `source=agent`) and admins
(`source=human`); one verdict per (decision, target, source) — an upsert.

A row **counts** when its decision is live and inside the window (decision
time, not verdict time) and either `source=human` (any decision) or
`source=agent` **and** the row's agent owns the decision. Feedback is read
from raw tables (not rolled up). **Only counts are exposed** — notes are free
text and never leave the write path through analytics.

| Where | Fields |
|---|---|
| tool rows (`/analytics/tools`, tool detail) | `feedbackHelpful`, `feedbackUnhelpful`, `helpfulRate` (`null` when none) |
| overview | `feedback{items, helpfulRate, coverage}`; `coverage` = live decisions with >= 1 counted row / live decisions in window |
| agent profiles | `feedbackItems`, `helpfulRate` (keyed by the decision's agent; human verdicts land there) |
| wasted exposure | `unhelpful` |

**Wasted-exposure ordering** (suggestions): filter unchanged (surfaced >=
`minSurfaced` and selectionRate < `maxSelectionRate`); order by `unhelpful`
desc, then `surfaced` desc, then `selectionRate` asc (`null` as 0), then tool
id. Feedback reorders; it never admits a row the filter rejects.

**Write rate limit**: 30 feedback calls / 60 s per principal, held **in
process memory** — with N workers the effective cap is up to N x 30.

## Usage prior (not wired into routing)

`MCPR_USAGE_PRIOR_ENABLED` (default off). Without feedback:
`cap * rate * confidence` (cap 0.05, confidence saturates at 200 surfaced).
With a helpfulRate:
`clamp(cap * (0.7 * rate * confidence + 0.3 * clamp((hr - 0.5) * 2, -1, 1)), 0, cap)`.
Same cap; never negative; cold tools (surfaced 0) are exactly 0; NaN/inf
helpfulRate = no feedback.

## Environment keys

| Key | Effect |
|---|---|
| `MCPR_ANALYTICS_ROLLUP_ENABLED` | run the daily rollup scheduler in-process |
| `MCPR_USAGE_PRIOR_ENABLED` | enable `usage_prior` (strict bool parse) |
| `MCPR_PREFILL_MS_PER_1K_TOKENS` | assumption for `estimatedTimeSavedMs` |
| `MCPR_PRICE_PER_1K_INPUT_TOKENS`, `MCPR_CURRENCY` | assumptions for `estimatedCostSaved` |
