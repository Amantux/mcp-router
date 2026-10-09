// Per-chunk size budgets for the production build (P-411). Run after `npm run build`:
//   npm run check:bundle            (checks ./dist/assets)
//   node scripts/check-bundle.mjs <assetsDir>
// Exits 1 naming every chunk over budget. Raise a budget only with a reason in the
// commit; the point is that growth is a decision, not an accident.
import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

/** Chunk name prefix -> max bytes. `index` is the first-paint entry chunk. */
export const BUDGETS = {
  index: 135 * 1024,
  fluent: 720 * 1024,
  react: 100 * 1024,
};

/** Returns one message per chunk over budget, plus one per budgeted chunk that is missing. */
export function checkBundle(assetsDir, budgets = BUDGETS) {
  const files = readdirSync(assetsDir).filter((f) => f.endsWith(".js"));
  const problems = [];
  for (const [prefix, max] of Object.entries(budgets)) {
    const hits = files.filter((f) => f.startsWith(`${prefix}-`));
    if (hits.length === 0) {
      problems.push(`${prefix}: no ${prefix}-*.js chunk in ${assetsDir} (renamed? update the budget)`);
      continue;
    }
    for (const f of hits) {
      const size = statSync(join(assetsDir, f)).size;
      if (size > max) problems.push(`${f}: ${(size / 1024).toFixed(1)} KiB > budget ${(max / 1024).toFixed(0)} KiB`);
    }
  }
  return problems;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const dir = process.argv[2] ?? join(fileURLToPath(new URL("..", import.meta.url)), "dist", "assets");
  const problems = checkBundle(dir);
  if (problems.length) {
    console.error(`Bundle over budget:\n- ${problems.join("\n- ")}`);
    process.exit(1);
  }
  console.log(`Bundle within budget (${Object.keys(BUDGETS).join(", ")}).`);
}
