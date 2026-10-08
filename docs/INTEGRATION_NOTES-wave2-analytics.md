# Integration notes: wave 2, analytics track (wave2/analytics)

This track adds the efficiency and funnel layer: execution-to-decision
attribution, the per-tool funnel, context economy, per-agent profiles,
duplicate-review evidence, staleness, daily rollups, Prometheus totals and a
bounded usage prior. It also closes gap 5 from the wave-1 integration notes
(two usage-stat writers).

## 1. Commits

| Commit | What |
|---|---|
| `fix(stats)` | The execution manager now delegates to `registry.stats.record_execution` (EMA, alpha 0.2). The running-mean duplicate is deleted. |
| `feat(analytics): attribute` | Adds `ExecutionRecord.route_request_id`, the `ExecutionManager.execute(..., route_request_id=)` keyword, the `tool_stats_daily` table, and idempotent ALTER and index statements. |
| `feat(analytics): funnel…` | The `mcprouter.analytics` package, `api/routes_analytics.py`, and the Prometheus collector. |
| `feat(dedup)` | Adds the read-only `usageEvidence` field to open duplicate suggestions. |

## 2. Integration: one line in app.py

Add this after the management routers. It needs `app.state.session_factory`.

```python
from mcprouter.api.routes_analytics import install_analytics
install_analytics(app)   # /api/v1/analytics/* (admin) + Prometheus funnel collector
```

The call is idempotent across app re-creation. The collector is registered
once per process and re-bound to the newest app's session factory. It
publishes through the existing `/metrics` mount.

## 3. Seam with the routing/gateway track (A)

`ExecutionManager.execute(principal, tool, arguments, *, route_request_id: str | None = None)`

* Pass the `RouteResult.request_id` (which equals `RoutingDecisionRecord.id`)
  of the decision whose exposure contained the called tool. Feature-detect
  it like this:
  ```python
  kw = {"route_request_id": rid} if "route_request_id" in inspect.signature(mgr.execute).parameters else {}
  ```
* The value is recorded on every audit row of the attempt: refusals,
  `started` to final, and `pending_approval`. It is telemetry only and never
  touches authorization.
* A malformed value is dropped, never fatal. Anything other than
  `[0-9A-Za-z-]{1,36}` is stored as NULL so that the audit write, and so the
  call, cannot fail.
* An absent value means unattributed. Every analytics query handles NULL:
  such rows are outside the funnel and are counted only in
  `attributionCoverage`.
* `approve()` replays are unattributed, because `ApprovalRequest` does not
  carry the id. The original `pending_approval` row is attributed and counts
  as the selection.

**What "rank" means (verified in `routing/pipeline.py::route`).**
`selected_tool_ids` is written from `tools`, which is the list after these
steps:

1. Sort by final blended score, descending. Ties break on server name, then
   tool name.
2. Truncate to `clamp(max_tools)`.
3. Run the scope re-check, which drops entries but never reorders them.

So the 1-based index in `selected_tool_ids` is the exposed rank. `scores` is
a dict built in the same order, but analytics reads rank only from the list.
Two consequences:

* On a model fallback, rank is the retrieval-score order.
* On `no_match`, the list is empty.

### Proposals for A (not done here: these are A's and shared files)

1. **Persist the retrieved stage.** Add a `candidate_tool_ids` JSON column on
   `RoutingDecisionRecord`. `_persist` writes `[c.tool_id for c in
   candidates]` in retrieval order. That is at most `retrieval_candidates`
   (20) UUIDs, about 800 bytes per row. Without it the funnel starts at
   *surfaced*: there is no "retrieved" stage today.
2. **Persist the budget.** Add `max_tools_applied` (int), the clamp actually
   used. The agent profile then fills `budgetTools` and `budgetUtilization`,
   which are always `null` today.
3. **Usage prior (optional, later).** `analytics.prior.usage_prior(selected,
   surfaced, enabled=prior_enabled())` returns a value in `[0, cap]`, with
   cap 0.05. It is meant as an additive nudge on the blended score. It is not
   wired.

