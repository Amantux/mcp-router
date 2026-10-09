# Integration notes: wave 2, final (`integrate/wave2`)

The branch merges `wave2/budgets`, `wave2/analytics` and `wave2/ui` into
`master`, then adds the integration work below. Each track's own notes are
still the detailed reference:

* `docs/INTEGRATION_NOTES-wave2-budgets.md`
* `docs/INTEGRATION_NOTES-wave2-analytics.md`
* `docs/INTEGRATION_NOTES-wave2-ui.md`

This file records what changed at integration, the decisions taken, and the
gaps that remain.

## 1. Merges

They were merged in this order: budgets, then analytics, then ui. All were `--no-ff`.

| Expected conflict | What actually happened |
|---|---|
| `api/app.py` lifespan | No conflict. Analytics never touched `app.py`, so `install_analytics(app)` was added by hand after the routers. |
| `db._ADDITIVE_COLUMNS` | **Conflicted.** I kept both sides: the budgets `max_servers` ALTER, and the analytics `route_request_id` ALTER plus its index. |
| `tests/conftest.py` `_CLEAN_TABLES` | Auto-merged. `tool_stats_daily` is present. |
| `interfaces.py` | Auto-merged. The budgets fields are kept. |
| `tests/test_execution_manager.py` | Auto-merged. Both sides are kept. |
| `tests/test_e2e_integration.py` | Auto-merged. The budgets version is the base, and it is extended in §6. |

The merged tree was green before any integration work: 767 passed, 4 skipped.

## 2. REST execute: `POST /api/v1/tools/{toolId}/execute`

The route lives in `api/routes_execute.py`. It is a thin wrapper over
`ExecutionManager.execute`, and there is no parallel execution path.

**Request:**

```json
{ "arguments": {}, "routeRequestId": "optional", "agentId": "optional" }
```

* Both camelCase and snake_case are accepted.
* Unknown keys get 422.
* `agentId` may also be passed as a query parameter. If the query and body
  values differ, the call gets 400.

**Response:** `200`. The shape matches the UI contract W1.

```json
{ "status": "...", "detail": "...", "recordId": "...", "approvalId": null,
  "errors": [], "result": { "content": [], "isError": false, "structuredContent": null },
  "latencyMs": 12.3 }
```

* Every manager outcome is a 200 carrying its `status`: `ok`, `error`,
  `timeout`, `denied`, `rate_limited`, `unavailable`, `invalid_args` and
  `pending_approval`.
* HTTP error codes are only for transport and auth problems: 400, 401,
  403, 404, 422 and 503.
* `result.content` must pass the same MCP shape check as the gateway's
  `tools/call`. Malformed content is withheld, with the detail `upstream
  returned malformed content`.
* `latencyMs` is the time of the upstream call. It is the new
  `ExecutionResult.latency_ms`.

### Identity

| Credential | Behaviour |
|---|---|
| Agent key | Runs as that agent. An `agentId` naming a different agent gets **403**. `routeRequestId` is passed through for attribution. |
| Dev mode (nothing configured) | Runs as the synthetic `dev` principal, which is deny-by-default. |
| Admin token | `agentId` is **required** (**400** with an explanatory message otherwise). An unknown agent gets **404**. `dev` exists only in dev mode, and dev mode never has an admin token. |

How the admin path works:

* The admin token is detected with `deps_auth.is_admin_bearer`, a
  constant-time hash compare that never raises.
* The handler loads the **real** principal row and calls
  `execute(..., initiated_by="admin")`.
* The agent's policy, availability, validation and approval rules apply
  unchanged. The admin token never widens what the call may do.

Provenance of an admin-impersonated call:

* `execution_records.initiated_by = 'admin'` is a new nullable
  `VARCHAR(16)`, added through an additive ALTER. Every audit row of the
  attempt also carries the detail prefix `[admin-initiated, impersonating
  '<agent>']`.
