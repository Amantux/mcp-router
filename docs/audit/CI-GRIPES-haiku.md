# CI / developer-friction gripes (Haiku investigator)

Scope: what a contributor or operator hits running, testing and shipping this repo.
Read-only pass. Only this file was written. Nothing was started with `docker compose up`,
and no full pytest or npm test run was done.

Legend: **verified** = I ran or read it in this pass. **believed** = read from code, not run.

## Findings

| id | severity | area | evidence | gripe | proposed fix | how to verify |
|---|---|---|---|---|---|---|
| CG-H-001 | high | tests/db | Ran `MCPR_DATABASE_URL=...:59999 pytest tests/test_skills_api.py::test_api_flow tests/test_dedup.py` (verified): 3 passed, 12 skipped, **1 error** (OperationalError). `tests/test_skills_api.py:23` takes conftest `db` fixture with no `requires_db`. conftest `db` calls `init_db` unconditionally. AST scan: 17 test functions use `db`/`world` without a gate (e.g. test_gateway_skills.py, test_skills_routing_classify.py). | Without a DB, gated tests skip quietly but ungated ones ERROR. A contributor sees a red run with no DB. | Gate those 17 with `requires_db`, or make the `db` fixture call `pytest.skip(...)` when `_db_available()` is false. | Repeat the run above with the bogus port. Expect 0 ERROR after the fix. A `db` mutation check: remove the skip and the test must error. |
| CG-H-002 | high | tooling/ruff | `ruff --version` on PATH (`/usr/local/bin/ruff`) = **0.16.0**. Pinned `pyproject.toml` `ruff==0.15.22`. `ruff format --check .` on PATH fails on `docs/INTEGRATION_NOTES-*.md` (verified). `.venv/bin/ruff format --check .` = 241 files formatted, exit 0 (verified). | The first gate fails for anyone who uses the global ruff. Ruff 0.16 also formats Markdown. | Run only `.venv/bin/ruff` in make targets. Add `ruff format` `exclude` for `*.md` or pin via `uv run`. The Makefile below uses `$(VENV)/bin/ruff`. | `ruff --version` on PATH vs `.venv/bin/ruff --version`. Run both `format --check`. |
| CG-H-003 | high | tooling/python | `mypy` on PATH fails: "Invalid syntax; you likely need Python 3.12" at `src/mcprouter/inference/engine.py:88` (verified). `.venv/bin/mypy` = "Success: no issues found in 120 source files" (verified). CLAUDE.md warns about system python 3.10. `tests/__pycache__/conftest.cpython-310.pyc` exists, so 3.10 has already been used here. | No documented interpreter. The default `mypy` produces a syntax error that looks like a code bug. | Makefile pins `VENV=.venv` and uses `.venv/bin/*`. `setup` checks `python3.12`. | `which mypy && mypy` fails. `.venv/bin/mypy` passes. |
| CG-H-004 | high | reproducibility | `git status` shows `?? uv.lock` (verified). `git ls-files` has no `uv.lock`, only `ui/package-lock.json`. CI runs `pip install -e '.[dev]'` (`ci.yml` backend job). Dockerfile runs `pip install .`. Transitive deps resolve fresh each time (believed). | The lockfile exists on disk but is not committed. Transitive dependencies can differ between CI, Docker and local, and CI can break with no code change. | Commit `uv.lock`. Use `uv sync --frozen --extra dev` in CI and the Makefile, and `uv export` or `uv pip install` in the Dockerfile. | `git ls-files uv.lock` is non-empty. A CI `uv sync --locked` step passes. |
| CG-H-005 | medium | env parity | Local `.venv` imports `torch` (2.14.1+cpu) and `sentence_transformers` without error (verified). CI installs only `.[dev]` (`ci.yml`). `tests/test_inference_bge.py` uses `pytest.importorskip("sentence_transformers")` and `("torch")`. Local and CI run different test sets. | Inference tests run on a dev box and skip silently in CI. A regression can pass CI. | Keep the zero-ML CI lane, and add a nightly or manual lane with `.[dev,inference]` and `MCPR_RUN_SLOW=1`. Print skip reasons (`-rs`) in CI. | `.venv/bin/python -c "import torch"` works. `pytest -rs` in CI lists the skipped inference tests. |
| CG-H-006 | high | smoke/operator | `scripts/smoke.sh` defaults `BASE=http://127.0.0.1:8450` and `COMPOSE="docker compose -p mcprsmoke"` (verified). `docker-compose.yml` defaults `MCPR_HOST_PORT` to 8400 and the project name to the directory name (verified). README step (`docker compose up -d --build --wait`) then `scripts/smoke.sh` fails with "no api container" or a curl error. Only `docs/deploy.md:62` says to set `MCPR_HOST_PORT=8450` and use `-p mcprsmoke`. | An operator who follows the README gets a smoke failure. The required settings live in a deploy doc, not at the script. | Make `smoke.sh` read `MCPR_HOST_PORT` (default 8400) from `.env`. Derive `COMPOSE` from `-p` only when set. Or add `make smoke` that sets both. | Run `docker compose up -d --wait` with a default `.env`, then `scripts/smoke.sh`. It should pass with no extra flags. |
| CG-H-007 | medium | smoke/secrets | `smoke.sh:9-10` require `MCPR_ADMIN_TOKEN` and `MCPR_AGENT_KEY` as **shell env**. Compose reads `.env`, but the script never does (verified). `MCPR_AGENT_KEY` is not in `.env.example` (verified). `smoke.sh:85` writes a fixed `/tmp/mcpr-smoke-route.json` (verified). | Two sources of truth for the same secrets. A failed local run leaves shared temp state. | Have `smoke.sh` source `.env` (dotenv parse, no `source`) and generate `MCPR_AGENT_KEY` from the first `MCPR_AGENT_KEYS` entry. Use `mktemp`. | Remove both env vars and run with only `.env`. Expect PASS. |
| CG-H-008 | high | ship/release | `release.yml` triggers on `push: tags: v*` with no `needs` on any test job (verified). The `image` job publishes `:latest` (verified). `ci.yml` runs on branches only. Tag protection and branch protection are not in the repo (believed; GitHub setting). | A tag on an untested or red commit publishes a `:latest` image. No smoke or test runs before publish. | Add a `verify` job that calls `ci.yml` (`workflow_call`) or re-runs backend + compose-smoke, with `needs: verify` on `image`. Or add a tag ruleset requiring a green `ci-gate` on the commit. | Tag a throwaway commit that fails a test. Confirm the release job does not start. |
| CG-H-009 | high | docs/onboarding | No `CONTRIBUTING.md`, `Makefile`, `justfile`, `SECURITY.md`, `.github/dependabot.yml` or `CODEOWNERS` (verified by `ls`). CLAUDE.md lists the gates (`ruff`, `mypy`, `pytest`), but it is an agent file. No test env var is documented outside `.env.example`. | A new contributor has to infer the gate list, DB setup, and env vars from CI YAML and CLAUDE.md. | Add `Makefile` (below) and `CONTRIBUTING.md` (outline below). Add `dependabot.yml` per the hygiene playbook. | `make check` works from a clean clone. `CONTRIBUTING.md` exists and the commands in it run. |
| CG-M-010 | medium | ci/tests | `ci.yml` backend: `pytest tests/ -q` with no `-rs`, and no `MCPR_RUN_SLOW` (verified). `MCPR_RUN_SLOW` appears only in `tests/` and `docs/` (verified). `test_routing_perf.py:40` skips without it. `test_backends_aoai.py:267` skips without it and `MCPR_AOAI_ENDPOINT`. | Routing-latency budgets and live-model tests never run in CI. Perf regressions can merge. | Add a nightly or `workflow_dispatch` job with `MCPR_RUN_SLOW=1` and an uploaded timing artifact. | `grep -rn MCPR_RUN_SLOW .github` should match after the fix. |
| CG-M-011 | medium | ci/conftest | `conftest.py:20` default `localhost:5434`. CI sets `MCPR_DATABASE_URL` to `5432` (verified). `_db_available()` runs at import and returns False on any `Exception` (verified). Skip reason is "compose db not running" (verified). Without `-rs`, the reason is hidden. | Wrong port, or a DB that is down, silently skips about 455 test functions. Only the count shows. | Print `TEST_DB_URL` and DB availability in `pytest_report_header`. Add `MCPR_REQUIRE_DB=1` in CI so a missing DB fails. Add `-rs` to `addopts`. | Set the URL to a dead port with `MCPR_REQUIRE_DB=1`. Expect a failing session, not a skip. |
| CG-M-012 | medium | ci/cost | `ci.yml`: no `paths:` filter (docs-only PRs run backend + compose-smoke, about 25 min). No buildx cache (`cache-from`/`cache-to`) in `compose-smoke` or `release.yml`. No `actions/upload-artifact` for test reports or failing logs (only `docker compose logs` on failure, verified). No `$GITHUB_STEP_SUMMARY`. No `pytest --junitxml`. No `vitest` reporter. | Slow PR loops and hard-to-read failures. Docs-only PRs spend compose time. | Add `paths-ignore: ['docs/**', '*.md']` to the compose job (not the required gate). Add `cache-from: type=gha` and `cache-to: type=gha,mode=max`. Upload `junit.xml` and `ui/test-results` on failure. | Check run time on a docs-only PR. Check the artifact list on a forced failure. |
| CG-M-013 | medium | ui/runtime | CI `ui` job: `node-version: "20"` (verified). `Dockerfile:2` `FROM node:22-slim` (verified). `ui/package.json` `engines.node >=20.19` (verified). No `.nvmrc` and no `packageManager` (verified). `engines` is not enforced by npm without `engine-strict`. | Three Node versions in play. The Docker build and CI can differ. A contributor on Node 18 gets a confusing Vite/Vitest error. | Add `ui/.nvmrc` with `22`. Set CI `node-version-file: ui/.nvmrc`. Set `engine-strict=true` in `ui/.npmrc`. Update the Dockerfile to match. | `node -v` matches `.nvmrc`. CI log shows the same major as the Docker stage. |
| CG-M-014 | medium | tests/ports | Fixed ports in tests (grep, verified): `test_discovery_connector.py` 8602, 8605, 8609; `test_discovery_import.py` and `test_testbed*.py` 8600-8603; `test_gateway_mcp.py:55` 8641; `test_e2e_integration.py` 8700, 8710; `test_gateway_skills_e2e.py:35` 8803; `test_e2e_skills.py` 8820, 8821. Shared DB: `conftest.py` `_CLEAN_TABLES` runs `DELETE FROM` on every test (verified). No xdist and no `pytest-timeout` in `dev` extras (verified). | Two `pytest` processes on one box collide. The second one gets EADDRINUSE or talks to the wrong uvicorn. Shared DB rows get wiped mid-run by the other process. 8609 assumes "nothing listens here" (believed fragile on a busy dev box). | For uvicorn tests, bind port 0 and read the bound port. Use one DB per run (`CREATE DATABASE` by a run-id suffix), or a schema per run. Add a comment on the dead-port assumption. | Run `pytest tests/test_gateway_mcp.py` twice in parallel. Expect one to fail on bind today, and both to pass after. |
| CG-M-015 | medium | tests/silent-pass | `tests/test_backends_remote.py:683-695`: `test_hostile_content_length_is_typed` wraps the call in `try: ... except (RemoteResponseError, RemoteDecisionError): pass` and has no `assert` (verified). A normal return also passes. `tests/test_setup_routes.py:76`: on 403 the test does `return` and passes with no assert (verified). `tests/test_eval_framework.py:162` `test_eval_table_created_idempotently` has no assert (verified, weak but intentional). | These tests pass without proving the guard. The security rule in CLAUDE.md says a security test must fail when the guard is removed. | Assert the exception type in the `except` (use `pytest.raises` with a `match`). For setup, split into two tests: dev-mode 200 and locked 403, both asserted. Add a mutation check for the content-length guard. | Remove the guard (e.g. return 200 always), run the test. It must fail. |
| CG-L-016 | low | ui/tests | `ui/vitest.config.ts` has no `testTimeout`, `hookTimeout`, `pool` or `fileParallelism` (verified). `ui/src/test/setup.ts` stubs only `ResizeObserver` (verified). `matchMedia` is not stubbed, but `ui/src/hooks/useColorScheme.ts` guards for it (verified). Fluent `react-motion` uses `.animate(` and `matchMedia` in its hooks (believed; not run). `npm test` = `vitest run`, no coverage, no reporter (verified). | A Fluent component that hits a missing jsdom API will throw in a hook, and the failure may look like an app bug. Default 5 s timeouts can flake on a loaded CI runner. | Add `testTimeout: 15000`. Stub `matchMedia` and `Element.prototype.animate` in `setup.ts` only when a test hits them. Add `--reporter=default --reporter=junit` in CI. | Delete the `ResizeObserver` stub and run `npm test` once. The first error shows the jsdom gap. |
| CG-L-017 | low | docs | `README.md:133-152` documents `.env` setup and `npm ci && npm run build` but not `npm test` or `npm run lint` (verified). `docs/deploy.md` has the smoke procedure. `bench/` has no README entry (verified by grep). | The UI gates and bench harness are undiscoverable. | Put every command in `CONTRIBUTING.md`. Link it from README. | `grep -n "npm test" CONTRIBUTING.md` matches. |

