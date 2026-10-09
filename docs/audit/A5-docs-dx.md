# A5 audit: docs and operator/developer experience

Auditor A5, read-only. Scope: README, CHANGELOG, CLAUDE.md, docs/*.md (not INTEGRATION_NOTES line by line),
.env.example, scripts/*, testbed/** and bench/** CLIs.

Checked tree: HEAD was `1886ede`, not the briefed `64bcbfc`. Three newer commits sit on top
(`767d31b` release 0.5.0, `dbdbcd0` wave-5 should-fixes, `1886ede` UI test fix). Everything below was read at `1886ede`.
`docs/audit/openapi-routes.json` and `uv.lock` are untracked.

Method: relative-link resolver over every `*.md`, a backticked-repo-path resolver, an `MCPR_*` diff of docs against
`src/`, `--help` on each CLI, and grep of each named endpoint, field and function against `src/`.
No pytest and no compose up was run, so the test-count claims are not re-measured.

Headline: the links and file paths are clean and the version string is consistent.
The real damage is in **defaults and first-run guidance**:

- Docs say `laya` and `local` are defaults, but the code defaults are `deterministic` and `hash`.
- `.env.example` is a no-op file and has one invalid value.
- The bare-metal DB step is wrong in INSTALL.
- There is no upgrade guide, no CLI reference, no CONTRIBUTING, and no architecture overview.

## 1. Findings

Severity: H = blocks or misleads a new operator, M = wrong or stale but survivable, L = polish.

| id | sev | file:line | finding | proposed fix |
|---|---|---|---|---|
| A5-001 | H | README.md:75; docs/backends.md:9 (matrix); src/mcprouter/settings.py:23 | Docs say `MCPR_DECISION_BACKEND` default is `laya`. The code default is `deterministic`, and docker-compose.yml sets `deterministic`. A source install without the `[inference]` extra never loads Laya. The README headline ("Default decision engine: Laya") is only true after opt-in. | State "default: `deterministic`; `laya` is opt-in (needs `[inference]`)" in README, backends.md and the settings comment. Fix `README.md:17-19` wording to "recommended engine". |
| A5-002 | H | docs/backends.md:15; .env.example:8 | The embedding backend is documented as "local (default)" and `.env.example` lists `local \| aoai`. The valid values are `hash \| bge \| aoai` (`inference/engine.py:64`), default `hash`. `MCPR_EMBEDDING_BACKEND=local`, copied straight from `.env.example`, raises `ValueError("embedding backend must be one of ...")` at engine load. | Replace with `hash \| bge \| aoai` in both files and note the default. |
| A5-003 | H | .env.example (whole file); README.md:133; docs/deploy.md:5-6 | The README says `cp .env.example .env # add MCPR_ADMIN_TOKEN=... and MCPR_AGENT_KEYS=...`. The file contains only commented backend and skill keys and no admin-token or agent-keys line. Nothing says how to generate a token, and a user who skips the "add" gets open dev mode. `POSTGRES_PASSWORD`, `MCPR_HOST_PORT`, `MCPR_ALLOWED_HOSTS` and `MCPR_DEVICE` (all consumed by compose) are also absent. | Make `.env.example` the complete operator template: commented `MCPR_ADMIN_TOKEN=` and `MCPR_AGENT_KEYS=` with a `# openssl rand -hex 24` hint, plus POSTGRES_*, HOST_PORT, ALLOWED_HOSTS, DEVICE. Keep the test-injected values working (`ci.yml:97` appends to it). |
| A5-004 | H | docs/INSTALL.md:30 | The bare-metal block says `docker compose up -d db`. The base compose publishes **no** host port, so the app and tests (default DSN `localhost:5434`, `settings.py:16`) cannot reach it. README.md:142 and CLAUDE.md use the `-f docker-compose.dev.yml` override. INSTALL says "same commands as the README Quickstart", which is false. | Use the dev override in INSTALL.md:30, or add `MCPR_DATABASE_URL` guidance. |
| A5-005 | H | Dockerfile:13-27 (image); README.md:121-124; docs/INSTALL.md:107-113 | The runtime image is `python:3.12-slim` plus git. It has no node, npx or uvx. INSTALL tells users to import a Claude-Desktop `mcpServers` config, which is nearly always `npx ...` or `uvx ...` stdio. Those servers cannot start inside the container, and no doc says so. The flow goes import, then "unhealthy", with no explanation. | Add a "stdio servers in Docker" section to deploy.md: run only HTTP servers, bind-mount binaries, or extend the image (`FROM mcp-router` plus node). Warn at the INSTALL import step. |
| A5-006 | H | docs/deploy.md:46-47 | The "Upgrades" section says "Schema init is additive (create_all) ... pull/rebuild". The real mechanism is `db.py:25-56`: `create_all` plus ~25 idempotent `ALTER ... IF NOT EXISTS` statements, with an explicit comment that this is "NOT a migration system" and Alembic is deferred. `alembic==1.17.1` is still a runtime dependency in pyproject.toml but is unused (no alembic dir). There are no downgrade, rollback or backup-before-upgrade steps. Type changes (VARCHAR widening) ride the same list. | Write `docs/UPGRADING.md`: 0.x to 0.5 path, what init_db does, "back up the pgdata volume first", no-downgrade statement, per-release schema deltas. Decide whether to drop the alembic dependency (code-side finding). |
| A5-007 | M | README.md:225 | "synthetic dataset (77 cases)". `load_named("synthetic_v1")` returns **111** cases. | Change to 111, or drop the number. |
| A5-008 | M | README.md:135 | `curl ... && open http://localhost:8400/` uses `open`, which is macOS only. Linux gives `command not found`, and the `&&` makes it look like the whole step failed. | `# then browse to http://localhost:8400/`. |
| A5-009 | M | README.md:131-135 vs 177-180 | "Run it" seeds `MCPR_AGENT_KEYS`, so `needsSetup=false` (`routes_setup.py:54`: `principals == 0 and completed is None`). "First run" then says "on a fresh install you are taken to /setup". Following the README top to bottom, the wizard never appears. | State both paths: with `MCPR_AGENT_KEYS` the wizard is skipped (agent already exists). Without it, the wizard creates one. Pick one recommended path. |
| A5-010 | M | README.md:140-149 | The Quickstart gives no prerequisites: uv, Node (and which version; the image uses node 22), and Docker Compose v2 (`--wait`). It also never says to run from the repo root, although `MCPR_UI_DIST` defaults to the relative `./ui/dist`. It starts with `.venv/bin/uvicorn` in the foreground and then asks for `cd ui && npm ci` in the same code block. | Add a prerequisites line and a "from repo root, second terminal" note. Say the dashboard needs `npm run build` before the server starts (or restart). |
| A5-011 | M | README.md:151-154 | `python -m testbed.serve --servers 10` is advertised "to play with", but it only prints `READY {name: url}` (testbed/serve.py header). Nothing explains how to register those URLs (`testbed.seed` does that, and is not mentioned anywhere in the docs). The command is also the system `python`, while CLAUDE.md says never to use system python. | Document `testbed.seed --servers 10 --transport http` (and `serve`) in a "Try it with a synthetic fleet" snippet, with `.venv/bin/python`. |
| A5-012 | M | docs/scoping.md:1 and 19-31 | Titled "binding for v0.1". Still cited by CLAUDE.md as binding, and several decisions are superseded. Item 4 says sync SQLAlchemy (the gateway is async; not verified in depth here). Item 6 says "Auth disabled only when no keys are configured", but the code and security-model.md say dev mode needs no keys, no admin token and zero principals. Item 1 predates the wave-5 container work. | Retitle "Scoping decisions (v0.1 baseline; amended)", add an "Amendments since v0.1" list, and fix item 6 to match security-model.md §1. |
| A5-013 | M | docs/security-model.md:190 and 228 | §6 is "Known limitations (accepted for v0.1)" and has not been refreshed since. Line 228 describes `DenyAllSkillPolicy` as the fail-closed default "until wired", but `api/app.py:208` wires `EngineSkillPolicy`. | Reword: "default if no policy is injected; the app wires EngineSkillPolicy (`policy/skill_bridge.py`)". Re-date §6 limitations. |
| A5-014 | M | src/mcprouter/analytics/feedback.py:4 | The docstring says "`router.feedback` is NOT wired yet". It is wired (`gateway/server.py:136`, `:491`) and CHANGELOG 0.5.0 and analytics.md advertise it. This is a code comment, but a reader will trust it. | Delete that sentence. Out of A5 scope to edit, so route to the code owner. |
| A5-015 | M | src/mcprouter/settings.py:21-23 | The comments list `hash \| bge` and `deterministic \| laya \| remote`. The accepted sets also include `aoai` (`engine.py:64-65`). The settings file is the de facto config reference. | Update the comments. |
| A5-016 | M | CHANGELOG.md:1-31 | Entries for 0.1 to 0.4 are single lines with no dates and no compare links. 0.5.0 has no date. There is no "Unreleased" section, no upgrade notes, no list of new env vars (`MCPR_ALLOWED_HOSTS`, `MCPR_UI_DIST`, `MCPR_HOST_PORT`, ...) or API additions (setup, feedback, `/decision/systemone`). Version history lacks "Breaking / schema" markers. | Adopt Keep-a-Changelog. Add dates, "Config" and "Schema" subsections per release, and an Unreleased heading. |
| A5-017 | M | docs/INTEGRATION_NOTES-* (24 files, 3,538 lines) | Wave and branch work logs (`feat/ui`, `wave4/ui`, "integrate/v0.1"). No README or CLAUDE.md links to them. They are referenced by **22 code and test comment sites** (`grep INTEGRATION_NOTES src tests`), so moving them breaks those pointers unless the comments are updated. One (`wave5-container.md:2-4`) is stale: it says the README quickstart assumes `:5434`, but README now uses the dev override. | Move to `docs/history/`. In the same commit, sed the 22 comment references to `docs/history/...`. Add `docs/history/README.md` ("archival, not operator docs"). Fold any still-true facts into SPEC or the architecture doc first. |
| A5-018 | M | docs/skills-plan.md:42-56 | A planning doc ("Wave 4 ... Fences (wave 4 executors)", branch ownership S1 to S4). It is linked from skills.md:5 as the "design record". The "Fences" section is executor-assignment scaffolding, not design. | Move to `docs/history/` or `docs/design/`, trim the Fences section, and keep the link from skills.md. |
| A5-019 | M | docs/SPEC.md:98-106 | Names `GET /api/v1/metrics`. Prometheus is mounted at `/metrics` (`api/app.py:235`) and no `/api/v1/metrics` route exists. SPEC §9 also shows `agent_id` in the `/route` body, which security-model.md says is ignored. SPEC is labelled "v0.2 proposing spec" but is read as current. | Add a banner "Proposal v0.2; shipped behaviour differs, see API reference" plus a short "Deviations" list (metrics path, agent_id ignored, SPEC §5 LLM fallback out of scope per scoping #2). |
| A5-020 | M | no CONTRIBUTING.md; CLAUDE.md:1-49 | CLAUDE.md is the only contributor guide and is written for the AI agent: it says "No GPU on this dev box" and "Co-Authored-By trailers". A human contributor gets no steps: clone, `uv venv`, install, start the dev db, run gates, run UI tests, where tests live. `tests/conftest.py:34` **silently skips** all `requires_db` tests when the db is down. A contributor can see "green" with integration tests skipped, while CLAUDE.md says integration tests REQUIRE the db. | Add CONTRIBUTING.md with a numbered setup and the gates (`ruff check/format`, `mypy`, `pytest`, `npm test/lint/build`). Add a warning about skip-when-no-db, and a command to count skips: `pytest -rs -q`. Consider `MCPR_REQUIRE_DB=1` to make absence an error (code-side suggestion). |
| A5-021 | M | no CLI/config reference | Config is scattered over README, deploy.md (compose vars), backends.md, analytics.md, skills.md and INSTALL. About 40 `MCPR_*` keys exist in `settings.py:84-144` and only ~25 are documented anywhere. Undocumented: `MCPR_DEFAULT_TOOL_TIMEOUT_S`, `MCPR_RATE_LIMIT_PER_AGENT_PER_MIN`, `MCPR_RETRIEVAL_CANDIDATES`, `MCPR_ROUTE_CONFIDENCE_FLOOR`, `MCPR_EMBEDDING_MODEL_ID`, `MCPR_LAYA_MODEL_ID`, `MCPR_OPERATING_MODE`, `MCPR_IDLE_UNLOAD_S`, `MCPR_EMBED_BATCH_SIZE`, `MCPR_LAYA_NOUL_MODE`, `MCPR_MAX_EXPOSED_TOOLS`, `MCPR_MAX_EXPOSED_SERVERS`, `MCPR_DECISION_TIMEOUT_S`, `MCPR_ROUTE_CACHE_TTL_S`, `MCPR_ROUTE_CACHE_SIZE`, `MCPR_SYNC_ENABLED`, `MCPR_UI_DIST`, `MCPR_MODELS_CACHE_DIR`, `MCPR_DATABASE_URL` (user table). Entry-point flags live only in `--help` of testbed and bench. | Create `docs/reference/configuration.md` generated from `Settings` (script plus CI diff). Add `docs/reference/cli.md` for testbed.*, bench.routing_latency and scripts/. |
| A5-022 | M | no architecture overview | The README has two mermaid diagrams and a repo map, but no single page that says: modular monolith, one worker, request lifecycle, data model, module boundaries, extension seams (`DecisionModel` protocol). The best statements are scattered in scoping.md, security-model.md §6 and the INTEGRATION_NOTES. | Write `docs/ARCHITECTURE.md` (about 1 page) from scoping #3-5, README diagrams and security-model §6. |
| A5-023 | M | no API reference | About 60 REST routes exist. Docs describe them piecemeal (skills.md tables, INSTALL curl). Nothing says where the OpenAPI is (`/docs`, set in `api/app.py:135`), whether `/docs` and `/openapi.json` are auth-gated, or what each admin vs agent route needs. The untracked `docs/audit/openapi-routes.json` shows the list is easily generated. | Add `docs/reference/api.md` generated from `app.openapi()` (method, path, auth class, tag). Mention `/docs` in README. Add a CI drift check. |
| A5-024 | M | docs/deploy.md:35-37 | The inference/GPU flavor commands assume a prior manual `docker build -t mcp-router:local .`. The "Verified" block also documents a host-specific `--network=host` workaround for BuildKit stalling `npm ci`. First-time users hitting that stall have no cross-link from the quickstart. | Move the workaround to a "Troubleshooting" section and link it from the Quickstart. State whether the first `up --build` needs `docker build` first. |
| A5-025 | L | README.md:45; docs/skills.md:1; .env.example:15 | "Skills (v0.4)" labels. The version tag adds nothing for a 0.5.0 product and reads as a stale status. CHANGELOG is the right home. | Drop the "(v0.4)" suffixes. |
| A5-026 | L | src/mcprouter/api/app.py:135; gateway/server.py:364 | Version `"0.5.0"` is hardcoded in two more places (plus `__init__.py`, pyproject, package.json). All agree now, but five sources of truth drifted before (`c01ef06` "advertise 0.4.0 everywhere"). | Have app.py and server.py import `mcprouter.__version__`. Add a test asserting pyproject, package.json and `__version__` match. |
| A5-027 | L | docs/INSTALL.md:19-22 vs README.md:131-133 | Two different quickstart sequences (env vars exported in shell vs `.env`). Not contradictory, but the user must pick. `export` plus `docker compose up` works because compose reads the shell env, and that is not stated. | Pick `.env` as canonical in both. Mention the shell-export alternative once in deploy.md. |
| A5-028 | L | scripts/smoke.sh:3-5, 8 | Defaults `BASE=http://127.0.0.1:8450` and `COMPOSE="docker compose -p mcprsmoke"`, which differ from the README's 8400. Fine for CI, but a user running it against the README stack fails. `scripts/docker-entrypoint.sh` header is accurate. `scripts/` has no README. | One line at the top of README "Run it" or deploy.md: `BASE=http://127.0.0.1:8400 COMPOSE="docker compose" scripts/smoke.sh`. |
| A5-029 | L | docs/hardware-validation.md (whole) | Accurate and clearly marked UNPROVEN, but it is a runbook for one future day and does not say the T1 150 ms target is probably unreachable (it does at ~line 180, in the findings). The README ledger lists T1 as "pending" without the caveat. | Add "T1 at risk, see hardware-validation.md" in the README ledger row. |
| A5-030 | L | README.md:186 | The ledger count "1339 backend (+5 skipped) + 131 UI". UI: 131 matches a grep of `it(`/`test(` in `ui/src`. Backend: 910 `def test_` functions plus parametrization, so 1339 is plausible but unverifiable without running pytest. It was last stamped at `767d31b` and three commits have landed since. | Re-stamp after the next full run. Add the `git` rev next to the number. |
| A5-031 | L | .gitignore; skills.md; `MCPR_SKILLS_CACHE_DIR` default `./skills-cache` | `models-cache/` is ignored. `skills-cache/` is not, and git skill sources clone into it on a source install. `uv.lock` is untracked although README/CLAUDE.md say "uv-managed". | Ignore `skills-cache/`. Commit `uv.lock` (or say it is not meant to be committed). |
| A5-032 | L | deploy/ (empty dir) | Empty directory at repo root, not referenced by any doc. | Remove, or document its purpose. |
| A5-033 | L | docs/backends.md:99-100 | "Edge pattern" rate-limit and 503-hop claims could not be cross-checked here beyond route existence (`POST /api/v1/decision/systemone`, `routes_decision.py`). Marked unverifiable in the matrix. | A4 or the code auditor to confirm. |

## 2. Doc claim verification matrix

"True" means checked in source or by running `--help`. "Unverifiable" means not checkable read-only.

| Claim | Verdict | Citation |
|---|---|---|
| Version 0.5.0 in pyproject, `__init__`, app.py, gateway, ui/package.json | **True** | pyproject.toml:3; `src/mcprouter/__init__.py:3`; `api/app.py:135`; `gateway/server.py:364`; `ui/package.json:4` |
| Every relative `](...)` link in README, docs/*.md resolves | **True** | resolver run over all `*.md`, 0 broken |
| Every backticked repo path in README, CLAUDE.md, docs/* (non-notes) exists | **True** | path resolver, 0 missing |
| `docs/security-model.md#skills-wave-4-s3` anchor | **True** | heading at security-model.md:210 |
| `classify_skill`, `SkillExposure`, `test_evaluate_signature_is_score_free`, `test_no_catastrophic_backtracking` exist | **True** | `registry/classify.py:217`; `gateway/skills.py`; `tests/test_policy_engine.py`; `tests/test_execution_redaction.py` |
| `python -m testbed.skills.generate --out --skills [--seed] [--include-invalid] [--as-git]` | **True** | `--help` output matches skills.md:66-67 |
| `python -m testbed.serve --servers 10` | **True** (flag exists) | `--help`; but see A5-011 for the missing follow-up |
| `bench.routing_latency` flags (`--embedding --decision --device --mode --iterations --batch-sizes --parity --out`) used in hardware-validation.md | **True** | `--help` output |
| `bench/results/cpu-baseline.json`, `cpu-zero-ml.json` | **True** | `ls bench/results` |
| `MCPR_*` named in docs exist in code | **Mostly true** | Doc-only names are compose, entrypoint or test knobs (`MCPR_HOST_PORT`, `MCPR_PORT`, `MCPR_IMAGE`, `MCPR_BASE_IMAGE`, `MCPR_INFERENCE_IMAGE`, `MCPR_TORCH_INDEX_URL`, `MCPR_DB_WAIT_TRIES`, `MCPR_RUN_SLOW`, `MCPR_AGENT_KEY` (smoke), `MCPR_KEY` (client example)), each used in compose, scripts or tests. `MCPR_MCP_ALLOWED_HOSTS` appears only in INTEGRATION_NOTES (historic rename). |
| Default decision backend is `laya` | **False** | settings.py:23 `"deterministic"`; docker-compose.yml `deterministic` (A5-001) |
| Default embeddings "local" / `.env.example` value `local` | **False** | engine.py:64 `("hash","bge","aoai")` (A5-002) |
| `.env.example` is where you add admin token and agent keys | **False** | file has no such line (A5-003) |
| Bare-metal `docker compose up -d db` reaches the DB at :5434 | **False** | docker-compose.yml: "No host port"; only the dev override publishes (A5-004) |
| Eval dataset has 77 cases | **False** | `load_named("synthetic_v1")` = 111 (A5-007) |
| `GET /api/v1/metrics` | **False** | no such route; `/metrics` ASGI mount `api/app.py:235` (A5-019) |
| Dashboard served at `:8400/` by the API, `ui/dist`, override `MCPR_UI_DIST` | **True** | `settings.py:75`; `api/static.py`; `api/app.py:237`. The "dev server on :5180" claim is correctly scoped to `npm run dev` (INSTALL.md:44; `ui/vite.config.ts:11`). |
| `/healthz` returns `{"status":"ok"}` and does a DB check | **True** | `api/app.py:227-233` |
| `POST /api/v1/servers/import`, `/principals`, `/principals/{id}/rotate-key`, `/policy-rules`, `/skill-sources`, `/skill-sources/{id}/sync`, `/skills/bundle`, `/skills/{id}/activate`, `/route/simulate`, `/route/{id}/feedback`, `/analytics/rollup`, `/setup/status`, `/decision/systemone`, `/models/health` | **True** | `docs/audit/openapi-routes.json` plus router decorators |
| `GET /api/v1/skills/{id}/resources/{path}` | **True** | `openapi-routes.json` |
| Skill source `syncIntervalS` default 3600, min 60 | **True** | `routes_skill_sources.py:37,45` |
| `max_skills` request 1-1000; principal 0-64 default 3 | **True** | `routes_route.py:105`; `routes_policy.py:70,78` |
| Bundle marker `.mcp-router-bundle.json` | **True** | `skills/bundle.py:27` |
| `SKILL.md` frontmatter cap "body cap + 16 KiB" | **True** | `skills/validate.py:22` |
| `router.find_tools`, `router.feedback` meta-tools | **True** | `gateway/server.py:97,136` |
| `router.activate_skill`, `router.read_skill_resource` meta-tools | **Likely true** | named at `gateway/skills.py` and smoke.sh output; not read in depth |
| INSTALL: `list_changed` via `subscriptions/listen` (2026-07-28 revision) | **True** | `gateway/server.py:30-31,239` |
| INSTALL: `421` for non-localhost Host unless `MCPR_ALLOWED_HOSTS` | **Unverifiable here** | settings.py:144,149-158 parse the list; runtime behaviour is covered by tests (not run) |
| deploy.md: single uvicorn worker, entrypoint guards, uid 1000, `/healthz` HEALTHCHECK | **True** | `scripts/docker-entrypoint.sh:50`; Dockerfile:45 |
| deploy.md: release workflow pushes `ghcr.io/amantux/mcp-router` tags | **Unverifiable** | `.github/workflows/release.yml` exists (job names `image`, `image-inference`); registry path not checked |
| deploy.md "Image built here: 1.54 GB" and smoke transcript | **Unverifiable** | historical measurement dated 2026-10-09; no docker run |
| "100 servers / 1,000 tools refresh in 6.1 s", "Laya loaded on CPU", "0 unauthorized executions" | **Unverifiable** | measurement claims; `bench/results/*.json` hold CPU baselines only |
| "100+ security guards mutation-checked" | **Unverifiable** | no list in the repo. The security-model.md preamble points to "the gateway workstream's report", which is not shipped. |
| "1339 backend (+5 skipped) + 131 UI tests" | **Backend unverifiable; UI true by static count (131)** | A5-030 |
| Ruff pinned 0.15.22 with pinned select; mypy strict | **True** | pyproject.toml `[project.optional-dependencies].dev` (`ruff==0.15.22`) and `[tool.ruff.lint] select`; `[tool.mypy] strict = true` |
| CLAUDE.md: "mypy (strict)" covers the code | **True with a gap** | `[tool.mypy] files = ["src/mcprouter", "bench"]`; `testbed/` and `tests/` are not type-checked |
| README: "Non-root image, internal-only Postgres" | **True** | compose has no db ports; entrypoint uid check |
| CHANGELOG 0.5.0 items: container, allowed hosts, setup wizard, feedback | **True** | compose files; `settings.py:144`; `routes_setup.py`; `routes_feedback.py` |
| analytics.md: "usage prior not wired into routing" | **Consistent** | settings.py comment says the same. Not traced to the routing code. |

## 3. First-run path: zero to a routed tool

**Docker (README "Run it") has 10 steps**, ordered here by when each blocks:

1. `cp .env.example .env`
2. **Hand-edit `.env`** to add `MCPR_ADMIN_TOKEN` and `MCPR_AGENT_KEYS`. Nothing tells you to generate them; the template is empty (A5-003). Skipping leaves the API in open dev mode.
3. `docker compose up -d --build --wait`. This needs Compose v2 and builds the UI (`npm ci`), which stalled for the verifier on a bridge network (deploy.md:59-61, A5-024).
4. `curl /healthz`. On Linux the `&& open ...` fails (A5-008).
5. Open the dashboard and click **Connect**, pasting the admin token.
6. If `AGENT_KEYS` was set, the wizard never appears (A5-009). Otherwise create an agent in `/setup`.
7. **Register a server** (import `mcpServers` JSON or dashboard). Stdio `npx` / `uvx` servers will not run in the image (**A5-005**). The first success needs an HTTP server or `testbed.serve`, which is itself a source-install tool.
8. **Grant a policy rule.** An agent with no rules gets an empty list (deny by default). The wizard offers read-only starter rules, but the REST-only path (INSTALL section 2) needs a hand-written curl with a `serverId`.
9. Point the client at `/mcp` with the agent key (INSTALL section 4).
10. Ask the agent to call `router.find_tools` and verify a routed tool.

Expect about 10 steps, with the stops at 2, 7 and 8.

**Source install:**

1. Install uv and Node (undeclared prerequisites).
2. Start the dev DB with the **override** (README) or without it (INSTALL, wrong, A5-004).
3. `uv venv`
4. `uv pip install -e '.[dev]'` (optionally `[inference]`).
5. Export two env vars.
6. Run uvicorn.
7. `cd ui && npm ci && npm run build`. This must run before the server starts, or the server serves the "UI not built" page (`api/static.py:45`).

Then steps 5 to 10 above.

## 4. Proposed documentation information architecture

```
README.md                    Keep: pitch, 5-step quickstart, status ledger, doc index. Move the long
                             Skills/feature bullets to docs/. Add a doc index table.
CHANGELOG.md                 Keep. Keep-a-Changelog format with dates, Config and Schema subsections.
CONTRIBUTING.md              NEW. Human setup, gates, db-skip trap, worktree and .venv notes, commit style.
CLAUDE.md                    Keep (agent rules). Link to CONTRIBUTING; drop duplicated gate lists.
docs/
  ARCHITECTURE.md            NEW (1 page). Monolith, request lifecycle, modules, extension seams.
  INSTALL.md                 Keep (client snippets); fix section 1; link deploy.md for the run steps.
  deploy.md                  Keep; split "Verified transcript" into a collapsed appendix. Add
                             stdio-in-Docker and troubleshooting.
  UPGRADING.md               NEW. init_db semantics, backup, no downgrade, per-release schema deltas.
  security-model.md          Keep; refresh section 6; fix line 228.
  backends.md, analytics.md, skills.md   Keep; fix defaults (A5-001/002); drop "(v0.4)".
  reference/
    configuration.md         NEW, generated from Settings (all MCPR_* with default, type, valid values)
    api.md                   NEW, generated from app.openapi()
    cli.md                   NEW (testbed.*, bench.*, scripts/*)
  hardware-validation.md     Keep (runbook).
  SPEC.md, scoping.md        Keep as "design baseline"; banner plus "Amendments since v0.1".
  history/                   MOVE here: all INTEGRATION_NOTES-*, skills-plan.md (update the 22 code refs).
  audit/                     Keep for audit reports; do not ship stale openapi-routes.json (generate it).
```

Sequencing: do the `history/` move plus the 22 comment rewrites in one commit. A pure file move plus sed is
behaviour-neutral, which suits the project's "refactor commit separate from behaviour commit" rule.

## 5. Docs meta-test proposal

File: `tests/test_docs_consistency.py` (pure Python, no DB, no network, fast, runs in the existing CI `pytest` step).

1. **Links resolve.** For every tracked `*.md` outside `docs/history/` and `node_modules`, extract `](target)`
   with a regex that ignores fenced code blocks. Skip `http(s):`, `mailto:` and bare `#frag`. Resolve relative
   to the file, strip the fragment, and assert existence. For `file.md#frag` also assert the heading slug exists
   (GitHub-style slugger: lowercase, strip punctuation, spaces to `-`). Catches broken `skills-wave-4-s3`-style anchors.
2. **Backticked repo paths exist.** Regex for `` `(src|tests|testbed|bench|ui|scripts|docs|\.github)/...` ``
   and assert `Path.exists()`. Allow `*` and `{}` globs through `glob`. Strip `::symbol` and grep the symbol
   in that file (catches renamed functions such as `classify_skill`).
3. **Every documented `MCPR_*` exists.** Collect `MCPR_[A-Z0-9_]+` from README, CLAUDE.md, `.env.example`, docs/*.md
   (not history). Allowed set = `{Settings.from_env env keys}` extracted by regex from `src/mcprouter/settings.py`
   (`get\("(MCPR_[A-Z_]+)"`), plus an explicit `NON_SETTINGS_ALLOWLIST` for compose and test knobs
   (`MCPR_HOST_PORT, MCPR_PORT, MCPR_IMAGE, MCPR_BASE_IMAGE, MCPR_INFERENCE_IMAGE, MCPR_TORCH_INDEX_URL,
   MCPR_DB_WAIT_TRIES, MCPR_RUN_SLOW, MCPR_AGENT_KEY, MCPR_KEY, MCPR_ADMIN_TOKEN`). Each allowlist entry must
   itself be found in compose, scripts or tests, so the allowlist cannot rot.
4. **Reverse check (the missing-docs direction).** Every key in `settings.py` must appear in
   `docs/reference/configuration.md`, or in a `# undocumented-ok:` allowlist. This closes A5-021 permanently.
5. **`.env.example` is valid.** Parse each commented `# MCPR_X=value` line, strip the comment, and for the enum
   keys assert the value is in `EMBEDDING_BACKENDS` / `DECISION_BACKENDS` / `{"auto","cpu","cuda"}`.
   Fails on today's `MCPR_EMBEDDING_BACKEND=local` (A5-002). Also assert `.env.example` mentions every
   `${MCPR_*}` / `${POSTGRES_*}` variable that docker-compose.yml consumes.
6. **Documented defaults match code.** Parse the `| Variable | Default |` tables in deploy.md and the settings
   comments; assert `Settings().decision_backend == "deterministic"`. Cheap guard for A5-001 drift.
7. **Version lockstep.** Assert `pyproject.version == mcprouter.__version__ == ui/package.json.version`, that
   the top CHANGELOG heading equals it, and (after A5-026) that `create_app().version` and the gateway version match.
8. **Endpoint mentions exist.** Regex `(GET|POST|PATCH|DELETE) /api/v1/...` in docs, normalise `{id}` / `{toolId}`
   to `{}`, and check against `create_app().openapi()["paths"]` (needs no DB if the app factory is injectable;
   otherwise use the generated `docs/reference/api.md`). Would have caught `/api/v1/metrics`.
9. **CLI examples run.** For each `python -m (testbed|bench)...` string in docs, run `python -m <mod> --help`
   (subprocess, 5 s timeout) and assert the documented flags appear in the output.
10. **Mutation check (house rule).** Each rule needs a fixture that breaks it: add a bogus link, a bogus
    `MCPR_NOPE`, and flip a version, then assert the named test fails. A temp-dir param version of the checkers
    makes this trivial.

Wire this into CI's `backend` job. Rules 3, 5 and 6 turn today's A5-001/002/003 findings from silent drift into red builds.

## 6. Noticed (out of A5 scope)

- `alembic==1.17.1` is a runtime dependency with no migrations in the repo (A5-006).
- `mypy` does not cover `testbed/`, so its argparse CLIs are untyped.
- The conftest skip-when-no-db behaviour hides failures on a contributor machine (A5-020).
