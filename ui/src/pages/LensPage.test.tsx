import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
import { camelizeKeys, normaliseSimulation } from "../api/client";
import { ClampView, LensPage, LensResult } from "./LensPage";

const SIM = {
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

describe("LensResult", () => {
  it("visualises a clamp: requested vs applied, the binding cap, and the fill proportion", () => {
    renderWithProviders(<LensResult result={normaliseSimulation(camel(SIM), "billing-bot")} showFiltered />);
    const tools = screen.getByTestId("clamp-maxTools");
    expect(within(tools).getByText("clamped")).toBeTruthy();
    expect(screen.getByTestId("clamp-maxTools-summary").textContent).toBe("requested 40 → applied 8");
    expect(screen.getByTestId("clamp-maxTools-by").textContent).toBe("Limited by the agent's own cap.");
    expect((screen.getByTestId("clamp-maxTools-fill") as HTMLElement).style.width).toBe("20%"); // 8 of max(40, 8, 20)
    expect(within(tools).getByText("agent cap 8 · global cap 20")).toBeTruthy();
    const servers = screen.getByTestId("clamp-maxServers");
    expect(within(servers).getByText("as requested")).toBeTruthy();
    expect(screen.getByTestId("clamp-maxServers-summary").textContent).toBe("requested default → applied ∞");
  });

  it("ranks exposed tools by score and lists filtered-out tools with their reasons", () => {
    renderWithProviders(<LensResult result={normaliseSimulation(camel(SIM), "billing-bot")} showFiltered />);
    const rows = within(screen.getByRole("table", { name: "Exposed tools" })).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[1].textContent)).toEqual(["list_prs", "get_pr"]);
    const filtered = screen.getByRole("table", { name: "Filtered tools" });
    expect(within(filtered).getByText("delete_repo")).toBeTruthy();
    expect(within(filtered).getByText("operation execute exceeds the agent's read ceiling")).toBeTruthy();
    expect(screen.getByLabelText("Pipeline stages").textContent).toContain("policy: 57 → 12 (−45)");
    expect(screen.getByText("57 candidates considered")).toBeTruthy();
  });

  it("shows no-match and fallback banners", () => {
    renderWithProviders(<LensResult result={normaliseSimulation({ ...camel(SIM), tools: [], fallbackUsed: true }, "a")} showFiltered={false} />);
    expect(screen.getByTestId("no-match-banner")).toBeTruthy();
    expect(screen.getByTestId("fallback-banner")).toBeTruthy();
    expect(screen.queryByLabelText("Filtered out")).toBeNull();
  });
});

// The EXACT backend shape (api/routes_route.py SimulateResponse, camelCase
// on the wire; budgets notes §4), read from the routing code.
const BACKEND_SIM = {
  requestId: "sim-2",
  agentId: "agent1",
  simulated: true,
  tools: [{ toolId: "t-1", server: "github", tool: "search_issues", score: 0.8 }],
  noMatch: false,
  fallbackUsed: false,
  latencyMs: 33.4,
  modelVersion: "simulated/deterministic-v1",
  maxToolsApplied: 5,
  maxServersApplied: 2,
  diagnostics: {
    candidatesConsidered: [
      { toolId: "t-1", server: "github", tool: "search_issues", domain: "development", operation: "read", retrievalScore: 0.49, matchedOn: ["vector"] },
      { toolId: "t-2", server: "github", tool: "create_issue", domain: "development", operation: "write", retrievalScore: 0.4, matchedOn: [] },
      { toolId: "t-3", server: "slack", tool: "search_messages", domain: "communication", operation: "read", retrievalScore: 0.3, matchedOn: [] },
    ],
    stages: [
      { stage: "retrieval", before: 3, after: 1, pruned: [{ toolId: "t-2", server: "github", tool: "create_issue" }], detail: { limit: 20, policyFiltered: 2 } },
      { stage: "maxTools", before: 1, after: 1, pruned: [], detail: { limit: 5 } },
    ],
    policyFiltered: [{ toolId: "t-3", server: "slack", tool: "search_messages", operation: "read", reason: "no matching policy rule" }],
    budgetClamps: [
      { budget: "maxTools", requested: 5, principal: 8, globalCap: 8, applied: 5, clampedBy: null },
      { budget: "maxServers", requested: 4, principal: 2, globalCap: null, applied: 2, clampedBy: "principal" },
    ],
  },
};

