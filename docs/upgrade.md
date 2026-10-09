# Upgrading, downgrading and backups

Read the [CHANGELOG](../CHANGELOG.md) entry for the target version first. The
**Breaking**, **Config** and **Schema** sections list what changes for an operator.

## 1. Back up first

The database is the only state you cannot recreate. Take a dump before every
upgrade:

```bash
docker compose exec -T db pg_dump -U mcprouter -d mcprouter -Fc > mcpr-$(date +%F).dump
```

Restore into an empty database (stop `api` first):

```bash
docker compose stop api
docker compose exec -T db pg_restore -U mcprouter -d mcprouter --clean --if-exists < mcpr-2026-10-09.dump
docker compose start api
```

Use your own `POSTGRES_USER` / `POSTGRES_DB` if you changed them.

What the volumes hold:

| Volume | Contents | Back up? |
|---|---|---|
| `mcpr-pgdata` | Postgres: catalog, principals, policy, audit, analytics | **Yes** (use `pg_dump`, not a copy of a live data directory) |
| `mcpr-skills-cache` | Clones of git skill sources | Optional; a sync re-clones them |
| `mcpr-models-cache` | Downloaded models (`HF_HOME`) | No; re-downloaded on demand |
| `mcpr-data` | Mounted at `/data`; the app writes nothing there today | No |

Embeddings are derived data. They are stored with the backend that produced
them and are recomputed on the next sync after you change
`MCPR_EMBEDDING_BACKEND`, so you never need to back them up separately.

## 2. Upgrade (Docker)

```bash
git pull
docker compose build            # always rebuild the base image first
docker compose up -d --wait
```

With the inference flavor, rebuild the base **before** the inference image:
`Dockerfile.inference` layers on whatever `mcp-router:local` currently is, so
skipping this ships the new model stack on top of the old code.

```bash
docker build -t mcp-router:local .
docker compose -f docker-compose.yml -f docker-compose.inference.yml up -d --build --wait
```

For an offline host, pre-load the models into `mcpr-models-cache` and set
`HF_HUB_OFFLINE=1` so the model loader never reaches the network.

## 3. Schema migrations (from v0.6)

From 0.6.0 the schema is versioned with Alembic.

- **Automatic.** The container entrypoint runs
  `python -m mcprouter.migrate upgrade` before it starts uvicorn (a failure
  stops the container, exit 1), and the app's own startup runs the same step.
  Both take a Postgres advisory lock, so several starting containers never
  race. A database that is already at `head` gets no DDL at all.
- **Manual.** To migrate before switching traffic, run the same step by hand:
  `docker compose run --rm api python -m mcprouter.migrate upgrade` (source
  install: `.venv/bin/python -m mcprouter.migrate upgrade`, against
  `MCPR_DATABASE_URL`). The subcommand is required; a bare call prints usage
  and exits 2.
- **From 0.5.x or older.** The first 0.6 start recognises the pre-Alembic
  schema, applies the old additive column fixes once, and stamps the database
  at revision `0001`. Later revisions then apply normally. No manual step.

## 4. Downgrade

- **One step back is supported from revision `0002` on**: run
  `python -m mcprouter.migrate downgrade -1` with the *newer* image, then
  deploy the older image.
- **An older image refuses a newer database.** If the database is at a
  revision the image does not know, startup fails with
  `database schema is newer than this build`. This is deliberate: an old build
  writing to a newer schema can corrupt data. Downgrade the schema first, or
  restore the backup from section 1.
- **0.6 back to 0.5.x** is not a migration: restore the pre-upgrade dump.

## 5. Breaking changes in 0.6

These need action when you upgrade from 0.5.x:

- `POSTGRES_PASSWORD` must be set in `.env`; compose no longer has a default.
- Without `MCPR_ADMIN_TOKEN` the API binds to `127.0.0.1` inside the container,
  so it is unreachable from the host. Set a token (recommended) or, for local
  development only, `MCPR_ALLOW_OPEN_DEV=1`.
- Compose publishes on `${MCPR_BIND:-127.0.0.1}`. Set `MCPR_BIND=0.0.0.0` to
  listen on every host interface.
- Requests with a `Host` header outside localhost and `MCPR_ALLOWED_HOSTS` get
  421 on every path, not only `/mcp`.
- `/metrics` needs the admin bearer token when one is configured.

## 6. Source installs

```bash
git pull
uv pip install -e '.[dev]'           # picks up dependency changes
cd ui && npm ci && npm run build && cd ..
```

Then restart the server. From v0.6 the restart applies migrations as above.
