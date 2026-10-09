# Security policy

## Supported versions

Only the latest release is supported. Security fixes land on the default
branch (`main`) and ship in the next release; there are no backports. Before
reporting, check that the issue reproduces on the latest release or on `main`.

## Reporting a vulnerability

**Do not open a public issue, pull request or discussion for a
vulnerability.** Report it privately through this repository's
**Security** tab → **Report a vulnerability** (GitHub private vulnerability
reporting). If that form is unavailable, open a minimal public issue asking
for a private contact and leave out every detail of the problem.

Please include:

- the MCP Router version (`pyproject.toml`, or the image tag) and how it is
  deployed (compose stack, image flavor, source install);
- the relevant non-secret settings (`MCPR_*` names and values; never send real
  tokens or keys);
- what an attacker can do and what they need first (network position, an agent
  key, the admin token, database access);
- reproduction steps or a proof of concept, ideally as a failing test.

## Scope

In scope, for example:

- bypassing agent or admin authentication, or an invalid credential being
  accepted or downgraded to another identity;
- one agent reaching another agent's tools, sessions, approvals or audit data,
  or executing a tool that policy (deny by default) does not allow;
- escalating from an agent key to admin capabilities;
- getting the router to call an internal or metadata address through a
  backend, a server registration or a skill source when that is refused;
- the DNS-rebinding guard (`MCPR_ALLOWED_HOSTS`, HTTP 421) or the fail-closed
  bind being bypassed;
- secrets (tokens, keys, stdio `env`, the database URL) appearing in logs, API
  responses, audit rows or model inputs.

Not vulnerabilities on their own (they are documented behaviour):

- secrets in `.env`, `*_FILE` files, or the database (stdio server `env` is
  stored in plaintext; see [docs/deploy.md](docs/deploy.md)): whoever controls
  those already controls the router;
- registering a **stdio** server runs its command: that is what an admin
  registration does, and it is admin-only;
- dev mode (no admin token, no agent keys, no principals) has an open admin
  API, and `MCPR_ALLOW_OPEN_DEV=1` exposes it on every interface: both are
  local-development settings;
- findings that need the admin token.

The threat model and the posture each guard enforces are in
[docs/security-model.md](docs/security-model.md).
