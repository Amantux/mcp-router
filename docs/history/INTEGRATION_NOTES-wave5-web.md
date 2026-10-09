# Integration notes — wave 5 web (setup wizard finish, W5a2)

Branch `wave5/webserver`, on top of `a1ea193` (first-run wizard at `/setup`).

## What shipped

- **Auto-redirect** (`ui/src/components/Layout.tsx`, `ui/src/pages/setup/redirect.ts`):
  with an admin token present, Layout probes `GET /api/v1/setup/status`; if `needsSetup`
  it routes to `/setup` once per browser session. Marker `mcpr.setup.autoRedirect` in
  sessionStorage (non-secret). **Skip setup** link marks the session `skipped` and clears
  wizard progress.
- **Step (c)** "Add one server" reuses `RegisterServerDialog` with a new `httpsOnly` prop
  (stdio command/args or https URL); paste-import is unchanged.
- **Step (d)** directory skill sources (absolute path, client-checked) next to git (https).
- **Step (e)** starter policy after the agent is created: "Read-only on selected
  servers/sources" posts one `PolicyRule` per pick — tool rule
  `{resourceKind:"tool", serverId:<server id>, toolName:null, maxOperation:"read"}`, skill rule
  `{resourceKind:"skill", serverId:<skill source id>, ...}` — or "I'll configure rules later"
  (no POSTs). Created count is shown. `CreateRuleRequest` gained optional `resourceKind`.
- **Tests**: `Layout.redirect.test.tsx` (3), `pages/setup/SetupPage.test.tsx` (5).

## Decisions

- The redirect marker is claimed only when the status probe *answers*: a wrong/expired
  token does not burn the once-per-session redirect, and React StrictMode's double effect
  cannot swallow it. No storage → never redirect (cannot guarantee "once").
- Rules are created sequentially so a partial failure can report "Created N of M".
- The agent key stays in component memory only; nothing new is written to storage.
- No backend change: `routes_setup.py` already exposed what the wizard needs.

## Verification

- Every mocked request in `SetupPage.test.tsx` is asserted free of the admin token.
  Mutation checks (each failed, then restored): leaking `bearerFor("admin")` into the
  principal POST body; disabling the https-only guard; forcing `resourceKind:"tool"`
  for skill rules; making `redirectClaimed()` always false.

## Residuals

- Directory-path check is client-side shape only; the server remains the authority on
  whether the path exists/is allowed.
- Skill-source ids reuse the `serverId` field per `RuleIn` (wire contract, not a UI choice).

## Review

Reviewer verdict (verbatim): **"Verdict: SHIP WITH FIXES (fix 1 and 2 first; no blockers)."**
Fixed: (1) rules target the server-confirmed `agentId` and the input locks after
creation; (2) "Create rules" disables while in flight and unpicks rules that landed so a retry
cannot duplicate; (3) directory source names are sanitised to the backend pattern.
Open NITs: Windows-style paths pass the client check; paste-import still accepts http://
(server-side policy); the starter-policy target list is loaded once; the redirect drops a
deep link on a fresh install.
