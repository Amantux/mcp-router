# Wave 4 — final integration notes (Agent Skills, v0.4.0)

Branch `integrate/wave4`. The operator guide is [`skills.md`](skills.md). The
security posture is in [`security-model.md` §Skills](security-model.md#skills-wave-4-s3).
The design record is [`skills-plan.md`](skills-plan.md).

## What merged

| Branch | Scope | Notes |
|---|---|---|
| `wave4/scaffold` | models, settings block (`MCPR_*SKILL*`), fences | — |
| `wave4/sources` (S1) | skill sources (directory / git), walker, validate, hashing, ingest, testbed generator, read-side REST | [sources](INTEGRATION_NOTES-wave4-sources.md) |
| `wave4/routing-pipeline` (S2) | `resourceKind` policy, skill classifier, unified retriever, `maxSkills` budget, `/route` `skills[]` + simulate | [routing](INTEGRATION_NOTES-wave4-routing.md) |
| `wave4/skills-analytics` (S2f/h) | funnel `surfaced → activated`, economy skill fields, overview | routing notes §S2f/S2h |
| `wave4/skills-eval` (S2g) | skill + mixed eval cases | routing notes §S2g |
| `wave4/exposure` (S3, T3) | `SkillExposure`, MCP prompts/resources, meta-tools, safe serving, REST activate / resources / bundle | [exposure](INTEGRATION_NOTES-wave4-exposure.md) |
| `wave4/ui` (S4) | Skill sources, catalog, classification and funnel tabs, kind-aware Lens and Analytics, bundle download | [ui](INTEGRATION_NOTES-wave4-ui.md) |

Integrator follow-ups that landed on `integrate/wave4` itself: the scheduled
skill-source sync tick (`tests/test_skill_source_tick.py`), curated git sync
errors, admin `PATCH /api/v1/skills/{id}/classification`, the e2e skills story
(`tests/test_e2e_skills.py`), the synthetic baseline with skills seeded, and the
UI types aligned to the real wire.

## Decisions (binding)

- One exposure implementation, `gateway/skills.py::SkillExposure`, applies
  visibility, then rate limit, then policy, then audit, and only then returns
  the body. MCP, meta-tools and REST all call it.
- Visibility comes only from the agent's latest routing decision
  (`skill:<id>` in `selected_tool_ids`). It is never taken from the client.
- Unrouted or unknown access is audited `denied` ("not routed") and returns the
  same 404 as a nonexistent skill.
- A bundle counts as an activation. A failed bundle writes one `error` row, and
  a rate-limited bundle writes one `rate_limited` row.
- An admin acting as an agent must pass `agentId`. A disabled agent gets 403,
  and the call is audited `initiated_by="admin"` with `routeRequestId` ignored.
- An agent's `routeRequestId` is attributed only when the decision belongs to
  that agent. Otherwise it is stored as NULL.
- The risk class fails closed: `unknown` is treated as `execute`. Automatic
  re-classification can only move toward a stricter class. A human review is
  final until the content changes; a content change can escalate the class and
  sets `review_stale`.
- Ingest never edits content. Unknown frontmatter keys are accepted but flagged.

## Baseline

- `tests/test_eval_synthetic.py` (fallback backend): skills top-1 1.0 and top-5
  1.0, mixed 0.75, 0 unauthorized skill exposures.
- Ingest scale: 1,000 skills in about 3.5 s (`tests/test_skills_scale.py`).
- Version is 0.4.0 (`pyproject.toml`, `FastAPI(version=)`).

## Residuals

- Skill bodies and resources are untrusted instructions. They are served
  verbatim, never executed or rendered, and not redacted. A `secret_like`
  match is only flagged.
- Git URL validation is name and literal based, so a DNS-alias (rebinding)
  residual remains.
- Staleness suggestions for skills (the wasted-exposure and stale analytics
  that tools have) are not built yet. A resource changed on disk returns 409
  until the next sync.
- `src/mcprouter/__init__.__version__` and the MCP server's advertised version
  (`gateway/server.py`) still say 0.1.0. They were outside the docs fence.
