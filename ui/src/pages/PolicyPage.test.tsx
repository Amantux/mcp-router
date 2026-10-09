import { describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
import { PolicyPage } from "./PolicyPage";

const PRINCIPAL = { id: "p1", agent_id: "billing-bot", enabled: true, max_tools: 8, max_servers: null, max_skills: 3, created_at: "2026-10-01T00:00:00Z" };
const TOOL_RULE = { id: "r1", agent_id: "billing-bot", resource_kind: "tool", server_id: "srv1", tool_name: "list_*", max_operation: "read", requires_approval: false, created_at: "2026-10-01T00:00:00Z" };
const SKILL_RULE = { id: "r2", agent_id: "billing-bot", resource_kind: "skill", server_id: "src1", tool_name: null, max_operation: "read", requires_approval: true, created_at: "2026-10-01T00:00:00Z" };

function routes(extra: Parameters<typeof mockFetch>[0] = {}) {
  return mockFetch({
    "GET /api/v1/principals": () => ({ json: [PRINCIPAL] }),
    "GET /api/v1/policy-rules": () => ({ json: [TOOL_RULE, SKILL_RULE] }),
    "GET /api/v1/servers": () => ({ json: [{ id: "srv1", name: "github" }] }),
    "GET /api/v1/skill-sources": () => ({ json: [{ id: "src1", name: "team-skills" }] }),
    ...extra,
  });
}

describe("PolicyPage", () => {
  it("renders rule kinds with the server or skill-source name, and principal limits", async () => {
    routes();
    renderWithProviders(<PolicyPage />, { route: "/policy" });
    const rules = await screen.findByRole("table", { name: "Rules" });
    await waitFor(() => expect(within(rules).getByText("team-skills")).toBeTruthy());
    const [, toolRow, skillRow] = within(rules).getAllByRole("row");
    expect(within(toolRow).getByText("tool")).toBeTruthy();
    expect(within(toolRow).getByText("github")).toBeTruthy();
    expect(within(skillRow).getByText("skill")).toBeTruthy();
    expect(within(skillRow).getByText("team-skills")).toBeTruthy();
    const principals = screen.getByRole("table", { name: "Principals" });
    const row = within(principals).getAllByRole("row")[1];
    expect(within(row).getByText("no limit")).toBeTruthy(); // maxServers null
    expect(within(row).getByText("3")).toBeTruthy(); // maxSkills
  });

  it("adds a skill rule against a skill source", async () => {
    const user = userEvent.setup();
    const { calls } = routes({ "POST /api/v1/policy-rules": (b) => ({ json: { ...SKILL_RULE, ...(b as object), id: "r3" } }) });
    renderWithProviders(<PolicyPage />, { route: "/policy" });
    const form = await screen.findByRole("form", { name: "Add rule" });
    await within(form).findByRole("option", { name: "billing-bot" });
    await user.selectOptions(within(form).getByRole("combobox", { name: /Agent/ }), "billing-bot");
    await user.selectOptions(within(form).getByRole("combobox", { name: "Kind" }), "skill");
    await within(form).findByRole("option", { name: "team-skills" });
    await user.selectOptions(within(form).getByRole("combobox", { name: "Skill source" }), "src1");
    await user.click(within(form).getByRole("button", { name: "Add rule" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      agentId: "billing-bot",
      resourceKind: "skill",
      serverId: "src1",
      toolName: null,
      maxOperation: "read",
      requiresApproval: false,
    }));
  });

  it("creates a principal with skill and server caps", async () => {
    const user = userEvent.setup();
    const { calls } = routes({ "POST /api/v1/principals": (b) => ({ json: { ...PRINCIPAL, ...(b as object), id: "p2", api_key: "key-once" } }) });
    renderWithProviders(<PolicyPage />, { route: "/policy" });
    await screen.findByRole("table", { name: "Principals" });
    await user.click(screen.getAllByRole("button", { name: "Create principal" })[0]);
    const dlg = await screen.findByRole("dialog");
    await user.type(within(dlg).getByRole("textbox", { name: /Agent id/ }), "triage-bot");
    await user.type(within(dlg).getByRole("spinbutton", { name: /Max distinct servers/ }), "2");
    await user.click(within(dlg).getByRole("button", { name: "Create principal" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ agentId: "triage-bot", maxTools: 8, maxServers: 2, maxSkills: 3 }));
    expect(await screen.findByRole("alertdialog")).toBeTruthy(); // the one-time key reveal
  });

  it("first run: empty principals explain deny-by-default; Add rule refuses without an agent", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      "GET /api/v1/principals": () => ({ json: [] }),
      "GET /api/v1/policy-rules": () => ({ json: [] }),
      "GET /api/v1/servers": () => ({ json: [] }),
      "GET /api/v1/skill-sources": () => ({ json: [] }),
    });
    renderWithProviders(<PolicyPage />, { route: "/policy" });
    expect(await screen.findByText("No agent principals yet")).toBeTruthy();
    expect(screen.getByText(/No allow rules — every agent is currently denied all tools/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    expect(await screen.findByText("Choose the agent this rule allows.")).toBeTruthy();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("reveals the new key once, copies it, and forgets it on close", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    routes({ "POST /api/v1/principals": (b) => ({ json: { ...PRINCIPAL, ...(b as object), id: "p2", api_key: "key-once" } }) });
    renderWithProviders(<PolicyPage />, { route: "/policy" });
    await screen.findByRole("table", { name: "Principals" });
    await user.click(screen.getAllByRole("button", { name: "Create principal" })[0]);
    const dlg = await screen.findByRole("dialog");
    await user.type(within(dlg).getByRole("textbox", { name: /Agent id/ }), "triage-bot{Enter}");
    const reveal = await screen.findByRole("alertdialog");
    expect(within(reveal).getByText("API key for “triage-bot”")).toBeTruthy();
    expect((within(reveal).getByRole("textbox", { name: "API key" }) as HTMLInputElement).value).toBe("key-once");
    await user.click(within(reveal).getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith("key-once");
    expect(await within(reveal).findByRole("button", { name: "Copied" })).toBeTruthy();
    await user.click(within(reveal).getByRole("button", { name: "I've stored the key" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(screen.queryByDisplayValue("key-once")).toBeNull();
  });
});

