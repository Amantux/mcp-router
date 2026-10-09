# A1 — REST API surface audit (MCP Router v0.5.0, master 64bcbfc)

Scope: the 62 `(METHOD, path)` routes in `docs/audit/openapi-routes.json`. Read-only audit; nothing was executed against the DB.

Method and confidence:
- **Auth, validation, error paths** were read from `src/mcprouter/api/routes_*.py`, `deps_auth.py`, `app.py`, `body_limit.py`. Every finding below cites file:line.
- **Test coverage** comes from an AST scan of `tests/*.py`, with helper-built URLs resolved by hand. A status code counts as covered only if an assertion on it sits in the same or next statement as the request. Two routes were missed by the scan and checked by hand: `decision/systemone` is called through the `PATH` constant in `tests/edge_app_helpers.py:15`, and `feedback` through `_url()` in `tests/test_feedback_api.py:35`. Treat the per-route codes as accurate to about one status code per row.
- **Verified by repro** (`.venv/bin/python`, no DB):
  - Unicode digit hop header: `"²".isdigit()` is True and `int()` raises `ValueError`.
  - Default FastAPI 422 echoes the request body, including `env` values (see A1-007).
  - `SourcePatch` accepts explicit `null` for every field, including `enabled`, `name` and `syncIntervalS` (see A1-001).

Auth legend:
- **ADMIN** = `Depends(require_admin)`. 401 for a missing or wrong bearer, and an agent key counts as wrong. 403 `admin token not configured` when `MCPR_ADMIN_TOKEN` is unset outside dev mode (`deps_auth.py:225-234`, `299-307`). In dev mode (no agent keys, no admin token, zero principals) it is open (`deps_auth.py:191-195`).
- **AGENT** = `get_principal` (`deps_auth.py:290-296`). Missing or invalid bearer is 401. Dev mode yields the synthetic `dev` principal.
- **EITHER** = the handler branches on `is_admin_bearer`. An admin must name `agentId` and runs under that agent's policy (400 if it doesn't, 404 `Unknown agent.` if it isn't a principal, 403 if the agent is disabled). An agent key may only act as itself (403 otherwise). Used by `execute`, `feedback` and the skills agent routes.
- **NONE** = unauthenticated.

## Findings

No blockers found. The admin gate is router-level on every management router and the agent routes derive identity from the credential only. The items below are should-fix hardening and test gaps.

