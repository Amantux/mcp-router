# MCP Router — wave-6 punch list (consolidated from audits A1–A7)

Base: `master` @ `64bcbfc` (A3/A4/A5/A7 read `1886ede`, verified to be an ancestor of `64bcbfc`; A1/A2/A6 read `64bcbfc`).
Inputs: `docs/audit/A1-rest-api.md … A7-ops.md`. **This file only consolidates them; no finding was re-verified here.**
Every executor re-verifies its finding at HEAD before fixing it (house rule: an audit finding is not automatically a bug).

Acceptance bar for the wave: **the entire surface is tested**. Every REST route, MCP method/meta-tool, UI route + client
export, `MCPR_*` setting, doc claim and container misconfiguration has a test, and meta-tests fail CI when one is added untested.

Legend: **sev** blocker (fix before any non-local deploy or a release) / should / nit. **eff** S ≤½ day, M ≈1 day, L 2+ days.
**T3** = schema, auth, public contract or deletes: the decision is pre-made below, but the executor still runs the
`reviewer` subagent before handing back. "Mut:" = the mutation that must turn the proving test red.

---

## 1. Decisions taken on the owner's behalf (binding for all executors)

| # | topic | decision |
|---|---|---|
| D1 | Default posture | **Fail closed.** No `MCPR_ADMIN_TOKEN` → entrypoint runs uvicorn on `127.0.0.1` and logs a WARNING (`admin API open; listening on loopback only; set MCPR_ADMIN_TOKEN`). Override `MCPR_ALLOW_OPEN_DEV=1` binds `0.0.0.0` in dev mode. Token set → `0.0.0.0`. Compose publishes `"${MCPR_BIND:-127.0.0.1}:${MCPR_HOST_PORT:-8400}:8400"`. Consequence (documented): compose without a token is reachable only from inside the container; the quickstart always generates a token. |
| D2 | Host allowlist | One app-wide ASGI `HostGuard` (outermost), allowed = `{localhost,127.0.0.1,[::1]}` ∪ `MCPR_ALLOWED_HOSTS`, port-insensitive, **421** JSON on mismatch, no path exemptions. SDK `/mcp` check stays (defence in depth). Tests admit `testserver` via a module constant `hardening._EXTRA_HOSTS` patched by an autouse fixture in `tests/support/settings.py` — never via env or `sys.modules` sniffing. |
| D3 | Compose env | `api` gets `env_file: [{path: .env, required: false}]` plus explicit derived vars (`MCPR_DATABASE_URL`). `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}` in base compose (dev default stays in `docker-compose.dev.yml`). |
| D4 | Logging | structlog → stdlib `ProcessorFormatter`; `MCPR_LOG_LEVEL` (INFO), `MCPR_LOG_FORMAT=console\|json` (console; compose sets json). Processors scrub CR/LF/ANSI and redact the configured secret values. Secrets never logged; exception TYPE only at boundaries (existing discipline). |
| D5 | Schema | Alembic: baseline `0001` generated from current metadata (all four MetaData objects + FTS/GIN/dedup/exec indexes + `vector` extension). Pre-0.6 DBs: legacy additive bridge (verbatim `_ADDITIVE_COLUMNS`) runs once, then `stamp 0001`. `init_db` = `pg_advisory_lock` → bridge-or-upgrade `head`; DB revision unknown to code → FATAL curated "database schema is newer than this build". Downgrade one step supported for revisions ≥ 0002. `upgrade head` at startup is documented, plus `python -m mcprouter.migrate`. |
| D6 | Version | Single source = `pyproject.toml`. Python reads `importlib.metadata.version("mcprouter")` everywhere (`__version__`, FastAPI, MCP serverInfo). UI gets `VITE_APP_VERSION` from a Docker build arg (fallback `package.json`, kept equal by a lockstep test). |
| D7 | Release | `release.yml` `verify` job (tag == pyproject version, CHANGELOG heading, green `CI` run for the SHA, SHA is an ancestor of `origin/master`); both image jobs `needs: verify`; trigger `v[0-9]+.[0-9]+.[0-9]+*`. Tag-protection ruleset = settings item to report, not commit. |
| D8 | Supply chain | `dependabot.yml` (pip `/`, npm `/ui`, docker `/`, github-actions `/`; weekly; minor+patch grouped, majors alone; ignore `mcp` major) + `security-scan.yml` per house playbook (CodeQL py+js-ts, pip-audit + npm audit advisory, dependency-review PR gate `high`, trivy fs+image SARIF, gitleaks `fetch-depth: 0` with existing `.gitleaks.toml`, SBOM). Top-level `contents: read`; `security-events: write` per job. Commit `uv.lock`; CI runs `uv lock --check`. |
| D9 | Dedup | `LIMIT :cap` (`MCPR_DEDUP_MAX_PAIRS`, default 5000) ordered by similarity + `truncated` flag; HNSW (`vector_cosine_ops`) on `mcp_tools.embedding` and `skills.embedding` (both `Vector(EMBEDDING_DIM)`, fixed dim — verified in `models.py:111,361`) in migration `0002`. |
| D10 | Shared code | One limiter registry (`mcprouter/limits.py`, keyed `(surface, agent)`, per-surface budgets). One `act_as_agent` dependency (`api/acting.py`). Auth core moves to `mcprouter/auth/`; `api/deps_auth.py` stays as a re-export façade (god-file playbook: every old import, incl. private names, keeps resolving; member count checked). |
| D11 | Dev-mode lockout (A1-008) | `POST /principals` in dev mode with no admin token → **409** "Set MCPR_ADMIN_TOKEN before creating the first principal; creating one ends dev mode." The setup wizard shows that message. |
| D12 | Dedup accept (A3-002) | Backend accepts optional `preferredToolId` (must be `toolA`/`toolB` of the pair, else 422) and persists `preferred_tool_id`; empty body keeps the scanner's pick (backward compatible). |
| D13 | Skills filters (A3-003) | Backend adds `Query(alias="sourceId")` (keeps `source`) and `hasScripts: bool \| None`. The client keeps its names. |
| D14 | Open surfaces | `/metrics` requires the admin bearer when an admin token is configured. `/docs` and `/openapi.json` stay open (the schema is not secret) and are documented as such. `/healthz` = liveness (DB down → curated 503); new `/readyz` = db + engine + degraded flag. |
| D15 | MCP sessions | Session `subject = hash_key(token)` (rotation forces re-initialize). `get_tool_input_schema=None` (no x-mcp-header support). `resources/templates/list` returns `[]`. |
| D16 | Image | **Add Node 22 (node + npm/npx) to the base image** so imported `npx` stdio servers run; record the size delta (base was 241 MB). `uvx` stays unsupported in the base image and is documented with an extend-the-image recipe. |
| D17 | Test suite | Per-xdist-worker databases from one template DB; `pytest-xdist`; `free_port()`/`run_app()` helpers; markers `db/e2e/slow/scale` with `--strict-markers`; nightly job runs `slow`+`scale`; `_CLEAN_TABLES` derived from metadata; no `from tests.test_*` imports (helpers in `tests/support/`); mypy on `tests`+`testbed` via a baseline, then strict. **No blanket retries — flakes are fixed.** |
| D18 | Docs | `INTEGRATION_NOTES-*` + `skills-plan.md` → `docs/history/` with the 22 code-comment paths rewritten. New: `CONTRIBUTING.md`, `docs/reference/configuration.md` (GENERATED from Settings, CI diff), `docs/reference/api.md` (GENERATED from OpenAPI, CI diff), `docs/upgrade.md`, `docs/architecture.md`. |
| D19 | Deferred (not this wave) | Envelope encryption of stdio env at rest (A4-013, documented instead); `gateway/server.py` split (A6-080); classification PATCH unification (A6-014); widening the ruff rule set (A6-060, touches every fence; integrator does it post-wave); A1-019 typed `response_model` for 13 dict routes; A3-019 Tools funnel column 500-row cap (documented by E7, not changed); A4-020 jsonschema key names in approval-replay errors (accepted as is); A4 residuals #2, #7, #9–12, #19–23, #30, #33–36 (unchanged, remain in A4 §4). |