## Gates a contributor must run to reproduce CI

Today, to match `ci.yml` exactly, a contributor runs **9 commands** in 3 directories:

1. `python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'`
2. `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --wait db`
3. `export MCPR_DATABASE_URL=postgresql+psycopg://mcprouter:mcprouter@localhost:5434/mcprouter`
4. `.venv/bin/ruff check . && .venv/bin/ruff format --check .`
5. `.venv/bin/mypy`
6. `.venv/bin/pytest tests/ -q -rs`
7. `cd ui && npm ci && npm run build && npm run lint && npm test`
8. Compose smoke: generate `MCPR_ADMIN_TOKEN` and `MCPR_AGENT_KEY`, write `.env`, `MCPR_HOST_PORT=8450 docker compose -p mcprsmoke up -d --build --wait`, then `scripts/smoke.sh`, then `docker compose -p mcprsmoke down -v`.
9. `docs/` gates: none. Note that ruff 0.16 on PATH would reformat the Markdown (CG-H-002).

There is **no** `Makefile`, `justfile`, `make check`, or `CONTRIBUTING.md`.

## Proposed Makefile

```make
# Makefile - mirrors .github/workflows/ci.yml. Use .venv tools only (CG-H-002/003).
VENV ?= .venv
PY   := $(VENV)/bin/python
export MCPR_DATABASE_URL ?= postgresql+psycopg://mcprouter:mcprouter@localhost:5434/mcprouter
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml
SMOKE_PORT ?= 8450

.PHONY: setup db-up check test-fast test-full ui smoke docs-gen

setup:  ## venv + python deps + ui deps (needs python3.12, node>=20.19)
	python3.12 -m venv $(VENV)
	$(VENV)/bin/pip install -e '.[dev]'
	cd ui && npm ci

db-up:  ## Postgres+pgvector on 127.0.0.1:5434 (dev override)
	$(COMPOSE_DEV) up -d --wait db

check: setup-check
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .
	$(VENV)/bin/mypy
	MCPR_REQUIRE_DB=1 $(VENV)/bin/pytest tests/ -q -rs
	$(MAKE) ui

setup-check:
	@test -x $(VENV)/bin/pytest || { echo "run: make setup"; exit 1; }

test-fast:  ## no DB, no slow: zero-ML unit lane (needs the marker from CG-H-001)
	$(VENV)/bin/pytest tests/ -q -rs -m "not slow and not db"

test-full:  ## DB + slow lane (what a nightly runs)
	$(MAKE) db-up
	MCPR_RUN_SLOW=1 MCPR_REQUIRE_DB=1 $(VENV)/bin/pytest tests/ -q -rs

ui:  ## build + lint + vitest (mirrors the ui job)
	cd ui && npm ci && npm run build && npm run lint && npm test

smoke:  ## compose stack + scripts/smoke.sh (needs docker; uses project mcprsmoke)
	@test -f .env || { echo "cp .env.example .env and set MCPR_ADMIN_TOKEN/MCPR_AGENT_KEYS"; exit 1; }
	MCPR_HOST_PORT=$(SMOKE_PORT) docker compose -p mcprsmoke up -d --build --wait --wait-timeout 180
	BASE=http://127.0.0.1:$(SMOKE_PORT) scripts/smoke.sh; st=$$?; \
	  docker compose -p mcprsmoke down -v; exit $$st

docs-gen:  ## regenerate docs/audit/openapi-routes.json (needs scripts/gen_openapi.py, which does not exist yet)
	$(PY) scripts/gen_openapi.py > docs/audit/openapi-routes.json
```

