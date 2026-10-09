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
  /** S2d item 2: may only LOWER the principal/global skills cap (1..1000). */
  maxSkills?: number;
  /** S2d item 2: narrows the routed kinds; omitted = both. */
  kinds?: RouteKind[];
}

export type RouteKind = "tool" | "skill";

/** S2d item 2 (aligned): /route skills[{source, skill, score, bodyTokensEst}]; simulate adds skillId. */
export interface RoutedSkill {
  skillId?: string;
  source: string;
  skill: string;
  score: number;
  bodyTokensEst: number;
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
  // Wave 2 (budgets): sent snake_case (max_tools_applied, ...) like the rest
  // of /route; camelised by the client. null maxServersApplied = unlimited.
  maxToolsApplied?: number;
  maxServersApplied?: number | null;
  /** S2d item 2: skills routed separately from tools (never mixed into tools[]). */
  skills?: RoutedSkill[];
  maxSkillsApplied?: number | null;
  cached?: boolean;
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

// Inference backend descriptors (routes_models.py decision_backend /
// embedding_backend). Hosts only — never full URLs or credentials.
export interface DecisionBackendInfo {
  kind: string | null; // "laya" | "deterministic" | "remote" | "aoai"
  model: string | null;
  endpointHost: string | null;
  deployment: string | null;
}

export interface EmbeddingBackendInfo {
  kind: string | null; // "local" | "aoai"
  name: string | null;
  endpointHost: string | null;
  deployment: string | null;
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
  decisionBackend?: DecisionBackendInfo | null;
  embeddingBackend?: EmbeddingBackendInfo | null;
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
  routeRequestId?: string | null; // attribution to the routing decision; null = unattributed
  initiatedBy?: string | null; // "admin" = impersonated (admin-initiated) run
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

// Reconciled at wave-2 integration (api/routes_execute.py):
// POST /api/v1/tools/{toolId}/execute {arguments, agentId?, routeRequestId?} →
// {status, detail, recordId, approvalId, errors[], result, latencyMs}; every
// manager outcome is a 200 with `status`. Made as the agent key when one is
// set, else the admin token — and an admin-token call MUST name `agentId`
// (400 otherwise); it then runs under that agent's policy, audited as
// impersonation.
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

// --------------------------------------------------------------- agent lens
/**
 * One budget's clamp chain (routing/budgets.py BudgetClamp, A's wave-2 branch):
 * applied = min(requested ?? principal, principal, globalCap); null = no cap.
 */
export interface BudgetClamp {
  budget: "maxTools" | "maxServers" | "maxSkills" | string;
  requested: number | null;
  principal: number | null;
  globalCap: number | null;
  applied: number | null;
  /** Which ceiling bound the result; null when the request value stood. */
  clampedBy: "principal" | "global" | null | string;
}

/** A tool removed before exposure, with the deterministic reason. Admin-only diagnostics. */
// CONTRACT: diagnostics.policyFiltered[] = {server, tool, reason} (toolId optional).
export interface FilteredTool {
  toolId?: string;
  serverName: string;
  toolName: string;
  reason: string;
  /** S2d item 2: "tool" | "skill" on every diagnostics entry (absent on older backends). */
  kind?: RouteKind;
  /** Pipeline stage that removed it ("policy", "budget", "maxServers", ...), if given. */
  stage?: string;
}

/** Candidate counts through the pipeline. */
// CONTRACT: diagnostics.stages[] = {stage, before, after}.
export interface PipelineStage {
  stage: string;
  before: number;
  after: number;
  /** Pruned entries counted per kind, when the stage lists them (S2d item 2). */
  prunedByKind?: Partial<Record<RouteKind, number>>;
}

export interface SimulateRequest {
  agentId: string;
  query: string;
  maxTools?: number;
  maxServers?: number;
  maxSkills?: number;
  kinds?: RouteKind[];
}

// CONTRACT: POST /api/v1/route/simulate (admin) → RouteResponse fields plus
// {agentId, maxToolsApplied, maxServersApplied (verified on A's branch),
// clamps[] (BudgetClamp, verified), diagnostics:{candidates, stages[],
// policyFiltered[]} (guessed)}. client.simulateAgent normalises.
export interface SimulateResponse {
  requestId?: string;
  agentId: string;
  tools: RoutedTool[];
  fallbackUsed: boolean;
  noMatch: boolean;
  latencyMs: number;
  maxToolsApplied: number | null;
  maxServersApplied: number | null;
  skills: RoutedSkill[];
  maxSkillsApplied: number | null;
  clamps: BudgetClamp[];
  candidates: number | null;
  stages: PipelineStage[];
  filtered: FilteredTool[];
}

// --------------------------------------------------------------- analytics
// Aligned to B's wave-2 analytics/wire.py (read from the in-progress
// /root/mcpr2-wt-analytics worktree). All rates are fractions 0..1 or null
// (null = no denominator yet). Every endpoint takes ?window=<n>h|<n>d.
export type AnalyticsWindow = "7d" | "30d" | "90d";

export interface AnalyticsWindowInfo {
  label: string;
  start: string;
  end: string;
}

/** "Tokens not sent": exposed vs the agent's authorized catalog (chars/4 estimate). */
export interface ContextEconomy {
  servedDecisions: number;
  unscoredDecisions: number;
  noMatchDecisions: number;
  exposedTokens: number;
  catalogTokens: number;
  tokensNotSent: number;
  savings: number | null;
  catalogTokensPerDecision?: number | null;
  estimator: string;
  catalogBasis: string;
}

export interface FunnelTotals {
  surfaced: number;
  selected: number;
  succeeded: number;
  failed: number;
  selectionRate: number | null;
  successRate: number | null;
}

export interface RoutingStats {
  decisions: number;
  noMatch: number;
  noMatchRate: number | null;
  fallback: number;
  fallbackRate: number | null;
  latencyP50Ms: number | null;
  latencyP95Ms: number | null;
}

export interface ExecutionStats {
  attempts: number;
  denied: number;
  denialRate: number | null;
  attributed: number;
  attributionCoverage: number | null;
  offFunnelSelections: number;
}

/** Position bias: how often a tool shown at `rank` was selected. */
export interface RankPoint {
  rank: number;
  shown: number;
  selected: number;
  rate: number | null;
}

export interface AnalyticsOverview {
  window: AnalyticsWindowInfo;
  contextEconomy: ContextEconomy;
  funnel: FunnelTotals;
  routing: RoutingStats;
  executions: ExecutionStats;
  positionCurve: RankPoint[];
  catalogDrift: Record<string, number>;
  // CONTRACT (guessed — S2f analytics wire not landed; names from the wave-4 brief):
  // overview.skills {surfaced, activated, activationRate, bodyTokensNotSent}. Absent on older backends.
  skills?: SkillsOverview;
}

export interface SkillsOverview {
  surfaced: number;
  activated: number;
  /** activated / surfaced, 0..1, null with no denominator. */
  activationRate: number | null;
  bodyTokensNotSent: number;
}

/** Analytics kind filter; "all" sends no ?kind=. */
export type AnalyticsKind = "all" | "tool" | "skill";

export interface ToolFunnel {
  toolId: string;
  // CONTRACT (guessed, S2f): rows gain kind "tool" | "skill".
  kind?: "tool" | "skill";
  /** null: the tool is no longer in the catalog. */
  toolName: string | null;
  serverName: string | null;
  enabled: boolean | null;
  tokens: number | null;
  surfaced: number;
  selected: number;
  succeeded: number;
  failed: number;
  selectionRate: number | null;
  successRate: number | null;
  avgRank: number | null;
  exposedTokens: number;
}

export type ToolFunnelSort =
  | "surfaced"
  | "selected"
  | "succeeded"
  | "failed"
  | "selectionRate"
  | "successRate"
  | "avgRank"
  | "exposedTokens"
  | "toolName";

export interface ToolFunnelPage extends Page<ToolFunnel> {
  window: AnalyticsWindowInfo;
}

export interface CoSurfaced {
  toolId: string;
  toolName: string | null;
  serverName: string | null;
  coSurfaced: number;
  thisSelected: number;
  otherSelected: number;
}

export interface ToolAnalytics {
  window: AnalyticsWindowInfo;
  tool: ToolFunnel;
  positionCurve: RankPoint[];
  coSurfaced: CoSurfaced[];
}

export interface AgentProfile {
  agentId: string;
  decisions: number;
  noMatch: number;
  noMatchRate: number | null;
  fallback: number;
  fallbackRate: number | null;
  latencyP50Ms: number | null;
  latencyP95Ms: number | null;
  attempts: number;
  denied: number;
  denialRate: number | null;
  attributed: number;
  attributionCoverage: number | null;
  surfaced: number;
  selected: number;
  selectionRate: number | null;
  avgSurfacedPerDecision: number | null;
  budgetTools?: number | null;
  budgetUtilization?: number | null;
  contextEconomy: ContextEconomy;
}

export interface WastedTool {
  toolId: string;
  toolName: string | null;
  serverName: string | null;
  surfaced: number;
  selected: number;
  selectionRate: number | null;
  exposedTokens: number;
  // CONTRACT (guessed, S2f): rows gain kind.
  kind?: "tool" | "skill";
}

export interface StaleTool {
  toolId: string;
  toolName: string;
  serverName: string;
  createdAt: string;
  lastSurfacedAt: string | null;
}

export interface NeverRoutedServer {
  serverId: string;
  serverName: string;
  toolCount: number;
  createdAt: string;
}

/** Suggestions only: nothing is disabled automatically. */
export interface AnalyticsSuggestions {
  window: AnalyticsWindowInfo;
  minSurfaced: number;
  maxSelectionRate: number;
  staleDays: number;
  wastedExposure: WastedTool[];
  staleTools: StaleTool[];
  neverRoutedServers: NeverRoutedServer[];
}

// ------------------------------------------------------------ wave 4: skills
// CONTRACT: shapes guessed from docs/skills-plan.md; backend notes for wave 4 were not yet
// published when this was written. Every field the UI does not strictly need is optional.
export type SkillSourceKind = "directory" | "git";
export interface SkillSource {
  id: string;
  name: string;
  kind: SkillSourceKind;
  location: string;
  gitRef?: string | null;
  enabled: boolean;
  status?: string | null;
  lastSyncedAt?: string | null;
  lastCommit?: string | null;
  skillCount?: number | null;
}
export interface CreateSkillSourceRequest {
  name: string;
  kind: SkillSourceKind;
  location: string;
  gitRef?: string;
}
export interface SyncSkipped {
  path: string;
  reason: string;
}
export interface SyncReport {
  added: number;
  changed: number;
  removed: number;
  skipped: SyncSkipped[];
}
/** Ingest flags the backend attaches; unknown strings are rendered verbatim. */
export type SkillIngestFlag = "secret_like" | "body_truncated" | "oversize" | (string & {});
export interface Skill {
  id: string;
  sourceId: string;
  sourceName?: string | null;
  name: string;
  description: string;
  operation: Operation;
  domain?: string | null;
  tags?: string[];
  hasScripts?: boolean;
  bodyTokensEst?: number | null;
  version?: string | null;
  enabled: boolean;
  available?: boolean;
  classificationReviewed?: boolean;
  ingestFlags?: SkillIngestFlag[];
  activationCount?: number | null;
}
export interface SkillResource {
  path: string;
  size: number;
  sha256?: string;
  kind: string;
  oversize?: boolean;
}
export interface SkillVersion {
  version?: string | null;
  contentHash?: string | null;
  createdAt?: string | null;
}
export interface SkillDetail extends Skill {
  license?: string | null;
  compatibility?: string | null;
  metadata?: Record<string, unknown> | null;
  allowedTools?: string[] | null;
  resourceManifest?: SkillResource[];
  versions?: SkillVersion[];
}
export interface SkillQuery {
  q?: string;
  domain?: string;
  operation?: Operation | "";
  sourceId?: string;
  enabled?: boolean;
  available?: boolean;
  reviewed?: boolean;
  hasScripts?: boolean;
  limit: number;
  offset: number;
}

/** POST /skills/{id}/activate result (S3 exposure notes, planned shape). `body` is inert text. */
export interface SkillActivation {
  body: string;
  resources: { path: string; size: number; kind: string }[];
  recordId: string | null;
}