---

## 2. Severity index (dedup result)

**Blockers (7):** P-101 open admin on 0.0.0.0 · P-102 no app-wide Host check (DNS rebinding → stdio RCE) · P-103 compose drops
env · P-107 `.env.example` crashes boot · P-301 MCP handshake leaks `str(exc)` · P-401 red/flaky Playground test ·
P-508 release ungated.

**Should (operator-visible or test-surface gaps):** P-104–106, P-108, P-109, P-111, P-201–206, P-208–212, P-302–306,
P-309, P-311, P-312, P-402–409, P-412, P-501–510, P-601–608, P-701–711.

**Nit:** P-110, P-207, P-307, P-308, P-310, P-410, P-411, P-609, P-610, P-712.

Merged duplicates (source ids → P): A4-001+A7-002 → P-101 · A4-002+A2-012(app part)+A4-018(headers) → P-102 ·
A7-001+A4-023+A4-022+A7-009+A7-016 → P-103 · A4-009+A5-002+A5-003 → P-107 · A2-002+A1-004+A1-005 → P-302 ·
A4-018+A7-006+A1-022+A7-017 → P-108 · A4-016+A6-015 → P-109 · A3-002+A4-res#34 → P-201/P-402 · A3-003 → P-202/P-403 ·
A3-001+A7-013 → P-401 · A4-011+A4-012+A7-005 → P-507 · A4-003+A7-004+A7-014 → P-508 · A5-026+A7-004(version)+A2 §2.1 → P-509 ·
A4-007+A5-006+A6-006+A7-007+A7-008+A4-res#4/#5 → P-601 · A6-010+A4-015 → P-606 · A6-024+A6-020+A4-014 → P-607 ·
A6-011+A6-012+A6-013+A6-023 → P-206 · A1-006+A1-009+A6-012(test) → P-210 · A5-017+A5-018+A6-026 → W0-1 ·
A6-043 → W0-2 · A2-015+A6-044+A4-res#29 → W0-2/P-311/P-504 · A6-045+A6-046+A6-050+A5-020(skip trap) → P-503.

---

## 3. Wave 0 — integrator, before fan-out (all executors branch from its tip)

| id | sources | content | rule |
|---|---|---|---|
| W0-1 | A5-017, A5-018, A6-026 | `git mv docs/INTEGRATION_NOTES-*.md docs/skills-plan.md docs/history/`; rewrite the 22 `INTEGRATION_NOTES` comment paths in `src/`+`tests/` (grep count before/after must match); `docs/history/README.md` ("archival, not operator docs"); fix the `skills.md` link. | Pure refactor commit, behaviour-neutral. |
| W0-2 | A6-043, A6-044, A2-015 | `tests/support/` package: move `test_execution_support.py`, `test_routing_fakes.py`, `test_analytics_support.py`, `test_registry_fixtures.py`, `edge_app_helpers.py` there (no `test_` prefix); rewrite every `from tests.test_x import` (17 private names); fixtures via `pytest_plugins`; add `ports.py` (`free_port()`, `bound_socket()`), `serve.py` (`run_app(app) -> (port, stop)` passing a pre-bound socket to `uvicorn.Server.serve(sockets=…)`), `wait.py` (`wait_for(cond, timeout)`). | Refactor commit; collected test count unchanged (report it). |
| W0-3 | seams | Stub modules + one call each from `api/app.py`: `api/hardening.py: install(app, settings)` (E1), `logging.py: configure_logging(settings)` (E1), `api/errors.py: install_error_handlers(app)` (E2), `limits.py: make_limiters(settings) -> app.state.limiters` (E6), `singleton.py: claim_loop_owner(engine)` (E6). Version literals in `app.py:135` and `gateway/server.py:364` → `mcprouter.__version__`. `Settings` field stubs (plain parse, E1 validates later): `admin_token` (`MCPR_ADMIN_TOKEN`), `log_level`, `log_format`, `allow_open_dev`, `mcp_max_sessions` (1000), `mcp_max_sessions_per_agent` (32), `decision_rate_limit_per_min` (120), `dedup_max_pairs` (5000). `conftest.py` `pytest_plugins` lists `tests.support.{db,settings,route_hits}` (stubs). | Behaviour-neutral; full suite green. |

---

## 4. Fences and items

### E1 — security posture, config, logging, env passthrough (11 items)

Owns: `src/mcprouter/settings.py`, `src/mcprouter/api/app.py` (after W0), `api/hardening.py`, `src/mcprouter/logging.py`,
`src/mcprouter/net_policy.py` (new), `inference/urlcheck.py`, `inference/engine.py` (`_aoai_settings` only),
`mcpclient/targets.py`, `skills/gitsource.py`, `scripts/docker-entrypoint.sh`, `docker-compose*.yml`, `.env.example`,
`tests/support/settings.py`, new tests `test_host_guard.py`, `test_logging.py`, `test_config_registry.py`,
`test_compose_contract.py`, `test_entrypoint.py`, `test_settings_secrets.py`, `test_net_policy.py`.
Must NOT touch: `api/deps_auth.py` (E6), any `routes_*.py` (E2/E3), `Dockerfile*` (E7), docs (E7), `conftest.py`/CI (E5).
Meta-test owned: **MT-5 config/compose/entrypoint contract**.

