// HS-U-013: the UI's audit-outcome list must cover every outcome the backend writes,
// or those rows can't be filtered on the Executions page and show a raw badge. The
// backend has no enum for it in openapi.json (outcome is a plain string), so this
// reads the two backend modules that write ExecutionRecord.outcome.
import { describe, expect, it } from "vitest";
import { EXECUTION_OUTCOMES } from "./types";
import { OUTCOME_LABEL } from "../components/common";

const BACKEND = import.meta.glob<string>(["../../../src/mcprouter/execution/manager.py", "../../../src/mcprouter/gateway/skills.py"], {
  query: "?raw",
  import: "default",
  eager: true,
});
const file = (suffix: string) => Object.entries(BACKEND).find(([k]) => k.endsWith(suffix))?.[1];

/** The quoted values of manager.py's outcome-constant block (the lines after its comment, up to a blank line). */
function managerOutcomes(src: string): string[] {
  const at = src.indexOf("# ExecutionRecord.outcome values");
  if (at < 0) return [];
  const block = src.slice(at).split(/\n\s*\n/)[0];
  return [...block.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
}

/** The literal outcome (3rd positional argument) of every record_skill_activation(...) call. */
function skillOutcomes(src: string): string[] {
  return [...src.matchAll(/record_skill_activation\(\s*[^,()]+,\s*[^,()]+,\s*"([a-z_]+)"/g)].map((m) => m[1]);
}

describe("audit outcomes: UI ⊇ backend", () => {
  const manager = file("execution/manager.py");
  const skills = file("gateway/skills.py");

  it("finds both backend modules and parses outcomes from them (guards this test)", () => {
    expect(manager, "src/mcprouter/execution/manager.py not found").toBeTypeOf("string");
    expect(skills, "src/mcprouter/gateway/skills.py not found").toBeTypeOf("string");
    expect(managerOutcomes(manager!)).toEqual(expect.arrayContaining(["ok", "denied", "invalid_args", "unavailable", "cancelled"]));
    expect(skillOutcomes(skills!)).toEqual(expect.arrayContaining(["read", "bundle"]));
  });

  it("every outcome the backend writes is in EXECUTION_OUTCOMES, with a label", () => {
    const backend = new Set([...managerOutcomes(manager!), ...skillOutcomes(skills!)]);
    const ui = new Set<string>(EXECUTION_OUTCOMES);
    expect([...backend].filter((o) => !ui.has(o)), "backend outcomes the UI doesn't know").toEqual([]);
    for (const o of EXECUTION_OUTCOMES) expect(OUTCOME_LABEL[o], o).toBeTruthy();
  });
});