## 4. Settings to promote

`MCPR_USAGE_PRIOR_ENABLED` is read directly in `analytics/prior.py`. Truthy
values are 1, true, yes and on; an empty string means unset; the default is
off. Promote it to `Settings` when A owns settings.py again.

## 5. Schema

* `execution_records.route_request_id VARCHAR(36) NULL` and
  `ix_execution_records_route_request_id`. New databases get both through
  `create_all`. Existing databases get them through `db._ADDITIVE_COLUMNS`
  (`ADD COLUMN IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`).
* `tool_stats_daily`: primary key `(tool_id, day)`, an index on `day`, and
  no foreign key on `tool_id` (history outlives a deleted tool). Columns:
  * `surfaced`, `selected`, `succeeded`, `failed`
  * `sum_rank` (sum of 1-based ranks)
  * `exposed_tokens`
  * `computed_at`

  `create_all` creates it.
* The Alembic baseline must include both.

## 6. Definitions and approximations

These are stated honestly so the UI can show them.

**Funnel** (`analytics/funnel.py`). Every count is anchored on the decision's
time.

| Stage | Definition |
|---|---|
| surfaced | Appearances in `selected_tool_ids`. |
| selected | Distinct (decision, tool) pairs that have at least one attributed execution row (any outcome except `started`) for a tool that decision surfaced. Repeated calls count once, so `selected <= surfaced`. Denied, rate-limited, invalid, pending and cancelled attempts are selections too: the agent chose the tool. |
| succeeded | The pair has at least one `ok`. |
| failed | The pair has an `error` or `timeout` and no `ok`. |

* Rates are `null` when the denominator is 0.
* An attributed call to a tool that the referenced decision did NOT surface
  is an **off-funnel selection**. It is reported in the overview, not in the
  funnel.

**Position bias.** The curve is P(selected | rank), computed only over
decisions with at least one attributed execution. A decision nobody acted
on, or one whose calls were not attributed, carries no rank-preference
signal. It is computed from raw tables over the window and is not rolled up.

**Token estimate** (`analytics/tokens.py`). It is `ceil(chars/4)` over the
compact, key-sorted JSON of `{"name": "server.tool", "description",
"inputSchema"}`, rendered the way the gateway's `tools/list` renders it
(including the object-schema substitution).

* It is a heuristic, not a tokenizer. Real BPE counts typically land within
  roughly ±25% of it, depending on the model.
* The savings ratio is much less sensitive than the absolute numbers,
  because both sides use the same estimator.
* It excludes the `find_tools` meta-tool, approval suffixes, MCP framing and
  redaction length changes.
* It uses current tool definitions. A deleted tool counts 0.

**Context economy** (`analytics/economy.py`).

* Per served decision (one that surfaced at least one tool):
  * `exposed` is the sum of token estimates for the surfaced tools.
  * `catalog` is the token estimate of the agent's authorized catalog.
* Aggregates:
  * `savings = 1 - Σexposed / Σcatalog`
  * `tokensNotSent = Σcatalog - Σexposed`

The raw components are always returned with the ratio.

* **Approximation: the authorized catalog is the CURRENT scope, not the
  scope at decision time.** It is computed from the agent's current
  principal and rules, using `policy.engine.evaluate`, over tools that are
  enabled and available on enabled servers. A rule change re-prices old
  decisions. This can make `exposed > catalog` (a negative `tokensNotSent`)
  if scope shrank since. We report that rather than clamp it.
* An agent id without a principal row (dev) is evaluated as an enabled
  principal against its rules.
* An agent whose current scope is empty has its served decisions counted in
  `unscoredDecisions`, and they are excluded from the ratio.
* No-match decisions are excluded from savings, because a miss is not a
  saving. They are counted in `noMatchDecisions`.
* The counterfactual is "without the router, this turn's context would carry
  the whole authorized catalog".

**Profiles.**

* `denialRate` = `denied` / attempts. Attempts are all execution rows except
  `started`, windowed by the execution's time and including unattributed
  rows.
