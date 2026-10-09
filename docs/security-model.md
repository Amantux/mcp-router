# Security model (FR-06 / FR-07)

Authorization in MCP Router is **deterministic code that runs after routing
and cannot be widened by it**. This document is the contract. Every rule
below has a test that fails when the rule is removed. The mutation table is
in the gateway workstream's report, and the tests are named after the rules.

## 1. Identities

| Who | Credential | Where checked |
|---|---|---|
| Agent | `Authorization: Bearer <api key>` | `api/deps_auth.resolve_principal`, shared by REST and `/mcp` |
| Admin | `Authorization: Bearer <MCPR_ADMIN_TOKEN>` | `api/deps_auth.check_admin` |

- Keys are stored only as `sha256` hex (`AgentPrincipal.key_hash`). The
  presented key is hashed and compared against **every** principal with
  `hmac.compare_digest` and no early exit. **Exactly one** match is required.
  A key shared by two principals names no one.
- A credential that is presented but invalid is **always 401**. It is never
  silently downgraded to dev mode or to another identity.
- Only the `Authorization` header is read. `X-Agent-Id`, `X-Forwarded-User`
  and similar headers are ignored. Request bodies never carry identity: an
  `agent_id` in a body (for example spec §9 `/route`) must be ignored in
  favour of the authenticated principal.
- Principals come from `MCPR_AGENT_KEYS` (bootstrapped at startup, hash only;
  the environment wins on rotation) or from the admin API. The API returns the
  raw key exactly once, at creation or rotation. Bootstrap fails loudly in
  three cases: malformed entries, one key reused for several agents, or an
  agent key equal to the admin token. The error message never contains a key.
- An agent key is never an admin credential. If `MCPR_ADMIN_TOKEN` is unset
  outside dev mode, admin endpoints answer **403** (fail closed).

**Dev mode** applies only when nothing at all is configured: no
`MCPR_AGENT_KEYS`, no `MCPR_ADMIN_TOKEN`, and zero principals in the
database. Callers without a key become the synthetic, unpersisted agent `dev`.
That agent is **still deny-by-default**, so it needs policy rules like anyone
else. A structured `auth.dev_mode` warning is logged once. Bind to localhost
only: dev mode has no credential to stop a DNS-rebinding page, so the Host
allowlist (§6) is the only guard.

## 2. Policy (`policy/engine.evaluate`)

```
evaluate(principal, server, tool, rules) -> Decision(allow, requires_approval, reason, rule_id)
```

**Routing scores never widen access.** The function takes no score, rank or
confidence input. This holds by construction: the signature is
`(principal, server, tool, rules)` and `test_evaluate_signature_is_score_free`
pins it. Routing decides what is shown. Policy alone decides what is allowed.

- **Deny by default.** With no matching rule, the call is denied.
- A rule matches when all three hold:
  - `rule.agent_id == principal.agent_id`, compared as an exact string (no glob).
  - `rule.server_id` is null or equals the tool's server.
  - `rule.tool_name` is null or `fnmatchcase(tool.name, rule.tool_name)`. The
    match is case-sensitive on every OS.
- **Operation ceiling**: read < write < execute, compared against the tool's
  classified operation. A tool whose operation is `unknown`, or any value
  that is not read, write or execute, is treated as **execute**, so a
  misclassified tool fails closed. A rule whose `max_operation` is not one of
  the three valid values grants nothing.
- **`requires_approval` is sticky.** If any matching rule requires approval,
  an allowed call requires approval, even when a broader rule would allow it
  outright. A narrow "needs a human" rule cannot be undone by a wide rule.
- A disabled principal is denied. A `(server, tool)` pair whose ids disagree
  is denied.

Glob caveats for rule authors:

- `tool_name` is a glob. To match a literal metacharacter, escape it (`[[]`).
- A rule with a null `server_id` automatically extends to servers discovered
  later.
- `*` with a null `server_id` means "every tool on every server, up to the
  ceiling".

Deleting a principal also deletes its rules, so re-creating that `agent_id`
does not resurrect old grants.

## 3. Execution (`execution/manager.ExecutionManager`)

The manager is the **only** path to an upstream tool. The gateway's
`tools/call` and the approval endpoint both go through it. Each attempt runs
these steps in order:

