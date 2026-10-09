# Command-line reference

Every command runs from the repository root with the project venv
(`.venv/bin/python`, never the system Python). Flags below are checked against
each module's `--help` by `tests/test_docs_consistency.py`.

## Server

```bash
.venv/bin/uvicorn --factory mcprouter.api.app:create_app --port 8400
```

Run **one** worker. The process owns the sync and rollup loops, the in-process
model, rate limits and MCP sessions. Configuration comes from the environment:
[configuration.md](configuration.md).

Schema migrations (from v0.6) run automatically at startup. To run them by hand
(for example before switching traffic to a new image), use
`python -m mcprouter.migrate`. See [upgrade.md](../upgrade.md).

## Synthetic fleet (`testbed`)

| Command | What it does |
|---|---|
| `.venv/bin/python -m testbed.fleet --servers 10 --tools 40` | Print the deterministic fleet manifest (tools with ground-truth domain, operation and canonical id). |
| `.venv/bin/python -m testbed.serve --servers 10 --port-base 8600 --ports 1 --host 127.0.0.1` | Serve the fleet over streamable HTTP, round-robin over `--ports` consecutive ports from `--port-base`, each server at `http://HOST:PORT/<name>/mcp`. Prints one `READY {name: url}` line and runs until Ctrl-C. |
| `.venv/bin/python -m testbed.seed --servers 10 --tools 40` | Register and discover the fleet straight into the database (`--database-url`, default `MCPR_DATABASE_URL`). The default `--transport inproc` needs no network; its rows carry placeholder endpoints, so they route but cannot execute. `--transport http` starts `testbed.serve` for the duration of the run (`--port-base`, `--ports`); `--concurrency` bounds parallel discovery. |
| `.venv/bin/python -m testbed.stdio_server --servers 10 --index 0` | Run one fleet server over stdio (what a registered stdio server executes). `--spec-file` loads a manifest instead. |
| `.venv/bin/python -m testbed.skills.generate --out /tmp/skills --skills 40 --seed 7` | Write a deterministic Agent Skills tree. `--include-invalid` adds spec-violating cases; `--as-git` makes it a git repository. |

Try-it recipes, with the router running against the same database:

```bash
# A routable catalog in seconds (route and inspect; tools do not execute):
.venv/bin/python -m testbed.seed --servers 10 --tools 40

# Live servers you can execute: serve them, then register one URL per server.
.venv/bin/python -m testbed.serve --servers 3 --port-base 8600   # prints READY {...}
curl -s -X POST localhost:8400/api/v1/servers -H "Authorization: Bearer $MCPR_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"<name>","transport":"streamable-http","endpoint":"<url from READY>"}'
```


## Benchmarks (`bench`)

```bash
.venv/bin/python -m bench.routing_latency --embedding hash --decision deterministic \
  --device auto --mode balanced --iterations 30 --warmup 5 --out bench/results/
```

Flags: `--embedding {hash,bge}`, `--decision {deterministic,laya}`, `--device`,
`--mode {performance,balanced,battery}`, `--iterations`, `--warmup`,
`--batch-sizes`, `--cache-dir`, `--threads`, `--parity` (CUDA only), `--out`.
The GPU runbook is [hardware-validation.md](../hardware-validation.md).

## Scripts

| Script | What it does |
|---|---|
| `scripts/docker-entrypoint.sh` | Container entrypoint: validates the environment, checks the writable directories, waits for Postgres, then starts one uvicorn worker. Arguments, if given, run instead of uvicorn. |
| `scripts/smoke.sh` | End-to-end check of a running compose stack. Needs `MCPR_ADMIN_TOKEN` and `MCPR_AGENT_KEY`; `BASE` and `COMPOSE` default to the CI project (`http://127.0.0.1:8450`, `docker compose -p mcprsmoke`). |
| `scripts/gen_config_docs.py` | Regenerates [configuration.md](configuration.md); `--check` exits 1 when it is stale. |

Against the README stack (default port, default project name):

```bash
BASE=http://127.0.0.1:8400 COMPOSE="docker compose" \
  MCPR_ADMIN_TOKEN=... MCPR_AGENT_KEY=... scripts/smoke.sh
```

## Make targets

The `Makefile` wraps the commands above: `setup`, `db-up`, `check`,
`test-fast`, `test-full`, `ui`, `smoke`, `docs-gen`. See
[CONTRIBUTING.md](../../CONTRIBUTING.md).
