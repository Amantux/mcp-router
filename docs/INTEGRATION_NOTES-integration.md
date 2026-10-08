# Integration notes — `integrate/v0.1`

This branch merges the six feature branches (all cut from `master@645399f`)
into one tree, wires every seam they documented, and proves the result end
to end in `tests/test_e2e_integration.py`. The per-track notes
(`docs/INTEGRATION_NOTES-<track>.md`) are still the reference for each
subsystem. This file covers what integration decided, what it changed, and
what is still open.

## 1. Merges

The branches were merged in this order: discovery, registry, inference,
routing, gateway, ui. Each one is its own `--no-ff` merge commit.

| Merge | Conflict | Resolution |
|---|---|---|
| discovery, registry, inference | none | — |
| routing | `interfaces.py` (EOF appends) | Kept both: inference `BatchScoringDecisionModel`, then routing `ScopeFilter` |
| gateway | `interfaces.py` (EOF appends) | Kept all three; the gateway's `ToolCallResult`/`ToolInvocationError`/`ToolInvoker`/`RouteFn` go last |
| ui | none | — |

`pyproject.toml` auto-merged with the gateway's `jsonschema` and
`types-jsonschema`. Integration then added the following:

* `httpx2==2.13.1`, the version installed in the discovery worktree. `mcpclient` imports it.
* `laya==0.4.0` in the `[inference]` extra.
* The pytest `slow` marker.
* mypy `files = ["src/mcprouter", "bench"]`.

**Judgment call (mypy):** CI runs without the `[inference]` extra, and strict
mypy then fails on the lazy `torch`, `numpy`, `sentence_transformers`,
`huggingface_hub` and `laya` imports. A `[[tool.mypy.overrides]]
ignore_missing_imports` for exactly those modules lets the gate pass both
with and without the extra (verified both ways). The two now-redundant
`type: ignore[import-untyped]` comments in `inference/laya.py` were removed.

## 2. Wiring (all in `api/app.py::create_app(settings=None, *, env=None)`)

`env` carries secrets that never enter `Settings` (`MCPR_ADMIN_TOKEN`). It
defaults to `os.environ`, and tests pass an explicit mapping.

`create_app` runs these steps in order:

1. **Schema.** `init_db` creates `Base`, `approval_requests` (gateway
   `SecurityBase`) and `eval_results` (eval metadata). `init_registry` adds
   the FTS and dedup indexes.
2. **Security.** `configure_security(app, env)` runs. The startup warning now
   states the real dev-mode condition: no agent keys, no admin token and no
   principals.
3. **Inference.** `InferenceEngine(settings, idle_unload_s, embed_batch_size)`
   is constructed. The lifespan calls `load()` and `unload()` in a worker
   thread, so they stay off the event loop.
4. **Discovery and management routers.** The app gets
   `app.state.discovery = DiscoveryService(factory)` and includes the
   servers, tools, dedup, models and executions routers.
5. **Routing.** `install_routing` is called with
   `RoutePipeline(HybridRetriever(EngineEmbedder(engine)),
   DeadlineDecisionModel(engine.decision_model, MCPR_DECISION_TIMEOUT_S))`
   and `scope_resolver=policy_scope_resolver(...)`.
6. **Execution.** `ExecutionManager.from_settings(..., ConnectorToolInvoker(factory))`
   is built, and `routes_policy` is included.
7. **Gateway.** `build_gateway(app, manager, route_fn)` mounts `/mcp` and wraps
   the lifespan. `route_fn` routes under the policy scope, and the gateway
   redacts the query before calling it.
8. **Client logging.** The `httpx`, `httpx2`, `httpcore` and `mcp.client`
   loggers default to WARNING, because they log request URLs. A level the
   operator set explicitly is left alone.

Run a single uvicorn worker (gateway notes §1). The Dockerfile's
`uvicorn --factory` invocation already does.

### New integration modules