0. **Reload** the principal, tool, server and rules from the database. The
   caller's copies may be stale, so this closes the time-of-check/time-of-use
   gap between routing and execution. The arguments are deep-copied, so what
   is validated is exactly what is invoked.
1. **Policy.** A refusal returns `denied`.
2. **Per-agent sliding-window rate limit**
   (`rate_limit_per_agent_per_min`, in-process). A refusal returns
   `rate_limited`.
3. **Availability** of the tool and server. A refusal returns `unavailable`.
4. **JSON-Schema validation** against `tool.input_schema`. A refusal returns
   `invalid_args`.
   - Errors are reported as `path: keyword` only, such as `$.limit: type` or
     `$: additionalProperties`. Argument values and unexpected property names
     are never echoed, because they may be secrets.
   - **Remote `$ref` is never fetched.** jsonschema 4.26's default registry
     does fetch them with `urlopen`; this was verified with a live listener.
     We pass an empty `referencing.Registry()` instead.
5. **Approval gate.** A refusal returns `pending_approval` and creates an
   `ApprovalRequest`.
6. **Invoke** through `ToolInvoker` under a hard `default_tool_timeout_s`.
   The outcome is `ok`, `error` or `timeout`.
   - Upstream exception text is never returned or stored. A
     `ToolInvocationError.curated` message is redacted. Any other exception
     becomes `upstream invocation failed`, and only its type name is logged.

**Audit.** One `ExecutionRecord` is written for every attempt, including
refusals. Detail is curated, redacted and CR/LF-scrubbed. On the invoke path,
a `started` row is written **before** the upstream call. If that write fails,
the call does not happen. The row is finalized afterwards (`cancelled` if the
caller goes away).

**Approvals** are stored in their own table `approval_requests`, on separate
metadata because `models.py` is frozen.

- Raw arguments stay server-side and never leave the process. The API shows
  only a `summary`: the tool, its effective operation, and **redacted**
  arguments (secret-named keys masked, secret-shaped strings masked, strings
  truncated).
- Approvals are **single-use**. `approve()` claims the row with one
  conditional `UPDATE … WHERE status='pending' AND expires_at > now
  RETURNING`, so concurrent approvals race in the database and exactly one
  wins. A test runs 8 concurrent approvals and sees one execution.
- Approvals **expire after 10 minutes**. An expired approval can no longer
  be approved (410). Denying one still succeeds: `deny` answers 200 with
  status `denied` and clears its arguments, so an admin can always close a
  stale request.
- The claimed call re-runs policy, availability, a **schema-hash equality
  check** and validation against *current* state. Revoking the rule or
  changing the tool's schema after the request voids it.
- Raw arguments are cleared on every terminal state, including a crash
  mid-approve.
- Only an admin can approve or deny. Agents can poll **their own** approval
  (`GET /api/v1/me/approvals/{id}`). Another agent's approval is
  indistinguishable from a missing one.

## 4. Dynamic exposure (`gateway/`, MCP at `/mcp`)

- Every HTTP request to `/mcp` is authenticated before the SDK sees it.
  Non-HTTP scopes are rejected.
- The wrapper sets `scope["user"]`, so the SDK **binds each MCP session to
  the credential that created it**. Another agent presenting that
  `Mcp-Session-Id` gets 404. *(From v0.6)* the binding is to the credential,
  not just the agent: after a key rotation the old key's sessions and
  `subscriptions/listen` streams are closed, never notified. Rotating a key,
  disabling a principal (`PATCH … enabled=false`) or deleting it closes them
  at once; the next re-route re-checks as well.
- *(From v0.6)* Session caps: `MCPR_MCP_MAX_SESSIONS` (1000) in total and
  `MCPR_MCP_MAX_SESSIONS_PER_AGENT` (32) per agent **credential**, so a
  rotated-away key's sessions never use up the new key's budget. The next
  `initialize` over the cap gets 429. `/mcp` bodies are capped at 1 MiB like
  every other route.
- `tools/list` returns the agent's last route result, or a deterministic
  default when no route has run: the top-N most-used tools, with a stable-id
  tiebreak.
  - The list is **always re-filtered through `evaluate`** (defense in depth)
    and capped at `min(principal.max_tools, max_exposed_tools)`.
  - Tool names are stable: `server_name.tool_name`. Upstream descriptions
    are redacted.
  - Results carry `cacheScope: private` and `ttlMs: 0`.
