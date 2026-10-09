import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { callsTo, mockFetch, renderWithProviders } from "../test/render";
import { ExecutionsPage } from "./ExecutionsPage";

const ROW = {
  id: "e1",
  agent_id: "billing-bot",
  tool_id: "tool-123",
  tool_name: "create_invoice",
  server_name: "billing",
  outcome: "success",
  detail: "",
  latency_ms: 12,
  created_at: "2026-10-08T00:00:00Z",
  route_request_id: "req-1",
};
const SKILL_ROW = { ...ROW, id: "e2", tool_id: "skill:s9", tool_name: "pdf", route_request_id: "req-2" };
const UNATTRIBUTED = { ...ROW, id: "e3", tool_name: "orphan_tool", route_request_id: null };

function mock(feedbackStatus = 200) {
  return mockFetch({
    "GET /api/v1/executions": () => ({ json: { items: [ROW, SKILL_ROW, UNATTRIBUTED], total: 3, limit: 50, offset: 0 } }),
    "POST /api/v1/route/req-1/feedback": () =>
      feedbackStatus === 200 ? { json: { recorded: 1, source: "human" } } : { status: feedbackStatus, json: { detail: "slow down" } },
    "POST /api/v1/route/req-2/feedback": () => ({ json: { recorded: 1, source: "human" } }),
  });
}

describe("ExecutionsPage feedback", () => {
  it("posts human feedback to the row's routeRequestId, targeting the tool/skill by id + kind", async () => {
    const { calls } = mock();
    renderWithProviders(<ExecutionsPage />, { route: "/executions" });
    await userEvent.click(await screen.findByRole("button", { name: "Helpful: create_invoice" }));
    await userEvent.click(screen.getByRole("button", { name: "Not helpful: pdf" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST")).toHaveLength(2));
    const [a, b] = calls.filter((c) => c.method === "POST");
    expect(a.url).toContain("/api/v1/route/req-1/feedback");
    expect(a.body).toEqual({ items: [{ kind: "tool", id: "tool-123", helpful: true }] });
    expect(b.url).toContain("/api/v1/route/req-2/feedback");
    expect(b.body).toEqual({ items: [{ kind: "skill", id: "skill:s9", helpful: false }] });
    // optimistic state sticks on success
    expect(screen.getByRole("button", { name: "Helpful: create_invoice" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("disables thumbs (with a reason) when the row has no routeRequestId, and links attributed rows to the lens", async () => {
    mock();
    renderWithProviders(<ExecutionsPage />, { route: "/executions" });
    const up = await screen.findByRole("button", { name: "Helpful: orphan_tool" });
    expect(up.hasAttribute("disabled") || up.getAttribute("aria-disabled") === "true").toBe(true);
    const links = screen.getAllByRole("link", { name: "Open in lens" });
    expect(links).toHaveLength(2);
    expect(links[0].getAttribute("href")).toBe("/lens?routeRequestId=req-1&agentId=billing-bot");
  });

  it("rolls back the optimistic vote and reports the error in the notification bar", async () => {
    mock(429);
    renderWithProviders(<ExecutionsPage />, { route: "/executions" });
    const up = await screen.findByRole("button", { name: "Helpful: create_invoice" });
    await userEvent.click(up);
    const alert = await screen.findByText(/Record feedback for create_invoice/);
    expect(alert).toBeTruthy();
    await waitFor(() => expect(up.getAttribute("aria-pressed")).toBe("false"));
    expect(within(document.body).queryByText(/recorded/)).toBeNull();
  });
});

describe("ExecutionsPage outcomes (HS-U-013)", () => {
  it("labels every backend outcome and can filter by one the old list missed", async () => {
    const rows = [
      { ...UNATTRIBUTED, id: "o1", tool_name: "a", outcome: "invalid_args" },
      { ...UNATTRIBUTED, id: "o2", tool_name: "b", outcome: "read" },
    ];
    const { calls } = mockFetch({ "GET /api/v1/executions": () => ({ json: { items: rows, total: 2, limit: 50, offset: 0 } }) });
    renderWithProviders(<ExecutionsPage />, { route: "/executions" });
    const table = await screen.findByRole("table", { name: "Executions" });
    expect(within(table).getByText("invalid arguments")).toBeTruthy();
    expect(within(table).getByText("resource read")).toBeTruthy();
    const select = screen.getByRole("combobox", { name: "Outcome" });
    for (const label of ["unavailable", "invalid arguments", "cancelled", "in bundle", "resource read"])
      expect(within(select).getByRole("option", { name: label })).toBeTruthy();
    await userEvent.selectOptions(select, "unavailable");
    await waitFor(() => expect(callsTo(calls, "GET", "/api/v1/executions").at(-1)?.query.outcome).toBe("unavailable"));
  });
});
