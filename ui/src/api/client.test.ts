import { describe, expect, it, vi } from "vitest";
import {
  ApiError,
  camelizeKeys,
  describeError,
  getModelsHealth,
  getTool,
  listDedupSuggestions,
  listRules,
  listServers,
  listTools,
  refreshServer,
  simulateRoute,
  snakeToCamel,
} from "./client";
import { mockFetch } from "../test/render";

describe("snakeToCamel", () => {
  it("converts snake_case and leaves camelCase alone", () => {
    expect(snakeToCamel("last_discovered_at")).toBe("lastDiscoveredAt");
    expect(snakeToCamel("latencyMs")).toBe("latencyMs");
    expect(snakeToCamel("p95_ms")).toBe("p95Ms");
  });
});

describe("camelizeKeys", () => {
  it("deep-converts keys in objects and arrays", () => {
    const out = camelizeKeys({ request_id: "r", tools: [{ server_name: "gh", tool_name: "x" }], fallback_used: true });
    expect(out).toEqual({ requestId: "r", tools: [{ serverName: "gh", toolName: "x" }], fallbackUsed: true });
  });

  it("never rewrites keys inside opaque payloads (input schema, snapshots, scores)", () => {
    const raw = {
      input_schema: { properties: { repo_name: { type: "string" } }, required: ["repo_name"] },
      versions: [{ change_kind: "schema", snapshot: { input_schema: { a_b: 1 } } }],
      scores: { "tool_id_1": 0.9 },
    };
    const out = camelizeKeys(raw) as Record<string, unknown>;
    expect(out.inputSchema).toEqual({ properties: { repo_name: { type: "string" } }, required: ["repo_name"] });
    expect((out.versions as Record<string, unknown>[])[0]).toEqual({ changeKind: "schema", snapshot: { input_schema: { a_b: 1 } } });
    expect(out.scores).toEqual({ tool_id_1: 0.9 });
  });
});

describe("client requests", () => {
  it("maps a snake_case tools page and sends camelCase filter params", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/tools": () => ({
        json: {
          items: [
            {
              id: "t1",
              server_id: "s1",
              name: "list_issues",
              description: "",
              input_schema: { properties: { page_size: {} } },
              schema_hash: "abc",
              tags: [],
              operation: "read",
              required_scopes: ["repo"],
              enabled: true,
              version: 2,
              call_count: 7,
              avg_latency_ms: 12.5,
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
        },
      }),
    });
    const page = await listTools({ q: "issues", serverId: "s1", enabled: true, limit: 50, offset: 0 });
    expect(page.total).toBe(1);
    const t = page.items[0];
    expect(t.serverId).toBe("s1");
    expect(t.requiredScopes).toEqual(["repo"]);
    expect(t.callCount).toBe(7);
    expect(t.avgLatencyMs).toBe(12.5);
    expect(t.inputSchema).toEqual({ properties: { page_size: {} } });
    const url = new URL(calls[0].url, "http://x");
    expect(url.searchParams.get("q")).toBe("issues");
    expect(url.searchParams.get("serverId")).toBe("s1");
    expect(url.searchParams.get("enabled")).toBe("true");
    expect(url.searchParams.has("domain")).toBe(false);
  });

  it("accepts a bare array where an envelope was expected", async () => {
    mockFetch({ "GET /api/v1/servers": () => ({ json: [{ id: "s1", name: "gh", last_discovered_at: null, tool_count: 3 }] }) });
    const servers = await listServers();
    expect(servers[0].toolCount).toBe(3);
  });

  it("defaults missing tool versions to an empty list", async () => {
    mockFetch({ "GET /api/v1/tools/t1": () => ({ json: { id: "t1", name: "x" } }) });
    expect((await getTool("t1")).versions).toEqual([]);
  });

  it("normalises the SPEC §9 literal route response ({server, tool}, snake_case)", async () => {
    const { calls } = mockFetch({
      "POST /api/v1/route": () => ({
        json: { request_id: "r1", tools: [{ server: "github", tool: "list_prs", score: 0.81 }], fallback_used: false, latency_ms: 42.1 },
      }),
    });
    const res = await simulateRoute({ query: "prs", agentId: "a1", maxTools: 5 });
    expect(res).toEqual({
      requestId: "r1",
      tools: [{ toolId: undefined, serverName: "github", toolName: "list_prs", score: 0.81 }],
      fallbackUsed: false,
      latencyMs: 42.1,
    });
    expect(calls[0].body).toEqual({ query: "prs", agentId: "a1", maxTools: 5 });
  });

  it("throws ApiError with status only — the response body never reaches the message", async () => {
    mockFetch({ "GET /api/v1/servers": () => ({ status: 500, text: "psycopg: password=hunter2 host=db" }) });
    const err = await listServers().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(500);
    const shown = JSON.stringify(describeError(err)) + (err as Error).message;
    expect(shown).not.toContain("hunter2");
    expect(describeError(err).status).toBe("HTTP 500");
  });

  it("reports a network failure as status 0", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    const err = await listServers().catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(0);
    expect(describeError(err).status).toBe("network error");
  });
});

