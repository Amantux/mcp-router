# Makefile - mirrors .github/workflows/ci.yml. Uses the project venv only
# (a global ruff/mypy/python3.10 gives different answers; CG-H-002/003).
VENV ?= .venv
BIN  := $(VENV)/bin
PY   := $(BIN)/python
# Names the Postgres SERVER: tests run on <db>_test_template/<db>_test_w<N>
# clones and never touch the database named here (tests/support/db.py).
export MCPR_DATABASE_URL ?= postgresql+psycopg://mcprouter:mcprouter@localhost:5434/mcprouter
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml
SMOKE_PORT  ?= 8450
SMOKE       := docker compose -p mcprsmoke

.PHONY: help setup setup-check db-up check lint type test-fast test-full ui smoke docs-gen

help:  ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

setup:  ## locked venv (python 3.12 + dev extra) and ui deps (node from .nvmrc)
	uv sync --locked --extra dev --python 3.12
	cd ui && npm ci

setup-check:
	@test -x $(BIN)/pytest || { echo "run: make setup"; exit 1; }

db-up:  ## Postgres+pgvector on 127.0.0.1:5434 (dev override)
	$(COMPOSE_DEV) up -d --wait db

lint: setup-check  ## ruff check + format check + lockfile check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	uv lock --check

type: setup-check  ## mypy: src+bench strict, tests+testbed <= mypy-baseline.txt
	$(PY) .github/scripts/mypy_baseline.py

test-fast: setup-check  ## the CI unit lane (needs db-up): no e2e/slow/scale, parallel
	MCPR_REQUIRE_DB=1 MCPR_ENFORCE_ROUTE_COVERAGE=1 $(BIN)/pytest tests/ -q -n auto -m "not e2e and not slow and not scale"

test-full: setup-check  ## everything incl. e2e + scale; slow ones need MCPR_RUN_SLOW=1
	MCPR_REQUIRE_DB=1 MCPR_ENFORCE_ROUTE_COVERAGE=1 $(BIN)/pytest tests/ -q -n auto --dist loadgroup

check: lint type test-full ui  ## every gate CI runs except compose smoke

ui:  ## build + lint + vitest (mirrors the ui job)
	cd ui && npm ci && npm run build && npm run check:bundle && npm run lint && npm test

smoke:  ## build + compose stack on :$(SMOKE_PORT) + scripts/smoke.sh, then tear down
	@test -f .env || { echo "cp .env.example .env and set MCPR_ADMIN_TOKEN, MCPR_AGENT_KEYS=smoke:<key>, POSTGRES_PASSWORD"; exit 1; }
	MCPR_HOST_PORT=$(SMOKE_PORT) $(SMOKE) up -d --build --wait --wait-timeout 180
	BASE=http://127.0.0.1:$(SMOKE_PORT) COMPOSE="$(SMOKE)" scripts/smoke.sh; st=$$?; \
	  $(SMOKE) down -v; exit $$st

docs-gen: setup-check  ## run every generator in scripts/gen_*.py (generated docs, E7)
	@set -e; found=0; for g in scripts/gen_*.py; do \
	  [ -e "$$g" ] || continue; found=1; echo "$(PY) $$g"; $(PY) "$$g"; done; \
	  [ "$$found" = 1 ] || echo "no scripts/gen_*.py generators yet"
