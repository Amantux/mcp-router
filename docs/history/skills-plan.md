# Wave 4 — Agent Skills routing

Apply the MCP tool machinery (discover → catalog → classify → dedup → route →
budget → policy → expose → audit → analytics) to **Agent Skills**
(https://agentskills.io/specification, verified 2026-10-09).

## Spec facts we build against
- A skill = directory with `SKILL.md` (YAML frontmatter + Markdown body); optional
  `scripts/`, `references/`, `assets/`, anything else.
- Frontmatter: `name` (required; 1–64; `[a-z0-9-]`; no leading/trailing/double
  hyphen; **must equal the parent directory name**), `description` (required;
  1–1024), `license`, `compatibility` (≤500), `metadata` (str→str map),
  `allowed-tools` (space-separated, experimental).
- Progressive disclosure: metadata (~100 tokens, always loaded) → body
  (<5000 tokens recommended, on activation) → resources (on demand).

## Design (binding)
- **Skill source** ≈ MCP server: `directory` (absolute path) or `git` (https URL,
  shallow clone into `MCPR_SKILLS_CACHE_DIR`, `core.hooksPath=/dev/null`, timeout,
  size cap, URL validated via `inference/urlcheck`). Sync walks for `SKILL.md`,
  refuses symlink escapes, validates the spec, hashes content + resource manifest,
  versions changes (`skill_versions`), flags secret-shaped strings (never edits).
- **Risk class** = `operation` on the same `read<write<execute` scale: ships
  `scripts/` or pre-approves executing tools (`allowed-tools` with Bash/exec-like
  entries) → `execute`; body declares writes/sends → `write`; guidance-only →
  `read`; undeterminable → `unknown` (policy treats as execute — fail closed).
- **Unified routing**: `ToolCandidate.kind` ∈ {tool, skill}; one retriever over
  both tables (vector + keyword, RRF), one pipeline, one policy engine
  (`PolicyRule.resource_kind`; skill rules key on source id + name glob), separate
  `maxSkills` budget (`RouteRequest.max_skills`, `AgentPrincipal.max_skills`,
  `MCPR_MAX_EXPOSED_SKILLS`, same min() chain), same cache + simulate diagnostics.
- **Exposure** (MCP-native progressive disclosure): routed skills appear per
  session as MCP **prompts** (name/description; `prompts/get` returns the body)
  and **resources** (`skill://<source>/<name>/<path>`; `safe_join`; size caps);
  meta-tools `router.activate_skill` / `router.read_skill_resource` for tool-only
  clients; `prompts/list_changed` + `resources/list_changed` on re-route. REST:
  `GET /api/v1/skills…`, `POST /api/v1/skills/{id}/activate` (audited), and
  **bundle export** `GET /api/v1/skills/bundle` (zip in spec layout of the agent's
  routed subset — for clients that only read `~/.claude/skills`).
- **Audit/analytics**: an activation = `ExecutionRecord(resource_kind="skill",
  skill_id, outcome=ok)` attributed via `route_request_id`; the funnel gains
  `surfaced → activated`; context economy counts body tokens not sent.
- **Dedup**: skill↔skill with the existing engine; cross-kind skill↔tool
  suggestions (same domain, high cosine) as a separate suggestion kind.

## Fences (wave 4 executors)
- S1 sources+ingest+testbed: `src/mcprouter/skills/{sources,ingest,walker,
  gitsource,validate,hashing}.py`, `api/routes_skill_sources.py`,
  `api/routes_skills.py` (read side), `testbed/skills/**`, tests, notes.
- S2 unified routing+classify+dedup+analytics: `routing/**` (skill leg),
  `registry/classify.py` (skill risk class), `dedup/**` (cross-kind),
  `policy/**` (resource_kind), `routing/budgets.py` (maxSkills), `analytics/**`
  (kind), `eval/**` (skill cases), `api/routes_route.py` (shape), tests, notes.
- S3 gateway exposure (T3): `gateway/**` (prompts/resources/meta-tools),
  `skills/serve.py` (safe_join, caps), `api/routes_skills.py` (activate, bundle),
  `execution/**` (activation audit), tests, docs/security-model.md §skills, notes.
- S4 UI: `ui/**` only — Skill sources page, Skills catalog (body viewer, resource
  tree, version timeline, classification editor), Lens/Analytics/Duplicates
  kind-aware, Playground "activate skill" preview, bundle download.
