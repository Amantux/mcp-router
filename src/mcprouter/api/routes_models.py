"""GET /api/v1/models/health (SPEC §9) — inference runtime status.

Thin router: everything comes from InferenceEngine.health(). The engine is
read from `app.state.inference_engine` (wired at integration; see
docs/history/INTEGRATION_NOTES-inference.md). The probe never loads models; it may
perform the battery-mode lazy idle check. Output carries backend names, model
repo ids, pinned revisions, device, mode, memory and semaphore stats — never
filesystem paths, settings values like the DB URL or API keys, or upstream
error text (fallback reasons are curated InferenceError messages).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from mcprouter.api.deps_auth import require_admin
from mcprouter.inference.engine import InferenceEngine

# Admin-only: exposes model ids/revisions/package versions/RSS (operator
# information), and in battery mode the probe can unload models.
router = APIRouter(prefix="/api/v1/models", tags=["models"], dependencies=[Depends(require_admin)])


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class BackendHealth(_Camel):
    backend: str | None
    requested: str
    model_id: str | None = None
    revision: str | None = None
    precision: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    load_seconds: float | None = None


class ConcurrencyHealth(_Camel):
    capacity: int
    in_flight: int
    waiting: int
    acquired_total: int
    peak_in_flight: int
    avg_wait_ms: float


class IdleUnloadHealth(_Camel):
    enabled: bool
    idle_seconds: float
    idle_unloads: int
    seconds_since_last_use: float


class ModelsHealth(_Camel):
    status: str  # ok | degraded
    loaded: bool
    mode: str
    requested_device: str
    device: str
    device_note: str | None
    embedding: BackendHealth
    decision: BackendHealth
    concurrency: ConcurrencyHealth
    memory: dict[str, int | None]
    idle_unload: IdleUnloadHealth
    versions: dict[str, str | None]
    decision_backend: dict[str, str | None] | None = None
    embedding_backend: dict[str, str | None] | None = None


def get_inference_engine(request: Request) -> InferenceEngine:
    engine = getattr(request.app.state, "inference_engine", None)
    if not isinstance(engine, InferenceEngine):
        raise HTTPException(status_code=503, detail="inference engine is not configured")
    return engine


def _host(url: str) -> str | None:
    """Host only: never scheme userinfo, path, query or key."""
    try:
        return urlsplit(url).hostname or None
    except ValueError:
        return None


def backend_summary(settings: Any) -> tuple[dict[str, str | None], dict[str, str | None]]:
    """Configured (requested) backends; actual/fallback state is in `decision`/`embedding`."""
    from mcprouter.settings import AoaiSettings

    aoai: AoaiSettings | None = None
    if "aoai" in (settings.decision_backend, settings.embedding_backend):
        try:
            aoai = AoaiSettings.from_env()
        except ValueError:
            aoai = None
    dec: dict[str, str | None] = {"kind": settings.decision_backend, "model": None}
    if settings.decision_backend == "remote":
        dec |= {"model": settings.decision_model, "endpointHost": _host(settings.decision_endpoint)}
    elif settings.decision_backend == "aoai" and aoai is not None:
        dec |= {"endpointHost": _host(aoai.endpoint), "deployment": aoai.chat_deployment}
    emb: dict[str, str | None] = {"kind": settings.embedding_backend, "name": None}
    if settings.embedding_backend == "aoai" and aoai is not None:
        emb |= {"endpointHost": _host(aoai.endpoint), "deployment": aoai.embedding_deployment}
    return dec, emb


@router.get("/health", response_model=ModelsHealth)
def models_health(request: Request, engine: InferenceEngine = Depends(get_inference_engine)) -> Any:
    health = dict(engine.health())
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        dec, emb = backend_summary(settings)
        emb["name"] = (health.get("embedding") or {}).get("backend")
        if dec["model"] is None:
            dec["model"] = (health.get("decision") or {}).get("backend")
        health["decisionBackend"], health["embeddingBackend"] = dec, emb
    return ModelsHealth.model_validate(health)
