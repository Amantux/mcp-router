# Integration notes — wave 2 UI (wave2/ui)

Every new call goes through `ui/src/api/client.ts`, with wire types in
`ui/src/api/types.ts`. The guesses are marked `// CONTRACT:` in those two
files and listed below. The wave-1 rules still hold: responses are camelised
client-side, request bodies are camelCase, and error bodies are never shown.

## 1. Admin auth (closes integration gap 2)

- Connect drawer (gear icon at the bottom of the nav). It takes the admin
  token and, optionally, an agent key. Both are stored in memory and in
  `sessionStorage` (`mcpr.adminToken`, `mcpr.agentKey`). They are never put
  in `localStorage` or cookies. `fetch` uses `credentials: "omit"`.
- `client.request()` sends `Authorization: Bearer <admin token>` by default.
  Calls made with `as: "agent"` (`GET /me`, `/me/approvals/{id}`, and tool
  execution) send the agent key when one is set. Without one they fall back
  to the admin token.
- A 401 or 403 on an **admin** request sets the global state to "rejected".
  One `AuthBanner` ("Not connected", with an Open Connect button) replaces the
  per-request error toasts. A 2xx flips the state back. A 403 on an agent
  request is a policy answer and does not disconnect. A 401 on an agent
  request marks only the agent key as refused.
- `/healthz` is called with `public: true`: no bearer is sent, and the
  response doesn't change the connection state.
- When credentials change, pages remount through `<Outlet key={epoch}>` and
  refetch.
- Save and verify calls `GET /principals` to verify the admin token and
  `GET /me` to verify the agent key. `/me` also returns the agent id, which
  the playground uses to say who a run executes as.
- Mutation evidence (`ui/src/api/auth.test.tsx`):

  | Mutation | Failing tests |
  |---|---|
  | Header injection removed | "sends the admin token as Authorization: Bearer on every admin request", "presents the agent key…", "uses password inputs…" |
  | `sessionStorage` → `localStorage` | "keeps credentials in sessionStorage only — localStorage and cookies stay empty", "clearing a credential removes it from sessionStorage" |
  | `noteResponse` attributed at response time instead of send time | "attributes a response to the credential presented when the request was sent" |
| `noteResponse` (the 401 flip) removed | "shows the not-connected bar…", "treats 403…", "a later 2xx admin response reconnects" |

## 2. Endpoint contracts

| # | Call | Status |
|---|---|---|
| W1 | `POST /api/v1/tools/{id}/execute` `{arguments}` → `{status, detail, recordId, approvalId?, errors[], result?{content[], isError, structuredContent?}, latencyMs?}` | **CONTRACT: no such endpoint exists in v0.1.** Execution today runs only through `/mcp` `tools/call` and the approval endpoint. The shape mirrors `ExecutionManager.ExecutionResult`. **Needed:** a thin REST route over `ExecutionManager.execute` for the principal behind the bearer. Decide what an admin-token call runs as (reject with 403/422, or take an explicit `agentId`). The UI says "admin token" and lets the backend decide. Policy denial etc. should be **200 with a status**, not 4xx. Without `latencyMs` the UI shows only the round-trip time it measures in the browser. |
| W2 | `GET /api/v1/me`, `GET /api/v1/me/approvals/{id}`, `GET /api/v1/approvals?status=`, `POST /api/v1/approvals/{id}/approve\|deny` | Verified against v0.1 `routes_policy.py`. There is no admin get-by-id endpoint, so the admin poll lists approvals and filters by id. |
| W3 | `POST /api/v1/route/simulate` `{agentId, query, maxTools?, maxServers?}` (admin) | **Partly aligned with A's branch.** A's `routing/budgets.py` and the `RouteResponse` on A's branch confirm `maxToolsApplied`, `maxServersApplied` and `BudgetClamp {budget, requested, principal, globalCap, applied, clampedBy}`. The simulate route itself was not written when this was checked, and A's notes file did not exist yet. **CONTRACT guesses:** `clamps[]`, then `diagnostics.{candidates, stages[{stage,before,after}], policyFiltered[{server,tool,reason,stage?}]}`. `normaliseSimulation` reads each of these from the top level or from `diagnostics`, and accepts `{server,tool}` or `{serverName,toolName}`. Anything missing renders as absent and does not break the page. |
| W4 | Analytics: `GET /api/v1/analytics/{overview,tools,tools/{id},agents,suggestions}?window=7d\|30d\|90d` | **Matches B's published `INTEGRATION_NOTES-wave2-analytics.md` §7 field for field.** I checked this after hand-off. All rates are fractions or null. `/tools` takes `sort`, `order`, `limit` (≤500) and `offset`. `/suggestions` is called with the default thresholds. `catalogDrift` keys are camelised (not opaque). |

