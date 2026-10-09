/**
 * The ONE module that talks to the backend (thin-client rule). Pages import
 * functions from here; no component calls fetch directly.
 *
 * Response normalisation:
 *  - keys are deep-converted snake_case → camelCase, EXCEPT inside opaque
 *    payloads (`inputSchema`, `snapshot`, `scores`) whose keys are user/tool
 *    data and must round-trip verbatim;
 *  - list endpoints tolerate a bare array or a {items,total,...} envelope.
 *
 * Errors: non-2xx and network failures throw ApiError carrying the status.
 * The response body is never surfaced to the UI, with one exception: a 409's
 * `detail` string. Every backend 409 is a curated, typed-exception message that
 * names the next step ("server is referenced by policy rules; remove those rules
 * before deleting it"), so it replaces the generic conflict advice (HS-U-014).
 * It is taken only from a JSON `{detail: "<string>"}` body, and only when it is
 * one short line (see curatedConflict).
 */
import type {
  FeedbackItem,
  FeedbackResult,
  CreateSkillSourceRequest, Skill, SkillActivation, SkillDetail, SkillQuery, SkillSource, SyncReport,
  Approval,
  ApprovalDecision,
  ApprovalStatus,
  ClassificationUpdate,
  ExecuteResult,
  AgentProfile,
  AnalyticsOverview,
  AnalyticsSuggestions,
  AnalyticsWindow,
  ToolAnalytics,
  ToolFunnel,
  ToolFunnelPage,
  ToolFunnelSort,
  BudgetClamp,
  FilteredTool,
  PipelineStage,
  SimulateRequest,
  SimulateResponse,
  CreatePrincipalRequest,
  CreateRuleRequest,
  CreatedPrincipal,
  PrincipalPatch,
  RotatedKey,
  RulePatch,
  SkillVersionRow,
  DedupStatus,
  DuplicateSuggestion,
  ExecutionQuery,
  ExecutionRecord,
  Healthz,
  MCPServer,
  MCPTool,
  ModelsHealth,
  Page,
  PolicyRule,
  Principal,
  RegisterServerRequest,
  RoutedTool,
  RoutedSkill,
  AnalyticsKind,
  RouteKind,
  ToolDetail,
  ToolQuery,
  JsonObject,
} from "./types";
import { bearerFor, effectiveIdentity, noteResponse, setAgentId, type Identity } from "./auth";

export const API_BASE = "/api/v1";
/** Raw Prometheus text; the app mounts it at /metrics (SPEC §9 says /api/v1/metrics). */
export const METRICS_URL = "/metrics";

// ------------------------------------------------------------ case mapping
// Opaque = user/tool data: tool schemas, version snapshots, score maps, tool
// call arguments and outputs, approval summaries (redacted arguments).
const OPAQUE_KEYS = new Set(["inputSchema", "snapshot", "scores", "arguments", "content", "structuredContent", "summary", "catalogDrift"]);

export function snakeToCamel(key: string): string {
  return key.replace(/_+([a-z0-9])/g, (_m, c: string) => c.toUpperCase());
}

/** Deep snake→camel key conversion that leaves opaque payloads untouched. */
export function camelizeKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(camelizeKeys);
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      const ck = snakeToCamel(k);
      out[ck] = OPAQUE_KEYS.has(ck) ? v : camelizeKeys(v);
    }
    return out;
  }
  return value;
}

// ------------------------------------------------------------------ errors
export class ApiError extends Error {
  /** HTTP status, or 0 for a network failure / unreachable backend. */
  readonly status: number;
  readonly path: string;
  /** The credential the failed request was made as. */
  readonly identity: Identity;
  /** A 409's curated backend reason (curatedConflict), else undefined. Never set for other statuses. */
  readonly conflict?: string;
  constructor(status: number, path: string, identity: Identity = "admin", conflict?: string) {
    super(status === 0 ? `Network error calling ${path}` : `HTTP ${status} from ${path}`);
    this.name = "ApiError";
    this.status = status;
    this.path = path;
    this.identity = identity;
    if (status === 409 && conflict) this.conflict = conflict;
  }
}

/** Longest 409 detail shown verbatim; anything longer is not a curated one-liner. */
const MAX_CONFLICT = 300;

/**
 * The backend's curated 409 reason, from a JSON `{detail: "<string>"}` body: one line,
 * no control characters, at most MAX_CONFLICT chars. Anything else (non-JSON, a
 * structured detail, a long or multi-line string) yields undefined and the generic
 * advice is shown instead.
 */
export function curatedConflict(text: string): string | undefined {
  let detail: unknown;
  try {
    detail = (JSON.parse(text) as { detail?: unknown } | null)?.detail;
  } catch {
    return undefined;
  }
  if (typeof detail !== "string") return undefined;
  const d = detail.trim();
  // eslint-disable-next-line no-control-regex -- rejecting control characters is the point
  if (!d || d.length > MAX_CONFLICT || /[\u0000-\u001f\u007f]/.test(d)) return undefined;
  return d;
}

