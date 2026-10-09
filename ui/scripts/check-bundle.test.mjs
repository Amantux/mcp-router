import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { afterEach, describe, expect, it } from "vitest";
import { checkBundle } from "./check-bundle.mjs";

// vitest runs from ui/ (the jsdom environment gives import.meta.url a non-file scheme).
const SCRIPT = join(process.cwd(), "scripts", "check-bundle.mjs");
let dir;
const make = (sizes) => {
  dir = mkdtempSync(join(tmpdir(), "bundle-"));
  for (const [name, bytes] of Object.entries(sizes)) writeFileSync(join(dir, name), "x".repeat(bytes));
  return dir;
};
afterEach(() => dir && rmSync(dir, { recursive: true, force: true }));

describe("check-bundle", () => {
  const budgets = { index: 100, fluent: 200 };

  it("passes within budget", () => {
    expect(checkBundle(make({ "index-a.js": 100, "fluent-b.js": 150, "x.css": 9999 }), budgets)).toEqual([]);
  });

  it("fails when index is over budget, and when a budgeted chunk is missing", () => {
    const problems = checkBundle(make({ "index-a.js": 101 }), budgets);
    expect(problems).toHaveLength(2);
    expect(problems[0]).toMatch(/^index-a\.js: .* > budget/);
    expect(problems[1]).toMatch(/^fluent: no fluent-\*\.js chunk/);
  });

  it("the CLI exits 1 over budget", () => {
    const over = make({ "index-a.js": 500 * 1024, "fluent-b.js": 1, "react-c.js": 1 });
    const r = spawnSync(process.execPath, [SCRIPT, over], { encoding: "utf8" });
    expect(r.status).toBe(1);
    expect(r.stderr).toMatch(/index-a\.js: 500\.0 KiB > budget/);
  });
});