| P | sev | sources | finding | fix | test (Mut:) | eff |
|---|---|---|---|---|---|---|
| P-101 T3 | blocker | A4-001, A7-002, A4-res#3 | Default stack is dev mode (open admin API, stdio registration = RCE) published on every host interface; only a WARNING. | D1 in entrypoint + compose port string. | `test_entrypoint.py` runs `sh scripts/docker-entrypoint.sh` with stub `python`/`uvicorn` on PATH: no token → argv `--host 127.0.0.1` + warning line; token → `0.0.0.0`; `MCPR_ALLOW_OPEN_DEV=1` → `0.0.0.0`. YAML test: api port starts `${MCPR_BIND:-127.0.0.1}`. Mut: drop the host branch. | M |
| P-102 T3 | blocker | A4-002, A2-012, A4-018, A4-res residual | Host/Origin validation only on `/mcp`; `/api/v1`, `/metrics`, `/docs`, SPA rebindable; security headers only on SPA. | D2 `HostGuard` in `hardening.install`; `nosniff`, `no-store` (API), `frame-ancestors 'none'` on all responses; `default-src 'self'` CSP on SPA. | `test_host_guard.py`: `Host: evil.example` → 421 on `/api/v1/servers`, `/metrics`, `/docs`, `/`, `/healthz`, `/mcp`; `MCPR_ALLOWED_HOSTS=router.lan` admits it; headers on a JSON 200. Mut: remove `install` call. | M |
| P-103 | blocker | A7-001, A4-023, A4-022, A7-009, A7-016 | `api` forwards 7 vars, no `env_file`: sync/rollup/remote/AOAI/skills settings never reach the container; no init, caps, pids, grace period. | D3; `init: true`, `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `pids_limit: 512`, `stop_grace_period: 30s`; entrypoint `--timeout-graceful-shutdown 20`; commented `mem_limit`/`cpus` + `OMP_NUM_THREADS` passthrough. Tell E5: smoke must set `POSTGRES_PASSWORD`. | `test_compose_contract.py` (pure YAML, no docker): every registry var reachable (env_file or explicit), every explicit name ∈ registry, `db` has no `ports`, base+inference+gpu merge keys valid, hardening keys present. Mut: delete `env_file`. | S |
| P-104 | should | A7-003 | Logging unconfigured; `mcprouter.*` INFO dropped; `structlog` pinned but unused. | D4 in `logging.configure_logging`; route uvicorn loggers through the same handler. | `test_logging.py`: INFO emitted with level+logger; json lines parse; canary admin token inside an exception message never in output. Mut: remove redaction processor. | M |
| P-105 | should | A4-010, A4-009, A4-024, A5-015 | Numeric settings accept nan/inf/≤0 (`EMBED_BATCH_SIZE=0` crashes); enums validated late; errors don't name the var; entrypoint reports bad settings as "Postgres not reachable". | `SETTINGS_SPEC` registry in `settings.py` (env, type, default, choices, lo/hi, secret, doc) driving `from_env`; `_pos_int/_pos_float/_choice`; entrypoint exits 2 `FATAL: <VAR> …` for invalid settings, 1 for DB; validate `MCPR_DB_WAIT_TRIES`; correct the backend comments. Registry is the input for E7's generator. | `test_config_registry.py` table: each numeric var × {nan, inf, -1, abc} → `ValueError` naming it; table keys == registry numeric∪enum set. Mut: remove finite check. | M |
| P-106 T3 | should | A4-004, A4-005, A4-006, A4-021 | `repr(Settings)` leaks DSN password + agent keys; no `_FILE` for admin token/agent keys/DB URL; 1-char keys accepted; `AoaiSettings` bypasses `_secret()` and leaks the key-file path on `OSError`. | `repr=False` on `database_url`, `agent_keys`, `admin_token`; `_FILE` variants via `_secret()`; min length 32 for env-provided admin token + agent keys (in `from_env` only, kwargs path unchanged); fold `AoaiSettings` into `Settings`, catch `OSError` → curated `ModelUnavailableError`. | `test_settings_secrets.py`: canaries absent from `repr`, caplog, `str(exc)`; `_FILE` wins; missing/oversized/non-UTF-8 file → error names var not content; short token rejected. Mut: remove `repr=False`. | M |
| P-107 | blocker | A5-002, A4-009, A5-003, A5-001 (env part) | `.env.example` says `MCPR_EMBEDDING_BACKEND=local` (boot crash), lacks admin token/agent keys/POSTGRES/BIND/HOST_PORT/ALLOWED_HOSTS/DEVICE. | Complete operator template: required block first with `# openssl rand -hex 32`, then every registry var commented with true choices/defaults (`hash\|bge\|aoai`, `deterministic`). Keep `ci.yml` append compatibility. | MT-5: every registry var present; every commented enum value ∈ real set; every `${VAR}` in compose present. Mut: restore `local`. | S |
| P-108 | should | A4-018, A7-006, A1-022, A7-017 | `/metrics` open; `/healthz` returns bare 500 on DB down; no readiness signal for degraded engines. | D14 (metrics gate in `hardening`, `/healthz` 503 curated, `/readyz`). | Token set: `/metrics` 401 → 200 with admin containing `mcpr_analytics_`; patched `engine.connect` → 503 without DSN text; `/readyz` degraded when laya requested but absent. Mut: drop the gate. | S |
| P-109 | should | A4-016, A6-015, A4-res#15/#16 | Outbound URL names not resolved (name → 169.254.169.254 passes); three loopback sets; server registration lacks metadata/link-local refusal. | `net_policy.py` (`LOOPBACK_HOSTS`, `host_of`, `redact_url`, `resolve_and_check`) used at point of use by `urlcheck`, `targets.validate_http_url` (now refuses link-local/metadata), `gitsource` (resolve before clone). E3 adopts `LOOPBACK_HOSTS` in `routes_decision`. | `test_net_policy.py`: one hostile-URL table → identical verdicts for server create and model config; `getaddrinfo` → 169.254.169.254 refused. Mut: skip resolution. | M |
| P-110 | nit | A4-017 | Failed git clone leaves `.<id>.tmp`; size cap checked only after clone. | Remove tmp in `finally`; document the cap window (E7). | Runner stub writes then raises → tmp dir gone. Mut: drop `finally`. | S |
| P-111 | should | A4 §5, A7 M1/M2 | No guard against config drift (6 undocumented vars, phantom names, stray `os.environ`). | **MT-5**: `test_config_registry.py` (code `MCPR_*` set ⊆ registry ∪ `NON_ROUTER`; `os.environ` call-site allowlist; secret canaries per secret var) + `test_compose_contract.py` + `test_entrypoint.py` (no DB URL, unreachable DB w/o DSN password in output, invalid setting exit 2, pass-through `exec "$@"` after guards). | Mut: add `os.environ.get("MCPR_FOO")` in src; delete an `.env.example` line. | M |

### E2 — REST hardening, auth matrix, route coverage, api.md (12 items)

Owns: all `src/mcprouter/api/routes_*.py` **except** `routes_decision.py` (E3) and `routes_setup.py` (E6); new
`api/errors.py`, `api/deps.py`, `api/acting.py`; `registry/api_deps.py`; `dedup/review.py`; `skills/sources.py`;
`scripts/gen_openapi.py`, `scripts/gen_api_docs.py`, `docs/reference/openapi.json`, `docs/reference/api.md`;
`tests/support/route_hits.py`; tests `test_zz_route_coverage.py`, `test_auth_matrix.py`, `test_rest_gaps_*.py`, and the
existing per-route test files it extends (`test_skills_api.py`, `test_policy_routes.py`, `test_feedback_api.py`, …).
Must NOT touch: `api/app.py` (use the W0 seam; `routes_skills.router`/`agent_router` names stay exported), `deps_auth.py`,
`dedup/detect.py`, gateway, UI, docs other than the two generated files.
Meta-tests owned: **MT-1 route coverage + OpenAPI drift**, **MT-2 generated auth matrix**.

