# Integration notes: wave 2, budgets / cache / lifecycle (`wave2/budgets`)

This branch is cut from `master` (the integrated v0.1 tree). It covers
exposure budgets, the route cache, the lifecycle gaps from
`INTEGRATION_NOTES-integration.md` §8 (gaps 1, 2, 4 and 6), and the
`/route/simulate` endpoint. Executor B (analytics) and executor C (UI) work in
parallel. §4 is the contract C consumes, and §6 is the one seam shared with B.

## 1. Commits

| Commit | What |
|---|---|
| `feat(routing): exposure budget clamp chain + maxServers` | `routing/budgets.py`, `max_servers` on the request, principal and settings |
| `fix(gateway): exclude offline servers from MCP exposure (integration gap 6)` | gateway `visible_tools` |
| `feat(gateway): pass the agent's last route request_id to execute()` | seam with B (§6) |
| `feat(routing): route cache with generation invalidation + query-embedding LRU` | `routing/cache.py`, `generation.py`, bump call sites |
| `feat(discovery): classify + embed after any catalog-changing sync (gap 1)` | `lifecycle.py`, the `DiscoveryService(post_sync=)` hook |
| `feat(app): start the SyncLoop behind MCPR_SYNC_ENABLED (gap 4)` | lifespan |
| `feat(routing): admin-only POST /api/v1/route/simulate with diagnostics` | `routing/trace.py`, endpoint |
| `fix(lifecycle): unattended reclassification never widens an operation` | review SF-1 (§5) |
| `fix(budgets): report a clamp only when a ceiling cut below the effective ask` | review nit (§4 `clampedBy`) |

## 2. Exposure budgets

There is one clamp chain, `routing.budgets.effective_budgets`, used by
`/route`, `/route/simulate` and the gateway (`tools/list` and
`router.find_tools`):

```
maxTools   = min(request.maxTools or principal.max_tools, principal.max_tools, MCPR_MAX_EXPOSED_TOOLS)
maxServers = min over the SET values of (request.maxServers, principal.max_servers, MCPR_MAX_EXPOSED_SERVERS)
             (None at every level = unlimited)
```

A request can lower a budget but never raise it.

**Behaviour fix:** before this change, `/route` used `body.max_tools or
settings.max_exposed_tools` and ignored `principal.max_tools`.

**Changelog-worthy:** with no `max_tools` in the body, `/route` now defaults
to `principal.max_tools`, which is 8 by default, including for
env-bootstrapped principals. It no longer uses `MCPR_MAX_EXPOSED_TOOLS`. An
operator who raised the global cap to 16 must also raise each principal's
`maxTools`. The gateway already behaved this way.

**Server cap semantics:** the cap is applied after ranking. Tools are kept in
rank order, and a tool whose server would be the (N+1)-th distinct server is
skipped. The slots it frees are filled from deeper ranks. The `max_tools`
truncation comes after that. The no-match (Noul) question is also asked over
the capped set, because that is what would actually be exposed. The gateway
applies the same cap to its exposure order and to the default most-used list.

### Wire changes

* **`/route` request.** `maxServers` / `max_servers` is optional, in the range 1–1000.
* **`/route` response.** It adds `max_tools_applied`, `max_servers_applied`
  (null means unlimited) and `cached` (see §3).
  * **Judgment call:** these fields are snake_case, not the camelCase
    `maxToolsApplied` in the brief. SPEC §9 / scoping §10 make the `/route`
    response snake_case, and mixing the two styles in one object would be
    worse.
  * The admin simulate response (§4) is camelCase and carries
    `maxToolsApplied` / `maxServersApplied`.
* **Principals.** `PrincipalOut`, `PrincipalIn` and `PrincipalPatch` gain
  `maxServers` (1–1000). In a PATCH, an explicit `null` clears it and an
  omitted field leaves it unchanged.
* **Contract additions (append-only, defaulted):**
  * `interfaces.RouteRequest.max_servers: int | None = None`
  * `interfaces.RouteResult.cached: bool = False`
  * `models.AgentPrincipal.max_servers` (nullable `INTEGER`)
* **Schema.** `db._ADDITIVE_COLUMNS` now includes `ALTER TABLE
  agent_principals ADD COLUMN IF NOT EXISTS max_servers INTEGER`. The deferred
  Alembic baseline must include it verbatim, as with the other ALTERs.

## 3. Route cache

`routing/cache.py` is an in-process `RouteCache`: a bounded LRU with a TTL.
It is built per `RoutePipeline` from settings.

**Key:**

