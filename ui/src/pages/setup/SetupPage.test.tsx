import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { mockFetch, renderWithProviders, type FetchCall } from "../../test/render";
import { clearCredentials, setCredentials } from "../../api/auth";
import { SetupPage } from "./SetupPage";
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