| id | severity | route | file:line | finding | proposed fix | test to add |
|---|---|---|---|---|---|---|
| A1-001 | should | PATCH /api/v1/skill-sources/{sid} | routes_skill_sources.py:40-46,104-118; models.py:294-298 | `SourcePatch` fields are `X \| None`, so explicit JSON `null` passes validation, and `model_dump(exclude_unset=True)` keeps it (repro: `{'enabled': None}`, `{'name': None, 'sync_interval_s': None}`). The handler then does `setattr(rec, k, None)`. `{"location": null}` goes into `validate_location(kind, None, ...)` and raises `TypeError`/`AttributeError`. `{"enabled": null}`, `{"name": null}` or `{"syncIntervalS": null}` violate NOT NULL at `commit()`. All of these are unhandled 500s. | Drop `None` for non-nullable fields, e.g. `upd = {k: v for k, v in upd.items() if v is not None or k == "git_ref"}`. Better: type the fields non-optional and rely on `exclude_unset` (only `git_ref` stays nullable). | `test_skill_source_patch_null_is_422_or_ignored`: PATCH each of `{enabled,name,location,syncIntervalS}: null` returns non-500 and the row is unchanged. |
| A1-002 | should | PATCH /api/v1/skill-sources/{sid} | routes_skill_sources.py:104-118; models.py:294 (`name` unique) | Renaming to an existing name has no duplicate check (POST does, at :90). The unique constraint violation surfaces at `commit()` as an unhandled `IntegrityError`, so 500. | Mirror the POST pre-check and catch `IntegrityError`, returning 409 `a skill source with that name exists`. | `test_patch_skill_source_duplicate_name_409`. |
| A1-003 | should | GET/POST/PATCH /api/v1/skill-sources[/{sid}] | routes_skill_sources.py:53-65,83-95; skills/sources.py:43-55 | `validate_location` only requires an `https://` prefix, so `https://user:token@host/repo` is accepted and stored. `_out()` returns `location` raw on every read. `GET /servers` already redacts endpoint secrets (`routes_servers.py:157-165`); skill sources do not. `tests/test_skill_source_git_errors.py:23` covers scrubbing of git error text only, not stored-URL echo. | Reject userinfo in `validate_location` (`urlsplit(...).username/password`), or redact it in `_out()` the way `redact_endpoint` does. Prefer rejecting. | `test_skill_source_location_with_userinfo_rejected_or_redacted`: POST `https://u:pw@h/r`, assert 422 or that no GET response contains `pw`. |
| A1-004 | should | POST /api/v1/decision/systemone | routes_decision.py:80-83 | The hop header is parsed with `raw_hop.isdigit()` then `int(raw_hop)`. `str.isdigit()` is True for superscript digits such as `"²"` (arrives via latin-1 header decode), and `int()` then raises `ValueError`. A 5001-digit value also raises (int digit limit). Either one is an unhandled 500 for any authenticated agent. Existing tests only use `"1","7","junk"` (`test_edge_hop_guard.py:23`). | Use `raw_hop.isascii() and raw_hop.isdecimal()` and cap the length, or wrap `int()` in `try/except ValueError` and treat the value as hop 1. | Extend the `hop` parametrization in `test_edge_hop_guard.py` with `"²"` and `"9"*5000`, asserting 503 (remote) or 200 (local) and never 500. |
| A1-005 | should | POST /api/v1/decision/systemone | routes_decision.py:87-88 | The 429 branch (`SlidingWindowLimiter`, 120/min per principal) has no test. `test_edge_deadline.py` and `test_edge_roundtrip.py` only hit 200, 401, 422, 503. | None needed in code. | `test_systemone_rate_limit_429`: set `app.state.decision_edge_limiter = SlidingWindowLimiter(2, 60)`, post 3 times, assert `[200, 200, 429]` and that principal B is unaffected. |
| A1-006 | should | all admin routes except 13 | tests/test_integration_auth.py:33-46,64-77 | The generic "missing/agent key/wrong token is 401, admin passes" matrix `MANAGEMENT` has 13 rows. The other ~25 admin routes rely on per-feature tests that miss several. No 401 test at all: `GET/PATCH/DELETE /principals/{id}`, `POST rotate-key`, `PATCH/DELETE /policy-rules/{id}`, `GET/PATCH /skill-sources/{sid}`, `POST /skill-sources`, `POST /skill-sources/{sid}/sync`, `DELETE /skill-sources/{sid}`, `GET /skills/{id}`, `/body`, `/versions`. Tools routes are only tested for the 403 fail-closed variant (`test_tools_api.py:197-223`), not for 401 with a configured admin token. | Generate the matrix from the app's route table (see Meta-test proposal, part C) instead of hand-listing. | `test_every_admin_route_rejects_anonymous_and_agent_key` (generated). |
| A1-007 | should | POST /api/v1/servers (and every non-/route body) | routes_servers.py:64-76; routes_route.py:137-148; app.py (no handler registered for other paths) | Only `/api/v1/route*` has a curated 422 handler (it exists because "the query may carry secrets"). Everywhere else FastAPI's default 422 echoes `input`. Repro with `ServerIn`: `POST {"name":"a","env":{"TOKEN":"s3cret"}}` returns 422 containing `"input":{"name":"a","env":{"TOKEN":"s3cret"}}`. The echo goes back to the admin who sent it, but it lands in any proxy or client log of response bodies. `test_servers_api.py:82` asserts `env` is write-only only for the success path. | Extend `_curated_validation_error` to all `/api/` paths, or at least drop `input` for `/servers`. | `test_validation_error_does_not_echo_env_values`: POST a server missing `transport` with `env={"K":"sentinel"}`, assert `"sentinel" not in r.text`. |
| A1-008 | should | POST /api/v1/principals | routes_policy.py:220-238; deps_auth.py:191-195,225-231; routes_setup.py:47-56 | In dev mode (no admin token), creating the first principal flips `_dev_mode_active` to false (it requires zero principals). With no admin token configured, `check_admin` then raises `PermissionError`, so every admin route returns 403 permanently. The first-run wizard (`needsSetup = principals == 0`) is exactly the flow that creates the first principal. `test_setup_routes.py:71-79` (`test_dev_mode_open_or_fail_closed`) returns early on 403 and so asserts nothing deterministic about this transition. INSTALL.md:322 describes dev mode but not the lock-out. | Document it. Optionally have `POST /principals` refuse (409/400) in dev mode unless an admin token exists, or return a warning field. | Replace the vacuous test with a deterministic one: fresh DB, no token, `POST /principals` returns 201, then `GET /principals` returns 403. Pin the intended behaviour either way. |
| A1-009 | should | all agent routes of skills | routes_skills.py:300-315,357-413; tests/test_skills_exposure_rest.py | No test asserts 401 for a missing or invalid key on `GET /skills/bundle`, `POST /skills/{id}/activate`, `GET /skills/{id}/resources/{path}` (the file contains no `401`). `GET /me/approvals/{id}` and `POST /route/{request_id}/feedback` have no 401 test either (`test_feedback_api.py` has no 401). These are all EITHER/AGENT routes where the auth branch lives inside the handler, so removing it would not trip any router-level test. | None needed in code. | Parametrized `test_agent_routes_401_without_key` over these 5 routes. Mutation check: delete the `get_principal` call in `_acting_agent` or `post_feedback`, and the test must fail. |
| A1-010 | should | GET /api/v1/skill-sources/{sid}, PATCH /api/v1/skill-sources/{sid} | routes_skill_sources.py:98-118 | No test in `tests/` calls either route (confirmed by scan plus grep of `skill-sources/{`). `GET` and `PATCH` 404 are likewise unexercised. | None. | `test_skill_source_get_patch_404_and_happy`: GET and PATCH unknown id returns 404; PATCH `{"enabled": false}` returns 200; PATCH with an invalid location returns 422. |
| A1-011 | should | GET /skills/{id}, /body, /versions; PATCH /skills/{id}/classification | routes_skills.py:52-56,99,134,162,169 | The 404 branch (`_get`) is untested for all four. `test_skills_api.py` is one linear `test_api_flow` that never requests an unknown id. | None. | Parametrized 404 test over the four routes with an unknown id. |
| A1-012 | should | principals/rules unknown-id paths | routes_policy.py:241-245,326-330 | 404 for `GET/PATCH/DELETE /principals/{id}`, `POST rotate-key`, `PATCH/DELETE /policy-rules/{id}` has no test. Only `approve` unknown is tested (`test_policy_routes.py:244`). 410 for an expired approval (`_APPROVAL_HTTP`, :186) is also untested for approve and deny. | None. | Parametrized 404 test (7 routes), plus `test_approve_expired_is_410`. |
| A1-013 | should | POST /api/v1/route/{request_id}/feedback | routes_feedback.py:27-45; tests/test_feedback_api.py | Request-schema validation is untested: `extra="forbid"` rejects unknown keys, empty `items` fails `min_length=1`, `items > 50` fails `max_length=50`, `note` over 2000 chars fails. The only 422 test is "target not surfaced" (`:74`). | None. | `test_feedback_body_validation_422` with `{items: []}`, `{items: [{helpful: true, bogus: 1}]}`, 51 items, and a 2001-char note. |
| A1-014 | should | INSTALL.md vs deps_auth | docs/INSTALL.md:318; deps_auth.py:233-234; tests/test_integration_auth.py:67 | The troubleshooting table says `403 on /api/v1/*` means "You used an agent key". The code and tests give **401** (`invalid or missing admin token`) for an agent key on an admin route. 403 only happens when `MCPR_ADMIN_TOKEN` is unset outside dev mode. An operator following the doc will chase the wrong cause. | Fix the row: agent key on admin route gives 401; 403 means the admin token is not configured. | Doc-lint test or manual check. The code behaviour is already tested. |
| A1-015 | nit | POST /tools/{id}/execute | routes_execute.py:114-125; manager.py:381-410 | Policy denial, rate limit (`RATE_LIMITED`), unavailable and invalid-args outcomes are all HTTP 200 with a `status` field (`test_execute_api.py:112,148` assert 200 deliberately). A proxy, retry client or load balancer cannot see a 429. This is a design choice and uniform denial (`unknown tool or principal`, manager.py:381) is correct for the oracle. | Document in README/INSTALL that clients must read `status`, or add a `Retry-After` header on `rate_limited`. | `test_rate_limited_sets_retry_after` if changed. |
| A1-016 | nit | GET /api/v1/approvals | routes_policy.py:362-364; manager.py:662-667 | `status` is free text (an unknown value silently returns `[]`), and there is a silent hard `.limit(200)` with no `limit`/`offset`/`total`. If more than 200 rows match, older pending approvals are invisible. Every other list uses `limit/offset/total`. | `status: Literal["pending","approved","denied","expired","executing"] \| None` plus the standard page envelope. | `test_approvals_status_filter_422` and a >200 rows pagination test. |
| A1-017 | nit | list endpoints without pagination | routes_policy.py:213,297; routes_servers.py:173; routes_skill_sources.py:75; routes_skills.py:169 (`/versions`) | `GET /principals`, `/policy-rules`, `/servers`, `/skill-sources`, `/skills/{id}/versions` return unbounded arrays. `/tools`, `/skills`, `/executions`, `/dedup/suggestions`, `/analytics/tools` are paginated with `limit` ≤ 200/500. Bounds are asserted by test only for analytics (`test_analytics_api.py:246`) and tools. `/executions`, `/skills`, `/dedup/suggestions` have no `limit=0/201` test. | Fine for local scale. At least add bound tests for the paginated ones. | Parametrized `limit in (0, MAX+1)` and `offset=-1` returning 422 for the 5 paginated routes. |
| A1-018 | nit | casing and error-shape consistency | routes_route.py:165-178,157-162,407,269-272; routes_decision.py:102-108; routes_skills.py:31-49; routes_setup.py:53-65 | Mixed wire casing. `/route` response is snake_case (`request_id`, `latency_ms`) but nests camelCase `bodyTokensEst`. `/route/simulate` is camelCase (`routes_route.py:340-342` says this is intentional). `/decision/systemone` is snake (`input_tokens`). Everything else is camelCase. Request bodies accept both (`populate_by_name`). Error `detail` is sometimes a string, sometimes `{message, unknown_servers}` (`routes_route.py:266-273`), and sometimes a list (422). | Document in INSTALL "API conventions"; do not change existing contracts. | `test_openapi_property_casing`: assert each response schema is entirely camel or entirely snake, with an explicit allow-list of the 3 exceptions. |
| A1-019 | nit | missing `response_model` | routes_skills.py:59-187; routes_skill_sources.py:75-142; routes_setup.py:47,68; routes_decision.py:73; routes_policy.py (204s ok) | 13 routes return `dict[str, Any]`, so OpenAPI has no schema (client codegen and the UI types are hand-maintained). Only `models/health` has a schema test (`test_models_api.py:85`). | Add pydantic out-models; the manual camelCase dicts map directly. | Extend `test_openapi_has_typed_response_schema` to all 2xx JSON routes with an allow-list (the zip/bundle and resource routes return raw `Response`). |
| A1-020 | nit | 400 vs 422 for validation | routes_route.py:266-273; routes_analytics.py:159; routes_execute.py:156,163; routes_servers.py:220 | Validation-class failures use 400 in some places (`allowed_servers` unknown, rollup not allowed, agentId mismatch, invalid registration) and 422 in others (analytics window, rule refs, skill source location). | Document the split, or converge on 422 for body/query semantic errors. | None. |
| A1-021 | nit | body size cap | body_limit.py:15,33-47; app.py:239; tests/test_body_size_cap.py | The 1 MiB cap (413) is middleware-wide, but tests only exercise it through `/decision/systemone`. No test covers it on a management route (e.g. `POST /servers/import`, which takes `dict[str, Any]`) or `/mcp`. Not mentioned in INSTALL/security-model/deploy (grep found no `413` or `1 MiB`). | Document the limit. | Parametrize `test_body_size_cap` over 3 routes: `/servers/import`, `/route`, `/policy-rules`. |
| A1-022 | nit | unauthenticated surfaces | app.py:135,227,235 | `/docs`, `/openapi.json`, `/healthz`, `/metrics` are unauthenticated (`/metrics` has no per-tool labels, `analytics/metrics.py:21`). `/healthz` runs `SELECT 1` and returns the framework's bare 500 when the DB is down (no test: `GET /healthz` only has 200 tests). `docs/security-model.md` does not list them as intentionally open. | List them in security-model.md and deploy.md (network exposure guidance). | `test_healthz_500_when_db_down` (patch `engine.connect`). |
| A1-023 | nit | POST /skill-sources/{sid}/sync | routes_skill_sources.py:132-142; skills/sources.py:65-80 | A git failure or unreadable root returns HTTP 200 with `{"error": ..., "added": 0, ...}`, so a caller treating 2xx as success misses it (covered only as message-scrub in `test_skill_source_git_errors.py`). The sync runs inline in the request with no timeout. | Return 502 on `error`, or document that `error` must be checked. | `test_sync_git_failure_status`. |
| A1-024 | nit | POST /servers, POST /skill-sources | routes_servers.py:64-76; routes_skill_sources.py:33 | `ServerIn` has no `extra="forbid"`, so typos such as `transprt` are silently dropped. `ServerIn.command` items and `env` keys/values have no length caps (bounded only by the 1 MiB body cap). `SourceIn.kind` is a bare `str`, so OpenAPI shows no enum even though only `directory`/`git` pass. | Add `extra="forbid"` (UI sends known fields) and `Literal["directory","git"]`. | `test_create_server_unknown_field_422`. |
| A1-025 | nit | operator docs gaps | docs/INSTALL.md; docs/deploy.md; README.md | No operator-facing page documents: `PATCH/DELETE /policy-rules/{id}` (grep of README, INSTALL, deploy, security-model, skills, analytics, scoping, backends, SPEC: no `policy-rules/`); how to list, approve and deny approvals (only README/security-model mention it); the `setup/*` endpoints (only deploy.md); 1 MiB cap; `/metrics`; the execute-200 convention (A1-015); no consolidated REST reference (the per-route text is spread across `INTEGRATION_NOTES-*` developer notes). `/docs` exists at runtime (`app.py:135`) but INSTALL does not point to it. | Add an "API reference" section to INSTALL.md: link `/docs`, list the casing and status conventions, and list auth per route class. | Doc lint: every `(method, path)` in the OpenAPI snapshot appears in `docs/` or is allow-listed. |

