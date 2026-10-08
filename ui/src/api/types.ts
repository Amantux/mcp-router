/**
 * Wire types for the MCP Router REST API (SPEC §8 / §9), camelCase.
 *
 * Every response passes through `camelizeKeys` in client.ts, so these hold
 * whether the backend emits snake_case (SPEC §9 literal) or camelCase
 * (CLAUDE.md wire convention). Fields marked `// CONTRACT:` are guesses beyond
 * the spec; each one is listed in docs/INTEGRATION_NOTES-ui.md.
 */

export type Transport = "stdio" | "streamable-http" | "sse";
// "unknown" is the ORM default before the first health check (models.py).
export type ServerStatus = "healthy" | "degraded" | "offline" | "unknown";
export type Operation = "read" | "write" | "execute" | "unknown";
export type OperationCeiling = "read" | "write" | "execute";
export type ExecutionOutcome = "ok" | "error" | "denied" | "timeout" | "rate_limited";
export type DedupStatus = "open" | "accepted" | "dismissed";

/** FR-04 domain vocabulary (matches feat/registry classify.py). */
export const DOMAINS = ["development", "communication", "files", "databases", "productivity"] as const;
export const OPERATIONS: Operation[] = ["read", "write", "execute", "unknown"];
export const CEILINGS: OperationCeiling[] = ["read", "write", "execute"];

export type JsonValue = string | number | boolean | null | JsonValue[] | { [k: string]: JsonValue };
export type JsonObject = { [k: string]: JsonValue };

/** Paged list envelope. */
// CONTRACT: list endpoints return {items, total, limit, offset}. A bare array is
// also accepted (normalised in client.ts) in case the backend returns one.
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

// ------------------------------------------------------------------ servers
export interface MCPServer {
  id: string;
  name: string;
  transport: Transport;
  endpoint?: string | null;
  // CONTRACT: stdio command line exposed as a string list (models.py stdio_command).
  stdioCommand?: string[] | null;
  enabled: boolean;
  status: ServerStatus;
  version?: string | null;
  lastDiscoveredAt?: string | null;
  // CONTRACT: server list includes a tool count so the UI needn't page the catalog.
  toolCount?: number;
}

// CONTRACT: register body. http/sse → endpoint; stdio → stdioCommand [cmd, ...args].
export type RegisterServerRequest =
  | { name: string; transport: "stdio"; stdioCommand: string[] }
  | { name: string; transport: "streamable-http" | "sse"; endpoint: string };

// ------------------------------------------------------------------- tools
export interface MCPTool {
  id: string;
  serverId: string;
  // CONTRACT: denormalised server name for display.
  serverName?: string;
  name: string;
  description: string;
  inputSchema: JsonObject;
  schemaHash: string;
  // SPEC §8 says categories[]; models.py has domain + capabilities. Both accepted.
  domain?: string | null;
  capabilities?: string[];
  categories?: string[];
  tags: string[];
  operation: Operation;
  requiredScopes: string[];
  enabled: boolean;
  // CONTRACT: availability/health flag (models.py `available`).
  available?: boolean;
  classificationReviewed?: boolean;
  version: number;
  // CONTRACT: usage stats flattened onto the tool (models.py column names).
  callCount?: number;
  errorCount?: number;
  avgLatencyMs?: number | null;
}

export interface ToolVersion {
  id: string;
  version: number;
  schemaHash: string;
  changeKind: "added" | "schema" | "metadata" | "removed" | "restored";
  recordedAt: string;
  snapshot?: JsonObject;
}

/** GET /api/v1/tools/{id} */
// CONTRACT: detail = MCPTool + `versions` (newest first or any order; UI sorts).
export interface ToolDetail extends MCPTool {
  versions: ToolVersion[];
}

export interface ToolQuery {
  q?: string;
  domain?: string;
  operation?: Operation;
  serverId?: string;
  enabled?: boolean;
  available?: boolean;
  limit: number;
  offset: number;
}

// CONTRACT: PATCH body; returns the updated MCPTool and marks it reviewed.
export interface ClassificationUpdate {
  domain: string | null;
  operation: Operation;
  tags: string[];
  requiredScopes: string[];
}

// ------------------------------------------------------------------- dedup
export interface DuplicateSuggestion {
  id: string;
  toolAId: string;
  toolBId: string;
  similarity: number;
  rationale: string;
  preferredToolId?: string | null;
  status: DedupStatus;
  createdAt: string;
  // CONTRACT: suggestions embed both tools; if absent the UI fetches them by id.
  toolA?: MCPTool;
  toolB?: MCPTool;
}

// ----------------------------------------------------------------- routing
// SPEC §9 body is {query, agent_id, max_tools, allowed_servers?}.
// CONTRACT: sent camelCase per the wire convention; backend should accept both.
export interface RouteRequest {
  query: string;
  agentId: string;
  maxTools: number;
  allowedServers?: string[];
}

