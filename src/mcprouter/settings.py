"""Env-driven settings. Pure resolution — no import-time env reads at module
scope beyond the registry call, no side effects (house rule: resolution is a
pure function so tests can build differently-configured apps in one process)."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg://mcprouter:mcprouter@localhost:5434/mcprouter"
    # Gateway auth: comma-separated "agent_id:key" pairs for v1 local API keys.
    # Empty = auth disabled with a loud startup warning (dev only).
    agent_keys: str = ""
    # Inference
    embedding_backend: str = "hash"  # hash | bge  (bge needs the [inference] extra)
    embedding_model_id: str = "BAAI/bge-small-en-v1.5"
    decision_backend: str = "deterministic"  # deterministic | laya | remote
    laya_model_id: str = "convaiinnovations/laya"
    device: str = "auto"  # auto | cuda | cpu
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
    # Execution
    default_tool_timeout_s: float = 30.0
    rate_limit_per_agent_per_min: int = 120
    # wave-3 remote decision backend (MCPR_DECISION_BACKEND=remote).
    # Endpoint is a full URL; with no path, /v1/decisions is appended. The key
    # comes from MCPR_DECISION_API_KEY_FILE (wins when both are set) or
    # MCPR_DECISION_API_KEY; excluded from repr so it never reaches a log.
    decision_endpoint: str = "https://api.aimlapi.com/v1/decisions"
    decision_model: str = "typesafe/jev"
    decision_api_key: str = field(default="", repr=False)
    decision_max_retries: int = 2

    @classmethod
    def from_env(cls) -> Settings:
        def get(name: str, default: str) -> str:
            v = os.environ.get(name, "")
            return v if v.strip() else default  # empty string means unset

        d = cls()
        return cls(
            database_url=get("MCPR_DATABASE_URL", d.database_url),
            agent_keys=get("MCPR_AGENT_KEYS", d.agent_keys),
            embedding_backend=get("MCPR_EMBEDDING_BACKEND", d.embedding_backend),
            embedding_model_id=get("MCPR_EMBEDDING_MODEL_ID", d.embedding_model_id),
            decision_backend=get("MCPR_DECISION_BACKEND", d.decision_backend),
            laya_model_id=get("MCPR_LAYA_MODEL_ID", d.laya_model_id),
            device=get("MCPR_DEVICE", d.device),
            operating_mode=get("MCPR_OPERATING_MODE", d.operating_mode),
            models_cache_dir=get("MCPR_MODELS_CACHE_DIR", d.models_cache_dir),
            idle_unload_s=float(get("MCPR_IDLE_UNLOAD_S", str(d.idle_unload_s))),
            embed_batch_size=int(get("MCPR_EMBED_BATCH_SIZE", str(d.embed_batch_size))),
            laya_noul_mode=get("MCPR_LAYA_NOUL_MODE", d.laya_noul_mode),
            max_exposed_tools=int(get("MCPR_MAX_EXPOSED_TOOLS", str(d.max_exposed_tools))),
            max_exposed_servers=_opt_int(get("MCPR_MAX_EXPOSED_SERVERS", "")),
            retrieval_candidates=int(get("MCPR_RETRIEVAL_CANDIDATES", str(d.retrieval_candidates))),
            route_confidence_floor=float(
                get("MCPR_ROUTE_CONFIDENCE_FLOOR", str(d.route_confidence_floor))
            ),
            decision_timeout_s=float(get("MCPR_DECISION_TIMEOUT_S", str(d.decision_timeout_s))),
            route_cache_ttl_s=float(get("MCPR_ROUTE_CACHE_TTL_S", str(d.route_cache_ttl_s))),
            route_cache_size=int(get("MCPR_ROUTE_CACHE_SIZE", str(d.route_cache_size))),
            sync_enabled=_bool(get("MCPR_SYNC_ENABLED", "false"), "MCPR_SYNC_ENABLED"),
            usage_prior_enabled=_bool(
                get("MCPR_USAGE_PRIOR_ENABLED", "false"), "MCPR_USAGE_PRIOR_ENABLED"
            ),
            analytics_rollup_enabled=_bool(
                get("MCPR_ANALYTICS_ROLLUP_ENABLED", "false"), "MCPR_ANALYTICS_ROLLUP_ENABLED"
            ),
            default_tool_timeout_s=float(
                get("MCPR_DEFAULT_TOOL_TIMEOUT_S", str(d.default_tool_timeout_s))
            ),
            rate_limit_per_agent_per_min=int(
                get("MCPR_RATE_LIMIT_PER_AGENT_PER_MIN", str(d.rate_limit_per_agent_per_min))
            ),
            # wave-3 remote decision backend
            decision_endpoint=get("MCPR_DECISION_ENDPOINT", d.decision_endpoint),
            decision_model=get("MCPR_DECISION_MODEL", d.decision_model),
            decision_api_key=_secret("MCPR_DECISION_API_KEY", get),
            decision_max_retries=_bounded_retries(
                get("MCPR_DECISION_MAX_RETRIES", str(d.decision_max_retries)),
                "MCPR_DECISION_MAX_RETRIES",
            ),
        )


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


def _opt_int(raw: str) -> int | None:
    """Empty/unset -> None. A set value must be a positive integer (fail loudly)."""
    if not raw.strip():
        return None
    value = int(raw)
    if value < 1:
        raise ValueError("must be a positive integer when set")
    return value


# wave-3 remote decision backend
def _secret(name: str, get: Callable[[str, str], str]) -> str:
    """<NAME>_FILE wins over <NAME>; surrounding whitespace is stripped. A set
    but unreadable, non-UTF-8 or oversized (> 64 KiB) file fails loudly; the
    message names the variable, never the content."""
    path = get(f"{name}_FILE", "")
    if not path:
        return get(name, "").strip()
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
    return text.strip()


_MAX_KEY_FILE_BYTES = 64 * 1024
_MAX_DECISION_RETRIES = 10


def _bounded_retries(raw: str, name: str) -> int:
    value = int(raw)
    if not 0 <= value <= _MAX_DECISION_RETRIES:
        raise ValueError(f"{name}: must be between 0 and {_MAX_DECISION_RETRIES}")
    return value
