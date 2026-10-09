import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expectQuery, mockFetch, renderWithProviders } from "../test/render";
import { ApprovalsPage } from "./ApprovalsPage";

const approval = (id: string, tool: string, agent: string) => ({
  id,
  agent_id: agent,
  tool_id: `tool-${id}`,
  status: "pending",
  summary: { tool, operation: "write", arguments: { title: "x" } },
  created_at: "2026-10-08T00:00:00Z",
  expires_at: "2026-10-08T00:10:00Z",
  decided_at: null,
  result_preview: null,
});

describe("ApprovalsPage", () => {
  it("names each row's Approve/Deny after its tool and agent, and the actions column", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      "GET /api/v1/approvals": () => ({ json: [approval("a1", "github.create_issue", "billing-bot"), approval("a2", "github.create_issue", "triage-bot")] }),
      "POST /api/v1/approvals/a2/approve": () => ({ json: { id: "a2", status: "executed" } }),
    });
    renderWithProviders(<ApprovalsPage />, { route: "/approvals" });
    expect(await screen.findByRole("button", { name: "Approve github.create_issue for billing-bot" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Deny github.create_issue for triage-bot" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "Actions" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Approve github.create_issue for triage-bot" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Approve and run" }));
    await screen.findByText(/Approved github.create_issue for triage-bot/);
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/v1/approvals/a2/approve")).toBe(true);
  });

  it("filters by status (pending by default; Any sends none) and explains an empty queue", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({ "GET /api/v1/approvals": () => ({ json: [] }) });
    renderWithProviders(<ApprovalsPage />, { route: "/approvals" });
    expect(await screen.findByText("Nothing is waiting for approval")).toBeTruthy();
    expectQuery(calls, "GET", "/api/v1/approvals", { status: "pending" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "");
    await waitFor(() => expectQuery(calls, "GET", "/api/v1/approvals", {}));
    expect(await screen.findByText("No approvals match")).toBeTruthy();
  });

  it("denies after a confirmation that says nothing runs; Cancel sends nothing", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      "GET /api/v1/approvals": () => ({ json: [approval("a1", "github.create_issue", "billing-bot")] }),
      "POST /api/v1/approvals/a1/deny": () => ({ json: { id: "a1", status: "denied" } }),
    });
    renderWithProviders(<ApprovalsPage />, { route: "/approvals" });
    await user.click(await screen.findByRole("button", { name: "Deny github.create_issue for billing-bot" }));
    let dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Deny github.create_issue for billing-bot?")).toBeTruthy();
    expect(dialog.textContent).toContain("never runs");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    await user.click(await screen.findByRole("button", { name: "Deny github.create_issue for billing-bot" }));
    dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Deny" }));
    expect(await screen.findByText("Denied github.create_issue for billing-bot")).toBeTruthy();
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual(["/api/v1/approvals/a1/deny"]);
  });

  it("a failed approve keeps the dialog open and reports the failure", async () => {
    const user = userEvent.setup();
    mockFetch({
      "GET /api/v1/approvals": () => ({ json: [approval("a1", "github.create_issue", "billing-bot")] }),
      "POST /api/v1/approvals/a1/approve": () => ({ status: 409 }),
    });
    renderWithProviders(<ApprovalsPage />, { route: "/approvals" });
    await user.click(await screen.findByRole("button", { name: "Approve github.create_issue for billing-bot" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Approve and run" }));
    expect(await screen.findByText(/Approve github.create_issue failed \(HTTP 409\)/)).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
});

