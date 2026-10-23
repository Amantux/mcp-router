import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
import { HealthPage } from "./HealthPage";

const FAKE_KEY = "sk-FAKE-should-never-render-0123456789";
const backend = { loaded: true, name: "x", device: "cpu", backend: "x" };

const HEALTH = {
  loaded: true,
  mode: "balanced",
  device: "cpu",
  embedding: backend,
  decision: backend,
  memory: {},
  decisionBackend: {
    kind: "aoai",
    model: null,
    endpointHost: "contoso.openai.azure.com",
    deployment: "gpt-4o-mini",
    apiKey: FAKE_KEY, // sibling field the UI must drop
  },
  embeddingBackend: { kind: "local", name: "bge-small", endpointHost: null, deployment: null, token: FAKE_KEY },
};

const CACHE_STATS = {
  enabled: true,
  entryCount: 12,
  size: 500,
  ttlS: 30,
  hits: 9,
  misses: 1,
  evictionsCapacity: 0,
  evictionsExpired: 0,
  catalogGeneration: 3,
  policyGeneration: 1,
};

describe("HealthPage backend cards", () => {
  it("shows backend kind/host/deployment and never renders a key-like sibling field", async () => {
    mockFetch({
      "GET /api/v1/models/health": () => ({ json: HEALTH }),
      "GET /healthz": () => ({ json: { status: "ok" } }),
      "GET /api/v1/cache": () => ({ json: CACHE_STATS }),
    });
    renderWithProviders(<HealthPage />);
    const dec = await screen.findByLabelText("Decision backend");
    expect(dec.textContent).toContain("aoai");
    expect(dec.textContent).toContain("gpt-4o-mini");
    expect(dec.textContent).toContain("contoso.openai.azure.com");
    const emb = screen.getByLabelText("Embedding backend");
    expect(emb.textContent).toContain("bge-small");
    expect(emb.textContent).toContain("local");
    expect(document.body.innerHTML).not.toContain(FAKE_KEY);
  });
});

describe("HealthPage route cache card", () => {
  it("shows cache stats and flushes on click (MT-4: exercises getCacheStats/flushCache)", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      "GET /api/v1/models/health": () => ({ json: HEALTH }),
      "GET /healthz": () => ({ json: { status: "ok" } }),
      "GET /api/v1/cache": () => ({ json: CACHE_STATS }),
      "POST /api/v1/cache/flush": () => ({ json: { catalogGeneration: 4, policyGeneration: 1 } }),
    });
    renderWithProviders(<HealthPage />);
    const card = await screen.findByLabelText("Route cache");
    expect(card.textContent).toContain("12");
    expect(card.textContent).toContain("90.0%");
    await user.click(await within(card).findByText("Flush cache"));
    await waitFor(() => expect(calls.some((c) => `${c.method} ${c.path}` === "POST /api/v1/cache/flush")).toBe(true));
  });

  it("shows a disabled state when the cache is off", async () => {
    mockFetch({
      "GET /api/v1/models/health": () => ({ json: HEALTH }),
      "GET /healthz": () => ({ json: { status: "ok" } }),
      "GET /api/v1/cache": () => ({ json: { ...CACHE_STATS, enabled: false } }),
    });
    renderWithProviders(<HealthPage />);
    const card = await screen.findByLabelText("Route cache");
    expect(await within(card).findByText(/Route cache is disabled/)).toBeTruthy();
  });
});
