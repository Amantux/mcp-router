import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
import { setCredentials } from "../api/auth";
import { ExecutionOutcomeView, PlaygroundPage } from "./PlaygroundPage";

const TOOL = {
  id: "t1",
  server_id: "s1",
  server_name: "github",
  name: "list_issues",
  description: "List issues in a repository",
  input_schema: {
    type: "object",
    required: ["repo"],
    additionalProperties: false,
    properties: { repo: { type: "string" }, limit: { type: "integer", maximum: 100 } },
  },
  schema_hash: "h1",
  tags: [],
  operation: "read",
  required_scopes: [],
  enabled: true,
  version: 1,
};

function mockTool(tool: Record<string, unknown>, execute: (body: unknown) => { status?: number; json?: unknown }) {
  return mockFetch({
    "GET /api/v1/servers": () => ({ json: [{ id: "s1", name: "github" }] }),
    "GET /api/v1/tools": () => ({ json: { items: [tool], total: 1 } }),
    [`GET /api/v1/tools/${tool.id}`]: () => ({ json: tool }),
    [`POST /api/v1/tools/${tool.id}/execute`]: execute,
    "GET /api/v1/me": () => ({ json: { id: "p1", agent_id: "billing-bot", enabled: true, max_tools: 8 } }),
    "GET /api/v1/principals": () => ({ json: [{ id: "p1", agent_id: "billing-bot", enabled: true, max_tools: 8, created_at: "2026-01-01T00:00:00Z" }] }),
  });
}