Verified-clean (no finding), for the record:
- **Typed errors:** every `str(exc)` at the API boundary maps a typed domain exception. These are `InvalidWindow`/`RollupNotAllowed` (`routes_analytics.py:82,159`), `SystemOneRequestError` (`routes_decision.py:92`), `Feedback*` (`routes_feedback.py:70-74`) and `SourceError` (`routes_skill_sources.py:88,114`). No bare `except Exception` returns text. `refresh` returns `ConnectorError.message`, which is documented as safe (`mcpclient/errors.py:26`).
- **Existence oracles:**
  - Out-of-scope vs unknown server name in `/route` is indistinguishable (`routes_route.py:249-274`, tested at `test_route_api.py:200`).
  - Feedback on another agent's or a simulated decision gives a uniform 404, and probing is rate-limited before lookup (`feedback.py:56-69`, tested).
  - Skill activate/bundle/resource share one 404 message (`routes_skills.py:228-269`, tested at `test_skills_exposure_rest.py:118-130`).
  - Execute denies unknown and out-of-scope tools identically (`manager.py:381-386`).
  - `GET /me/approvals/{id}` returns 404 for another agent's approval (tested at `test_policy_routes.py:256`).
- **Identity:** `/route` ignores a body `agent_id` unless it equals the credential (403, `routes_route.py:287-291`, tested). Only the `Authorization` header is read (tested in `test_gateway_auth.py:157`).