| P | sev | sources | finding | fix | test (Mut:) | eff |
|---|---|---|---|---|---|---|
| P-201 T3 | should | A3-002, A4-res#34 | Dedup accept ignores the user's preferred tool; UI toast lies. | D12: `AcceptIn{preferredToolId?}` `extra="forbid"`; persist; response echoes it. Also add `truncated` to `DedupRunOut` (from E6 P-603). | Accept with B → row `preferred_tool_id == B`; foreign id → 422; empty body → scanner pick. Mut: ignore body. | M |
| P-202 | should | A3-003 | `sourceId`/`hasScripts` silently dropped by `GET /skills`. | D13. | Each filter narrows a seeded set; `?sourceId=` == `?source=`. Mut: drop the WHERE clause. | S |
| P-203 | should | A1-001, A1-002, A1-003, A1-024 | Skill-source PATCH `null` → 500; rename to dup → 500; `https://user:token@…` stored and echoed; `ServerIn`/`SourceIn` accept typos; `kind` is bare `str`. | Non-optional patch fields (only `git_ref` nullable); `IntegrityError` → 409; reject userinfo in `validate_location`; `extra="forbid"`; `kind: Literal["directory","git"]`. | Null per field → 422, row unchanged; dup → 409; `https://u:pw@h/r` → 422 and `pw` in no response; `transprt` → 422. Mut: revert each guard. | M |
| P-204 | should | A1-007 | Default FastAPI 422 echoes `input` (e.g. server `env` secrets) everywhere except `/route*`. | Move `_curated_validation_error` to `api/errors.py`, apply to all `/api/` paths. | `POST /servers` missing `transport`, `env={"K":"sentinel"}` → 422 without `sentinel`; `/route` shape unchanged. Mut: unregister handler. | S |
| P-205 T3 | should | A1-008 | First principal in dev mode with no admin token permanently locks every admin route (403); test is vacuous. | D11. | Fresh DB, no token: `POST /principals` → 409 with the message; with token → 201 then admin still works. Replace `test_dev_mode_open_or_fail_closed`. Mut: remove the check. | S |
| P-206 T3 | should | A6-011, A6-012, A6-013, A6-023 | Two `get_session`, three `_factory`, two `_manager`, two `require_admin` spellings; act-as-agent logic copied 4× with drifting messages; router→router private imports. | `api/deps.py` (`get_session`, `session_factory`, `get_manager`); `api/acting.py` (`act_as_agent`, `admin_actor`); adopt in execute/skills/feedback/route/dedup/tools; `registry/api_deps.py` becomes a re-export façade. | Table over the 4 routes: admin w/o `agentId` 400, unknown 404, disabled 403, agent naming another 403. Mut: drop the disabled check in `acting.py`. | L |
| P-207 | nit | A6-025 | Two routers + 8 mid-file E402 imports in `routes_skills.py`; order-dependent `/skills/bundle` shadowing. | Split into `routes_skills_admin.py` / `routes_skills_agent.py`; `routes_skills.py` re-exports `router`, `agent_router`. | Agent key on `GET /skills/bundle` never reaches the admin handler. Mut: swap include order. | S |
| P-208 | should | A1-013, A1-016, A1-017, A1-018 | Feedback schema 422s untested; approvals `status` free text + silent 200 cap; `limit/offset` bounds untested on 5 routes; casing undocumented. | `status: Literal[...]`; `limit` (≤500, default 200) on `/approvals` (shape unchanged); casing test with the 3-exception allowlist. | Feedback `{items:[]}`/extra key/51 items/2001-char note → 422; bad status → 422; `limit∈{0,MAX+1}`, `offset=-1` → 422 ×5 routes; `test_openapi_property_casing`. | S |
| P-209 | should | A1-010, A1-011, A1-012, A1-021 | Untested: `GET/PATCH /skill-sources/{sid}` (zero requests), 404 on skills ×4 and principals/rules ×7, 410 expired approve/deny, 1 MiB cap off the edge route. | — (tests only). | Parametrised 404/410 tests; happy GET/PATCH; 413 on `/servers/import`, `/route`, `/policy-rules`. | M |
| P-210 | should | A1-006, A1-009, A6-012 | Hand-listed 13-row auth matrix; ~25 admin and 5 EITHER/AGENT routes lack 401 tests. | **MT-2** `test_auth_matrix.py`: walk each route's dependant for `require_admin` → 401 for none/agent/wrong, admin passes; explicit `EITHER`/`AGENT` map → 401 without key; every `/api/v1` route must be in ADMIN∪EITHER∪AGENT∪PUBLIC (unclassified → red). | Mut: remove `dependencies=[Depends(require_admin)]` from `routes_skill_sources` → 6 rows red; delete `get_principal` in `post_feedback` → red. | M |
| P-211 | should | A1 meta A/B | Nothing fails when a route ships untested. | **MT-1**: `tests/support/route_hits.py` recorder (wraps `TestClient.request`, per-xdist-worker file, merged at controller `sessionfinish`); `test_zz_route_coverage.py`: OpenAPI vs committed `docs/reference/openapi.json` (generated by `scripts/gen_openapi.py`) and every `(method, path)` requested with ≥1 2xx unless in `NEGATIVE_ONLY`; enforced when `MCPR_ENFORCE_ROUTE_COVERAGE=1`. Retire `docs/audit/openapi-routes.json`. | Mut: delete the only `GET /skill-sources/{sid}` test → red; add a dummy route → drift red. | M |
| P-212 | should | A5-023, A1-025 | No API reference. | `scripts/gen_api_docs.py` → `docs/reference/api.md` (method, path, auth class from the P-210 classifier, tag, summary); drift test. | Edit a route summary without regenerating → red. | S |

### E3 — MCP gateway + System One edge (12 items)

Owns: `src/mcprouter/gateway/**`, `api/routes_decision.py`, `inference/serve.py`, tests `test_gateway_*`, `test_edge_*`,
`test_body_size_cap.py` (MCP rows), new `test_gateway_surface_e2e.py`.
Must NOT touch: `limits.py` (E6, adopt only), `deps_auth.py` (E6), `app.py`, `settings.py` (fields pre-declared in W0).
Meta-test owned: **MT-3 MCP surface (both eras, real transport)**.
Before coding: confirm every SDK hook named here (`Server.middleware`, `get_tool_input_schema`, `max_sessions`,
`max_request_body_size`, `on_list_resource_templates`) against the INSTALLED `mcp` 2.3.0 in `.venv/`.

| P | sev | sources | finding | fix | test (Mut:) | eff |
|---|---|---|---|---|---|---|
| P-301 | blocker | A2-001 | Handshake era returns `ErrorData(code=0, message=str(e))` for uncaught handler errors (SQLAlchemy text to agents). | Server middleware: catch non-`MCPError` → log class name (scrubbed) → `MCPError(INTERNAL_ERROR, "Internal server error")`. | Both eras: `visible_tools` raises `RuntimeError("password=hunter2")` → -32603, no `hunter2` on the wire. Mut: remove middleware. | S |
| P-302 | should | A2-002, A1-004, A1-005, A5-033 | `X-MCPR-Decision-Hop: ²` or 5000 digits → 500; edge 429, `engine is None` 503 and `InferenceError` 503 untested. | `isascii() and isdecimal() and len ≤ 3`, else hop 1. Adopt E6 limiter registry (`decision` surface) and E1 `LOOPBACK_HOSTS`. | Hop table `"²","٣","9"*5000," 1","-1","1.0"` → 503/200 never 500; `[200,200,429]` with budget 2, other principal unaffected; both 503 paths. Mut: revert parse. | S |
| P-303 | should | A2-003 | Non-`Broken*` notify exceptions escape as `ExceptionGroup`, failing `find_tools` after exposure changed. | `one()` catches `Exception` (log type, prune); `_find_tools` shields `apply_route` notify. | Tracked session raising `RuntimeError` on send: `find_tools` ok, session pruned, others notified. Mut: narrow the except. | S |
| P-304 T3 | should | A2-004 | 10k global session cap, no per-agent cap: one key starves all agents. | `max_sessions=settings.mcp_max_sessions`; `_AuthASGI` refuses `initialize` without session id when agent has ≥ `mcp_max_sessions_per_agent` live → 429. | N+1th session of agent A refused; agent B connects. Mut: remove the per-agent check. | M |
| P-305 | should | A2-005 | Modern-era `tools/call` runs a full `tools/list` first (2× DB load). | D15 `get_tool_input_schema=None`. | Spy: `visible_tools` calls per modern call == 0. Mut: remove kwarg. | S |
| P-306 T3 | should | A2-006 | Sessions bind to agent id, not credential; comment claims otherwise. | D15 `subject=hash_key(token)`; fix comment. | Rotate key → old `Mcp-Session-Id` + new key → 404. Mut: drop `subject`. | S |
| P-307 | nit | A2-007, A2-014 | Reserved meta-tool names only partly shielded; meta-tool args not validated against the advertised schema. | Reserved-name set filtered in `visible_tools` regardless of flags; validate meta-tool args with the manager's jsonschema validator. | Catalog `router.activate_skill` etc. never listed (±skills, ±route_fn); schema-violating args → `is_error` and no side effect. | S |
| P-308 | nit | A2-011 | Unregistered methods unpinned; clients calling `resources/templates/list` see errors. | D15 empty templates list. | Both eras: `completion/complete`, `logging/setLevel`, `resources/subscribe` → -32601, then `ping` ok. | S |
| P-309 | should | A2-012, A2-013 | No test of Host/Origin rejection on `/mcp`; two stacked body caps; under-declared length drops the connection. | Pass `max_request_body_size=MAX_BODY_BYTES`. | Authenticated foreign Host → 421, foreign Origin → 403, allowed host → 200; 2 MiB declared and chunked → 413, no session created; 1 MiB−1 accepted. | S |
| P-310 | nit | A2-008, A2-009, A2-010 | Session tracked late, LRU evicts live sessions, DELETEd sessions linger; exposure + skill ids updated non-atomically; open streams survive revocation. | Skill ids inside the `Exposure` snapshot; prune on DELETE; re-check principal at fan-out and close revoked streams. | Concurrency test `(request_id, skills)` from one route; DELETE then re-route → no send; disabled principal's listen stream closes. | M |
| P-311 | should | A2-015, A6-044, A6-022, A6-010 (adopt) | Fixed ports 8641/8761/8762/8803, copy-pasted uvicorn harness, 0.3 s attach sleeps in E3's tests; private `_build_bundle` import; ad-hoc limiters at `server.py:358`, `routes_decision.py:68`. | Use `tests/support/serve.run_app` + readiness events; `from skills.bundle import build_bundle`; `app.state.limiters`. | Suite green under `-n 4` twice. | M |
| P-312 | should | A2 §4 | Adding a handler/meta-tool/notification fails nothing. | **MT-3** `test_gateway_surface_e2e.py`: expected set derived from `get_request_handler` + rendered meta-tools + notification types; `SURFACE` table per era; recording ASGI wrapper proves each row crossed HTTP; unsupported rows → -32601; extras: authenticated ping, two sessions same agent both notified / other agent not, unexposed-but-authorized call after re-route. | Mut: register a dummy handler → "uncovered" red; delete a row → red. | L |

