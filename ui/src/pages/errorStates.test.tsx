import { useState, type ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
import { setCredentials } from "../api/auth";
import { AnalyticsPage } from "./AnalyticsPage";
import { ApprovalsPage } from "./ApprovalsPage";
import { DuplicatesPage } from "./DuplicatesPage";
import { ExecutionsPage } from "./ExecutionsPage";
import { HealthPage } from "./HealthPage";
import { PlaygroundPage } from "./PlaygroundPage";
import { PolicyPage } from "./PolicyPage";
import { ServersPage } from "./ServersPage";
import { SkillSourcesPage } from "./SkillSourcesPage";
import { SkillsPage } from "./SkillsPage";
import { ToolsPage } from "./ToolsPage";
import { ToolDetailDrawer } from "./ToolDetailDrawer";
import { SkillDrawer } from "./SkillsPage";

const API = "/api/v1";
const page = { items: [], total: 0, limit: 50, offset: 0 };
const WIN = { label: "7d", start: "2026-10-01T00:00:00Z", end: "2026-10-08T00:00:00Z" };
const EMPTY_OVERVIEW = {
  window: WIN,
  context_economy: { served_decisions: 0, unscored_decisions: 0, no_match_decisions: 0, exposed_tokens: 0, catalog_tokens: 0, tokens_not_sent: 0, savings: 0, estimator: "chars/4", catalog_basis: "current scope" },
  funnel: { surfaced: 0, selected: 0, succeeded: 0, failed: 0, selection_rate: 0, success_rate: 0 },
  routing: { decisions: 0, no_match: 0, no_match_rate: 0, fallback: 0, fallback_rate: 0, latency_p50_ms: null, latency_p95_ms: null },
  executions: { attempts: 0, denied: 0, denial_rate: 0, attributed: 0, attribution_coverage: 0, off_funnel_selections: 0 },
  position_curve: [],
};

interface Case {
  name: string;
  ui: ReactElement;
  route: string;
  /** Every GET the page makes, answered OK and empty. */
  ok: Record<string, unknown>;
  /** The route whose first load fails. */
  fails: string;
  /** Copy that would wrongly claim there is nothing (absent on failure, present after a good Retry). */
  emptyCopy: RegExp | null;
  what: string;
}

const CASES: Case[] = [
  { name: "Servers", ui: <ServersPage />, route: "/servers", ok: { [`GET ${API}/servers`]: [] }, fails: `GET ${API}/servers`, emptyCopy: /No MCP servers registered yet/, what: "Servers" },
  {
    name: "Tools",
    ui: <ToolsPage />,
    route: "/tools",
    ok: { [`GET ${API}/servers`]: [], [`GET ${API}/tools`]: page, [`GET ${API}/analytics/tools`]: page },
    fails: `GET ${API}/tools`,
    emptyCopy: /The tool catalog is empty/,
    what: "Tools",
  },
  {
    name: "Skills",
    ui: <SkillsPage />,
    route: "/skills",
    ok: { [`GET ${API}/skills`]: page, [`GET ${API}/skill-sources`]: [], [`GET ${API}/principals`]: [] },
    fails: `GET ${API}/skills`,
    emptyCopy: /No skills cataloged yet/,
    what: "Skills",
  },
  { name: "Skill sources", ui: <SkillSourcesPage />, route: "/skill-sources", ok: { [`GET ${API}/skill-sources`]: [] }, fails: `GET ${API}/skill-sources`, emptyCopy: /No skill sources yet/, what: "Skill sources" },
  { name: "Duplicates", ui: <DuplicatesPage />, route: "/duplicates", ok: { [`GET ${API}/dedup/suggestions`]: [] }, fails: `GET ${API}/dedup/suggestions`, emptyCopy: /No open duplicate suggestions/, what: "Duplicate suggestions" },
  { name: "Approvals", ui: <ApprovalsPage />, route: "/approvals", ok: { [`GET ${API}/approvals`]: [] }, fails: `GET ${API}/approvals`, emptyCopy: /Nothing is waiting for approval/, what: "Approvals" },
  { name: "Executions", ui: <ExecutionsPage />, route: "/executions", ok: { [`GET ${API}/executions`]: page }, fails: `GET ${API}/executions`, emptyCopy: /No tool executions recorded yet/, what: "Executions" },
  {
    name: "Policy principals",
    ui: <PolicyPage />,
    route: "/policy",
    ok: { [`GET ${API}/principals`]: [], [`GET ${API}/policy-rules`]: [], [`GET ${API}/servers`]: [], [`GET ${API}/skill-sources`]: [] },
    fails: `GET ${API}/principals`,
    emptyCopy: /No agents yet/,
    what: "Agents",
  },
  {
    name: "Policy rules",
    ui: <PolicyPage />,
    route: "/policy",
    ok: { [`GET ${API}/principals`]: [], [`GET ${API}/policy-rules`]: [], [`GET ${API}/servers`]: [], [`GET ${API}/skill-sources`]: [] },
    fails: `GET ${API}/policy-rules`,
    emptyCopy: /No allow rules/,
    what: "Policy rules",
  },
  {
    name: "Analytics",
    ui: <AnalyticsPage />,
    route: "/analytics",
    ok: { [`GET ${API}/analytics/overview`]: EMPTY_OVERVIEW, [`GET ${API}/analytics/suggestions`]: { window: WIN, wasted_exposure: [], stale_tools: [], never_routed_servers: [] }, [`GET ${API}/analytics/tools`]: page, [`GET ${API}/analytics/agents`]: { window: WIN, items: [] } },
    fails: `GET ${API}/analytics/overview`,
    emptyCopy: /No routing data in this window yet/,
    what: "Analytics",
  },
  {
    name: "Health",
    ui: <HealthPage />,
    route: "/health",
    ok: { [`GET ${API}/models/health`]: { loaded: true, mode: "balanced", device: "cpu", embedding: { backend: "x" }, decision: { backend: "y" }, memory: {} }, "GET /healthz": { status: "ok" } },
    fails: `GET ${API}/models/health`,
    emptyCopy: null,
    what: "Model health",
  },
  {
    name: "Playground tools",
    ui: <PlaygroundPage />,
    route: "/playground",
    ok: { [`GET ${API}/servers`]: [], [`GET ${API}/tools`]: page },
    fails: `GET ${API}/tools`,
    emptyCopy: /No enabled tools yet/,
    what: "Tools",
  },
  {
    name: "Playground skills",
    ui: <PlaygroundPage />,
    route: "/playground?tab=skills",
    ok: { [`GET ${API}/skills`]: page },
    fails: `GET ${API}/skills`,
    emptyCopy: /No enabled skills yet/,
    what: "Skills",
  },
];

describe("first-load failure renders an error state with Retry, never empty copy", () => {
  it.each(CASES)("$name", async ({ ui, route, ok, fails, emptyCopy, what }) => {
    const user = userEvent.setup();
    let failing = true;
    const routes = Object.fromEntries(
      Object.entries(ok).map(([k, json]) => [k, () => (k === fails && failing ? { status: 500 } : { json })]),
    );
    const { calls } = mockFetch(routes);
    renderWithProviders(ui, { route });
    const alert = await screen.findByRole("alert", { name: `${what} couldn't be loaded` });
    if (emptyCopy) expect(screen.queryByText(emptyCopy)).toBeNull();
    expect(calls.some((c) => `${c.method} ${c.path}` === fails)).toBe(true);
    failing = false;
    await user.click(within(alert).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByRole("alert", { name: `${what} couldn't be loaded` })).toBeNull());
    if (emptyCopy) expect(await screen.findByText(emptyCopy)).toBeTruthy();
  });
});

