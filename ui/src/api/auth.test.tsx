import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
import { getAuthSnapshot, hydrateAuth, setCredentials } from "./auth";
import { describeError, getMe, listServers, listTools, probeAdmin } from "./client";
import { AuthBanner, ConnectPanel } from "../components/ConnectPanel";
import { useLoader } from "../hooks/useLoader";

const ADMIN = "adm-SECRET-7f3a";
const AGENT = "agt-SECRET-91bc";

describe("auth header injection", () => {
  it("sends the admin token as Authorization: Bearer on every admin request", async () => {
    setCredentials({ adminToken: ADMIN });
    const { calls } = mockFetch({
      "GET /api/v1/servers": () => ({ json: [] }),
      "GET /api/v1/tools": () => ({ json: { items: [], total: 0 } }),
      "GET /api/v1/principals": () => ({ json: [] }),
    });
    await listServers();
    await listTools({ limit: 50, offset: 0 });
    await probeAdmin();
    expect(calls).toHaveLength(3);
    for (const c of calls) expect(c.headers.authorization).toBe(`Bearer ${ADMIN}`);
  });

  it("presents the agent key on agent-identity requests, and falls back to admin without one", async () => {
    setCredentials({ adminToken: ADMIN, agentKey: AGENT });
    const { calls } = mockFetch({
      "GET /api/v1/me": () => ({ json: { id: "p1", agent_id: "billing-bot", enabled: true, max_tools: 8 } }),
    });
    expect((await getMe()).agentId).toBe("billing-bot");
    expect(calls[0].headers.authorization).toBe(`Bearer ${AGENT}`);
    expect(getAuthSnapshot().agentId).toBe("billing-bot");

    setCredentials({ agentKey: null });
    await getMe();
    expect(calls[1].headers.authorization).toBe(`Bearer ${ADMIN}`);
  });

  it("sends no Authorization header when no credential is configured (dev mode)", async () => {
    const { calls } = mockFetch({ "GET /api/v1/servers": () => ({ json: [] }) });
    await listServers();
    expect(calls[0].headers.authorization).toBeUndefined();
  });
});

describe("credential storage", () => {
  it("keeps credentials in sessionStorage only — localStorage and cookies stay empty", () => {
    setCredentials({ adminToken: ADMIN, agentKey: AGENT });
    expect(window.localStorage.length).toBe(0);
    expect(document.cookie).toBe("");
    expect(window.sessionStorage.getItem("mcpr.adminToken")).toBe(ADMIN);
    expect(window.sessionStorage.getItem("mcpr.agentKey")).toBe(AGENT);
    // A reload of the tab (re-hydration) restores them from the session.
    hydrateAuth();
    expect(getAuthSnapshot()).toMatchObject({ hasAdminToken: true, hasAgentKey: true });
  });

  it("clearing a credential removes it from sessionStorage", () => {
    setCredentials({ adminToken: ADMIN, agentKey: AGENT });
    setCredentials({ agentKey: null });
    expect(window.sessionStorage.getItem("mcpr.agentKey")).toBeNull();
    expect(window.sessionStorage.getItem("mcpr.adminToken")).toBe(ADMIN);
  });
});

function ServersProbe() {
  const l = useLoader("Load servers", (sig) => listServers(sig), []);
  return <span>{l.data ? `servers:${l.data.length}` : "loading"}</span>;
}

