# Haiku fresh-eyes sweep: MCP Router dashboard (ui/)

Lens: what would a user find missing or weird? Read-only sweep. Only this file was written.

Scope notes
- Checked-out branch is `master` (HEAD 73d3bc5), not `main` as requested. Version reads 0.6.0 in pyproject.toml, ui/package.json and the nav; they agree.
- Read: ui/src/routes.tsx, components/Layout.tsx, ConnectPanel.tsx, Notifications.tsx, common.tsx, FeedbackThumbs.tsx, every non-test page in pages/ and pages/setup/SetupPage.tsx, plus api/client.ts error copy. Backend cross-checks used src/mcprouter/settings.py, api/routes_policy.py, api/routes_servers.py, discovery/registry.py, execution/manager.py, api/deps_auth.py, api/app.py.
- Verified: `tsc --noEmit -p tsconfig.app.json --noUnusedLocals --noUnusedParameters` exits 0 (no dead imports or locals in src). Not run: the app itself, the test suite, or a browser pass.

## Verified OK (no action)

- Every MCPR_* name in user-facing copy exists in SETTINGS_SPEC: MCPR_ADMIN_TOKEN, MCPR_DECISION_BACKEND, MCPR_DECISION_ENDPOINT, MCPR_DECISION_API_KEY_FILE, MCPR_AOAI_*, MCPR_DEDUP_MAX_PAIRS, MCPR_PREFILL_MS_PER_1K_TOKENS, MCPR_PRICE_PER_1K_INPUT_TOKENS. MCPR_MAX_EXPOSED_* is a wildcard for the three real MAX_EXPOSED_* vars.
- The 409 first-principal string in ui SetupPage.tsx:85 is identical to routes_policy.py:224-225.
- Approval TTL "10 minutes" matches execution/manager.py:72.
- Health "healthy - database reachable" matches app.py:257-260 (/healthz checks the DB).
- Connect copy about session-only credentials matches api/auth.ts (sessionStorage and memory only).
- No icon-only Button lacks aria-label; no localhost literals in ui/src.

## Findings