- **Exposure is not an authorization boundary.** An authorized tool that is
  not currently exposed can still be called, so clients that cache tool lists
  keep working. An exposed tool still has to pass the full execution pipeline.
- `router.find_tools` re-routes from a natural-language query. The query is
  **redacted before it reaches the routing model**. The meta tool is
  rate-limited per agent, router failures are curated, and the previous
  exposure is kept on failure. A discovered tool cannot shadow its name.
- *(From v0.6)* Meta-tool arguments (`router.find_tools`, `router.feedback`
  and the skill tools) are validated against the schema each tool
  advertises, with the execution pipeline's validator. A bad call gets an
  `isError` result `Refused: invalid arguments (…)` that names the location
  and keyword, never the values.
- `tools/list_changed` is sent only to the agent whose exposure changed:
  - Handshake-era sessions get it on their standalone stream.
  - 2026-07-28-era clients get it via `subscriptions/listen` on a
    bus **per agent and credential** *(from v0.6)*.

## 5. Redaction (`execution/redaction`)

`redact()` masks:

- URL userinfo
- `Bearer`, `Basic` and `token` credentials
- `key=value`, `key: value` and `"key": "value"` pairs whose key names a
  secret (token, password, secret, api_key, authorization, credential,
  private_key, session_id, cookie, matched as a suffix)
- provider-prefixed tokens (GitHub, OpenAI-style `sk-`, Slack, AWS `AKIA`,
  JWT)
- high-entropy runs of 32 characters or more. UUIDs are exempt, so audit ids
  stay readable.

`redact_value()` applies the same masking to nested arguments. `scrub_log()`
redacts and then escapes CR, LF and other control characters to prevent log
forging. Every pattern runs in linear time, which the
`test_no_catastrophic_backtracking` tests pin. One pattern measured 1.5s on
200k characters before it was anchored.

Redaction is applied to logs, audit detail, approval summaries and previews,
tool descriptions shown to agents, and the routing query. It is **not**
applied to tool results returned to the authorized caller, because a tool may
legitimately return a credential the agent asked for.

## 6. Deployment posture and known limitations

Reviewed for 0.6. Items marked *(from v0.6)* describe the 0.6.0 behaviour.

**Posture**

- **Fail-closed bind** *(from v0.6)*: without `MCPR_ADMIN_TOKEN` the container
  listens on `127.0.0.1` only and logs a warning; `MCPR_ALLOW_OPEN_DEV=1`
  overrides it for development. Compose publishes on
  `${MCPR_BIND:-127.0.0.1}`.
- **Host allowlist** *(from v0.6)*: one app-wide check, outermost, admits
  `localhost`, `127.0.0.1`, `[::1]` and `MCPR_ALLOWED_HOSTS` (any port) and
  answers anything else with **421**, on every path. The MCP SDK's own check
  on `/mcp` stays as defence in depth. Before 0.6 only `/mcp` was checked.
- **Open surfaces**: `/docs` and `/openapi.json` are unauthenticated (the
  schema is not secret); `/healthz` and `/readyz` *(from v0.6)* are
  unauthenticated and return no data beyond status. `/metrics` requires the
  admin token when one is configured *(from v0.6)*.
- **First principal** *(from v0.6)*: in dev mode with no admin token,
  `POST /api/v1/principals` answers 409, because creating an agent would end
  dev mode and leave no admin credential.
- **Secrets at rest**: agent keys are hashed; the `env` of stdio servers is
  stored in plaintext JSON (`mcp_server_credentials`), never returned or
  logged. Protect the database and its backups.

**Known limitations (accepted, flagged)**

- **TOCTOU window.** Policy is evaluated on fresh state immediately before
  invoke. A rule revoked in the milliseconds between that check and the
  upstream call does not stop the call.
- **Rate limiting runs after policy**, which is the mandated order. A flood
  of denied calls is not rate-limited, but each one still writes an audit
  row.
- **In-process state.** The rate limiter, exposure sets and notification
  routing live in one process. Running several uvicorn workers would split
  rate limits and exposure. scoping.md mandates a monolith, so run one
  worker.
