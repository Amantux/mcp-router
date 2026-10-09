# Architecture

One page on how MCP Router is built. Product overview: [README](../README.md).
Security posture: [security-model.md](security-model.md).

## Shape: a modular monolith, one worker

One FastAPI process hosts everything: the REST API (`/api/v1`), the MCP gateway
(`/mcp`), the dashboard (`/`), the inference engine, and the background sync
and rollup loops. There is no Redis, no queue and no sidecar. Postgres with
pgvector is the only other service.

Run **exactly one uvicorn worker**. Rate limits, MCP sessions, exposure sets,
the route cache and the loaded model are all in-process; a second worker would
split them and run every background loop twice. From v0.6 a Postgres advisory
lock (`src/mcprouter/singleton.py`) makes sure only one process runs the
background loops even if several are started by mistake.

## Request lifecycle

```
agent ──HTTP──▶ Host check (421, from v0.6) ─▶ body cap (1 MiB) ─▶ auth
       │                                                         │
       ├─ POST /api/v1/route ──▶ policy scope ─▶ hybrid retrieval ─▶ decision model
       │                          (what may this     (pgvector + FTS,    (choice/score/noul,
       │                           agent see?)        RRF fusion)         deadline + fallback)
       │                                                         │
       │                         ◀── re-filter by policy ◀── budgets (tools, servers, skills)
       │
       └─ /mcp tools/call ─────▶ ExecutionManager: policy ─▶ schema validation ─▶
                                 rate limit ─▶ approval gate ─▶ downstream call (timeout) ─▶ audit row
```

1. **Authenticate** (`api/deps_auth.py`): agent key or admin token. An agent
   key is never an admin credential.
2. **Route** (`routing/pipeline.py`): the policy engine first narrows the
   catalog to what the agent may use; retrieval and the decision model only
   rank inside that scope; policy filters the ranked list again. A model score
   can never widen access.
3. **Expose** (`gateway/`): the routed set becomes the agent's MCP `tools/list`
   for its sessions; `router.find_tools` re-routes and pushes `list_changed`.
4. **Execute** (`execution/manager.py`): one path for MCP calls and the REST
   playground. Every attempt, including refusals, writes an
   `execution_records` row.

## Data model

All state is in Postgres.

| Area | Tables |
|---|---|
| Catalog | `mcp_servers`, `mcp_server_credentials`, `mcp_tools` (with embeddings), `tool_versions`, `duplicate_suggestions` |
| Identity and policy | `agent_principals` (hashed keys), `policy_rules`, `approval_requests` |
| Routing and audit | `routing_decisions`, `execution_records`, `route_feedback`, `tool_stats_daily` |
| Skills | `skill_sources`, `skills`, `skill_versions` |
| Settings | `app_settings` (setup wizard state) |

From v0.6 the schema is managed by Alembic migrations, applied at startup
under a lock; see [upgrade.md](upgrade.md).

## Module boundaries

| Package | Owns |
|---|---|
| `mcpclient/` | Connectors to downstream servers (stdio, streamable HTTP, SSE). |
| `discovery/` | `tools/list` sync, schema versioning, health, config import. |
| `registry/` | Catalog queries, classification and review, usage stats. |
| `dedup/` | Duplicate detection; suggestions only, never auto-disable. |
| `inference/` | Embedding and decision backends and the engine that loads them. |
| `routing/` | Retrieval, the routing pipeline, budgets, route cache. |
| `policy/` | The deny-by-default rules engine and scope filter. |
| `execution/` | Validation, approvals, rate limits, redaction, audit. |
| `gateway/` | The MCP endpoint, per-agent exposure, skill exposure. |
| `analytics/` | Funnel, attribution, rollups, Prometheus collector. |
| `skills/` | Skill sources, ingest and validation, safe file serving, bundles. |
| `api/` | FastAPI routers. Thin: parse, authorize, call a service, shape the response. |

**Layering rule (from v0.6):** nothing outside `api/` imports `mcprouter.api`.
Authentication primitives live in `mcprouter/auth/`; `api/deps_auth.py` only
adapts them to FastAPI. `tests/test_layering.py` enforces it.

## Extension seams

The contracts are in `src/mcprouter/interfaces.py`:

- `EmbeddingBackend` (hash, BGE, Azure OpenAI);
- `DecisionModel` and `BatchScoringDecisionModel` (deterministic, Laya,
  remote, Azure OpenAI). A decision model only picks among options it is
  given; it never names a tool or executes anything;
- `Retriever`, `ScopeFilter`, `ToolInvoker` and `RouteFn`, which the tests use to
  swap in fakes.

Adding a backend means implementing the protocol and registering it in
`inference/engine.py`.

## Scale ceilings

- Catalog target: 100 servers and 1,000 tools (synthetic fleet; full refresh
  measured at 6.1 s on CPU).
- Dedup compares every embedding pair. From v0.6 one scan stores at most
  `MCPR_DEDUP_MAX_PAIRS` pairs and reports `truncated`.
- One process is the ceiling for request concurrency. Inference runs 1 to 4
  concurrent model calls depending on `MCPR_OPERATING_MODE`.
- The dashboard's Tools funnel column covers the first 500 rows.
