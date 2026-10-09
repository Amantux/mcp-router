import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expectQuery, mockFetch, renderWithProviders } from "../test/render";
import { ToolsPage } from "./ToolsPage";

const TOOL = {
  id: "t1",
  server_id: "s1",
  server_name: "github",
  name: "list_issues",
  description: "List issues in a repository",
  input_schema: { type: "object" },
  schema_hash: "h1",
  tags: [],
  operation: "read",
  required_scopes: [],
  enabled: true,
  version: 1,
  call_count: 3,
};

function toolsRoutes(extra: Parameters<typeof mockFetch>[0] = {}) {
  return mockFetch({
    "GET /api/v1/servers": () => ({ json: [{ id: "s1", name: "github" }] }),
    "GET /api/v1/tools": () => ({ json: { items: [TOOL], total: 1, limit: 50, offset: 0 } }),
    "GET /api/v1/analytics/tools": () => ({ json: { items: [], total: 0 } }),
    "GET /api/v1/tools/t1": () => ({ json: TOOL }),
    ...extra,
  });
}

describe("ToolsPage", () => {
  it("opens a tool row from the keyboard with Enter and with Space", async () => {
    const user = userEvent.setup();
    toolsRoutes();
    renderWithProviders(<ToolsPage />, { route: "/tools" });
    const row = (await screen.findByText("list_issues")).closest('[role="row"]') as HTMLElement;
    row.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("link", { name: /Try in playground/ })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("link", { name: /Try in playground/ })).toBeNull());
    const again = (await screen.findByText("list_issues")).closest('[role="row"]') as HTMLElement;
    again.focus();
    await user.keyboard(" ");
    expect(await screen.findByRole("link", { name: /Try in playground/ })).toBeTruthy();
  });

  it("loads the catalog, shows the 7-day funnel column, and counts the total", async () => {
    toolsRoutes({
      "GET /api/v1/tools": () => ({ json: { items: [TOOL], total: 120, limit: 50, offset: 0 } }),
      "GET /api/v1/analytics/tools": () => ({ json: { items: [{ tool_id: "t1", tool_name: "list_issues", surfaced: 40, selected: 10, succeeded: 9, failed: 1 }], total: 1 } }),
    });
    renderWithProviders(<ToolsPage />, { route: "/tools" });
    expect(await screen.findByText("120 catalogued")).toBeTruthy();
    expect(await screen.findByRole("img", { name: "list_issues funnel: surfaced 40, selected 10, succeeded 9" })).toBeTruthy();
    expect(within(screen.getByRole("grid", { name: "Tools" })).getByText("github")).toBeTruthy();
  });

  it("sends every filter under the backend's query names, debounces search, and clears", async () => {
    const user = userEvent.setup();
    const { calls } = toolsRoutes();
    renderWithProviders(<ToolsPage />, { route: "/tools" });
    await screen.findByText("list_issues");
    expectQuery(calls, "GET", "/api/v1/tools", { limit: "50", offset: "0" });
    await user.type(screen.getByRole("searchbox"), "issue");
    await user.selectOptions(screen.getByRole("combobox", { name: "Domain" }), "development");
    await user.selectOptions(screen.getByRole("combobox", { name: "Operation" }), "write");
    await user.selectOptions(screen.getByRole("combobox", { name: "Server" }), "s1");
    await user.selectOptions(screen.getByRole("combobox", { name: "Enabled" }), "false");
    await user.selectOptions(screen.getByRole("combobox", { name: "Availability" }), "true");
    await waitFor(() =>
      expectQuery(calls, "GET", "/api/v1/tools", { q: "issue", domain: "development", operation: "write", serverId: "s1", enabled: "false", available: "true", limit: "50", offset: "0" }),
    );
    // Debounced: typing five characters did not issue five searches.
    expect(calls.filter((c) => c.path === "/api/v1/tools" && c.query.q && c.query.q !== "issue")).toHaveLength(0);
    await user.click(screen.getAllByRole("button", { name: "Clear filters" })[0]);
    await waitFor(() => expectQuery(calls, "GET", "/api/v1/tools", { limit: "50", offset: "0" }));
  });

  it("filtered-empty and first-run-empty say different things", async () => {
    const user = userEvent.setup();
    toolsRoutes({ "GET /api/v1/tools": () => ({ json: { items: [], total: 0, limit: 50, offset: 0 } }) });
    renderWithProviders(<ToolsPage />, { route: "/tools" });
    expect(await screen.findByText("The tool catalog is empty")).toBeTruthy();
    await user.selectOptions(screen.getByRole("combobox", { name: "Operation" }), "write");
    expect(await screen.findByText("No tools match these filters")).toBeTruthy();
  });

  it("pages with offset", async () => {
    const user = userEvent.setup();
    const { calls } = toolsRoutes({ "GET /api/v1/tools": (_b, call) => ({ json: { items: [TOOL], total: 75, limit: 50, offset: Number(call.query.offset ?? 0) } }) });
    renderWithProviders(<ToolsPage />, { route: "/tools" });
    await screen.findByText("list_issues");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expectQuery(calls, "GET", "/api/v1/tools", { limit: "50", offset: "50" }));
  });
});

