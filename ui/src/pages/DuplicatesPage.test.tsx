import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
import { DismissDialog, DuplicatesPage } from "./DuplicatesPage";
import type { MCPTool } from "../api/types";

const tool = (id: string, name: string, server: string): MCPTool => ({
  id,
  serverId: `s-${server}`,
  serverName: server,
  name,
  description: `${name} description`,
  inputSchema: { type: "object" },
  schemaHash: "h",
  tags: [],
  operation: "read",
  requiredScopes: [],
  enabled: true,
  version: 1,
  callCount: 3,
});

describe("DismissDialog", () => {
  it("refuses to dismiss without a justification", async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn(async () => {});
    renderWithProviders(<DismissDialog open pairLabel="“a” ↔ “b”" onCancel={() => {}} onDismiss={onDismiss} />);
    await user.click(screen.getByRole("button", { name: "Dismiss suggestion" }));
    expect(onDismiss).not.toHaveBeenCalled();
    expect(screen.getByText(/Explain why these are not duplicates/)).toBeTruthy();

    await user.type(screen.getByRole("textbox", { name: /Justification/ }), "   ");
    await user.click(screen.getByRole("button", { name: "Dismiss suggestion" }));
    expect(onDismiss).not.toHaveBeenCalled();

    await user.type(screen.getByRole("textbox", { name: /Justification/ }), "different scopes ");
    await user.click(screen.getByRole("button", { name: "Dismiss suggestion" }));
    expect(onDismiss).toHaveBeenCalledWith("different scopes");
  });
});

describe("DuplicatesPage", () => {
  it("shows pairs side by side, states nothing is auto-disabled, and sends the justification on dismiss", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      "GET /api/v1/dedup/suggestions": () => ({
        json: [
          {
            id: "d1",
            tool_a_id: "a",
            tool_b_id: "b",
            similarity: 0.934,
            rationale: "Same schema, near-identical descriptions",
            preferred_tool_id: "b",
            status: "open",
            created_at: "2026-10-01T00:00:00Z",
            tool_a: tool("a", "search_issues", "github"),
            tool_b: tool("b", "find_issues", "gitea"),
          },
        ],
      }),
      "POST /api/v1/dedup/suggestions/d1/dismiss": () => ({ json: { id: "d1", status: "dismissed" } }),
    });
    renderWithProviders(<DuplicatesPage />);
    await waitFor(() => expect(screen.getByText("93.4% similar")).toBeTruthy());
    expect(screen.getByText("Same schema, near-identical descriptions")).toBeTruthy();
    expect(screen.getByLabelText("Tool A: search_issues")).toBeTruthy();
    expect(screen.getByLabelText("Tool B: find_issues")).toBeTruthy();
    expect(screen.getByText(/Nothing is ever auto-disabled or deleted/)).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Dismiss…" }));
    await user.click(screen.getByRole("button", { name: "Dismiss suggestion" }));
    expect(calls.some((c) => c.url.endsWith("/dismiss"))).toBe(false);

    await user.type(screen.getByRole("textbox", { name: /Justification/ }), "Gitea is a different forge");
    await user.click(screen.getByRole("button", { name: "Dismiss suggestion" }));
    await waitFor(() => expect(calls.find((c) => c.url.endsWith("/dismiss"))?.body).toEqual({ justification: "Gitea is a different forge" }));
  });

  it("accepts with the chosen preferred tool", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      "GET /api/v1/dedup/suggestions": () => ({
        json: [
          {
            id: "d1",
            toolAId: "a",
            toolBId: "b",
            similarity: 0.8,
            rationale: "",
            preferredToolId: "b",
            status: "open",
            createdAt: "2026-10-01T00:00:00Z",
            toolA: tool("a", "search_issues", "github"),
            toolB: tool("b", "find_issues", "gitea"),
          },
        ],
      }),
      "POST /api/v1/dedup/suggestions/d1/accept": () => ({ json: { id: "d1", status: "accepted" } }),
    });
    renderWithProviders(<DuplicatesPage />);
    await waitFor(() => expect(screen.getByRole("radio", { name: "Prefer search_issues" })).toBeTruthy());
    await user.click(screen.getByRole("radio", { name: "Prefer search_issues" }));
    await user.click(screen.getByRole("button", { name: "Accept preferred" }));
    await waitFor(() => expect(calls.find((c) => c.url.endsWith("/accept"))?.body).toEqual({ preferredToolId: "a" }));
  });
});