- **Regex `pattern` in upstream tool schemas** is evaluated by Python `re`
  during validation. A hostile upstream schema plus crafted arguments could
  cause catastrophic backtracking. The upstream server is semi-trusted, since
  it is registered by an admin.
- **Approval approve path** does not consume the agent's rate-limit budget.
- **Tool `input_schema`** is shown to agents verbatim. Only descriptions are
  redacted.

## Skills (wave 4, S3)

Skill bodies and resource files are **untrusted author content** handed to
agents. Posture:

- **Never executed, never interpolated** into the router's own model prompts.
  The routing track only embeds/classifies `description` + a ~1 KB body prefix.
  Served bodies are returned verbatim; redaction is NOT applied (author content),
  and ingest flags (secret-shaped strings, S1) are exposed via REST only —
  prompt/resource descriptions stay verbatim.
- **Prompt injection** is the agent's problem to contain, the router's job is to
  make activation *deliberate, authorized and audited*: a skill is only visible
  to an agent whose last route surfaced it (default exposure: none), and every
  activation re-checks policy (defense in depth — routing already filtered).
- **Activation order** (`gateway/skills.py::SkillExposure`, one implementation
  for MCP prompts/resources, meta-tools and REST): visibility (routed set only;
  another agent's skill is indistinguishable from a nonexistent one) → per-principal
  sliding-window rate limit (`skill:<agent>` key) → policy re-check (`SkillPolicy`;
  the app wires `EngineSkillPolicy` from `policy/skill_bridge.py`; the
  fail-closed `DenyAllSkillPolicy` is the default only when no policy is
  injected) → `ExecutionRecord(resource_kind="skill")`
  committed → body returned. Audit failure ⇒ no body. Denials/rate limits are
  audited with curated detail only (never the body; resource reads log the path).
- **File access** (`skills/serve.py`): relative POSIX path only (no absolute,
  drive, backslash, NUL, `..`); must be listed in the ingest `resource_manifest`;
  realpath must stay inside the realpath of the skill dir (symlink escapes
  refused); size capped by `MCPR_SKILL_RESOURCE_MAX_BYTES` at stat *and* after
  read. Errors are typed (`SkillServeError.code`) with curated messages that
  never contain filesystem paths.
- **Bundle** (`skills/bundle.py`): entries are normalized relative paths
  (zip-slip guard), top-level dir = validated single-segment skill name, ≤50
  skills, ≤50 MiB total, per-file cap = resource cap; unreadable/escaping files
  are skipped and reported, never followed. SKILL.md frontmatter is rebuilt with
  JSON-quoted scalars so author text cannot inject YAML keys.
- **Final wave-4 decisions** (as merged on `integrate/wave4`):
  - *Unrouted access is audited as denied.* A request for an unknown skill, or
    one outside the agent's routed set (including another agent's skill), writes
    an `ExecutionRecord` with outcome `denied` and detail `not routed`. It
    returns the same 404 "Unknown skill or resource." as a nonexistent skill,
    so a caller cannot probe the catalog. The routed set comes from the server,
    from the agent's latest `RoutingDecisionRecord`. It is never taken from the
    client.
  - *Bundle outcome.* A bundle ships bodies, so it counts as an activation:
    each routed skill is policy-checked, denied ones are audited and left out,
    and the rest are audited with outcome `bundle` (counted as an activation in
    the funnel; never bumps `activation_count`). A failed bundle (`too_many`, `too_large`,
    `invalid_name`, `duplicate_name`) writes **one** `error` row
    (`bundle: <code>`), and a rate-limited bundle writes **one** `rate_limited`
    row. Neither writes a row per skill.
  - *A disabled principal cannot be impersonated.* An admin acting as an agent
    must name `agentId` (otherwise 400). An unknown agent gets 404, and a
    **disabled** agent gets 403. An agent key that names a different agent
    gets 403. Admin calls are audited `initiated_by="admin"`.
  - *`routeRequestId` ownership.* An agent-supplied `routeRequestId` is used for
    attribution only when it is one of **that agent's own** routing decisions
    (`ExecutionManager.owned_route_request_id`). A malformed, unknown or
    other-agent id is stored as NULL rather than raising an error. For admin
    calls the id is ignored, because an admin trial is not the agent choosing
    the skill.
  - Operator guide: [`skills.md`](skills.md).
