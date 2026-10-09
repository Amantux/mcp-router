# A7 - Containers, CI/CD, operations audit

Scope: /root/mcp-router, working tree HEAD 1886ede (task named 64bcbfc; I read the tree as found). Read-only audit.
Method: files read in full; `docker compose config` (base, and base+inference+gpu); `gh run list/view`; one 3-line
logging repro with `.venv/bin/python`. **Verified** = I read or ran it. **Believed** = from memory of the tool or
dependency, not checked against the installed version (flagged inline).

## Summary

No blocker for a localhost/dev deployment. Weak areas: (1) the compose env whitelist silently drops most documented
settings, (2) fail-open defaults on a published port, (3) logging is effectively unconfigured, (4) release is ungated,
has no tag/version check, no scan, no signing, and has never run, (5) no Dependabot and no security-scan workflow.
21 findings: 0 critical, 5 high, 11 medium, 5 low.

## Findings

| id | severity | file:line | finding | proposed fix | test/check to add |
|---|---|---|---|---|---|
| A7-001 | high | docker-compose.yml:28-39 | `api` forwards only 7 env vars (DATABASE_URL, AGENT_KEYS, ADMIN_TOKEN, EMBEDDING/DECISION_BACKEND, DEVICE, ALLOWED_HOSTS) and there is no `env_file:`. `.env` feeds only `${}` substitution, so `MCPR_SYNC_ENABLED`, `MCPR_ANALYTICS_ROLLUP_ENABLED`, `MCPR_DECISION_ENDPOINT/API_KEY/MODEL`, all `MCPR_AOAI_*`, `MCPR_SKILL_*`, rate limit, `MCPR_IDLE_UNLOAD_S`, `MCPR_CURRENCY` etc. never reach the container (verified: `compose config` environment lists exactly those 7 + HF_HOME). `.env.example:1-19` documents them as if they work. Result: in the shipped stack SyncLoop and RollupLoop (default False, settings.py:45-51) can never start, and remote/AOAI backends are unusable. | Add `env_file: [{path: .env, required: false}]` to `api` (keep explicit entries for derived values such as DATABASE_URL), or forward the full documented set. | M2 below: for every `MCPR_*` read in settings.py, set it in a temp `.env`, run `docker compose config`, assert it appears in `api.environment`. |
| A7-002 | high | docker-compose.yml:33,40-41; scripts/docker-entrypoint.sh:13-17 | Fail-open default on a public listener. With no tokens the stack publishes `0.0.0.0:8400` with the admin API OPEN (dev mode, app.py:148) and only a log WARNING. A user who runs `docker compose up` and skips the "append tokens" step exposes an unauthenticated admin API (create principals, add MCP servers, execute tools) to the LAN. | Default bind to loopback (`"${MCPR_BIND:-127.0.0.1}:${MCPR_HOST_PORT:-8400}:8400"`) and/or FATAL in the entrypoint when no admin token and no agent keys, unless `MCPR_ALLOW_OPEN_DEV=1`. | M1: no token/keys -> container exits non-zero with `FATAL` unless override set; `compose config` default binding is 127.0.0.1. |
| A7-003 | high | src/mcprouter/api/app.py:70-91; pyproject.toml:17 | Logging is not configured. `structlog==25.5.0` is a pinned dependency but is imported nowhere in src/tests/docs (verified grep). No `basicConfig`/`dictConfig`; uvicorn configures only its own loggers. Repro (verified): root logger has 0 handlers, level WARNING; `mcprouter.*` `log.info` is dropped (14 `log.info` call sites), `log.warning` prints via the last-resort handler as a bare message with no timestamp, level or logger name. No JSON option. | One `configure_logging(settings)` called from `create_app` (stdlib `dictConfig`, `MCPR_LOG_LEVEL`, `MCPR_LOG_FORMAT=text|json`; keep the existing "log exception TYPE only" discipline). Use structlog for real or drop the dependency. | After `create_app()`, `getLogger("mcprouter.x").info` is emitted at LEVEL=INFO with level+name; secret-scrub test (token inside an exception message never appears in output). |
| A7-004 | high | .github/workflows/release.yml:4-6,18-44 | Release is not gated on CI and does no version check. Any `v*` tag publishes `:<tag>` and `:latest` with no `needs`, no CI-status check, no smoke of the built image, and no comparison of tag vs version. Version lives in 5 hand-edited places: pyproject.toml:3, src/mcprouter/__init__.py:3, api/app.py:135, gateway/server.py:364, ui/package.json:4 (verified). Zero tags and zero release runs exist (verified: `git tag` empty, `gh run list --workflow release` empty), so the workflow has never executed. | Add a `verify` job: tag minus `v` must equal pyproject version (and the other 4, or derive them from `importlib.metadata`), CHANGELOG must have that heading, the tagged SHA must have a green `CI` run; make `image` `needs: verify`. Restrict trigger to `v[0-9]*.[0-9]*.[0-9]*`. | M5 below, plus a dry run with a pre-release tag before the first real tag. |
| A7-005 | high | .github/ (only workflows/ci.yml, release.yml); .gitleaks.toml | No `.github/dependabot.yml` (verified) and no CodeQL/trivy/gitleaks/pip-audit/npm-audit/dependency-review/SBOM workflow; `.gitleaks.toml` is committed but no job runs it. Exact-pinned deps plus an untracked `uv.lock` (`git status` shows `?? uv.lock`) mean no automated bump path and no CVE signal. | Add both files, shapes given at the end of the CI section. | `gh run list --workflow "Security scan"` by name after adding; YAML parse check. |
| A7-006 | medium | src/mcprouter/api/app.py:235; static.py:25 | `/metrics` is mounted unauthenticated on the same listener as the admin API and is published on the host port. Content is funnel counters (no per-tool labels, metrics.py docstring) plus default process/python collectors, so the leak is small, but it is free recon and cannot be restricted separately. deploy.md never mentions `/metrics`. Believed: `app.mount("/metrics")` 307-redirects `/metrics` to `/metrics/` (check with `curl -i`). | Require admin bearer or `MCPR_METRICS_TOKEN`, or serve on a separate internal port; document it. | With an admin token set, unauthenticated `GET /metrics` -> 401/403, authenticated -> 200 containing `mcpr_analytics_`. |
| A7-007 | medium | src/mcprouter/db.py:44-63,84-86 | Boot-time DDL runs on EVERY start: `UPDATE ... WHERE resource_kind IS NULL` (sequential scan of `execution_records`, an append-only audit table), four `ALTER COLUMN TYPE VARCHAR(48)` and two `SET NOT NULL`, each taking ACCESS EXCLUSIVE locks. Believed (not tested): same-type ALTER and already-set NOT NULL skip the rewrite/scan but still take the lock; the UPDATE definitely scans. Start time and lock contention grow with data; healthcheck start-period is only 20 s (Dockerfile:44). | Record applied bridge steps in a `schema_version` row and run each once; or guard with `information_schema` checks; set `lock_timeout` for the DDL session. | Seed 1M rows in `execution_records`; second start finishes in < 5 s and issues no UPDATE. |
| A7-008 | medium | src/mcprouter/db.py:23-26,67-86; pyproject.toml:12; docs/deploy.md:50-53 | Upgrade path is create_all + additive ALTERs ("Alembic deferred"), yet `alembic==1.17.1` is a runtime dependency imported nowhere (verified grep over src, tests, bench, testbed). No stored schema version, so an older image started on a newer DB cannot detect it; type changes/drops cannot be expressed; downgrade is undocumented (deploy.md says only "pull/rebuild and up"). Concurrent starters (`up --scale api=2`, or old/new overlapping during `up -d`) race the DDL with no advisory lock (believed benign for IF NOT EXISTS, can deadlock on ALTER). | `schema_version` row; refuse to start when DB version > code version; `pg_advisory_lock` around `init_db`; document "downgrade = restore the pre-upgrade pg_dump". Either adopt Alembic or drop the dependency. | DB version = code+1 -> FATAL with curated message; two concurrent `init_db` calls both succeed. |
| A7-009 | medium | docker-compose.yml (api has no `stop_grace_period`); scripts/docker-entrypoint.sh:49-50 | Graceful shutdown is unbounded. `exec uvicorn` is correctly PID 1 and receives SIGTERM (verified), but there is no `--timeout-graceful-shutdown`. Believed: uvicorn waits for open connections (MCP streamable-HTTP/SSE streams are long-lived) with no deadline, then Compose SIGKILLs at the default 10 s, skipping the lifespan `finally` (app.py:127-133: rollups.stop, loop.stop, inference.unload) and any in-flight execution/audit write. Also `SyncLoop.stop()` cancels a task awaiting `anyio.to_thread.run_sync` (loop.py:65-71); anyio threads are not abandoned on cancel by default (believed), so an in-flight sync can push shutdown past 10 s. | `--timeout-graceful-shutdown 20` in the entrypoint, `stop_grace_period: 30s` in compose; confirm a timeout on the git subprocess in skills/gitsource.py. | M4(d): open MCP stream, `docker stop -t 30`, exit code 0 and shutdown-complete log within 25 s. |
| A7-010 | medium | Dockerfile:30-33 | Poor layer caching: `COPY src` precedes `pip install .`, so any source edit reinstalls all dependencies. CI compose-smoke spends 70 s of its 77 s building (verified) and has no buildx cache. | Install dependencies in a layer keyed on pyproject only, then COPY src and `pip install --no-deps .`; add `cache-from/to: type=gha` in CI and release. | Touching a `src/` file rebuilds only the last layers (`CACHED` for the dependency layer). |
| A7-011 | medium | Dockerfile:1,13; ci.yml:72; ui/package.json:6-8 | Node drift: Docker builds the UI on `node:22-slim`, CI tests it on Node 20, `engines` says `>=20.19`. The shipped artefact is built on a different Node than the one tests ran on. Base images are tag-pinned only (`python:3.12-slim`, `node:22-slim`, `pgvector/pgvector:pg16`), no digests. | One Node version everywhere (`.nvmrc` + Dockerfile ARG + CI); pin digests once Dependabot docker is on. | CI step compares `node -v` major to the Dockerfile ARG. |
| A7-012 | medium | .github/workflows/ci.yml:3-6 | No path filters: docs-only pushes run backend (140-175 s), ui and compose-smoke in full (verified: the `docs:` and `test(ui):` pushes in the last 10 runs ran every job). Workflow-level `paths:` would stall the required `ci-gate`, so use a `changes` job. | `changes` job (paths-filter action pinned by SHA) feeding `if:` on each job; ci-gate treats `skipped` as pass only when the filter says irrelevant. | Docs-only PR shows backend/ui/smoke skipped and ci-gate green. |
| A7-013 | medium | ci.yml:75-78; recent runs | UI flake with no policy: 2 of the last 8 master runs failed at `npm test` (37815571643, 37802345192; fixed by 1886ede "let the playground's async loads settle"), turning master red and ci-gate red. No failure artifacts. Backend pytest has no per-test timeout (a hang burns the full 30 min). | No blanket retry (it hides races). Upload vitest output on failure; add `pytest-timeout` or a ~10 min `timeout-minutes` on the pytest step. | Track the flake rate with `gh run list --json conclusion`; review if above 5%. |
| A7-014 | medium | .github/workflows/release.yml:46-74 | The inference image is built only at release time, with `continue-on-error: true` (line 49): `Dockerfile.inference`, both overrides and the torch index logic are untested until a tag, and a failure there is green. It also lacks the `provenance`/`sbom` the base image gets (lines 40-41), has no cache, and is amd64-only. A retried job can leave `latest-inference` on a different base than `latest`. | Build `Dockerfile.inference` in a weekly/`workflow_dispatch` CI job (no push); pass provenance/sbom in release; surface failures with an annotation. | Weekly job builds the inference image and runs smoke with the hash fallback (no model network needed). |
| A7-015 | medium | docker-compose.inference.yml:12-13; Dockerfile.inference:7-8 | Stale-base trap: the inference override rebuilds only `Dockerfile.inference` FROM the pre-existing local `mcp-router:local`. `docker compose -f ... -f docker-compose.inference.yml up -d --build` after `git pull` does not rebuild the base, so the "upgraded" inference container runs OLD application code. Documented only as "build the base first" (compose header, deploy.md:42). | Single multi-stage Dockerfile with `--target inference`, or compose `additional_contexts`/build ordering; at minimum put the explicit base rebuild in the upgrade docs. | Smoke asserts `mcprouter.__version__` in the container equals the checked-out version. |
| A7-016 | medium | docker-compose.yml:8,25-26 | No resource limits (`mem_limit`, `cpus`, `pids_limit`) and no torch/OMP thread control (grep of src shows none); in-process models can take GBs and threads equal host cores. A runaway model or large skill clone can OOM the host, and single-worker design makes the container a single point of failure. Runtime RSS per flavor is unmeasured (only image sizes: 241 MB base, 1.54 GB inference). | Commented example limits, `OMP_NUM_THREADS` passthrough, measure and document RSS idle/under load (link hardware-validation.md). | RSS table in deploy.md produced by a script, rerun per release. |
| A7-017 | low | Dockerfile:44-45; app.py:227-233 | `/healthz` is `SELECT 1` only. Model load completes before uvicorn accepts connections, so healthy implies engine loaded (verified from ordering), but degraded fallbacks (engine.py:377,398) are invisible to Docker; `restart: unless-stopped` does not act on `unhealthy` (verified: no autoheal), so an unhealthy container just stays up. Inference override sets `start_period: 300s` (merged, verified) but a cold download longer than that burns the retries. | Keep /healthz as liveness; add `/readyz` (db, engine loaded, degraded flag). Document the semantics. | `/readyz` reports degraded when `MCPR_DECISION_BACKEND=laya` is requested without laya installed. |
| A7-018 | low | docker-compose.gpu.yml:12,15-21; Dockerfile.inference:11-12 | GPU override renders valid (verified `compose config`: nvidia driver, count 1, capabilities gpu; `MCPR_DEVICE=cuda`). Unproven items (believed, unchecked): the cu124 index serves torch only up to some release for cp312 while pyproject says `torch>=2.4` unpinned, and the second `pip install "/srv[inference]"` has no `--index-url`/constraint, so a resolver change could swap the CUDA wheel for a PyPI build. `MCPR_DEVICE=cuda` falls back to CPU with only a curated note (engine docstring), so mis-setup is silent. | Pin `torch==X.Y.Z` via ARG and `--constraint` on both pip calls; log `torch.version.cuda` and `cuda.is_available()` at startup. | On a GPU host: `compose exec api python -c "import torch;print(torch.cuda.is_available(), torch.version.cuda)"`; CI: `pip freeze | grep torch` identical before/after the second pip call. |
| A7-019 | low | Dockerfile:21-23; docker-compose.inference.yml:18; docs/deploy.md:50-53 | Model cache and backups. `HF_HOME` persists weights in `mcpr-models-cache`, but `HF_HUB_OFFLINE` is never set and air-gapped use is undocumented (laya.py:117 `snapshot_download` with pinned revisions; Hub calls mostly skipped when pinned, believed). Backup text covers `mcpr-pgdata` and `mcpr-skills-cache` but gives no `pg_dump`/restore commands, says nothing about `mcpr-data`, and does not state that embeddings are re-derivable. | Document backup and restore commands, which volumes matter, and `HF_HUB_OFFLINE=1` after the cache is warm. | Run the documented backup+restore on a smoke stack and compare `setup/status` counts. |
| A7-020 | low | scripts/smoke.sh | Smoke has 9 positive checks (3 s of the 77 s job) but does not cover `/metrics`, restart persistence, graceful stop, entrypoint FATALs (verified manually once per deploy.md:78-81, not automated), auth rejection (no key on /mcp or admin API), or the inference image. Fixed `/tmp/mcpr-smoke-route.json` path. | Extend per M4. | n/a |
| A7-021 | low | docs/deploy.md:59-81; CHANGELOG.md; entrypoint:49 | Docs/process: deploy.md carries a host-specific `--network=host` workaround and a dated transcript that will go stale; CHANGELOG has no dates and no Unreleased section; no deploy.md section on logging, metrics, downgrade, resource limits or reverse proxy/TLS (uvicorn runs without `--proxy-headers`/`--forwarded-allow-ips`, so behind a proxy the client IP is the proxy's). | Add the sections; keep an Unreleased heading. | CI: CHANGELOG has a heading for the pyproject version. |

Positive (verified): non-root uid 1000 with volumes pre-chowned (Dockerfile:35-39); loud writability probe; bounded DB wait that logs only the
exception type (entrypoint:42, no DSN leak); `exec` keeps uvicorn as PID 1; db not published, dev override isolated; every Action SHA-pinned with
a version comment; least-privilege `permissions`; `persist-credentials: false`; concurrency cancels PR runs only; ci-gate checks each result;
ephemeral masked smoke credentials; `provenance: mode=max` + `sbom: true` on the base image.

## Single-worker enforcement

The entrypoint hard-codes `--workers 1` (entrypoint:49-50); an explicit CLI flag beats `WEB_CONCURRENCY` (believed). It is NOT enforced when a
command is passed (`if [ "$#" -gt 0 ]; then exec "$@"`, line 48, e.g. `docker run image gunicorn -w 4 ...`) or when someone runs uvicorn with
`--workers N` outside Docker. With N workers: N SyncLoops duplicating connector syncs, N model loads, per-process rate limiter and exposure
sets (app.py:18). Nothing detects it. Fix: loop-owner takes `pg_try_advisory_lock`; non-owners skip the loops and log a FATAL-level warning.
Test: two app instances on one DB, assert exactly one holds the loops.

## Startup/shutdown ordering diagram (verified: entrypoint.sh, app.py, db.py, gateway/server.py:387-396)

```
docker start
 v
entrypoint.sh (sh, becomes uvicorn via exec)
 1. MCPR_DATABASE_URL set?                no -> FATAL exit 1
 2. MCPR_ADMIN_TOKEN set?                 no -> WARNING only            (A7-002)
 3. /data, skills-cache, models-cache: mkdir + write probe   fail -> FATAL exit 1
 4. python: SELECT 1, up to 30 x 2 s      fail -> FATAL exit 1          (no DDL yet)
 5. exec uvicorn --factory create_app --workers 1        (uvicorn is PID 1)
 v
uvicorn imports mcprouter.api.app and CALLS create_app()   [before the socket listens, before lifespan]
 a. Settings.from_env(); quiet httpx loggers; InferenceEngine CONSTRUCTED, not loaded
 b. make_engine; init_db: CREATE EXTENSION vector -> create_all x4 metadata -> ~25 additive ALTER/UPDATE   (A7-007)
 c. init_registry (FTS + dedup indexes); ensure_skill_keyword_index
 d. configure_security (env principals seeded; dev-mode warning)
 e. routers, pipeline, ExecutionManager, SkillExposure, build_gateway(/mcp), /healthz, /metrics, SPA 404 handler, BodySizeLimit
    any exception in a-e -> raw traceback, non-zero exit, restart loop (no curated message, e.g. no privilege for CREATE EXTENSION)
 v
ASGI lifespan startup   (gateway wrapper: session_manager.run() OUTER, our lifespan INNER)
 1. MCP StreamableHTTP session manager starts
 2. inference.load() in a worker thread (download/load; degrades bge->hash-v1, laya->deterministic on ModelUnavailableError)
 3. if MCPR_SYNC_ENABLED (default false, and unreachable from compose, A7-001): SyncLoop.start()
 4. if MCPR_ANALYTICS_ROLLUP_ENABLED (same): RollupLoop.start()
 5. yield -> port binds ONLY NOW; HEALTHCHECK can first pass (start-period 20 s, 300 s inference)
 ... serving ...
SIGTERM -> uvicorn stops accepting, waits for in-flight requests (NO deadline, A7-009)
 v
lifespan shutdown (reverse)
 1. RollupLoop.stop()      cancel + await (thread-bound work can delay)
 2. SyncLoop.stop()
 3. inference.unload()     worker thread
 4. gateway session_manager exits (MCP streams closed)
 5. engine.dispose() is NOT called (process exit; harmless)
 Docker SIGKILL at 10 s if still alive (default) -> steps 1-4 skipped
```

Gaps: DDL runs in the factory, before model load, outside any lock. The port is not bound until model load ends, which is good for
readiness but a 5-minute first-boot download looks like "connection refused". A Postgres outage after start surfaces as `/healthz` 500
(`pool_pre_ping` recovers connections afterwards).

## CI matrix (`gh run list/view`, last 10 master runs, all push-triggered)

| job | triggers | runner / timeout | duration (last runs) | caching | gaps |
|---|---|---|---|---|---|
| backend (ruff, mypy, pytest) | push master/main, every PR | ubuntu-latest, 30 min | 139-174 s; pytest 80-121 s (rising); pgvector service init 20 s | setup-python `cache: pip` keyed on pyproject only; transitive deps unpinned; uv.lock untracked | Python 3.12 only; no coverage; no pytest timeout; inference extra untested; `ubuntu-latest` floats |
| ui (build, lint, test) | same | ubuntu-latest, 20 min | 48-77 s (build 20 s, test 26 s) | `cache: npm` with lockfile path | Node 20 vs Docker 22 (A7-011); 2 of last 8 failed (A7-013) |
| compose-smoke | same | ubuntu-latest, 25 min | 77 s (build+up 70 s, smoke 3 s) | none | zero-ML flavor only; no inference build; no shutdown/restart/metrics/FATAL checks |
| ci-gate | `if: always()` | ubuntu-latest, 5 min | 3-9 s | n/a | correct (asserts each result == success). Confirm branch protection names `ci-gate` (a settings item, not visible from the repo) |
| release / image | tag `v*` | 40 min | NEVER RUN | none | not gated on CI; no version check; no scan; no cosign; amd64 only (A7-004) |
| release / image-inference | `needs: image` | 60 min, continue-on-error | NEVER RUN | none | failure invisible; no sbom/provenance (A7-014) |
| security scan | absent | - | - | - | CodeQL, trivy fs+image, gitleaks, pip-audit, npm audit, dependency-review, SBOM (A7-005) |
| dependabot | absent | - | - | - | pip, npm, docker, github-actions (A7-005) |

Wall clock per push is about 3 min, dominated by backend. Last 10 runs: 8 success, 2 failure (both `npm test`). Not found: retry policy,
matrix, path filters, docker layer cache, image signing (cosign), image vulnerability scan. SBOM and provenance exist for the base image only.

### Dependabot shape to add (`.github/dependabot.yml`)

```yaml
version: 2
updates:
  - {package-ecosystem: pip, directory: "/", schedule: {interval: weekly}, open-pull-requests-limit: 5,
     groups: {python-minor-patch: {update-types: [minor, patch]}}}
  - {package-ecosystem: npm, directory: "/ui", schedule: {interval: weekly}, open-pull-requests-limit: 5,
     groups: {ui-minor-patch: {update-types: [minor, patch]}}}
  - {package-ecosystem: docker, directory: "/", schedule: {interval: weekly}}   # Dockerfile + Dockerfile.inference
  - {package-ecosystem: github-actions, directory: "/", schedule: {interval: weekly},
     groups: {actions: {patterns: ["*"]}}}
```

Majors stay ungrouped so each is a deliberate PR; add `ignore` for the `mcp` major (memory note: pinned shut). The compose-file image
`pgvector/pgvector:pg16` is not covered by the docker ecosystem; track manually.

### Security workflow shape (`.github/workflows/security-scan.yml`)

Triggers: push/PR to master plus weekly cron at an odd minute. Top-level `permissions: contents: read`; grant `security-events: write`
per job only. Jobs: codeql (python, javascript-typescript); pip-audit and `npm audit --audit-level=high` with `continue-on-error: true`
(advisory); dependency-review (PR only, `fail-on-severity: high`, the one gate); trivy fs and trivy image of the built base image
(SARIF); gitleaks with `fetch-depth: 0` reusing `.gitleaks.toml`; SBOM artifact. Pin all actions by SHA. Verify by name:
`gh run list --workflow "Security scan" --limit 1`.

## Ops meta-test proposal

**M1. Entrypoint misconfiguration harness** (`tests/ops/`, `docker run --rm` against the built image, about 20 s; only the last two cases
need Postgres). Each case asserts exit code and a stderr substring:

| case | setup | expect |
|---|---|---|
| no DB URL | no env | exit 1, `FATAL: MCPR_DATABASE_URL is required` |
| unreachable DB | `MCPR_DATABASE_URL=...@nowhere:5432/x MCPR_DB_WAIT_TRIES=2` | exit 1, `waiting for database (2/2)`, then `FATAL: Postgres not reachable`; the DSN password must not appear in output |
| unwritable dir | root-owned bind mount at /data, then skills-cache, then models-cache | exit 1, `FATAL: <dir> is not writable by uid 1000` |
| open dev mode | valid DB, no token, no keys | after A7-002: exit 1 unless `MCPR_ALLOW_OPEN_DEV=1`; today: WARNING present |
| pass-through | `... python -c 'print(1)'` | prints 1; guards still ran first |
| worker count | read `/proc/1/cmdline` in the running container | contains `--workers 1`; uid != 0 |

**M2. Compose/env contract test** (pure, `docker compose config` + python): every `MCPR_*` read in settings.py or listed in `.env.example`
is forwarded to `api.environment` (or `env_file` is present) or is on an explicit "not forwarded" allowlist; every forwarded name is read by
settings.py (catches typos); `db` has no `ports`; base+inference+gpu merge parses. Would have caught A7-001.

**M3. Version single-source test** (backend job): `pyproject.version == mcprouter.__version__ == FastAPI.version == gateway server version ==
ui/package.json version`, and CHANGELOG has a heading for it. Better: derive the Python ones from `importlib.metadata.version("mcprouter")`.

**M4. Lifecycle smoke additions** (extend scripts/smoke.sh): (a) `/metrics` contains `mcpr_analytics_` (401 without credentials after A7-006);
(b) 401/403 without key on `/mcp` and on the admin API; (c) create a principal, `compose restart api`, assert it persists; (d) hold an MCP
stream open, `docker stop -t 30`, exit 0 and shutdown log within 25 s; (e) stop the db container, `/healthz` goes 500 and the container turns
`unhealthy`, then recovers when db returns.

**M5. Release consistency job** in release.yml, as `needs` of both image jobs:

```
v="${GITHUB_REF_NAME#v}"
test "$v" = "$(python -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
grep -q "^## ${v%%-*}\$" CHANGELOG.md
test "$(gh run list --workflow CI --commit "$GITHUB_SHA" --json conclusion -q '.[0].conclusion')" = success
```

Mutation-check it: push `v9.9.9-rc.0` on a throwaway fork, confirm the job fails and no image is pushed. After a real push, run
`docker buildx imagetools inspect` and assert provenance and SBOM attestations; add `cosign verify` once signing exists.

**M6. Upgrade/downgrade rehearsal** (nightly): start the previous release image on a seeded DB, then the new image on the same volume,
assert data and `setup/status` unchanged; start the OLD image on the migrated DB and assert it serves or fails with the A7-008 "database
newer than code" FATAL (decide which and document it). Time the second start with 1M rows in `execution_records` to catch A7-007.

## Noticed (outside scope, not acted on)

- `uv.lock` is untracked while Dockerfile and CI install via pip from pyproject; decide whether the lockfile is authoritative (then `uv sync --frozen` in both) or delete it.
- The `mcpr-data` volume (/data) appears unused by the app (no setting references it; only the entrypoint write probe); confirm before documenting it as state.
- metrics.py docstring (lines ~23-27) says "nothing schedules rollups yet" though RollupLoop now exists; stale comment.
