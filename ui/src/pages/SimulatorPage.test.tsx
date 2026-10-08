import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
import { RouteResultView, SimulatorPage } from "./SimulatorPage";
import type { RouteResponse } from "../api/types";

const base: RouteResponse = { requestId: "req-1", tools: [], fallbackUsed: false, latencyMs: 37.4, modelVersion: "deterministic-v1" };

describe("RouteResultView", () => {
  it("ranks tools by score, shows latency, and lists contributing servers", () => {
    renderWithProviders(
      <RouteResultView
        result={{
          ...base,
          tools: [
            { serverName: "slack", toolName: "post_message", score: 0.41 },
            { serverName: "github", toolName: "list_prs", score: 0.92 },
            { serverName: "github", toolName: "get_pr", score: 0.77 },
          ],
        }}
      />,
    );
    const rows = within(screen.getByRole("table", { name: "Ranked tools" })).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[1].textContent)).toEqual(["list_prs", "get_pr", "post_message"]);
    expect(screen.getByText("0.920")).toBeTruthy();
    expect(screen.getByText("37 ms")).toBeTruthy();
    const impact = screen.getByLabelText("Catalog impact");
    expect(within(impact).getByText("github — 2 tools")).toBeTruthy();
    expect(within(impact).getByText("slack — 1 tool")).toBeTruthy();
    expect(screen.queryByTestId("no-match-banner")).toBeNull();
    expect(screen.queryByTestId("fallback-banner")).toBeNull();
  });

  it("shows the no-match banner when noMatch is set, and no ranking table", () => {
    renderWithProviders(<RouteResultView result={{ ...base, noMatch: true }} />);
    expect(screen.getByTestId("no-match-banner").textContent).toContain("No match");
    expect(screen.queryByRole("table", { name: "Ranked tools" })).toBeNull();
  });

  it("treats an empty tool list as no-match even without the flag", () => {
    renderWithProviders(<RouteResultView result={{ ...base, tools: [] }} />);
    expect(screen.getByTestId("no-match-banner")).toBeTruthy();
  });

  it("shows the fallback banner when fallbackUsed", () => {
    renderWithProviders(<RouteResultView result={{ ...base, fallbackUsed: true, tools: [{ serverName: "gh", toolName: "x", score: 0.5 }] }} />);
    expect(screen.getByTestId("fallback-banner").textContent).toContain("Deterministic fallback used");
  });
});

describe("SimulatorPage", () => {
  it("validates, posts the request, and renders a mocked snake_case response with both banners", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      "POST /api/v1/route": () => ({
        json: { request_id: "r9", tools: [], fallback_used: true, no_match: true, latency_ms: 12, model_version: "deterministic-v1" },
      }),
    });
    renderWithProviders(<SimulatorPage />);
    await user.click(screen.getByRole("button", { name: "Simulate route" }));
    expect(screen.getByText("Describe the task the agent is trying to do.")).toBeTruthy();
    expect(calls).toHaveLength(0);

    await user.type(screen.getByRole("textbox", { name: /Task query/ }), "delete the prod database");
    await user.type(screen.getByRole("textbox", { name: /Agent id/ }), "agent-7");
    await user.click(screen.getByRole("button", { name: "Simulate route" }));

    await waitFor(() => expect(screen.getByTestId("no-match-banner")).toBeTruthy());
    expect(screen.getByTestId("fallback-banner")).toBeTruthy();
    expect(screen.getByText("r9", { exact: false })).toBeTruthy();
    expect(calls[0].body).toEqual({ query: "delete the prod database", agentId: "agent-7", maxTools: 5 });
  });

  it("shows a curated persistent error on failure, not the response body", async () => {
    const user = userEvent.setup();
    mockFetch({ "POST /api/v1/route": () => ({ status: 503, text: "Traceback: secret_token=abc" }) });
    renderWithProviders(<SimulatorPage />);
    await user.type(screen.getByRole("textbox", { name: /Task query/ }), "q");
    await user.type(screen.getByRole("textbox", { name: /Agent id/ }), "a");
    await user.click(screen.getByRole("button", { name: "Simulate route" }));
    await waitFor(() => expect(screen.getByText("Simulate route failed (HTTP 503)")).toBeTruthy());
    expect(document.body.textContent).not.toContain("secret_token");
  });
});