/** "server is referenced…; remove those rules" -> "Server is referenced…; remove those rules." */
function asSentence(s: string): string {
  const t = s.charAt(0).toUpperCase() + s.slice(1);
  return /[.!?]$/.test(t) ? t : `${t}.`;
}

/** The ApiError for a non-2xx response; reads the body only for a 409's curated reason. */
async function failure(res: Response, path: string, presented: Identity): Promise<ApiError> {
  if (res.status !== 409) return new ApiError(res.status, path, presented);
  let text = "";
  try {
    text = await res.text();
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e; // an abort stays an abort
    /* unreadable body: generic advice */
  }
  return new ApiError(409, path, presented, curatedConflict(text));
}

/**
 * True for an admin-credential refusal (401/403). The global "not connected"
 * bar explains these, so per-action error toasts are suppressed for them.
 */
export function isAdminAuthError(err: unknown): boolean {
  return err instanceof ApiError && err.identity === "admin" && (err.status === 401 || err.status === 403);
}

/** Curated, user-facing explanation of a failure. Never includes a response body. */
export function describeError(err: unknown): { status: string; advice: string } {
  if (!(err instanceof ApiError)) {
    return { status: "", advice: "Something unexpected went wrong in the dashboard. Reload the page and try again. If it keeps happening, the browser console has the details." };
  }
  const s = err.status;
  if (s === 0)
    return { status: "network error", advice: "Couldn't reach the MCP Router backend. Check that it is running and reachable from this browser, then retry." };
  const status = `HTTP ${s}`;
  if ((s === 401 || s === 403) && err.identity === "agent")
    return {
      status,
      advice:
        s === 401
          ? "The backend refused the agent key. Open Connect (bottom of the left menu) and paste a current agent key, or forget it to act as admin."
          : "The agent this key belongs to isn't allowed to do this.",
    };
  if (s === 401 || s === 403)
    return { status, advice: "The backend refused these credentials. Open Connect (bottom of the left menu) and paste the admin token, then retry." };
  if (s === 404)
    return { status, advice: "The item no longer exists, or this backend doesn't provide the endpoint yet. Refresh and retry." };
  if (s === 409)
    return {
      status,
      advice: err.conflict
        ? asSentence(err.conflict)
        : "It conflicts with existing data (for example a duplicate name). Change the input and retry.",
    };
  if (s === 400 || s === 422) return { status, advice: "The backend rejected the input. Check the fields and retry." };
  if (s === 429) return { status, advice: "Rate limit reached. Wait a moment, then retry." };
  if (s >= 500) return { status, advice: "The backend hit an internal error. Check the server logs, then retry." };
  return { status, advice: "The request did not succeed. Retry, and check the server logs if it persists." };
}

// ------------------------------------------------------------------- core
type QueryValue = string | number | boolean | undefined | null;

interface RequestOptions {
  query?: Record<string, QueryValue>;
  body?: unknown;
  signal?: AbortSignal;
  /** Which credential to present. Default admin; "agent" uses the agent key when set. */
  as?: Identity;
  /** Unauthenticated endpoint (e.g. /healthz): no bearer, and its 2xx proves nothing about credentials. */
  public?: boolean;
}

/** Every request's headers. The bearer comes from the session credential store. */
function buildHeaders(hasBody: boolean, identity: Identity, withAuth = true): Record<string, string> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (hasBody) headers["Content-Type"] = "application/json";
  const bearer = withAuth ? bearerFor(identity) : null;
  if (bearer) headers.Authorization = `Bearer ${bearer}`;
  return headers;
}

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === "") continue;
    params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const url = buildUrl(path, opts.query);
  const identity = opts.as ?? "admin";
  // The credential actually presented: an agent request without an agent key goes as admin.
  const presented: Identity = effectiveIdentity(identity) === "agent" ? "agent" : "admin";
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: opts.public ? buildHeaders(opts.body !== undefined, "admin", false) : buildHeaders(opts.body !== undefined, identity),
      credentials: "omit", // bearer only; never send or accept cookies
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, path, presented);
  }
  if (!opts.public) noteResponse(presented, res.status);
  if (!res.ok) throw await failure(res, path, presented);
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new ApiError(res.status, path, presented);
  }
  return camelizeKeys(parsed) as T;
}

export function isAbort(e: unknown): boolean {
  return e instanceof DOMException && e.name === "AbortError";
}

function toPage<T>(raw: unknown, limit: number, offset: number): Page<T> {
  if (Array.isArray(raw)) return { items: raw as T[], total: raw.length, limit, offset };
  const p = (raw ?? {}) as Partial<Page<T>>;
  const items = p.items ?? [];
  return { items, total: p.total ?? items.length, limit: p.limit ?? limit, offset: p.offset ?? offset };
}

