"""Generate docs/reference/configuration.md from the settings code.

    .venv/bin/python scripts/gen_config_docs.py           # rewrite the file
    .venv/bin/python scripts/gen_config_docs.py --check   # exit 1 if it is stale

Source of truth: ``mcprouter.settings.SETTINGS_SPEC`` (one entry per variable:
env, type, default, choices, secret, doc, file_var). Each ``file_var`` gets its
own row after its variable. ``DOCS`` below holds the longer user-facing text
and wins over the registry's short ``doc``; a variable without a ``DOCS`` entry
uses the registry text, so a new setting is never undocumented.

The drift test (tests/test_docs_consistency.py) fails when the committed file
differs from a fresh render, so a new setting cannot ship undocumented.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "reference" / "configuration.md"


@dataclass(frozen=True)
class Var:
    env: str
    type: str
    default: str  # rendered; "" means unset
    choices: tuple[str, ...]
    secret: bool
    doc: str


# User-facing descriptions; they win over SETTINGS_SPEC `doc` (the fallback).
DOCS: dict[str, str] = {
    "MCPR_DATABASE_URL": "SQLAlchemy URL of the Postgres + pgvector database. Compose derives it "
    "from `POSTGRES_*`; the default matches the dev override (`127.0.0.1:5434`).",
    "MCPR_AGENT_KEYS": "Comma list of `agentId:key` pairs. Each pair seeds (or re-keys) an agent "
    "principal at startup; only the key hash is stored.",
    "MCPR_EMBEDDING_BACKEND": "Embedding backend. `bge` needs the `[inference]` extra; `aoai` "
    "needs the `MCPR_AOAI_*` variables.",
    "MCPR_EMBEDDING_MODEL_ID": "Hugging Face model id for `bge` embeddings.",
    "MCPR_DECISION_BACKEND": "Decision backend. `laya` needs the `[inference]` extra; any "
    "backend error falls back to `deterministic`.",
    "MCPR_LAYA_MODEL_ID": "Hugging Face model id for the Laya decision model.",
    "MCPR_DEVICE": "Inference device: `auto`, `cpu`, `cuda` or `cuda:<index>`.",
    "MCPR_OPERATING_MODE": "Inference concurrency profile (performance 4, balanced 2, battery 1 "
    "concurrent model calls).",
    "MCPR_MODELS_CACHE_DIR": "Where downloaded models are cached.",
    "MCPR_IDLE_UNLOAD_S": "Battery mode only: unload models after this many idle seconds.",
    "MCPR_EMBED_BATCH_SIZE": "Embedding batch size during catalog sync.",
    "MCPR_LAYA_NOUL_MODE": "How the Laya no-match question is asked.",
    "MCPR_MAX_EXPOSED_TOOLS": "Global cap on tools exposed per route (requests and principals can "
    "lower it, never raise it).",
    "MCPR_MAX_EXPOSED_SERVERS": "Optional global cap on distinct servers per route. Unset means "
    "no cap.",
    "MCPR_RETRIEVAL_CANDIDATES": "Hybrid-retrieval candidates passed to the decision model.",
    "MCPR_ROUTE_CONFIDENCE_FLOOR": "Score below which a candidate is dropped.",
    "MCPR_ROUTE_CACHE_TTL_S": "Route cache entry lifetime in seconds. 0 disables the cache.",
    "MCPR_ROUTE_CACHE_SIZE": "Route cache entries. 0 disables the cache.",
    "MCPR_DECISION_TIMEOUT_S": "Per-question deadline for the decision model; on overrun the "
    "route falls back to deterministic ranking.",
    "MCPR_SYNC_ENABLED": "Run the background catalog sync + health loop in the app process.",
    "MCPR_USAGE_PRIOR_ENABLED": "Enable the bounded usage-prior score nudge (docs/analytics.md).",
    "MCPR_ANALYTICS_ROLLUP_ENABLED": "Recompute the daily analytics rollups in the app (at "
    "startup, then every 24 h).",
    "MCPR_PREFILL_MS_PER_1K_TOKENS": "Savings estimate input; 0 means not configured (reported "
    "as null).",
    "MCPR_PRICE_PER_1K_INPUT_TOKENS": "Savings estimate input; 0 means not configured (reported "
    "as null).",
    "MCPR_CURRENCY": "Currency code shown next to price estimates.",
    "MCPR_DEFAULT_TOOL_TIMEOUT_S": "Deadline for one downstream tool call.",
    "MCPR_RATE_LIMIT_PER_AGENT_PER_MIN": "Per-agent tool execution and skill activation budget.",
    "MCPR_MAX_EXPOSED_SKILLS": "Global cap on routed skills per request.",
    "MCPR_SKILL_BODY_MAX_BYTES": "SKILL.md body cap; longer bodies are flagged `body_truncated`.",
    "MCPR_SKILL_RESOURCE_MAX_BYTES": "Per-resource serve cap for skill files.",
    "MCPR_SKILLS_CACHE_DIR": "Where git skill sources are cloned.",
    "MCPR_DECISION_ENDPOINT": "`remote` backend URL (https; http only to localhost). With no "
    "path, `/v1/decisions` is appended.",
    "MCPR_DECISION_MODEL": "`remote` backend model name.",
    "MCPR_DECISION_API_KEY": "`remote` backend API key.",
    "MCPR_DECISION_API_KEY_FILE": "File holding the `remote` API key; wins over "
    "`MCPR_DECISION_API_KEY`.",
    "MCPR_DECISION_MAX_RETRIES": "`remote` backend retries (0 to 10).",
    "MCPR_UI_DIST": "Directory of the built dashboard served at `/`.",
    "MCPR_ALLOWED_HOSTS": "Extra `Host` values accepted besides localhost, `127.0.0.1` and "
    "`[::1]` (comma list, `host` or `host:port`). Anything else gets 421.",
    "MCPR_ADMIN_TOKEN": "Admin API bearer token. Unset: dev mode while no agents exist, "
    "otherwise the admin API fails closed. From v0.6 the container binds to loopback "
    "when it is unset.",
    "MCPR_LOG_LEVEL": "Log level (from v0.6).",
    "MCPR_LOG_FORMAT": "Log format (from v0.6; compose sets `json`).",
    "MCPR_ALLOW_OPEN_DEV": "From v0.6: with no admin token, bind `0.0.0.0` anyway (dev only).",
    "MCPR_MCP_MAX_SESSIONS": "From v0.6: global cap on live MCP sessions.",
    "MCPR_MCP_MAX_SESSIONS_PER_AGENT": "From v0.6: live MCP sessions per agent; the next "
    "`initialize` gets 429.",
    "MCPR_DECISION_RATE_LIMIT_PER_MIN": "From v0.6: per-principal budget on "
    "`POST /api/v1/decision/systemone`.",
    "MCPR_DEDUP_MAX_PAIRS": "From v0.6: cap on duplicate pairs one dedup scan stores "
    "(the run reports `truncated`).",
    "MCPR_AOAI_ENDPOINT": "Azure OpenAI resource URL (`https://<resource>.openai.azure.com`).",
    "MCPR_AOAI_API_KEY": "Azure OpenAI API key.",
    "MCPR_AOAI_API_KEY_FILE": "File holding the Azure OpenAI key; wins over `MCPR_AOAI_API_KEY`.",
    "MCPR_AOAI_CHAT_DEPLOYMENT": "Deployment used by `MCPR_DECISION_BACKEND=aoai`.",
    "MCPR_AOAI_EMBEDDING_DEPLOYMENT": "Deployment used by `MCPR_EMBEDDING_BACKEND=aoai`.",
    "MCPR_AOAI_MAX_RETRIES": "Azure OpenAI retries (0 to 10).",
}

# Read by compose, the entrypoint, the image or the tooling, never by Settings.
DEPLOY_ONLY: tuple[tuple[str, str, str], ...] = (
    ("POSTGRES_USER", "mcprouter", "Database user created by the `db` service."),
    (
        "POSTGRES_PASSWORD",
        "",
        "Database password. Required from v0.6: compose refuses to start without it.",
    ),
    ("POSTGRES_DB", "mcprouter", "Database name."),
    ("MCPR_HOST_PORT", "8400", "Host port compose publishes the API on."),
    ("MCPR_BIND", "127.0.0.1", "From v0.6: host address compose publishes the API on."),
    (
        "MCPR_PORT",
        "8400",
        "Port uvicorn listens on inside the container (entrypoint, healthcheck).",
    ),
    ("MCPR_DB_WAIT_TRIES", "30", "Entrypoint database wait attempts, 2 s apart."),
    ("MCPR_DATA_DIR", "/data", "Entrypoint: directory probed for writability at start."),
    ("MCPR_IMAGE", "mcp-router:local", "Image tag compose builds and runs."),
    ("MCPR_BASE_IMAGE", "mcp-router:local", "Base image the inference flavor builds on."),
    ("MCPR_INFERENCE_IMAGE", "mcp-router:local-inference", "Inference flavor image tag."),
    (
        "MCPR_TORCH_INDEX_URL",
        "https://download.pytorch.org/whl/cu124",
        "GPU override: torch wheel index for the CUDA build.",
    ),
    ("HF_HOME", "/srv/models-cache/hf", "Hugging Face cache inside the image."),
)

TOOLING: tuple[tuple[str, str], ...] = (
    ("MCPR_RUN_SLOW", "Run the slow and live-model tests (`1`)."),
    ("MCPR_REQUIRE_DB", "From v0.6: fail instead of skip when the test database is down."),
    (
        "MCPR_ALLOW_ANY_DB",
        "From v0.6: let the suite wipe a database whose name has no `_test` word (`1`).",
    ),
    (
        "MCPR_ENFORCE_ROUTE_COVERAGE",
        "From v0.6: fail unless every API route was requested and returned a 2xx (`1`).",
    ),
    (
        "MCPR_TEST_BASE_DATABASE_URL",
        "Set by the suite itself: the operator's URL handed to pytest-xdist workers.",
    ),
    ("MCPR_AGENT_KEY", "`scripts/smoke.sh`: the key half of one `MCPR_AGENT_KEYS` pair."),
)

# SETTINGS_SPEC `type` -> the "Values" column when there are no choices.
_TYPE_NAMES = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "opt_int": "integer (optional)",
    "secret": "string",
    "upper": "string",
    "enum": "string",
    "device": "`auto`, `cpu`, `cuda` or `cuda:<index>`",
    "hosts": "comma list",
    "agent_keys": "comma list of `agentId:key`",
}


def render_default(value: Any) -> str:
    if value is None or value == "" or value == ():
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, tuple | list):
        return ",".join(str(v) for v in value)
    return str(value)


def from_spec(spec: Iterable[Any]) -> list[Var]:
    """One Var per SETTINGS_SPEC entry, plus one per `file_var` (a path)."""
    out = []
    for entry in spec:
        env = str(entry.env)
        out.append(
            Var(
                env=env,
                type=_TYPE_NAMES.get(str(entry.type), str(entry.type)),
                default=render_default(entry.default),
                choices=tuple(str(c) for c in entry.choices),
                secret=bool(entry.secret),
                doc=DOCS.get(env) or str(entry.doc),
            )
        )
        if entry.file_var:
            fv = str(entry.file_var)
            doc = DOCS.get(fv) or f"File holding `{env}` (Docker secret); wins over `{env}`."
            out.append(Var(fv, "path", "", (), True, doc))
    return out


def collect(mod: ModuleType | None = None) -> list[Var]:
    if mod is None:
        import mcprouter.settings

        mod = mcprouter.settings
    return from_spec(mod.SETTINGS_SPEC)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _code(text: str) -> str:
    return f"`{text}`" if text else "unset"


def render(app_vars: list[Var]) -> str:
    lines = [
        "<!-- GENERATED by scripts/gen_config_docs.py from src/mcprouter/settings.py. "
        "Do not edit by hand: run `make docs-gen`. -->",
        "",
        "# Configuration reference",
        "",
        "Every `MCPR_*` variable the router reads, with its default. An empty value counts",
        "as unset. Variables marked *secret* are never logged. Behaviour marked",
        '"from v0.6" ships in 0.6.0.',
        "",
        "For a deployment, put these in `.env` next to `docker-compose.yml`",
        "(template: `.env.example`). See [deploy.md](../deploy.md).",
        "",
        "## Router settings",
        "",
        "| Variable | Default | Values | Description |",
        "|---|---|---|---|",
    ]
    for v in app_vars:
        values = " | ".join(f"`{c}`" for c in v.choices) if v.choices else v.type
        if v.secret:
            values += ", secret"
        lines.append(f"| `{v.env}` | {_code(v.default)} | {_cell(values)} | {_cell(v.doc)} |")
    lines += [
        "",
        "## Compose, image and entrypoint",
        "",
        "Read by `docker-compose*.yml`, the `Dockerfile*` or `scripts/docker-entrypoint.sh`,",
        "not by the router process.",
        "",
        "| Variable | Default | Description |",
        "|---|---|---|",
    ]
    for env, default, doc in DEPLOY_ONLY:
        lines.append(f"| `{env}` | {_code(default)} | {_cell(doc)} |")
    lines += [
        "",
        "## Tests and tooling",
        "",
        "| Variable | Description |",
        "|---|---|",
    ]
    for env, doc in TOOLING:
        lines.append(f"| `{env}` | {_cell(doc)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    ap.add_argument("--out", type=Path, default=OUT, help="output path")
    args = ap.parse_args(argv)
    text = render(collect())
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != text:
            print(f"{args.out} is stale: run scripts/gen_config_docs.py", file=sys.stderr)
            return 1
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
