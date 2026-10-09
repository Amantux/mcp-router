# Install MCP Router into your coding agent

Run the router once, give each coding agent its own API key and policy rules,
then point the agent at one MCP endpoint: `http://localhost:8400/mcp`. The
agent sees a small routed subset of your tools plus the `router.find_tools`
meta-tool. It does not see every tool from every server.

Client snippets were checked against each vendor's docs on **2026-10-09**.
The source URL is listed under each client. Clients change quickly, so check
the linked page if a flag gets rejected.

---

## 1. Run the router

**Docker Compose (db + api)**, configured through `.env`:

```bash
cp .env.example .env
# in .env:
#   MCPR_ADMIN_TOKEN=<openssl rand -hex 32>
#   POSTGRES_PASSWORD=<openssl rand -hex 32>
#   MCPR_AGENT_KEYS=copilot:<openssl rand -hex 32>,claude:<openssl rand -hex 32>
docker compose up -d --build --wait   # db (pgvector, internal only) + api (127.0.0.1:8400)
```

Full container guide (environment, open surfaces, stdio servers, limits,
reverse proxy, troubleshooting): [deploy.md](deploy.md).

**From source** (the [README source install](../README.md#run-from-source)):

```bash
POSTGRES_PASSWORD=mcprouter docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --wait db   # dev only: 127.0.0.1:5434
uv sync --locked --extra dev --python 3.12                   # add --extra inference for real embeddings
(cd ui && npm ci && npm run build)                           # dashboard, before the server starts
export MCPR_ADMIN_TOKEN=$(openssl rand -hex 32)              # >= 32 chars, or startup is FATAL
export MCPR_AGENT_KEYS="claude:$(openssl rand -hex 32)"      # optional; each key >= 32 chars
.venv/bin/uvicorn --factory mcprouter.api.app:create_app --port 8400
```

The dev override publishes the database on the port the source install
expects by default (`MCPR_DATABASE_URL`, see
[configuration.md](reference/configuration.md)). Never use it in production.

- **Run a single worker only.** Approvals, rate limits, sessions and routing
  all live in one process ([security-model.md](security-model.md)). Do not
  pass `--workers N`.
- MCP endpoint: `http://localhost:8400/mcp`. Admin API: `http://localhost:8400/api/v1/*`
  with `Authorization: Bearer $MCPR_ADMIN_TOKEN`.
- Dashboard: served by the API itself at `http://localhost:8400/` (the Docker
  image builds it in). From source, build it once with
  `cd ui && npm ci && npm run build`; the server reads `ui/dist` (override with
  `MCPR_UI_DIST`). `npm run dev` (port :5180, proxies to :8400) is for UI work only.
  With no principals yet, the dashboard opens the setup wizard (`/setup`).
- Reaching the router from another machine: it keeps DNS-rebinding
  protection on and by default accepts only `localhost`/`127.0.0.1`/`[::1]`
  Host headers (anything else gets HTTP 421, on
  every path). Set
  `MCPR_ALLOWED_HOSTS=router.lan,10.0.0.5:8400` (comma list; a bare host means
  any port) to allow named hosts. Tradeoff: every name you add is one a
  malicious web page could point at your router via DNS rebinding, so list
  exact names you control, never a wildcard, and keep agent keys required.
- Inference backends (embeddings, decision model) are set with `MCPR_*` env
  variables. See [backends.md](backends.md).

## 2. Create an agent identity

Give each client its own principal. Routing, caps, policy and analytics are
all tracked per agent.

**Option A, at startup:** set `MCPR_AGENT_KEYS="<agentId>:<key>[,<agentId>:<key>...]"`.
The router stores only the key hash. The env value wins on rotation.

**Option B, admin API:** the raw key is returned **once**, so save it.

```bash
ADMIN="Authorization: Bearer $MCPR_ADMIN_TOKEN"
curl -s -X POST localhost:8400/api/v1/principals -H "$ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"agentId":"copilot","maxTools":8}'
# -> {"id":"...","agentId":"copilot",...,"apiKey":"<SAVE THIS>"}
```

Rotate a key with `POST /api/v1/principals/{id}/rotate-key`.

**Grant policy.** Every agent is deny-by-default, including the dev agent.
A rule names `agentId` and optionally `serverId` and/or `toolName`, plus
`maxOperation` (`read` | `write` | `execute`) and `requiresApproval`:

```bash
# read-only access to every tool on one server (serverId from GET /api/v1/servers)
curl -s -X POST localhost:8400/api/v1/policy-rules -H "$ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"agentId":"copilot","serverId":"<server-id>","maxOperation":"read"}'

# one write tool, gated behind human approval (Dashboard -> Approvals)
curl -s -X POST localhost:8400/api/v1/policy-rules -H "$ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"agentId":"copilot","serverId":"<server-id>","toolName":"create_issue",
       "maxOperation":"write","requiresApproval":true}'
```

**Skill rules.** A skill rule uses `resourceKind: "skill"`. Its `serverId` is
the skill source id and its `toolName` is a skill-name glob. An `unknown`-class
skill needs an `execute` ceiling. `resourceKind` cannot be changed after
create.

```bash
curl -s -X POST localhost:8400/api/v1/policy-rules -H "$ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"agentId":"copilot","resourceKind":"skill","serverId":"<skill-source-id>",
       "toolName":"pdf-*","maxOperation":"read"}'
```

## 3. Register MCP servers (and skill sources)

You can import an existing Claude-Desktop-style config directly:

```bash
curl -s -X POST localhost:8400/api/v1/servers/import -H "$ADMIN" \
  -H 'Content-Type: application/json' \
  -d @claude_desktop_config.json        # {"mcpServers": {...}}
```

> **Stdio servers in Docker.** Imported entries usually run `npx …` or
> `uvx …`, and with Docker they run *inside the api container*. The
> image has Node 22, so `npx` servers work; `uvx` servers do not unless you
> extend the image. See [Stdio servers in Docker](deploy.md#stdio-servers-in-docker).
> A server whose command is missing shows as `unhealthy` after the import.

To add one server, use `POST /api/v1/servers` or the dashboard's **Servers**
page. After that, add the policy rules from section 2 that reference the new
server ids.

**Skill sources.** Register a local skill directory (absolute path) or a git
source (https only), then sync it:

```bash
curl -s -X POST localhost:8400/api/v1/skill-sources -H "$ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"team-skills","kind":"directory","location":"/srv/skills"}'
curl -s -X POST localhost:8400/api/v1/skill-sources/<id>/sync -H "$ADMIN"
```

For git, use `"kind":"git","location":"https://…","gitRef":"main"`. Ingest
rules, risk classes and exposure are covered in [`skills.md`](skills.md).

## 4. Point your client at the router

Every client below uses the same server: URL `http://localhost:8400/mcp`,
header `Authorization: Bearer <agent key>`, streamable-HTTP transport. Keep the
key in an env variable when the client supports expansion. Do not commit raw
keys to a repo-scoped config file.

**What the agent sees:** the tools/list call returns at most `maxTools` routed
tools, named `server_name.tool_name`, plus **`router.find_tools`**
(input `{"query": "<task>"}`). To re-route, ask the agent to *"call
router.find_tools with the task I'm working on"*. The router replaces that
agent's exposure and sends `tools/list_changed`. A tool that is authorized but
not currently exposed can still be called, so a client with a stale tool list
keeps working. Exposure is not a security boundary.

**Clients that cache tool lists:** these clients may not pick up the new set
until the server is refreshed or restarted. The refresh method for each client
is listed below.

### Claude Code

Source: https://code.claude.com/docs/en/mcp (fetched 2026-10-09)

```bash
claude mcp add --transport http mcp-router http://localhost:8400/mcp \
  --header "Authorization: Bearer $MCPR_KEY"
```

Project form (`.mcp.json`; `${VAR}` / `${VAR:-default}` are expanded):

```json
{
  "mcpServers": {
    "mcp-router": {
      "type": "http",
      "url": "http://localhost:8400/mcp",
      "headers": { "Authorization": "Bearer ${MCPR_KEY}" }
    }
  }
}
```

Refresh: per the docs, interactive sessions fetch the updated tool list
automatically when `list_changed` arrives. That push uses the 2026-07-28
protocol revision, which the router supports through per-agent
`subscriptions/listen`.

### GitHub Copilot CLI (`copilot`)

Source: https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers
and https://docs.github.com/en/copilot/how-tos/use-copilot-agents/use-copilot-cli (fetched 2026-10-09)

```bash
copilot mcp add --transport http mcp-router http://localhost:8400/mcp \
  --header "Authorization: Bearer <key>"
```

The command above writes to `~/.copilot/mcp-config.json`. You can also edit
that file directly, or use `/mcp add` inside a session:

```json
{
  "mcpServers": {
    "mcp-router": {
      "type": "http",
      "url": "http://localhost:8400/mcp",
      "headers": { "Authorization": "Bearer <key>" },
      "tools": ["*"]
    }
  }
}
```

The docs do not mention env-variable expansion for this file, so put the
literal key here. The file is user-scoped, not in a repo. Refresh: list_changed
handling is not documented. If tools look stale, restart the `copilot` session.

### OpenAI Codex CLI

Source: https://learn.chatgpt.com/docs/extend/mcp?surface=cli (redirected from
https://developers.openai.com/codex/mcp; fetched 2026-10-09)

```bash
export MCPR_KEY=<key>
codex mcp add mcp-router --url http://localhost:8400/mcp --bearer-token-env-var MCPR_KEY
```

Or edit `~/.codex/config.toml` directly:

```toml
[mcp_servers.mcp-router]
url = "http://localhost:8400/mcp"
bearer_token_env_var = "MCPR_KEY"     # sends Authorization: Bearer $MCPR_KEY
```

Use `http_headers` for static headers or `env_http_headers` for headers taken
from env variables. Refresh: list_changed handling is not documented. Start a
new Codex session to pick up a re-routed set.

### Cursor

Source: https://cursor.com/docs/context/mcp (fetched 2026-10-09)

`.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "mcp-router": {
      "url": "http://localhost:8400/mcp",
      "headers": { "Authorization": "Bearer ${env:MCPR_KEY}" }
    }
  }
}
```

Refresh: list_changed handling is not documented. Toggle the server off and on
in Cursor's MCP settings to refresh.

### VS Code (Copilot Chat agent mode)

Source: https://code.visualstudio.com/docs/copilot/reference/mcp-configuration (fetched 2026-10-09)

`.vscode/mcp.json`. With this config, VS Code prompts for the key the first
time the server starts:

```json
{
  "inputs": [
    { "type": "promptString", "id": "mcpr-key", "description": "MCP Router agent key", "password": true }
  ],
  "servers": {
    "mcp-router": {
      "type": "http",
      "url": "http://localhost:8400/mcp",
      "headers": { "Authorization": "Bearer ${input:mcpr-key}" }
    }
  }
}
```

Refresh: the reference page does not cover list_changed. Use the Command
Palette, **MCP: List Servers → Restart**.

### Gemini CLI

Source: https://geminicli.com/docs/tools/mcp-server/ (fetched 2026-10-09)

`~/.gemini/settings.json` (user) or `.gemini/settings.json` (project). Use
**`httpUrl`** for streamable HTTP, because `url` means SSE:

```json
{
  "mcpServers": {
    "mcp-router": {
      "httpUrl": "http://localhost:8400/mcp",
      "headers": { "Authorization": "Bearer <key>" }
    }
  }
}
```

The docs describe `$VAR` / `${VAR}` expansion, but the page I checked only
shows it in the `env` block. Use a literal key, and keep it out of a
project-scoped file you commit. Refresh: list_changed handling is not
documented. Restart the CLI.

## 5. Verify

```bash
curl -s localhost:8400/api/v1/models/health -H "$ADMIN"     # admin-only; inference runtime status
```

1. Ask the agent to list its MCP tools. You should see `router.find_tools`
   and any routed tools.
2. Ask it to *"call router.find_tools with query 'open a GitHub issue for this bug'"*.
   The tool set it gets back should match the task.
3. In the dashboard, check **Execution history** (one row per call, with the
   policy decision), **Analytics** (the routing funnel) and **Agent lens**
   (what a given agent is shown, and why).

## 6. API conventions

- **Reference.** The live OpenAPI UI is at `/docs` and the schema at
  `/openapi.json` (both open: the schema holds no data). A generated route
  table with the auth class of every route is in
  [reference/api.md](reference/api.md).
- **Auth classes.** Admin routes take `Authorization: Bearer $MCPR_ADMIN_TOKEN`;
  agent routes take an agent key; a few take either. An agent key is never an
  admin credential.
- **Casing.** JSON is camelCase in and out, and request bodies also accept
  snake_case. Two responses are snake_case for historical reasons:
  `POST /api/v1/route` (with a camelCase `bodyTokensEst` inside) and
  `POST /api/v1/decision/systemone`. `POST /api/v1/route/simulate` is camelCase.
- **Tool execution returns 200.** `POST /api/v1/tools/{id}/execute` answers
  200 for policy denials, rate limits, unavailable servers and invalid
  arguments alike. Read the `status` field; do not treat 2xx as success.
- **Skill source sync returns 200.** `POST /api/v1/skill-sources/{id}/sync`
  reports a git or filesystem failure in its `error` field. Check it.
- **400 vs 422.** 422 is a body or query that fails schema validation. 400 is
  a well-formed request the router refuses (an unknown `allowed_servers` entry,
  an invalid registration). Error bodies carry `detail`.
- **Body cap.** Requests over 1 MiB get 413 on every route.
- **Paged lists** take `limit` and `offset` and return `total`;
  `GET /api/v1/servers` returns a plain array.

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `401` on `/mcp` | Missing or wrong `Authorization: Bearer <agent key>`. An invalid key is never downgraded to dev mode. Rotate the key and update the client. |
| `401` on `/api/v1/*` | No bearer, a wrong admin token, or an agent key on an admin route (an agent key is never an admin credential). |
| `403 admin token not configured` | `MCPR_ADMIN_TOKEN` is unset and the router is not in dev mode (agent keys or principals exist), so the admin API fails closed. Set the token and restart. |
| `409` on the first `POST /api/v1/principals` | In dev mode with no admin token, creating the first agent would lock you out of the admin API. Set `MCPR_ADMIN_TOKEN` first. |
| `421` (Invalid Host header) | The `Host` you used is not `localhost`, `127.0.0.1`, `[::1]` or in `MCPR_ALLOWED_HOSTS`. Add the exact name you use, or tunnel (`ssh -L 8400:localhost:8400`) and use `localhost`. |
| Empty tool list, or only `router.find_tools` | The agent has no policy rules yet (deny-by-default), no servers are registered, or no route has run yet. Add rules (section 2), then call `router.find_tools`. |
| Tools never change after `find_tools` | The client caches its tool list. Refresh it as described for that client in section 4. Calling an authorized tool by name still works. |
| `auth.dev_mode` warning in logs | No agent keys, no admin token and no principals are configured, so the admin API is open. Use this only on localhost. Set `MCPR_ADMIN_TOKEN` and `MCPR_AGENT_KEYS` before any real use. |