function toList<T>(raw: unknown): T[] {
  if (Array.isArray(raw)) return raw as T[];
  return ((raw as { items?: T[] } | undefined)?.items ?? []) as T[];
}

// ----------------------------------------------------------------- servers
export async function listServers(signal?: AbortSignal): Promise<MCPServer[]> {
  return toList<MCPServer>(await request("GET", `${API_BASE}/servers`, { signal }));
}

export function registerServer(body: RegisterServerRequest): Promise<MCPServer> {
  return request("POST", `${API_BASE}/servers`, { body });
}

// Reconciled at integration: the backend returns {server, added[], ...}; the page wants the server.
export async function refreshServer(id: string): Promise<MCPServer> {
  const raw = await request<MCPServer | { server: MCPServer }>("POST", `${API_BASE}/servers/${encodeURIComponent(id)}/refresh`);
  return "server" in raw ? raw.server : raw;
}

// CONTRACT: PATCH /servers/{id} {enabled} verified: ServersPage.test.tsx › disabling asks first (naming the server and the consequence); cancelling sends nothing
export function setServerEnabled(id: string, enabled: boolean): Promise<MCPServer> {
  return request("PATCH", `${API_BASE}/servers/${encodeURIComponent(id)}`, { body: { enabled } });
}

/** DELETE /servers/{id} (204). 409 while policy rules reference it or it has execution history. */
export async function deleteServer(id: string): Promise<void> {
  await request("DELETE", `${API_BASE}/servers/${encodeURIComponent(id)}`);
}

// ------------------------------------------------------------------- tools
/** Reconciled at integration: the backend nests usage under `stats` and has no version `id`. */
interface BackendToolStats {
  callCount?: number;
  errorCount?: number;
  avgLatencyMs?: number | null;
}

export function normaliseTool<T extends MCPTool>(raw: T & { stats?: BackendToolStats }): T {
  const { stats, ...rest } = raw;
  return {
    ...(rest as unknown as T),
    callCount: raw.callCount ?? stats?.callCount,
    errorCount: raw.errorCount ?? stats?.errorCount,
    avgLatencyMs: raw.avgLatencyMs ?? stats?.avgLatencyMs,
  };
}

// CONTRACT: query names q, domain, operation, serverId, enabled, available, limit, offset verified: ToolsPage.test.tsx › sends every filter under the backend's query names, debounces search, and clears
export async function listTools(q: ToolQuery, signal?: AbortSignal): Promise<Page<MCPTool>> {
  const raw = await request<unknown>("GET", `${API_BASE}/tools`, {
    signal,
    query: {
      q: q.q,
      domain: q.domain,
      operation: q.operation,
      serverId: q.serverId,
      enabled: q.enabled,
      available: q.available,
      limit: q.limit,
      offset: q.offset,
    },
  });
  const page = toPage<MCPTool>(raw, q.limit, q.offset);
  return { ...page, items: page.items.map((t) => normaliseTool(t)) };
}

export async function getTool(id: string, signal?: AbortSignal): Promise<ToolDetail> {
  const t = normaliseTool(await request<ToolDetail>("GET", `${API_BASE}/tools/${encodeURIComponent(id)}`, { signal }));
  return {
    ...t,
    versions: (t.versions ?? []).map((v) => ({ ...v, id: v.id ?? `${id}@${v.version}` })),
  };
}

export function updateClassification(id: string, body: ClassificationUpdate): Promise<MCPTool> {
  return request("PATCH", `${API_BASE}/tools/${encodeURIComponent(id)}/classification`, { body });
}

/** POST /tools/{id}/enable | /disable: one tool, without disabling its server. */
export function setToolEnabled(id: string, enabled: boolean): Promise<MCPTool> {
  return request("POST", `${API_BASE}/tools/${encodeURIComponent(id)}/${enabled ? "enable" : "disable"}`);
}

/** Skills mirror the tools PATCH (admin; sets classificationReviewed, human override wins). */
export function updateSkillClassification(id: string, body: ClassificationUpdate): Promise<Skill> {
  return request("PATCH", `${API_BASE}/skills/${encodeURIComponent(id)}/classification`, { body });
}

// ------------------------------------------------------------------- dedup
/** Backend embeds a small tool ref ({id, name, serverName, enabled}), not a full MCPTool. */
interface BackendSuggestion extends Omit<DuplicateSuggestion, "toolA" | "toolB" | "toolAId" | "toolBId"> {
  toolAId?: string;
  toolBId?: string;
  toolA?: (Partial<MCPTool> & { id: string }) | null;
  toolB?: (Partial<MCPTool> & { id: string }) | null;
}

/** A full embedded tool is kept; the backend's slim ref is dropped so the page fetches by id. */
function fullTool(t: (Partial<MCPTool> & { id: string }) | null | undefined): MCPTool | undefined {
  return t && t.operation !== undefined && t.inputSchema !== undefined ? normaliseTool(t as MCPTool) : undefined;
}