* `avgSurfacedPerDecision` excludes no-match decisions.
* Budget fields are `null` (see §3).

**Windows and rollups.**

* The `window` parameter is `<n>h` or `<n>d`, from 1 to 365 days; the
  default is `7d`.
* The live horizon is UTC midnight of `now - 48h`. Raw tables are always used
  from the horizon on. Rollup rows are used only for whole UTC days that are
  fully inside the window and before the horizon. A day without rollup rows
  is computed live, so no marker table is needed. A partial first day is
  always computed live.
* The rollup and live paths run the same SQL. A test asserts that
  merged equals live.
* Rollups accelerate only the per-tool funnel and the all-time Prometheus
  totals. Position, co-surfacing, economy and profiles always read raw
  tables, bounded by the window.
* A late attributed execution, arriving more than 48h after its decision's
  day was rolled up, is counted only after that day is recomputed.
* `exposed_tokens` in a rollup row is frozen at recompute time.

**Staleness** (suggestions only).

* A tool is stale when it is enabled, at least `staleDays` old, and has not
  been surfaced in `staleDays`, or ever.
* A server is never-routed when it is enabled, at least `staleDays` old, has
  tools, and none of them has ever been surfaced.
* "Last surfaced" is the newer of the raw log (an all-time scan) and the
  rollups.

## 7. Wire shapes (camelCase)

C builds against these.

Rules for every endpoint:

* **Admin only.** Every endpoint uses `require_admin` from `deps_auth` at the
  router level. No credential, or an agent key, gets 401. An admin token gets
  200.
* **Window.** Every GET takes `?window=7d`. A malformed value gets 422.
* **Types.** All timestamps are ISO-8601 UTC. A `float | null` field is
  `null` when its denominator is 0.

```jsonc
// shared
Window   = {"label": "7d", "start": "2026-10-01T12:00:00Z", "end": "2026-10-08T12:00:00Z"}
Economy  = {"servedDecisions": int, "unscoredDecisions": int, "noMatchDecisions": int,
            "exposedTokens": int, "catalogTokens": int, "tokensNotSent": int,
            "savings": float|null,
            "catalogTokensPerDecision": int|null,   // per-agent only; null in overview
            "estimator": "chars/4 over compact JSON of name+description+inputSchema",
            "catalogBasis": "current_policy_scope"}
RankPoint = {"rank": int /*1-based*/, "shown": int, "selected": int, "rate": float|null}
ToolFunnel = {"toolId": str, "toolName": str|null, "serverName": str|null, "enabled": bool|null,
              "tokens": int|null,                   // current estimated definition tokens
              "surfaced": int, "selected": int, "succeeded": int, "failed": int,
              "selectionRate": float|null, "successRate": float|null,
              "avgRank": float|null, "exposedTokens": int}
// toolName/serverName/enabled/tokens are null for a tool removed from the catalog
```

**GET /api/v1/analytics/overview**
```jsonc
{"window": Window,
 "contextEconomy": Economy,                       // the headline: savings + tokensNotSent
 "funnel": {"surfaced": int, "selected": int, "succeeded": int, "failed": int,
            "selectionRate": float|null, "successRate": float|null},
 "routing": {"decisions": int, "noMatch": int, "noMatchRate": float|null,
             "fallback": int, "fallbackRate": float|null,
             "latencyP50Ms": float|null, "latencyP95Ms": float|null},
 "executions": {"attempts": int, "denied": int, "denialRate": float|null,
                "attributed": int, "attributionCoverage": float|null,
                "offFunnelSelections": int},
 "positionCurve": [RankPoint],
 "catalogDrift": {"added": int, "schema": int, "metadata": int, "removed": int, "restored": int}}
```

**GET /api/v1/analytics/tools**

