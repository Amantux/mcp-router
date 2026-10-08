# Integration notes: gateway and security workstream (`feat/gateway`)

This is what the integrator needs to wire, along with every contract
addition. The security model itself is in `docs/security-model.md`.

## 1. Wiring in `create_app` (app.py, at integration)

```python
import os
from mcprouter.api.deps_auth import configure_security
from mcprouter.api.routes_policy import router as policy_router
from mcprouter.execution.manager import ExecutionManager
from mcprouter.gateway.server import build_gateway

# after app.state.settings / .engine / .session_factory are set:
configure_security(app, os.environ)       # tables + env bootstrap + app.state.security;
                                          # raises AgentKeysConfigError on bad config
manager = ExecutionManager.from_settings(settings, app.state.session_factory, tool_invoker)
app.state.execution_manager = manager
app.include_router(policy_router)         # /api/v1/principals, /policy-rules, /approvals, /me
build_gateway(app, manager=manager, route_fn=route_fn)   # POST/GET/DELETE /mcp
```

- `build_gateway` appends a Starlette `Route("/mcp")`. It also wraps
  `app.router.lifespan_context` so the SDK's `session_manager.run()` runs for
  the lifetime of the app, because a mounted sub-app's lifespan never runs.
  Call it once per app.
- **DNS-rebinding protection.** By default, `build_gateway(host="127.0.0.1")`
  allows only `Host: 127.0.0.1:*`, `localhost:*` and `[::1]:*`. Behind Docker
  or a reverse proxy, pass
  `transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[...], allowed_origins=[...])`.
  Otherwise every request is rejected with 421 or 403.
- **Run a single uvicorn worker.** The rate limiter, exposure sets and
  notification routing are in-process state.
- **Stale app.py warning.** It says auth is disabled when
  `MCPR_AGENT_KEYS` is unset. Auth is now disabled only when there are also
  no `MCPR_ADMIN_TOKEN` and no principals; `deps_auth` logs the accurate
  `auth.dev_mode` warning when that is the case. Delete or reword the
  app.py warning.

## 2. Seams to wire (protocols appended to `interfaces.py`)

| Seam | Protocol | Provided by | Notes |
|---|---|---|---|
| Upstream tool calls | `ToolInvoker.call_tool(server, tool_name, arguments, timeout_s) -> ToolCallResult` (async) | discovery / mcpclient track | See the adapter rules below. |
| Routing | `RouteFn(RouteRequest) -> RouteResult` (sync; run in a worker thread) | routing track | `RouteResult.tools[].tool_id` must be the internal tool UUID. |

**ToolInvoker adapter rules:**

- Convert the SDK `CallToolResult` to
  `ToolCallResult(content=[c.model_dump(by_alias=True, mode="json") for c in r.content], is_error=r.is_error, structured_content=r.structured_content)`.
- On transport or upstream failure, raise
  `ToolInvocationError("<curated text>")`. Never include the upstream body
  or `str(exc)`.
- Honour `timeout_s`. The manager also enforces it with `anyio.fail_after`.

**REST `/api/v1/route` (routing track):**

1. Authenticate with `Depends(get_principal)` and route for
   `principal.agent_id`. **Ignore the body's `agent_id`**, which spec §9
   includes; trusting it is identity spoofing.
2. After routing, call
   `request.app.state.gateway.apply_route_threadsafe(principal.agent_id, result)`
   from the sync endpoint. This publishes the new exposure to the agent's MCP
   sessions and sends `tools/list_changed`. Use `await gateway.apply_route(...)`
   from async code.
3. For the authorization filter, call
   `mcprouter.policy.engine.evaluate(principal, server, tool, rules)`. **Do
   not write a second policy implementation.** Its signature is score-free on
   purpose.
