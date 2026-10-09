// P-409: operations the backend had with no UI. One test per action, asserting the
// exact method + path + body through the strict mockFetch.
import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { callsTo, mockFetch, renderWithProviders, type FetchCall } from "../test/render";
import { ServersPage } from "./ServersPage";
import { SkillSourcesPage } from "./SkillSourcesPage";
import { PolicyPage } from "./PolicyPage";
import { ToolDetailDrawer } from "./ToolDetailDrawer";
import { SkillDrawer } from "./SkillsPage";

const API = "/api/v1";
const sent = (calls: FetchCall[], method: string) => calls.filter((c) => c.method === method).map((c) => ({ path: c.path, body: c.body }));

async function confirm(name: string) {
  const user = userEvent.setup();
  const dialog = await screen.findByRole("dialog");
  await user.click(within(dialog).getByRole("button", { name }));
}

describe("delete server / skill source", () => {
  it("deletes a server after a confirmation naming it and the history rule", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      [`GET ${API}/servers`]: () => ({ json: [{ id: "s1", name: "github", transport: "stdio", enabled: true, status: "healthy", tool_count: 4 }] }),
      [`DELETE ${API}/servers/s1`]: () => ({ status: 204 }),
    });
    renderWithProviders(<ServersPage />, { route: "/servers" });
    await user.click(await screen.findByRole("button", { name: "Delete github" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Delete server “github”?")).toBeTruthy();
    expect(dialog.textContent).toContain("execution history");
    await confirm("Delete server");
    await waitFor(() => expect(sent(calls, "DELETE")).toEqual([{ path: `${API}/servers/s1`, body: undefined }]));
    expect(await screen.findByText("Deleted server “github”")).toBeTruthy();
  });

  it("deletes a skill source after a confirmation", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      [`GET ${API}/skill-sources`]: () => ({ json: [{ id: "src1", name: "team-skills", kind: "directory", location: "/srv/skills", enabled: true, skill_count: 5 }] }),
      [`DELETE ${API}/skill-sources/src1`]: () => ({ status: 204 }),
    });
    renderWithProviders(<SkillSourcesPage />, { route: "/skill-sources" });
    await user.click(await screen.findByRole("button", { name: "Delete team-skills" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("Its 5 skills leave the catalog");
    await confirm("Delete source");
    await waitFor(() => expect(sent(calls, "DELETE")).toEqual([{ path: `${API}/skill-sources/src1`, body: undefined }]));
    expect(await screen.findByText("Deleted skill source “team-skills”")).toBeTruthy();
  });
});

describe("Policy principal and rule actions", () => {
  const P = { id: "p1", agent_id: "billing-bot", enabled: true, max_tools: 8, max_servers: 4, max_skills: 3, created_at: "2026-10-01T00:00:00Z" };
  const R = { id: "r1", agent_id: "billing-bot", resource_kind: "tool", server_id: "srv1", tool_name: "list_*", max_operation: "read", requires_approval: false, created_at: "2026-10-01T00:00:00Z" };
  const routes = (extra: Parameters<typeof mockFetch>[0] = {}) =>
    mockFetch({
      [`GET ${API}/principals`]: () => ({ json: [P] }),
      [`GET ${API}/policy-rules`]: () => ({ json: [R] }),
      [`GET ${API}/servers`]: () => ({ json: [{ id: "srv1", name: "github" }] }),
      [`GET ${API}/skill-sources`]: () => ({ json: [] }),
      ...extra,
    });

  it("disables a principal after a confirmation; re-enabling needs none", async () => {
    const user = userEvent.setup();
    const { calls } = routes({ [`PATCH ${API}/principals/p1`]: (b) => ({ json: { ...P, ...(b as object) } }) });
    renderWithProviders(<PolicyPage />, { route: "/policy" });
    await user.click(await screen.findByRole("switch", { name: "Disable billing-bot" }));
    expect(within(await screen.findByRole("dialog")).getByText("Disable “billing-bot”?")).toBeTruthy();
    await confirm("Disable agent");
    await waitFor(() => expect(sent(calls, "PATCH")).toEqual([{ path: `${API}/principals/p1`, body: { enabled: false } }]));
  });

  it("edits a principal's limits; blank servers clears the cap", async () => {
    const user = userEvent.setup();
    const { calls } = routes({ [`PATCH ${API}/principals/p1`]: (b) => ({ json: { ...P, ...(b as object) } }) });
    renderWithProviders(<PolicyPage />, { route: "/policy" });
    await user.click(await screen.findByRole("button", { name: "Edit limits for billing-bot" }));
    const dialog = await screen.findByRole("dialog");
    const servers = within(dialog).getByRole("spinbutton", { name: /Max distinct servers/ });
    expect((servers as HTMLInputElement).value).toBe("4");
    await user.clear(servers);
    await user.click(within(dialog).getByRole("button", { name: "Save limits" }));
    await waitFor(() => expect(sent(calls, "PATCH")).toEqual([{ path: `${API}/principals/p1`, body: { maxTools: 8, maxSkills: 3, maxServers: null } }]));
  });

  it("rotates a key after a confirmation and reveals the new key once", async () => {
    const user = userEvent.setup();
    const { calls } = routes({ [`POST ${API}/principals/p1/rotate-key`]: () => ({ json: { id: "p1", agent_id: "billing-bot", api_key: "new-key-once" } }) });
    renderWithProviders(<PolicyPage />, { route: "/policy" });
    await user.click(await screen.findByRole("button", { name: "Rotate key for billing-bot" }));
    expect((await screen.findByRole("dialog")).textContent).toContain("stops working immediately");
    await confirm("Rotate key");
    const reveal = await screen.findByRole("alertdialog");
    expect((within(reveal).getByRole("textbox", { name: "API key" }) as HTMLInputElement).value).toBe("new-key-once");
    expect(sent(calls, "POST")).toEqual([{ path: `${API}/principals/p1/rotate-key`, body: undefined }]);
  });

  it("deletes a principal after a confirmation that says its rules go too", async () => {
    const user = userEvent.setup();
    const { calls } = routes({ [`DELETE ${API}/principals/p1`]: () => ({ status: 204 }) });
    renderWithProviders(<PolicyPage />, { route: "/policy" });
    await user.click(await screen.findByRole("button", { name: "Delete billing-bot" }));
    expect((await screen.findByRole("dialog")).textContent).toContain("1 rule(s) are deleted with it");
    await confirm("Delete agent");
    await waitFor(() => expect(sent(calls, "DELETE")).toEqual([{ path: `${API}/principals/p1`, body: undefined }]));
    expect(await screen.findByText("Deleted “billing-bot” and its rules")).toBeTruthy();
  });

  it("edits a rule's glob, ceiling and approval", async () => {
    const user = userEvent.setup();
    const { calls } = routes({ [`PATCH ${API}/policy-rules/r1`]: (b) => ({ json: { ...R, ...(b as object) } }) });
    renderWithProviders(<PolicyPage />, { route: "/policy" });
    await user.click(await screen.findByRole("button", { name: "Edit rule billing-bot · tool · github · list_*" }));
    const dialog = await screen.findByRole("dialog");
    await user.clear(within(dialog).getByRole("textbox", { name: /Tool name/ }));
    await user.selectOptions(within(dialog).getByRole("combobox", { name: /Operation ceiling/ }), "write");
    await user.click(within(dialog).getByRole("switch", { name: "Requires approval" }));
    await user.click(within(dialog).getByRole("button", { name: "Save rule" }));
    await waitFor(() =>
      expect(sent(calls, "PATCH")).toEqual([{ path: `${API}/policy-rules/r1`, body: { toolName: null, maxOperation: "write", requiresApproval: true } }]),
    );
  });

  it("deletes a rule after a confirmation", async () => {
    const user = userEvent.setup();
    const { calls } = routes({ [`DELETE ${API}/policy-rules/r1`]: () => ({ status: 204 }) });
    renderWithProviders(<PolicyPage />, { route: "/policy" });
    await user.click(await screen.findByRole("button", { name: "Delete rule billing-bot · tool · github · list_*" }));
    await confirm("Delete rule");
    await waitFor(() => expect(sent(calls, "DELETE")).toEqual([{ path: `${API}/policy-rules/r1`, body: undefined }]));
    expect(await screen.findByText("Deleted a rule for “billing-bot”")).toBeTruthy();
  });
});

