# Deploying MCP Router with Docker

## Quickstart

```bash
cp .env.example .env
# edit .env: set MCPR_ADMIN_TOKEN and POSTGRES_PASSWORD (openssl rand -hex 32 for each)
docker compose up -d --build --wait
# then browse to http://localhost:8400/  (API at /api/v1, MCP at /mcp)
```

The `db` service publishes **no host port**; only `api` is reachable. The
README has the full five-step [quickstart](../README.md#run-it-docker).

## Environment

Compose reads `.env` next to `docker-compose.yml`. Every variable, with its
default, is in the generated [configuration reference](reference/configuration.md).
The ones a deployment usually sets:

| Variable | Default | Notes |
|---|---|---|
| `MCPR_ADMIN_TOKEN` | unset | Admin API bearer. Set it for every deployment. |
| `MCPR_AGENT_KEYS` | unset | `id:key[,id:key]`; seeds agent principals at start (then the setup wizard is skipped). |
| `POSTGRES_PASSWORD` | unset | Required from v0.6: compose refuses to start without it. |
| `MCPR_BIND` | `127.0.0.1` | From v0.6: host address the API port is published on. |
| `MCPR_HOST_PORT` | `8400` | Host port for the API. |
| `MCPR_ALLOWED_HOSTS` | unset | Extra `Host` names accepted (reverse proxy, LAN name). |
| `MCPR_DB_WAIT_TRIES` | `30` | Entrypoint database wait (2 s apart), then fail. |

Exported shell variables work too (compose reads the shell environment before
`.env`), but `.env` is the documented way.

## Security posture (from v0.6)

- **Fail-closed bind.** With no `MCPR_ADMIN_TOKEN` the entrypoint starts
  uvicorn on `127.0.0.1` inside the container and logs
  `admin API open; listening on loopback only; set MCPR_ADMIN_TOKEN`. The API
  is then unreachable from the host. Set a token (it binds `0.0.0.0`), or for
  local development only set `MCPR_ALLOW_OPEN_DEV=1`.
- **Published on loopback.** Compose publishes
  `${MCPR_BIND:-127.0.0.1}:${MCPR_HOST_PORT:-8400}`. Set `MCPR_BIND=0.0.0.0`
  to listen on every interface, and only with a token set.
- **Host allowlist.** Every path checks the `Host` header against `localhost`,
  `127.0.0.1`, `[::1]` and `MCPR_ALLOWED_HOSTS` (any port). A mismatch gets
  **421** with a JSON body. This blocks DNS-rebinding attacks from web pages in
  your browser.
- **Credential length.** `MCPR_ADMIN_TOKEN` and every key in
  `MCPR_AGENT_KEYS` must be at least 32 characters (`openssl rand -hex 32`);
  a shorter one stops the container at start with exit 2 and a FATAL line
  naming the variable.
- **Secrets from files.** `MCPR_DATABASE_URL`, `MCPR_AGENT_KEYS`,
  `MCPR_ADMIN_TOKEN`, `MCPR_DECISION_API_KEY` and `MCPR_AOAI_API_KEY` each
  have a `_FILE` variant (for example `MCPR_ADMIN_TOKEN_FILE=/run/secrets/admin`)
  that wins over the plain variable, so the value never sits in the process
  environment.
- **Schema first.** The entrypoint runs `python -m mcprouter.migrate upgrade`
  before it starts uvicorn; a failed migration stops the container (exit 1)
  instead of serving an old schema.

### Open and protected surfaces

| Path | Auth | Purpose |
|---|---|---|
| `/` | none (the app asks for the admin token) | Dashboard |
| `/api/v1/*` | admin token or agent key, per route ([api.md](reference/api.md)) | REST API |
| `/mcp` | agent key | MCP gateway |
| `/docs`, `/openapi.json` | **none** | API schema. The schema is not secret; it holds no data. |
| `/healthz` | none | Liveness |
| `/readyz` | none | Readiness (from v0.6) |
| `/metrics` | admin token when one is configured (from v0.6) | Prometheus |

Prometheus scrape config with the token:

```yaml
scrape_configs:
  - job_name: mcp-router
    authorization: { credentials_file: /etc/prometheus/mcpr-admin-token }
    static_configs: [{ targets: ["router.lan:8400"] }]
```

### Health semantics

- `/healthz` is **liveness**: `200 {"status":"ok"}` when the process can reach
  the database. From v0.6 a database failure returns a curated `503` instead
  of a bare 500. The image `HEALTHCHECK` uses it.
- `/readyz` (from v0.6) is **readiness**: database, inference engine and a
  `degraded` flag (for example `laya` requested but the model is unavailable,
  so routing runs on the deterministic fallback). Point load balancers here.

## Stdio servers in Docker

An imported `mcpServers` config usually starts servers with `npx …` or
`uvx …`. These run **inside the api container**.

- **`npx`**: from v0.6 the image ships Node 22 (`node`, `npm`, `npx`), so
  `npx -y <package>` servers work. Packages download into the app user's npm
  cache on first start, which needs outbound network access.
- **`uvx`**: not in the image. Either run that server over HTTP elsewhere and
  register its URL, or extend the image (save as `Dockerfile.uvx`):

  ```dockerfile
  FROM mcp-router:local
  USER root
  RUN pip install --no-cache-dir uv
  USER app
  ```

  Build it (`docker build -t mcp-router:uvx -f Dockerfile.uvx .`) and set
  `MCPR_IMAGE=mcp-router:uvx` in `.env`. Verified 2026-10-09: `uvx` runs as
  uid 1000 and fetches packages into the app user's cache.
- **Anything else** (a binary on your host, a Python script): bind-mount it
  read-only into the container and register the in-container path, or run it
  as an HTTP server.

A stdio server that cannot start shows as `unhealthy` in the dashboard after a
refresh.

**Stdio env is stored in plaintext.** The `env` you give a server (API tokens
for GitHub and the like) is stored as JSON in the `mcp_server_credentials`
table. It is never returned by the API or logged, but anyone with database
access or a backup can read it. Protect `mcpr-pgdata` and your dumps
accordingly.

## Logging (from v0.6)

Logs go to stdout. `MCPR_LOG_LEVEL` (default `INFO`) and `MCPR_LOG_FORMAT`
(`console` or `json`; compose sets `json`) control them. Control characters
are scrubbed from logged fields, configured secret values are redacted, and at
API boundaries only exception types are logged, never messages that could
carry a DSN or key.

```bash
docker compose logs -f api
```

## Limits

| Limit | Value | Where |
|---|---|---|
| Request body | 1 MiB (413 above it) | every API and MCP route |
| Tool executions and skill activations | `MCPR_RATE_LIMIT_PER_AGENT_PER_MIN` (120) per agent | execution manager |
| Decision edge | `MCPR_DECISION_RATE_LIMIT_PER_MIN` (120) per principal (from v0.6) | `POST /api/v1/decision/systemone` |
| MCP sessions | `MCPR_MCP_MAX_SESSIONS` (1000) total, `MCPR_MCP_MAX_SESSIONS_PER_AGENT` (32) per agent credential (from v0.6) | `/mcp` |
| `router.find_tools` re-routes | `MCPR_RATE_LIMIT_PER_AGENT_PER_MIN` (120) per agent | `/mcp` |
| Route feedback | 30 per minute per principal, REST and MCP together | `POST /api/v1/route/{request_id}/feedback`, `router.feedback` |
| Tool call deadline | `MCPR_DEFAULT_TOOL_TIMEOUT_S` (30 s) | execution manager |
| Git skill source clone | 120 s, 200 MiB | `src/mcprouter/skills/gitsource.py` |

The git size cap is checked **after** the clone finishes: a hostile repository
can use up to 120 s of clone time and its full size on disk before it is
refused and removed. Only admins can register skill sources.

### Memory

Measured on the zero-ML image (`docker stats` and `/proc/1/status`,
2026-10-09, CPU host):

| State | Container memory | Process RSS peak |
|---|---|---|
| Idle after boot, empty catalog | 96 MiB | |
| 100 servers / 1,000 tools, after 150 routes | 101 MiB | 129 MiB |

The inference flavor adds the BGE and Laya models on top (not measured here;
plan for several GiB). Image sizes: 486 MB (zero-ML, with Node) and 1.78 GB
(inference, CPU torch).

## Reverse proxy and TLS

The router speaks plain HTTP. Terminate TLS in a reverse proxy and keep the
router on loopback or a private network:

- add the public name to `MCPR_ALLOWED_HOSTS` (for example `router.example.com`),
  or the proxy's requests get 421;
- pass the original `Host` header through (`proxy_set_header Host $host;` in
  nginx);
- disable response buffering for `/mcp` (`proxy_buffering off;`), since MCP
  responses can be event streams;
- keep `MCPR_BIND=127.0.0.1` when the proxy runs on the same host.

## Flavors

- **Inference (CPU):** `docker build -t mcp-router:local .` then
  `docker compose -f docker-compose.yml -f docker-compose.inference.yml up -d --build`
  (bge + laya; models cached in the `mcpr-models-cache` volume via `HF_HOME`).
  The first boot downloads the models before `/healthz` turns healthy.
- **GPU:** add `-f docker-compose.gpu.yml` on top of the inference override
  (NVIDIA reservation, `MCPR_DEVICE=cuda`, CUDA torch index via
  `MCPR_TORCH_INDEX_URL`, default cu124). **UNPROVEN**: authored without a GPU.

## Upgrades and backups

See [upgrade.md](upgrade.md): `pg_dump` before every upgrade, which volumes
matter, schema migrations and downgrades.

## Releases

Tag `vX.Y.Z` on a green `master` commit: `.github/workflows/release.yml` pushes
`ghcr.io/amantux/mcp-router:<tag>`, `:latest`, and `-inference` variants. From
v0.6 a `verify` job checks the tag against `pyproject.toml`, the CHANGELOG and a
green CI run before any image is pushed.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `docker compose up --build` hangs at `npm ci` | BuildKit's bridge network stalls on some hosts. Build the image with the host network first: `docker build --network=host -t mcp-router:local .`, then `docker compose up -d --wait`. |
| Connection refused on `:8400` from the host, container healthy | No `MCPR_ADMIN_TOKEN`, so the API listens on loopback inside the container (from v0.6). Set the token. |
| `421` on every request | The `Host` you use is not localhost or in `MCPR_ALLOWED_HOSTS`. |
| `FATAL: MCPR_DATABASE_URL is required` | Running the image outside compose without a database URL. |
| `FATAL: Postgres not reachable after N attempts` | Database down or wrong credentials. Check `docker compose logs db` and `POSTGRES_*`. |
| `FATAL: /data is not writable by uid 1000` | A bind mount or read-only volume replaced the image's directory. `chown -R 1000:1000` the host path. |
| Imported server stays `unhealthy` | Its stdio command does not exist in the container. See [Stdio servers in Docker](#stdio-servers-in-docker). |
| `/metrics` returns 401 | From v0.6 it needs the admin token. Configure the scrape as above. |

## Verified (2026-10-09, Docker 29, clean `git clone` of wave5/container)

CI builds with plain `up --build`. On the verification host the image was built
with `docker build --network=host -t mcp-router:local .` before
`docker compose -p mcprsmoke up -d --wait` (`MCPR_HOST_PORT=8450`).

```
$ MCPR_ADMIN_TOKEN=… MCPR_AGENT_KEY=… scripts/smoke.sh
PASS HEALTHCHECK healthy
PASS GET / 200 HTML with app root
PASS GET /healthz {"status":"ok"}
PASS models/health decisionBackend.kind=deterministic
PASS setup/status needsSetup=false principals=1 (env-seeded)
PASS POST /principals apiKey returned (len 48)
PASS MCP initialize server=mcp-router tools=['router.activate_skill', 'router.find_tools', 'router.read_skill_resource']
PASS POST /api/v1/route 200 {"request_id":"fbdceb97-…","tools":[],"skills":[],"fallback_used":false,…}
PASS api uid=1000 (non-root)
SMOKE OK in 3s
$ docker compose -p mcprsmoke down -v
```

Entrypoint guards (each run separately): no DB URL → `FATAL: MCPR_DATABASE_URL
is required`; unreachable DB → `waiting for database (2/2): OperationalError` then
`FATAL: Postgres not reachable after 2 attempts`; `--tmpfs /data:ro` →
`FATAL: /data is not writable by uid 1000`.