/** GET /dedup/suggestions is paged ({items,total,limit,offset}; default limit 50). */
export async function listDedupSuggestions(
  q: { status?: DedupStatus; limit: number; offset: number },
  signal?: AbortSignal,
): Promise<Page<DuplicateSuggestion>> {
  const page = toPage<BackendSuggestion>(
    await request("GET", `${API_BASE}/dedup/suggestions`, { signal, query: { status: q.status ?? "open", limit: q.limit, offset: q.offset } }),
    q.limit,
    q.offset,
  );
  // Reconciled at integration: take ids from the embedded refs; when they are
  // slim refs (no description/schema) the page fetches the full tools by id.
  const items = page.items.map(({ toolA, toolB, ...s }) => ({
    ...s,
    toolAId: s.toolAId ?? toolA?.id ?? "",
    toolBId: s.toolBId ?? toolB?.id ?? "",
    toolA: fullTool(toolA),
    toolB: fullTool(toolB),
  }));
  return { ...page, items };
}

/** What a scan did. `truncated`: the scan hit MCPR_DEDUP_MAX_PAIRS (null on backends that don't say). */
export interface DedupRun {
  pairsConsidered: number;
  created: number;
  refreshed: number;
  skippedDecided: number;
  truncated?: boolean | null;
}

/** POST /dedup/suggestions runs a scan and returns its counts (DedupRunOut). */
export function runDedupScan(): Promise<DedupRun> {
  return request("POST", `${API_BASE}/dedup/suggestions`);
}

// Accept body {preferredToolId} (D12): the backend persists it and echoes the stored id.
export function acceptDedup(id: string, preferredToolId: string): Promise<DuplicateSuggestion> {
  return request("POST", `${API_BASE}/dedup/suggestions/${encodeURIComponent(id)}/accept`, { body: { preferredToolId } });
}

// CONTRACT: dismiss body {justification} (the backend 422s an empty one) verified: DuplicatesPage.test.tsx › shows pairs side by side, states nothing is auto-disabled, and sends the justification on dismiss
export function dismissDedup(id: string, justification: string): Promise<DuplicateSuggestion> {
  return request("POST", `${API_BASE}/dedup/suggestions/${encodeURIComponent(id)}/dismiss`, { body: { justification } });
}

// ----------------------------------------------------------------- routing
function normaliseRoutedTool(raw: Record<string, unknown>): RoutedTool {
  // SPEC §9 literal uses {server, tool}; interfaces.RoutedTool uses server_name/tool_name.
  return {
    toolId: (raw.toolId as string | undefined) ?? undefined,
    serverName: (raw.serverName ?? raw.server ?? "") as string,
    toolName: (raw.toolName ?? raw.tool ?? "") as string,
    score: Number(raw.score ?? 0),
  };
}

/** S2d item 2 (aligned): {source, skill, score, bodyTokensEst} (+ skillId on simulate). */
function normaliseRoutedSkill(raw: Record<string, unknown>): RoutedSkill {
  return {
    skillId: (raw.skillId as string | undefined) ?? undefined,
    source: String(raw.source ?? raw.sourceName ?? ""),
    skill: String(raw.skill ?? raw.name ?? ""),
    score: Number(raw.score ?? 0),
    bodyTokensEst: Number(raw.bodyTokensEst ?? 0),
  };
}

// ------------------------------------------------------------ models/health
/** Backend shape (api/routes_models.py ModelsHealth), camelised. */
interface BackendHealth {
  backend: string | null;
  requested: string;
  modelId?: string | null;
  revision?: string | null;
}

interface BackendModelsHealth {
  loaded: boolean;
  mode: string;
  device: string;
  embedding: BackendHealth;
  decision: BackendHealth;
  memory?: Record<string, number | null>;
  decisionBackend?: Record<string, unknown> | null;
  embeddingBackend?: Record<string, unknown> | null;
}

// Copy only the named descriptor fields: anything else the server adds
// (never expected, but e.g. a key) is dropped before it can reach the UI.
function str(v: unknown): string | null {
  return typeof v === "string" && v !== "" ? v : null;
}

function pickBackend<K extends string>(
  raw: Record<string, unknown> | null | undefined,
  nameKey: K,
): ({ kind: string | null; endpointHost: string | null; deployment: string | null } & Record<K, string | null>) | null {
  if (!raw) return null;
  return {
    kind: str(raw.kind),
    endpointHost: str(raw.endpointHost),
    deployment: str(raw.deployment),
    [nameKey]: str(raw[nameKey]),
  } as { kind: string | null; endpointHost: string | null; deployment: string | null } & Record<K, string | null>;
}

const MB = 1024 * 1024;

function toMb(bytes: number | null | undefined): number | undefined {
  return bytes == null ? undefined : Math.round(bytes / MB);
}