describe("normaliseSimulation (backend shape)", () => {
  it("reads budgetClamps and candidatesConsidered from diagnostics", () => {
    const n = normaliseSimulation(camel(BACKEND_SIM), "agent1");
    expect(n.clamps.map((c) => [c.budget, c.applied, c.clampedBy])).toEqual([
      ["maxTools", 5, null],
      ["maxServers", 2, "principal"],
    ]);
    expect(n.clamps[0].globalCap).toBe(8);
    expect(n.candidates).toBe(3);
    expect(n.stages).toEqual([
      { stage: "retrieval", before: 3, after: 1 },
      { stage: "maxTools", before: 1, after: 1 },
    ]);
    expect(n.filtered).toEqual([{ toolId: "t-3", serverName: "slack", toolName: "search_messages", reason: "no matching policy rule", stage: undefined }]);
    expect([n.maxToolsApplied, n.maxServersApplied, n.requestId, n.noMatch]).toEqual([5, 2, "sim-2", false]);
    expect(n.tools).toEqual([{ toolId: "t-1", serverName: "github", toolName: "search_issues", score: 0.8 }]);
  });

  it("renders the backend shape in the lens", () => {
    renderWithProviders(<LensResult result={normaliseSimulation(camel(BACKEND_SIM), "agent1")} showFiltered />);
    expect(screen.getByTestId("clamp-maxServers-by").textContent).toBe("Limited by the agent's own cap.");
    expect(screen.getByText("3 candidates considered")).toBeTruthy();
    expect(within(screen.getByRole("table", { name: "Filtered tools" })).getByText("search_messages")).toBeTruthy();
  });
});

describe("ClampView", () => {
  it("names the exact global setting that clamped a budget, not a wildcard (HS-U-046)", () => {
    renderWithProviders(<ClampView clamp={{ budget: "maxSkills", requested: 5, principal: 4, globalCap: 3, applied: 3, clampedBy: "global" }} />);
    expect(screen.getByTestId("clamp-maxSkills-by").textContent).toBe("Limited by the global cap (MCPR_MAX_EXPOSED_SKILLS).");
  });
});

