# Integration notes — discovery workstream (FR-01 + shared testbed)

Branch `feat/discovery` off `645399f`. Nothing outside the fence was edited:
no change to `app.py`, `models.py`, `interfaces.py`, `settings.py`, `db.py`,
`pyproject.toml` or `tests/conftest.py`.

## 1. Router include (app.py)

```python
from mcprouter.api.routes_servers import router as servers_router
...
app.include_router(servers_router)
```

The import MUST happen before `init_db(engine)` runs (a top-of-module import
in app.py satisfies this): importing `routes_servers` imports
`mcprouter.discovery.credentials`, which registers the
`mcp_server_credentials` table on the shared `Base` so `create_all` creates
it. Optional: set `app.state.discovery = DiscoveryService(...)` in the
factory to share one health tracker with a `SyncLoop` (otherwise the router
creates one lazily on first refresh).

Background sync is OFF unless someone calls it. To enable in the app:

```python
from mcprouter.discovery import DiscoveryService, SyncLoop
svc = DiscoveryService(app.state.session_factory)
app.state.discovery = svc
loop = SyncLoop(svc, sync_interval_s=300, health_interval_s=60)
# in a FastAPI lifespan:  await loop.start()  ...  await loop.stop()
```

## 2. Endpoints (camelCase wire)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/servers` | list, ordered by name |
| GET | `/api/v1/servers/{id}` | 404 if missing |
| POST | `/api/v1/servers` | `{name, transport, endpoint?, command?[], env?{}, enabled?}` → 201; 400 invalid (curated), 409 duplicate name, 422 shape |
| POST | `/api/v1/servers/import` | Claude-Desktop `{"mcpServers": {...}}` → `{created[], skipped[{name, reason}]}`; 400 if the document itself is malformed |
| POST | `/api/v1/servers/{id}/refresh` | sync now → `{server, added[], schemaChanged[], metadataChanged[], removed[], restored[], unchanged, skipped, latencyMs}`; 404; 502 unreachable/protocol, 504 timeout, 400 invalid stored target — detail always `"discovery failed: <curated>"` |
| DELETE | `/api/v1/servers/{id}` | 204; 404; 409 while any `PolicyRule.server_id == id` |

Server shape = SPEC §8 MCPServer `{id, name, transport, endpoint, enabled,
status, version, lastDiscoveredAt}` + additive `lastHealthAt, toolCount,
envNames`. `env` is write-only (names only in responses). `status` can also
be `unknown` (never probed) — matches the model default.

**INTEGRATION BLOCKER — no auth on these routes.** Management-API auth is
not specified in the scaffold (no admin key in `Settings`, no auth
dependency anywhere in `src/`), and this branch may not edit `settings.py`.
The adversarial review proved it: anyone who can reach the API can
`POST {"transport":"stdio","command":["/bin/sh","-c",...]}` then `/refresh`
and execute commands as the app user; the Dockerfile binds `0.0.0.0`.
`/refresh` has no body, so a cross-origin "simple" POST from any web page
can re-trigger registered commands / dial registered URLs, and the curated
502/504 distinctions make it a weak internal port-probe oracle.
Required before this router is exposed: an admin-auth dependency on the
router (`APIRouter(dependencies=[...])` or at `include_router`), FAIL-CLOSED
when no admin credential is configured, plus a test that fails when it is
removed. Consider also an explicit opt-in setting for stdio registration.

## 3. Contract additions / needs

`interfaces.py`: **no additions.**

`models.py` needs (worked around locally, please adopt at integration):

1. **Credential storage for stdio `env`** — no column exists. Workaround:
   new table `mcp_server_credentials(server_id PK/FK→mcp_servers ON DELETE
   CASCADE, env JSON)` defined in `src/mcprouter/discovery/credentials.py`
   on the shared `Base`. Rationale for keeping it a separate table even after
   adoption: secrets stay out of the row every serializer touches. Stored
   **plaintext at rest** — FR-07 "server-side credential storage" is met,
   encryption-at-rest is not (flag for the security workstream).
   `tests/conftest.py::_CLEAN_TABLES` does not list it; rows go via the FK
   cascade from `mcp_servers`.
