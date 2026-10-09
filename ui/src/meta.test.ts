// MT-4: the UI's structural meta-tests. Each fails when a page, a client export or a
// backend contract drifts, instead of waiting for a user to find it:
//   1. NAV <-> routes <-> pages <-> <Page>.test.tsx parity;
//   2. every api/client export is exercised by a test (by name, or by a mocked
//      route its request matches) or allow-listed with a reason;
//   3. every client request (method + path, static query keys) exists in
//      docs/reference/openapi.json; response fields the UI's types rely on exist;
//      (runtime: the strict mockFetch checks every request a test makes, including
//      JSON body keys, against the same document — see test/render.tsx);
//   4. every `// CONTRACT:` comment names the test that verifies it;
//   5. the accessible-name guard (test/a11y.ts, run after every test) is live.
import { describe, expect, it } from "vitest";
import { NAV } from "./components/Layout";
import { router } from "./routes";
import { OPENAPI, findOperation, objectKeys, responseSchema } from "./test/contract";
import { unnamedControls } from "./test/a11y";

const SOURCES = import.meta.glob<string>(["./**/*.{ts,tsx}", "!./**/*.d.ts"], { query: "?raw", import: "default", eager: true });
const isTest = (f: string) => /\.test\.tsx?$/.test(f);
const TEST_FILES = Object.keys(SOURCES).filter(isTest);
const base = (f: string) => f.split("/").pop()!;
/** Test sources with the `${API}` shorthand some files use expanded, so mock keys read literally. */
const TEST_TEXT = TEST_FILES.filter((f) => f !== "./meta.test.ts")
  .map((f) => SOURCES[f].replaceAll("${API}", "/api/v1"))
  .join("\n");

// ------------------------------------------------------------------ 1. pages
const PAGE_FILES = Object.keys(SOURCES).filter((f) => /^\.\/pages\/(.+\/)?\w+Page\.tsx$/.test(f));
/** Pages exempt from the <Page>.test.tsx rule. Must stay empty unless a reason is written here. */
const PAGE_TEST_ALLOW: Record<string, string> = {};

describe("MT-4 pages and routes", () => {
  const children = router.routes[0].children ?? [];

  it("every NAV entry has a route with a defined element", () => {
    for (const n of NAV) {
      const r = children.find((c) => c.path === n.to.slice(1));
      expect(r, `NAV ${n.to} has no route`).toBeTruthy();
      expect(r!.element, `route ${n.to} renders nothing (missing PAGES entry?)`).toBeTruthy();
    }
  });

  it("every routed path is in NAV, or is one of the known non-NAV routes", () => {
    const NON_NAV = new Set([undefined, "*", "setup", "simulator"]); // index, catch-all, wizard, legacy redirect
    const navPaths = new Set(NAV.map((n) => n.to.slice(1)));
    for (const c of children) if (!NON_NAV.has(c.path)) expect(navPaths.has(c.path!), `route /${c.path} is not in NAV`).toBe(true);
  });

  it("every NAV label is its page's title (HS-U-003/004/005: one name per page)", () => {
    const routes = SOURCES["./routes.tsx"];
    for (const n of NAV) {
      // routes.tsx maps the path to <XPage />; that page's PageHeader title must equal the label.
      const page = new RegExp(`"${n.to}":\\s*(?:page\\()?<(\\w+)`).exec(routes)?.[1];
      expect(page, `no page for ${n.to} in routes.tsx`).toBeTruthy();
      const src = PAGE_FILES.map((f) => SOURCES[f]).find((t) => new RegExp(`export function ${page}\\b`).test(t)) ?? "";
      expect(src, `${page} title`).toContain(`title="${n.label}"`);
    }
  });

  it("every page component is routed and has its own test file", () => {
    expect(PAGE_FILES.length).toBeGreaterThan(10);
    const routes = SOURCES["./routes.tsx"];
    for (const f of PAGE_FILES) {
      const name = base(f).replace(/\.tsx$/, "");
      expect(routes, `${name} is never routed in routes.tsx`).toMatch(new RegExp(`\\b${name}\\b`));
      if (PAGE_TEST_ALLOW[name]) continue;
      expect(SOURCES[f.replace(/\.tsx$/, ".test.tsx")], `${f} has no ${name}.test.tsx`).toBeTypeOf("string");
    }
    for (const name of Object.keys(PAGE_TEST_ALLOW))
      expect(PAGE_FILES.some((f) => SOURCES[f.replace(/\.tsx$/, ".test.tsx")] === undefined && base(f) === `${name}.tsx`), `stale PAGE_TEST_ALLOW entry ${name}`).toBe(true);
  });
});