### E4 — UI (12 items)

Owns: `ui/**` only (incl. `ui/package.json`, `vite.config.ts`, `ui/src/test/*`). Reads `docs/reference/openapi.json` (E2).
Must NOT touch: backend, docs, CI. Meta-test owned: **MT-4 UI meta (`ui/src/meta.test.ts`)**.

| P | sev | sources | finding | fix | test (Mut:) | eff |
|---|---|---|---|---|---|---|
| P-401 | blocker | A3-001, A7-013, A3 §5 | Playground test red/flaky (2/8 master runs); page button and dialog confirm share the name "Run tool". | Distinct confirm labels (`Run ${tool.name}`, `Activate ${skill}`); query distinct names; `configure({asyncUtilTimeout: 5000})` in `setup.ts`; fix the §5 fragile sites. No timeout bumps, no retries. | `ConfirmDialog` unit test; `npx vitest run PlaygroundPage` 20× locally, 0 failures (report). | S |
| P-402 | should | A3-002 | Success toast and radio claim a preference the backend ignored. | Toast reads the response `preferredToolId` (after P-201). | Toast text matches response, not the radio. Mut: read radio state. | S |
| P-403 | should | A3-003, A3 §6.4 | `mockFetch` strips query strings and 404s unmocked routes silently. | `mockFetch` records `query` and body, `expectQuery()`, unmocked route fails the test; Skills filter tests assert exact query. | Mut: rename `sourceId` in client → red. | M |
| P-404 | should | A3-004, A3-005, A3-018 | Skill funnel rows open the tool drawer (404); skill/tool rows not keyboard-reachable; per-row "Approve/Deny" unnamed; empty header cell. | Route `skill:` ids to SkillDrawer (reuse `kindOfId`); focusable rows with Enter+Space; `aria-label="Approve ${tool} for ${agent}"`; name header. | Click skill row → no `GET /tools/skill:`; `tab`+`{Enter}`/`{ }` opens drawer; role query by row-specific name. | S |
| P-405 | should | A3-006, A3-007, A3-008 | Failures render as perpetual "Loading…" or empty tables with "No … yet"; stale drawer content; empty Run-as picker unexplained. | Shared `ErrorState` (Retry → `reload`) for `failed && !data` on ~10 pages; drawers keyed on id, `tab` reset on close; Run-as hints. | Per list page: 500 → error region with Retry, no empty copy; drawer 500 → error copy; `/principals` 500 and `[]` → hint. | M |
| P-406 | should | A3-009, A3-010, A3-011, A3-022, D11 | Policy ignores `resourceKind`, lacks `maxServers/maxSkills`, max 50 vs 64; AddSourceDialog never resets; Setup leaks `HTTP 409 from /api/…`, sticky messages, vacuous "models: ok", wrong copy, uncaught clipboard. | Kind column + source names + kind select + limits; dialog reset + `<form>`; Setup via `describeError`, clears per step, real models check, "Connect" copy, clipboard catch, shows the P-205 409 message; warn when connecting mid-wizard. | PolicyPage test seeds a skill rule; add-source twice → blank; Setup 409 → curated text. | M |
| P-407 | should | A3-012, A3-024 | Scan result discarded ("finished" even with 0 new); only 50 suggestions shown with wrong count; approval polling lists all statuses. | Toast "N new"/"No new duplicates" (+ truncated note); pager via `total`; poll with `status=pending`. | created=0 → "No new duplicates"; 51 → pager; poll query asserted. | S |
| P-408 | should | A3 §3 | No tests for ServersPage, ToolsPage, PolicyPage, ApprovalsPage (9 of 20 mutating calls), RegisterServerDialog, hooks. | Page tests covering load/empty/error/actions/confirm; hook tests (`useLoader` supersede/quiet refresh, `useDebounced`, `useVisiblePolling`). | MT-4 page-test rule passes with no allowlist entries for these four. | L |
| P-409 | should | A3-025 | Route-only operations have no UI (lost key cannot be rotated; tools cannot be disabled individually). | Delete server/principal/rule/skill-source (confirm names the object and consequence); PATCH principal (enabled, limits) and rule; rotate key (KeyRevealDialog); tool enable/disable in ToolDetailDrawer; Versions tab uses `/skills/{id}/versions`. | One test per action asserting method+path+body via strict mockFetch. | L |
| P-410 | nit | A3-013, A3-014, A3-016, A3-017, A3-020, A3-023 | Lens reads `window.location` once; dead `simulateRoute`; Run-as logic duplicated 3×; uncleared toast timers; second NotificationStack in tests; tools loader runs on Skills tab. | `useSearchParams` + deep-link test; delete dead exports; `useRunAs()` + `<RunAsPicker>`; clear timers on unmount; single stack; per-tab loaders. | Lens deep link prefill + feedback post to `/route/r1/feedback`; `useRunAs` hook test. | M |
| P-411 | nit | A3-021, D6 | `fluent` chunk 716 KB vs 700 limit, stale comment; all pages in `index`. | Route-level `lazy()` for Analytics/Playground/Setup; correct comment; `scripts/check-bundle.mjs` budget; `VITE_APP_VERSION` (fallback package.json) shown in Layout. | Budget script fails when index > budget. | S |
| P-412 | should | A3 §6, A3-015 | Nothing fails when a page, export or contract drifts. | **MT-4**: NAV↔routes (no undefined element) ↔ `<Page>.test.tsx`; every client export exercised (global recorded call set) or allow-listed with reason; every client method+path, query key and body key exists in `docs/reference/openapi.json`; `// CONTRACT:` without `verified: <test>` fails; button/input accessible-name smoke. | Mut: add an export without a test; send an undeclared query key → red. | M |

### E5 — test-suite health, CI, supply chain, release, version (11 items)

Owns: `tests/conftest.py`, `tests/support/{db,ports,serve,wait,fakes}.py`, `pyproject.toml`, `uv.lock`, `mypy-baseline.txt`,
`.github/**` (ci.yml, release.yml, new nightly.yml, security-scan.yml, dependabot.yml), `scripts/smoke.sh`,
`src/mcprouter/__init__.py`, `testbed/**`, and port/sleep migration in non-E2/E3 test files (`test_e2e_*`, `test_testbed*`,
`test_discovery_*`, `test_execution_validation.py`, `test_inference_engine.py`, `test_analytics_scheduler.py`, …).
Must NOT touch: `test_gateway_*`/`test_edge_*` (E3), src beyond `__init__.py`. Other executors request deps through their handback; E5 adds them.
Meta-tests owned: **MT-7 suite health + version lockstep + workflow policy**.