* The attempt is **never attributed**: `routeRequestId` is ignored. An
  admin trying a tool is not the agent selecting it.
* An approval created this way records `summary.initiatedBy = "admin"`.
  `approve()` restores the provenance, so the row that records the real side
  effect is marked too.
* The attempt uses a separate rate-limit window, `admin:<agent>`. Admin
  trials neither spend the agent's budget nor can be starved by it. This is
  a resource guard, not authorization.
* Analytics profiles exclude `initiated_by` rows from attempts, denied and
  attributed.

A `routeRequestId` that is too long or malformed is dropped by the manager
(the call is then unattributed). It is not rejected, which matches MCP.

## 3. Analytics: simulation and cache semantics

There is one predicate, `analytics.funnel.LIVE_DECISION_SQL`:

```
NOT starts_with(coalesce(d.model_version, ''), 'simulated/')
```

It is ANDed into **every** query that reads `routing_decisions`:

* the surfaced CTE. That covers the funnel, the position curve,
  co-surfacing, dedup pair evidence, rollups and the Prometheus totals.
* economy decision counts
* profile decision counts
* the profile execution-attribution join
* staleness

The rules:

* `simulated/` rows (admin `/route/simulate`) are invisible to all of these.
  An execution attributed to a simulated decision still counts as an attempt,
  but not as attributed.
* `cached/` rows (route-cache hits) **count**, because the agent really was
  shown those tools.
* A test asserts that the SQL literal matches
  `routing.pipeline.SIMULATED_MARKER`.

The tests were written to fail first (`tests/test_analytics_markers.py`).

**Upgrade note:** a rollup day computed before this commit, on a database
built during integration, still contains simulated rows. Re-run
`POST /api/v1/analytics/rollup` over those days. Production databases have
no such days.

## 4. Other work items

* **Gap 6 remainder.** `ExecutionManager._available()` now covers both
  `execute()` and `approve()`. A tool is available only when it is enabled
  and available and its server is enabled and not `offline`. Anything else
  is `unavailable` and audited. A `degraded` server still executes.
* **Settings.**
  * `usage_prior_enabled` reads `MCPR_USAGE_PRIOR_ENABLED`.
    `analytics/prior.py` no longer reads `os.environ`; it now exposes
    `prior_enabled(settings)`. I chose this over keeping a second, lenient
    parser.
  * `analytics_rollup_enabled` reads `MCPR_ANALYTICS_ROLLUP_ENABLED` and
    defaults to false.
  * Both use the strict boolean parser: an empty string means unset,
    anything unrecognised fails startup, and the error names the variable.
* **Rollup scheduling.** When `analytics_rollup_enabled` is on, the
  lifespan starts `analytics.scheduler.RollupLoop`.
  * It runs one pass at startup, then one every 24h.
  * Each pass recomputes the newest **3** eligible days, ending at
    `horizon - 1`, through the same idempotent, advisory-locked
    `recompute_range` that the rollup endpoint uses. "Yesterday" is still
    inside the 48h live window, so it cannot be rolled up.
  * A failed pass logs the exception type and the loop continues.
  * The loop is stopped before the inference engine unloads.
  * The alternative is a cron job calling `POST /api/v1/analytics/rollup`
    (see the README).
* **`/executions`** exposes `routeRequestId` as a read-only field.
* **`app.py`** includes the execute router and calls `install_analytics(app)`.

## 5. UI reconciliation (`ui/src/api`)

* **`normaliseSimulation`** now reads `diagnostics.budgetClamps`. The UI had
  guessed `clamps` or `budgets`, so against the real backend it showed no
  clamps at all. It also derives the candidate count from the
  `diagnostics.candidatesConsidered[]` list. A new test pins the exact shape
  from `api/routes_route.py`.
* **`RouteResponse`** gains `maxToolsApplied`, `maxServersApplied` and
  `cached`. A client test confirms that the generic snake-to-camel conversion
  handles `max_tools_applied` and the other new fields.
