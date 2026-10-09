"""Env-driven settings. Pure resolution — no import-time env reads at module
scope beyond the registry call, no side effects (house rule: resolution is a
pure function so tests can build differently-configured apps in one process)."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from typing import Any


@dataclass(frozen=True)
class Settings:
    # Secrets (database_url carries the password) never appear in repr().
    database_url: str = field(
        default="postgresql+psycopg://mcprouter:mcprouter@localhost:5434/mcprouter", repr=False
    )
    # Gateway auth: comma-separated "agent_id:key" pairs for v1 local API keys.
    # Empty = auth disabled with a loud startup warning (dev only).
    agent_keys: str = field(default="", repr=False)
    # Inference
    embedding_backend: str = "hash"  # hash | bge | aoai  (bge needs the [inference] extra)
    embedding_model_id: str = "BAAI/bge-small-en-v1.5"
    decision_backend: str = "deterministic"  # deterministic | laya | remote | aoai
    laya_model_id: str = "convaiinnovations/laya"
    device: str = "auto"  # auto | cpu | cuda | cuda:<index>
    operating_mode: str = "balanced"  # performance | balanced | battery
    models_cache_dir: str = "./models-cache"
    idle_unload_s: float = 300.0  # battery mode only
    embed_batch_size: int = 32
    laya_noul_mode: str = "choice"  # choice | native
    # Routing
    max_exposed_tools: int = 8
    # Optional global cap on DISTINCT servers in one exposure (None = unlimited).
    max_exposed_servers: int | None = None
    retrieval_candidates: int = 20
    route_confidence_floor: float = 0.35
    # In-process route cache (routing/cache.py). 0 on either knob disables it.
    route_cache_ttl_s: float = 60.0
    route_cache_size: int = 1024
    # Per-call deadline for decision-model questions; a hung model becomes an
    # InferenceError and the route falls back deterministically (FR-06).
    decision_timeout_s: float = 2.0
    # Discovery: run the background SyncLoop (catalog sync + health) inside
    # the app process. Off by default = manual refresh only (v0.1 behaviour).
    sync_enabled: bool = False
    # Analytics. The usage prior (analytics/prior.py) is a bounded score nudge,
    # NOT wired into routing yet; this is its one switch. The rollup loop
    # recomputes tool_stats_daily once a day inside the app (alternative:
    # cron POST /api/v1/analytics/rollup). Both off by default.
    usage_prior_enabled: bool = False
    analytics_rollup_enabled: bool = False
    # Savings ESTIMATES (docs/analytics.md). 0 = not configured -> the estimate is
    # reported as null, never as a measured 0.
    prefill_ms_per_1k_tokens: float = 0.0
    price_per_1k_input_tokens: float = 0.0
    currency: str = "USD"
    # Execution
    default_tool_timeout_s: float = 30.0
    rate_limit_per_agent_per_min: int = 120
    # Wave 4: Agent Skills
    max_exposed_skills: int = 3
    skill_body_max_bytes: int = 65536
    skill_resource_max_bytes: int = 5 * 1024 * 1024
    skills_cache_dir: str = "./skills-cache"  # where git skill sources are cloned
    # wave-3 remote decision backend (MCPR_DECISION_BACKEND=remote).
    # Endpoint is a full URL; with no path, /v1/decisions is appended. The key
    # comes from MCPR_DECISION_API_KEY_FILE (wins when both are set) or
    # MCPR_DECISION_API_KEY; excluded from repr so it never reaches a log.
    decision_endpoint: str = "https://api.aimlapi.com/v1/decisions"
    decision_model: str = "typesafe/jev"
    decision_api_key: str = field(default="", repr=False)
    decision_max_retries: int = 2
    # wave-5: built dashboard served at / (MCPR_UI_DIST), and extra Host values
    # the /mcp gateway accepts beyond the localhost set (MCPR_ALLOWED_HOSTS).
    ui_dist: str = "./ui/dist"
    allowed_hosts: tuple[str, ...] = ()
    # wave-6: validated through SETTINGS_SPEC (E1); E3/E6 consume the limits.
    admin_token: str = field(default="", repr=False)  # MCPR_ADMIN_TOKEN (secret)
    log_level: str = "INFO"
    log_format: str = "console"  # console | json
    allow_open_dev: bool = False
    mcp_max_sessions: int = 1000
    mcp_max_sessions_per_agent: int = 32
    decision_rate_limit_per_min: int = 120
    dedup_max_pairs: int = 5000
    # wave-3 Azure OpenAI backends (folded in from AoaiSettings, wave-6 E1).
    # The endpoint is validated at point of use (inference/aoai.py).
    aoai_endpoint: str = ""
    aoai_api_key: str = field(default="", repr=False)
    aoai_chat_deployment: str = ""
    aoai_embedding_deployment: str = ""
    aoai_max_retries: int = 2

    def aoai(self) -> AoaiSettings:
        """The Azure OpenAI backend's config view of these settings."""
        return AoaiSettings(
            endpoint=self.aoai_endpoint,
            api_key=self.aoai_api_key,
            chat_deployment=self.aoai_chat_deployment,
            embedding_deployment=self.aoai_embedding_deployment,
            max_retries=self.aoai_max_retries,
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Parse every SETTINGS_SPEC entry from `env` (default os.environ).
        Empty string means unset. Any invalid value raises ValueError whose
        message names the variable and never echoes the value."""
        source = os.environ if env is None else env

        def get(name: str, default: str) -> str:
            v = source.get(name, "")
            return v if v.strip() else default  # empty string means unset

        return cls(**{spec.name: _parse(spec, get) for spec in SETTINGS_SPEC})


def _host_list(raw: str) -> tuple[str, ...]:
    """MCPR_ALLOWED_HOSTS: comma list of host or host:port (``host:*`` = any port).
    Strict charset so a value can never smuggle header syntax or a wildcard host."""
    out: list[str] = []
    for item in (p.strip().lower() for p in raw.split(",")):
        if not item:
            continue
        if not re.fullmatch(
            r"(\[[0-9a-f:]+\]|[a-z0-9.-]*[a-z0-9][a-z0-9.-]*)(:(\d{1,5}|\*))?", item
        ):
            raise ValueError(f"MCPR_ALLOWED_HOSTS: invalid host {item!r}")
        out.append(item)
    return tuple(out)


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _bool(raw: str, name: str = "value") -> bool:
    """Strict boolean: an unrecognised value fails loudly at startup rather
    than silently meaning false."""
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError(f"{name}: expected one of 1/0, true/false, yes/no, on/off")


# wave-3 remote decision backend
def _secret(name: str, get: Callable[[str, str], str], *, printable: bool = True) -> str:
    """<NAME>_FILE wins over <NAME>; surrounding whitespace is stripped. A set
    but unreadable, non-UTF-8 or oversized (> 64 KiB) file fails loudly; the
    message names the variable, never the content."""
    path = get(f"{name}_FILE", "")
    if not path:
        plain = get(name, "").strip()
        return _printable_key(plain, name) if printable else _no_controls(plain, name)
    failed = False
    data = b""
    try:
        with open(path, "rb") as fh:
            data = fh.read(_MAX_KEY_FILE_BYTES + 1)
    except OSError:
        failed = True
    if failed:
        raise ValueError(f"{name}_FILE: cannot read the key file")
    if len(data) > _MAX_KEY_FILE_BYTES:
        raise ValueError(f"{name}_FILE: key file is larger than 64 KiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        failed = True
    if failed:
        raise ValueError(f"{name}_FILE: key file is not UTF-8 text")
    if not text.strip():
        raise ValueError(f"{name}_FILE: key file is empty")
    text = text.strip()
    return _printable_key(text, name) if printable else _no_controls(text, name)


_KEY_RE = re.compile(r"[\x21-\x7e]*")
_NO_CONTROLS_RE = re.compile(r"[\x20-\x7e]*")
MIN_KEY_LEN = 32


def _no_controls(value: str, name: str) -> str:
    """Printable ASCII, spaces allowed (comma lists). Never echoes the value."""
    if not _NO_CONTROLS_RE.fullmatch(value):
        raise ValueError(f"{name}: must be printable ASCII")
    return value


def _printable_key(key: str, name: str) -> str:
    """A key goes into an HTTP header: printable ASCII, no whitespace/control.
    The message names the variable, never the key."""
    if not _KEY_RE.fullmatch(key):
        raise ValueError(f"{name}: must be printable ASCII with no whitespace")
    return key


_MAX_KEY_FILE_BYTES = 64 * 1024


# --- settings registry (P-105) -----------------------------------------------
# SETTINGS_SPEC is the ONE declaration of every MCPR_* variable `Settings`
# reads: `from_env` is driven by it, and scripts/gen_config_docs.py (E7)
# renders docs/reference/configuration.md from it. ENV_ONLY_SPEC lists the
# variables read only by the container entrypoint or by compose interpolation.
#
# Shape (plain data, importable without side effects):
#   env       variable name             name     Settings attribute ("" = none)
#   type      str|upper|int|float|bool|enum|opt_int|hosts|secret|device
#   default   the parsed default        doc      one-line operator description
#   choices   accepted values (enum/device/bool)  lo/hi  inclusive numeric bounds
#             (secret/agent_keys: lo = minimum key length, env/_FILE path only)
#   secret    never logged / never in repr        file_var  "<ENV>_FILE" or None
#   scope     app | entrypoint | compose

EMBEDDING_BACKEND_CHOICES = ("hash", "bge", "aoai")
DECISION_BACKEND_CHOICES = ("deterministic", "laya", "remote", "aoai")
OPERATING_MODE_CHOICES = ("performance", "balanced", "battery")
LAYA_NOUL_MODE_CHOICES = ("choice", "native")
LOG_FORMAT_CHOICES = ("console", "json")
LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEVICE_CHOICES = ("auto", "cpu", "cuda", "cuda:<index>")
_BOOL_CHOICES = ("true", "false")
_DEVICE_RE = re.compile(r"auto|cpu|cuda(:\d{1,2})?")


@dataclass(frozen=True)
class SettingSpec:
    env: str
    name: str
    type: str
    default: object
    doc: str
    choices: tuple[str, ...] = ()
    lo: float | None = None
    hi: float | None = None
    secret: bool = False
    file_var: str | None = None
    scope: str = "app"


_FIELD_DEFAULTS = {f.name: f.default for f in fields(Settings)}


def _s(name: str, type_: str, doc: str, **kw: object) -> SettingSpec:
    env = f"MCPR_{name.upper()}"
    if type_ == "bool":
        kw.setdefault("choices", _BOOL_CHOICES)
    if kw.get("file_var") is True:
        kw["file_var"] = f"{env}_FILE"
    return SettingSpec(env, name, type_, _FIELD_DEFAULTS[name], doc, **kw)  # type: ignore[arg-type]


SETTINGS_SPEC: tuple[SettingSpec, ...] = (
    # core / security
    _s(
        "database_url",
        "secret",
        "SQLAlchemy Postgres DSN (compose derives it).",
        secret=True,
        file_var=True,
    ),
    _s(
        "agent_keys",
        "agent_keys",
        "Comma list of agent_id:key API keys (each key >= 32 chars).",
        secret=True,
        file_var=True,
        lo=MIN_KEY_LEN,
    ),
    _s(
        "admin_token",
        "secret",
        "Bearer token for the admin API, >= 32 chars (unset: loopback bind).",
        secret=True,
        file_var=True,
        lo=MIN_KEY_LEN,
    ),
    _s("allowed_hosts", "hosts", "Extra Host names accepted besides loopback (421 otherwise)."),
    _s("allow_open_dev", "bool", "Entrypoint: bind 0.0.0.0 even without an admin token."),
    _s("log_level", "enum", "Root log level.", choices=LOG_LEVEL_CHOICES),
    _s("log_format", "enum", "Log line format (compose sets json).", choices=LOG_FORMAT_CHOICES),
    # inference
    _s("embedding_backend", "enum", "Embedding backend.", choices=EMBEDDING_BACKEND_CHOICES),
    _s("embedding_model_id", "str", "Hugging Face model id for the bge backend."),
    _s("decision_backend", "enum", "Decision backend.", choices=DECISION_BACKEND_CHOICES),
    _s("laya_model_id", "str", "Hugging Face model id for the laya backend."),
    _s("device", "device", "Torch device for local models.", choices=DEVICE_CHOICES),
    _s("operating_mode", "enum", "Inference concurrency profile.", choices=OPERATING_MODE_CHOICES),
    _s("models_cache_dir", "str", "Model download cache directory."),
    _s(
        "idle_unload_s",
        "float",
        "Battery mode: unload models after this idle time.",
        lo=1,
        hi=86400,
    ),
    _s("embed_batch_size", "int", "Embedding batch size.", lo=1, hi=4096),
    _s("laya_noul_mode", "enum", "Laya no-UL question mode.", choices=LAYA_NOUL_MODE_CHOICES),
    _s("decision_timeout_s", "float", "Deadline per decision-model call.", lo=0.01, hi=600),
    # routing
    _s("max_exposed_tools", "int", "Tools exposed per route.", lo=1, hi=1000),
    _s("max_exposed_servers", "opt_int", "Distinct servers per route (empty: no cap).", lo=1),
    _s("retrieval_candidates", "int", "Retriever candidates before the decision.", lo=1, hi=10000),
    _s("route_confidence_floor", "float", "Minimum route confidence.", lo=0, hi=1),
    _s("route_cache_ttl_s", "float", "Route cache TTL (0 disables).", lo=0, hi=86400),
    _s("route_cache_size", "int", "Route cache entries (0 disables).", lo=0, hi=1_000_000),
    # discovery / analytics
    _s("sync_enabled", "bool", "Run the background catalog sync + health loop."),
    _s("usage_prior_enabled", "bool", "Enable the bounded usage-prior score nudge."),
    _s("analytics_rollup_enabled", "bool", "Run the daily tool_stats rollup loop."),
    _s("prefill_ms_per_1k_tokens", "float", "Savings estimate: prefill ms per 1k tokens.", lo=0),
    _s("price_per_1k_input_tokens", "float", "Savings estimate: price per 1k tokens.", lo=0),
    _s("currency", "upper", "Currency code for savings estimates."),
    # execution / gateway
    _s("default_tool_timeout_s", "float", "Default tool-call timeout.", lo=0.01, hi=3600),
    _s(
        "rate_limit_per_agent_per_min",
        "int",
        "Tool calls per agent per minute.",
        lo=1,
        hi=1_000_000,
    ),
    _s("mcp_max_sessions", "int", "Live MCP sessions (all agents).", lo=1, hi=1_000_000),
    _s("mcp_max_sessions_per_agent", "int", "Live MCP sessions per agent.", lo=1, hi=100_000),
    # skills
    _s("max_exposed_skills", "int", "Skills exposed per route.", lo=0, hi=100),
    _s("skill_body_max_bytes", "int", "SKILL.md body cap.", lo=1, hi=64 * 1024 * 1024),
    _s("skill_resource_max_bytes", "int", "Per-resource serve cap.", lo=1, hi=1024 * 1024 * 1024),
    _s("skills_cache_dir", "str", "Where git skill sources are cloned."),
    # remote decision backend
    _s("decision_endpoint", "str", "Remote decision endpoint URL (https; http only to loopback)."),
    _s("decision_model", "str", "Remote decision model name."),
    _s("decision_api_key", "secret", "Remote decision API key.", secret=True, file_var=True),
    _s("decision_max_retries", "int", "Remote decision retries.", lo=0, hi=10),
    _s(
        "decision_rate_limit_per_min",
        "int",
        "Decision edge calls per principal/min.",
        lo=1,
        hi=1_000_000,
    ),
    # Azure OpenAI backend
    _s("aoai_endpoint", "str", "Azure OpenAI endpoint (https://<resource>.openai.azure.com)."),
    _s("aoai_api_key", "secret", "Azure OpenAI API key.", secret=True, file_var=True),
    _s("aoai_chat_deployment", "str", "Azure OpenAI chat deployment (decisions)."),
    _s("aoai_embedding_deployment", "str", "Azure OpenAI embedding deployment."),
    _s("aoai_max_retries", "int", "Azure OpenAI retries.", lo=0, hi=10),
    # dashboard / dedup
    _s("ui_dist", "str", "Built dashboard directory served at /."),
    _s("dedup_max_pairs", "int", "Cap on duplicate pairs per dedup run.", lo=1, hi=10_000_000),
)


def _e(env: str, type_: str, default: object, doc: str, scope: str, **kw: object) -> SettingSpec:
    return SettingSpec(env, "", type_, default, doc, scope=scope, **kw)  # type: ignore[arg-type]


ENV_ONLY_SPEC: tuple[SettingSpec, ...] = (
    _e(
        "MCPR_DB_WAIT_TRIES",
        "int",
        30,
        "Postgres wait attempts (2 s apart).",
        "entrypoint",
        lo=1,
        hi=1000,
    ),
    _e(
        "MCPR_PORT",
        "int",
        8400,
        "Port uvicorn listens on inside the container.",
        "entrypoint",
        lo=1,
        hi=65535,
    ),
    _e("MCPR_DATA_DIR", "str", "/data", "Directory probed for writability at start.", "entrypoint"),
    _e("MCPR_BIND", "str", "127.0.0.1", "Host interface compose publishes the API on.", "compose"),
    _e(
        "MCPR_HOST_PORT",
        "int",
        8400,
        "Host port compose publishes the API on.",
        "compose",
        lo=1,
        hi=65535,
    ),
    _e("MCPR_IMAGE", "str", "mcp-router:local", "Image tag of the base flavor.", "compose"),
    _e(
        "MCPR_BASE_IMAGE",
        "str",
        "mcp-router:local",
        "Base image the inference build extends.",
        "compose",
    ),
    _e(
        "MCPR_INFERENCE_IMAGE",
        "str",
        "mcp-router:local-inference",
        "Image tag of the inference flavor.",
        "compose",
    ),
    _e(
        "MCPR_TORCH_INDEX_URL",
        "str",
        "https://download.pytorch.org/whl/cu124",
        "Torch wheel index (GPU build).",
        "compose",
    ),
)


def _agent_keys(spec: SettingSpec, get: Callable[[str, str], str]) -> str:
    """`id:key,id:key` (spaces around commas allowed). Each key must meet the
    minimum length; malformed entries are left to auth's parse_agent_keys,
    whose messages never contain key material either."""
    entries = [p.strip() for p in _secret(spec.env, get, printable=False).split(",")]
    entries = [e for e in entries if e]
    for idx, entry in enumerate(entries, 1):
        _, sep, key = entry.partition(":")
        if sep and spec.lo is not None and len(key) < spec.lo:
            raise ValueError(
                f"{spec.env}: entry #{idx} key must be at least {spec.lo:g} characters"
            )
    return ",".join(entries)


def _num(raw: str, spec: SettingSpec) -> float:
    try:
        value = int(raw) if spec.type in ("int", "opt_int") else float(raw)
    except ValueError:
        kind = "an integer" if spec.type in ("int", "opt_int") else "a number"
        raise ValueError(f"{spec.env}: must be {kind}") from None
    if not math.isfinite(value):
        raise ValueError(f"{spec.env}: must be a finite number")
    lo, hi = spec.lo, spec.hi
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        bounds = f">= {lo:g}" if hi is None else f"between {lo:g} and {hi:g}"
        raise ValueError(f"{spec.env}: must be {bounds}")
    return value


def _parse(spec: SettingSpec, get: Callable[[str, str], str]) -> Any:
    """One registry entry -> its typed value. Messages name the variable and
    never echo the value."""
    if spec.type == "secret":
        value = _secret(spec.env, get)
        if value and spec.lo is not None and len(value) < spec.lo:
            raise ValueError(f"{spec.env}: must be at least {spec.lo:g} characters")
        return value or spec.default
    if spec.type == "agent_keys":
        return _agent_keys(spec, get)
    raw = get(spec.env, "").strip()
    if not raw:
        return spec.default
    match spec.type:
        case "str":
            return raw
        case "upper":
            return raw.upper()
        case "int" | "opt_int":
            return int(_num(raw, spec))
        case "float":
            return _num(raw, spec)
        case "bool":
            return _bool(raw, spec.env)
        case "hosts":
            return _host_list(raw)
        case "device":
            if not _DEVICE_RE.fullmatch(raw.lower()):
                raise ValueError(f"{spec.env}: must be auto, cpu, cuda or cuda:<index>")
            return raw.lower()
        case "enum":
            value = raw.upper() if spec.choices[0].isupper() else raw.lower()
            if value not in spec.choices:
                raise ValueError(f"{spec.env}: must be one of {' | '.join(spec.choices)}")
            return value
    raise AssertionError(f"unknown setting type {spec.type!r}")  # pragma: no cover


# --- container entrypoint adapter (D1) -------------------------------------
# The shell never re-parses config: scripts/docker-entrypoint.sh asks this one
# tested function for the resolved bind host and DB-wait budget.

LOOPBACK_BIND = "127.0.0.1"
ALL_INTERFACES_BIND = "0.0.0.0"  # noqa: S104 - deliberate when a token is set (D1)


@dataclass(frozen=True)
class EntrypointPlan:
    bind_host: str
    db_wait_tries: int
    warnings: tuple[str, ...]
    port: int = 8400


def entrypoint_plan(settings: Settings, env: Mapping[str, str]) -> EntrypointPlan:
    """D1 fail-closed posture. Admin token set -> all interfaces. No token ->
    loopback only (the admin API is open in dev mode, or locked with agent keys)
    unless MCPR_ALLOW_OPEN_DEV explicitly opts back in to all interfaces."""

    def get(name: str, default: str) -> str:
        v = env.get(name, "")
        return v if v.strip() else default

    parsed = {spec.env: _parse(spec, get) for spec in ENV_ONLY_SPEC if spec.scope == "entrypoint"}
    tries = int(parsed["MCPR_DB_WAIT_TRIES"])
    port = int(parsed["MCPR_PORT"])
    if settings.admin_token:
        warn: tuple[str, ...] = ()
        if not settings.allowed_hosts:
            warn = (
                "listening on all interfaces but MCPR_ALLOWED_HOSTS is empty: "
                "requests by any non-loopback name get 421",
            )
        return EntrypointPlan(ALL_INTERFACES_BIND, tries, warn, port)
    state = "locked (agent keys set)" if settings.agent_keys.strip() else "open"
    if settings.allow_open_dev:
        return EntrypointPlan(
            ALL_INTERFACES_BIND,
            tries,
            (
                f"admin API {state}; MCPR_ALLOW_OPEN_DEV=1 binds ALL interfaces; "
                "set MCPR_ADMIN_TOKEN for any shared host",
            ),
            port,
        )
    return EntrypointPlan(
        LOOPBACK_BIND,
        tries,
        (f"admin API {state}; listening on loopback only; set MCPR_ADMIN_TOKEN",),
        port,
    )


def _entrypoint_main() -> int:
    """`python -m mcprouter.settings entrypoint`: prints "<host> <tries> <port>" on
    stdout, warnings on stderr. Invalid settings -> exit 2 with a FATAL line
    that names the variable (never its value)."""
    import sys

    try:
        plan = entrypoint_plan(Settings.from_env(), os.environ)
    except ValueError as exc:
        print(f"[entrypoint] FATAL: {exc}", file=sys.stderr)
        return 2
    for w in plan.warnings:
        print(f"[entrypoint] WARNING: {w}", file=sys.stderr)
    print(f"{plan.bind_host} {plan.db_wait_tries} {plan.port}")
    return 0


# wave-3 Azure OpenAI backends
# Self-contained so it never conflicts with other appended blocks. Selected by
# MCPR_DECISION_BACKEND=aoai and/or MCPR_EMBEDDING_BACKEND=aoai (Settings above
# carries those strings unchanged; the wiring run maps "aoai" to
# mcprouter.inference.aoai). The endpoint is NOT validated here: it is
# validated at point of use (inference/aoai.py) because env is not the only
# way config arrives.
@dataclass(frozen=True)
class AoaiSettings:
    endpoint: str = ""  # https://<resource>.openai.azure.com | *.services.ai.azure.com
    api_key: str = ""
    chat_deployment: str = ""
    embedding_deployment: str = ""
    max_retries: int = 2

    def __repr__(self) -> str:  # never print the key
        return (
            f"AoaiSettings(endpoint={self.endpoint!r}, api_key_set={bool(self.api_key)}, "
            f"chat_deployment={self.chat_deployment!r}, "
            f"embedding_deployment={self.embedding_deployment!r}, max_retries={self.max_retries})"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AoaiSettings:
        """Only the MCPR_AOAI_* subset of SETTINGS_SPEC (unrelated settings
        cannot break the AOAI backend)."""
        source = os.environ if env is None else env

        def get(name: str, default: str) -> str:
            v = source.get(name, "")
            return v if v.strip() else default

        values = {
            spec.name: _parse(spec, get)
            for spec in SETTINGS_SPEC
            if spec.env.startswith("MCPR_AOAI_")
        }
        return Settings(**values).aoai()


if __name__ == "__main__":  # pragma: no cover - exercised by tests/test_entrypoint.py
    import sys

    if sys.argv[1:] != ["entrypoint"]:
        print("usage: python -m mcprouter.settings entrypoint", file=sys.stderr)
        raise SystemExit(64)
    raise SystemExit(_entrypoint_main())