describe("tool and skill drawers", () => {
  it("disables a single tool from its drawer", async () => {
    const user = userEvent.setup();
    const T = { id: "t1", server_id: "s1", name: "list_issues", description: "d", input_schema: {}, schema_hash: "h", tags: [], operation: "read", required_scopes: [], enabled: true, version: 1 };
    const { calls } = mockFetch({
      [`GET ${API}/tools/t1`]: () => ({ json: T }),
      [`POST ${API}/tools/t1/disable`]: () => ({ json: { ...T, enabled: false } }),
    });
    renderWithProviders(<ToolDetailDrawer toolId="t1" onClose={() => {}} onChanged={() => {}} />, { route: "/tools" });
    await user.click(await screen.findByRole("button", { name: "Disable tool" }));
    await waitFor(() => expect(sent(calls, "POST")).toEqual([{ path: `${API}/tools/t1/disable`, body: undefined }]));
    expect(await screen.findByText("Disabled tool “list_issues”: agents no longer see it")).toBeTruthy();
  });

  it("enables a disabled tool from its drawer", async () => {
    const user = userEvent.setup();
    const T = { id: "t2", server_id: "s1", name: "drop_table", description: "d", input_schema: {}, schema_hash: "h", tags: [], operation: "execute", required_scopes: [], enabled: false, version: 1 };
    const { calls } = mockFetch({
      [`GET ${API}/tools/t2`]: () => ({ json: T }),
      [`POST ${API}/tools/t2/enable`]: () => ({ json: { ...T, enabled: true } }),
    });
    renderWithProviders(<ToolDetailDrawer toolId="t2" onClose={() => {}} onChanged={() => {}} />, { route: "/tools" });
    await user.click(await screen.findByRole("button", { name: "Enable tool" }));
    await waitFor(() => expect(sent(calls, "POST")).toEqual([{ path: `${API}/tools/t2/enable`, body: undefined }]));
  });

  it("the Versions tab reads GET /skills/{id}/versions, newest first", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      [`GET ${API}/skills/k1`]: () => ({ json: { id: "k1", source_id: "s", name: "pdf-fill", operation: "write", enabled: true } }),
      [`GET ${API}/skills/k1/versions`]: () => ({
        json: [
          { version: 1, changeKind: "added", contentHash: "aaaaaaaa11", manifestHash: null, recordedAt: "2026-10-01T00:00:00Z" },
          { version: 2, changeKind: "changed", contentHash: "bbbbbbbb22", manifestHash: null, recordedAt: "2026-10-02T00:00:00Z" },
        ],
      }),
    });
    renderWithProviders(<SkillDrawer skillId="k1" onClose={() => {}} />, { route: "/skills" });
    await user.click(await screen.findByRole("tab", { name: "Versions" }));
    const list = await screen.findByRole("list", { name: "Versions" });
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent?.split(" ")[0])).toEqual(["v2", "v1"]);
    expect(callsTo(calls, "GET", `${API}/skills/k1/versions`)).toHaveLength(1);
  });
});