describe("PlaygroundPage", () => {
  it("generates the form, runs as the agent, and shows a denial with its policy reason, audit id and latency", async () => {
    const user = userEvent.setup();
    setCredentials({ adminToken: "adm", agentKey: "agt" });
    const { calls } = mockTool(TOOL, () => ({
      json: { status: "denied", detail: "no matching policy rule", record_id: "rec-42", errors: [], latency_ms: 3.2 },
    }));
    renderWithProviders(<PlaygroundPage />, { route: "/playground?tool=t1" });
    await waitFor(() => expect(screen.getByTestId("run-as").textContent).toContain("agent “billing-bot”"));
    await user.type(screen.getByRole("textbox", { name: /repo/ }), "acme/api");
    await user.click(screen.getByRole("button", { name: "Run tool" }));

    const outcome = await screen.findByTestId("outcome-denied");
    expect(within(outcome).getByText("Denied by policy")).toBeTruthy();
    expect(within(outcome).getByTestId("outcome-detail").textContent).toBe("Reason: no matching policy rule");
    expect(within(outcome).getByTestId("audit-id").textContent).toBe("rec-42");
    expect(within(outcome).getByText("3.2 ms")).toBeTruthy();
    const exec = calls.find((c) => c.method === "POST")!;
    expect(exec.body).toEqual({ arguments: { repo: "acme/api" } });
    expect(exec.headers.authorization).toBe("Bearer agt");
  });

  it("blocks Run on client validation errors without calling the backend", async () => {
    const user = userEvent.setup();
    const { calls } = mockTool(TOOL, () => ({ json: { status: "ok" } }));
    renderWithProviders(<PlaygroundPage />, { route: "/playground?tool=t1" });
    await screen.findByRole("textbox", { name: /repo/ });
    await user.click(screen.getByRole("button", { name: "Run tool" }));
    expect(screen.getByText("Required.")).toBeTruthy();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("maps backend invalid_args errors onto the form fields", async () => {
    const user = userEvent.setup();
    mockTool(TOOL, () => ({ json: { status: "invalid_args", detail: "schema validation failed", record_id: "r1", errors: ["$.limit: maximum", "$: additionalProperties"] } }));
    renderWithProviders(<PlaygroundPage />, { route: "/playground?tool=t1" });
    await user.type(await screen.findByRole("textbox", { name: /repo/ }), "a/b");
    await user.type(screen.getByRole("textbox", { name: /limit/ }), "50");
    await user.click(screen.getByRole("button", { name: "Run tool" }));
    await screen.findByTestId("outcome-invalid_args");
    expect(screen.getByText("Above the maximum.")).toBeTruthy();
    expect(screen.getByText("Arguments: Contains a field the tool doesn't accept.")).toBeTruthy();
  });

  it("admin-only sessions must pick an agent; the confirmation and POST carry the impersonation", async () => {
    const user = userEvent.setup();
    setCredentials({ adminToken: "adm" });
    const { calls } = mockTool({ ...TOOL, name: "create_issue", operation: "write" }, () => ({ json: { status: "ok", detail: "ok", record_id: "r2", result: { content: [{ type: "text", text: '{"number":7}' }], is_error: false } } }));
    renderWithProviders(<PlaygroundPage />, { route: "/playground?tool=t1" });
    await user.type(await screen.findByRole("textbox", { name: /repo/ }), "a/b");
    // The backend refuses admin runs with no agent named, so Run stays disabled until one is picked.
    expect((screen.getByRole("button", { name: "Run tool" }) as HTMLButtonElement).disabled).toBe(true);
    // The principals list loads asynchronously: wait for the option itself, not just the picker.
    await screen.findByRole("option", { name: "billing-bot" });
    await user.selectOptions(screen.getByTestId("run-as-picker"), "billing-bot");
    await user.click(screen.getByRole("button", { name: "Run tool" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Run create_issue as agent “billing-bot” (admin-initiated)?")).toBeTruthy();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    // Fluent keeps the surface aria-hidden during its open transition; wait it out.
    await user.click(await within(dialog).findByRole("button", { name: "Run tool" }, { timeout: 3000 }));
    const ok = await screen.findByTestId("outcome-ok");
    expect(within(ok).getByText("Ran create_issue")).toBeTruthy();
    expect(within(ok).getByLabelText("Result block 1").textContent).toBe('{\n  "number": 7\n}');
    const exec = calls.find((c) => c.method === "POST")!;
    expect((exec.body as Record<string, unknown>).agentId).toBe("billing-bot");
  });

  it("raw JSON toggle: form→JSON always works; unrepresentable JSON stays raw with a notice", async () => {
    const user = userEvent.setup();
    mockTool(TOOL, () => ({ json: { status: "ok" } }));
    renderWithProviders(<PlaygroundPage />, { route: "/playground?tool=t1" });
    await user.type(await screen.findByRole("textbox", { name: /repo/ }), "acme/api");
    await user.click(screen.getByRole("tab", { name: "Raw JSON" }));
    const json = screen.getByRole("textbox", { name: /Arguments \(JSON\)/ }) as HTMLTextAreaElement;
    expect(JSON.parse(json.value)).toEqual({ repo: "acme/api" });

    await user.clear(json);
    await user.click(json);
    await user.paste('{"repo":"x/y","token":"t"}');
    await user.click(screen.getByRole("tab", { name: "Form" }));
    expect(screen.getByTestId("raw-notice").textContent).toContain('"token" is not a field in the form');
    expect(screen.getByRole("textbox", { name: /Arguments \(JSON\)/ })).toBeTruthy();

    await user.clear(json);
    await user.paste('{"repo":"x/y","limit":5}');
    await user.click(screen.getByRole("tab", { name: "Form" }));
    expect((screen.getByRole("textbox", { name: /limit/ }) as HTMLInputElement).value).toBe("5");
  });

  it("falls back to raw JSON only when the schema root can't be a form", async () => {
    mockTool({ ...TOOL, input_schema: { oneOf: [{ type: "object" }, { type: "string" }] } }, () => ({ json: { status: "ok" } }));
    renderWithProviders(<PlaygroundPage />, { route: "/playground?tool=t1" });
    expect((await screen.findByTestId("schema-raw-only")).textContent).toContain("uses oneOf");
    expect(screen.getByRole("textbox", { name: /Arguments \(JSON\)/ })).toBeTruthy();
  });
});

describe("ExecutionOutcomeView — pending approval", () => {
  it("stops polling and says so when the approval is gone (agent 404) or the credentials are refused (401)", async () => {
    setCredentials({ agentKey: "agt" });
    let polls = 0;
    mockFetch({ "GET /api/v1/me/approvals/ap-x": () => (polls++, { status: 404 }), "GET /api/v1/me/approvals/ap-y": () => (polls++, { status: 401 }) });
    const view = (id: string) => (
      <ExecutionOutcomeView tool={{ name: "x" }} asAgent pollMs={20} outcome={{ status: "pending_approval", detail: "", recordId: null, approvalId: id }} />
    );
    const r = renderWithProviders(view("ap-x"), { route: "/" });
    await screen.findByTestId("approval-gone");
    r.unmount();
    renderWithProviders(view("ap-y"), { route: "/" });
    await screen.findByTestId("approval-refused");
    const settled = polls;
    await new Promise((res) => setTimeout(res, 80));
    expect(polls).toBe(settled);
  });

  it("links to the approvals list and polls until the approval resolves", async () => {
    setCredentials({ agentKey: "agt" });
    let polls = 0;
    const { calls } = mockFetch({
      "GET /api/v1/me/approvals/ap-1": () => {
        polls += 1;
        return {
          json: {
            id: "ap-1",
            agent_id: "billing-bot",
            tool_id: "t1",
            status: polls < 2 ? "pending" : "executed",
            summary: { tool: "github.create_issue", operation: "write", arguments: { title: "x" } },
            created_at: "2026-10-08T00:00:00Z",
            expires_at: "2026-10-08T00:10:00Z",
            decided_at: null,
            result_preview: polls < 2 ? null : "issue #7 created",
          },
        };
      },
    });
    renderWithProviders(
      <ExecutionOutcomeView
        tool={{ name: "create_issue" }}
        asAgent
        pollMs={20}
        outcome={{ status: "pending_approval", detail: "approval required", recordId: "rec-9", approvalId: "ap-1", roundTripMs: 12 }}
      />,
      { route: "/playground" },
    );
    const pending = await screen.findByTestId("outcome-pending");
    expect(within(pending).getByRole("link", { name: "Approvals" }).getAttribute("href")).toBe("/approvals");
    expect(screen.getByTestId("audit-id").textContent).toBe("rec-9");
    await screen.findByTestId("approval-executed", {}, { timeout: 2000 });
    expect(screen.getByText(/issue #7 created/)).toBeTruthy();
    const settled = polls;
    await new Promise((r) => setTimeout(r, 80));
    expect(polls).toBe(settled); // polling stops once resolved
    expect(calls.every((c) => c.headers.authorization === "Bearer agt")).toBe(true);
  });
});
