# Integration notes — routing track (feat/routing)

FR-03 / FR-04 / FR-06 routing, SPEC §9 `POST /api/v1/route`, SPEC §10
evaluation. Coded only against `interfaces.py`; no import from
`mcprouter.inference`.

## One-line app.py integration

```python
from mcprouter.api.routes_route import install_routing
install_routing(app, RoutePipeline(factory, HybridRetriever(factory, embedder), decision_model, settings),
                scope_resolver=<gateway's agent_id -> ScopeFilter>)
```

`install_routing` also runs `ensure_keyword_index(app.state.engine)` (see
"DDL" below) and logs a loud warning when `scope_resolver` is None.

## Contract additions (append-only)

- `interfaces.ScopeFilter` (commit 25cb674): `server_ids() -> list[str] | None`
  and `permits(candidate: ToolCandidate) -> bool`. The **gateway track
  implements it** from AgentPrincipal/PolicyRule (deny-by-default). Routing
  narrows within it, never widens it.
- `RouteRequest.allowed_servers` is treated as **server ids** inside the
  pipeline. The API resolves names to ids.

## Seams the gateway must close

1. **Scope resolver.** `get_scope_resolver` (FastAPI dependency) returns
   `app.state.route_scope_resolver`, or a **permissive AllowAllScope by default**
   (dev only, loudly logged). The gateway should install a resolver, or
   override the dependency with one that (a) authenticates the request,
   (b) rejects a body `agent_id` that does not match the authenticated
   principal, and (c) returns that principal's ScopeFilter. Until then, any
   caller can rank over the whole catalog as any agent. Routing never
   executes anything; post-ranking authorization (FR-07) is still required.
2. **Where scope is applied.** It is applied before any model call
   (`server_ids` pushed into SQL; `permits` filters candidates), and again on
   the exposed list (defence in depth). A denied tool is never shown to the
   decision model.
3. **Secret redaction.** The query goes into decision-model inputs and into
   `routing_decisions.query` **unredacted**. No redaction helper exists in the
   scaffold. Whoever owns redaction should apply it before `pipeline.route`.
   Routing logs only `request_id` plus the exception type name, never the query.

## API decisions

- Wire shape is SPEC §9 verbatim, **snake_case**: scoping.md §10 makes §9
  canonical, which overrides the CLAUDE.md camelCase convention. Response adds
  `no_match`.
- `max_tools` is optional (defaults to `max_exposed_tools`), `ge=1`. The
  pipeline also clamps into `[1, max_exposed_tools]` for non-API callers.
- Empty or whitespace `query`, empty `agent_id`, or a query over 4,000 chars
  returns 422.
- **Unknown `allowed_servers` names return 400** with `unknown_servers` (capped at
  50). They are never silently ignored: dropping `["githb"]` would turn the
  request into "no restriction" and widen the caller's own filter.
  `allowed_servers: []` means no servers, so the result is `no_match`.
- No pipeline installed returns 503 "Routing is not configured on this
  instance."
- `POST /api/v1/route/evaluate {dataset, max_tools=5}` runs a server-shipped
  dataset. Names are whitelisted from the `eval/datasets/*.jsonl` listing,
  never path-joined. Unknown names return 404. It runs synchronously (77 routes
  for synthetic_v1), so it should sit behind admin auth in the gateway.

## Pipeline semantics (pipeline.py docstring is authoritative)

- Domain Choice runs over the distinct candidate domains. It keeps the chosen
  domain plus any domain with p >= 0.5·p(top), so cross-domain workflows can
  survive. Unclassified (`domain=None`) candidates are always kept.
- Operation Choice is **soft**: a mismatch multiplies the score by 0.5 and the
  tool is never dropped. A classifier error must not hide the only right tool.
- Score uses a 5-level relevance scale. The expected level, in [0,1], is
  blended 0.7/0.3 with the RRF retrieval score.