| P | sev | sources | finding | fix | test (Mut:) | eff |
|---|---|---|---|---|---|---|
| P-501 | should | A6-041, A6-042 | `_CLEAN_TABLES` misses `route_feedback`, `app_settings`; whole-table deletes assume exclusive DB; nothing stops running against a real DB. | `TRUNCATE … RESTART IDENTITY CASCADE` over `ALL_METADATA` sorted tables (E6 P-608); refuse unless DB name ends `_test` or `MCPR_ALLOW_ANY_DB=1`. | Every table empty after teardown; guard raises on `mcprouter`. Mut: drop a table from the list. | S |
| P-502 | should | A6-040, A6 §5.1 | Function-scoped engine + full `init_db` per test (est. 2.5–7 min aggregate). | Template DB built once (`mcprouter_test_template`: extension + `init_db`), per-xdist-worker `CREATE DATABASE … TEMPLATE`, session engine; add `pytest-xdist`. | Serial and `-n auto` pass counts equal on two runs (report both wall times). | L |
| P-503 | should | A6-045, A6-046, A6-050, A5-020 | One marker; no `--strict-markers`; slow tests never run in CI; DB-down silently skips the DB suite; no per-test timeout. | Markers `db` (auto from fixture), `e2e`, `slow`, `scale`; `--strict-markers`; `pytest-timeout` 120 s (e2e 300 s); `-ra --durations=25`; `MCPR_REQUIRE_DB=1` turns `requires_db` skips into failures (set in CI). | Unregistered marker → collection error; skip count ≤ budget. | S |
| P-504 | should | A6-044, A2-015, A4-res#29 | Hand-allocated fixed ports 8600–8821, 5 copy-pasted harnesses, fixed sleeps; testbed CLI defaults collide with the suite. | `free_port`/`run_app`/`wait_for` in E5-owned tests; `testbed` fleet `port_base=None` allocates free ports; dead-port tests bind-then-close. | `rg "port(_base)?=\s*8[0-9]{3}" tests` → only allow-listed literals (MT-7 rule). | M |
| P-505 | should | A6-070 | `tests`/`testbed` not type-checked (107 errors / 41 files). | Add to mypy `files` with relaxed overrides for `tests.*`/`testbed.*`; delete 13 unused ignores; commit `mypy-baseline.txt`; CI fails if the count rises. | Mut: add an `attr-defined` error → CI red. | S |
| P-506 | should | A7-012, A7-013, A7-014, A6-045, A7-010, A7-011 | No path filters; one serial pytest; no nightly; inference image untested; no build cache; CI Node 20 vs image Node 22; no failure artifacts. | `ci.yml`: `changes` job (paths-filter, SHA-pinned) → lint-type, unit (`-m "not e2e and not slow and not scale" -n auto`), e2e (`--dist loadfile`), ui (Node from `.nvmrc`), compose-smoke (buildx `type=gha` cache); `ci-gate` passes filter-skipped jobs only when irrelevant. `nightly.yml`: slow+scale with `[inference]` + `models-cache` cache, build `Dockerfile.inference` + smoke on hash fallback. vitest/pytest output artifacts on failure. No retries. | Docs-only PR: backend/ui/smoke skipped, gate green; trigger nightly via `workflow_dispatch`, verify by name. | M |
| P-507 | should | A4-011, A4-012, A7-005 | No Dependabot, no security scanning, gitleaks config unused, `uv.lock` untracked. | D8. | `gh run list --workflow "Security scan" --limit 1` green (by name); YAML parses; MT-7 asserts ecosystems + jobs. | M |
| P-508 T3 | blocker | A4-003, A7-004, A7-014 | Any `v*` tag publishes `:latest` untested; inference image `continue-on-error`, no provenance/SBOM; never run. | D7; inference job fails visibly with provenance + SBOM. | MT-7 parses `release.yml` (`needs: verify` on both images, trigger pattern); dry-run `v9.9.9-rc.0` on a fork: verify fails, no image pushed. Mut: delete `needs`. | M |
| P-509 | should | A5-026, A7-004, A2 §2.1 | Version hand-edited in 5 places. | D6: `__version__` from `importlib.metadata`. | **MT-7** `test_version_lockstep.py`: pyproject == `__version__` == `create_app().version` == MCP serverInfo == `ui/package.json` == top CHANGELOG heading. Mut: bump one. | S |
| P-510 | should | A7-020, A7-015, A7 M4 | Smoke misses auth rejection, `/metrics`, restart persistence, graceful stop, stale-base inference. | `smoke.sh`: 401 on `/mcp` + admin without key, `/metrics` 401/200, principal survives `compose restart`, `docker stop -t 30` exits 0 within 25 s, container `__version__` == checkout, `npx --version` works (D16), `mktemp` paths, sets `POSTGRES_PASSWORD`. | Smoke red if any check fails (run once with a guard removed). | M |
| P-511 | should | A6 §5, house CI rules | No guard on suite hygiene or workflow policy. | **MT-7** `test_suite_health.py` (tables empty after teardown, `tests/support` collects 0 tests, no `from tests.test_`, markers registered, fixed-port allowlist) + `test_workflow_policy.py` (every action SHA-pinned with `# vX.Y.Z`, every workflow declares `permissions`, write scopes per job only, dependabot ecosystems, security jobs, nightly present). | Mut: unpin one action; add workflow-level `packages: write`. | S |

### E6 — data, performance, architecture (10 items)

Owns: `src/mcprouter/db.py`, `models.py`, new `migrations/**` + `alembic.ini` + `mcprouter/migrate.py`, `singleton.py`,
`limits.py`, `mcprouter/auth/**` (new), `api/deps_auth.py` (façade), `api/routes_setup.py` (AppSetting move only),
`dedup/detect.py`, `policy/**`, `execution/**`, `analytics/**`, `registry/catalog.py`, `registry/schema.py`,
`routing/retriever.py` (index DDL only), `skills/bundle.py`, `skills/ingest.py`, `discovery/logsafe.py`, `inference/laya.py`
(`_scrub` only), `registry/audit.py`, tests `test_migrations.py`, `test_layering.py`, `test_limits.py`, `test_query_counts.py`.
Must NOT touch: routers (E2/E3), gateway (E3), settings (fields in W0). Keep every renamed private name as an alias.
Meta-tests owned: **MT-8 migrations + layering**.

