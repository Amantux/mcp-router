/**
 * Wire types for the MCP Router REST API (SPEC §8 / §9), camelCase.
 *
 * Every response passes through `camelizeKeys` in client.ts, so these hold
 * whether the backend emits snake_case (SPEC §9 literal) or camelCase
 * (CLAUDE.md wire convention). A CONTRACT line comment names the test that pins it
 * (meta.test.ts enforces this); response fields are checked against
 * docs/reference/openapi.json there.
 */

export type Transport = "stdio" | "streamable-http" | "sse";
// "unknown" is the ORM default before the first health check (models.py).
export type ServerStatus = "healthy" | "degraded" | "offline" | "unknown";
export type Operation = "read" | "write" | "execute" | "unknown";
export type OperationCeiling = "read" | "write" | "execute";
/**
 * Every ExecutionRecord.outcome the backend writes (HS-U-013): execution/manager.py's
 * outcome constants plus the skill-access outcomes gateway/skills.py records ("read" =
 * a skill resource read, "bundle" = delivered in a skill bundle). outcomes.test.ts
 * reads both backend files and fails when this list misses one.
 */
export const EXECUTION_OUTCOMES = [
  "ok",
  "error",
  "timeout",
  "denied",
  "rate_limited",
  "invalid_args",
  "unavailable",
  "pending_approval",
  "started",
  "cancelled",
  "read",
  "bundle",
] as const;
export type ExecutionOutcome = (typeof EXECUTION_OUTCOMES)[number];
export type DedupStatus = "open" | "accepted" | "dismissed";

/** FR-04 domain vocabulary (matches feat/registry classify.py). */
export const DOMAINS = ["development", "communication", "files", "databases", "productivity"] as const;
export const OPERATIONS: Operation[] = ["read", "write", "execute", "unknown"];
export const CEILINGS: OperationCeiling[] = ["read", "write", "execute"];
/** New-agent defaults: api/routes_policy.py PrincipalIn (max_tools=8, max_skills=3). */
export const DEFAULT_MAX_TOOLS = 8;
export const DEFAULT_MAX_SKILLS = 3;

export type JsonValue = string | number | boolean | null | JsonValue[] | { [k: string]: JsonValue };
export type JsonObject = { [k: string]: JsonValue };

/** Paged list envelope. */
// CONTRACT: list endpoints return {items, total, limit, offset} verified: meta.test.ts › page.envelope
// A bare array is
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
  enabled: boolean;
  status: ServerStatus;
  version?: string | null;
  lastDiscoveredAt?: string | null;
  // CONTRACT: the server list carries a tool count verified: meta.test.ts › servers.toolCount
  toolCount?: number;
}

/** POST /servers (ServerIn, extra keys refused): http/sse → endpoint; stdio → command [cmd, ...args]. */
export type RegisterServerRequest =
  | { name: string; transport: "stdio"; command: string[] }
  | { name: string; transport: "streamable-http" | "sse"; endpoint: string };

// ------------------------------------------------------------------- tools
export interface MCPTool {
  id: string;
  serverId: string;
  // CONTRACT: denormalised server name verified: meta.test.ts › tools.serverName
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
  // CONTRACT: availability flag verified: meta.test.ts › tools.available
  available?: boolean;
  classificationReviewed?: boolean;
  version: number;
  // Usage stats: the backend nests them under `stats`; client.normaliseTool flattens them.
  // CONTRACT: stats.callCount/errorCount/avgLatencyMs verified: meta.test.ts › tools.stats
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
// CONTRACT: detail = MCPTool + `versions` (any order; the UI sorts) verified: meta.test.ts › tools.versions
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

// CONTRACT: PATCH returns the updated tool, marked reviewed verified: meta.test.ts › classification.returnsTool
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
  // CONTRACT: suggestions embed refs to both sides; slim refs make the UI fetch by id verified: meta.test.ts › dedup.embedsTools
  toolA?: MCPTool;
  toolB?: MCPTool;
}

// ----------------------------------------------------------------- routing
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

// ------------------------------------------------------------ models/health
// UI-side shape: client.mapModelsHealth builds it from GET /models/health.
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
// GET /executions?agentId=&outcome=&limit=&offset= → Page<ExecutionRecord> (query names: meta.test.ts).
export interface ExecutionRecord {
  id: string;
  agentId: string;
  toolId?: string | null;
  serverId?: string | null;
  // CONTRACT: tool/server names denormalised for display verified: meta.test.ts › executions.names
  toolName?: string | null;
  serverName?: string | null;
  outcome: ExecutionOutcome;
  detail: string;
  latencyMs?: number | null;
  createdAt: string;
  routeRequestId?: string | null; // attribution to the routing decision; null = unattributed
  initiatedBy?: string | null; // "admin" = impersonated (admin-initiated) run
}

// POST /route/{requestId}/feedback {items:[{kind?, id|name, helpful, note?}]}
// CONTRACT: response {recorded, source} verified: meta.test.ts › feedback.result
// → {recorded, source}; 404 unknown/foreign decision, 422 bad body, 429 rate-limited.
export interface FeedbackItem {
  kind?: "tool" | "skill";
  id?: string;
  name?: string;
  helpful: boolean;
  note?: string;
}

