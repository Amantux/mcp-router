# Integration notes — UI (feat/ui)

The UI (`ui/`, React 18 + Vite 7 + Fluent UI v9) was built while the backend
was in flight. Every endpoint call lives in **one module**,
`ui/src/api/client.ts`, with wire types in `ui/src/api/types.ts`. Reconcile
drift there; no page calls `fetch` directly. Every guess is marked
`// CONTRACT:` in those two files and listed below.

## Wire conventions the client assumes

- **Responses**: deep snake_case→camelCase key conversion happens client-side,
  so either backend casing works. Exception: keys under `inputSchema`,
  `snapshot` and `scores` are passed through untouched (that's user and tool data).
- **Request bodies**: sent **camelCase** (CLAUDE.md wire rule). SPEC §9 shows
  `POST /route` as `{query, agent_id, max_tools}`. The backend should accept
  camelCase, ideally both (`populate_by_name` + alias generator).
- **Query params**: camelCase (`serverId`, `agentId`). Check FastAPI aliases.
- **Lists**: either a bare array or `{items, total, limit, offset}`. Paged
  views (tools, executions) need `total` for the pager. With a bare array they
  show one page.
- **Errors**: the UI reads only the HTTP status and shows its own copy.
  Response bodies are never displayed.

## Endpoint guesses

| # | Call | Guess |
|---|------|-------|
| 1 | `GET /api/v1/servers` | Each server has a `toolCount` (shown in table; "—" if absent). `stdioCommand: string[]` for stdio. |
| 2 | `POST /api/v1/servers` | Body: `{name, transport:"stdio", stdioCommand:[cmd,...args]}` or `{name, transport:"streamable-http"\|"sse", endpoint}`. UI only allows http(s) URLs. The backend must re-validate at point of use. |
| 3 | `POST /api/v1/servers/{id}/refresh` | Returns the updated `MCPServer` (toolCount used in toast if present). |
| 4 | `PATCH /api/v1/servers/{id}` | `{enabled: bool}` for enable/disable. **Not in SPEC §9.** |
| 5 | `GET /api/v1/tools` | Params `q, domain, operation, serverId, enabled, available, limit(50), offset`. Items are `MCPTool` with optional `serverName`, `available`, `classificationReviewed`, `callCount`, `errorCount`, `avgLatencyMs`, `domain`, `capabilities`. |
| 6 | `GET /api/v1/tools/{id}` | `MCPTool` + `versions: ToolVersion[]` (`{id, version, schemaHash, changeKind, recordedAt, snapshot?}`). Missing `versions` → `[]`. |
| 7 | `PATCH /api/v1/tools/{id}/classification` | Body `{domain: string\|null, operation, tags[], requiredScopes[]}` → updated `MCPTool`; expected to set `classificationReviewed=true`. |
| 8 | `GET /api/v1/dedup/suggestions?status=open` | Suggestions ideally embed `toolA`/`toolB`. If absent the UI calls `GET /tools/{id}` for each (2 requests per card). |
| 9 | `POST /api/v1/dedup/suggestions` | Triggers a scan; response ignored. |
| 10 | `POST /api/v1/dedup/suggestions/{id}/accept` | Body `{preferredToolId}`. |
| 11 | `POST /api/v1/dedup/suggestions/{id}/dismiss` | Body `{justification}` (non-empty; UI enforces it, backend should 422 on empty). |
| 12 | `POST /api/v1/route` | Response: `{requestId, tools:[{toolId?, serverName\|server, toolName\|tool, score}], fallbackUsed, latencyMs, modelVersion?, noMatch?}`. SPEC §9 `{server,tool}` and `interfaces.RoutedTool` `{server_name,tool_name}` both accepted. An empty `tools` also counts as no-match. |
| 13 | `POST /api/v1/route` auth | **No auth header is sent.** If `/route` requires an agent key when `MCPR_AGENT_KEYS` is set, the simulator will 401. Decide on admin auth for the UI (see Flags). |
| 14 | `GET /api/v1/models/health` | Whole shape guessed: `{device, mode, gpu?:{name,memoryUsedMb,memoryTotalMb,utilizationPct}, memory?:{rssMb,systemUsedMb,systemTotalMb}, models:[{name,kind,backend?,device?,loaded,version?}], routingP50Ms?, routingP95Ms?}`. |
| 15 | `GET /healthz` | `{status:"ok"}` (verified in `api/app.py`); a non-2xx means "failing". |
| 16 | Metrics link | Links to `/metrics`, where `app.py` mounts it. SPEC §9 says `/api/v1/metrics`; change `METRICS_URL` if that moves. |
| 17 | `GET /api/v1/executions` | Params `agentId, outcome, limit, offset` → page of `{id, agentId, toolId?, serverId?, toolName?, serverName?, outcome, detail, latencyMs?, createdAt}`. |
| 18 | `GET/POST /api/v1/principals` | Create body `{agentId, maxTools}`; response = principal + **`apiKey`** (plaintext, once). |
| 19 | `GET/POST /api/v1/rules` | `{agentId, serverId\|null, toolName\|null, maxOperation, requiresApproval}`. Path could instead be `/api/v1/policy/rules`; one-line change in client.ts. |

## Not built (out of fence / not requested)

- No delete/edit for servers, principals, or rules (create + list only).
- `allowedServers` isn't exposed in the simulator form (the type supports it).
- No generated OpenAPI types yet (scoping #9). Swap `types.ts` for generated
  types at integration and keep `client.ts` as the seam.

## Dev

- `cd ui && npm ci && npm run dev` serves on :5180 (strictPort). `/api`, `/mcp`,
  `/healthz` and `/metrics` are proxied to :8400.
- Gates: `npm run build` (tsc + vite), `npm run lint`, `npm test` (vitest,
  jsdom, fetch mocked, no backend needed).
- Pinned to Node 20-compatible majors: vite 7, vitest 4, react-router 7,
  jsdom 26, TS 5.9, eslint 9. Newer majors need Node 22.