| Module | Seam |
|---|---|
| `policy/scope.py` | `PolicyScope`/`policy_scope_resolver`. Implements `ScopeFilter` with `policy.engine.evaluate`, the one policy implementation |
| `execution/invoker.py` | `ConnectorToolInvoker`. Implements `ToolInvoker` over `mcpclient.Connector`. `ConnectorError` becomes `ToolInvocationError(<fixed message>)` |
| `inference/adapters.py` | `EngineEmbedder` and `DeadlineDecisionModel`, which give each decision call its own deadline |
| `execution/history.py` + `api/routes_executions.py` | `GET /api/v1/executions` (UI contract #17) |

## 3. Security blocker (discovery escalation): closed

Every management route sits behind **one** admin dependency,
`deps_auth.require_admin`, which fails closed outside dev mode:

* At the router level: `routes_servers`, `routes_models`, `routes_executions`.
* Per endpoint: `/route/evaluate` and all admin routes in `routes_policy`.
* `routes_tools` and `routes_dedup` go through `registry.api_deps.require_admin`,
  which now **delegates** to the gateway dependency and only names the audit
  actor (`admin`, or `local-dev` in dev mode). There is no second auth decision.

`/api/v1/route` authenticates with `get_principal` and routes for the
authenticated agent. The body `agent_id` may be absent or equal to the
authenticated agent; a mismatch is 403. The route redacts the query (both
model input and the persisted `RoutingDecisionRecord`), then publishes the
result to the agent's MCP sessions with `apply_route_threadsafe`. The body
accepts both snake_case and camelCase.

### Mutation evidence (each guard broken by hand, named tests fail, guard restored)

| Guard removed | Failing tests |
|---|---|
| `dependencies=[Depends(require_admin)]` on `routes_servers.router` | 8 in `test_integration_auth.py`: every servers case, `test_stdio_registration_is_not_reachable_without_admin`, `test_fails_closed_when_no_admin_token_outside_dev_mode` |
| registry wrapper's delegation (`gateway_require_admin(request)` → `pass`) | 3: the tools, dedup and fail-closed cases |
| `routes_models`, `routes_executions` router dependency; `/route/evaluate` dependency | 1 each (its matrix case) |
| `/route` agent_id mismatch check | `test_body_agent_id_must_match_the_authenticated_agent` |
| routing as the body agent (`agent_id = body.agent_id or principal...`) | `test_body_agent_id_must_match_the_authenticated_agent` |
| `redact()` on the query | `test_query_is_redacted_before_the_model_and_the_decision_row` |
| decision deadline (call the model directly) | `test_hung_model_raises_inference_error_at_the_deadline`, `test_pipeline_falls_back_when_the_model_hangs` |
| `PolicyScope.permits` → `True` | all 4 in `test_policy_scope.py` |
| ANALYZE after bulk sync | `test_bulk_sync_refreshes_planner_statistics` |
| old routing call shape (see §4) | 3 new tests in `test_routing_pipeline.py` |

## 4. Cross-track bug found by the e2e test: decision-model call shape

The routing track called `score(state="Task+Tool…", SCORE_QUESTION)` and
`noul(state="Task+Tools…", NOUL_QUESTION)`. The inference track's
deterministic-v1 model, Laya's `score_batch(state, questions, levels)` and the
bench all assume the opposite: `state` is the **task**, and each candidate is
in the **question**.

The effect with the default zero-ML backend: **every route was a no_match**,
because noul compared the task to a constant sentence. Score also gave every
candidate the same relevance. The routing tests never noticed because they
use scripted models.

Fixed in `routing/pipeline.py`:

* `state = query` everywhere.
* One Score question per candidate, sent as one `score_batch` call when the
  model offers it (Laya: one forward pass).
* The Noul question lists the exposed candidates one per line.

Regression tests run the real `DeterministicDecisionModel`.

## 5. Contract adoptions (models.py)

* `ServerCredentialRecord` (`mcp_server_credentials`) moves into `models.py`.
  `discovery/credentials.py` keeps the helpers and re-exports the class.
* New `MCPToolRecord` columns:
  * `title` and `annotations`, written by discovery sync and used in removal snapshots.
  * `classification_source`, which holds the classifier name or `human`.
* `DuplicateSuggestion.resolved_by`, `resolved_at` and `resolution_note`.
  Review sets them and the wire exposes them. **Judgment call:** the old
  "append to `rationale`" workaround is kept, for wire and test compatibility.
* **Judgment call:** `create_all` never alters an existing table. So
  `init_db` also runs literal, idempotent `ALTER TABLE … ADD COLUMN IF NOT
  EXISTS` statements for the six adopted columns, which lets pre-existing dev
  databases keep working. This is a bridge, not migrations.

### Alembic TODO (deferred, out of scope for this pass)

There is no Alembic baseline yet. Schema comes from one `create_all`-style
path (`db.init_db`) plus idempotent DDL in three places:

* `registry/schema.py`: `ix_tools_fts`, `ux_dup_pair`.
* `routing/retriever.ensure_keyword_index`: `ix_tools_routing_fts`, created
  with a non-CONCURRENT `CREATE INDEX` at startup.
* The additive ALTERs above.

The baseline migration must include `Base.metadata`,
`SecurityBase.metadata` and `eval_metadata`, plus those statements verbatim.

## 6. Settings added

| Variable | Default | Notes |
|---|---|---|
| `MCPR_DECISION_TIMEOUT_S` | 2.0 | |
| `MCPR_IDLE_UNLOAD_S` | 300 | |
| `MCPR_EMBED_BATCH_SIZE` | 32 | |
| `MCPR_LAYA_NOUL_MODE` | choice | The default Laya loader honours it |

All four follow the empty-string-means-unset rule. `MCPR_ADMIN_TOKEN` stays
out of `Settings` (secret) and is read from `env`. `docker-compose.yml` now
passes it through.

## 7. UI contract reconciliation

| # | Guess | Outcome |
|---|---|---|
| 2 | register body `stdioCommand` | Backend accepts it as an alias of `command` |
| 3 | refresh returns MCPServer | Client unwraps `{server, …}` |
| 4 | `PATCH /servers/{id} {enabled}` | Added (admin) |
| 5, 6 | flat stats, version ids | Client flattens `stats{}` and synthesises version ids |
| 8 | embedded tools | Client takes ids from the slim refs; the page fetches full tools by id |
| 12 | route casing | Backend accepts camelCase bodies |
| 14 | models health shape | Client maps the backend payload |
| 17 | executions | Backend endpoint added |
| 19 | rules path | Client uses `/policy-rules` |
| 18 | principals | Already matched |

Items not reconciled are in the gap list (#1, #3 below).

## 8. Gaps deferred

1. **The UI sends no `Authorization` header.** It works only in dev mode.
   With an admin token configured, every UI call gets 401. Needs an admin
   token entry, which sits outside the UI's API-only edit zone.
2. **Simulator identity.** The UI simulator sends a free-text `agentId`. In
   dev mode only `dev` (or no agent) passes; any other value is 403 by design.
   Proposal: an admin-only `POST /api/v1/route/simulate` that routes under a
   named agent's scope without publishing exposure. The switch needs a page
   test edit, which is outside the zone.
3. **Nothing in production runs classification or embedding after discovery.**
   No production code calls `auto_classify` or `embed_pending_tools` after
   discovery, so new tools stay `operation=unknown`. Policy treats `unknown`
   as execute, so read-only agents see nothing. The tools also stay
   unembedded, which leaves the vector leg empty. The e2e test runs both
   explicitly. **Highest-priority follow-up:** a post-sync hook on refresh
   and in `SyncLoop`.
4. The `SyncLoop` is not started by `create_app` (no setting to enable it).
5. Two usage-stat implementations exist. The execution manager keeps a
   running mean, while `registry.stats.record_execution` (EMA, alpha 0.2) is
   unused. Pick one.
6. Neither gateway exposure nor manager availability checks
   `server.status != 'offline'`, which discovery's eligibility rule includes.
7. Gateway DNS-rebinding allowed hosts are fixed to localhost. Behind a proxy
   or another container's hostname, `/mcp` answers 421. There is no setting yet.
8. `ConnectorToolInvoker` opens one connection per call; stdio servers spawn
   per call.
9. **Laya latency.** With real BGE + Laya, a route through the integrated app
   took 1.4–1.7 s on this CPU box (smoke run, no fallback). Each decision call
   has a 2 s deadline, and `score_batch` over about 20 candidates could
   exceed it on a slower laptop CPU, which forces the fallback. Tune
   `MCPR_DECISION_TIMEOUT_S`, or Laya-score only the top ~5 (inference §8).
   Validate on the RTX 4060.
10. `/route/evaluate` runs under the live policy resolver, so dataset agents
    without rules get no_match everywhere. Eval routes are also persisted as
    `routing_decisions`.
11. Stdio credentials are stored plaintext at rest. `policy_rules.server_id`
    has no foreign key (discovery §8).
12. In dev mode the Dockerfile's `0.0.0.0` bind exposes the open admin API to
    the network. The startup warning says so.
13. Smaller items:
    * `/api/v1/metrics` alias missing (still `/metrics`).
    * `stdioCommand` is not returned on reads (argv may hold secrets).
    * Accept ignores `preferredToolId`.
    * GPU name and utilization are not reported.
    * The response `tools[]` has no `tool_id`.

## 9. Review fixes (reviewer subagent, no blockers)

* **SF-1.** `DeadlineDecisionModel.name` and the engine handles never load on
  the request thread. `decision_model()` and `embedding_backend()` now load
  outside the engine lock.
* **SF-2.** Legacy-session `tools/list_changed` notifications are sent
  concurrently, so N stalled sessions cost about one 2 s timeout.
* **N-1.** The synthetic `dev` scope is granted only while dev mode is active.
* **N-3.** A failure to publish exposure is logged and does not turn `/route`
  into a 500.

All four are mutation-checked. Two review items are noted here but not
changed:

* **N-2 (fails closed, as intended).** In dev mode, creating the first
  principal through the API ends dev mode. With no `MCPR_ADMIN_TOKEN`, the
  admin API then returns 403 until a restart with a token.
* **CI Node version.** CI pins `node-version: "20"`, which matches the UI's
  Node-20 pins (vite 7, vitest 4). Node 20 has been end-of-life since
  2026-04. Bumping it means re-validating the UI toolchain on 22.

## 10. Ports

`tests/test_e2e_integration.py` uses 8700 (testbed fleet) and 8710 (uvicorn).
`tests/test_execution_invoker.py` uses 8719 as a known-dead port. Other
tracks keep their own ranges (8600s, 8641, 8659).
