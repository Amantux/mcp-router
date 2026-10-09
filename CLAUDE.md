# CLAUDE.md — MCP Router

> Project override: this is NOT the global CLAUDE.md's business web app.
> Local-first MCP discovery + intelligent tool-routing platform targeting a
> single-GPU laptop (RTX 4060 8GB). Human contributor guide (setup, gates,
> test database, docs regeneration): CONTRIBUTING.md. Architecture:
> docs/architecture.md. Design baseline: docs/SPEC.md and docs/scoping.md
> (both amended; shipped behaviour wins where they differ).

## Environment
- Python 3.12 venv at `.venv` (uv-managed). ALWAYS `.venv/bin/python`; never
  system python3 (3.10).
- `mcp` SDK is **2.x** — FastMCP was RENAMED: `from mcp.server.mcpserver
  import MCPServer`. Your training prior writes 1.x code; verify every SDK
  call against the installed package (migration guide:
  py.sdk.modelcontextprotocol.io/v2/migration). Server apps:
  `streamable_http_app()`; client: `mcp.client.stdio.stdio_client`,
  `mcp.client.streamable_http`, `ClientSession.initialize/list_tools/call_tool`.
- Postgres+pgvector via `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d db`
  (dev override publishes 127.0.0.1:5434; base compose has no host port). Tests use
  MCPR_DATABASE_URL; integration tests REQUIRE the db container.
- No GPU on this dev box. CUDA/FP16 paths are written device-agnostic and
  validated on the target laptop later (bench/ harness). Never skip writing
  the CUDA path; never claim it verified.
- The `[inference]` extra (torch/transformers/sentence-transformers) is heavy
  and OPTIONAL. Everything must degrade to the hash-embedding + deterministic
  decision backends with zero ML imports.

## Contracts
- `src/mcprouter/interfaces.py` and `models.py` are the shared contract.
  Feature branches may ADD, never change existing signatures — contract
  changes happen only at integration.
- Laya = typed Choice/Score/Noul questions (non-autoregressive). The decision
  model selects among supplied options only; it never generates tool names
  and never executes anything.
- Policy/authz is deterministic code AFTER ranking. A routing score must
  never widen access. Deny by default.

## Gates (all of them, before "done")
- The gate table in CONTRIBUTING.md §3 (`make check`), with `MCPR_REQUIRE_DB=1`
  so a missing database fails instead of skipping.
- Security guards mutation-checked: break the guard, named test fails, restore
- Curated errors only at API boundaries; secret redaction before any log/model
  input; CR/LF-scrub attacker-controlled log fields
- A new or changed `MCPR_*` setting: regenerate docs/reference/configuration.md
  (`make docs-gen`) in the same commit.

## Conventions
- camelCase on the wire, snake_case inside. UUID string ids. UTC aware datetimes.
- Routers thin; logic in services. One shared loader/helper per concern.
- Commit per logical change; Co-Authored-By + Claude-Session trailers.
- Skills: one exposure path (`gateway/skills.py::SkillExposure`), visibility only from the latest routing decision, curated errors; conventions + residuals in `docs/skills.md`.