describe("401/403 flips the global not-connected state", () => {
  it("shows the not-connected bar (linking to Connect) instead of a raw error toast", async () => {
    setCredentials({ adminToken: ADMIN });
    mockFetch({ "GET /api/v1/servers": () => ({ status: 401, text: "invalid token adm-SECRET" }) });
    const onOpen = vi.fn();
    renderWithProviders(
      <>
        <AuthBanner onOpen={onOpen} />
        <ServersProbe />
      </>,
    );
    await waitFor(() => expect(screen.getByTestId("auth-banner")).toBeTruthy());
    expect(getAuthSnapshot().status).toBe("rejected");
    expect(screen.queryByText(/Load servers failed/)).toBeNull();
    await userEvent.setup().click(screen.getByRole("button", { name: "Open Connect" }));
    expect(onOpen).toHaveBeenCalled();
    expect(document.body.textContent).not.toContain("SECRET");
  });

  it("treats 403 (no admin token configured server-side) the same way", async () => {
    mockFetch({ "GET /api/v1/servers": () => ({ status: 403 }) });
    renderWithProviders(<AuthBanner onOpen={() => {}} />);
    await listServers().catch(() => {});
    await waitFor(() => expect(screen.getByTestId("auth-banner")).toBeTruthy());
  });

  it("an agent-key 403 is a policy answer and does not disconnect the dashboard", async () => {
    setCredentials({ adminToken: ADMIN, agentKey: AGENT });
    mockFetch({ "GET /api/v1/me": () => ({ status: 403 }) });
    await getMe().catch(() => {});
    expect(getAuthSnapshot().status).not.toBe("rejected");
    expect(getAuthSnapshot().agentKeyRejected).toBe(false);
  });

  it("attributes a response to the credential presented when the request was sent", async () => {
    setCredentials({ adminToken: ADMIN, agentKey: AGENT });
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    mockFetch({ "GET /api/v1/me": async () => (await gate, { status: 403 }) });
    const p = getMe().catch(() => {});
    setCredentials({ agentKey: null }); // forgotten mid-flight
    release();
    await p;
    expect(getAuthSnapshot().status).not.toBe("rejected");
  });

  it("gives agent-key advice, not admin-token advice, for an agent 401", async () => {
    setCredentials({ agentKey: AGENT });
    mockFetch({ "GET /api/v1/me": () => ({ status: 401 }) });
    const err = await getMe().catch((e: unknown) => e);
    expect(describeError(err).advice).toContain("refused the agent key");
  });

  it("a later 2xx admin response reconnects", async () => {
    setCredentials({ adminToken: ADMIN });
    let status = 401;
    mockFetch({ "GET /api/v1/servers": () => ({ status, json: [] }) });
    await listServers().catch(() => {});
    expect(getAuthSnapshot().status).toBe("rejected");
    status = 200;
    await listServers();
    expect(getAuthSnapshot().status).toBe("connected");
  });
});

describe("ConnectPanel", () => {
  it("uses password inputs, stores the token without rendering or logging it, and verifies", async () => {
    const user = userEvent.setup();
    const logs = [vi.spyOn(console, "log"), vi.spyOn(console, "info"), vi.spyOn(console, "warn"), vi.spyOn(console, "error"), vi.spyOn(console, "debug")];
    const { calls } = mockFetch({ "GET /api/v1/principals": () => ({ json: [] }) });
    renderWithProviders(<ConnectPanel open onClose={() => {}} />);
    const input = screen.getByLabelText(/Admin token/) as HTMLInputElement;
    expect(input.type).toBe("password");
    expect((screen.getByLabelText(/Agent API key/) as HTMLInputElement).type).toBe("password");
    await user.type(input, ADMIN);
    await user.click(screen.getByRole("button", { name: "Save and verify" }));
    await waitFor(() => expect(screen.getByTestId("connect-verdict").textContent).toContain("Admin access confirmed"));
    expect(calls[0].headers.authorization).toBe(`Bearer ${ADMIN}`);
    expect(input.value).toBe("");
    expect(input.placeholder).toContain("saved");
    expect(document.body.innerHTML).not.toContain(ADMIN);
    for (const spy of logs) for (const args of spy.mock.calls) expect(JSON.stringify(args)).not.toContain(ADMIN);
  });

  it("reports a refused token in its own copy", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /api/v1/principals": () => ({ status: 401 }) });
    renderWithProviders(<ConnectPanel open onClose={() => {}} />);
    await user.type(screen.getByLabelText(/Admin token/), "wrong");
    await user.click(screen.getByRole("button", { name: "Save and verify" }));
    await waitFor(() => expect(screen.getByTestId("connect-verdict").textContent).toContain("refused the admin token"));
  });

  it("Disconnect forgets both credentials", async () => {
    const user = userEvent.setup();
    setCredentials({ adminToken: ADMIN, agentKey: AGENT });
    renderWithProviders(<ConnectPanel open onClose={() => {}} />);
    await user.click(screen.getByRole("button", { name: "Disconnect" }));
    expect(getAuthSnapshot()).toMatchObject({ hasAdminToken: false, hasAgentKey: false });
    expect(window.sessionStorage.length).toBe(0);
  });
});
