import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../test/render";
import { ConfirmDialog } from "./common";

describe("ConfirmDialog", () => {
  it("names the object, confirms with its own label and cancels without confirming", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    renderWithProviders(
      <>
        <button type="button">Run tool</button>
        <ConfirmDialog open title="Run create_issue?" body="It changes data." confirmLabel="Run create_issue" pendingLabel="Running…" pending={false} onConfirm={onConfirm} onCancel={onCancel} />
      </>,
    );
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Run create_issue?")).toBeTruthy();
    // Clicked immediately on open: no waiting for transitions.
    await user.click(within(dialog).getByRole("button", { name: "Run create_issue" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("while pending shows the pending label and disables both buttons", async () => {
    renderWithProviders(<ConfirmDialog open title="t" body="b" confirmLabel="Go" pendingLabel="Going…" pending onConfirm={() => {}} onCancel={() => {}} />);
    const dialog = await screen.findByRole("dialog");
    expect((within(dialog).getByRole("button", { name: "Going…" }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(dialog).getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
