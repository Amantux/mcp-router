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
 * Errors: non-2xx and network failures throw ApiError carrying only the
 * status. The response body is deliberately never surfaced to the UI.
 */
import type {
  ClassificationUpdate,
  CreatePrincipalRequest,
  CreateRuleRequest,
  CreatedPrincipal,
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
  RouteRequest,
  RouteResponse,
  RoutedTool,
  ToolDetail,
  ToolQuery,
} from "./types";

export const API_BASE = "/api/v1";
/** Raw Prometheus text; the app mounts it at /metrics (SPEC §9 says /api/v1/metrics). */
export const METRICS_URL = "/metrics";

// ------------------------------------------------------------ case mapping
const OPAQUE_KEYS = new Set(["inputSchema", "snapshot", "scores"]);

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
  constructor(status: number, path: string) {
    super(status === 0 ? `Network error calling ${path}` : `HTTP ${status} from ${path}`);
    this.name = "ApiError";
    this.status = status;
    this.path = path;
  }
}

/** Curated, user-facing explanation of a failure. Never includes a response body. */
export function describeError(err: unknown): { status: string; advice: string } {
  if (!(err instanceof ApiError)) {
    return { status: "", advice: "Something unexpected went wrong in the dashboard. Reload the page and try again." };
  }
  const s = err.status;
  if (s === 0)
    return { status: "network error", advice: "Couldn't reach the MCP Router backend. Check it is running on port 8400, then retry." };
  const status = `HTTP ${s}`;
  if (s === 401 || s === 403)
    return { status, advice: "The backend refused this request. Check the gateway auth configuration, then retry." };
  if (s === 404)
    return { status, advice: "The item no longer exists, or this backend doesn't provide the endpoint yet. Refresh and retry." };
  if (s === 409) return { status, advice: "It conflicts with existing data (for example a duplicate name). Change the input and retry." };
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
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: opts.body !== undefined ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,
    });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, path);
  }
  if (!res.ok) throw new ApiError(res.status, path);
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new ApiError(res.status, path);
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

export function refreshServer(id: string): Promise<MCPServer> {
  return request("POST", `${API_BASE}/servers/${encodeURIComponent(id)}/refresh`);
}

// CONTRACT: enable/disable via PATCH /api/v1/servers/{id} {enabled}.
export function setServerEnabled(id: string, enabled: boolean): Promise<MCPServer> {
  return request("PATCH", `${API_BASE}/servers/${encodeURIComponent(id)}`, { body: { enabled } });
}

// ------------------------------------------------------------------- tools
// CONTRACT: query params q, domain, operation, serverId, enabled, available, limit, offset.
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
  return toPage<MCPTool>(raw, q.limit, q.offset);
}

export async function getTool(id: string, signal?: AbortSignal): Promise<ToolDetail> {
  const t = await request<ToolDetail>("GET", `${API_BASE}/tools/${encodeURIComponent(id)}`, { signal });
  return { ...t, versions: t.versions ?? [] };
}

export function updateClassification(id: string, body: ClassificationUpdate): Promise<MCPTool> {
  return request("PATCH", `${API_BASE}/tools/${encodeURIComponent(id)}/classification`, { body });
}

// ------------------------------------------------------------------- dedup
export async function listDedupSuggestions(status: DedupStatus = "open", signal?: AbortSignal): Promise<DuplicateSuggestion[]> {
  return toList<DuplicateSuggestion>(await request("GET", `${API_BASE}/dedup/suggestions`, { signal, query: { status } }));
}

// CONTRACT: POST /dedup/suggestions triggers a scan; response body ignored.
export async function runDedupScan(): Promise<void> {
  await request("POST", `${API_BASE}/dedup/suggestions`);
}

// CONTRACT: accept body {preferredToolId}.
export function acceptDedup(id: string, preferredToolId: string): Promise<DuplicateSuggestion> {
  return request("POST", `${API_BASE}/dedup/suggestions/${encodeURIComponent(id)}/accept`, { body: { preferredToolId } });
}

// CONTRACT: dismiss body {justification}; backend rejects empty (422).
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

// CONTRACT: no auth header is sent; the simulator assumes the dev/admin API is
// reachable without an agent key (or that the backend authorises the UI separately).
export async function simulateRoute(body: RouteRequest): Promise<RouteResponse> {
  const raw = await request<RouteResponse & { tools: Record<string, unknown>[] }>("POST", `${API_BASE}/route`, { body });
  return { ...raw, tools: (raw.tools ?? []).map(normaliseRoutedTool) };
}

// ------------------------------------------------------------ models/health
export function getModelsHealth(signal?: AbortSignal): Promise<ModelsHealth> {
  return request("GET", `${API_BASE}/models/health`, { signal });
}

export function getHealthz(signal?: AbortSignal): Promise<Healthz> {
  return request("GET", "/healthz", { signal });
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

export async function listRules(signal?: AbortSignal): Promise<PolicyRule[]> {
  return toList<PolicyRule>(await request("GET", `${API_BASE}/rules`, { signal }));
}

export function createRule(body: CreateRuleRequest): Promise<PolicyRule> {
  return request("POST", `${API_BASE}/rules`, { body });
}
