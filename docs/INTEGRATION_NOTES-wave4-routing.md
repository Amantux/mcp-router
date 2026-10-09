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

## S2c (wave4/routing-analytics) — embeddings, eval; dedup/analytics BLOCKED
Done:
- `inference.pipeline.embed_pending_skills(session, backend)` -> `EmbedReport`.
  Text = `canonical_skill_text(name, description, body)` =
  `"name: description\n" + body[:1024]`. Same recompute/backend/optimistic
  `updated_at` rules as tools; caller commits. Not yet wired into any job/route
  (whoever owns the refresh job calls it next to `embed_pending_tools`).
- Eval: JSONL gains `expected_skills` (ranked skill NAMES, matched on
  `RoutedTool.tool_name` where `kind == "skill"`) and `kinds` (subset of
  tool/skill, derived when omitted). `CaseOutcome.returned` is now tools-only;
  `returned_skills` holds skill names. Metrics: `skills: {positive_cases,
  top1_accuracy, top5_recall}`, `mixed: {cases, both_top1_rate}`. Case rows gain
  `expected_skills`/`returned_skills`.
BLOCKER (schema, needs a decision — outside the S2c fence):
`duplicate_suggestions.tool_a_id/tool_b_id/preferred_tool_id` and
`tool_stats_daily.tool_id` are `VARCHAR(36)` (verified in the live DB). The
fixed convention `"skill:<uuid>"` is 42 chars, so cross-kind/skill dedup rows and
skill rollup rows cannot be stored as planned; "rollups need no schema change"
does not hold. Smallest fix: widen those four columns to VARCHAR(48) in
models.py and add idempotent `ALTER TABLE ... ALTER COLUMN ... TYPE VARCHAR(48)`
lines to db.py's upgrade list (widening varchar is metadata-only in PG). Alt:
add a `kind` column and keep ids bare (bigger, touches every reader).
Not done: dedup skill pairs/cross-kind + review kindA/kindB; analytics funnel/
economy/profiles/`kind` filter/overview `skills`; 20 skill + 10 mixed synthetic
cases + skills fixture in eval/synthetic_catalog.py.

## S2f — skills in analytics (wave4/skills-analytics)

Funnel ids are **kind-keyed**: a tool is its bare tool id, a skill is
`"skill:<SkillRecord.id>"` — exactly the string the router writes into
`RoutingDecisionRecord.selected_tool_ids`, so rank = 1-based position in that
array for both kinds. `service.kind_of(id)` / `service.SKILL_PREFIX` are the
one place the prefix is interpreted.

- **Activation** (`funnel.ATT_CTE`, shared by funnel, position curve,
  co-surfacing, pair evidence, profiles, rollups): an `ExecutionRecord` with
  `resource_kind="skill"` contributes key `"skill:" || skill_id`; any other row
  contributes its bare `tool_id`, *unless* that tool_id starts with `skill:`
  (dropped — a tool-kind row can never forge a skill activation, and a skill
  row never credits a bare id). The existing same-agent ownership join and
  `outcome <> 'started'` apply unchanged; NULL `route_request_id` never counts.
  `simulated/` decisions stay excluded via `SURF_CTE`.
- **Rollups**: `tool_stats_daily.tool_id` (48 wide) stores `"skill:<id>"`
  rows; rollup/live merge treats both kinds alike (tested: merged == live on a
  mixed day; recompute is idempotent).
- **Wire** (`ToolFunnelOut`, used by `/analytics/tools` items, `/tools/{id}`
  `.tool`): new field `kind: "tool" | "skill"`. For skills, `toolName` = skill
  name, `serverName` = skill **source** name, `enabled` = source `enabled`;
  `tokens` is `null` (skill token economy not yet wired — see below).
- **`GET /analytics/tools?kind=tool|skill|all`** (default `all`; anything else
  422). `all` lists catalog tools + catalog skills (zero rows included) + any
  removed id with funnel data.
- **`GET /analytics/tools/{id}`** accepts `skill:<id>` (path max_length 42);
  unknown skill → 404 like tools. Position curve + co-surfacing are kind-blind,
  so skills appear in tools' co-surfaced lists (with `toolName`/`serverName`
  resolved) and vice versa.
- **Profiles**: per-agent `attributed`/selection counts include skill
  activations via the shared ATT_CTE (no separate skill columns yet).

**Not done in S2f (deferred):** economy (`skillMetadataTokens`,
`skillBodyTokensExposed`, overview `skills{...}`), per-agent skill
activationRate, skill staleness, `kind` on wasted-exposure suggestions,
`mcpr_analytics_skills_{surfaced,activated}_total` counters.

**Known gap (pre-existing, not introduced by S2f; reviewer finding):**
`economy._DEC_CTE` counts a decision as complete only when every surfaced id
is in `tool_token_map`. Skill ids never are, so any decision that surfaces a
skill drops out of the context economy and is counted in
`staleRefDecisions`. On mixed traffic the economy undercounts and shows false
drift until the economy item treats `skill:` ids as known.

**Rollup backfill:** days rolled up before this change keep skill
`selected=0`. Recompute them (`POST /analytics/rollup`) to pick up skill
activations.

**Metrics:** `mcpr_analytics_tools_{selected,succeeded}` now include skill
activations, as `surfaced` already did. Their help text still says "Tool".