* Query parameters: `?window&sort&order&limit&offset`.
  * `sort` is one of `surfaced` (the default), `selected`, `succeeded`,
    `failed`, `selectionRate`, `successRate`, `avgRank`, `exposedTokens` or
    `toolName`.
  * `order` is `asc` or `desc` (the default).
  * `limit` is 1 to 500 (default 100). `offset` is at least 0.
* Every catalog tool is listed, including ones with all-zero counts, plus
  removed tools that still have data.
* `null` sorts last in both directions. Ties break on `toolId`.

```jsonc
{"window": Window, "items": [ToolFunnel], "total": int, "limit": int, "offset": int}
```

**GET /api/v1/analytics/tools/{toolId}**

Returns 404 `{"detail": "Unknown tool."}` if the tool is not in the catalog.

```jsonc
{"window": Window, "tool": ToolFunnel,
 "positionCurve": [RankPoint],                    // this tool only
 "coSurfaced": [{"toolId": str, "toolName": str|null, "serverName": str|null,
                 "coSurfaced": int, "thisSelected": int, "otherSelected": int}]}  // top 10
```

**GET /api/v1/analytics/agents**

Agents are sorted by id.

```jsonc
{"window": Window,
 "items": [{"agentId": str, "decisions": int, "noMatch": int, "noMatchRate": float|null,
            "fallback": int, "fallbackRate": float|null,
            "latencyP50Ms": float|null, "latencyP95Ms": float|null,
            "attempts": int, "denied": int, "denialRate": float|null,
            "attributed": int, "attributionCoverage": float|null,
            "surfaced": int, "selected": int, "selectionRate": float|null,
            "avgSurfacedPerDecision": float|null,
            "budgetTools": null, "budgetUtilization": null,   // see §3 proposal 2
            "contextEconomy": Economy}]}
```

**GET /api/v1/analytics/suggestions**

* Query parameters:
  * `minSurfaced`: 1 to 100000, default 20.
  * `maxSelectionRate`: 0 to 1, default 0.05.
  * `staleDays`: 1 to 365, default 30.
* Each list is capped at 50 items.
* `wastedExposure` is sorted by surfaced count, descending.

```jsonc
{"window": Window, "minSurfaced": int, "maxSelectionRate": float, "staleDays": int,
 "wastedExposure": [{"toolId": str, "toolName": str|null, "serverName": str|null,
                     "surfaced": int, "selected": int, "selectionRate": float|null,
                     "exposedTokens": int}],
 "staleTools": [{"toolId": str, "toolName": str, "serverName": str,
                 "createdAt": ts, "lastSurfacedAt": ts|null}],
 "neverRoutedServers": [{"serverId": str, "serverName": str, "toolCount": int, "createdAt": ts}]}
```

**POST /api/v1/analytics/rollup**

* The body is optional: `{"day"?: "YYYY-MM-DD", "days"?: 1..90}`. Unknown
  keys get 422.
* `day` is the newest day to recompute. It defaults to the newest eligible
  day, `liveHorizon - 1`. `days` days ending at `day` are recomputed. A day
  at or after the live horizon gets 400, with `detail` "day is inside the
  live window (last 48h); only older days are rolled up." Nothing is written
  in that case.
* Re-running is idempotent.

```jsonc
{"liveHorizon": "YYYY-MM-DD", "days": [{"day": "YYYY-MM-DD", "toolRows": int}]}  // oldest first
```

**GET /api/v1/dedup/suggestions** (extended, read-only)

* `usageEvidence` is set only on `open` items.
* It is `null` on items that are not open, and on accept/dismiss responses.

```jsonc
items[i].usageEvidence = {"window": "30d", "toolASurfaced": int, "toolBSurfaced": int,
                          "coSurfaced": int, "toolASelected": int, "toolBSelected": int,
                          "bothSelected": int} | null
```

**Prometheus** (`/metrics`). These are all-time counters computed from the
database (rollups plus raw) at scrape time, with no labels:

* `mcpr_analytics_tools_surfaced_total`
* `mcpr_analytics_tools_selected_total`
* `mcpr_analytics_tools_succeeded_total`

A database failure yields no samples (it is logged by exception type); it
never fails the scrape.

