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

## S2g — skills eval cases (`wave4/skills-eval`)

- **Fixture** (`eval/synthetic_catalog.py`, appended): `SKILLS` (15 skills,
  sources `team-skills` + `community-skills`, all five tool domains),
  `NEAR_DUPLICATE_SKILLS` (pdf-fill ~ pdf-form-filler, pr-review ~
  pr-reviewer), two execute-class skills (`deploy-service`,
  `schema-migration`: has_scripts + `Bash`). `seed_synthetic_skills(s, embedder)`
  inserts them with uuid5 ids and ground-truth classification marked
  `classification_reviewed=True` / `classification_source="synthetic-ground-truth"`
  (so `apply_skill_classification` never moves it).
  `skill_classification_mismatches()` is asserted empty: `classify_skill`
  agrees with every ground-truth operation today.
- **Dataset** (`synthetic_v1.jsonl`, appended; tool cases untouched): 22
  skill-only cases (`skill_direct` 12, `skill_ambiguous` 4 — each names a
  near-dup pair, `skill_unauthorized` 6 — `readonly-agent` with write/execute
  skills in `forbidden_skills`; 2 of those have `kinds: ["skill"]` and no
  expected skill) + 12 `mixed` cases (ids `mx01..mx12`, expected tool AND
  skill; `mx12` uses `allowed_servers: ["github"]`). Mixed cases also count in
  the TOOL metrics' positive set, so tool baseline denominators grew 12.
- **Scope note:** skill "allowed scope" is expressed through the agent's
  scope (read ceiling), not `allowed_servers` — server scoping is MCP-server
  only (see Policy above), and an unknown `allowed_servers` name errors the case.
- **Metrics proof** (`tests/test_skills_eval_cases.py`): a fake route returning
  mixed kinds over the new cases pins `skills.top1_accuracy`, `top5_recall`,
  `unauthorized_skill_exposures`, `mixed.both_top1_rate` to exact values.
- **INTEGRATOR TODO:** the LIVE skills baseline over the real pipeline is not
  run here — S2d's skill-aware pipeline lives on another branch. After merge,
  seed `seed_synthetic_catalog` + `seed_synthetic_skills` in
  `test_synthetic_v1_baseline` and report the skills/mixed numbers; assert
  `unauthorized_skill_exposures == 0` as a security invariant.
