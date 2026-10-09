import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch, renderWithProviders } from "../test/render";
import { SkillSourcesPage } from "./SkillSourcesPage";

const SRC = { id: "s1", name: "team-skills", kind: "git", location: "https://github.com/org/skills", gitRef: "main", enabled: true, status: "ok", lastSyncedAt: null, lastCommit: "abcdef123", skillCount: 3 };

describe("SkillSourcesPage", () => {
  it("refuses a non-https git URL client-side and posts an https one", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/skill-sources": () => ({ json: [] }),
      "POST /api/v1/skill-sources": (b) => ({ json: { ...SRC, ...(b as object) } }),
    });
    renderWithProviders(<SkillSourcesPage />);
    fireEvent.click((await screen.findAllByRole("button", { name: "Add source" }))[0]);
    const dlg = await screen.findByRole("dialog");
    fireEvent.change(within(dlg).getByLabelText("Name"), { target: { value: "team" } });
    fireEvent.click(within(dlg).getByLabelText("Git (https)"));
    const url = within(dlg).getByLabelText(/Repository URL/);
    fireEvent.change(url, { target: { value: "http://github.com/org/skills" } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Add source" }));
    expect(await within(dlg).findByText(/Only https:\/\/ git URLs are accepted/)).toBeTruthy();
    expect(calls.filter((c) => c.method === "POST")).toHaveLength(0);

    fireEvent.change(url, { target: { value: "https://github.com/org/skills" } });
    fireEvent.click(within(dlg).getByRole("button", { name: "Add source" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "POST")).toHaveLength(1));
    expect(calls.find((c) => c.method === "POST")?.body).toMatchObject({ kind: "git", location: "https://github.com/org/skills" });
  });

  it("syncs a source and renders the report with skipped reasons", async () => {
    mockFetch({
      "GET /api/v1/skill-sources": () => ({ json: [SRC] }),
      "POST /api/v1/skill-sources/s1/sync": () => ({ json: { added: 2, changed: 1, removed: 0, skipped: [{ path: "bad/SKILL.md", reason: "invalid frontmatter" }] } }),
    });
    renderWithProviders(<SkillSourcesPage />);
    expect(await screen.findByText("github.com @ main")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Sync" }));
    const report = await screen.findByLabelText("Sync report");
    expect(report.textContent).toContain("2 added · 1 changed · 0 removed · 1 skipped");
    expect(within(report).getByText("invalid frontmatter")).toBeTruthy();
    expect(within(report).getByText("bad/SKILL.md")).toBeTruthy();
  });

  it("reopens the add dialog blank after an add and after a cancel; Enter submits", async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      "GET /api/v1/skill-sources": () => ({ json: [] }),
      "POST /api/v1/skill-sources": (b) => ({ json: { ...SRC, ...(b as object) } }),
    });
    renderWithProviders(<SkillSourcesPage />);
    await user.click((await screen.findAllByRole("button", { name: "Add source" }))[0]);
    let dlg = await screen.findByRole("dialog");
    await user.type(within(dlg).getByLabelText("Name"), "team");
    await user.type(within(dlg).getByLabelText(/Directory path/), "/srv/skills{Enter}");
    await waitFor(() => expect(calls.filter((c) => c.method === "POST")).toHaveLength(1));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    await user.click((await screen.findAllByRole("button", { name: "Add source" }))[0]);
    dlg = await screen.findByRole("dialog");
    expect((within(dlg).getByLabelText("Name") as HTMLInputElement).value).toBe("");
    expect((within(dlg).getByLabelText(/Directory path/) as HTMLInputElement).value).toBe("");
    // Touch the form so errors show, then cancel: the next open must not start in an error state.
    await user.click(within(dlg).getByRole("button", { name: "Add source" }));
    expect(within(dlg).getByText("Enter a name.")).toBeTruthy();
    await user.click(within(dlg).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await user.click((await screen.findAllByRole("button", { name: "Add source" }))[0]);
    dlg = await screen.findByRole("dialog");
    expect(within(dlg).queryByText("Enter a name.")).toBeNull();
  });
});