## Coverage matrix

Legend: **auth** A=ADMIN, G=AGENT, E=EITHER, -=none. Test names drop `test_`/`.py`. Codes are asserted codes found by the scan, per statement, so treat them as approximate. "Gaps" lists finding ids and short-form gaps. 401 = missing/invalid credential test; 404 = unknown id.

| route | auth | tests | codes asserted | gaps |
|---|---|---|---|---|
| DELETE /policy-rules/{rule_id} | A | policy_routes, route_cache | 204 | 401, 404 (A1-006, A1-012) |
| DELETE /principals/{principal_id} | A | policy_routes, route_cache | 204 | 401, 404 (A1-006, A1-012) |
| DELETE /servers/{server_id} | A | servers_api, integration_auth | 204, 404, 409, 401 | none |
| DELETE /skill-sources/{sid} | A | skills_api | 204, 409 | 401, 404 (A1-006, A1-010) |
| GET /analytics/agents | A | analytics_api, e2e_integration, savings_metrics | 200, 401 | none |
| GET /analytics/overview | A | analytics_api, e2e_integration, e2e_skills, savings_metrics | 200, 401 | `window` 422 only tested via /tools |
| GET /analytics/suggestions | A | analytics_api | 200, 401 | minSurfaced/maxSelectionRate/staleDays 422 bounds untested |
| GET /analytics/tools | A | analytics_api, e2e_integration, skills_analytics | 200, 401, 422 | none |
| GET /analytics/tools/{tool_id} | A | analytics_api, skills_analytics | 200, 401, 404 | tool_id over 42 chars (422) |
| GET /approvals | A | policy_routes | 200, 401, 503 | status filter, 200 cap (A1-016) |
| GET /dedup/suggestions | A | tools_api, integration_auth | 200, 401, 403 | status/limit/offset 422 (A1-017) |
| GET /executions | A | execute_api, e2e_integration, integration_auth | 200, 401 | limit/offset 422, outcome filter (A1-017) |
| GET /me | G | policy_routes | 200, 401 | none |
| GET /me/approvals/{approval_id} | G | policy_routes | 200, 404 | 401 (A1-009) |
| GET /models/health | A | models_api, backend_wiring, integration_auth | 200, 401, 503 | none |
| GET /policy-rules | A | policy_routes, integration_auth | 200, 401, 403 | `agentId` filter value untested |
| GET /principals | A | policy_routes, route_skills_api | 200, 401 | none |
| GET /principals/{principal_id} | A | policy_routes | 200 | 401, 404 (A1-006, A1-012) |
| GET /servers | A | servers_api, integration_auth | 200, 401, 403 | none |
| GET /servers/{server_id} | A | servers_api, integration_auth | 200, 401, 404 | none |
| GET /setup/status | A | setup_routes | 200, 401, 403 | dev-mode transition vacuous (A1-008) |
| GET /skill-sources | A | skills_api | 200, 401/403 | none |
| GET /skill-sources/{sid} | A | **none** | none | A1-010 |
| GET /skills | A | skills_api, skills_classification_api | 200, 401/403 | filter and limit bounds 422 (A1-017) |
| GET /skills/bundle | E | skills_exposure_rest, e2e_skills | 200, 403, 404, 409, 413 | 401 (A1-009) |
| GET /skills/{skill_id} | A | skills_api, e2e_skills | 200 | 401, 404 (A1-006, A1-011) |
| GET /skills/{skill_id}/body | A | skills_api | 200 | 401, 404 (A1-006, A1-011) |
| GET /skills/{skill_id}/resources/{path} | E | skills_exposure_rest, skills_exposure_serve_hardening | 200, 404, 409 | 401 (A1-009) |
| GET /skills/{skill_id}/versions | A | skills_api | 200 | 401, 404 (A1-006, A1-011) |
| GET /tools | A | tools_api, integration_auth | 200, 401, 422 | none |
| GET /tools/{tool_id} | A | tools_api | 200, 404, 403 (fail-closed only) | 401 (A1-006) |
| GET /healthz | - | scaffold, webserver_static | 200 | DB-down 500 (A1-022) |
| PATCH /policy-rules/{rule_id} | A | policy_routes, route_cache | 200 | 401, 404, 422 (A1-006, A1-012) |
| PATCH /principals/{principal_id} | A | policy_routes, budgets, route_cache, route_skills_api | 200, 422 | 401, 404 (A1-006, A1-012) |
| PATCH /servers/{server_id} | A | servers_api, integration_auth | 200, 401, 404, 422 | none |
| PATCH /skill-sources/{sid} | A | **none** | none | A1-001, A1-002, A1-010 |
| PATCH /skills/{skill_id}/classification | A | skills_classification_api | 200, 401, 403, 422 | 404 (A1-011) |
| PATCH /tools/{tool_id}/classification | A | tools_api | 200, 422, 403 (fail-closed only) | 401, 404 |
| POST /analytics/rollup | A | analytics_api | 200, 400, 401, 422 | none |
| POST /approvals/{approval_id}/approve | A | policy_routes | 200, 401, 404, 409 | 410 (A1-012) |
| POST /approvals/{approval_id}/deny | A | policy_routes | 200, 409 | 401, 404, 410 (A1-012) |
| POST /decision/systemone | G | edge_roundtrip, edge_hop_guard, edge_question_keys, edge_deadline, body_size_cap | 200, 401, 413, 422, 503 | 429, unicode hop (A1-004, A1-005) |
| POST /dedup/suggestions | A | tools_api | 200, 403 (fail-closed) | 401, threshold 422 bounds |
| POST /dedup/suggestions/{id}/accept | A | tools_api | 200, 404, 409 | 401 |
| POST /dedup/suggestions/{id}/dismiss | A | tools_api | 200, 400/422 | 401, 404 |
| POST /policy-rules | A | policy_routes, e2e_integration, e2e_skills, route_cache | 201, 401, 422 | none |
| POST /principals | A | policy_routes, budgets, route_cache | 201, 401, 409, 422 | dev-mode lockout (A1-008) |
| POST /principals/{id}/rotate-key | A | policy_routes | 200 | 401, 404 (A1-006, A1-012) |
| POST /route | G | route_api, budgets, route_cache, route_skills, e2e_* | 200, 400, 401, 403, 422, 503 | no 429 exists, not rate limited (by design?) |
| POST /route/evaluate | A | eval_framework, route_cache, integration_auth | 200, 401, 404 | 422 (`max_tools` bounds) |
| POST /route/simulate | A | route_simulate, route_skills_api, e2e_* | 200, 401, 403, 404, 422 | none |
| POST /route/{request_id}/feedback | E | feedback_api | 200, 404, 422, 429 | 401, schema 422 (A1-009, A1-013) |
| POST /servers | A | servers_api, integration_auth, e2e_* | 201, 401, 403, 409, 422 | A1-007 |
| POST /servers/import | A | servers_api, integration_auth | 200, 400, 401 | 409 race, non-object body 422 |
| POST /servers/{id}/refresh | A | servers_api, lifecycle, e2e_* | 200, 401, 404, 409, 502, 504 | none |
| POST /setup/complete | A | setup_routes | 200, 401 | none |
| POST /skill-sources | A | skills_api, skill_source_git_errors, e2e_skills | 201, 422 | 401, 409 duplicate name (A1-003, A1-006) |
| POST /skill-sources/{sid}/sync | A | skills_api, skill_source_git_errors, e2e_skills | 200 | 401, 404 (A1-006, A1-023) |
| POST /skills/{skill_id}/activate | E | skills_exposure_rest, e2e_skills | 200, 403, 404, 429 | 401 (A1-009) |
| POST /tools/{tool_id}/disable | A | tools_api | 200, 404, 403 (fail-closed) | 401 (A1-006) |
| POST /tools/{tool_id}/enable | A | tools_api, route_cache | 200, 404, 403 (fail-closed) | 401 (A1-006) |
| POST /tools/{tool_id}/execute | E | execute_api, e2e_integration | 200, 400, 401, 403, 404, 422 | none |