```
(agent_id, whitespace-normalized query, scope.fingerprint(),
 catalog_generation, policy_generation, effective max_tools,
 effective max_servers, sorted allowed server ids, decision-model name)
```

* **Query normalization** collapses whitespace only. Case is kept, because
  the model sees it. The query has already been redacted.
* **Decision-model name.** It is read at lookup and again at store time.
  `DeadlineDecisionModel.name` is `"<backend> (not loaded)"` until the engine
  lazy-loads, so a key built once at the start would never hit after the
  first route.
* **Scope fingerprint.** `PolicyScope.fingerprint()` is the sha256 of the
  principal's agent_id and enabled flag plus its full rule set. `DenyAllScope`,
  `AllowAllScope` and `StaticScope` have constant or derived fingerprints. A
  scope with no `fingerprint()` is never cached: an unknown implementation
  cannot be keyed safely.

**Invariant: the cache can skip retrieval and model calls, but never
authorization.** On every hit, `RoutePipeline._revalidate` does the following:

1. It reloads the cached tool rows with `retriever.eligibility_filters()`:
   tool enabled and available, server enabled and not offline. This is the
   one shared helper, now also used by both retrieval legs.
2. It re-applies the CURRENT scope: the server ids intersected with
   `allowed_servers`, then `scope.permits` on a candidate built from the
   freshly loaded operation and name.
3. If any cached tool fails either check, it discards the entry and
   recomputes the route.

So correctness never depends on a generation bump landing. Bumps exist so
that NEW tools and NEW grants appear before the TTL expires.

**Never cached:**

* Fallback results. A transient model timeout must not pin a degraded
  ranking for the TTL.
* `/route/evaluate`. Its scopes are wrapped in `routing.scope.UncachedScope`
  (no fingerprint), so an eval re-run measures the pipeline, not the cache.
  This was found by a failing test.
* `/route/simulate` (§4).

**Query-embedding LRU.** `HybridRetriever` keeps a `QueryEmbeddingCache`
keyed by `(text, backend name)`, with 512 entries. It is a pure function, so
there is nothing to invalidate.

**Decision record for a hit (column-free convention).** A hit still writes a
`RoutingDecisionRecord` with a fresh `request_id`, and its
`model_version = "cached/<model that made the original decision>"` (truncated
to 80). Simulations use `"simulated/<model>"`. Analytics (B) should treat
these prefixes as markers.

**Metrics (Prometheus):**

* `mcpr_route_cache_hits_total`, counted before revalidation
* `mcpr_route_cache_misses_total`
* `mcpr_route_cache_evictions_total{reason="capacity"|"expired"}`

### Generation counters (`mcprouter/generation.py`)

These are process-global and in-process (scoping §3). Every bump runs AFTER
the transaction commits, so a concurrent route cannot cache pre-commit state
under the new generation.

| Counter | Bumped at |
|---|---|
| catalog | `discovery/sync.py` `sync_server` apply (when the report changed, or the status crossed into or out of `offline`) |
| catalog | `discovery/sync.py` `_set_status` (crossing into or out of `offline` only; healthy↔degraded flaps do not bump) |
| catalog | `lifecycle.py` post-sync hook (after classify + embed) |
| catalog | `api/routes_tools.py` `patch_classification`, `_toggle` (enable/disable) |
| catalog | `api/routes_servers.py` `patch_server` (enable/disable), `remove_server` |
| policy | `api/routes_policy.py` create, patch and delete principal; create, patch and delete rule (6 calls) |

`rotate-key` does not bump, because it does not change what routing returns.

## 4. `POST /api/v1/route/simulate` (admin-only): contract for the UI

**Request** (camelCase or snake_case):

```json
{ "agentId": "agent1", "query": "...", "maxTools": 5, "maxServers": 2, "allowedServers": ["github"] }
```

* **`agentId`** is required. It must name a principal. `dev` is also
  accepted, but only while dev mode is active.
* **Errors:**
  * Unknown agent: **404**.
  * Unknown or out-of-scope `allowedServers`: **400**, the same as `/route`.
  * Validation errors: **422**, with the curated message that does not echo
    the query.
* **Auth:** `require_admin`, so the request needs the
  `Authorization: Bearer <MCPR_ADMIN_TOKEN>` header. Outside dev mode the UI
  must send it; integration §8 gap 1 is still open.

**Response 200.** This shape is stable: fields may be added, never renamed.