4. Pass model inputs (query, tool descriptions) through
   `mcprouter.execution.redaction.redact()` before inference (FR-07 "secret
   redaction in model inputs").

## 3. Contract additions (all append-only)

- `interfaces.py`: `ToolCallResult`, `ToolInvocationError`, `ToolInvoker`,
  `RouteFn`. No existing signature changed.
- `ExecutionRecord.outcome` gains new values: `invalid_args`, `unavailable`,
  `pending_approval`, `started`, `cancelled`. All fit `String(16)` and no
  column changed. The UI and metrics should treat `started` as in-flight, or
  as a crashed process if it persists.
- `MCPToolRecord.call_count`, `error_count` and `avg_latency_ms` are
  **updated by the execution manager** with one atomic UPDATE per invocation.
  Discovery and registry refresh must not overwrite them.
- New table `approval_requests` on `execution.models.SecurityBase.metadata`.
  It is a separate metadata because `models.py` is frozen.
  `configure_security` creates it with `create_all`. Alembic adoption must
  include this metadata. tests/conftest.py `_CLEAN_TABLES` could add
  `approval_requests`; my tests clean it themselves through the `sec_db`
  fixture.
- New environment variable `MCPR_ADMIN_TOKEN`. `settings.py` was frozen, so
  it is read through the `env` mapping passed to `configure_security`. At
  integration it could move into `Settings`, keeping the same name.
- Dependencies: `jsonschema==4.26.0`, plus dev `types-jsonschema==4.26.0.20260408`.

## 4. Wire shapes

- MCP endpoint: **`/mcp`** (streamable HTTP, both the 2025-11-25 handshake
  era and the 2026-07-28 era). Tool names are `server_name.tool_name`. The
  meta tool `router.find_tools` is present only when a `RouteFn` is wired.
- REST (camelCase) under `/api/v1`:
  - **admin:** `GET|POST /principals`, `GET|PATCH|DELETE /principals/{id}`,
    `POST /principals/{id}/rotate-key`, `GET|POST /policy-rules`
    (`?agentId=`), `PATCH|DELETE /policy-rules/{id}`,
    `GET /approvals?status=`, `POST /approvals/{id}/approve|deny`
    (status codes: 404 not found, 409 already decided, 410 expired).
  - **agent:** `GET /me`, `GET /me/approvals/{id}`.

## 5. What the mcp 2.3 SDK allowed (verified against the installed source)

- `MCPServer` has one process-wide `ToolManager`, so it cannot serve
  per-session tool lists. The gateway uses the low-level
  `mcp.server.lowlevel.Server(on_list_tools=..., on_call_tool=..., on_subscriptions_listen=...)`.
  Handlers receive a `ServerRequestContext` whose `.request` is the
  Starlette request for **every** message, which is how the principal
  reaches each handler.
- `streamable_http_app()` builds `InitializationOptions` through
  `create_initialization_options()` with no `NotificationOptions`, which
  would advertise `tools.listChanged=false`. The gateway overrides that
  method to default to `tools_changed=True`. On the 2026-07-28 era the flag
  derives from the registered listen handler.
- Handshake era: `ServerSession.send_tool_list_changed()` on the standalone
  stream works outside a request, as long as a reference to a request's
  `ctx.session` is kept. The e2e test verifies this.
- 2026-07-28 era: change notifications travel **only** on
  `subscriptions/listen` streams; connection-level copies are dropped by the
  SDK. The SDK's `ListenHandler` fans out to every listener on its bus, so
  the gateway keeps **one bus and handler per agent**. The e2e test verifies
  no cross-agent leak.
- Session ownership: the SDK's `StreamableHTTPSessionManager` compares
  `scope["user"]` (an `AuthenticatedUser`) with the session creator and
  returns 404 on mismatch. The gateway sets that user from the API-key
  principal, with a hash in `AccessToken.token`, not the raw key, rather
  than using the SDK's OAuth `AuthSettings`, which require issuer metadata.

## 6. Tests and ports

- Tests: `tests/test_policy_*.py`, `tests/test_execution_*.py`,
  `tests/test_gateway_*.py`. Shared fixtures live in
  `tests/test_execution_support.py`, which registers the `sec_db` fixture.
- The e2e tests run uvicorn on **127.0.0.1:8641**. The remote-`$ref` SSRF
  test opens a listener on **127.0.0.1:8659**.
