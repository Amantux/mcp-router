import { describe, expect, it } from "vitest";
import { useState } from "react";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../test/render";
import { compileSchema, type ObjectNode } from "./model";
import { initialDraft, toArguments, type Draft, type FieldErrors } from "./draft";
import { SchemaForm } from "./SchemaForm";

const SCHEMA = {
  type: "object",
  required: ["repo", "state"],
  properties: {
    repo: { type: "string", description: "owner/name of the repository" },
    state: { type: "string", enum: ["open", "closed"] },
    when: { type: "string", format: "date" },
    limit: { type: "integer", minimum: 1 },
    draft: { type: "boolean" },
    labels: { type: "array", items: { type: "string" } },
    kinds: { type: "array", items: { enum: ["bug", "feature"] } },
    since: { type: "object", description: "Time window", properties: { days: { type: "integer" } }, required: ["days"] },
    meta: { type: "object", additionalProperties: { type: "string" } },
  },
};

function Harness({ errors = {} }: { errors?: FieldErrors }) {
  const c = compileSchema(SCHEMA);
  const root = (c as { root: ObjectNode }).root;
  const [draft, setDraft] = useState<Draft>(() => initialDraft(root));
  return (
    <>
      <SchemaForm root={root} draft={draft} errors={errors} onChange={setDraft} />
      <pre data-testid="out">{JSON.stringify(toArguments(root, draft).value)}</pre>
    </>
  );
}

describe("SchemaForm rendering", () => {
  it("renders one control per kind, with required markers, descriptions and format hints", () => {
    renderWithProviders(<Harness />);
    expect(screen.getByRole("textbox", { name: /repo/ })).toBeTruthy();
    expect((screen.getByRole("textbox", { name: /repo/ }) as HTMLInputElement).required).toBe(true);
    expect(screen.getByText("owner/name of the repository")).toBeTruthy();
    expect(screen.getByRole("combobox", { name: /state/ })).toBeTruthy();
    expect(screen.getByText(/Format: date \(e\.g\. 2026-10-08\)/)).toBeTruthy();
    expect(screen.getByRole("combobox", { name: /draft/ })).toBeTruthy(); // optional boolean → tri-state select
    expect(screen.getByRole("group", { name: "kinds" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "feature" })).toBeTruthy();
    expect(screen.getByRole("group", { name: "since" })).toBeTruthy();
    expect(screen.getByText(/Edited as JSON: free-form map of values/)).toBeTruthy();
  });

  it("collapses and expands a nested object group", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness />);
    const group = screen.getByRole("group", { name: "since" });
    const toggle = within(group).getByRole("button", { name: /since/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(within(group).getByRole("textbox", { name: /days/ })).toBeTruthy();
    await user.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(within(group).queryByRole("textbox", { name: /days/ })).toBeNull();
  });

  it("edits flow into typed JSON arguments", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness />);
    await user.type(screen.getByRole("textbox", { name: /repo/ }), "acme/api");
    await user.selectOptions(screen.getByRole("combobox", { name: /state/ }), "closed");
    await user.type(screen.getByRole("textbox", { name: /limit/ }), "25");
    await user.click(screen.getByRole("checkbox", { name: "bug" }));
    await user.type(within(screen.getByRole("group", { name: "since" })).getByRole("textbox", { name: /days/ }), "7");
    expect(JSON.parse(screen.getByTestId("out").textContent ?? "")).toEqual({
      repo: "acme/api",
      state: "closed",
      limit: 25,
      kinds: ["bug"],
      since: { days: 7 },
    });
  });

  it("shows validation errors inline at the field", () => {
    renderWithProviders(<Harness errors={{ repo: "Required.", "since.days": "Must be at least 1." }} />);
    expect(screen.getByText("Required.")).toBeTruthy();
    expect(screen.getByText("Must be at least 1.")).toBeTruthy();
  });

  it("says so when a tool takes no arguments", () => {
    const c = compileSchema({ type: "object", properties: {}, additionalProperties: false }) as { root: ObjectNode };
    renderWithProviders(<SchemaForm root={c.root} draft={{}} errors={{}} onChange={() => {}} />);
    expect(screen.getByText("This tool takes no arguments.")).toBeTruthy();
  });
});
