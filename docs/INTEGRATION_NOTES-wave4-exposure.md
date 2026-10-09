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

## Gateway wiring (landed, S3b)
- `GatewayServer(..., skills: SkillExposure | None = None)` / `build_gateway(..., skills=)`.
  **Integrator must construct a SkillExposure (S2 policy behind SkillPolicy) and pass
  it**; with `None` the prompts/resources lists are empty and no skill meta-tools appear.
- Installed SDK (verified in .venv): `Server(on_list_prompts, on_get_prompt,
  on_list_resources, on_read_resource)`; `NotificationOptions(prompts_changed,
  resources_changed)` (we set all three true); handshake era via
  `ServerSession.send_prompt_list_changed/send_resource_list_changed`; 2026-07-28 era
  via `PromptsListChanged`/`ResourcesListChanged` on the per-agent subscription bus.
  List results require `cache_scope`/`ttl_ms` (private/0).
- Routed skill ids: `RoutedTool.kind == "skill"` from the agent's last RouteResult,
  held in `GatewayServer._skill_ids[agent]` (not in `Exposure`, to stay out of
  exposure.py); tool ids passed to ExposureStore now exclude skills.
- Resource URI `skill://<source>/<skill>/<path>`, split on the first two `/`, no
  percent-decoding; the path is validated only by SkillFiles.
- All MCP skill errors are `INVALID_PARAMS` with messages chosen by code:
  rate_limited / denied / too_large, else "Unknown skill or resource.".
- Meta-tools `router.activate_skill{name}` and `router.read_skill_resource{name,path}`
  go through the same `_activate` / `_read_skill_resource` helpers (tested with a stub).

## NOT yet landed (out of budget, S3b — next executor)
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

## Reviewer verdict on 3cc6a40: CHANGES (no blockers) — OPEN, not yet fixed
1. serve.py:97 `e["path"]` KeyError on malformed manifest entry (crashes bundle / raw 500); one invalid entry poisons the whole allowlist → build allowlist leniently, skip bad entries.
2. serve.py:101-111 TOCTOU symlink/FIFO swap between realpath check and open → open with O_NOFOLLOW / openat on a dir fd, fstat the fd for regular-file + size checks.
3. bundle.py has no gate → add `SkillExposure.bundle()` running resolve + _gate + audit per skill before returning bytes.
4. serve.py `.html`/`.js` mime types → stored XSS if passed through as Content-Type; serve text/plain or attachment + nosniff.
5. Resource reads bump activation_count → distinct outcome/flag for reads.
6. load_routed is 2N queries per call → single query by name/id with selectinload(source).
7. nit: case-insensitive dup names; skill named like MARKER.
8. nit: nested metadata str() repr.
Also: `routed_ids` must be derived server-side from the agent's own route, never taken from the client.

## S3c step 1 — reviewer should-fixes (closed)

- Every skill path (prompts/get, resources/read, both meta-tools) now funnels
  untyped exceptions to a curated `INTERNAL_ERROR` ("Internal error while
  serving the skill."); SkillExposure audits them as outcome `error`, detail
  `internal: <ClassName>` (best-effort). Never `str(exc)`.
- `exposure.clear(agent)` is the reset for skills too: `_routed_skills` drops
  `_skill_ids[agent]` and returns nothing when the agent has no exposure (fail closed).
- `resolve()` refuses names with more than one "/" (`invalid_name`, constant
  message), and never matches by name a source whose name contains "/" (id still
  works). **Integrator:** skill-source ingest should reject "/" in source names;
  skill names already cannot contain "/" (spec regex).
- Meta-tools (`router.*`) do not count toward `max_tools`. Their names are
  checked before upstream dispatch, so an upstream server named "router" cannot
  shadow them (its tools are simply unreachable under those two names).
- `_track_session` now also runs on prompts/get and resources/read.

## S3c step 2 — serve.py hardening

- Resource files open with `O_NOFOLLOW|O_CLOEXEC`; `fstat` on the fd must be a
  regular file and `O_NONBLOCK` (a FIFO cannot hang a worker). Closes the swap-to-symlink
  TOCTOU on the FINAL component only. **Open:** an intermediate directory
  swapped to a symlink after realpath() still escapes unless the entry has a
  `sha256` (reviewer proved it). Fix: require sha256, or walk with dir fds.
- Active content (`.html/.htm/.xhtml/.svg/.svgz/.xml/.js/.mjs/.cjs/.jsx`) is
  never served as text: always an `application/octet-stream` blob (mime-XSS
  posture). `.ts/.tsx` stay `text/plain`: no browser executes TypeScript.
- `sha256` is REQUIRED on every manifest entry: the bytes read must match it,
  and an entry WITHOUT one is refused as `stale`. This content check is what
  closes the directory-swap race. **Integrator: ingest MUST always write
  `sha256`** (and re-index existing sources), or their resources are unservable.

## S3c step 2 — reviewer should-fixes (closed)

- A malformed manifest entry is logged and skipped (`serve.manifest_entries`),
  not fatal for the whole skill (read, list and bundle all use it).
- MIME: `serve.resource_mime(path, is_text)` is the ONE decision used by
  resources/read and resources/list.
- "escapes the skill directory" wording only for ELOOP; other open errors are
  `unreadable`.
- Audit outcomes: body activation = `ok` (bumps `activation_count`); resource
  read = `read` (no bump); bundle = `ok`/detail `bundle` per included skill
  (a bundle ships the body, so it counts as an activation).
- `load_routed` is one joined query (skill + source) per call.
- `bundle._build_bundle` is private (no gating). Routes MUST use
  `SkillExposure.bundle(agent_id, routed_ids, route_request_id=None,
  initiated_by=None) -> tuple[bytes, list[str]]` (visibility -> limiter (one
  token per bundle) -> policy per skill, denied ones audited and omitted ->
  audit -> bytes).
