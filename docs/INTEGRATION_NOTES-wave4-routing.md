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

## S2d — item 1: skills routed end-to-end (interim gate removed)

- `RoutePipeline` now retrieves `kinds=("tool","skill")` regardless of settings;
  `RouteRequest.kinds` (new, optional, default None) may only NARROW. The S2b
  interim `kind == "tool"` gate is gone. Skill score questions are prefixed `[skill]`.
- Budgets after ranking, per kind (`pipeline._cap`): tools via maxServers ->
  maxTools (MCP servers only — skills never consume server budget); skills via
  `RoutePipeline.skills_budget(request, scope) -> BudgetClamp` =
  `min(request.max_skills, principal.max_skills, settings.max_exposed_skills)`
  (`budgets.clamp_budget`, same attribution rules as maxTools). Principal value is
  read from `scope._principal.max_skills` (PolicyScope) — None for other scopes.
- `RoutedTool.kind` / `CachedTool.kind` set; decision rows use `skill:<id>` for
  skills, bare ids for tools (`selected_tool_ids` and `scores` keys).
- Cache key += `max_skills`, `kinds`. `_revalidate` reloads skills with
  `skill_eligibility_filters()` and re-runs `scope.permits` on them.
- Trace: `RouteTrace.skill_budget` (BudgetClamp, `budget="maxSkills"`); new
  `maxSkills` stage; retrieval stage detail gains `beforeTools/beforeSkills/
  afterTools/afterSkills`. `maxServers`/`maxTools` stages count tools only.
  Pruned / policyFiltered entries are `ToolCandidate`s carrying `.kind`.
- Install hook (`api/routes_route.py`) calls `ensure_skill_keyword_index` next
  to `ensure_keyword_index`.
- For item 2: `RouteResult.tools` is mixed and rank-ordered — split on
  `t.kind`; `max_skills_applied = pipeline.skills_budget(req, scope).applied`.

## S2d — item 2: /route and /route/simulate wire shapes for skills

Additive only: no existing field renamed or retyped. Code: `api/routes_route.py`
(`_split` divides `RouteResult.tools` on `RoutedTool.kind`; `_skill_tokens` reads
`SkillRecord.body_tokens_est` for the routed skills in ONE query, because neither
`RoutedTool` nor the route cache carries it). Tests: `tests/test_route_skills_api.py`.

**Request** (both endpoints; snake_case and camelCase accepted as before):

```json
{
  "query": "fill pdf form",
  "max_tools": 5, "max_servers": 2, "allowed_servers": ["docs"],
  "max_skills": 1,
  "kinds": ["tool", "skill"]
}
```
`max_skills` (alias `maxSkills`): optional int 1..1000; may only LOWER the
principal/global cap. `kinds`: optional, 1..2 items, each `"tool"|"skill"`.
`kinds` only narrows: `["tool"]` -> `skills: []`; `["skill"]` -> `tools: []`.
`[]`, `["prompt"]`, `["tool","bogus"]` -> 422 (curated body, input not echoed).

**POST /api/v1/route response** (SPEC §9 snake_case top level; skill entries are
camelCase `bodyTokensEst` as specified):

```json
{
  "request_id": "6f1c…",
  "tools":  [{"server": "docs", "tool": "fill_pdf_form", "score": 0.91}],
  "skills": [{"source": "src-pdf-form-filler", "skill": "pdf-form-filler",
              "score": 0.88, "bodyTokensEst": 100}],
  "fallback_used": false, "latency_ms": 12.345, "no_match": false,
  "max_tools_applied": 5, "max_servers_applied": null,
  "max_skills_applied": 1,
  "cached": false
}
```
- `tools` is MCP tools ONLY (unchanged shape `{server, tool, score}`); skills never appear in it.
- `skills[].source` = skill source name; `skill` = skill name; order = rank order.
- `max_skills_applied` = `RoutePipeline.skills_budget(req, scope).applied` =
  min(request, principal.max_skills, settings.max_exposed_skills); typed nullable
  for parity with `max_servers_applied`, but always an int today (the global cap
  `MCPR_MAX_EXPOSED_SKILLS` is an int, default 3; principal default 3).
- `bodyTokensEst` = `SkillRecord.body_tokens_est` (chars/4 estimate set at ingest; 0 if unset).

**POST /api/v1/route/simulate response** (camelCase throughout):
`"…"` marks an elided value; `stages` shows only the new `maxSkills` stage
(retrieval/score/policy/maxServers/maxTools stages unchanged and omitted).

```json
{
  "requestId": "…", "agentId": "sk", "simulated": true,
  "tools":  [{"toolId": "…", "server": "docs", "tool": "fill_pdf_form", "score": 0.91}],
  "skills": [{"skillId": "…", "source": "src-pdf-form-filler", "skill": "pdf-form-filler",
              "score": 0.88, "bodyTokensEst": 100}],
  "noMatch": false, "fallbackUsed": false, "latencyMs": 15.2,
  "modelVersion": "simulated/…",
  "maxToolsApplied": 5, "maxServersApplied": null, "maxSkillsApplied": 1,
  "diagnostics": {
    "candidatesConsidered": [
      {"toolId": "…", "server": "docs", "tool": "fill_pdf_form", "domain": null,
       "operation": "read", "retrievalScore": 0.5, "matchedOn": ["keyword:pdf"], "kind": "tool"},
      {"toolId": "<skill id>", "server": "src-pdf-form-filler", "tool": "pdf-form-filler",
       "domain": null, "operation": "read", "retrievalScore": 0.4, "matchedOn": ["vector"],
       "kind": "skill"}
    ],
    "stages": [
      {"stage": "maxSkills", "before": 2, "after": 1,
       "pruned": [{"toolId": "<skill id>", "server": "src-pdf-form-helper",
                   "tool": "pdf-form-helper", "kind": "skill"}], "detail": {"…": "see item 1"}}
    ],
    "policyFiltered": [{"toolId": "…", "server": "slack", "tool": "search_messages",
                        "kind": "tool", "operation": "read", "reason": "no matching policy rule"}],
    "budgetClamps": [
      {"budget": "maxTools",   "requested": 5,  "principal": 10, "globalCap": 20, "applied": 5,  "clampedBy": null},
      {"budget": "maxServers", "requested": null, "principal": null, "globalCap": null, "applied": null, "clampedBy": null},
      {"budget": "maxSkills",  "requested": 1,  "principal": 3,  "globalCap": 3,  "applied": 1,  "clampedBy": null}
    ]
  }
}
```
- `kind` ("tool"|"skill") added to every `candidatesConsidered`, `stages[].pruned`
  and `policyFiltered` entry. For a skill, `toolId` = skill id, `server` = source
  name, `tool` = skill name (same mapping as `ToolCandidate`).
- `budgetClamps` gains the `maxSkills` entry (same shape as the others), taken from
  `RouteTrace.skill_budget`, falling back to `pipeline.skills_budget(...)` when the
  pipeline returned before its budget stage. Stage `detail` contents are as in item 1.
- Simulate `tools` entries keep exactly `{toolId, server, tool, score}` (no `kind`).
- Gateway `apply_route` consumer is untouched (S3 owns it; it still receives the mixed list).