describe("integration reconciliation (backend shapes)", () => {
  it("maps the backend models-health payload onto the UI shape", async () => {
    mockFetch({
      "GET /api/v1/models/health": () => ({
        json: {
          status: "ok",
          loaded: true,
          mode: "balanced",
          device: "cpu",
          embedding: { backend: "hash-v1", requested: "hash", model_id: null },
          decision: { backend: "laya@55cf4c4ebb4e", requested: "laya", model_id: "convaiinnovations/laya", revision: "55cf4c4" },
          memory: { rss_bytes: 50 * 1024 * 1024 },
        },
      }),
    });
    const h = await getModelsHealth();
    expect(h.device).toBe("cpu");
    expect(h.gpu).toBeNull();
    expect(h.memory?.rssMb).toBe(50);
    expect(h.models.map((m) => [m.kind, m.name, m.loaded])).toEqual([
      ["embedding", "hash-v1", true],
      ["decision", "convaiinnovations/laya", true],
    ]);
  });

  it("flattens nested tool stats", async () => {
    mockFetch({
      "GET /api/v1/tools/t1": () => ({
        json: { id: "t1", name: "x", stats: { call_count: 3, error_count: 1, avg_latency_ms: 12.5 }, versions: [{ version: 2 }] },
      }),
    });
    const t = await getTool("t1");
    expect([t.callCount, t.errorCount, t.avgLatencyMs]).toEqual([3, 1, 12.5]);
    expect(t.versions[0].id).toBe("t1@2");
  });

  it("uses the backend's /policy-rules path", async () => {
    const { calls } = mockFetch({ "GET /api/v1/policy-rules": () => ({ json: [] }) });
    expect(await listRules()).toEqual([]);
    expect(calls).toHaveLength(1);
  });

  it("takes suggestion tool ids from the backend's slim refs and drops the refs", async () => {
    mockFetch({
      "GET /api/v1/dedup/suggestions": () => ({
        json: { items: [{ id: "d1", tool_a: { id: "a", name: "x" }, tool_b: { id: "b", name: "y" }, similarity: 0.9, status: "open" }] },
      }),
    });
    const [s] = await listDedupSuggestions();
    expect([s.toolAId, s.toolBId, s.toolA, s.toolB]).toEqual(["a", "b", undefined, undefined]);
  });
});

describe("refreshServer", () => {
  it("unwraps the backend's {server, added, ...} refresh report", async () => {
    mockFetch({
      "POST /api/v1/servers/s1/refresh": () => ({ json: { server: { id: "s1", name: "gh", tool_count: 12 }, added: ["x"] } }),
    });
    const s = await refreshServer("s1");
    expect([s.id, s.toolCount]).toEqual(["s1", 12]);
  });
});
