"""Env-driven settings. Pure resolution — no import-time env reads at module
scope beyond the registry call, no side effects (house rule: resolution is a
pure function so tests can build differently-configured apps in one process)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg://mcprouter:mcprouter@localhost:5434/mcprouter"
    # Gateway auth: comma-separated "agent_id:key" pairs for v1 local API keys.
    # Empty = auth disabled with a loud startup warning (dev only).
    agent_keys: str = ""
    # Inference
    embedding_backend: str = "hash"  # hash | bge  (bge needs the [inference] extra)
    embedding_model_id: str = "BAAI/bge-small-en-v1.5"
    decision_backend: str = "deterministic"  # deterministic | laya
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
    def from_env(cls) -> AoaiSettings:
        def get(name: str) -> str:
            v = os.environ.get(name, "")
            return v.strip() if v.strip() else ""  # empty string means unset

        key = ""
        key_file = get("MCPR_AOAI_API_KEY_FILE")
        if key_file:  # _FILE wins over the plain variable
            with open(key_file, encoding="utf-8") as fh:
                key = fh.read().strip()
            if not key:
                raise ValueError("MCPR_AOAI_API_KEY_FILE: file is empty")
        else:
            key = get("MCPR_AOAI_API_KEY")
        retries_raw = get("MCPR_AOAI_MAX_RETRIES") or "2"
        retries = int(retries_raw)
        if not 0 <= retries <= 10:
            raise ValueError("MCPR_AOAI_MAX_RETRIES: expected an integer in 0..10")
        return cls(
            endpoint=get("MCPR_AOAI_ENDPOINT"),
            api_key=key,
            chat_deployment=get("MCPR_AOAI_CHAT_DEPLOYMENT"),
            embedding_deployment=get("MCPR_AOAI_EMBEDDING_DEPLOYMENT"),
            max_retries=retries,
        )
