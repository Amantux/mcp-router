# Wave 4 S2 — unified routing: integration notes (PARTIAL)

Branch `wave4/routing` off `wave4/scaffold @ 807fcb8`. This executor ran out of
budget after the policy + classification slice. What is DONE is binding; what is
NOT DONE is listed so S3/S4 do not build against shapes that do not exist yet.

## Done

### Policy (`policy/engine.py`, `policy/scope.py`)
- `rule_matches(rule, agent_id, container_id, name, kind="tool")` — a rule matches
  only its own kind. `PolicyRule.resource_kind` NULL (transient, unflushed rule)
  is treated as `"tool"` (`rule_kind()`).
- `evaluate(...)` unchanged signature (score-free pin intact); tool rules only.
- `evaluate_skill(principal, source: SkillSourceRecord, skill: SkillRecord, rules)`
  — skill rules only; `server_id` = skill source id, `tool_name` = glob over the
  skill name; ceiling over `skill.operation` with anything not read/write/execute
  (incl. `unknown`) escalated to execute. Score-free by construction.
- `PolicyScope.permits(candidate)` dispatches on `candidate.kind`.
- `PolicyScope.server_ids()` counts **tool rules only** — maxServers / server
  scoping is about MCP servers; a NULL-server skill rule does not widen it.
- `PolicyScope.fingerprint()` includes each rule's kind (route-cache key).

### Policy API (`api/routes_policy.py`)
- Rules: `resourceKind: "tool" | "skill"` on create (default `"tool"`) and in
  `RuleOut`. **Immutable after create** (no PATCH field): flipping kind would
  silently re-target `serverId` from a server to a skill source. For skill rules,
  `serverId` must name a skill source (422 `serverId does not name a skill source`).
- Principals: `maxSkills` (int, 0..64, default 3) on create/patch/out. 0 = no
  skills exposed.

### Classification (`registry/classify.py`)
- `classify_skill(name, description, body, *, has_scripts, allowed_tools) ->
  Classification`: scripts/ or exec-shaped allowed-tools (bash/sh/shell/exec*/
  run*/terminal/command) → execute; run/execute/bash/shell prose in the
  description/2KB body prefix with nothing backing it → unknown; send/write/
  create/delete/deploy → write; else read. Domain via the tool keyword tables over
  name + description + body prefix.
- `apply_skill_classification(session, skill_id, c) -> bool`: never touches a
  `classification_reviewed=True` skill; once auto-classified
  (`classification_source` non-NULL) only moves toward execute/unknown
  (read < write < execute < unknown). Both guards are in the UPDATE WHERE clause.
  Sets `classification_source = "skill-rules-v1"`. **S1: call this after ingest.**

## NOT done (open for a follow-up executor)
Retriever skill leg + GIN index; pipeline kind-awareness, maxSkills budget chain
and `skills: [...]` response block; cache key `max_skills`; simulate diagnostics;
`embed_pending_skills`; dedup skill↔skill + cross-kind; analytics funnel/economy/
profiles + `kind` on /analytics/tools; eval `expected_skills` + 20 skill cases.
Conventions fixed by the plan for that work (unchanged): `RoutingDecisionRecord.
selected_tool_ids` carries skills as `"skill:<id>"` (tool ids bare); cross-kind
DuplicateSuggestion uses `tool_b_id = "skill:<id>"`, rationale "cross-kind", never
auto-applied; maxServers counts MCP servers only.

## S2b — skills in the routing pipeline (partial: items 1 + 4 landed)

**Retriever (`routing/retriever.py`)**
- `HybridRetriever.retrieve(..., kinds=("tool",))` — default is TOOLS ONLY until item 2 (the pipeline would otherwise fetch skills only to drop them, starving tools; reviewer finding). Item 2 must pass `kinds` explicitly (Retriever Protocol in interfaces.py needs the param too). Skill legs: vector
  (`SkillRecord.embedding`, `embedding_backend == embedder.name`) + keyword
  (weighted tsvector: name A with `-`→space, tags B, description C,
  `left(body, 2048)` D). Un-embedded skills are reachable by keyword only.
- Eligibility (`skill_eligibility_filters()`): skill enabled AND available AND
  source enabled AND source status != 'offline' (both legs; mutation-checked).
- Fusion: one RRF over up to four ranked lists (tool vec, tool kw, skill vec,
  skill kw); fusion key is `tool_id` for tools and `"skill:<id>"` for skills.
- Skill `ToolCandidate`: `kind="skill"`, `tool_id=skill.id`,
  `server_id=source.id`, `server_name=source.name`, `tool_name=skill.name`,
  `body_tokens_est`.
- `server_ids` scopes MCP tools only; skills are scoped by `PolicyScope.permits`.
- **App init:** call `ensure_skill_keyword_index(engine)` next to
  `ensure_keyword_index(engine)` (GIN index `ix_skills_routing_fts`; belongs in
  an Alembic revision at integration).

**Interim gate in `routing/pipeline.py`:** retrieval results are filtered to
`kind == "tool"` (both the main and the "extra" retrieval loops) until per-kind
budgets (item 2) land. Remove that gate as part of item 2.

**`registry/classify.py`:** `apply_skill_classification(session, id, c, *,
content_changed=False)`. With `content_changed=True` a reviewed row may move
toward execute/unknown only, gets `classification_reviewed=False` and
`"review_stale"` appended to `ingest_flags` (no duplicates; PG jsonb, one
UPDATE). Unchanged content: reviewed guard absolute. Callers (skill sync) must
pass `content_changed` when content_hash/manifest_hash moved.

**Not done (items 2, 3):** per-kind budgets/max_skills clamp, `skill:<id>`
decision-row prefix, cache key (max_skills + kinds) and cache-hit skill
re-filter, `/route` `skills`/`max_skills_applied`, simulate `kind` fields and
maxSkills clamp. The response shapes for S4b are therefore NOT yet fixed here.
