# Changelog

## 0.5.0

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

## 0.4.0

- Agent Skills routing: skill sources, catalog, unified tool + skill routing,
  and MCP prompts/resources exposure.

## 0.3.0

- Remote and Azure OpenAI inference backends; edge deployment support.

## 0.2.0

- Budgets, response cache, analytics and the admin UI.

## 0.1.0

- Initial release: MCP gateway, tool discovery, routing, execution and audit.
