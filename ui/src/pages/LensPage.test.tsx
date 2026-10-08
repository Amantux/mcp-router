import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
import { camelizeKeys, normaliseSimulation } from "../api/client";
import { LensPage, LensResult } from "./LensPage";

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

describe("LensPage", () => {
  it("simulates for a picked principal, then re-queries (debounced) when a budget slider moves", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      "GET /api/v1/principals": () => ({ json: [{ id: "p1", agent_id: "billing-bot", enabled: true, max_tools: 8, created_at: "2026-10-01T00:00:00Z" }] }),
      "POST /api/v1/route/simulate": () => ({ json: SIM }),
    });
    renderWithProviders(<LensPage debounceMs={10} />);
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
    renderWithProviders(<LensPage />);
    expect(await screen.findByText("No agents yet")).toBeTruthy();
  });
});

/** The client camelises responses before normaliseSimulation sees them; mirror that for direct calls. */
const camel = (o: unknown) => camelizeKeys(o) as Record<string, unknown>;
