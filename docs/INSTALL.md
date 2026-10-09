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

**Docker Compose (db + api):**

```bash
export MCPR_ADMIN_TOKEN=$(openssl rand -hex 24)
export MCPR_AGENT_KEYS="copilot:$(openssl rand -hex 24),claude:$(openssl rand -hex 24)"
docker compose up -d            # db (pgvector, host :5434) + api (:8400)
```

**Bare metal** (same commands as the [README Quickstart](../README.md#quickstart)):

```bash
docker compose up -d db
uv venv --python 3.12 .venv && uv pip install -e '.[dev]'   # add '.[inference]' for real embeddings
export MCPR_ADMIN_TOKEN=... MCPR_AGENT_KEYS=...
.venv/bin/uvicorn --factory mcprouter.api.app:create_app --port 8400
```

- **Run a single worker only.** Approvals, rate limits, sessions and routing
  all live in one process ([security-model.md](security-model.md)). Do not
  pass `--workers N`.
- MCP endpoint: `http://localhost:8400/mcp`. Admin API: `http://localhost:8400/api/v1/*`
  with `Authorization: Bearer $MCPR_ADMIN_TOKEN`.
- Dashboard: currently the UI dev server, `cd ui && npm ci && npm run dev`
  (port :5180). Production static serving is still in progress, so check the
  [README](../README.md) for the current method.
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

> **Skill rules: available from v0.4.** Policy rules for skills (a
> skill resource kind) are not on `master` yet. Until they land, `RuleIn`
> accepts only the server/tool fields shown above.

## 3. Register MCP servers (and skill sources)

You can import an existing Claude-Desktop-style config directly:

```bash
curl -s -X POST localhost:8400/api/v1/servers/import -H "$ADMIN" \
  -H 'Content-Type: application/json' \
  -d @claude_desktop_config.json        # {"mcpServers": {...}}
```

To add one server, use `POST /api/v1/servers` or the dashboard's **Servers**
page. After that, add the policy rules from section 2 that reference the new
server ids.

> **Skill sources: available from v0.4.** `POST /api/v1/skill-sources`
> (a local skill directory or a git source) is not on `master` yet.

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

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `401` on `/mcp` | Missing or wrong `Authorization: Bearer <agent key>`. An invalid key is never downgraded to dev mode. Rotate the key and update the client. |
| `403` on `/api/v1/*` | You used an agent key (it is never an admin credential), or `MCPR_ADMIN_TOKEN` is unset outside dev mode (fails closed). |
| `421 Invalid Host header` | The `/mcp` endpoint has DNS-rebinding protection, which the MCP SDK enables automatically because the gateway is built with `host="127.0.0.1"`. It accepts only `localhost`, `127.0.0.1` and `[::1]` Host headers (any port). On `master` there is no env setting for this. Reach the router through `localhost` (for a remote host, use `ssh -L 8400:localhost:8400`) instead of an IP or DNS name. Allowing other hosts means passing `transport_security` to `build_gateway`, which is a code change. |
| Empty tool list, or only `router.find_tools` | The agent has no policy rules yet (deny-by-default), no servers are registered, or no route has run yet. Add rules (section 2), then call `router.find_tools`. |
| Tools never change after `find_tools` | The client caches its tool list. Refresh it as described for that client in section 4. Calling an authorized tool by name still works. |
| `auth.dev_mode` warning in logs | No agent keys, no admin token and no principals are configured, so the admin API is open. Use this only on localhost. Set `MCPR_ADMIN_TOKEN` and `MCPR_AGENT_KEYS` before any real use. |