| P | sev | sources | finding | fix | test (Mut:) | eff |
|---|---|---|---|---|---|---|
| P-601 T3 | should | A4-007, A5-006, A6-006, A7-007, A7-008, A4-res#4/#5 | ~25 DDL statements (ACCESS EXCLUSIVE ALTERs, audit-table UPDATE) on every boot, no lock, no version; `alembic` unused; no downgrade story. | D5. Legacy bridge moved verbatim to `migrations/legacy_bridge.py`; `lock_timeout` on DDL session; `CREATE INDEX` in later revisions uses `CONCURRENTLY` (autocommit block). | `test_migrations.py`: fresh `upgrade head` → `compare_metadata` diff empty; 0.5-shaped DB → bridged + stamped + equal; second boot issues zero DDL (cursor listener); two concurrent `init_db` both succeed; DB at unknown rev → curated FATAL; `downgrade -1` from 0002 and back. Mut: skip `stamp` → zero-DDL test red. | L |
| P-602 | should | A4-008, A7 single-worker | Single worker is convention only; N processes duplicate SyncLoop/RollupLoop. | `singleton.claim_loop_owner`: session `pg_try_advisory_lock`; non-owner skips loops and logs ERROR. | Two apps, one DB → exactly one owner. Mut: always return True. | S |
| P-603 | should | A6-001, A6-008 | Dedup self-join O(n²), no LIMIT, no ANN index, fully materialised. | D9 (revision `0002`); `truncated` in `DedupRun` (E2 surfaces it). | 400 identical embeddings → ≤ cap rows and `truncated`; EXPLAIN shows HNSW on a vector query. Mut: remove LIMIT. | M |
| P-604 | should | A6-002 | Dedup upsert is 2 round-trips per pair. | One `INSERT … ON CONFLICT … DO UPDATE … WHERE status='open' RETURNING (xmax = 0)` over a VALUES list + one prior SELECT. | 200 pairs → ≤ 3 statements (listener). Mut: restore loop. | M |
| P-605 | should | A6-003, A6-004, A6-005, A6-007 | Skill policy = 2 queries + 1 session per skill in `bundle()`; hot `routed_skill_ids` lacks composite index; staleness re-parses JSON over the window; skills never ANALYZEd. | `EngineSkillPolicy.check_many`; `(agent_id, created_at DESC, id DESC)` index in 0002; staleness bounded window + `statement_timeout`; `ANALYZE skills` after ingest. | Statement count constant for 3 vs 10 skills; EXPLAIN uses the new index on 5k rows; staleness timeout → curated 503. | M |
| P-606 | should | A6-010, A4-015 | Six ad-hoc limiters + a second implementation with a module-global (`feedback.LIMITER`) shared across apps; 4× budget across surfaces. | D10 `LimiterRegistry` via W0 seam; delete `analytics/feedback.RateLimiter`; manager uses registry; prune idle keys; decision-edge budget from settings. E3 adopts in gateway/edge. | Two apps don't share budget; 31st feedback → 429; pruning; documented shared-budget semantics. Mut: make registry module-global. | M |
| P-607 T3 | should | A6-024, A6-020, A4-014, A6-012 | `execution`/`policy`/`gateway`/`registry`/`db` import up into `mcprouter.api`; private `_dev_mode_active` imported cross-module; full-table hash scan per auth. | D10: `mcprouter/auth/{config,principals,keys}.py`; `deps_auth.py` = FastAPI deps + re-export façade (incl. `_dev_mode_active`, `_reset_dev_warning_for_tests`); admin token from `settings.admin_token`; indexed lookup on `key_hash` (index in 0002) + constant-time compare on the hit; rewrite imports in execution/policy/db. | `test_layering.py` AST: nothing outside `api/` imports `mcprouter.api` (allowlist: none after E3 switches `gateway/server.py:75`); façade snapshot: every pre-move name resolves, count unchanged; one query per auth. Mut: add an upward import. | L |
| P-608 | should | A6-024, A6-081 | `AppSetting` ORM table defined in a router; four MetaData objects hand-listed (why cleanup drifts). | Move `AppSetting` to `models.py` (re-export from `routes_setup`); `models.ALL_METADATA` used by migrations, `init_db`, E5 truncate helper. Single-Base merge deferred. | Covered by MT-8 + P-501. | S |
| P-609 | nit | A6-021, A6-022, A6-030, A6-031, A5-014, A7 noticed | Private cross-module imports (`_meta`, `_build_bundle`); dead `session_scope`, `utc_today`; stale docstrings (feedback "NOT wired", metrics "nothing schedules rollups"). | Public `meta`, `build_bundle` (aliases kept); delete dead code; fix docstrings. | `vulture --min-confidence 80` clean on touched modules. | S |
| P-610 | nit | A6-016, A6-032, A4-019 | Five log sanitisers with different guarantees (`logsafe.scrub`, `laya._scrub` pass ANSI); curated typed-exception messages rely on convention. | Collapse to `scrub_log` + `audit.scrub`; rename `logsafe.redact` → `mask_secrets`; AST test: raises of curated exception classes interpolate only allow-listed names. | `"\x1b[2J\r\n"` through each remaining helper → no control chars; f-string with `{url}` in a curated raise → red. | M |

### E7 — docs IA, generated config docs, image/runtime (12 items)

Owns: `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md` (new), `CLAUDE.md`, `docs/**` except `docs/reference/api.md`,
`docs/reference/openapi.json` (E2) and `docs/audit/**`; `Dockerfile`, `Dockerfile.inference`, `.nvmrc` (new), `.gitignore`,
`deploy/`, `scripts/gen_config_docs.py`, `tests/test_docs_consistency.py`.
Must NOT touch: `.env.example`, `settings.py` (E1), source code. Meta-test owned: **MT-6 docs consistency**.