/** Reconciled at integration: map the backend's health payload onto the UI's ModelsHealth. */
export function mapModelsHealth(raw: BackendModelsHealth): ModelsHealth {
  const mem = raw.memory ?? {};
  const model = (kind: string, b: BackendHealth) => ({
    name: b.modelId ?? b.backend ?? b.requested,
    kind,
    backend: b.backend ?? undefined,
    device: raw.device,
    loaded: raw.loaded && b.backend != null,
    version: b.revision ?? undefined,
  });
  return {
    device: raw.device,
    mode: raw.mode,
    gpu:
      mem.cudaTotalBytes != null
        ? { memoryUsedMb: toMb(mem.cudaReservedBytes), memoryTotalMb: toMb(mem.cudaTotalBytes) }
        : null,
    memory: { rssMb: toMb(mem.rssBytes) },
    models: [model("embedding", raw.embedding), model("decision", raw.decision)],
    decisionBackend: pickBackend(raw.decisionBackend, "model"),
    embeddingBackend: pickBackend(raw.embeddingBackend, "name"),
  };
}

export async function getModelsHealth(signal?: AbortSignal): Promise<ModelsHealth> {
  return mapModelsHealth(await request<BackendModelsHealth>("GET", `${API_BASE}/models/health`, { signal }));
}

export function getHealthz(signal?: AbortSignal): Promise<Healthz> {
  return request("GET", "/healthz", { signal, public: true });
}

// -------------------------------------------------------------- executions
export async function listExecutions(q: ExecutionQuery, signal?: AbortSignal): Promise<Page<ExecutionRecord>> {
  const raw = await request<unknown>("GET", `${API_BASE}/executions`, {
    signal,
    query: { agentId: q.agentId, outcome: q.outcome, limit: q.limit, offset: q.offset },
  });
  return toPage<ExecutionRecord>(raw, q.limit, q.offset);
}

// ------------------------------------------------------------------ policy
export async function listPrincipals(signal?: AbortSignal): Promise<Principal[]> {
  return toList<Principal>(await request("GET", `${API_BASE}/principals`, { signal }));
}

export function createPrincipal(body: CreatePrincipalRequest): Promise<CreatedPrincipal> {
  return request("POST", `${API_BASE}/principals`, { body });
}

// Reconciled at integration: the backend (routes_policy) serves /policy-rules.
export async function listRules(signal?: AbortSignal): Promise<PolicyRule[]> {
  return toList<PolicyRule>(await request("GET", `${API_BASE}/policy-rules`, { signal }));
}

export function createRule(body: CreateRuleRequest): Promise<PolicyRule> {
  return request("POST", `${API_BASE}/policy-rules`, { body });
}

/** PATCH /principals/{id}. Omitted fields are unchanged; `maxServers: null` clears the cap. */
export function updatePrincipal(id: string, patch: PrincipalPatch): Promise<Principal> {
  return request("PATCH", `${API_BASE}/principals/${encodeURIComponent(id)}`, { body: patch });
}

/** POST /principals/{id}/rotate-key: the old key stops working; the new one is returned once. */
export function rotatePrincipalKey(id: string): Promise<RotatedKey> {
  return request("POST", `${API_BASE}/principals/${encodeURIComponent(id)}/rotate-key`);
}

/** DELETE /principals/{id} (204). The backend deletes the agent's rules with it. */
export async function deletePrincipal(id: string): Promise<void> {
  await request("DELETE", `${API_BASE}/principals/${encodeURIComponent(id)}`);
}

/** PATCH /policy-rules/{id}. resourceKind is immutable after create. */
export function updateRule(id: string, patch: RulePatch): Promise<PolicyRule> {
  return request("PATCH", `${API_BASE}/policy-rules/${encodeURIComponent(id)}`, { body: patch });
}

export async function deleteRule(id: string): Promise<void> {
  await request("DELETE", `${API_BASE}/policy-rules/${encodeURIComponent(id)}`);
}

// -------------------------------------------------------------- identity
/** Cheap admin-only call used by the Connect panel to verify the admin token. */
export async function probeAdmin(signal?: AbortSignal): Promise<void> {
  await request("GET", `${API_BASE}/principals`, { signal });
}

/** GET /me as the agent key: which agent the key authenticates. Records the (non-secret) id. */
export async function getMe(signal?: AbortSignal): Promise<Principal> {
  const me = await request<Principal>("GET", `${API_BASE}/me`, { signal, as: "agent" });
  setAgentId(me.agentId);
  return me;
}

// --------------------------------------------------------------- execution
/**
 * Run a tool through the execution manager (the only path to an upstream
 * tool). Made as the agent key when set, else the admin token. Returns the
 * manager's outcome for every status — a policy denial is a 200 with
 * status "denied", not an exception. `roundTripMs` is measured here.
 */
