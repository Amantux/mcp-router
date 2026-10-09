import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { mockFetch } from "../test/render";
import { clearCredentials, setCredentials } from "../api/auth";
import { Layout } from "./Layout";
import { REDIRECT_KEY } from "../pages/setup/redirect";

const STATUS = { completedAt: null, hasAdminToken: true, devMode: false, backend: "laya", counts: { principals: 0, servers: 0, tools: 0, skillSources: 0 } };

function mount(start = "/servers") {
  const router = createMemoryRouter(
    [{ path: "/", element: <Layout />, children: [
      { path: "servers", element: <p>servers page</p> },
      { path: "setup", element: <p>setup page</p> },
    ] }],
    { initialEntries: [start] },
  );
  render(<FluentProvider theme={webLightTheme}><RouterProvider router={router} /></FluentProvider>);
  return router;
}

describe("Layout first-run redirect", () => {
  beforeEach(() => sessionStorage.clear());
  afterEach(() => clearCredentials());

  it("routes an admin to /setup once per session when setup is needed", async () => {
    mockFetch({ "GET /api/v1/setup/status": () => ({ json: { ...STATUS, needsSetup: true } }) });
    setCredentials({ adminToken: "adm-secret" });
    const router = mount();
    expect(await screen.findByText("setup page")).toBeTruthy();
    expect(sessionStorage.getItem(REDIRECT_KEY)).toBe("1");
    // Navigating away does not bounce back (no loop).
    await act(() => router.navigate("/servers"));
    expect(await screen.findByText("servers page")).toBeTruthy();
  });

  it("never redirects when the session already skipped", async () => {
    const { calls } = mockFetch({ "GET /api/v1/setup/status": () => ({ json: { ...STATUS, needsSetup: true } }) });
    sessionStorage.setItem(REDIRECT_KEY, "skipped");
    setCredentials({ adminToken: "adm-secret" });
    mount();
    expect(await screen.findByText("servers page")).toBeTruthy();
    expect(calls.filter((c) => c.url.includes("setup/status"))).toHaveLength(0);
  });

  it("stays put when setup is complete, and does nothing without an admin token", async () => {
    const { calls } = mockFetch({ "GET /api/v1/setup/status": () => ({ json: { ...STATUS, needsSetup: false } }) });
    mount();
    await screen.findByText("servers page");
    expect(calls).toHaveLength(0);
    setCredentials({ adminToken: "adm-secret" });
    await waitFor(() => expect(sessionStorage.getItem(REDIRECT_KEY)).toBe("1"));
    expect(screen.getByText("servers page")).toBeTruthy();
  });
});

describe("Layout Connect panel on the setup wizard", () => {
  afterEach(() => clearCredentials());
  it("warns that saving credentials restarts the wizard step only while on /setup", async () => {
    mockFetch({ "GET /api/v1/setup/status": () => ({ json: { ...STATUS, needsSetup: false } }) });
    const router = mount("/setup");
    await screen.findByText("setup page");
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByTestId("connect-remount-warning")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await act(() => router.navigate("/servers"));
    await screen.findByText("servers page");
    fireEvent.click(await screen.findByRole("button", { name: "Connect" }));
    expect(await screen.findByRole("form", { name: "Connect" })).toBeTruthy();
    expect(screen.queryByTestId("connect-remount-warning")).toBeNull();
  });
});