2. **Per-server discovery interval** — `SyncLoop(intervals={server_id: s})`
   / `set_interval()` is process-local. Want `discovery_interval_s: int|None`
   on `MCPServerRecord`.
3. **Health failure streak** — process-local in `HealthTracker` (a restart
   resets it; first failure after restart reads `degraded`, never worse).
   Optional `consecutive_failures: int` column if persistence matters.
4. **Tool title / annotations** — `MCPToolRecord` has no `title` or
   `annotations` columns. They are captured in every `ToolVersionRecord.snapshot`
   (`annotations.readOnlyHint` / `destructiveHint` are strong signals for the
   classifier's read/write/execute) and changes to them are versioned as
   `metadata`. Classifier can read the latest snapshot; a column would be
   cheaper.

## 4. Semantics other workstreams rely on

- **Tool identity**: `(server_id, name)` → stable `MCPToolRecord.id` across
  versions, removals and restores.
- **`available`** = "present in the last SUCCESSFUL listing". A server outage
  does NOT flip `available` (that would spam removed/restored versions).
  Eligibility for routing should therefore be
  `tool.enabled AND tool.available AND server.enabled AND server.status != 'offline'`.
- **Versioning**: every change bumps `MCPToolRecord.version` and appends a
  `ToolVersionRecord` (`added|schema|metadata|removed|restored`). Precedence
  when several change at once: restored > schema > metadata. Snapshot shape
  (camelCase): `{name, title, description, inputSchema, annotations, schemaHash}`.
- **`schema_hash`** = sha256 of canonical JSON (sorted keys, compact,
  `ensure_ascii=False`) of `inputSchema` — `mcprouter.discovery.hashing.schema_hash`.
- Discovery never touches classification/embedding columns (`domain`,
  `operation`, `embedding*`, `classification_reviewed`). Re-embed triggers
  should key on `embedding_text_hash` vs current text, as designed.
- Health: `healthy` (probe OK, ≤2s) / `degraded` (slow, or 1–2 consecutive
  failures of a previously-seen server) / `offline` (≥3 failures, or never
  successfully reached). Probe = connect + `tools/list` (`ping` is deprecated
  on 2026-07-28 MCP).

## 5. Connector API (for the gateway/execution workstream)

`mcprouter.mcpclient.Connector(target: ServerTarget | MCPServer, *,
connect_timeout_s=15, request_timeout_s=30)`: `connect() / initialize() ->
ServerInfo`, `list_tools() -> list[ToolDescriptor]`, `call_tool(name, args,
timeout_s=None) -> ToolCallResult(is_error, content, structured)`, `close()`;
use `async with`. Build targets from DB rows with
`mcprouter.discovery.registry.target_for(session, server)` (loads env).

- Every failure is a `ConnectorError` subclass with `.kind`
  (`invalid_target|spawn_failed|unreachable|timeout|protocol|not_connected`)
  and a fixed `.message` safe for API responses / `ExecutionRecord.detail`.
  Upstream MCP error *text* is dropped (only the numeric code survives).
- `ToolCallResult.content` is the tool's own output, passed through
  unmodified — it is upstream data. Redaction before model input is the
  gateway's job (FR-07).
- Subprocess stderr is discarded (devnull), never logged.
- `connect()` and `close()` must run in the same task (anyio).
- mcp 2.x facts: `mcp.Client` fuses transport + handshake;
  `mode="auto"` negotiates 2026-07-28 via `server/discover` and falls back
  to legacy `initialize`; transport errors arrive as nested ExceptionGroups;
  a timed-out stdio connect takes ~2s more to tear down the subprocess.
- Depends on `httpx2` (transitive via mcp, not declared in pyproject) for
  exception mapping.
- HTTP headers / bearer auth for remote servers are NOT supported yet; config
  entries with `headers` are refused at import rather than imported broken.

## 6. Testbed (for every workstream)

- `testbed.fleet.generate_fleet(n, tools=None)` — deterministic; 17 families
  over the 5 FR-04 domains; per-tool ground truth `domain`, `operation`
  (read|write|execute), `destructive`, `canonical` (dedup group: e.g.
  `files.read_file` and `fs.get_file` are both `filesystem.read_file`;
  `github.search_issues`/`gitlab.search_issues` share `code_host.search_issues`).
  `--tools` pads with `batch_*` variants (canonical suffixed `#batch`).
  Mutators: `without_tool`, `with_tool`, `with_description`, `with_extra_param`.
- `testbed.servers.build_server(spec)` — real `MCPServer`; advertises exactly
  `ToolSpec.input_schema()`; annotations from operation/destructive.
- `testbed.harness`: `InprocFleet` (connector factory: in-process servers,
  `down` set for outages), `http_fleet(n, port_base, ports, tools)`,
  `sse_server(spec, port)`, `stdio_command_for_index(...)` + `stdio_env()`
  (stdio children need `PYTHONPATH=<repo root>` because the SDK passes only a
  minimal default env).
- CLIs: `python -m testbed.fleet`, `python -m testbed.serve --servers N
  --port-base P [--ports K]` (many servers per port at `/<name>/mcp`; prints
  `READY {json}`), `python -m testbed.stdio_server`, `python -m testbed.seed
  --servers 100 --tools 1000 [--transport http]`.
- `testbed` is importable from tests via pytest rootdir; it is not in the
  installed package and not covered by the `mypy` gate (it passes
  `mypy --strict testbed` on its own).

Seeding requires an explicit DB (`--database-url` or `MCPR_DATABASE_URL`;
no fallback to the app default) and refuses to repoint an existing server
whose endpoint is not a testbed endpoint.

Ports used by this branch's tests: 8600–8605 (listeners), 8609 and 8619
(intentionally dead).

## 7. Measured (dev box, no GPU — not the target laptop)

SPEC §3 "catalog refresh < 60s for 1,000 tools": 100 servers / 1,000 tools
over real streamable HTTP (2 ports) through Connector + DiscoveryService +
PostgreSQL at concurrency 16: **cold 6.07s, warm 4.33s**
(`tests/test_testbed_scale.py`). Seed CLI: http 4.92s refresh, inproc 8.19s.

## 8. Known limitations / follow-ups

- Delete guard checks `PolicyRule.server_id` only (rules with
  `server_id=None` don't pin a server). Check-then-delete is not atomic with
  a concurrent rule insert (no FK on `policy_rules.server_id`); an FK with
  `ON DELETE RESTRICT` at integration would close it.
- `stdio` registration = arbitrary command execution on the host: the router
  needs admin auth (see §2).
- Delete permanently removes the server's tools and their
  `tool_versions` history (FK `ON DELETE CASCADE` in `models.py`). The guard
  now refuses while `PolicyRule.server_id` or any `ExecutionRecord.server_id`
  references the server (409, "disable it instead"). `DuplicateSuggestion` /
  `RoutingDecisionRecord.selected_tool_ids` are not checked. If version history
  must survive, switch to soft-delete at integration.
- `endpoint` query strings are redacted in responses (`...?redacted`);
  secrets embedded in the URL PATH (Zapier/Smithery-style) cannot be detected
  generically and are returned as-is to API callers.
- A tool that comes back (`restored`) with a different schema is recorded as
  `restored` only (precedence restored > schema); the snapshot holds the new
  schema and `schema_hash` is updated.
- Per-server process-local state (`DiscoveryService._locks`,
  `HealthTracker._failures`, `SyncLoop._last_*`) is never pruned on delete;
  small, but grows with churn. The router lazily creates its own
  `DiscoveryService` unless integration sets `app.state.discovery` (then
  router and loop share one health streak).
- `GET /api/v1/servers` is unpaginated (fine at the 100-server target).
- No unique index on `tool_versions(tool_id, version)`; uniqueness relies on
  the `FOR UPDATE` lock on the server row during reconcile. Worth adding.
- Upstream HTTP request URLs are logged at INFO by the SDK's httpx2 client
  (`HTTP Request: POST http://...`); a URL with a token in its query string
  would land in logs. Consider raising the `httpx2` logger to WARNING at
  app startup.
