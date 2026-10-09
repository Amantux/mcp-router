import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { mockFetch, renderWithProviders, type FetchCall } from "../../test/render";
import { clearCredentials, setCredentials } from "../../api/auth";
import { createAgentFailure, FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN, SetupPage, sourceName } from "./SetupPage";
import { ApiError } from "../../api/client";
import { STEP_KEY } from "./redirect";

const ADMIN = "adm-SECRET-token-123";
const STATUS = { needsSetup: true, completedAt: null, hasAdminToken: true, devMode: false, backend: "laya", counts: { principals: 0, servers: 0, tools: 0, skillSources: 0 } };
let calls: FetchCall[] = [];

function routes(extra: Parameters<typeof mockFetch>[0] = {}) {
  ({ calls } = mockFetch({
    "GET /api/v1/setup/status": () => ({ json: STATUS }),
    "GET /api/v1/servers": () => ({ json: [{ id: "srv1", name: "github" }] }),
    "GET /api/v1/skill-sources": () => ({ json: [{ id: "src1", name: "team-skills" }] }),
    "POST /api/v1/principals": (b) => ({ json: { ...(b as object), id: "p1", apiKey: "agent-key-once" } }),
    "POST /api/v1/policy-rules": (b) => ({ json: { ...(b as object), id: "r1", createdAt: "now" } }),
    "POST /api/v1/skill-sources": (b) => ({ json: { ...(b as object), id: "src2" } }),
    "POST /api/v1/servers": (b) => ({ json: { ...(b as object), id: "srv2" } }),
    ...extra,
  }));
}
const posts = (path: string) => calls.filter((c) => c.method === "POST" && c.url.endsWith(path));
const goTo = (label: string) => fireEvent.click(screen.getByRole("tab", { name: new RegExp(label) }));

it("derives a backend-valid skill source name from a path", () => {
  expect(sourceName("/srv/My Skills")).toBe("My-Skills");
  expect(sourceName("/opt/_x")).toBe("x");
  expect(sourceName("/")).toBe("skills");
});