## 8. Mutation evidence

For each guard: break it, confirm the named test fails, then restore it.

| Guard | Mutant | Result |
|---|---|---|
| Single stats writer | Old manager (running mean) restored | `test_manager_stats_match_the_single_registry_writer` FAILED |
| Rollup idempotency | `DELETE` in `recompute_day` removed | `test_rollup_is_idempotent` FAILED (primary-key violation) |
| NULL attribution | Profiles `FILTER (WHERE route_request_id IS NOT NULL)` removed | `test_null_attribution_only_world_is_graceful` FAILED |
| Attribution sanitizer | `_attribution` passes anything | 5 tests in `test_analytics_attribution.py` FAILED |
| Prior bounds | Rate clamp AND outer clamp removed | `test_prior_never_exceeds_cap_and_never_negative[*]` FAILED (3) |
| Prior cold tool | `surfaced <= 0` changed to `< 0` | 4 prior tests FAILED |
| Admin auth | Router `Depends(require_admin)` stripped | `test_analytics_routes_require_admin[*]` FAILED (6 of 6) |

The prior's rate clamp and outer clamp are redundant defences. Removing
either one alone keeps the bound, so both had to be removed for the mutant to
fail.

## 9. Performance (dev box, CPU, compose Postgres)

Synthetic load: 1,000 tools, 20,000 decisions × 8 surfaced, 5,000 attributed
executions, 10 agents. All decisions were inside 28 days.

| Window | overview | tools | agents | tool detail | metrics (all-time) |
|---|---|---|---|---|---|
| 7d | 1.05 s | 0.16 s | 0.42 s | 0.44 s | 0.44 s |
| 30d | 3.25 s | 0.46 s | 0.93 s | 2.2 s | |

**FLAG: plan instability.** The FIRST run, right after a bulk insert into
tables whose planner statistics still described near-empty tables, took
9.4 s for the 7d tool detail and 148 s for the 30d one, plus 21 s for the
30d overview. The cause is a CTE join misestimate. After autoanalyze it took
the times above. Run `ANALYZE` after bulk imports. If it recurs on real
growth curves, restructure co-surfacing to pre-filter decisions. The UI
should default to 7d.

## 10. Touches outside the core fence

* `src/mcprouter/registry/wire.py` (registry fence, wave 1): adds the
  `SuggestionEvidence` model and an optional `SuggestionOut.usage_evidence`.
  Additive only.
* `src/mcprouter/dedup/review.py`: `list_suggestions` attaches evidence for
  open rows. Read-only; no review logic changed.
* `src/mcprouter/db.py`: two literal statements appended to
  `_ADDITIVE_COLUMNS` (the "init path" ALTERs).
* `tests/conftest.py`: `tool_stats_daily` added to `_CLEAN_TABLES` (DELETE).
* `tests/test_execution_manager.py`: one test appended (gap 5).
* `api/routes_executions.py` is NOT touched. `ExecutionRow` now carries
  `route_request_id`, but the `/executions` wire does not expose it yet. That
  is a one-field follow-up for whoever owns that router.

## 11. Flags

1. Attribution is only as good as what the gateway passes. Until A wires the
   keyword, `selected` is 0 everywhere and `attributionCoverage` reads 0.
   This is visible, not silent.
2. Eval runs (`/route/evaluate`) persist `routing_decisions` (wave-1 gap 10),
   so they inflate surfaced and no-match counts. Consider tagging eval
   decisions (for example via `agent_id` or `model_version`) and excluding
   them.
3. `pending_approval` counts as a selection, and the approved replay is
   unattributed (`ApprovalRequest` has no `route_request_id`). To fix it,
   carry the id on the approval row; that file is out of this fence.
4. The economy uses current scope and current definitions for historical
   decisions (see §6). This is the main honest caveat on the headline
   number.
5. The staleness "last surfaced" check is an all-time raw scan. It is fine at
   the local-first scale. Revisit with a raw-log retention policy (rollups
   already preserve history).