* **`executeTool`** takes `(toolId, args, signal, {agentId?, routeRequestId?})`.
  The `ExecuteResult` contract comment now describes the real endpoint.

## 6. End-to-end test: the efficiency loop

This extends `tests/test_e2e_integration.py`, which runs against a real
uvicorn server and a real MCP fleet. After the v0.1 story, it checks the
following:

1. The audit row for the MCP `tools/call` of `github.search_issues` carries
   agent1's **live** `/route` `request_id`. This verifies the gateway's
   feature-detected seam on the real row.
2. `GET /analytics/overview` shows `attributionCoverage > 0`, and the funnel
   shows `selected >= 1` and `succeeded >= 1`.
3. `GET /analytics/tools` shows `search_issues` with `selected >= 1`.
4. REST execute as agent1 with `routeRequestId` returns `ok`, and
   `/executions` shows the attribution on that row.
5. As admin:
   * Without `agentId`, the call gets 400.
   * With `agentId=agent1`, it returns `ok`, carries the impersonation
     prefix, and is unattributed.
   * A server-2 tool stays `denied`, so impersonation does not widen access.
6. `/route/simulate` writes a `simulated/` row, and overview funnel, routing,
   economy, position and `/analytics/agents` are all **unchanged**.
7. Repeating the same `/route` is a cache hit (`cached: true`), and it
   counts as one more decision plus its surfaced tools.

The e2e catches a broken seam or filter:

* With the gateway pass-through disabled, it fails on assertion 1.
* With the simulated filter removed from the surfaced CTE, it fails on
  assertion 6.

## 7. Mutation evidence (integration guards)

In each case the guard was broken by a scripted literal replace, the named
test failed, and the guard was restored.

| Guard | Mutant | Failing test(s) |
|---|---|---|
| Simulated filter in the surfaced CTE | predicate → `TRUE` | `test_simulated_decisions_are_invisible_to_all_analytics`, `test_simulated_decisions_are_not_rolled_up` |
| Economy decision filter | same | `test_simulated_decisions_are_invisible_to_all_analytics` |
| Profile decision filter | same | same |
| Profile attribution-join filter | same | same |
| Staleness filter | same | same |
| Cached rows count as traffic | also exclude `cached/` | `test_cached_decisions_count_as_real_traffic`, `test_marker_constants_match_the_routing_track` |
| Offline term in `_available` | dropped | `test_offline_server_tool_is_unavailable_and_audited`, `test_server_going_offline_voids_pending_approval` |
| REST agent path through the manager | `_load` + `_invoke` directly | `test_policy_denial_is_200_with_status_and_audited`, `test_execute_flows_through_the_one_manager`, plus 4 more |
| Impersonation through the manager (policy) | `_load` + `_invoke` directly | `test_admin_impersonation_is_evaluated_under_the_agents_policy`, `test_execute_flows_through_the_one_manager`, `test_admin_impersonation_ok_is_audited_and_never_attributed` |
| Admin must name an agent | 400 check disabled | `test_admin_without_agent_id_is_400` |
| An agent key cannot name another agent | 403 check disabled | `test_agent_key_cannot_name_another_agent` |
| Impersonation audit marker (pre-review note) | note removed | `test_admin_impersonation_is_evaluated_under_the_agents_policy`, `test_admin_impersonation_ok_is_audited_and_never_attributed` |
| Impersonation never attributed | rrid forwarded | `test_admin_impersonation_ok_is_audited_and_never_attributed` |
| `initiated_by` column written | dropped | `test_impersonation_is_a_structured_column_on_every_row`, `test_impersonated_approval_keeps_provenance_through_the_replay` |
| Approval replay keeps provenance | `prov = _NO_PROV` | `test_impersonated_approval_keeps_provenance_through_the_replay` |
| Separate admin limiter window | key on `agent_id` only | `test_admin_trials_use_their_own_rate_limit_window` |
| Profiles exclude impersonated rows | filter dropped | `test_admin_impersonated_attempts_are_not_agent_behaviour` |
| REST content shape check | validation removed | `test_malformed_upstream_content_is_withheld` |
| Rollup loop lifespan start / explicit stop | either one removed | `test_create_app_starts_and_stops_rollup_loop_when_enabled` |
| UI `budgetClamps` read | dropped | LensPage "reads budgetClamps…", "renders the backend shape…" |
| e2e: gateway seam | pass-through disabled | `test_end_to_end_…` (attribution assert) |
| e2e: simulated filter | surfaced CTE filter dropped | `test_end_to_end_…` (simulate assert) |

