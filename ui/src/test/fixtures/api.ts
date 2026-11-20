// Shared API-mock fixtures for component tests (T-a8aa1db9).
// One handler per endpoint the component tests hit, plus knobs (rows / empty / error).
// Tests spread defaultHandlers() and override per-test where they need specific data.

export type MockResponse = { status?: number; json?: unknown; text?: string };
export type MockHandler = (body: unknown) => MockResponse;

const ISO = "2026-10-01T00:00:00Z";

/* ---------- row builders (defaults + per-test rows) ---------- */

export function serverRow(over?: Partial<{ id: string; name: string; transport: string; enabled: boolean; status: string; tool_count: number }>) {
  return { id: "s1", name: "github", transport: "stdio", enabled: true, status: "healthy", tool_count: 4, ...over };
}

export function toolRow(over?: Partial<{ id: string; server_id: string; name: string; description: string; input_schema: object; schema_hash: string; tags: string[]; operation: string; required_scopes: string[]; enabled: boolean; version: number }>) {
  return { id: "t1", server_id: "s1", name: "list_issues", description: "d", input_schema: {}, schema_hash: "h", tags: [], operation: "read", required_scopes: [], enabled: true, version: 1, ...over };
}

export function skillRow(over?: Partial<{ id: string; source_id: string; name: string; operation: string; enabled: boolean }>) {
  return { id: "k1", source_id: "src1", name: "pdf-fill", operation: "write", enabled: true, ...over };
}

export function skillSourceRow(over?: Partial<{ id: string; name: string; kind: string; location: string; enabled: boolean; skill_count: number }>) {
  return { id: "src1", name: "team-skills", kind: "directory", location: "/srv/skills", enabled: true, skill_count: 5, ...over };
}

export function principalRow(over?: Partial<{ id: string; agent_id: string; enabled: boolean; max_tools: number; max_servers: number; max_skills: number; created_at: string }>) {
  return { id: "p1", agent_id: "billing-bot", enabled: true, max_tools: 8, max_servers: 4, max_skills: 3, created_at: ISO, ...over };
}

export function policyRuleRow(over?: Partial<{ id: string; agent_id: string; resource_kind: string; server_id: string; tool_name: string; max_operation: string; requires_approval: boolean; created_at: string }>) {
  return { id: "r1", agent_id: "billing-bot", resource_kind: "tool", server_id: "srv1", tool_name: "list_*", max_operation: "read", requires_approval: false, created_at: ISO, ...over };
}

export function pageRows<T>(rows: T[]) {
  return { items: rows, total: rows.length, limit: 50, offset: 0 };
}

export const emptyPage = () => ({ items: [], total: 0, limit: 50, offset: 0 });

/* ---------- knob helpers ---------- */

/** Answer `status` with a `detail` body. */
export function errorResponse(status = 500, detail = "Server is having a moment") {
  return () => ({ status, json: { detail } });
}

/* ---------- knobs: whole payloads ---------- */

/** POST /route/simulate response (snake_case wire shape), shared by Lens tests. */
export const simulation = {
  request_id: "sim-1",
  agent_id: "billing-bot",
  tools: [
    { server: "github", tool: "get_pr", score: 0.61 },
    { server: "github", tool: "list_prs", score: 0.93 },
  ],
  fallback_used: false,
  no_match: false,
  latency_ms: 21,
  max_tools_applied: 8,
  max_servers_applied: null,
  clamps: [
    { budget: "maxTools", requested: 40, principal: 8, global_cap: 20, applied: 8, clamped_by: "principal" },
    { budget: "maxServers", requested: null, principal: null, global_cap: null, applied: null, clamped_by: null },
  ],
  diagnostics: {
    candidates: 57,
    stages: [
      { stage: "retrieve", before: 312, after: 57 },
      { stage: "policy", before: 57, after: 12 },
      { stage: "budget", before: 12, after: 2 },
    ],
    policy_filtered: [{ server: "github", tool: "delete_repo", reason: "operation execute exceeds the agent's read ceiling" }],
  },
};

/* ---------- default handlers (one per mocked endpoint) ---------- */

export function defaultHandlers(): Record<string, MockHandler> {
  return {
    "GET /healthz": () => ({ json: { status: "ok" } }),
    "GET /api/v1/analytics/overview": () => ({
      json: {
        window: { label: "7d", start: "2026-10-01T00:00:00Z", end: "2026-10-08T00:00:00Z" },
        context_economy: { served_decisions: 0, unscored_decisions: 0, no_match_decisions: 0, exposed_tokens: 0, catalog_tokens: 0, tokens_not_sent: 0, savings: 0, estimator: "chars/4", catalog_basis: "current scope" },
        funnel: { surfaced: 0, selected: 0, succeeded: 0, failed: 0, selection_rate: 0, success_rate: 0 },
        routing: { decisions: 0, no_match: 0, no_match_rate: 0, fallback: 0, fallback_rate: 0, latency_p50_ms: null, latency_p95_ms: null },
        executions: { attempts: 0, denied: 0, denial_rate: 0, attributed: 0, attribution_coverage: 0, off_funnel_selections: 0 },
        position_curve: [],
      },
    }),
    "GET /api/v1/analytics/suggestions": () => ({ json: { window: { label: "7d", start: "2026-10-01T00:00:00Z", end: "2026-10-08T00:00:00Z" }, wasted_exposure: [], stale_tools: [], never_routed_servers: [] } }),
    "GET /api/v1/analytics/tools": () => ({ json: emptyPage() }),
    "GET /api/v1/analytics/agents": () => ({ json: { window: { label: "7d", start: "2026-10-01T00:00:00Z", end: "2026-10-08T00:00:00Z" }, items: [] } }),
    "GET /api/v1/servers": () => ({ json: [serverRow()] }),
    "GET /api/v1/skill-sources": () => ({ json: [skillSourceRow()] }),
    "GET /api/v1/principals": () => ({ json: [principalRow()] }),
    "GET /api/v1/policy-rules": () => ({ json: [policyRuleRow()] }),
    "GET /api/v1/tools": () => ({ json: emptyPage() }),
    "GET /api/v1/skills": () => ({ json: emptyPage() }),
    "GET /api/v1/approvals": () => ({ json: [] }),
    "GET /api/v1/dedup/suggestions": () => ({ json: [] }),
    "GET /api/v1/executions": () => ({ json: emptyPage() }),
    "GET /api/v1/models/health": () => ({ json: { loaded: true, mode: "balanced", device: "cpu", embedding: { backend: "x" }, decision: { backend: "y" }, memory: {} } }),
    "GET /api/v1/me": () => ({ json: { id: "p1", agent_id: "billing-bot", enabled: true, max_tools: 8 } }),
    "POST /api/v1/route/simulate": () => ({ json: simulation }),
    "GET /api/v1/tools/t1": () => ({ json: toolRow() }),
    "GET /api/v1/skills/k1": () => ({ json: skillRow() }),
    "GET /api/v1/skills/k1/versions": () => ({
      json: [
        { version: 1, changeKind: "added", contentHash: "aaaaaaaa11", manifestHash: null, recordedAt: "2026-10-01T00:00:00Z" },
        { version: 2, changeKind: "changed", contentHash: "bbbbbbbb22", manifestHash: null, recordedAt: "2026-10-02T00:00:00Z" },
      ],
    }),
  };
}

/** Spread defaults into a test's own handlers; per-test keys win. */
export function withDefaults(overrides: Record<string, MockHandler> = {}): Record<string, MockHandler> {
  return { ...defaultHandlers(), ...overrides };
}