// Backend: api/routes_execute.py. With the ADMIN token the backend requires
// `agentId` (400 otherwise) and runs the call as that agent, under its policy;
// with an agent key, `agentId` may only name that same agent (403 otherwise).
export async function executeTool(
  toolId: string,
  args: JsonObject,
  signal?: AbortSignal,
  opts: { agentId?: string; routeRequestId?: string } = {},
): Promise<ExecuteResult & { roundTripMs: number }> {
  const t0 = performance.now();
  const body: JsonObject = { arguments: args };
  if (opts.agentId) body.agentId = opts.agentId;
  if (opts.routeRequestId) body.routeRequestId = opts.routeRequestId;
  const raw = await request<ExecuteResult>("POST", `${API_BASE}/tools/${encodeURIComponent(toolId)}/execute`, {
    body,
    signal,
    as: "agent",
  });
  return { ...raw, errors: raw.errors ?? [], roundTripMs: performance.now() - t0 };
}

export async function listApprovals(status?: ApprovalStatus, signal?: AbortSignal): Promise<Approval[]> {
  return toList<Approval>(await request("GET", `${API_BASE}/approvals`, { signal, query: { status } }));
}

/**
 * One approval's current state. With an agent key: GET /me/approvals/{id}
 * (agents see only their own). Without: there is no admin get-by-id endpoint, so
 * poll the (small) pending list, and only when the id has left it look once in the
 * unfiltered list for its final state. Undefined if it no longer exists.
 */
export async function getApproval(id: string, asAgent: boolean, signal?: AbortSignal): Promise<Approval | undefined> {
  if (asAgent) {
    try {
      return await request<Approval>("GET", `${API_BASE}/me/approvals/${encodeURIComponent(id)}`, { signal, as: "agent" });
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return undefined; // gone (or not ours): same as the admin path
      throw e;
    }
  }
  const pending = (await listApprovals("pending", signal)).find((a) => a.id === id);
  if (pending) return pending;
  return (await listApprovals(undefined, signal)).find((a) => a.id === id);
}

export function approveApproval(id: string): Promise<ApprovalDecision> {
  return request("POST", `${API_BASE}/approvals/${encodeURIComponent(id)}/approve`);
}

export function denyApproval(id: string): Promise<ApprovalDecision> {
  return request("POST", `${API_BASE}/approvals/${encodeURIComponent(id)}/deny`);
}

// -------------------------------------------------------------- agent lens
type Loose = Record<string, unknown>;
const arr = (v: unknown): Loose[] => (Array.isArray(v) ? (v as Loose[]) : []);
const numOrNull = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

function normaliseFiltered(raw: Loose): FilteredTool {
  return {
    toolId: (raw.toolId as string | undefined) ?? undefined,
    serverName: String(raw.serverName ?? raw.server ?? ""),
    toolName: String(raw.toolName ?? raw.tool ?? raw.name ?? ""),
    reason: String(raw.reason ?? raw.detail ?? "filtered"),
    stage: (raw.stage as string | undefined) ?? undefined,
    kind: raw.kind === "skill" || raw.kind === "tool" ? raw.kind : undefined,
  };
}

function countKinds(entries: Loose[]): Partial<Record<RouteKind, number>> | undefined {
  const out: Partial<Record<RouteKind, number>> = {};
  for (const e of entries) if (e.kind === "tool" || e.kind === "skill") out[e.kind] = (out[e.kind] ?? 0) + 1;
  return Object.keys(out).length ? out : undefined;
}

/** Tolerant mapping of the simulate payload (diagnostics may be top-level or nested). */
export function normaliseSimulation(raw: Loose, agentId: string): SimulateResponse {
  const diag = (raw.diagnostics ?? {}) as Loose;
  const pick = (k: string) => (raw[k] !== undefined ? raw[k] : diag[k]);
  const tools = arr(raw.tools).map(normaliseRoutedTool);
  const filtered = arr(pick("policyFiltered") ?? pick("filtered")).map(normaliseFiltered);
  const stages: PipelineStage[] = arr(pick("stages")).map((st) => ({
    stage: String(st.stage ?? st.name ?? "stage"),
    before: Number(st.before ?? 0),
    after: Number(st.after ?? 0),
    prunedByKind: countKinds(arr(st.pruned)),
  }));
  // Backend (routes_route.DiagnosticsOut): diagnostics.budgetClamps[] and
  // diagnostics.candidatesConsidered[] (a list; the lens shows its length).
  const clamps = arr(pick("budgetClamps") ?? pick("clamps") ?? pick("budgets")) as unknown as BudgetClamp[];
  const considered = pick("candidatesConsidered");
  return {
    requestId: (raw.requestId as string | undefined) ?? undefined,
    agentId: String(raw.agentId ?? agentId),
    tools,
    fallbackUsed: raw.fallbackUsed === true,
    noMatch: raw.noMatch === true || (tools.length === 0 && arr(raw.skills).length === 0),
    latencyMs: Number(raw.latencyMs ?? 0),
    maxToolsApplied: numOrNull(raw.maxToolsApplied),
    maxServersApplied: numOrNull(raw.maxServersApplied),
    skills: arr(raw.skills).map(normaliseRoutedSkill),
    maxSkillsApplied: numOrNull(raw.maxSkillsApplied),
    clamps: clamps.map((c) => ({
      budget: String(c.budget),
      requested: numOrNull(c.requested),
      principal: numOrNull(c.principal),
      globalCap: numOrNull(c.globalCap),
      applied: numOrNull(c.applied),
      clampedBy: (c.clampedBy as string | null | undefined) ?? null,
    })),
    candidates: Array.isArray(considered) ? considered.length : numOrNull(pick("candidates") ?? pick("candidateCount")),
    stages,
    filtered,
  };
}

