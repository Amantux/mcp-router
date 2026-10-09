import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
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

function toolsRoutes(extra: Record<string, () => { status?: number; json?: unknown }> = {}) {
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
});