export interface FeedbackResult {
  recorded: number;
  source: "agent" | "human";
}

export interface ExecutionQuery {
  agentId?: string;
  outcome?: ExecutionOutcome;
  limit: number;
  offset: number;
}

// ----------------------------------------------------------------- policy
export interface Principal {
  id: string;
  agentId: string;
  enabled: boolean;
  maxTools: number;
  /** Distinct-server exposure cap; null = unlimited. */
  maxServers?: number | null;
  /** Routed-skill exposure cap (separate from maxTools). */
  maxSkills?: number;
  createdAt: string;
}

export interface CreatePrincipalRequest {
  agentId: string;
  maxTools: number;
  maxServers?: number | null;
  maxSkills?: number;
}

// CONTRACT: create returns the principal plus `apiKey` exactly once verified: meta.test.ts › principals.apiKey
export interface CreatedPrincipal extends Principal {
  apiKey: string;
}

export interface PrincipalPatch {
  enabled?: boolean;
  maxTools?: number;
  /** null clears the cap (no limit). */
  maxServers?: number | null;
  maxSkills?: number;
}

export interface RotatedKey {
  id: string;
  agentId: string;
  apiKey: string;
}

export interface PolicyRule {
  id: string;
  agentId: string;
  /** "skill": serverId is a skill source id and toolName a skill-name glob. Absent = tool. */
  resourceKind?: "tool" | "skill";
  serverId: string | null;
  toolName: string | null;
  maxOperation: OperationCeiling;
  requiresApproval: boolean;
  createdAt: string;
}

export type CreateRuleRequest = Omit<PolicyRule, "id" | "createdAt"> & {
  /** "skill" re-targets serverId at a skill source id; immutable after create. */
  resourceKind?: "tool" | "skill";
};

export interface RulePatch {
  serverId?: string | null;
  toolName?: string | null;
  maxOperation?: OperationCeiling;
  requiresApproval?: boolean;
}

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
// CONTRACT: diagnostics.policyFiltered[] = {server, tool, reason, toolId?} verified: meta.test.ts › simulate.policyFiltered
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
// CONTRACT: diagnostics.stages[] = {stage, before, after} verified: meta.test.ts › simulate.stages
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

// POST /route/simulate (admin): the /route fields plus agentId, applied budgets, clamps[]
// and diagnostics {candidates, stages[], policyFiltered[]}; client.simulateAgent normalises.
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
  /** Skill metadata (name + description) shown in routes; part of exposedTokens. */
  skillMetadataTokens: number;
  /** Bodies of skills actually activated on the decision; part of exposedTokens. */
  skillBodyTokensExposed: number;
  /** Bodies of surfaced-but-never-activated skills; NOT in exposedTokens. */
  skillBodyTokensNotSent: number;
  /** ESTIMATES (tokensNotSent x operator rates), never measured; null = rate not configured. */
  estimatedTimeSavedMs?: number | null;
  estimatedCostSaved?: number | null;
  currency?: string | null;
  assumptions?: EconomyAssumptions;
}

/** analytics/wire.py AssumptionsOut: the basis every estimate must be shown with. */
export interface EconomyAssumptions {
  prefillMsPer1kTokens: number | null;
  pricePer1kInputTokens: number | null;
  estimator: string;
}

/** analytics/wire.py MeasuredOut: recorded per row, kept apart from estimates. */
export interface MeasuredLatency {
  routeLatencyP50Ms: number | null;
  routeLatencyP95Ms: number | null;
  executionLatencyP50Ms: number | null;
  executionLatencyP95Ms: number | null;
}

// Overview feedback rollup; every value is null until there is feedback.
export interface FeedbackOverview {
  items: number | null;
  helpfulRate: number | null;
  coverage: number | null;
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
  /** analytics/wire.py SkillsOverviewOut. */
  skills: SkillsOverview;
  measured?: MeasuredLatency;
  // CONTRACT: overview.feedback verified: meta.test.ts › overview.feedback
  feedback?: FeedbackOverview | null;
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
  /** From the id prefix ("skill:<id>"); skills: toolName = skill, serverName = source. */
  kind: "tool" | "skill";
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
  // CONTRACT: per-row feedback counts verified: meta.test.ts › funnel.feedback
  feedbackHelpful?: number | null;
  feedbackUnhelpful?: number | null;
  helpfulRate?: number | null;
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
  skillsSurfaced: number;
  skillsActivated: number;
  /** skillsActivated / skillsSurfaced, null with no denominator. */
  skillActivationRate: number | null;
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
  // CONTRACT: rows carry kind verified: meta.test.ts › wasted.kind
  kind?: "tool" | "skill";
  // CONTRACT: unhelpful feedback count verified: meta.test.ts › wasted.unhelpful
  unhelpful?: number | null;
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
  categories?: string[];
  requiredScopes?: string[];
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
/** One row of GET /skills/{id}/versions. */
export interface SkillVersionRow {
  version: number;
  changeKind: string;
  contentHash: string;
  manifestHash?: string | null;
  recordedAt: string;
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
