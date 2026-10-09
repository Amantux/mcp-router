import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { mockFetch, renderWithProviders } from "../test/render";
import { SkillsPage } from "./SkillsPage";

const SKILL = { id: "k1", sourceId: "s1", sourceName: "team", name: "pdf-fill", description: "Fill PDFs", operation: "write", domain: "files", bodyTokensEst: 1234, enabled: true, ingestFlags: ["secret_like", "body_truncated", "oversize"], activationCount: 5 };
const EVIL = "<script>window.__pwned = 1</script><b>bold</b>";

function routes(extra: Record<string, () => { json?: unknown; text?: string }> = {}) {
  return mockFetch({
    "GET /api/v1/skills": () => ({ json: { items: [SKILL], total: 1, limit: 50, offset: 0 } }),
    "GET /api/v1/skill-sources": () => ({ json: [] }),
    "GET /api/v1/principals": () => ({ json: [{ id: "p1", agentId: "claude-desk", enabled: true, maxTools: 10, createdAt: "" }] }),
    "GET /api/v1/skills/k1": () => ({ json: { ...SKILL, license: "MIT", allowedTools: ["Read"], resourceManifest: [], versions: [] } }),
    "GET /api/v1/skills/k1/body": () => ({ text: EVIL }),
    ...extra,
  });
}

afterEach(() => vi.restoreAllMocks());

describe("SkillsPage", () => {
  it("renders ingest-flag chips and tabular token counts", async () => {
    routes();
    renderWithProviders(<SkillsPage />, { route: "/skills" });
    expect(await screen.findByText("secret-like")).toBeTruthy();
    expect(screen.getByText("body truncated")).toBeTruthy();
    expect(screen.getByText("oversize")).toBeTruthy();
    expect(screen.getByText("1,234")).toBeTruthy();
  });

  it("shows the untrusted body as inert text, never as HTML", async () => {
    routes();
    renderWithProviders(<SkillsPage />, { route: "/skills" });
    fireEvent.click(await screen.findByText("pdf-fill"));
    fireEvent.click(await screen.findByRole("tab", { name: "Body" }));
    const pre = await screen.findByLabelText("Skill body");
    expect(pre.textContent).toBe(EVIL);
    expect(pre.querySelector("script")).toBeNull();
    expect(pre.querySelector("b")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("downloads the bundle for the chosen agent", async () => {
    const { calls } = routes({ "GET /api/v1/skills/bundle": () => ({ text: "PK-zip" }) });
    const create = vi.fn(() => "blob:x");
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    renderWithProviders(<SkillsPage />, { route: "/skills" });
    await screen.findByRole("option", { name: "claude-desk" });
    fireEvent.change(screen.getByLabelText("Bundle agent"), { target: { value: "claude-desk" } });
    fireEvent.click(screen.getByRole("button", { name: "Download bundle" }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(calls.some((c) => c.url.includes("/skills/bundle?agentId=claude-desk"))).toBe(true);
    expect(create).toHaveBeenCalled();
  });
});