/**
 * Admin-only: route `query` under a named agent's scope and budgets without
 * publishing exposure (the agent's MCP tools/list is untouched).
 */
// CONTRACT: POST /route/simulate {agentId, query, maxTools, maxServers?, maxSkills?} verified: LensPage.test.tsx › simulates for a picked principal, then re-queries (debounced) when a budget slider moves
export async function simulateAgent(body: SimulateRequest, signal?: AbortSignal): Promise<SimulateResponse> {
  const raw = await request<Loose>("POST", `${API_BASE}/route/simulate`, { body, signal });
  return normaliseSimulation(raw ?? {}, body.agentId);
}

// --------------------------------------------------------------- analytics
// Aligned to B's wave-2 routes_analytics.py: admin-only, ?window=7d|30d|90d.
const ANALYTICS = `${API_BASE}/analytics`;

export function getAnalyticsOverview(window: AnalyticsWindow, signal?: AbortSignal): Promise<AnalyticsOverview> {
  return request("GET", `${ANALYTICS}/overview`, { signal, query: { window } });
}

export async function listToolFunnels(
  q: { window: AnalyticsWindow; sort?: ToolFunnelSort; order?: "asc" | "desc"; limit: number; offset: number; kind?: AnalyticsKind },
  signal?: AbortSignal,
): Promise<ToolFunnelPage> {
  const raw = await request<ToolFunnelPage>("GET", `${ANALYTICS}/tools`, {
    signal,
    // GET /analytics/tools?kind=tool|skill; omitted = all kinds.
    query: { window: q.window, sort: q.sort, order: q.order, limit: q.limit, offset: q.offset, kind: q.kind && q.kind !== "all" ? q.kind : undefined },
  });
  const page: ToolFunnelPage = { ...raw, ...toPage<ToolFunnel>(raw, q.limit, q.offset) };
  // Client-side fallback for a backend that ignores ?kind=: rows without a kind count as tools.
  if (q.kind && q.kind !== "all") page.items = page.items.filter((r) => (r.kind ?? "tool") === q.kind);
  return page;
}

export function getToolAnalytics(toolId: string, window: AnalyticsWindow, signal?: AbortSignal): Promise<ToolAnalytics> {
  return request("GET", `${ANALYTICS}/tools/${encodeURIComponent(toolId)}`, { signal, query: { window } });
}

export async function listAgentProfiles(window: AnalyticsWindow, signal?: AbortSignal): Promise<AgentProfile[]> {
  return toList<AgentProfile>(await request("GET", `${ANALYTICS}/agents`, { signal, query: { window } }));
}

/** Wasted exposure + staleness; thresholds left at the backend defaults. */
export function getAnalyticsSuggestions(window: AnalyticsWindow, signal?: AbortSignal): Promise<AnalyticsSuggestions> {
  return request("GET", `${ANALYTICS}/suggestions`, { signal, query: { window } });
}

// ------------------------------------------------------------ wave 4: skills
// REST surface: api/routes_skill_sources.py + api/routes_skills.py.