```json
{
  "requestId": "uuid",
  "agentId": "agent1",
  "simulated": true,
  "tools": [{"toolId": "uuid", "server": "github", "tool": "search_issues", "score": 0.80}],
  "noMatch": false,
  "fallbackUsed": false,
  "latencyMs": 33.4,
  "modelVersion": "simulated/deterministic-v1",
  "maxToolsApplied": 5,
  "maxServersApplied": 2,
  "diagnostics": {
    "candidatesConsidered": [
      {"toolId": "...", "server": "github", "tool": "list_issues", "domain": "development",
       "operation": "read", "retrievalScore": 0.49, "matchedOn": ["vector", "keyword:issues"]}
    ],
    "stages": [
      {"stage": "retrieval", "before": 3, "after": 2,
       "pruned": [{"toolId": "...", "server": "github", "tool": "create_issue"}],
       "detail": {"limit": 20, "policyFiltered": 2}}
    ],
    "policyFiltered": [
      {"toolId": "...", "server": "slack", "tool": "search_messages", "operation": "read",
       "reason": "no matching policy rule"}
    ],
    "budgetClamps": [
      {"budget": "maxTools", "requested": 5, "principal": 8, "globalCap": 8, "applied": 5, "clampedBy": null},
      {"budget": "maxServers", "requested": 2, "principal": null, "globalCap": null, "applied": 2, "clampedBy": null}
    ]
  }
}
```

**Stages.** Stages appear in pipeline order. A stage that did not run is
absent; for example, `domain` only runs when the candidates span more than
one domain.

| `stage` | `pruned` | `detail` keys |
|---|---|---|
| `retrieval` | scope-denied and over-limit candidates | `limit`, `policyFiltered` (count) |
| `domain` | candidates outside the kept domains | `options`, `chosen`, `probabilities`, `kept` |
| `operation` | none (the operation stage is soft) | `options`, `chosen`, `probabilities`, `downweighted` (tool ids), `weight` |
| `score` | none | `scores` (toolId → final score) |
| `noMatch` | none | `pYes`, `floor`, `noMatch` |
| `fallback` | none | `error` (exception type name), `scores` |
| `maxServers` | skipped by the server cap | `limit` (null = unlimited) |
| `maxTools` | truncated | `limit` |

On a model failure, the partial model stages are dropped and replaced by
`fallback`. `maxServers` and `maxTools` are absent on a no-match.

**`policyFiltered[].reason`** is the policy engine's curated
`Decision.reason`:

* `no matching policy rule`
* `operation 'write' exceeds policy ceiling`
* `principal disabled`

If the scope has no `explain()`, the fallback reasons are `server not in
scope` and `denied by scope`.

Out-of-scope servers are pushed into SQL by the live path. To surface them,
the trace runs one extra retrieval without the scope's server restriction.
The request's own `allowedServers` still applies to that retrieval. It runs
only in simulation.

**`clampedBy`** is `"principal"`, `"global"` or null. It is set only when a
ceiling cut below the caller's effective ask: the request value, or the
principal default when the request omits one. A plain default is not a
clamp. When the principal cap and the global cap tie, it is attributed to
`global`.

**Side effects.** Simulation never publishes exposure to a session, never
executes, and neither reads nor writes the route cache. It does write a
decision row marked `simulated/`. `RoutePipeline.route(..., trace=RouteTrace())`
is the simulation switch, and live callers never pass a trace.

## 5. Lifecycle (integration §8)

* **Gap 1 (post-sync hook).** `DiscoveryService(..., post_sync=hook)` runs
  the hook in a worker thread after any sync that changed the catalog.
  `sync_server` (the manual refresh) runs it once per call. `sync_all` (the
  SyncLoop) runs it once per pass, after `ANALYZE`. `lifecycle.make_post_sync_hook`
  does the following:
  1. It classifies (rules-v1) the changed tools (added, schema, metadata,
     restored) plus the never-classified backlog. Writes go through
     `apply_auto_classification`, whose UPDATE guard means a reviewed tool is
     never overwritten. The guard has two layers: the hook's selection and
     the UPDATE WHERE.
     * **Non-widening rule (review SF-1).** Upstream metadata is untrusted.
       For a tool that an earlier automatic run already classified, the
       operation may only move toward execute/unknown (rank order: read <
       write < execute < unknown). A widening answer keeps the stricter
       current class and is logged. A tool never classified before gets the
       classifier's answer.
  2. It runs `embed_pending_tools` with the engine's embedder, using
     `MCPR_EMBED_BATCH_SIZE`.
  3. It bumps the catalog generation.

  Each step commits on its own and is best effort: a failure is logged by
  type name and never fails the refresh. The e2e test now asserts that the
  refresh already classified and embedded everything, where it used to run
  both by hand.