/** SPEC §9 tools:[{server, tool, score}]; interfaces.RoutedTool adds tool_id. */
export interface RoutedTool {
  toolId?: string;
  serverName: string;
  toolName: string;
  score: number;
}

export interface RouteResponse {
  requestId: string;
  tools: RoutedTool[];
  fallbackUsed: boolean;
  latencyMs: number;
  modelVersion?: string;
  // interfaces.RouteResult.no_match — not in SPEC §9 literal, present in the contract.
  noMatch?: boolean;
}

// ------------------------------------------------------------ models/health
// CONTRACT: entire shape is a guess; SPEC only names the endpoint.
export interface LoadedModel {
  name: string;
  kind: string; // "embedding" | "decision"
  backend?: string; // e.g. "bge-small", "hash", "laya", "deterministic-v1"
  device?: string;
  loaded: boolean;
  version?: string;
}

export interface ModelsHealth {
  device: string; // "cuda" | "cpu"
  mode: string; // "performance" | "balanced" | "battery"
  gpu?: { name?: string; memoryUsedMb?: number; memoryTotalMb?: number; utilizationPct?: number } | null;
  memory?: { rssMb?: number; systemUsedMb?: number; systemTotalMb?: number } | null;
  models: LoadedModel[];
  // Optional latency readouts if the backend tracks them.
  routingP50Ms?: number;
  routingP95Ms?: number;
}

export interface Healthz {
  status: string;
}

// ------------------------------------------------------------- executions
// CONTRACT: GET /api/v1/executions?agentId=&outcome=&limit=&offset= → Page<ExecutionRecord>.
export interface ExecutionRecord {
  id: string;
  agentId: string;
  toolId?: string | null;
  serverId?: string | null;
  toolName?: string | null; // CONTRACT: denormalised for display
  serverName?: string | null; // CONTRACT: denormalised for display
  outcome: ExecutionOutcome;
  detail: string;
  latencyMs?: number | null;
  createdAt: string;
}

export interface ExecutionQuery {
  agentId?: string;
  outcome?: ExecutionOutcome;
  limit: number;
  offset: number;
}

// ----------------------------------------------------------------- policy
// CONTRACT: /api/v1/principals and /api/v1/rules (prompt says "under /api/v1").
export interface Principal {
  id: string;
  agentId: string;
  enabled: boolean;
  maxTools: number;
  createdAt: string;
}

export interface CreatePrincipalRequest {
  agentId: string;
  maxTools: number;
}

// CONTRACT: create returns the principal plus `apiKey` exactly once.
export interface CreatedPrincipal extends Principal {
  apiKey: string;
}

export interface PolicyRule {
  id: string;
  agentId: string;
  serverId: string | null;
  toolName: string | null;
  maxOperation: OperationCeiling;
  requiresApproval: boolean;
  createdAt: string;
}

export type CreateRuleRequest = Omit<PolicyRule, "id" | "createdAt">;

// -------------------------------------------------------------- execution
/** execution/manager.py statuses (ExecutionResult.status). */
export type ExecutionStatus =
  | "ok"
  | "error"
  | "timeout"
  | "denied"
  | "rate_limited"
  | "unavailable"
  | "invalid_args"
  | "pending_approval"
  | "cancelled";

/** MCP tool output (interfaces.ToolCallResult), camelised; content blocks are tool data. */
export interface ToolCallOutput {
  content: JsonObject[];
  isError?: boolean;
  structuredContent?: JsonValue | null;
}

// CONTRACT: POST /api/v1/tools/{toolId}/execute {arguments} → ExecutionManager's
// ExecutionResult {status, detail, recordId, approvalId?, errors[], result?}
// plus an optional server-side latencyMs. No REST execute endpoint exists in
// v0.1 (execution is /mcp tools/call + the approval endpoint); the integrator
// adds it or repoints executeTool() in client.ts. The request is made as the
// agent key when one is set, else the admin token — the backend decides which
// principal an admin-token call runs under.
export interface ExecuteResult {
  status: ExecutionStatus;
  /** Curated, redacted audit detail written by the manager. */
  detail: string;
  recordId: string | null;
  approvalId?: string | null;
  /** invalid_args only: "path: keyword" (never values). */
  errors?: string[];
  result?: ToolCallOutput | null;
  latencyMs?: number | null;
}

export type ApprovalStatus = "pending" | "executing" | "executed" | "failed" | "denied" | "expired";

/** routes_policy.ApprovalOut. `summary` holds the tool id, operation and REDACTED arguments. */
export interface Approval {
  id: string;
  agentId: string;
  toolId: string;
  status: ApprovalStatus;
  summary: { tool?: string; operation?: string; arguments?: JsonValue } & JsonObject;
  createdAt: string;
  expiresAt: string;
  decidedAt: string | null;
  resultPreview: string | null;
}

/** routes_policy.ApprovalDecision */
export interface ApprovalDecision {
  approvalId: string;
  status: string;
  detail: string;
  recordId: string | null;
  resultPreview?: string | null;
}
