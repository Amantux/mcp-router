# Deploying MCP Router with Docker

## Quickstart (zero-ML)
```bash
cp .env.example .env
# append: MCPR_ADMIN_TOKEN=<long random>  MCPR_AGENT_KEYS=<agent>:<key>
docker compose up -d --build --wait
open http://localhost:8400/            # dashboard; API at /api/v1, MCP at /mcp
```
The `db` service publishes **no host port**; only `api` is reachable.

## Environment (.env)
| Variable | Default | Notes |
|---|---|---|
| `MCPR_ADMIN_TOKEN` | unset | Admin API bearer. Unset + no agent keys = OPEN dev mode (entrypoint warns). |
| `MCPR_AGENT_KEYS` | unset | `id:key[,id:key]`; seeds principals at start (so `needsSetup` is false). |
| `MCPR_HOST_PORT` | 8400 | Host port for the api. |
| `MCPR_ALLOWED_HOSTS` | unset | Extra Host names `/mcp` accepts (see INSTALL §1). |
| `MCPR_EMBEDDING_BACKEND` / `MCPR_DECISION_BACKEND` | hash / deterministic | Zero-ML defaults. |
| `MCPR_DEVICE` | auto | cpu / cuda / auto. |
| `POSTGRES_USER/PASSWORD/DB` | mcprouter | Change the password for anything non-local. |
| `MCPR_DB_WAIT_TRIES` | 30 | Entrypoint DB wait (2s apart), then fail. |

Container: uid 1000 `app`, one uvicorn worker (the lifespan owns the sync/rollup
loops and model load), `HEALTHCHECK` on `/healthz`. The entrypoint fails loudly
if `MCPR_DATABASE_URL` is missing, if `/data`, `/srv/skills-cache` or
`/srv/models-cache` is not writable (chown bind mounts to 1000:1000), or if
Postgres is unreachable after the bounded wait.

## Flavors
- **Inference (CPU):** `docker build -t mcp-router:local .` then
  `docker compose -f docker-compose.yml -f docker-compose.inference.yml up -d --build`
  (bge + laya; models cached in the `mcpr-models-cache` volume via `HF_HOME`).
  Image built here: **1.54 GB** (base: 241 MB). Not boot-tested with models.
- **GPU:** add `-f docker-compose.gpu.yml` on top of the inference override
  (NVIDIA reservation, `MCPR_DEVICE=cuda`, CUDA torch index via
  `MCPR_TORCH_INDEX_URL`, default cu124). **UNPROVEN** — authored without a GPU.

## Upgrades & backups
Schema init is additive (`create_all`) at startup; pull/rebuild and
`docker compose up -d`. Back up the `mcpr-pgdata` volume (or `pg_dump`) and
`mcpr-skills-cache`; `mcpr-models-cache` is re-downloadable.

## Releases
Tag `v*` → `.github/workflows/release.yml` pushes `ghcr.io/amantux/mcp-router:<tag>`,
`:latest`, and `-inference` variants.

## Verified (2026-10-09, Docker 29, clean `git clone` of wave5/container)
Host note: on this host BuildKit's bridge network stalls `npm ci`, so the image
was built with `docker build --network=host -t mcp-router:local .` before
`docker compose -p mcprsmoke up -d --wait` (MCPR_HOST_PORT=8450). CI uses plain
`up --build`.
```
$ MCPR_ADMIN_TOKEN=… MCPR_AGENT_KEY=… scripts/smoke.sh
PASS HEALTHCHECK healthy
PASS GET / 200 HTML with app root
PASS GET /healthz {"status":"ok"}
PASS models/health decisionBackend.kind=deterministic
PASS setup/status needsSetup=false principals=1 (env-seeded)
PASS POST /principals apiKey returned (len 48)
PASS MCP initialize server=mcp-router tools=['router.activate_skill', 'router.find_tools', 'router.read_skill_resource']
PASS POST /api/v1/route 200 {"request_id":"fbdceb97-2c7d-43e0-9712-f40d55b548ab","tools":[],"skills":[],"fallback_used":false,"latency_ms":26.325,"no_match":true,"max_tools_applied":3,"max
PASS api uid=1000 (non-root)
SMOKE OK in 3s
$ docker compose -p mcprsmoke down -v
```
Entrypoint guards (each run separately): no DB URL → `FATAL: MCPR_DATABASE_URL
is required`; unreachable DB → `waiting for database (2/2): OperationalError` then
`FATAL: Postgres not reachable after 2 attempts`; `--tmpfs /data:ro` →
`FATAL: /data is not writable by uid 1000`.
