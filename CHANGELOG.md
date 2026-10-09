# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Dates come from the git history.
Upgrade steps: [docs/upgrade.md](docs/upgrade.md).

## [Unreleased]

Planned as 0.6.0 (wave 6: hardening and full test coverage).

### Breaking

- The container binds to `127.0.0.1` when `MCPR_ADMIN_TOKEN` is unset and logs
  a warning; set the token, or `MCPR_ALLOW_OPEN_DEV=1` for local development.
- Compose publishes the API on `${MCPR_BIND:-127.0.0.1}` instead of every
  host interface.
- `POSTGRES_PASSWORD` must be set in `.env`; the base compose file has no
  default password any more.
- A `Host` header outside localhost and `MCPR_ALLOWED_HOSTS` gets **421** on
  every path (before: `/mcp` only).
- `/metrics` requires the admin token when one is configured.
- In dev mode with no admin token, the first `POST /api/v1/principals`
  returns **409**: set `MCPR_ADMIN_TOKEN` before creating the first agent.

### Added

- `/readyz` readiness endpoint (database, inference engine, degraded flag);
  `/healthz` answers a curated 503 when the database is down.
- Node 22 (`node`, `npm`, `npx`) in the image, so imported `npx` stdio servers
  run in the container.
- Structured logging (`MCPR_LOG_LEVEL`, `MCPR_LOG_FORMAT`).
- Per-agent MCP session cap (`MCPR_MCP_MAX_SESSIONS_PER_AGENT`) and a
  decision-edge rate limit (`MCPR_DECISION_RATE_LIMIT_PER_MIN`).
- Docs: `CONTRIBUTING.md`, `docs/architecture.md`, `docs/upgrade.md`, and the
  generated `docs/reference/configuration.md` and `docs/reference/api.md`, plus
  `docs/reference/cli.md`.

### Config

- New: `MCPR_ADMIN_TOKEN` in `.env.example`, `MCPR_ALLOW_OPEN_DEV`,
  `MCPR_BIND`, `MCPR_LOG_LEVEL`, `MCPR_LOG_FORMAT`, `MCPR_MCP_MAX_SESSIONS`,
  `MCPR_MCP_MAX_SESSIONS_PER_AGENT`, `MCPR_DECISION_RATE_LIMIT_PER_MIN`,
  `MCPR_DEDUP_MAX_PAIRS`.
- Compose passes `.env` to the api service, so every `MCPR_*` setting reaches
  the container.
- Invalid settings stop startup with `FATAL: <VAR> …` (exit 2) instead of a
  misleading database error.
- `.env.example` lists every setting with its real choices; the embedding
  backends are `hash | bge | aoai` (`local` was never valid).

### Schema

- Alembic migrations replace the startup `ALTER TABLE` list. Revision `0001`
  is the baseline; `0002` adds the HNSW vector indexes and a routing-decision
  index. A 0.5 database is bridged and stamped automatically on first start.
- Migrations run at startup under an advisory lock; `python -m mcprouter.migrate`
  runs them by hand. One-step downgrade from `0002`; an old image refuses a
  newer schema.

### API

- `POST /api/v1/dedup/suggestions/{id}/accept` takes an optional
  `preferredToolId`; dedup scans report `truncated`.
- `GET /api/v1/skills` accepts `sourceId` and `hasScripts`.
- Validation errors no longer echo request input; unknown body fields are
  rejected (422).

## [0.5.0] - 2026-10-09

### Added

- **Containers**: Dockerfile, entrypoint, `docker-compose.yml` with inference and
  GPU overrides, a dev-only `docker-compose.dev.yml` for the test database,
  `scripts/smoke.sh`, a compose-smoke CI job and a release workflow
  (see `docs/deploy.md`).
- **Web server**: the API serves the built UI; `MCPR_ALLOWED_HOSTS` controls
  Host/transport security for the API and MCP gateway.
- **Setup wizard**: first-run setup endpoints and a guided UI wizard.
- **Savings and feedback**: savings metrics, a routing feedback table with REST
  endpoints and the `router.feedback` MCP meta-tool, feedback analytics and a
  prior (see `docs/analytics.md`), plus UI cards, thumbs and the route lens.

### Config

- `MCPR_ALLOWED_HOSTS`, `MCPR_UI_DIST`, `MCPR_HOST_PORT`,
  `MCPR_PREFILL_MS_PER_1K_TOKENS`, `MCPR_PRICE_PER_1K_INPUT_TOKENS`,
  `MCPR_CURRENCY`.

### Schema

- New table `route_feedback` and `app_settings` (created at startup).

## [0.4.0] - 2026-10-09

### Added

- Agent Skills routing: skill sources, catalog, unified tool + skill routing,
  and MCP prompts/resources exposure.

### Config

- `MCPR_MAX_EXPOSED_SKILLS`, `MCPR_SKILL_BODY_MAX_BYTES`,
  `MCPR_SKILL_RESOURCE_MAX_BYTES`, `MCPR_SKILLS_CACHE_DIR`.

### Schema

- New tables `skill_sources`, `skills`, `skill_versions`.

## [0.3.0] - 2026-10-08

### Added

- Remote and Azure OpenAI inference backends; edge deployment support.

## [0.2.0] - 2026-10-08

### Added

- Budgets, response cache, analytics and the admin UI.

## [0.1.0] - 2026-10-08

### Added

- Initial release: MCP gateway, tool discovery, routing, execution and audit.
