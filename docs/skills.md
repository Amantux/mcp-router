# Agent Skills — operator guide

MCP Router routes [Agent Skills](https://agentskills.io/specification) through the
same machinery as MCP tools: discover → catalog → classify → route (budget +
policy) → expose → audit → analytics. Design record: [`history/skills-plan.md`](history/skills-plan.md).
Security posture: [`security-model.md` §Skills](security-model.md#skills-wave-4-s3).

```mermaid
flowchart LR
  SRC["Skill source<br/>directory | git"] -->|POST /skill-sources/{id}/sync<br/>or scheduled tick| ING["Ingest<br/>walk · validate · hash · flag"]
  ING --> CAT[("Catalog<br/>skills + skill_versions")]
  CAT --> CLS["Classify<br/>read/write/execute/unknown<br/>+ human review"]
  CLS --> RT["/route<br/>retrieve → policy (resourceKind=skill)<br/>→ maxSkills budget"]
  RT -->|skills[] + skill:&lt;id&gt; in decision| EXP["Exposure<br/>MCP prompts/resources · meta-tools<br/>REST activate / resources / bundle"]
  EXP -->|visibility → rate limit → policy → audit| AUD[("ExecutionRecord<br/>resource_kind=skill")]
  AUD --> AN["Analytics<br/>surfaced → activated · economy"]
```

## 1. Register a skill source

A **skill source** is the skill analogue of an MCP server. Admin-only
(`/api/v1/skill-sources`, admin bearer):

| Route | Purpose |
|---|---|
| `GET /api/v1/skill-sources` · `GET /{id}` | list / read |
| `POST /api/v1/skill-sources` (201) | create: `name`, `kind` (`directory` \| `git`), `location`, `gitRef?`, `enabled` (default true), `syncIntervalS` (default 3600, min 60) |
| `PATCH /{id}` | change name/location/gitRef/enabled/syncIntervalS |
| `DELETE /{id}` (204) | remove the source |
| `POST /{id}/sync` | ingest now; returns the added/changed/removed/skipped report |

Enabled sources are also re-synced by the discovery loop's skill-source tick
once `syncIntervalS` has elapsed.

- **directory** — an absolute path on the router host.
- **git** — `https` URL only, validated by `inference/urlcheck` (no localhost).
  Shallow clone into `MCPR_SKILLS_CACHE_DIR` with hooks disabled
  (`core.hooksPath=/dev/null`), every protocol except https denied, no submodules,
  no tags, a 120 s timeout and a 200 MiB post-clone size cap. Sync errors are
  curated; git's stderr is never echoed. Treat a git source as **untrusted
  author content** that you chose to pull.

## 2. Ingest rules

- Each directory with a `SKILL.md` is one skill. The spec is enforced strictly:
  `name` (1–64, `[a-z0-9-]`, no leading/trailing/double hyphen, **equals the
  directory name**) and `description` (1–1024), plus field types and lengths.
- **Nested skills:** a child directory with its own `SKILL.md` is a separate
  skill. Its files are excluded from the parent's manifest and its `scripts/`
  do not count toward the parent's `has_scripts`.
- **Symlinks:** `SKILL.md` is opened with `O_NOFOLLOW` and must be a regular file;
  symlink escapes out of the skill directory are refused.
- Content and resource manifest are hashed; every change writes a `skill_versions` row.
- Skip reason `skill_md_too_large`: SKILL.md is over `MCPR_SKILL_BODY_MAX_BYTES`
  + 16 KiB, so it is never read in full.
- **Flags** (`ingestFlags`, informational; ingest never edits content):

| Flag | Meaning |
|---|---|
| `unknown_frontmatter_keys` | Non-spec keys were accepted; their names are stored in `metadata._unknown_keys` |
| `body_truncated` | Body over `MCPR_SKILL_BODY_MAX_BYTES`, cut on a UTF-8 boundary |
| `secret_like` | Body matches a secret pattern; it is stored unchanged. Review the source |
| `resource_oversize` | A file is over `MCPR_SKILL_RESOURCE_MAX_BYTES`; its sha256 is null and it is not served |
| `review_stale` | Content changed after a human review (see §3) |

Generate test fleets with `python -m testbed.skills.generate --out DIR --skills N
[--seed S] [--include-invalid] [--as-git]` ([`testbed/skills/`](../testbed/skills/)).

## 3. Risk classes and review

Skills use the tool scale `read < write < execute` (`registry/classify.py::classify_skill`):

1. **execute**: the skill ships `scripts/`, or `allowed-tools` pre-approves a
   Bash/exec/run/shell-shaped tool. Any `allowed-tools` entry outside the
   known read set (Read, Grep, Glob, LS, WebSearch, TodoWrite) and write set
   (Write, Edit, MultiEdit, NotebookEdit) also counts as execute.
2. **unknown**: the body prefix tells the agent to run commands, but there are
   no scripts or allowed-tools to back that up. **Policy treats unknown as
   execute** (fail closed).
3. **write**: a write verb (send/write/create/delete/deploy) appears in the
   description or the body prefix.
4. **read**: guidance only.

Review with `PATCH /api/v1/skills/{id}/classification` (admin) or the dashboard's
Classification tab. Automatic re-classification can only move a skill toward a
stricter class, never to a laxer one. A human review is final while the content
stays unchanged. **Escalation on change:** if the body or manifest changes after
a review, the classifier may re-classify toward execute/unknown (never toward
read). The skill then loses its reviewed mark and gains `review_stale`.

## 4. Policy rules (`resourceKind: "skill"`)

Skills are deny-by-default, the same as tools. A skill rule is
`POST /api/v1/policy-rules` with `resourceKind: "skill"`. For a skill rule,
`serverId` is the **skill source id** and `toolName` is a **skill-name glob**.
`maxOperation` caps the risk class, so an unknown skill needs an `execute`
ceiling. `resourceKind` cannot be changed after create. See
[INSTALL.md §2](INSTALL.md).

## 5. Budgets

The skill budget is separate from `maxTools` and uses the same min() chain:
`RouteRequest.max_skills` (per request, 1–1000) → the agent principal's
`maxSkills` (default 3, 0–64; 0 = no skills) → `MCPR_MAX_EXPOSED_SKILLS`
(default 3). A request can lower the budget but never raise it. `/route`
reports the effective cap as `max_skills_applied`.

## 6. Routing

`POST /api/v1/route` returns MCP tools in `tools` and routed skills in `skills[]`
(`source`, `skill`, `score`, `bodyTokensEst`). One retriever covers both kinds
(vector + keyword, RRF), and the route cache and policy re-checks also cover
both. The decision persists routed skills as `skill:<id>` in
`selected_tool_ids`. **That latest decision is the only thing that grants
visibility.** `POST /api/v1/route/simulate` adds per-skill diagnostics
(`skillId`, score, `bodyTokensEst`) and `max_skills_applied`.

## 7. Exposure

All paths go through one implementation, `gateway/skills.py::SkillExposure`. It
checks visibility (the agent's routed set only), then the per-agent rate limit,
then re-checks policy, then commits an `ExecutionRecord(resource_kind="skill")`,
and only then returns content. If the audit write fails, no body is returned.

- **MCP:** routed skills are listed as **prompts** (`prompts/get` returns the
  body) and as **resources** (`skill://<source>/<name>/<path>`). Re-routing sends
  `list_changed`. Clients that only use tools get the meta-tools
  `router.activate_skill` and `router.read_skill_resource`.
- **REST** (agent key, or admin + `agentId`). An admin call acts as that agent
  and is audited `initiated_by="admin"`:

| Route | 200 |
|---|---|
| `POST /api/v1/skills/{id}/activate` body `{agentId?, routeRequestId?}` | `{body, resources[], recordId}` |
| `GET /api/v1/skills/{id}/resources/{path}` | raw bytes, `nosniff`, attachment unless text/* |
| `GET /api/v1/skills/bundle` | zip of the routed subset in spec layout; `X-Skipped-Resources` = URL-quoted skipped paths |

Errors use curated messages only: 400 (admin without agentId) · 401 · 403
(policy denied, disabled agent, or an agent key naming another agent) · 404
(**one** message for unknown, unrouted, bad path, not in manifest, and
unreadable; also returned when nothing is routed for a bundle) · 409 (stale
resource; duplicate skill name in a bundle) · 413 (resource too large; bundle
over 50 skills or 50 MiB) · 429 (rate limited) · 500 · 503 (exposure not
configured).

**`~/.claude/skills` sync pattern** (for clients that only read local skills):
download the bundle and unzip it to a temp directory. Read
`.mcp-router-bundle.json` (`{"skills": [names]}`). In `~/.claude/skills`, delete
only the directories listed in the **previous** marker, never user-authored
skills. Copy in the new directories and keep the new marker at
`~/.claude/skills/.mcp-router-bundle.json`.

## 8. Analytics

- Funnel ids are `skill:<id>`. The funnel stage `surfaced → activated` is
  attributed through `routeRequestId`. An agent-supplied id is kept only if it
  is one of that agent's own decisions; any other id is stored as NULL.
- **A bundle counts as an activation** (it ships the body).
- Context economy: a surfaced skill costs its metadata (`skill_metadata_tokens`).
  Its body counts as exposed (`skill_body_tokens_exposed`) only when the skill
  was activated on that decision. Bodies that were surfaced but never activated
  are reported as `skill_body_tokens_not_sent`.
- Eval baseline (`tests/test_eval_synthetic.py`, fallback backend): skills top-1
  1.0 / top-5 1.0, mixed 0.75, **0 unauthorized skill exposures**.

## 9. Limits and residuals

- **Bodies and resources are untrusted instructions.** The router never executes
  them, never renders them, and never interpolates them into its own model
  prompts. Classification reads only `description` and a body prefix. Redaction
  is not applied to served skill content.
- `secret_like` only flags a match; the content is still served to routed
  agents. Remove the secret from the source.
- Git URL validation is name/literal based (https only, no localhost). A
  public hostname that resolves, or later re-resolves, to an internal address
  is a known DNS-alias residual. Restrict git sources to hosts you trust.
- Staleness: a resource changed on disk since the last sync returns 409 until the
  skill is re-synced. Freshness between ticks depends on `syncIntervalS`.
- Unrouted access, including another agent's skill, is audited as `denied` and
  looks the same as a nonexistent skill.