describe("LensPage", () => {
  it("simulates for a picked principal, then re-queries (debounced) when a budget slider moves", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      "GET /api/v1/principals": () => ({ json: [{ id: "p1", agent_id: "billing-bot", enabled: true, max_tools: 8, created_at: "2026-10-01T00:00:00Z" }] }),
      "POST /api/v1/route/simulate": () => ({ json: SIM }),
    });
    renderWithProviders(<LensPage debounceMs={10} />, { route: "/lens" });
    await user.click(screen.getByRole("button", { name: "Show agent's view" }));
    expect(screen.getByText("Choose the agent whose view you want to see.")).toBeTruthy();

    await waitFor(() => expect(screen.getByRole("option", { name: /billing-bot/ })).toBeTruthy());
    await user.selectOptions(screen.getByRole("combobox", { name: /Agent/ }), "billing-bot");
    await user.type(screen.getByRole("textbox", { name: /Task query/ }), "review open PRs");
    await user.click(screen.getByRole("button", { name: "Show agent's view" }));
    await screen.findByTestId("clamp-maxTools");
    const sims = () => calls.filter((c) => c.url === "/api/v1/route/simulate");
    expect(sims()[0].body).toEqual({ agentId: "billing-bot", query: "review open PRs", maxTools: 8 });

    fireEvent.change(screen.getByRole("slider", { name: "Requested max tools" }), { target: { value: "40" } });
    fireEvent.change(screen.getByRole("slider", { name: "Requested max servers" }), { target: { value: "3" } });
    await waitFor(() => expect(sims().length).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(sims().at(-1)!.body).toEqual({ agentId: "billing-bot", query: "review open PRs", maxTools: 40, maxServers: 3 }));
  });

  it("explains the empty state when no agents exist", async () => {
    mockFetch({ "GET /api/v1/principals": () => ({ json: [] }) });
    renderWithProviders(<LensPage />, { route: "/lens" });
    expect(await screen.findByText("No agents yet")).toBeTruthy();
  });
  it("renders tools and skills sections, kind badges and the maxSkills clamp", () => {
    const sim = normaliseSimulation(
      {
        agentId: "sk",
        tools: [{ toolId: "t1", server: "docs", tool: "fill_pdf_form", score: 0.91 }],
        skills: [{ skillId: "s1", source: "src-pdf", skill: "pdf-form-filler", score: 0.88, bodyTokensEst: 1200 }],
        maxSkillsApplied: 1,
        diagnostics: {
          stages: [{ stage: "maxSkills", before: 2, after: 1, pruned: [{ toolId: "s2", server: "src-pdf", tool: "pdf-helper", kind: "skill" }] }],
          policyFiltered: [{ server: "src-x", tool: "secret-skill", kind: "skill", reason: "no matching policy rule" }],
          budgetClamps: [{ budget: "maxSkills", requested: 5, principal: 1, globalCap: 3, applied: 1, clampedBy: "principal" }],
        },
      },
      "sk",
    );
    renderWithProviders(<LensResult result={sim} showFiltered />);
    expect(within(screen.getByRole("table", { name: "Exposed tools" })).getByText("fill_pdf_form")).toBeTruthy();
    const skills = within(screen.getByRole("table", { name: "Surfaced skills" }));
    expect(skills.getByText("pdf-form-filler")).toBeTruthy();
    expect(skills.getByText("~1,200")).toBeTruthy();
    expect(screen.getByTestId("clamp-maxSkills")).toBeTruthy();
    expect(screen.getByTestId("clamp-maxSkills-by").textContent).toContain("agent's own cap");
    expect(screen.getByText(/maxSkills: 2 → 1.*1 skill\]/)).toBeTruthy();
    expect(within(screen.getByRole("table", { name: "Filtered tools" })).getByText("skill")).toBeTruthy();
  });

  it("re-queries with maxSkills when the skills slider moves", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      "GET /api/v1/principals": () => ({ json: [{ id: "p1", agent_id: "billing-bot", enabled: true, max_tools: 8, created_at: "2026-10-01T00:00:00Z" }] }),
      "POST /api/v1/route/simulate": () => ({ json: SIM }),
    });
    renderWithProviders(<LensPage debounceMs={10} />, { route: "/lens" });
    await waitFor(() => expect(screen.getByRole("option", { name: /billing-bot/ })).toBeTruthy());
    await user.selectOptions(screen.getByRole("combobox", { name: /Agent/ }), "billing-bot");
    await user.type(screen.getByRole("textbox", { name: /Task query/ }), "fill pdf");
    await user.click(screen.getByRole("button", { name: "Show agent's view" }));
    await screen.findByTestId("clamp-maxTools");
    fireEvent.change(screen.getByRole("slider", { name: "Requested max skills" }), { target: { value: "2" } });
    const sims = () => calls.filter((c) => c.url === "/api/v1/route/simulate");
    await waitFor(() => expect(sims().at(-1)!.body).toEqual({ agentId: "billing-bot", query: "fill pdf", maxTools: 8, maxSkills: 2 }));
  });
});

describe("LensPage deep link from an execution", () => {
  it("prefills the agent from ?agentId= and rates against ?routeRequestId=", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      "GET /api/v1/principals": () => ({ json: [{ id: "p1", agent_id: "billing-bot", enabled: true, max_tools: 8, created_at: "2026-10-01T00:00:00Z" }] }),
      "POST /api/v1/route/simulate": () => ({ json: SIM }),
      "POST /api/v1/route/r1/feedback": () => ({ json: { recorded: 1 } }),
    });
    renderWithProviders(<LensPage debounceMs={10} />, { route: "/lens?routeRequestId=r1&agentId=billing-bot" });
    await screen.findByRole("option", { name: /billing-bot/ });
    expect((screen.getByRole("combobox", { name: /Agent/ }) as HTMLSelectElement).value).toBe("billing-bot");
    await user.type(screen.getByRole("textbox", { name: /Task query/ }), "review open PRs");
    await user.click(screen.getByRole("button", { name: "Show agent's view" }));
    await user.click(await screen.findByRole("button", { name: "Helpful: list_prs" }));
    await waitFor(() =>
      expect(calls.find((c) => c.path === "/api/v1/route/r1/feedback")?.body).toEqual({ items: [{ kind: "tool", name: "list_prs", helpful: true }] }),
    );
  });
});

/** The client camelises responses before normaliseSimulation sees them; mirror that for direct calls. */
const camel = (o: unknown) => camelizeKeys(o) as Record<string, unknown>;
