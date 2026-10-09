import { describe, expect, it, vi } from "vitest";
import {
  ApiError,
  camelizeKeys,
  curatedConflict,
  deleteServer,
  describeError,
  executeTool,
  getModelsHealth,
  getTool,
  getApproval,
  listDedupSuggestions,
  listRules,
  listServers,
  listTools,
  refreshServer,
  normaliseSimulation,
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

  it("executeTool sends agentId/routeRequestId only when given, and keeps result content opaque", async () => {
    const { calls } = mockFetch({
      "POST /api/v1/tools/t1/execute": () => ({
        json: {
          status: "ok",
          detail: "",
          recordId: "rec1",
          approvalId: null,
          errors: [],
          result: { content: [{ type: "text", text: "x", some_key: 1 }], isError: false, structuredContent: { a_b: 1 } },
          latencyMs: 4.2,
        },
      }),
    });
    await executeTool("t1", { repo_name: "a" });
    const res = await executeTool("t1", { repo_name: "a" }, undefined, { agentId: "agent1", routeRequestId: "r9" });
    expect(calls[0].body).toEqual({ arguments: { repo_name: "a" } });
    expect(calls[1].body).toEqual({ arguments: { repo_name: "a" }, agentId: "agent1", routeRequestId: "r9" });
    expect(res.status).toBe("ok");
    expect(res.latencyMs).toBe(4.2);
    expect(res.result?.content).toEqual([{ type: "text", text: "x", some_key: 1 }]);
    expect(res.result?.structuredContent).toEqual({ a_b: 1 });
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

  // The real message from discovery/registry.py delete_server (HS-U-014).
  const REFERENCED = "server is referenced by policy rules; remove those rules before deleting it";

  it("shows a 409's curated detail (the backend's next step) instead of the generic conflict advice", async () => {
    mockFetch({ "DELETE /api/v1/servers/s1": () => ({ status: 409, json: { detail: REFERENCED } }) });
    const err = await deleteServer("s1").catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(409);
    expect(describeError(err)).toEqual({
      status: "HTTP 409",
      advice: "Server is referenced by policy rules; remove those rules before deleting it.",
    });
  });

  it("falls back to the generic 409 advice when the detail is missing, structured, multi-line or long", async () => {
    const generic = "It conflicts with existing data (for example a duplicate name). Change the input and retry.";
    for (const text of ["", "not json", JSON.stringify({ detail: [{ msg: "x" }] }), JSON.stringify({ detail: "a\nb" }), JSON.stringify({ detail: "x".repeat(301) })]) {
      mockFetch({ "DELETE /api/v1/servers/s1": () => ({ status: 409, text }) });
      const err = await deleteServer("s1").catch((e: unknown) => e);
      expect(describeError(err).advice, text).toBe(generic);
    }
    expect(curatedConflict(JSON.stringify({ detail: "  agentId already exists " }))).toBe("agentId already exists");
  });

  it("reads a detail only from a 409: other statuses keep the body out", async () => {
    mockFetch({ "DELETE /api/v1/servers/s1": () => ({ status: 400, json: { detail: "psycopg: password=hunter2" } }) });
    const err = await deleteServer("s1").catch((e: unknown) => e);
    expect(JSON.stringify(describeError(err)) + JSON.stringify(err)).not.toContain("hunter2");
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
    const {
      items: [s],
    } = await listDedupSuggestions({ limit: 50, offset: 0 });
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

describe("skills on /route/simulate (S2d item 2)", () => {
  it("normalises simulate skills, kinds, per-kind pruning and the maxSkills clamp", () => {
    const s = normaliseSimulation(
      {
        tools: [],
        skills: [{ skillId: "s1", source: "src-pdf", skill: "pdf-form-filler", score: 0.88, bodyTokensEst: 100 }],
        maxSkillsApplied: 1,
        diagnostics: {
          candidatesConsidered: [{ kind: "tool" }, { kind: "skill" }],
          stages: [{ stage: "maxSkills", before: 2, after: 1, pruned: [{ toolId: "s2", server: "src", tool: "pdf-helper", kind: "skill" }] }],
          policyFiltered: [{ server: "slack", tool: "search", kind: "tool", reason: "no matching policy rule" }],
          budgetClamps: [{ budget: "maxSkills", requested: 1, principal: 3, globalCap: 3, applied: 1, clampedBy: null }],
        },
      },
      "sk",
    );
    expect(s.skills[0].skillId).toBe("s1");
    expect(s.maxSkillsApplied).toBe(1);
    expect(s.noMatch).toBe(false);
    expect(s.stages[0].prunedByKind).toEqual({ skill: 1 });
    expect(s.filtered[0].kind).toBe("tool");
    expect(s.clamps.find((c) => c.budget === "maxSkills")?.applied).toBe(1);
  });

  it("defaults skills to [] on an older backend", () => {
    const s = normaliseSimulation({ tools: [{ server: "a", tool: "b", score: 1 }] }, "x");
    expect(s.skills).toEqual([]);
    expect(s.maxSkillsApplied).toBeNull();
  });
});

describe("getApproval (admin path)", () => {
  const ap = (status: string) => ({ id: "ap-1", agent_id: "a", tool_id: "t", status, summary: {}, created_at: "", expires_at: "", decided_at: null, result_preview: null });
  it("polls only the pending list while the approval is pending", async () => {
    const { calls } = mockFetch({ "GET /api/v1/approvals": () => ({ json: [ap("pending")] }) });
    expect((await getApproval("ap-1", false))?.status).toBe("pending");
    expect(calls.map((c) => c.query)).toEqual([{ status: "pending" }]);
  });
  it("looks once in the full list after it leaves pending", async () => {
    const { calls } = mockFetch({ "GET /api/v1/approvals": (_b, call) => ({ json: call.query.status === "pending" ? [] : [ap("executed")] }) });
    expect((await getApproval("ap-1", false))?.status).toBe("executed");
    expect(calls.map((c) => c.query)).toEqual([{ status: "pending" }, {}]);
  });
});