* **Gap 4 (SyncLoop startup).** With `MCPR_SYNC_ENABLED=true`, the lifespan
  starts a `SyncLoop` over `app.state.discovery`, the same service, so the
  hook runs. It stops the loop on shutdown, before the inference engine
  unloads. `app.state.sync_loop` holds the loop or `None`. The default is
  false, which keeps today's behaviour. The value is parsed strictly; an
  unrecognised value fails startup.
* **Gap 6 (offline servers).** Gateway exposure, both the routed and the
  default list, now excludes servers with `status == 'offline'`. Routing
  already did. **Not done (B's fence):** `ExecutionManager`'s availability
  check still ignores `server.status`, so an authorized tool on an offline
  server is still callable and fails upstream. B or integration should add
  `status != 'offline'` there.

**`api/app.py` coordination.** The changes are additive:

* the `SyncLoop` start and stop in `lifespan`
* `app.state.sync_loop`
* `DiscoveryService(post_sync=...)`
* one shared `EngineEmbedder` for the retriever and the hook

If B also edits `lifespan`, keep both blocks, with the loop stop before
`inference.unload`.

## 6. Seam with B: `route_request_id`

On `tools/call`, the gateway passes `route_request_id=<the agent's current
Exposure.request_id>` to `ExecutionManager.execute`, but only when that method
accepts the keyword. Detection is done once, in `GatewayServer.__init__`, with
`inspect.signature`: an explicit keyword parameter or `**kwargs` both count.
With no route yet, the keyword is not passed at all.

Exposure is kept per AGENT, not per MCP session, so the id is the agent's
last route across all of its sessions. A cache hit gets its own fresh
`request_id` and decision row, so attribution always points at a real
`routing_decisions` row.

The branch is green with or without B's change, which is tested with a
recording subclass.

## 7. Settings added

All of these follow the rule that an empty string means unset.

| Variable | Default | Notes |
|---|---|---|
| `MCPR_MAX_EXPOSED_SERVERS` | unset (unlimited) | Positive int; 0 or a negative value fails startup |
| `MCPR_ROUTE_CACHE_TTL_S` | 60 | 0 disables the cache |
| `MCPR_ROUTE_CACHE_SIZE` | 1024 | 0 disables the cache |
| `MCPR_SYNC_ENABLED` | false | Accepts 1/0, true/false, yes/no, on/off |

## 8. Files touched outside the core fence

* `src/mcprouter/api/routes_tools.py`: `bump_catalog()` after commit in
  `patch_classification` and `_toggle`, plus the import.
* `src/mcprouter/api/routes_servers.py`: `bump_catalog()` after
  `patch_server` and `remove_server`, plus the import. `patch_server` now
  returns its view after the `with` block, so the bump runs after the commit.
* `src/mcprouter/api/routes_policy.py`:
  * `bump_policy()` after commit at 6 sites, plus the import
  * `max_servers` on `PrincipalOut`, `PrincipalIn` and `PrincipalPatch`, and
    in create and patch (the brief allowed this)
* `src/mcprouter/db.py`: one ALTER line (§2).
* `src/mcprouter/interfaces.py`: two appended, defaulted fields (§2).
* `tests/test_route_api.py`: the wire-shape assertion gains the three new
  response keys.
* `tests/test_e2e_integration.py`: the manual classify/embed step became an
  assertion that the hook did it.

## 9. Mutation evidence

For each row, the guard was broken by hand, the named tests failed, and the
guard was restored. A scripted literal-replace harness was used.

| Guard removed | Failing tests |
|---|---|
| **Post-cache `scope.permits` pass in `_revalidate`** | `test_cache_hit_reapplies_policy_when_rule_revoked`, `test_cache_hit_rechecks_fresh_operation_against_policy` |
| Eligibility filter in `_revalidate` | `test_cache_hit_rechecks_eligibility`, `test_cache_hit_rechecks_offline_server` |
| Scope server-id check in `_revalidate` | `test_cache_hit_rechecks_scope_server_ids` |
| Principal term in the clamp chain | 7 (`test_max_tools_clamp_chain[...]` ×2, `test_max_servers_clamp_chain[...]` ×2, `test_route_request_cannot_raise_past_principal_max_tools`, `test_route_max_servers_both_casings_and_principal_cap`, `test_gateway_exposure_honours_principal_max_servers`) |
| `/route` bypassing budgets (`body.max_tools or settings...`) | `test_route_request_cannot_raise_past_principal_max_tools` |
| Pipeline server cap | `test_pipeline_caps_distinct_servers_in_rank_order`, `test_route_max_servers_both_casings_and_principal_cap` |
| Gateway server cap | `test_gateway_exposure_honours_principal_max_servers` |
| Gateway offline filter | `test_gateway_exposure_excludes_offline_servers` |
| `route_request_id` pass-through | `test_gateway_passes_last_route_request_id_when_supported` |
| Tool enable/disable bump | `test_enabling_a_tool_invalidates_cached_routes_via_api` |
| Sync on-change bump / offline-crossing bump | `test_discovery_sync_bumps_catalog_generation_only_on_change` / `test_health_crossing_offline_bumps_catalog_generation` |
| Rule-create policy bump | `test_policy_routes_bump_policy_generation` |
| Post-sync hook in `sync_server` / in `sync_all` | `test_sync_server_runs_post_sync_classify_and_embed` + `test_manual_refresh_endpoint_classifies_and_embeds` / `test_sync_loop_runs_the_hook_once_per_pass` |
| Both reviewed-guard layers | `test_post_sync_respects_classification_reviewed`. These checks ran on the pre-SF-1 code, which had a third layer in `auto_classify`'s SELECT. Removing one or two layers still passed; removing all three failed the test. |
| Non-widening rule (SF-1) disabled / applied to never-classified tools too | `test_post_sync_never_widens_an_auto_classification` / `test_post_sync_first_classification_may_set_any_operation` + the 3 refresh/loop tests |
| SyncLoop start / explicit stop | `test_create_app_starts_and_stops_sync_loop_when_enabled` (the stop is caught by a spy; without it, the portal teardown cancels the task silently) |
| `/route/simulate` `require_admin` | `test_simulate_is_admin_only` |
| Cache bypass under a trace; injected exposure publish in simulate | `test_simulate_never_publishes_executes_or_caches` |
| (Found by a failing test, then fixed) eval re-runs served from the cache | `test_evaluate_never_serves_from_the_route_cache` |

## 10. Adversarial review (reviewer subagent): no blockers

The reviewer independently re-proved the cache-revalidation mutations in a
scratch copy.

**Fixed:**

* **SF-1.** Unattended reclassification could widen access. Fixed as
  described in §5.
* **Nit.** `clampedBy` was reported for plain defaults. Fixed as described in
  §4.

**Accepted and flagged, not changed:**

* **SF-2: refresh latency.** The refresh endpoint awaits the hook inline, and
  `embed_pending_tools` embeds every pending tool in the catalog, not only the
  refreshed server's. Right after an embedding-backend switch, one refresh
  therefore re-embeds the whole catalog before it responds. The sync has
  already committed by then.
  * Lifespan shutdown also waits for an in-flight hook thread, because
    `to_thread.run_sync` without `abandon_on_cancel` finishes the thread
    first. This is the reviewer's reading; I did not verify it against the
    installed anyio.
  * Inline was kept so that a refresh response reflects classified,
    routable tools.
  * The proper fix is a `tool_ids` parameter on `embed_pending_tools`
    (inference-owned) or a single-flight background job. It is a follow-up.
* **SF-3: simulation and cache-hit rows.** Simulations and cache hits write
  `routing_decisions` rows under the agent's id, marked with the
  `simulated/` and `cached/` prefixes. **Analytics (B) must filter or label on
  these prefixes**, or it will count admin simulations as agent traffic.
* **Nit: embedding backend not in the cache key.** The route-cache key lacks
  the embedding backend's name. The `Retriever` protocol does not expose it.
  This affects freshness only, up to the TTL, and never authorization.
* **Nit: `QueryEmbeddingCache` TOCTOU.** `EngineEmbedder.name` and `.embed()`
  resolve the backend separately, so a mid-call backend switch could cache
  one vector under the other backend's name. Fixing it needs an
  inference-side accessor that returns the name and vector together.
* **Nit: private helper import.** `/route/simulate` imports the private
  `deps_auth._dev_mode_active`, as `policy/scope.py` already did. A public
  alias belongs in `deps_auth`, which is outside this fence.

## 11. Open items and flags

* `ExecutionManager` offline check (B's fence; §5).
* **Flaky under parallel executors:**
  * `test_testbed_scale.py` fails with "testbed.serve exited early".
  * The e2e test uses ports 8700 and 8710.

  Both use fixed ports outside the per-executor ranges. Each passes in
  isolation; once, under a concurrent run, the scale test failed on a port
  conflict.
* **Simulate and out-of-scope servers.** A simulate `allowedServers` that
  names an out-of-scope server returns 400 "unknown", the same as `/route`.
  An admin might prefer an explicit reason. This is left consistent with
  `/route` for now.
* **Cache hits renew exposure.** A hit still publishes exposure via `/route`,
  which is the intended behaviour. The `cached` flag lets clients tell the
  two cases apart.