describe("SetupPage wizard", () => {
  beforeEach(() => {
    sessionStorage.clear();
    setCredentials({ adminToken: ADMIN });
    routes();
  });
  afterEach(() => {
    // No admin token in any request body, ever (it belongs in the header only).
    for (const c of calls) expect(JSON.stringify(c.body ?? "")).not.toContain(ADMIN);
    clearCredentials();
  });

  it("moves forward/back and resumes the persisted step after a remount", async () => {
    renderWithProviders(<SetupPage />, { route: "/setup" });
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    expect(sessionStorage.getItem(STEP_KEY)).toBe("2");
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(sessionStorage.getItem(STEP_KEY)).toBe("1");
    cleanup();
    renderWithProviders(<SetupPage />, { route: "/setup" });
    expect(screen.getByRole("tab", { name: /Decision backend/ }).getAttribute("aria-selected")).toBe("true");
  });

  it("shows the agent key once, then posts read-only starter rules per selected target", async () => {
    renderWithProviders(<SetupPage />, { route: "/setup" });
    goTo("Agent identity");
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    expect((await screen.findByTestId("api-key")).textContent).toBe("agent-key-once");
    expect((screen.getByLabelText("Agent id") as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(await screen.findByLabelText("Server: github"));
    fireEvent.click(screen.getByLabelText("Skill source: team-skills"));
    fireEvent.click(screen.getByRole("button", { name: "Create rules" }));
    expect(await screen.findByText("Created 2 rule(s).")).toBeTruthy();
    expect(posts("/policy-rules").map((c) => c.body)).toEqual([
      { agentId: "my-agent", resourceKind: "tool", serverId: "srv1", toolName: null, maxOperation: "read", requiresApproval: false },
      { agentId: "my-agent", resourceKind: "skill", serverId: "src1", toolName: null, maxOperation: "read", requiresApproval: false },
    ]);
    cleanup(); // remount: the key lived in memory only
    renderWithProviders(<SetupPage />, { route: "/setup" });
    expect(screen.queryByTestId("api-key")).toBeNull();
    expect(JSON.stringify(sessionStorage)).not.toContain("agent-key-once");
  });

  it("creates no rules when the admin chooses to configure them later", async () => {
    renderWithProviders(<SetupPage />, { route: "/setup" });
    goTo("Agent identity");
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    await screen.findByTestId("api-key");
    fireEvent.click(screen.getByLabelText("I'll configure rules later"));
    expect(screen.queryByRole("button", { name: "Create rules" })).toBeNull();
    expect(posts("/policy-rules")).toHaveLength(0);
  });

  it("refuses http:// in Add one server, posts an https server", async () => {
    renderWithProviders(<SetupPage />, { route: "/setup" });
    goTo("MCP servers");
    fireEvent.click(screen.getByRole("button", { name: "Add one server" }));
    const dlg = await screen.findByRole("dialog");
    fireEvent.change(within(dlg).getByLabelText(/Name/), { target: { value: "remote" } });
    fireEvent.click(within(dlg).getByLabelText("Streamable HTTP"));
    const url = within(dlg).getByLabelText(/URL/);
    fireEvent.change(url, { target: { value: "http://mcp.example.com/mcp" } });
    fireEvent.click(within(dlg).getByRole("button", { name: /Register/ }));
    expect(await within(dlg).findByText(/Only https:\/\/ URLs are accepted/)).toBeTruthy();
    expect(posts("/servers")).toHaveLength(0);
    fireEvent.change(url, { target: { value: "https://mcp.example.com/mcp" } });
    fireEvent.click(within(dlg).getByRole("button", { name: /Register/ }));
    await waitFor(() => expect(posts("/servers")).toHaveLength(1));
    expect(posts("/servers")[0].body).toEqual({ name: "remote", transport: "streamable-http", endpoint: "https://mcp.example.com/mcp" });
  });

  it("posts a directory skill source for an absolute path and refuses a relative one", async () => {
    renderWithProviders(<SetupPage />, { route: "/setup" });
    goTo("Skill sources");
    const path = screen.getByLabelText("Directory path");
    fireEvent.change(path, { target: { value: "skills/local" } });
    fireEvent.click(screen.getByRole("button", { name: "Add directory source" }));
    expect(await screen.findByText(/absolute directory path/)).toBeTruthy();
    fireEvent.change(path, { target: { value: "/srv/team-skills" } });
    fireEvent.click(screen.getByRole("button", { name: "Add directory source" }));
    await waitFor(() => expect(posts("/skill-sources")).toHaveLength(1));
    expect(posts("/skill-sources")[0].body).toEqual({ name: "team-skills", kind: "directory", location: "/srv/team-skills" });
  });
});

describe("SetupPage messages", () => {
  beforeEach(() => {
    sessionStorage.clear();
    setCredentials({ adminToken: ADMIN });
  });
  afterEach(() => clearCredentials());

  it("a 409 on a skill source reads as curated advice, never `HTTP 409 from /api/…`, and clears on step change", async () => {
    routes({ "POST /api/v1/skill-sources": () => ({ status: 409 }) });
    renderWithProviders(<SetupPage />, { route: "/setup" });
    goTo("Skill sources");
    fireEvent.change(screen.getByLabelText("Directory path"), { target: { value: "/srv/team-skills" } });
    fireEvent.click(screen.getByRole("button", { name: "Add directory source" }));
    const msg = await screen.findByRole("status");
    expect(msg.textContent).toBe("Add directory source failed (HTTP 409). It conflicts with existing data (for example a duplicate name). Change the input and retry.");
    expect(msg.textContent).not.toContain("/api/");
    goTo("Agent identity");
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("dev mode without an admin token: creating the first agent shows the backend's 409 guidance (D11)", async () => {
    routes({
      "GET /api/v1/setup/status": () => ({ json: { ...STATUS, hasAdminToken: false, devMode: true } }),
      "POST /api/v1/principals": () => ({ status: 409 }),
    });
    renderWithProviders(<SetupPage />, { route: "/setup" });
    await screen.findByText("Admin token: not configured");
    goTo("Agent identity");
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    expect((await screen.findByRole("status")).textContent).toBe("Set MCPR_ADMIN_TOKEN before creating the first principal; creating one ends dev mode.");
  });

  it("a Create agent 409 follows the backend's curated detail, and names both causes when nothing says which (HS-U-019)", () => {
    const e409 = (detail?: string) => new ApiError(409, "/api/v1/principals", "admin", detail);
    const known = { ...STATUS, hasAdminToken: true };
    // The curated detail wins over a stale or missing status.
    expect(createAgentFailure("a", e409(FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN), known)).toBe(FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN);
    expect(createAgentFailure("a", e409("agentId already exists"), null)).toBe("An agent “a” already exists. Choose another id.");
    // No detail and no status: both explanations, not a guess.
    const both = createAgentFailure("a", e409(), null);
    expect(both).toContain("already exists");
    expect(both).toContain(FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN);
  });

  it("the health check reports unloaded models instead of a vacuous ok", async () => {
    const backend = (loaded: boolean) => ({ backend: loaded ? "x" : null });
    routes({
      "GET /api/v1/models/health": () => ({ json: { loaded: true, mode: "balanced", device: "cpu", embedding: backend(true), decision: backend(false), memory: {} } }),
    });
    renderWithProviders(<SetupPage />, { route: "/setup" });
    goTo("Verify");
    fireEvent.click(screen.getByRole("button", { name: "Run checks" }));
    expect(await screen.findByText("models not loaded: decision; servers 0, tools 0")).toBeTruthy();
  });

  it("a refused clipboard says so instead of an unhandled rejection", async () => {
    routes();
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: () => Promise.reject(new Error("insecure origin")) } });
    renderWithProviders(<SetupPage />, { route: "/setup" });
    fireEvent.click(screen.getByRole("button", { name: "Copy suggestion" }));
    expect((await screen.findByRole("status")).textContent).toMatch(/Couldn't copy to the clipboard/);
  });
});

describe("SetupPage import and finish", () => {
  beforeEach(() => {
    sessionStorage.clear();
    setCredentials({ adminToken: ADMIN });
  });
  afterEach(() => clearCredentials());

  it("posts a pasted Claude Desktop config to the import endpoint as-is, and refuses invalid JSON", async () => {
    routes({ "POST /api/v1/servers/import": (b) => ({ json: { imported: Object.keys((b as { mcpServers: object }).mcpServers) } }) });
    renderWithProviders(<SetupPage />, { route: "/setup" });
    goTo("MCP servers");
    const box = screen.getByRole("textbox", { name: "mcpServers JSON" });
    fireEvent.change(box, { target: { value: "{not json" } });
    fireEvent.click(screen.getByRole("button", { name: "Import servers" }));
    expect((await screen.findByRole("status")).textContent).toBe("That is not valid JSON.");
    const cfg = { mcpServers: { files: { command: "uvx", args: ["mcp-files"] } } };
    fireEvent.change(box, { target: { value: JSON.stringify(cfg) } });
    fireEvent.click(screen.getByRole("button", { name: "Import servers" }));
    expect(await screen.findByText("Servers imported.")).toBeTruthy();
    expect(posts("/servers/import").map((c) => c.body)).toEqual([cfg]);
  });

  it("Finish setup marks setup complete", async () => {
    routes({ "POST /api/v1/setup/complete": () => ({ json: { completed: true, completedAt: "2026-10-09T00:00:00Z" } }) });
    renderWithProviders(<SetupPage />, { route: "/setup" });
    goTo("Verify");
    fireEvent.click(screen.getByRole("button", { name: "Finish setup" }));
    expect(await screen.findByText("Setup complete.")).toBeTruthy();
    expect(posts("/setup/complete")).toHaveLength(1);
  });
});

