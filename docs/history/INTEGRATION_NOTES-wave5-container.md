# Wave 5 — container (W5c) integration notes
- db no longer publishes :5434 (task requirement). README Quickstart line
  `docker compose up -d db  # pgvector on :5434` and tests/conftest.py's default
  DSN still assume it: run the test DB separately (e.g. `docker run -d -p 5434:5432
  -e POSTGRES_USER=mcprouter -e POSTGRES_PASSWORD=mcprouter pgvector/pgvector:pg16`)
  or add a dev override. Not fixed here (outside fence).
- /api/v1/setup/status is admin-gated; MCPR_AGENT_KEYS seeds a principal, so
  needsSetup is false on any keyed stack.
- Installed mcp SDK: `streamable_http_client(url, http_client=create_mcp_http_client(headers=...))`,
  `InitializeResult.server_info` (snake_case).
- Host BuildKit bridge networking stalls npm ci here; built with --network=host.
