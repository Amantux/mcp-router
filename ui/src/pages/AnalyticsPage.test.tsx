import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
import { AnalyticsPage } from "./AnalyticsPage";
import { ToolFunnelPanel } from "./ToolDetailDrawer";
import { FunnelBars } from "../components/analytics";

const WIN = { label: "7d", start: "2026-10-01T00:00:00Z", end: "2026-10-08T00:00:00Z" };
const ECON = {
  served_decisions: 120,
  unscored_decisions: 4,
  no_match_decisions: 6,
  exposed_tokens: 30_000,
  catalog_tokens: 240_000,
  tokens_not_sent: 210_000,
  savings: 0.875,
  estimator: "chars/4",
  catalog_basis: "current scope",
};
const OVERVIEW = {
  window: WIN,
  context_economy: ECON,
  funnel: { surfaced: 600, selected: 90, succeeded: 80, failed: 10, selection_rate: 0.15, success_rate: 0.889 },
  routing: { decisions: 126, no_match: 6, no_match_rate: 0.0476, fallback: 3, fallback_rate: 0.0238, latency_p50_ms: 41, latency_p95_ms: 180 },
  executions: { attempts: 100, denied: 5, denial_rate: 0.05, attributed: 92, attribution_coverage: 0.92, off_funnel_selections: 2 },
  position_curve: [
    { rank: 1, shown: 126, selected: 60, rate: 0.476 },
    { rank: 2, shown: 120, selected: 20, rate: 0.167 },
    { rank: 3, shown: 110, selected: 10, rate: 0.091 },
  ],
  catalog_drift: { added: 2 },
};
const SUGGESTIONS = {
  window: WIN,
  min_surfaced: 20,
  max_selection_rate: 0.05,
  stale_days: 30,
  wasted_exposure: [
    { tool_id: "t2", tool_name: "search_everything", server_name: "files", surfaced: 110, selected: 1, selection_rate: 0.009, exposed_tokens: 9000 },
    { tool_id: "t3", tool_name: "legacy_export", server_name: "db", surfaced: 40, selected: 0, selection_rate: 0, exposed_tokens: 2400 },
  ],
  stale_tools: [{ tool_id: "t9", tool_name: "fax_send", server_name: "comms", created_at: "2026-01-01T00:00:00Z", last_surfaced_at: null }],
  never_routed_servers: [],
};
const TOOLS = {
  window: WIN,
  items: [
    { tool_id: "t1", tool_name: "list_prs", server_name: "github", enabled: true, tokens: 120, surfaced: 200, selected: 50, succeeded: 45, failed: 5, selection_rate: 0.25, success_rate: 0.9, avg_rank: 1.4, exposed_tokens: 24000 },
  ],
  total: 1,
  limit: 25,
  offset: 0,
};
const AGENTS = {
  window: WIN,
  items: [
    {
      agent_id: "billing-bot",
      decisions: 126,
      no_match: 6,
      no_match_rate: 0.0476,
      fallback: 3,
      fallback_rate: 0.0238,
      latency_p50_ms: 41,
      latency_p95_ms: 180,
      attempts: 100,
      denied: 5,
      denial_rate: 0.05,
      attributed: 92,
      attribution_coverage: 0.92,
      surfaced: 600,
      selected: 90,
      selection_rate: 0.15,
      avg_surfaced_per_decision: 4.8,
      context_economy: ECON,
    },
  ],
};

function mockAnalytics(overview: unknown = OVERVIEW) {
  return mockFetch({
    "GET /api/v1/analytics/overview": () => ({ json: overview }),
    "GET /api/v1/analytics/suggestions": () => ({ json: SUGGESTIONS }),
    "GET /api/v1/analytics/tools": () => ({ json: TOOLS }),
    "GET /api/v1/analytics/agents": () => ({ json: AGENTS }),
  });
}