- Noul p(yes) < `route_confidence_floor` returns `no_match=true` with no tools.
- **Fallback (FR-06):** any model exception, or a contract violation (an option
  that wasn't offered, malformed or NaN probabilities), falls back to
  retrieval-only ranking with `fallback_used=true` and
  `model_version="retrieval-fallback/<model>"`. DB and retrieval errors are
  not swallowed. In fallback mode there is **no no-match detection** (no
  model). no_match is only returned when retrieval finds nothing.
- Ties break on `(server_name, tool_name)`, not random UUIDs, so evals are
  reproducible.
- FR-04 "→ server" stage: not a separate Choice. Candidates are
  server-qualified and the Score stage ranks them directly. Revisit if Laya
  numbers show server confusion among overlapping tools.

## DDL owned by this track (move to Alembic at integration)

- `ix_tools_routing_fts`: a GIN expression index on `mcp_tools`, created
  idempotently by `ensure_keyword_index`. The expression must stay
  byte-identical to `retriever._DOC_TEMPLATE`, otherwise Postgres silently
  stops using it. `test_keyword_index_is_idempotent_and_matches_the_query`
  guards this.
- `eval_results`: a table on `eval.store.eval_metadata` (its own MetaData,
  because models.py is frozen), created with `checkfirst`.
- Tests: `conftest._CLEAN_TABLES` does not include `eval_results`. The eval
  endpoint test deletes its own rows. Add it there at integration.

## Performance (dev box, no GPU, oversubscribed shared host)

- Measured on 1,000 tools: keyword leg 27ms → ~8ms with the GIN index; vector
  leg 18ms → 2.5ms once the planner has statistics.
- **Registry track: run `ANALYZE mcp_tools, mcp_servers` after bulk sync.**
  Without it the planner estimates ~4 rows and the vector leg runs about 7x
  slower.
- Decision-row insert uses `SET LOCAL synchronous_commit TO OFF`, because it is
  telemetry.
- `db.make_engine` uses `pool_pre_ping=True`, which adds one round trip per
  checkout (2 per route). Worth reconsidering for the hot path.
- Perf test is opt-in (`MCPR_RUN_SLOW=1`). Pipeline overhead excluding model
  and embedder time:

  | Measure | Result |
  |---|---|
  | p50 | 18–33ms |
  | best-of-3 p95 | 29–47ms at load ≤ 160, 49–57ms at load ~220 |

  The real number must come from the target laptop. Register the `slow`
  marker in pyproject at integration; it is currently unregistered, which
  produces one PytestUnknownMarkWarning.

## Eval baseline (synthetic_v1, 77 cases; fake hash embedder; untuned)

| backend | top-1 | top-5 recall | wrong-tool | no-match acc | denied acc | unauth/unavail exposures |
|---|---|---|---|---|---|---|
| retrieval fallback (pre-Laya baseline) | 0.759 | 0.911 | 0.241 | 0.000 | 1.000 | 0 / 0 |
| flat scripted model (uniform probs, picks first option) | 0.310 | 0.770 | 0.690 | 0.000 | 1.000 | 0 / 0 |

The flat-model row shows that a wrong *hard* domain prune is costly. An
uninformed domain classifier cuts top-1 from 0.76 to 0.31. Calibrated Laya
probabilities, together with the 0.5 keep-margin, are what make that prune
safe. Check this first when real Laya numbers arrive.

## Adversarial review: deferred items (fixed items are in commit history)

A reviewer subagent's verdict was "PASS WITH NOTES". These were fixed: the
pipeline now enforces server scope itself; control characters (including
NUL) are rejected with 422; the 422 is curated and no longer echoes input;
the 400 no longer acts as an existence oracle for out-of-scope servers;
errored eval cases stay in the denominators; model_version is truncated.
These remain open:

- **`/route/evaluate` needs admin auth plus single-flight** (gateway track).
  It runs a full dataset synchronously in the threadpool. Its routes are also
  persisted as RoutingDecisionRecords under the dataset's agent_ids, so eval
  traffic mixes into live telemetry. Consider an `eval:` agent prefix or a
  persist flag.
- **No model-call timeout.** A *hung* DecisionModel never triggers the FR-06
  fallback. Sync calls cannot be interrupted from the pipeline, so the
  inference backend must enforce a per-call deadline and raise.
- **`unauthorized_exposures`** counts only each case's hand-listed
  `forbidden_tools`. Exposure of other write tools to readonly-agent is
  prevented by the pipeline's own `permits` re-check, but the metric does not
  independently measure it.
- **`latency_ms`** is measured inside the pipeline. It excludes API-side name
  resolution and the decision-row insert.
- The post-exposure `permits` re-check is masked by the pre-filter, so no test
  isolates it.
- Nits: the `[a-z0-9]+` word split breaks non-ASCII words for the keyword
  leg. Tools with `unknown` operation are never down-weighted. Response
  `score` is a blended and weighted value, not the raw calibrated probability
  (scoping.md §10 wording). `install_routing` runs a non-CONCURRENT
  `CREATE INDEX` at startup; move it to Alembic.
