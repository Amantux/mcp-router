# Integration notes: wave 5, install guide (W5c)

Docs-only change. Files: `docs/INSTALL.md` (new), `README.md` (3-line pointer
under Quickstart), and this note. No code changes.

## Verified against source (master at branch point)

- Meta-tool `router.find_tools`, input `{"query": string}`
  (`gateway/server.py` `META_TOOL`). The MCP path is `/mcp` (`MCP_PATH`).
- `POST /api/v1/principals` body `{agentId, maxTools, maxServers, enabled}`
  (camelCase aliases). The response includes `apiKey` once. Rotation:
  `POST /principals/{id}/rotate-key`.
- `POST /api/v1/policy-rules` body `{agentId, serverId?, toolName?,
  maxOperation: read|write|execute, requiresApproval}`. **There is no skill
  resource kind on master.** INSTALL.md marks skill rules and
  `/api/v1/skill-sources` as "available from v0.4".
- `/api/v1/models/health` is admin-only (`require_admin` on the router).
- `MCPR_AGENT_KEYS` is comma-separated `agentId:key` (`deps_auth.py`).
- DNS rebinding: `build_gateway(host="127.0.0.1")` and `app.py` passes no
  `transport_security`. The installed `mcp` SDK
  (`.venv/.../mcp/server/lowlevel/server.py`) therefore enables Host and Origin
  allowlists for `127.0.0.1:*`, `localhost:*` and `[::1]:*`. Any other Host
  gets **421**. There is no env setting to change this.

## Client docs (all fetched 2026-10-09)

| Client | URL | Header support |
|---|---|---|
| Claude Code | https://code.claude.com/docs/en/mcp | `--header`, `.mcp.json` `headers` with `${VAR}` |
| GitHub Copilot CLI | https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers | `--header`, `~/.copilot/mcp-config.json` `headers` (no env expansion documented) |
| Codex CLI | https://learn.chatgpt.com/docs/extend/mcp?surface=cli | `bearer_token_env_var`, `http_headers`, `env_http_headers`, `codex mcp add --url --bearer-token-env-var` |
| Cursor | https://cursor.com/docs/context/mcp | `headers` with `${env:NAME}` |
| VS Code | https://code.visualstudio.com/docs/copilot/reference/mcp-configuration | `headers` + `inputs` (`password: true`) |
| Gemini CLI | https://geminicli.com/docs/tools/mcp-server/ | `httpUrl` + `headers` (env expansion shown only for `env`) |

No client needed a "no custom header" fallback. Only Claude Code's docs say
anything about `list_changed` (interactive sessions auto-refresh on the
2026-07-28 revision). For every other client, INSTALL.md gives the restart or
toggle step from its docs and does not claim push support.

## Follow-ups for the integrator

- README Quickstart still says `http://host:8400/mcp`. With the default
  DNS-rebinding settings, any non-localhost Host gets 421, so that wording
  can mislead. Consider `localhost`, or add an `MCPR_MCP_ALLOWED_HOSTS` style
  setting that feeds `transport_security`.
- INSTALL.md is about 290 lines, above the ~250 guideline. It was kept as one
  file so the per-client snippets sit next to the shared setup steps. Split it
  into `docs/clients/` if it grows.
- Once static UI serving lands, update INSTALL.md §1 (dashboard) and change
  the v0.4 markers when skills merge.