## 8. Adversarial review

The reviewer subagent did a static review only. Its verdict was **PASS WITH
NOTES**, with no blockers.

**Fixed:**

* **SF-1.** Approvals lost impersonation provenance. Fixed as described in §2.
* **SF-2.** The admin shared the agent's rate limit. Fixed as described in §2.
* **SF-3.** Impersonated attempts skewed the agent's profile and were
  identifiable only by free text. Fixed with the structured column, as
  described in §2.
* **Nit.** A `routeRequestId` of 37 characters got 422.
* **Nit.** The REST content-shape check differed from the gateway's.
* **Nit.** Strict-bool errors did not name the variable.
* **Test gaps.** Added tests for a disabled principal, the shared limiter
  and approval provenance.

**Accepted and documented:**

* **Nit.** `RollupLoop.stop()` waits for an in-flight pass. The thread is
  not abandoned, so shutdown may wait for up to one 3-day recompute.
* **Nit.** Rollup days computed before the simulated filter landed (see the
  upgrade note in §3).

**Not fixed, flagged:**

* **SF-4.** The playground does not work in admin-only sessions (see §9).

## 9. Remaining gaps and flags

1. **The playground in an admin-only session returns 400.**
   `PlaygroundPage` calls `executeTool(tool.id, args)` without `agentId`.
   * The client API now supports `agentId`.
   * The page needs a "Run as" agent picker (from `listPrincipals`) when no
     agent key is set. I did not do this because the brief restricted UI
     edits to `ui/src/api`. It is a small follow-up.
   * Today the user sees the curated 400 message, so nothing leaks.
2. **`/executions` does not expose `initiatedBy`.** The impersonation prefix
   on `detail` is visible. Adding the field is a one-line follow-up in
   `execution/history.py` and `routes_executions.py`.
3. **Usage statistics include admin trials.** `call_count` and the latency
   EMA include admin-initiated executions. This is intentional: they were
   real upstream calls.
4. **The usage prior is still not wired into routing.** Only its switch was
   promoted to settings.
5. **Still open from the track notes:**
   * analytics proposals 1, 2 and 4 (persist candidates, budget and catalog
     size on decisions)
   * eval decisions counted as traffic
   * approval replays unattributed
   * budgets SF-2 (refresh latency)
6. **Alembic baseline.** It must include every `_ADDITIVE_COLUMNS` line,
   including the new `execution_records.initiated_by`.
7. **Not visually verified.** The UI was not checked in a browser against a
   live backend. Only jsdom tests and the build ran.

## 10. Gates (final tree)

* `ruff check` clean.
* `ruff format --check` clean.
* `mypy` (strict): no issues in 96 source files.
* `pytest tests/ -q`: **800 passed, 4 skipped**.
* `MCPR_RUN_SLOW=1 pytest tests/ -q`: **804 passed**.
* Zero-ML venv (fresh `uv venv`, `.[dev]` only, no torch, transformers,
  sentence-transformers or laya): **796 passed, 8 skipped**.
* UI:
  * `npm ci` OK.
  * `npm run build` OK.
  * `npm run lint` clean.
  * `vitest`: 10 files, **94 passed**.