// ------------------------------------------------------------ 2+3. the client
const CLIENT = SOURCES["./api/client.ts"];
const PARAM = "\u0000";

interface ClientRequest {
  method: string;
  /** Concrete path with PARAM for each interpolation, one entry per ternary branch. */
  paths: string[];
  query: string[];
}
interface ClientExport {
  name: string;
  kind: "function" | "const" | "class";
  requests: ClientRequest[];
}

/** `${API_BASE}/tools/${encodeURIComponent(id)}/${on ? "enable" : "disable"}` -> concrete alternatives. */
function expandTemplate(t: string): string[] {
  const s = t.replaceAll("${API_BASE}", "/api/v1").replaceAll("${ANALYTICS}", "/api/v1/analytics");
  const tern = /\$\{[^}?]*\?\s*"([^"]*)"\s*:\s*"([^"]*)"\s*\}/.exec(s);
  if (tern) return [tern[1], tern[2]].flatMap((alt) => expandTemplate(s.slice(0, tern.index) + alt + s.slice(tern.index + tern[0].length)));
  return [s.replace(/\$\{[^}]*\}/g, PARAM)];
}

function parseClient(src: string): ClientExport[] {
  // Top-level declarations start at column 0; a body runs to the next one.
  const decl = /^(export\s+)?(async\s+)?(function|const|class|interface|type)\s+(\w+)/gm;
  const marks: { at: number; exported: boolean; kind: string; name: string }[] = [];
  for (let m; (m = decl.exec(src)); ) marks.push({ at: m.index, exported: !!m[1], kind: m[3], name: m[4] });
  const out: ClientExport[] = [];
  marks.forEach((d, i) => {
    if (!d.exported || d.kind === "interface" || d.kind === "type") return;
    const body = src.slice(d.at, marks[i + 1]?.at ?? src.length);
    const requests: ClientRequest[] = [];
    const call = /request(?:<[^(]*?>)?\(\s*"(GET|POST|PATCH|DELETE|PUT)",\s*(?:`([^`]*)`|"([^"]*)")/g;
    for (let m; (m = call.exec(body)); ) requests.push({ method: m[1], paths: expandTemplate(m[2] ?? m[3]), query: [] });
    const raw = /requestRaw\(\s*`([^`]*)`/g;
    for (let m; (m = raw.exec(body)); ) requests.push({ method: "GET", paths: expandTemplate(m[1]), query: [] });
    // Static query keys: `query: { a: x, b }` or requestRaw(path, { a }).
    const q = /(?:query:\s*|requestRaw\(\s*`[^`]*`,\s*)\{([^}]*)\}/.exec(body);
    if (q && requests.length === 1)
      requests[0].query = q[1]
        .split(",")
        .map((p) => /^\s*(\w+)\s*(?::|$)/.exec(p)?.[1])
        .filter((k): k is string => !!k);
    out.push({ name: d.name, kind: d.kind as ClientExport["kind"], requests });
  });
  return out;
}

const EXPORTS = parseClient(CLIENT);

/** Client exports that no test exercises directly, each with the reason that is acceptable. */
const CLIENT_EXPORT_ALLOW: Record<string, string> = {
  API_BASE: "path prefix constant; every request test exercises it",
  METRICS_URL: "a link target (HealthPage), not a request",
  isAbort: "predicate used inside useLoader/ApprovalTracker; abort paths are covered by the useLoader supersede test",
  isAdminAuthError: "predicate used by Notifications; 401/403 suppression is covered by auth.test.tsx",
  normaliseTool: "applied inside getTool/listTools, which are tested",
  mapModelsHealth: "applied inside getModelsHealth, which is tested",
};

const MOCKED = [...TEST_TEXT.matchAll(/\b(GET|POST|PATCH|DELETE|PUT) (\/[^\s"'`]*)/g)].map((m) => `${m[1]} ${m[2]}`);
const pathRegex = (p: string) => new RegExp(`^${p.split(PARAM).map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("[^/\\s\"'`]+")}$`);

function exercised(e: ClientExport): boolean {
  if (new RegExp(`\\b${e.name}\\b`).test(TEST_TEXT)) return true;
  return (
    e.requests.length > 0 &&
    e.requests.every((r) => r.paths.every((p) => MOCKED.some((m) => pathRegex(`${r.method} ${p}`).test(m))))
  );
}

describe("MT-4 api/client exports", () => {
  it("the client parser sees the request table (guards this meta-test itself)", () => {
    const byName = Object.fromEntries(EXPORTS.map((e) => [e.name, e]));
    expect(EXPORTS.length).toBeGreaterThan(50);
    expect(byName.listSkills.requests[0]).toMatchObject({ method: "GET", paths: ["/api/v1/skills"] });
    expect(byName.listSkills.requests[0].query).toContain("sourceId");
    expect(byName.setToolEnabled.requests[0].paths).toEqual([`/api/v1/tools/${PARAM}/enable`, `/api/v1/tools/${PARAM}/disable`]);
    expect(byName.downloadSkillBundle.requests[0].query).toEqual(["agentId"]);
  });

  it("every export is exercised by a test, or allow-listed with a reason", () => {
    const missing = EXPORTS.filter((e) => !CLIENT_EXPORT_ALLOW[e.name] && !exercised(e)).map((e) => e.name);
    expect(missing, "client exports no test exercises (test them, or allow-list with a reason)").toEqual([]);
  });

  it("the allow-list is not stale", () => {
    const names = new Set(EXPORTS.map((e) => e.name));
    for (const n of Object.keys(CLIENT_EXPORT_ALLOW)) {
      expect(names.has(n), `CLIENT_EXPORT_ALLOW names a non-export: ${n}`).toBe(true);
      expect(exercised(EXPORTS.find((e) => e.name === n)!), `${n} is now exercised by a test: drop it from CLIENT_EXPORT_ALLOW`).toBe(false);
    }
  });
});

// Response fields the UI's types depend on. A `// CONTRACT:` comment in types.ts
// names one of these ids as its proof.
const RESPONSE_CONTRACTS: { id: string; op: string; steps: string[] }[] = [
  { id: "page.envelope", op: "GET /api/v1/tools", steps: ["total"] },
  { id: "servers.toolCount", op: "GET /api/v1/servers", steps: ["[]", "toolCount"] },
  { id: "tools.serverName", op: "GET /api/v1/tools/{tool_id}", steps: ["serverName"] },
  { id: "tools.available", op: "GET /api/v1/tools/{tool_id}", steps: ["available"] },
  { id: "tools.stats", op: "GET /api/v1/tools/{tool_id}", steps: ["stats", "callCount"] },
  { id: "tools.versions", op: "GET /api/v1/tools/{tool_id}", steps: ["versions", "[]", "changeKind"] },
  { id: "classification.returnsTool", op: "PATCH /api/v1/tools/{tool_id}/classification", steps: ["classificationReviewed"] },
  { id: "dedup.embedsTools", op: "GET /api/v1/dedup/suggestions", steps: ["items", "[]", "toolA", "id"] },
  { id: "dedup.kinds", op: "GET /api/v1/dedup/suggestions", steps: ["items", "[]", "kindA"] },
  { id: "executions.names", op: "GET /api/v1/executions", steps: ["items", "[]", "toolName"] },
  { id: "feedback.result", op: "POST /api/v1/route/{request_id}/feedback", steps: ["recorded"] },
  { id: "principals.apiKey", op: "POST /api/v1/principals", steps: ["apiKey"] },
  { id: "simulate.policyFiltered", op: "POST /api/v1/route/simulate", steps: ["diagnostics", "policyFiltered", "[]", "reason"] },
  { id: "simulate.stages", op: "POST /api/v1/route/simulate", steps: ["diagnostics", "stages", "[]", "after"] },
  { id: "overview.feedback", op: "GET /api/v1/analytics/overview", steps: ["feedback", "helpfulRate"] },
  { id: "funnel.feedback", op: "GET /api/v1/analytics/tools", steps: ["items", "[]", "feedbackUnhelpful"] },
  { id: "wasted.kind", op: "GET /api/v1/analytics/suggestions", steps: ["wastedExposure", "[]", "kind"] },
  { id: "wasted.unhelpful", op: "GET /api/v1/analytics/suggestions", steps: ["wastedExposure", "[]", "unhelpful"] },
  { id: "activate.result", op: "POST /api/v1/skills/{skill_id}/activate", steps: ["recordId"] },
];

describe("MT-4 client <-> OpenAPI contract", () => {
  it("docs/reference/openapi.json is present (the backend commits it; scripts/gen_openapi.py)", () => {
    expect(OPENAPI, "docs/reference/openapi.json is missing: every contract check below and in mockFetch is off").toBeTruthy();
  });

  it("every client request is an operation in the OpenAPI document, with declared query keys", () => {
    const problems: string[] = [];
    for (const e of EXPORTS)
      for (const r of e.requests)
        for (const p of r.paths) {
          const concrete = p.replaceAll(PARAM, "x");
          const hit = OPENAPI && findOperation(OPENAPI, r.method, concrete);
          if (!hit) {
            problems.push(`${e.name}: ${r.method} ${p.replaceAll(PARAM, "{…}")} is not in openapi.json`);
            continue;
          }
          const declared = new Set((hit.op.parameters ?? []).filter((x) => x.in === "query").map((x) => x.name));
          for (const k of r.query) if (!declared.has(k)) problems.push(`${e.name}: query key "${k}" is not declared on ${r.method} ${hit.template}`);
        }
    expect(problems).toEqual([]);
  });

  it.each(RESPONSE_CONTRACTS)("response contract: $id", ({ op, steps }) => {
    const [method, template] = op.split(" ");
    expect(OPENAPI?.paths[template]?.[method.toLowerCase()], `${op} is not in openapi.json`).toBeTruthy();
    expect(responseSchema(OPENAPI!, method, template, steps), `${op} response has no ${steps.join(".")}`).toBeTruthy();
  });

  it("objectKeys reads request schemas through $ref and nullable anyOf (guards the runtime check)", () => {
    const accept = OPENAPI!.paths["/api/v1/dedup/suggestions/{suggestion_id}/accept"].post.requestBody?.content?.["application/json"]?.schema;
    expect([...(objectKeys(OPENAPI!, accept) ?? [])]).toEqual(["preferredToolId"]);
  });
});

// ------------------------------------------------------------- 4. CONTRACT
describe("MT-4 CONTRACT comments", () => {
  it("every CONTRACT comment names the test that verifies it", () => {
    const problems: string[] = [];
    for (const [f, src] of Object.entries(SOURCES)) {
      if (isTest(f)) continue;
      src.split("\n").forEach((line, i) => {
        if (!/\/\/\s*CONTRACT\b/.test(line)) return;
        const v = /verified: (\S+\.test\.tsx?) › (.+?)\s*$/.exec(line);
        if (!v) return problems.push(`${f}:${i + 1} has no "verified: <file>.test.ts(x) › <test title or contract id>"`);
        // import.meta.glob leaves out the importing file, so this file's ids are checked directly.
        if (v[1] === "meta.test.ts") {
          if (!RESPONSE_CONTRACTS.some((c) => c.id === v[2])) problems.push(`${f}:${i + 1}: no response contract "${v[2]}" in meta.test.ts`);
          return;
        }
        const file = TEST_FILES.find((t) => base(t) === v[1]);
        if (!file) return problems.push(`${f}:${i + 1} names a missing test file ${v[1]}`);
        if (!SOURCES[file].includes(`"${v[2]}"`)) problems.push(`${f}:${i + 1}: ${v[1]} has no test or contract id "${v[2]}"`);
      });
    }
    expect(problems).toEqual([]);
  });
});

// ----------------------------------------------------------------- 5. a11y
describe("MT-4 accessible names", () => {
  it("the after-each guard flags an unnamed control and accepts a named one", () => {
    const box = document.createElement("div");
    box.innerHTML = '<button type="button"><svg></svg></button><button type="button" aria-label="Close"></button><input aria-label="Name" /><input />';
    expect(unnamedControls(box)).toEqual(['<button>', '<input>']);
  });
});
