import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
import { ClassificationEditor } from "./ClassificationEditor";
import type { MCPTool, Skill } from "../api/types";
import { updateSkillClassification } from "../api/client";

const tool: MCPTool = {
  id: "t1",
  serverId: "s1",
  name: "run_query",
  description: "",
  inputSchema: {},
  schemaHash: "h",
  domain: "databases",
  tags: ["sql"],
  operation: "read",
  requiredScopes: [],
  enabled: true,
  version: 1,
  classificationReviewed: false,
};

describe("ClassificationEditor", () => {
  it("is not saveable until something changes", () => {
    mockFetch({});
    renderWithProviders(<ClassificationEditor tool={tool} onSaved={() => {}} />);
    expect((screen.getByRole("button", { name: "Save classification" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/AI-generated — not yet reviewed/)).toBeTruthy();
  });

  it("shows a pending label, PATCHes the edited classification, and reports success", async () => {
    const user = userEvent.setup();
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const { calls } = mockFetch({
      "PATCH /api/v1/tools/t1/classification": async (body) => {
        await gate;
        return { json: { ...tool, ...(body as object), classification_reviewed: true } };
      },
    });
    const onSaved = vi.fn();
    renderWithProviders(<ClassificationEditor tool={tool} onSaved={onSaved} />);

    await user.selectOptions(screen.getByRole("combobox", { name: /Operation/ }), "execute");
    const tags = screen.getByRole("textbox", { name: /Tags/ });
    await user.clear(tags);
    await user.type(tags, "sql, admin ,");
    await user.type(screen.getByRole("textbox", { name: /Required scopes/ }), "db:write");
    await user.click(screen.getByRole("button", { name: "Save classification" }));

    const pendingBtn = await screen.findByRole("button", { name: "Saving…" });
    expect((pendingBtn as HTMLButtonElement).disabled).toBe(true);
    release();

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(calls[0].body).toEqual({ domain: "databases", operation: "execute", tags: ["sql", "admin"], requiredScopes: ["db:write"] });
    expect(onSaved.mock.calls[0][0].classificationReviewed).toBe(true);
    expect(screen.getByText("Saved classification for “run_query”")).toBeTruthy();
  });

  it("sends null when the domain is cleared and surfaces a curated error on failure", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({ "PATCH /api/v1/tools/t1/classification": () => ({ status: 422, text: '{"detail":"raw validator dump"}' }) });
    const onSaved = vi.fn();
    renderWithProviders(<ClassificationEditor tool={tool} onSaved={onSaved} />);
    await user.selectOptions(screen.getByRole("combobox", { name: /Domain/ }), "(unclassified)");
    await user.click(screen.getByRole("button", { name: "Save classification" }));
    await waitFor(() => expect(screen.getByText("Save classification for “run_query” failed (HTTP 422)")).toBeTruthy());
    expect((calls[0].body as { domain: unknown }).domain).toBeNull();
    expect(onSaved).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain("raw validator dump");
    expect(screen.getByRole("button", { name: "Save classification" })).toBeTruthy();
  });

  it("saves a skill through the injected save function (skills PATCH, not tools)", async () => {
    const user = userEvent.setup();
    const skill = { id: "s1", sourceId: "src", name: "pdf-fill", description: "d", operation: "read", domain: null, tags: [], requiredScopes: [] } as unknown as Skill;
    const { calls } = mockFetch({
      "PATCH /api/v1/skills/s1/classification": (body) => ({ json: { ...skill, ...(body as object), classificationReviewed: true } }),
    });
    const onSaved = vi.fn();
    renderWithProviders(<ClassificationEditor<Skill> tool={skill} save={updateSkillClassification} onSaved={onSaved} />);
    await user.type(screen.getByRole("textbox", { name: /Required scopes/ }), "docs:read");
    await user.click(screen.getByRole("button", { name: "Save classification" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(calls).toHaveLength(1);
    expect(calls[0].body).toMatchObject({ operation: "read", requiredScopes: ["docs:read"] });
    expect(onSaved.mock.calls[0][0].classificationReviewed).toBe(true);
  });
});