describe("drawers", () => {
  it("tool drawer: reopening on another tool starts on Details; a failed load says so instead of loading forever", async () => {
    const tool = (id: string, description: string) => ({ id, server_id: "s1", name: id, description, input_schema: {}, schema_hash: "h", tags: [], operation: "read", required_scopes: [], enabled: true, version: 1 });
    mockFetch({
      [`GET ${API}/tools/t1`]: () => ({ json: tool("t1", "Lists issues") }),
      [`GET ${API}/tools/t2`]: () => ({ json: tool("t2", "Creates issues") }),
      [`GET ${API}/tools/t3`]: () => ({ status: 500 }),
      [`GET ${API}/analytics/tools/t1`]: () => ({ status: 404 }),
    });
    const user = userEvent.setup();
    function Switcher() {
      const [id, setId] = useState<string | null>(null);
      return (
        <>
          {["t1", "t2", "t3"].map((t) => (
            <button key={t} type="button" onClick={() => setId(t)}>
              open {t}
            </button>
          ))}
          <ToolDetailDrawer toolId={id} onClose={() => setId(null)} onChanged={() => {}} />
        </>
      );
    }
    renderWithProviders(<Switcher />, { route: "/tools" });
    await user.click(screen.getByRole("button", { name: "open t1" }));
    expect(await screen.findByText("Lists issues")).toBeTruthy();
    await user.click(screen.getByRole("tab", { name: "Funnel" }));
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(await screen.findByRole("button", { name: "open t2" }));
    expect(await screen.findByText("Creates issues")).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Details" }).getAttribute("aria-selected")).toBe("true");
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(await screen.findByRole("button", { name: "open t3" }));
    expect(await screen.findByText("Tool details couldn't be loaded.")).toBeTruthy();
    expect(screen.queryByText("Creates issues")).toBeNull();
  });

  it("skill drawer: a failed load says so instead of loading forever", async () => {
    mockFetch({ [`GET ${API}/skills/k9`]: () => ({ status: 500 }) });
    renderWithProviders(<SkillDrawer skillId="k9" onClose={() => {}} />, { route: "/skills" });
    expect(await screen.findByText("Skill details couldn't be loaded.")).toBeTruthy();
    expect(screen.queryByText("Loading skill…")).toBeNull();
    // HS-U-032: the drawer offers Retry like every other failed load.
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });
});

describe("Playground Run-as picker explains an empty list", () => {
  const routes = (principals: () => { status?: number; json?: unknown }) =>
    mockFetch({
      [`GET ${API}/servers`]: () => ({ json: [] }),
      [`GET ${API}/tools`]: () => ({ json: page }),
      [`GET ${API}/tools/t1`]: () => ({ json: { id: "t1", server_id: "s1", name: "x", input_schema: { type: "object" }, schema_hash: "h", tags: [], operation: "read", required_scopes: [], enabled: true, version: 1 } }),
      [`GET ${API}/principals`]: principals,
    });

  it("when /principals fails", async () => {
    setCredentials({ adminToken: "adm" });
    routes(() => ({ status: 500 }));
    renderWithProviders(<PlaygroundPage />, { route: "/playground?tool=t1" });
    expect((await screen.findByTestId("run-as-hint")).textContent).toMatch(/agent list couldn't be loaded/);
  });

  it("when no agent is enabled", async () => {
    setCredentials({ adminToken: "adm" });
    routes(() => ({ json: [{ id: "p1", agent_id: "off-bot", enabled: false, max_tools: 8, created_at: "" }] }));
    renderWithProviders(<PlaygroundPage />, { route: "/playground?tool=t1" });
    const hint = await screen.findByTestId("run-as-hint");
    expect(hint.textContent).toMatch(/No enabled agents to run as/);
    expect(within(hint).getByRole("link", { name: "Policy" }).getAttribute("href")).toBe("/policy");
  });
});