New opaque keys (passed through camelisation untouched): `arguments`,
`content`, `structuredContent`, `summary`. These hold tool data and redacted
approval summaries.

## 3. Pages

- `/playground`: server → searchable tool picker → a form generated from
  `inputSchema` (`ui/src/lib/schemaForm`), with a two-way raw-JSON toggle.
  Tools that are not read-only (write, execute, unknown) need a confirmation
  that names the tool and the identity. All outcomes render. A
  `pending_approval` run polls every 3 s until it reaches a terminal state,
  then stops. Deep link: `/playground?tool=<id>`. The tool drawer has a "Try
  in playground" link.
- `/approvals`: an admin list with redacted arguments. Approve and Deny each
  have a confirmation that names the tool and agent. The list polls every
  10 s while visible.
- `/lens`: replaces `/simulator`, which now redirects to `/lens`.
  `SimulatorPage` and its test were removed. `client.simulateRoute` (the
  agent-facing `/route`) and its client test remain.
- `/analytics`: wasted exposure comes first, then headline cards, position
  bias, funnel, tools, agents, and staleness. The tools table has a
  best-effort "Funnel (7d)" column; it fails silently if analytics is not
  installed. The tool drawer has a Funnel tab.

## 4. schemaForm coverage

The form handles string, number, integer, boolean (a switch when required, a
three-state select when optional), `enum`/`const` (a select), arrays of
scalars (one item per line), arrays of enums (checkboxes), nested objects
(collapsible and recursive), local `$ref`/`$defs`/`definitions`, and nullable
unions (`type:[X,"null"]`, `anyOf:[X,null]`).

These become a raw-JSON field, with a reason shown: `oneOf`/`allOf`/`not`/`if`,
unions of more than one type, arrays of objects or arrays, tuples, free-form
maps, recursive/remote/broken refs, `{}`, and non-scalar enums. A root that is
not an object makes the whole tool raw-only.

JSON → form refuses unknown keys, type mismatches, empty strings, empty
lists or groups, list items containing newlines or leading/trailing spaces,
and repeated enum items. Each of these would be lost or changed by the form,
so the arguments stay raw and the notice names the reason.

## 5. Flags for the integrator

1. **W1 is the critical gap.** The playground can't execute anything until a
   REST execute route exists.
2. W3 diagnostics are guesses. Reconcile `normaliseSimulation` with A's final
   shape.
3. The admin `getApproval` goes through the list endpoint. An admin
   `GET /approvals/{id}` would be cheaper.
4. Not visually verified in a browser against a live backend. Only jsdom
   tests and the build ran.

## 6. Review

A reviewer subagent found 0 blockers, 4 should-fix and 4 nits.

**Fixed** (see the `fix(ui): address review` commit):

- Optional-group seeding.
- `[]` in an optional enum list.
- The approval poll on a 404/401.
- Agent-key error advice.
- Send-time auth attribution.
- Stale re-query on the lens.
- Opaque `catalogDrift` keys.
- A flaky dialog test.

**Left as-is:** switching form → JSON drops number inputs that don't parse,
and nothing tells the user. This is by design ("form → JSON always works") and
noted in a code comment.

## Gates (final)

`npm run build` OK · `npm run lint` clean · `vitest run`: 10 files, 90 tests
passed (26 at the start of wave 2).
