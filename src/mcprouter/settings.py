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
    # Routing
    max_exposed_tools: int = 8
    retrieval_candidates: int = 20
    route_confidence_floor: float = 0.35
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
            max_exposed_tools=int(get("MCPR_MAX_EXPOSED_TOOLS", str(d.max_exposed_tools))),
            retrieval_candidates=int(get("MCPR_RETRIEVAL_CANDIDATES", str(d.retrieval_candidates))),
            route_confidence_floor=float(
                get("MCPR_ROUTE_CONFIDENCE_FLOOR", str(d.route_confidence_floor))
            ),
            default_tool_timeout_s=float(
                get("MCPR_DEFAULT_TOOL_TIMEOUT_S", str(d.default_tool_timeout_s))
            ),
            rate_limit_per_agent_per_min=int(
                get("MCPR_RATE_LIMIT_PER_AGENT_PER_MIN", str(d.rate_limit_per_agent_per_min))
            ),
        )