| P | sev | sources | finding | fix | test (Mut:) | eff |
|---|---|---|---|---|---|---|
| P-701 | should | A5-001, A5-007, A5-008, A5-009, A5-010, A5-011, A5-025, A5-027, A5-028, A5-029, A5-030, A4-025 | README claims laya/local defaults, 77 cases, macOS `open`, wizard that never appears, no prerequisites, `testbed.serve` dead end, `host:8400/mcp` → 421, stale ledger. | 5-step quickstart (`.env` canonical, token generation, `up -d --build --wait`, browse), both wizard paths, prerequisites, `testbed.seed` snippet with `.venv/bin/python`, smoke usage line, ledger with rev stamp + T1 caveat, doc index, drop "(v0.4)". | MT-6 rules 3, 6, 9. | M |
| P-702 | should | A5-004, A1-014, A1-015, A1-020, A1-023, A1-025, A5-005 (import step) | INSTALL bare-metal DB step unreachable; 403-means-agent-key wrong; no API conventions; import step silent about stdio in Docker. | Dev override in §1; fix the troubleshooting row (agent key → 401; 403 = no admin token); "API conventions" (`/docs`, casing exceptions, execute-200 + `status`, 400 vs 422, sync `error` field, 1 MiB cap, auth per class → `reference/api.md`); stdio warning at import. | MT-6 rule 8. | M |
| P-703 | should | A5-020 | No human contributor guide; silent DB skips. | `CONTRIBUTING.md`: setup, gates, `MCPR_REQUIRE_DB=1`, fast loop `-m "not e2e and not slow and not scale"`, regenerating docs, commit style; CLAUDE.md links it. | MT-6 rule 2 (paths exist). | S |
| P-704 | should | A5-021, A4 §2 | ~19 settings undocumented; no CLI reference. | `scripts/gen_config_docs.py` → `docs/reference/configuration.md` from `SETTINGS_SPEC` + compose/entrypoint-only table; `docs/reference/cli.md` (testbed.*, bench.*, scripts/*, `mcprouter.migrate`). | Drift test: regenerate == committed. Mut: add a Settings var without regenerating. | M |
| P-705 | should | A5-006, A7-008, A7-015, A7-019, A4-013 | No upgrade, downgrade, backup or stale-base guidance. | `docs/upgrade.md`: back up first (`pg_dump` commands), 0.5→0.6 bridge + stamp, auto `upgrade head` + manual CLI, one-step downgrade ≥0.6, old image on newer DB refuses, rebuild base before inference override, which volumes matter, embeddings re-derivable, `HF_HUB_OFFLINE=1`. | MT-6 rules 2, 9. | M |
| P-706 | should | A5-022, A6-008 | No architecture overview. | `docs/architecture.md` (1 page): monolith, single worker + loop-owner lock, request lifecycle, data model, module boundaries incl. `auth/` layering rule, extension seams, scale ceilings. | MT-6 rule 2. | S |
| P-707 | should | A5-005, A5-024, A7-016, A7-017, A7-021, A1-022, A4-022, A4-013, P-110 | deploy.md lacks stdio-in-Docker, troubleshooting, logging, metrics auth, health semantics, limits, reverse proxy/TLS, backups, open surfaces, plaintext-at-rest warning. | Add those sections; move the BuildKit `--network=host` note to Troubleshooting; RSS table (measured by E7 on the built image). | MT-6 rules 1, 3. | M |
| P-708 | should | A5-005, A7-010, A4-011, A7-011, A7-018, D16 | Image lacks node; any `src/` edit reinstalls all deps; floating base tags; Node mismatch; inference torch unpinned across two pip calls. | Node 22 runtime from the node stage; deps layer keyed on `pyproject.toml` then `pip install --no-deps .`; digests with `# tag` comments; `ARG NODE_VERSION` matching `.nvmrc`; `ARG APP_VERSION` → `VITE_APP_VERSION`; `Dockerfile.inference` `torch==` via ARG + `--constraint` on both pip calls. Report size delta. | E5 smoke `npx --version`; rebuild after a `src/` touch shows the deps layer `CACHED`. | M |
| P-709 | should | A5-012, A5-013, A5-019 | scoping.md item 6 wrong; security-model §6 stale + `DenyAllSkillPolicy` claim; SPEC names `/api/v1/metrics`. | Banners + "Amendments"/"Deviations" lists; refresh §6 with D1/D2/D14 posture and open surfaces. | MT-6 rule 8 catches `/api/v1/metrics`. | S |
| P-710 | should | A5-016, A7-021 | CHANGELOG undated, no Unreleased, no config/schema notes. | Keep-a-Changelog; dates from git; Unreleased with Breaking (bind default, 409 first principal, 421 Host, `/metrics` auth, `POSTGRES_PASSWORD` required), Config, Schema (Alembic 0001/0002), API. | MT-7 heading check. | S |
| P-711 | should | A5 §5 | Docs drift is invisible. | **MT-6** `test_docs_consistency.py` rules 1–9 (links + anchors, backticked paths/symbols, documented `MCPR_*` exist and reverse via generated config docs, `.env.example` enum values, documented defaults == code, endpoint mentions ∈ OpenAPI, `python -m … --help` flags) with a temp-dir mutation fixture per rule. | Each rule's fixture breaks it → red. | M |
| P-712 | nit | A5-031, A5-032 | `skills-cache/` not ignored; empty `deploy/`. | Ignore it; remove `deploy/`. | — | S |

---

## 5. Integration order and conflict hotspots

Merge order: **W0 → E6 → E1 → E2 → E3 → E5 → E4 → E7.** Each executor rebases on the integration branch, reruns all
gates, then merges. Why: E6 provides `init_db`/`ALL_METADATA`/auth façade/limiter registry (E5, E2, E3 consume);
E1 provides `SETTINGS_SPEC` + HostGuard (E7 generator, E3 `/mcp` 421 expectation); E2 commits `openapi.json` (E4 MT-4);
E5 restructures CI once the code exists; E7 documents final behaviour and its meta-test runs against it.
Cross-fence dependencies, built against W0 stubs and finished on rebase: E3←E6 (limits), E3←E1 (`LOOPBACK_HOSTS`),
E2←E6 (`truncated`), E4←E2 (P-201, P-205, openapi.json), E5←E6 (`ALL_METADATA`), E7←E1 (`SETTINGS_SPEC`).

| hotspot | owner | others | rule |
|---|---|---|---|
| `src/mcprouter/settings.py` | E1 | none | W0 pre-declares every field other fences need; nobody else edits. New field later → ask the integrator. |
| `src/mcprouter/api/app.py` | E1 (after W0) | none | Others hook only through their W0 seam module. Router include order unchanged (E2 keeps exported names). |
| `tests/conftest.py` | E5 | none | Others ship fixtures in their own `tests/support/<x>.py`, already listed in `pytest_plugins` by W0. |
| `.github/workflows/*.yml`, `pyproject.toml`, `uv.lock` | E5 | none | Dependency or CI needs go in the executor's handback; E5 (or the integrator post-merge) adds them. Meta-tests run inside existing pytest/vitest steps, so no new CI steps are needed elsewhere. |
| `README.md`, `docs/**` | E7 | E2 (`docs/reference/api.md`, `openapi.json` only) | Behaviour changes from other fences are pre-listed in D1–D18; E7 writes them. |
| `api/deps_auth.py` | E6 | none | Façade keeps every old name; E2 imports public names only. |
| `gateway/server.py` | E3 | none | E6 renames keep aliases; E3 switches imports. |
| `docker-compose*.yml` vs `Dockerfile*` | E1 vs E7 | — | Healthcheck stays in Dockerfile (E7); ports/env/hardening in compose (E1). |

Commit discipline (house rules): one logical change per commit; refactor commits (W0-1, W0-2, P-207, P-607 move,
P-608) never mixed with behaviour commits; T3 items get the `reviewer` subagent before handback.

---

## 6. Definition of done for the wave

**Meta-tests (all green in CI, each with its mutation check recorded in the executor's handback):**

| MT | owner | file(s) | fails when |
|---|---|---|---|
| MT-1 | E2 | `tests/support/route_hits.py`, `tests/test_zz_route_coverage.py` | a route is added/removed without regenerating `openapi.json`, or any `(method, path)` is never requested / never 2xx (outside `NEGATIVE_ONLY`) |
| MT-2 | E2 | `tests/test_auth_matrix.py` | an admin route accepts none/agent/wrong token, an agent route accepts no key, or a route is unclassified |
| MT-3 | E3 | `tests/test_gateway_surface_e2e.py` | an MCP handler, meta-tool or notification lacks a real-transport row in either era, or an unsupported method stops returning -32601 |
| MT-4 | E4 | `ui/src/meta.test.ts` + strict `mockFetch` | NAV/route/page-test parity breaks, an export is untested, a client call/query/body key is not in OpenAPI, an unverified `CONTRACT` comment exists |
| MT-5 | E1 | `test_config_registry.py`, `test_compose_contract.py`, `test_entrypoint.py` | a `MCPR_*` is read but unregistered, missing from `.env.example`/compose reach, unvalidated, leaks in repr/logs, or an entrypoint guard regresses |
| MT-6 | E7 | `test_docs_consistency.py` (+ generated `configuration.md` drift) | a doc link/path/symbol/env var/endpoint/CLI flag/default is wrong or generated docs are stale |
| MT-7 | E5 | `test_suite_health.py`, `test_version_lockstep.py`, `test_workflow_policy.py` | tables leak, `support/` collects tests, cross-test imports or fixed ports return, versions diverge, an action is unpinned, permissions widen, release loses `needs: verify` |
| MT-8 | E6 | `test_migrations.py`, `test_layering.py` | metadata and migrations diverge, a steady-state boot issues DDL, a lower layer imports `mcprouter.api`, the façade loses a name |

**Gates (all must pass on the merged integration branch):**
- `ruff check` + `ruff format --check`; `mypy` strict on `src`+`bench`; `mypy tests testbed` ≤ committed baseline.
- `pytest` full **serial once** and `-n auto` twice, with `MCPR_REQUIRE_DB=1 MCPR_ENFORCE_ROUTE_COVERAGE=1`; identical pass counts.
- `npm run lint && npx tsc --noEmit && npx vitest run && npm run build` + bundle budget.
- CI green **by workflow name**: `gh run list --workflow CI`, `--workflow "Security scan"`, `--workflow Nightly` (via `workflow_dispatch`); never `--limit 1` alone.
- Compose smoke (extended P-510) green; release dry-run on a fork with `v9.9.9-rc.0` fails at `verify` and pushes nothing.
- Three consecutive green `CI` runs on the integration branch (flake check; no retries configured).
- `reviewer` subagent run on every T3 item: P-101, P-102, P-106, P-201, P-205, P-206, P-304, P-306, P-508, P-601, P-607.

**Numbers to report at wave end:**
backend tests (collected / passed / skipped with reasons) and UI tests; serial vs `-n auto` wall time; route coverage
(N/N routes requested, N/N with 2xx) and auth-matrix rows; MCP surface rows per era; `MCPR_*` documented N/N and in
`.env.example` N/N; mypy tests baseline (start 107 → end); image size before/after node (241 MB base); `index`/`fluent`
bundle sizes; boot time on a DB with 1M `execution_records` (second boot, zero DDL); dedup run time at 1k tools; CI wall
time per push before/after path filters; settings items left for the owner (tag ruleset, branch protection naming
`ci-gate`, secret scanning + push protection, private vulnerability reporting).