Routes with zero test requests: `GET /skill-sources/{sid}` and `PATCH /skill-sources/{sid}` (A1-010). No other route has zero coverage.

## Meta-test proposal: every OpenAPI `(method, path)` is hit

Facts about this codebase that shape the design:
- Tests drive the app with Starlette's `TestClient` in 51 places. They reach the app via `c.app` (`test_analytics_api.py` uses `c.app.state...`).
- Many tests build **partial** apps (`FastAPI()` plus one `install_*`, e.g. `test_analytics_api.py:40-50`, `test_execute_api.py:55-62`), so recording must resolve the route template from the app the client holds, not from a global app.
- Every `create_app()` needs Postgres (`tests/conftest.py:requires_db`). The openapi snapshot `docs/audit/openapi-routes.json` is committed.
- One test module uses real uvicorn plus `httpx` (`test_edge_roundtrip.py`). Its calls are invisible to a TestClient hook, but `systemone` is also hit via TestClient in four other files.

**Part A — recorder (tests/conftest.py, about 25 lines).** Wrap `TestClient.request`. `TestClient.request` is the single choke point in Starlette, because `.get/.post/.patch/.delete` delegate to it. After the call, resolve the route template from `self.app` via `route.matches`, and store `(METHOD, template, status)`.

```python
# tests/conftest.py (additions)
import json, os, pathlib
from starlette.routing import Match
from starlette.testclient import TestClient

HITS: dict[tuple[str, str], set[int]] = {}
_real_request = TestClient.request

def _template(app, method: str, path: str) -> str | None:
    scope = {"type": "http", "method": method, "path": path, "root_path": "", "headers": []}
    for r in getattr(app, "routes", []):          # APIRoute.path is the template, e.g. /api/v1/servers/{server_id}
        m, _ = r.matches(scope)
        if m is Match.FULL:
            return getattr(r, "path", None)
    return None

def _recording_request(self, method, url, *a, **kw):
    resp = _real_request(self, method, url, *a, **kw)
    path = str(resp.request.url.path)
    tpl = _template(self.app, method.upper(), path)
    if tpl:
        HITS.setdefault((method.upper(), tpl), set()).add(resp.status_code)
    return resp

def pytest_configure(config):
    TestClient.request = _recording_request

def pytest_sessionfinish(session, exitstatus):
    out = pathlib.Path(os.environ.get("MCPR_ROUTE_HITS", ".route_hits.json"))
    prev = json.loads(out.read_text()) if out.exists() and os.environ.get("MCPR_ROUTE_HITS_MERGE") else {}
    for (m, p), codes in HITS.items():
        prev.setdefault(f"{m} {p}", [])
        prev[f"{m} {p}"] = sorted(set(prev[f"{m} {p}"]) | codes)
    out.write_text(json.dumps(prev, indent=1))
```

