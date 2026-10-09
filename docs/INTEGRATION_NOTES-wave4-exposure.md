# Integration notes — wave 4 S3 (skill exposure)

## Landed (branch wave4/exposure)
- `skills/serve.py` — `SkillFiles(source_root, max_bytes).read_resource(skill, path)`,
  `read_body(skill, max_bytes)`, `normalize_relpath`, `source_root(source, cache_dir)`.
- `gateway/skills.py` — `SkillExposure` (single implementation for prompts/get,
  resources/read, meta-tools and REST): `load_routed(ids)`, `resolve(name|id, ids)`,
  `activate(...) -> Activation{skill_id,name,body,resources,record_id}`,
  `read_resource(...) -> ResourceContent`. Errors: `SkillAccessError.code` ∈
  not_found | denied | rate_limited | invalid_path | not_in_manifest | too_large | not_found | unreadable.
- `execution/manager.py::record_skill_activation(agent_id, skill_id, outcome, detail,
  route_request_id=None, initiated_by=None) -> record id` (bumps activation_count on ok).
- `skills/bundle.py::build_bundle([(skill, SkillFiles)]) -> (zip_bytes, skipped)`.
- `skills/__init__.py` is an empty file — S1 also creates it; resolve as empty.

## Seams for the integrator
- **SkillPolicy** (`gateway/skills.py`): `check(agent_id, skill, source) -> (allowed, reason)`.
  Wire S2's kind-aware `policy.engine` (PolicyRule resource_kind="skill") behind it.
  Default `DenyAllSkillPolicy` fails closed.
- **Git source root**: served from `<MCPR_SKILLS_CACHE_DIR>/<source.id>`; S1's clone
  path must match (or change `serve.source_root`).
- **Routed set**: callers pass the skill ids with `RoutedTool.kind == "skill"` from
  the session's last RouteResult (gateway) / agent's latest decision (REST/bundle).

## NOT yet landed (out of budget this run — next executor)
- `gateway/server.py`: register `list_prompts/get_prompt/list_resources/read_resource`
  handlers + `router.activate_skill` / `router.read_skill_resource` meta-tools calling
  `SkillExposure`; advertise prompts/resources capabilities; send
  prompts/list_changed + resources/list_changed on re-route — verify against the
  installed mcp SDK (same mechanism as tools/list_changed). Not yet verified.
- `api/routes_skills.py` section "# --- wave-4 S3: activation + bundle ---".
  Planned shapes (S4 can build against these):
  - `POST /api/v1/skills/{id}/activate` body `{agentId?, routeRequestId?}` (agentId
    required for admin → `initiated_by="admin"` like routes_execute) →
    `200 {body, resources:[{path,size,kind}], recordId}`; 403 denied, 404 not routed,
    429 rate_limited.
  - `GET /api/v1/skills/{id}/resources/{path:path}[?agentId=]` → bytes with
    content-type from `ResourceContent.mime_type`; same auth/audit.
  - `GET /api/v1/skills/bundle?agentId=` → `application/zip`, header
    `X-Skipped-Resources` count; every included skill audited as an activation.

## `~/.claude/skills` sync pattern
Download the bundle, unzip into a temp dir, read `.mcp-router-bundle.json`
(`{"skills": [names]}`). Remove from `~/.claude/skills` only the dirs listed in the
*previous* marker (never user-authored skills), copy the new dirs in, and keep the
new marker at `~/.claude/skills/.mcp-router-bundle.json`.
