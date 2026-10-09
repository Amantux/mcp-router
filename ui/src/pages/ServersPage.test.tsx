import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { callsTo, mockFetch, renderWithProviders } from "../test/render";
import { ServersPage } from "./ServersPage";

const GH = { id: "s1", name: "github", transport: "stdio", env_names: [], enabled: true, status: "healthy", tool_count: 12, last_discovered_at: "2026-10-01T00:00:00Z" };
const WEB = { id: "s2", name: "web", transport: "streamable-http", endpoint: "https://mcp.example.com/mcp", enabled: false, status: "offline", tool_count: 3 };

function routes(extra: Parameters<typeof mockFetch>[0] = {}) {
  return mockFetch({ "GET /api/v1/servers": () => ({ json: [GH, WEB] }), ...extra });
}

describe("ServersPage", () => {
  it("lists servers with transport, endpoint, status and tool count", async () => {
    routes();
    renderWithProviders(<ServersPage />, { route: "/servers" });
    const table = await screen.findByRole("table", { name: "Servers" });
    const [, gh, web] = within(table).getAllByRole("row");
    expect(within(gh).getByText("local process")).toBeTruthy();
    expect(within(gh).getByText("healthy")).toBeTruthy();
    expect(within(gh).getByText("12")).toBeTruthy();
    expect(within(web).getByText("https://mcp.example.com/mcp")).toBeTruthy();
    expect(within(web).getByText("offline")).toBeTruthy();
    expect(screen.getByText("2 registered")).toBeTruthy();
  });

  it("shows the empty state with a register action when there are no servers", async () => {
    mockFetch({ "GET /api/v1/servers": () => ({ json: [] }) });
    renderWithProviders(<ServersPage />, { route: "/servers" });
    expect(await screen.findByText("No MCP servers registered yet")).toBeTruthy();
  });

  it("refreshes a server and reports its new tool count", async () => {
    const user = userEvent.setup();
    const { calls } = routes({ "POST /api/v1/servers/s1/refresh": () => ({ json: { server: { ...GH, tool_count: 14 }, added: ["x", "y"] } }) });
    renderWithProviders(<ServersPage />, { route: "/servers" });
    const table = await screen.findByRole("table", { name: "Servers" });
    await user.click(within(within(table).getAllByRole("row")[1]).getByRole("button", { name: "Refresh" }));
    expect(await screen.findByText("Refreshed “github” — 14 tools")).toBeTruthy();
    expect(callsTo(calls, "POST", "/api/v1/servers/s1/refresh")).toHaveLength(1);
  });

  it("disabling asks first (naming the server and the consequence); cancelling sends nothing", async () => {
    const user = userEvent.setup();
    const { calls } = routes({ "PATCH /api/v1/servers/s1": (b) => ({ json: { ...GH, ...(b as object) } }) });
    renderWithProviders(<ServersPage />, { route: "/servers" });
    await user.click(await screen.findByRole("switch", { name: "Disable github" }));
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Disable server “github”?")).toBeTruthy();
    expect(dialog.textContent).toContain("Its 12 tools will stop being routed or exposed to agents");
    expect(dialog.textContent).toContain("Nothing is deleted");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(callsTo(calls, "PATCH", "/api/v1/servers/s1")).toHaveLength(0);

    await user.click(await screen.findByRole("switch", { name: "Disable github" }));
    dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Disable server" }));
    await waitFor(() => expect(callsTo(calls, "PATCH", "/api/v1/servers/s1").map((c) => c.body)).toEqual([{ enabled: false }]));
    expect(await screen.findByText("Disabled server “github”")).toBeTruthy();
  });

  it("enabling needs no confirmation", async () => {
    const user = userEvent.setup();
    const { calls } = routes({ "PATCH /api/v1/servers/s2": (b) => ({ json: { ...WEB, ...(b as object) } }) });
    renderWithProviders(<ServersPage />, { route: "/servers" });
    await user.click(await screen.findByRole("switch", { name: "Enable web" }));
    await waitFor(() => expect(callsTo(calls, "PATCH", "/api/v1/servers/s2").map((c) => c.body)).toEqual([{ enabled: true }]));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("registers a stdio server from the dialog with one argv entry per line, then reloads the list", async () => {
    const user = userEvent.setup();
    const { calls } = routes({ "POST /api/v1/servers": (b) => ({ json: { ...GH, id: "s3", ...(b as object) } }) });
    renderWithProviders(<ServersPage />, { route: "/servers" });
    await screen.findByRole("table", { name: "Servers" });
    await user.click(screen.getAllByRole("button", { name: /Register server/ })[0]);
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: /Name/ }), "files");
    await user.type(within(dialog).getByRole("textbox", { name: /Command/ }), "uvx");
    await user.type(within(dialog).getByRole("textbox", { name: /Arguments/ }), "mcp-files{Enter}--root /srv/data{Enter}");
    await user.click(within(dialog).getByRole("button", { name: "Register server" }));
    await waitFor(() => expect(callsTo(calls, "POST", "/api/v1/servers").map((c) => c.body)).toEqual([{ name: "files", transport: "stdio", command: ["uvx", "mcp-files", "--root /srv/data"] }]));
    await waitFor(() => expect(callsTo(calls, "GET", "/api/v1/servers").length).toBeGreaterThan(1));
  });
});