export async function listSkillSources(signal?: AbortSignal): Promise<SkillSource[]> {
  return toList<SkillSource>(await request("GET", `${API_BASE}/skill-sources`, { signal }));
}
export function createSkillSource(body: CreateSkillSourceRequest): Promise<SkillSource> {
  return request("POST", `${API_BASE}/skill-sources`, { body });
}
// POST /skill-sources/{id}/sync -> {added, changed, removed, skipped[{path, reason}]}.
export async function syncSkillSource(id: string): Promise<SyncReport> {
  const r = (await request<Partial<SyncReport>>("POST", `${API_BASE}/skill-sources/${encodeURIComponent(id)}/sync`)) ?? {};
  return { added: r.added ?? 0, changed: r.changed ?? 0, removed: r.removed ?? 0, skipped: r.skipped ?? [] };
}
// PATCH /skill-sources/{id} {enabled} mirrors PATCH /servers/{id}.
/** DELETE /skill-sources/{id} (204). 409 while policy rules reference it. */
export async function deleteSkillSource(id: string): Promise<void> {
  await request("DELETE", `${API_BASE}/skill-sources/${encodeURIComponent(id)}`);
}
export function setSkillSourceEnabled(id: string, enabled: boolean): Promise<SkillSource> {
  return request("PATCH", `${API_BASE}/skill-sources/${encodeURIComponent(id)}`, { body: { enabled } });
}
// CONTRACT: query names incl. sourceId and hasScripts (D13) verified: SkillsPage.test.tsx › sends the Source and Scripts filters under the backend's query names
export async function listSkills(q: SkillQuery, signal?: AbortSignal): Promise<Page<Skill>> {
  const raw = await request<unknown>("GET", `${API_BASE}/skills`, {
    signal,
    query: { q: q.q, domain: q.domain, operation: q.operation, sourceId: q.sourceId, enabled: q.enabled, available: q.available, reviewed: q.reviewed, hasScripts: q.hasScripts, limit: q.limit, offset: q.offset },
  });
  return toPage<Skill>(raw, q.limit, q.offset);
}
/** GET /skills/{id}/versions: the recorded version history, oldest first. */
export async function listSkillVersions(id: string, signal?: AbortSignal): Promise<SkillVersionRow[]> {
  return toList<SkillVersionRow>(await request("GET", `${API_BASE}/skills/${encodeURIComponent(id)}/versions`, { signal }));
}
export function getSkill(id: string, signal?: AbortSignal): Promise<SkillDetail> {
  return request("GET", `${API_BASE}/skills/${encodeURIComponent(id)}`, { signal });
}

/** Non-JSON GET (text body, zip bundle): same credential/headers/error rules as request(). */
async function requestRaw(path: string, query?: Record<string, QueryValue>, signal?: AbortSignal): Promise<Response> {
  const presented: Identity = "admin";
  let res: Response;
  try {
    res = await fetch(buildUrl(path, query), { method: "GET", headers: buildHeaders(false, "admin"), credentials: "omit", signal });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, path, presented);
  }
  noteResponse(presented, res.status);
  if (!res.ok) throw await failure(res, path, presented);
  return res;
}
// GET /skills/{id}/body -> JSON {id, body, bodyTokensEst}. Rendered as plain text only.
export async function getSkillBody(id: string, signal?: AbortSignal): Promise<string> {
  const raw = await request<{ body?: unknown }>("GET", `${API_BASE}/skills/${encodeURIComponent(id)}/body`, { signal });
  return typeof raw?.body === "string" ? raw.body : "";
}
// GET /skills/bundle?agentId= -> application/zip; 409 stale / 413 too large|too many.
export async function downloadSkillBundle(agentId: string): Promise<Blob> {
  return (await requestRaw(`${API_BASE}/skills/bundle`, { agentId })).blob();
}

// POST /skills/{id}/activate {agentId?, routeRequestId?}; 403 denied, 404 not routed, 429 rate_limited.
// agentId is required for admin-initiated activation.
// CONTRACT: response {body, resources[], recordId} verified: meta.test.ts › activate.result
export async function activateSkill(id: string, opts: { agentId?: string } = {}, signal?: AbortSignal): Promise<SkillActivation> {
  const body: JsonObject = {};
  if (opts.agentId) body.agentId = opts.agentId;
  const raw = await request<Partial<SkillActivation>>("POST", `${API_BASE}/skills/${encodeURIComponent(id)}/activate`, { body, signal, as: "agent" });
  return { body: typeof raw?.body === "string" ? raw.body : "", resources: raw?.resources ?? [], recordId: raw?.recordId ?? null };
}

// ---- wave-5 setup wizard ----
export interface SetupStatus {
  needsSetup: boolean;
  completedAt: string | null;
  hasAdminToken: boolean;
  devMode: boolean;
  backend: string;
  counts: { principals: number; servers: number; tools: number; skillSources: number };
}

export function getSetupStatus(signal?: AbortSignal): Promise<SetupStatus> {
  return request("GET", `${API_BASE}/setup/status`, { signal });
}

export function completeSetup(): Promise<{ completed: boolean; completedAt: string }> {
  return request("POST", `${API_BASE}/setup/complete`);
}

/** Claude-Desktop `{"mcpServers": {...}}` JSON, posted as-is to the import endpoint. */
export function importServers(config: unknown): Promise<unknown> {
  return request("POST", `${API_BASE}/servers/import`, { body: config });
}

// CONTRACT: POST /route/{requestId}/feedback {items} (admin bearer → source=human) verified: LensPage.test.tsx › prefills the agent from ?agentId= and rates against ?routeRequestId=
export async function postRouteFeedback(requestId: string, items: FeedbackItem[]): Promise<FeedbackResult> {
  return request<FeedbackResult>("POST", `${API_BASE}/route/${encodeURIComponent(requestId)}/feedback`, {
    body: { items } as unknown as JsonObject,
  });
}