Notes:
- `r.matches` on `Mount` entries (`/metrics`, `/mcp`) returns FULL with no `.path` template, so they are skipped. Add an allow-list entry if wanted.
- If xdist is ever adopted (not in `pyproject.toml` today), write a per-worker file and merge. The dump-and-merge shape already allows this.

**Part B — the meta-test (tests/test_zz_route_coverage.py).** Two layers, so it is useful both in a full run and a partial run:

```python
# 1) Snapshot drift guard (needs DB, runs anywhere). Fails when a route is added/removed without updating the snapshot.
@requires_db
def test_openapi_route_snapshot_is_current():
    app = create_app(Settings(database_url=TEST_DB_URL), env={})
    spec = {(m.upper(), p) for p, ops in app.openapi()["paths"].items()
            for m in ops if m in {"get","post","put","patch","delete"}}
    snap = {tuple(x) for x in json.loads(Path("docs/audit/openapi-routes.json").read_text())}
    assert spec == snap, f"added={sorted(spec-snap)} removed={sorted(snap-spec)}"

# 2) Coverage gate (only meaningful after the whole suite). Runs last by filename (test_zz_*),
#    skips unless the full suite ran (HITS non-empty AND >N files collected).
EXEMPT = {  # (method, path): reason. Must stay tiny and justified.
}
def test_every_route_is_requested_at_least_once(request):
    from tests.conftest import HITS
    if request.config.getoption("-k") or len(request.session.items) < 500:   # partial run: skip, don't false-fail
        pytest.skip("coverage gate runs only on the full suite")
    snap = {tuple(x) for x in json.loads(Path("docs/audit/openapi-routes.json").read_text())}
    hit  = set(HITS)
    missing = sorted(snap - hit - set(EXEMPT))
    assert not missing, f"routes never requested by any test: {missing}"
    # stronger invariants, still cheap:
    for key in snap - set(EXEMPT):
        codes = HITS.get(key, set())
        assert codes & set(range(200, 300)) or key[1] in NEGATIVE_ONLY, f"{key} never succeeded"
```