| id | severity | file:line | finding | suggested fix |
|---|---|---|---|---|
| HS-U-001 | med | ui/src/components/Layout.tsx:27-40 | The setup wizard (/setup) has no nav entry and no in-app link. After "Skip setup" (pages/setup/redirect.ts:26-33) the only route back is typing the URL. | Add a Setup item to NAV, or a link in the Connect panel. |
| HS-U-002 | med | Layout.tsx:27-40 | Nav order does not follow a setup-to-use workflow. Policy is 12th, after Health and Executions. Approvals sits after Playground. | Reorder to: Servers, Tools, Skill sources, Skills, Policy, Approvals, Agent lens, Playground, Duplicates, Analytics, Executions, Models & health. Route paths stay the same. |
| HS-U-003 | med | Layout.tsx:35 vs pages/PlaygroundPage.tsx:896 | Nav says "Tool playground". The page H1 says "Playground", and the page also hosts a Skills tab. | Use "Playground" in nav, or "Playground" plus a subtitle naming tools and skills. |
| HS-U-004 | low | Layout.tsx:37 vs pages/HealthPage.tsx:134 | Nav "Models & health" vs H1 "Models & system health". | Use one label. |
| HS-U-005 | low | Layout.tsx:32 vs pages/DuplicatesPage.tsx:283 | Nav "Duplicates" vs H1 "Duplicate review". | Use one label. |
| HS-U-006 | med | pages/PolicyPage.tsx:477, 482, 89, 492; pages/LensPage.tsx:417 | The same object is called "principal" (button "Create principal", section "Principals", dialog "Create agent principal", empty state "No agent principals yet", Lens empty state "Create an agent principal"). Everywhere else it is "agent". | Use "agent" in all UI copy. Keep "principal" in the API and code. |
| HS-U-007 | low | pages/LensPage.tsx:127-128; pages/AnalyticsPage.tsx:252 | The fallback ranker has three names: "Deterministic fallback used" (title), "heuristic scorer" (body), "heuristic rankings" (Analytics). | Use "Deterministic fallback" everywhere. |
| HS-U-008 | low | pages/ToolDetailDrawer.tsx:210 vs pages/SkillsPage.tsx:80 | Tools drawer says "Try in playground". Skills drawer says "Try activation". Tools run, skills activate. | Use "Try in playground" in both places. |
| HS-U-009 | med | pages/PlaygroundPage.tsx:214, 216, 648-652 | Skill activation reuses tool copy through ExecutionOutcomeView. Examples: "No policy rule lets this identity run this tool" and "The tool or its server is disabled or offline". | Pass the noun ("tool" or "skill") into ExecutionOutcomeView and parameterize the copy. |
| HS-U-010 | med | pages/PlaygroundPage.tsx:644, 545, 689 | Button and dialog titles interpolate the identity text, giving "Activate as dev mode", "Activate as the admin token", "Run X as the admin token?". | Use a short identity label ("as agent X", "as admin"). Keep the long explanation in RunAsLine. |
| HS-U-011 | low | pages/PlaygroundPage.tsx:90-94 | RunAsLine reads as a fragment: "Runs as admin token - pick an agent below to run as (...)". The dev-mode case also names "dev" without saying it is the audit actor. | Rewrite as "Runs as the admin token. Choose an agent below." |
| HS-U-012 | low | pages/PlaygroundPage.tsx:287-290 | "Audit record <id>" links to /executions with no id filter, so the user cannot find the record they were shown. | Filter by record id, or drop the link and keep the id as text. |
| HS-U-013 | med | pages/ExecutionsPage.tsx:26, 72-73; ui/src/api/types.ts:16; components/common.tsx:118-129 | ExecutionOutcome omits unavailable, invalid_args and cancelled. The manager writes them to audit rows (execution/manager.py:78-79, 417-429, 478). Those rows cannot be filtered and show raw text in the badge. | Extend the type, the OUTCOMES list, OUTCOME_COLOR, and add labels. |
| HS-U-014 | med | pages/ServersPage.tsx:47, 185-189; ui/src/api/client.ts:137 | Deleting a server that policy rules reference returns HTTP 409 with a curated message: "server is referenced by policy rules; remove those rules before deleting it" (discovery/registry.py:177; routes_servers.py:291-292). The toast shows describeError's generic 409 advice instead: "It conflicts with existing data (for example a duplicate name)...". The user is stuck and the advice is wrong. | Surface the curated detail for 409 on delete paths, or add a per-action hint. Keep the message curated, not raw. |
| HS-U-015 | med | pages/SkillSourcesPage.tsx:322-323; pages/PolicyPage.tsx:199-200 | The delete body says "delete or retarget those rules first". EditRuleDialog only edits name, ceiling and approval, so retargeting is impossible in the UI. | Say "delete those rules first on the Policy page." |
| HS-U-016 | med | pages/setup/SetupPage.tsx:126 | "Connect (gear icon, top right)". Connect is at the bottom of the left nav (Layout.tsx:117-122). | Say "bottom of the left menu". |
| HS-U-017 | low | pages/setup/SetupPage.tsx:188-192, 217-222, 240-245, 266-271 | Setup inputs have aria-label and placeholder but no visible label. Placeholder is standing in for the label. | Wrap in Field with a visible label, as the rest of the app does. |
| HS-U-018 | low | pages/setup/SetupPage.tsx:433 | "Next (or skip)" suggests skipping and Next are the same. "Skip setup" is a separate link with a different effect. | Rename to "Next". |
| HS-U-019 | low | pages/setup/SetupPage.tsx:282-286 | If getSetupStatus failed (status null), a dev-mode first-principal 409 is reported as "already exists. Choose another id." | When status is unknown, refetch or show both explanations. |
| HS-U-020 | low | pages/setup/SetupPage.tsx:169-171 | "Admin token configured: true" renders a raw boolean. | "Configured" / "Not configured". |
| HS-U-021 | low | pages/setup/SetupPage.tsx:275; pages/PolicyPage.tsx:47, 124; pages/LensPage.tsx:311 | Default maxTools 8 is hard-coded in four places. | One shared constant. |
| HS-U-022 | low | components/ConnectPanel.tsx:101; ui/src/api/client.ts:123 | Port 8400 is hard-coded in messages. MCPR_PORT is configurable (settings.py:501). | Drop the port, or derive it from window.location. |
| HS-U-023 | low | components/ConnectPanel.tsx:193-205 | "Disconnect" clears the admin token as well as the agent key, and the label does not say so. "Forget agent key" sits beside it. | "Disconnect (clear admin token and agent key)". |
| HS-U-024 | low | components/ConnectPanel.tsx:166, 175; pages/setup/SetupPage.tsx:380 | Credential naming drifts: "Agent key", "agent API key", "API key", "agent-key" placeholder in snippets. | Pick "agent API key" and use it everywhere. |
| HS-U-025 | med | pages/PolicyPage.tsx:493 | Empty state: "Until keys exist, gateway auth is disabled (dev mode only)." Per deps_auth.py:17-19 dev mode needs no agent keys AND no admin token AND no principals. Creating a key does not end it by itself. | "Dev mode is on only while no admin token, agent keys, or agents are configured. Creating an agent turns it off." |
| HS-U-026 | low | pages/PolicyPage.tsx:612 | "No allow rules - every agent is currently denied all tools." gives no next step. | Add "Add a rule above to allow access." |
| HS-U-027 | low | pages/PlaygroundPage.tsx:751, 838 | "Add a skill source first." and "Register a server first." have no link. The Tools and Skills pages do have one. | Link to /skill-sources and /servers. |
| HS-U-028 | low | pages/ToolsPage.tsx:152 vs pages/ToolDetailDrawer.tsx:94 | Funnel column is 7d ("Funnel (7d)"). The drawer defaults to 30d. | Align the default, or label the window in the drawer. |
| HS-U-029 | low | pages/ToolDetailDrawer.tsx:130-137 | Co-surfaced column headers "Together", "This picked", "Other picked" are unclear. | "Shown together", "Picked here", "Picked elsewhere". |
| HS-U-030 | low | pages/ToolDetailDrawer.tsx:105, 109, 116-119; pages/ToolsPage.tsx:152 | Jargon with no gloss: "surfaced", "Context spent (tokens)", "Avg rank", "Selection rate". | Add a one-line glossary under the Funnel tab. |
| HS-U-031 | low | pages/ToolDetailDrawer.tsx:203; pages/ToolsPage.tsx:111; pages/PolicyPage.tsx:634; pages/ExecutionsPage.tsx:117-118 | Raw server ids (UUID or 8-char prefix) show when the name is missing. | Show "Unknown server" with a copy action. |
| HS-U-032 | low | pages/ToolDetailDrawer.tsx:107, 226; pages/SkillsPage.tsx:93, 115, 142 | Load-failure copy ("Tool details couldn't be loaded.") offers no Retry. ErrorState does. | Use ErrorState with Retry in drawers. |
| HS-U-033 | low | pages/ToolsPage.tsx:146, 149 | Headers "Ver." and "Avg latency" use abbreviations. | "Version", "Average latency". |
| HS-U-034 | low | pages/ToolsPage.tsx:138-142 vs 202-212 | State badge says "active". The filter says "Enabled". Badge "unavailable" vs filter "Availability". | Use "enabled" in the badge too. |
| HS-U-035 | low | pages/ToolsPage.tsx:238 | Empty state: "...run tools/list again". Protocol jargon. | "Refresh the server to rediscover its tools." |
| HS-U-036 | med | pages/SkillsPage.tsx:33, 40 | Ingest flags (secret-like, oversize, body truncated, resource oversize) show as chips with no explanation. secret-like is red. | Add a title or tooltip per flag saying what it means and what to do. |
| HS-U-037 | low | pages/SkillsPage.tsx:236-237, 277; pages/ToolsPage.tsx:118-120; pages/ClassificationEditor.tsx:96 | The same concept (read, write, execute) is "Risk class" and "Risk" for skills, but "Operation" for tools and classification. | Use "Operation" everywhere. |
| HS-U-038 | low | pages/DuplicatesPage.tsx:306 | "compares ... capability classes". "Capability" appears nowhere else. | "Compares descriptions, input schemas, domains and operations." |
| HS-U-039 | low | pages/SkillsPage.tsx:197 | Help text hard-codes "~/.claude/skills". Other clients differ. | Keep the generic wording; drop the path. |
| HS-U-040 | low | pages/AnalyticsPage.tsx:221 | Empty state exposes internals: "Every /route call and MCP tools/list exposure is counted". | "Every routing decision and tool listing is counted." |
| HS-U-041 | low | pages/AnalyticsPage.tsx:342 | "No tool was surfaced in this window." also shows when the Kind filter is Skills. | Make the noun depend on the filter. |
| HS-U-042 | low | pages/AnalyticsPage.tsx:254, 272 | "Routing latency" and "Measured route latency" appear side by side. The source of the first is unclear. | Rename the first, or merge. |
| HS-U-043 | low | pages/AnalyticsPage.tsx:239 | Savings sub-text is a dense run-on that ends with the raw estimator id (e.estimator). | Split into a caption; show the estimator name in words. |
| HS-U-044 | low | pages/LensPage.tsx:378 | Hint: "Routing runs under this agent's policy and budgets. Nothing is exposed to it." Reads as contradictory. | "Dry run. Nothing is sent to the agent." |
| HS-U-045 | low | pages/LensPage.tsx:392 | "Ask for more than the caps allow to see the clamp." "clamp" is never explained. | "Ask for more than the caps allow to see where a cap applies." |
| HS-U-046 | low | pages/LensPage.tsx:62 | BY_LABEL shows the env wildcard "MCPR_MAX_EXPOSED_*" to every viewer, including non-admins. | Name the exact variable only in admin-facing text. |
| HS-U-047 | low | pages/LensPage.tsx:265, 285 | Backend-speak "the backend didn't report filter diagnostics". Raw stage ids, with "policy" as the fallback. | "No filter details are available for this run." Map stage ids to labels. |
| HS-U-048 | low | pages/ApprovalsPage.tsx:52 | Toast "Approved X for agent - executed" shows a raw status enum. | Map to a label such as "Approved and ran". |
| HS-U-049 | low | pages/SkillSourcesPage.tsx:279 | Status column shows a raw enum with no legend. | Map to labels. |
| HS-U-050 | low | pages/SkillSourcesPage.tsx:271 | The sync report renders inside the name cell and stretches the row. | Move it to an expandable row or a toast. |
| HS-U-051 | low | pages/ServersPage.tsx:24; pages/RegisterServerDialog.tsx:134-136 | Transport labels are inconsistent: lowercase "stdio", "Streamable HTTP", "SSE (legacy)". | Consistent casing, with stdio shown as "Local process (stdio)" in the table. |
| HS-U-052 | low | pages/ExecutionsPage.tsx:122 | The "admin" badge explains itself only in a title tooltip, which keyboard and touch users cannot reach. | Visible caption, or aria-describedby. |
| HS-U-053 | low | pages/LensPage.tsx:210, 259; pages/setup/SetupPage.tsx:305; pages/PlaygroundPage.tsx:670 | aria-label on a plain div or pre with no role is not announced by screen readers. | Use section with a heading, or role="region". |
| HS-U-054 | low | pages/PlaygroundPage.tsx:670 | The untrusted skill body is in a pre with the muted (tertiary-foreground) colour. Long low-contrast text. | Use the default foreground colour. |
| HS-U-055 | low | pages/PlaygroundPage.tsx:195 | "It was re-checked against current policy and schema, or the upstream call failed." Ambiguous "or". | Name each case separately. |
| HS-U-056 | low | pages/PolicyPage.tsx:521 | Principal switch label is visible ("enabled"/"disabled"). Servers and Sources switches have no visible label. | Make the three switches consistent. |
| HS-U-057 | low | pages/DuplicatesPage.tsx:31-39 | Imports and a const are declared mid-file, after a function. Compiles, but hard to read. | Move to the top of the file. |
| HS-U-058 | low | pages/ClassificationEditor.tsx:35 | `as unknown as` cast hides a mismatched default save signature. | Type the default properly. |
| HS-U-059 | low | pages/DuplicatesPage.tsx:270 | Toast tells every viewer to raise MCPR_DEDUP_MAX_PAIRS, an operator setting. | "The scan hit its pair limit, so some pairs were not compared." |
| HS-U-060 | low | ui/src/api/client.ts:119 | Fallback copy "Something unexpected went wrong in the dashboard. Reload the page and try again." gives no detail. | Keep it, but add "If it repeats, check the server log." |

## If I could change one thing

Fix the error path for refused deletes (HS-U-014). This is the one place where a user does something normal, gets refused by a policy rule, and receives a message that is both generic and wrong ("duplicate name", "change the input"). The backend already sends the exact next step, and describeError drops it. Pass the curated 409 detail through for the delete actions. It is a small change, and it removes the most common dead end in the app. Vocabulary drift (agent vs principal, Playground vs Tool playground, deterministic vs heuristic) is the second priority; it is cheap to fix and affects every page.