Notes: `test-fast` needs a `db` marker, which conftest does not register today. `docs-gen` names a script that does not exist (no generator found by grep). Add it or drop the target.

## Proposed CONTRIBUTING.md outline

1. **Prerequisites**: Python 3.12, Node >= 20.19 (`ui/.nvmrc`), Docker Compose v2, uv (optional).
2. **First run**: `make setup`, `make db-up`, `make check`. Expected duration and what "green" means.
3. **The gates** (table): ruff check, ruff format, mypy, pytest (with DB), ui build/lint/test, compose smoke. One line each, and which CI job runs it.
4. **Test database**: why `requires_db` exists; port 5434 (dev override) vs CI 5432; `MCPR_DATABASE_URL`; `MCPR_REQUIRE_DB=1`; how to read skip reasons (`-rs`).
5. **Environment variables** for tests and runs: `MCPR_DATABASE_URL`, `MCPR_RUN_SLOW`, `MCPR_REQUIRE_DB`, `MCPR_ADMIN_TOKEN`, `MCPR_AGENT_KEYS`, `MCPR_AOAI_*` (live tests), `HF_HOME` (bge tests), `MCPR_HOST_PORT`. Point to `.env.example` and `docs/deploy.md` for the rest.
6. **Optional ML lane**: `.[dev,inference]`, `MCPR_RUN_SLOW=1`, `HF_HUB_OFFLINE=1`. Not in CI.
7. **Compose smoke**: prerequisites, `make smoke`, what it checks, port 8450 vs 8400.
8. **UI**: Node version, `npm ci`, dev server on 5180 proxying 8400, test and lint commands.
9. **Ports used by tests**: list from CG-M-014, and the rule that new tests bind port 0.
10. **Commits and PRs**: one logical change per commit; trailers from CLAUDE.md; PR must pass `ci-gate`.
11. **Release**: tag `vX.Y.Z` on a green `main` commit; `release.yml` publishes `:tag`, `:latest` and `-inference`. Note CG-H-008 until fixed.
12. **Security reports**: private advisory path (needs `SECURITY.md`, which does not exist yet).

## Noticed (out of scope)

- `testbed/` and `bench/` have no CI job and no README entry. Run instructions live only in `docs/hardware-validation.md`.
- `tests/__pycache__/conftest.cpython-310.pyc` is a stale artifact from a 3.10 run. `.gitignore` covers it, so it is harmless.