Items are collected in alphabetical order by file, so `test_zz_*` runs after the rest in a default single-process run. Alternatively use a `pytest_sessionfinish` hook that raises `pytest.exit` when `exitstatus == 0` and routes are missing, which avoids ordering assumptions. For the real conftest this should be gated by `MCPR_ENFORCE_ROUTE_COVERAGE=1` in CI.

With the current suite the gate would immediately flag: `GET /skill-sources/{sid}` and `PATCH /skill-sources/{sid}`. It would also flag any route that a test only reaches through the uvicorn path. The hook's 2xx check additionally catches routes that only ever receive 401/403/422 from tests.

**Part C — generated auth matrix (closes A1-006, A1-009).** Derive the matrix from the live route table instead of a hand-written list. Admin routes can be found by walking `route.dependant` recursively for `deps_auth.require_admin` (router-level `dependencies=[...]` are merged into each route's dependant by `include_router`). Routes using `is_admin_bearer` inside the handler (`execute`, `feedback`, `skills` agent routes) cannot be introspected, so list them in a small explicit `EITHER = {...}` map.

```python
def _calls(dep):                       # flatten FastAPI Dependant
    yield dep.call
    for d in dep.dependencies: yield from _calls(d)

@requires_db
@pytest.mark.parametrize("route", _admin_routes(), ids=lambda r: f"{sorted(r.methods)[0]} {r.path}")
def test_admin_route_gate(app_with_admin_and_agent, route):
    c = TestClient(app_with_admin_and_agent)
    path = re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", route.path)
    m = sorted(route.methods)[0]
    for tok in (None, AGENT_KEY, "wrong"):
        assert c.request(m, path, json={}, headers=auth(tok)).status_code == 401
    assert c.request(m, path, json={}, headers=auth(ADMIN)).status_code not in (401, 403)
```

Dependency exceptions (`HTTPException` 401) are raised during `solve_dependencies` before body or path validation errors are returned, so a placeholder id and an empty body are sufficient for the 401 assertions. The final line only proves the gate passes; body validation can still return 404/422, which is fine. `tests/test_integration_auth.py:64-77` is the existing pattern to generalize, with the same mutation target (remove `dependencies=[Depends(require_admin)]` from a router, test fails).

Mutation checks (per user rules; do before merging):
1. Delete one `@router.get` handler's test, so the coverage gate must fail.
2. Remove `dependencies=[Depends(require_admin)]` from `routes_skill_sources.router`, so the generated auth matrix must fail on all 6 skill-source routes.
3. Add a dummy route to `routes_tools.py`, so the snapshot-drift test must fail.