describe("AnalyticsPage", () => {
  it("renders headline cards with the raw token components behind the savings %", async () => {
    mockAnalytics();
    renderWithProviders(<AnalyticsPage />);
    const savings = await screen.findByTestId("card-savings");
    expect(savings.textContent).toContain("87.5%");
    expect(savings.textContent).toContain("210,000 tokens not sent: 30,000 exposed of 240,000");
    expect(savings.textContent).toContain("Estimate: chars/4");
    expect(screen.getByTestId("card-selection").textContent).toContain("15.0%");
    expect(screen.getByTestId("card-nomatch").textContent).toContain("4.8%");
    expect(screen.getByTestId("card-fallback").textContent).toContain("2.4%");
    expect(screen.getByTestId("card-denial").textContent).toContain("5.0%");
    expect(screen.getByTestId("card-latency").textContent).toContain("41 ms");
    expect(screen.getByTestId("card-latency").textContent).toContain("p95 180 ms");
    // Position bias: rank 1 is the tallest bar.
    expect((screen.getByTestId("rank-1") as HTMLElement).style.height).toBe("100%");
    expect(screen.getByRole("table", { name: "Agent analytics" }).textContent).toContain("billing-bot");
  });

  it("leads with wasted exposure, worst context spend first, framed as suggestions", async () => {
    mockAnalytics();
    renderWithProviders(<AnalyticsPage />);
    const section = await screen.findByRole("region", { name: "Wasted exposure" });
    expect(section.textContent).toContain("2 tools were shown at least 20 times but selected at most 5% of the time, spending 11,400 tokens");
    expect(section.textContent).toContain("nothing changes automatically");
    const rows = within(within(section).getByRole("table")).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[0].textContent)).toEqual(["search_everything", "legacy_export"]);
    // It sits above the overview cards.
    const cards = screen.getByLabelText("Overview");
    expect(section.compareDocumentPosition(cards) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("region", { name: "Staleness" }).textContent).toContain("nothing is disabled automatically");
    expect(screen.getByRole("table", { name: "Stale tools" }).textContent).toContain("fax_send");
  });

  it("shows per-tool funnel bars scaled to surfaced", async () => {
    mockAnalytics();
    renderWithProviders(<AnalyticsPage />);
    const table = await screen.findByRole("table", { name: "Tool funnels" });
    expect(within(table).getByRole("img", { name: "list_prs: surfaced 200, selected 50, succeeded 45" })).toBeTruthy();
  });

  it("re-queries every panel with the chosen window", async () => {
    const user = userEvent.setup();
    const { calls } = mockAnalytics();
    renderWithProviders(<AnalyticsPage />);
    await screen.findByTestId("card-savings");
    await user.click(screen.getByRole("tab", { name: "90 days" }));
    await waitFor(() => {
      const last = calls.filter((c) => c.url.includes("window=90d")).map((c) => c.url.split("?")[0]);
      expect(new Set(last)).toEqual(new Set(["/api/v1/analytics/overview", "/api/v1/analytics/suggestions", "/api/v1/analytics/tools", "/api/v1/analytics/agents"]));
    });
  });

  it("shows the no-data-yet empty state before any routing", async () => {
    mockAnalytics({ ...OVERVIEW, routing: { ...OVERVIEW.routing, decisions: 0 } });
    renderWithProviders(<AnalyticsPage />);
    expect(await screen.findByText("No routing data in this window yet")).toBeTruthy();
    expect(screen.getByText(/Analytics accrue once agents start routing/)).toBeTruthy();
  });
});

describe("FunnelBars", () => {
  it("scales selected and succeeded relative to surfaced", () => {
    renderWithProviders(<FunnelBars label="x" surfaced={200} selected={50} succeeded={45} />);
    expect((screen.getByTestId("funnel-surfaced") as HTMLElement).style.width).toBe("100%");
    expect((screen.getByTestId("funnel-selected") as HTMLElement).style.width).toBe("25%");
    expect((screen.getByTestId("funnel-succeeded") as HTMLElement).style.width).toBe("22.5%");
  });
});

describe("ToolDetailDrawer funnel tab", () => {
  it("loads the per-tool funnel, rank curve and co-surfaced tools", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/analytics/tools/t1": () => ({
        json: {
          window: WIN,
          tool: TOOLS.items[0],
          position_curve: [{ rank: 1, shown: 150, selected: 45, rate: 0.3 }],
          co_surfaced: [{ tool_id: "t5", tool_name: "get_pr", server_name: "github", co_surfaced: 80, this_selected: 30, other_selected: 25 }],
        },
      }),
    });
    renderWithProviders(<ToolFunnelPanel toolId="t1" />);
    expect(await screen.findByRole("img", { name: "list_prs: surfaced 200, selected 50, succeeded 45" })).toBeTruthy();
    expect(screen.getByRole("table", { name: "Co-surfaced tools" }).textContent).toContain("get_pr");
    expect(calls[0].url).toContain("window=30d");
  });
});
