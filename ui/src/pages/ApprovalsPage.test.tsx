import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
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
});
