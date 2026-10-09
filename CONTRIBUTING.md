# Contributing to MCP Router

This guide is for people. `CLAUDE.md` holds the extra rules for AI agents
working in this repository; both apply.

## 1. Prerequisites

- Python 3.12. Always use the project venv (`.venv/bin/python`, `.venv/bin/ruff`,
  `.venv/bin/mypy`). A global `ruff` or `mypy` is a different version and
  reports different results.
- Node 22 (the exact version is in `.nvmrc`; the Docker image and CI use it).
- Docker with Compose v2 (`docker compose`, with `--wait`).
- [uv](https://docs.astral.sh/uv/) is recommended; plain `venv` + `pip` works too.

## 2. First run

From the repository root:

```bash
make setup     # .venv with the [dev] extra + ui/node_modules
make db-up     # Postgres + pgvector on 127.0.0.1:5434 (dev override)
make check     # every gate below
```

Without make:

```bash
uv venv --python 3.12 .venv && uv pip install -e '.[dev]'
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --wait db
cd ui && npm ci && cd ..
```

## 3. The gates

All of these must pass before a change is done. CI runs the same commands.

| Gate | Command |
|---|---|
| Lint | `.venv/bin/ruff check .` |
| Format | `.venv/bin/ruff format --check .` |
| Types | `.venv/bin/mypy` (strict) |
| Backend tests | `MCPR_REQUIRE_DB=1 .venv/bin/pytest tests/ -q` |
| UI | `cd ui && npm run lint && npx tsc --noEmit && npx vitest run && npm run build` |
| Generated docs | `.venv/bin/python scripts/gen_config_docs.py --check` |
| Compose smoke | `make smoke` |

`make check` runs the first six; `make smoke` builds the image and runs
`scripts/smoke.sh` against a throwaway compose project.

## 4. The test database

Tests that touch the database are marked `requires_db`. They need the dev
database from `make db-up` (port 5434). CI uses its own Postgres service on
5432 through `MCPR_DATABASE_URL`.

**When the database is down, those tests are skipped, not failed.** A run can
look green with hundreds of tests skipped. Guard against it:

- set `MCPR_REQUIRE_DB=1` (from v0.6) so a missing database fails the run;
- read the skip reasons with `pytest -rs`.

## 5. Faster loops

```bash
make test-fast                                              # unit lane
.venv/bin/pytest tests/ -q -m "not e2e and not slow and not scale"
.venv/bin/pytest tests/test_policy_engine.py -q            # one file
make test-full                                              # DB + slow lane (what nightly runs)
```

The `e2e`, `slow` and `scale` markers (from v0.6) are registered with
`--strict-markers`, so a typo in a marker name is a collection error.
`MCPR_RUN_SLOW=1` enables the slow and live-model tests.

## 6. Optional ML lane

```bash
uv pip install -e '.[dev,inference]'
MCPR_RUN_SLOW=1 HF_HUB_OFFLINE=1 .venv/bin/pytest tests/test_inference_bge.py -q
```

The `[inference]` extra (torch, transformers, sentence-transformers, laya) is
heavy and optional. Everything must still work without it: the hash embedder
and the deterministic decision backend are the zero-ML path CI tests.
`HF_HUB_OFFLINE=1` makes model loads use the local cache only.

## 7. UI work

```bash
cd ui
npm ci
npm run dev        # http://localhost:5180, proxies /api and /mcp to :8400
npx vitest run     # tests
npm run lint
```

The production dashboard is the `npm run build` output in `ui/dist`, served by
the API at `/`.

## 8. Ports and test isolation

New tests never hard-code a port. Use `free_port()` and `run_app()` from
`tests/support/` and wait on readiness with `wait_for()`, not `sleep`. Shared
fixtures live in `tests/support/`; never import from another `test_*.py` file.

## 9. Regenerating docs

`docs/reference/configuration.md` is generated from `src/mcprouter/settings.py`:

```bash
make docs-gen      # or: .venv/bin/python scripts/gen_config_docs.py
```

Run it after adding or changing a setting, and commit the result.
`tests/test_docs_consistency.py` fails when the file is stale, a doc link or
path is broken, a documented `MCPR_*` variable does not exist, a documented
default differs from the code, or a documented CLI flag is gone.

## 10. Commits and pull requests

- One logical change per commit. Never mix a refactor with a behaviour change.
- Message: `type: summary` (`feat`, `fix`, `docs`, `test`, `build`, `chore`,
  `refactor`), then the why in the body.
- Security fixes come with a test that fails when the guard is removed. Check
  it: remove the guard, see the named test fail, restore.
- A pull request must pass the `ci-gate` job.

## 11. Releases

Releases are cut by tagging `vX.Y.Z` on a commit with a green `CI` run on
`master`. From v0.6 the release workflow verifies the tag against
`pyproject.toml` and the top `CHANGELOG.md` heading before it publishes the
images. Update `CHANGELOG.md` (move the Unreleased entries under the new
version) in the same pull request as the version bump.

## 12. Security reports

Do not open a public issue for a vulnerability. Use the repository's
**Security → Report a vulnerability** form.
