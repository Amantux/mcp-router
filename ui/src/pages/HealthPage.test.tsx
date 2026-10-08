import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
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

describe("HealthPage backend cards", () => {
  it("shows backend kind/host/deployment and never renders a key-like sibling field", async () => {
    mockFetch({
      "GET /api/v1/models/health": () => ({ json: HEALTH }),
      "GET /healthz": () => ({ json: { status: "ok" } }),
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
