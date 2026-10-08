"""GET /api/v1/models/health (SPEC §9) — inference runtime status.

Thin router: everything comes from InferenceEngine.health(). The engine is
read from `app.state.inference_engine` (wired at integration; see
docs/INTEGRATION_NOTES-inference.md). The probe never loads models; it may
perform the battery-mode lazy idle check. Output carries backend names, model
repo ids, pinned revisions, device, mode, memory and semaphore stats — never
filesystem paths, settings values like the DB URL or API keys, or upstream
error text (fallback reasons are curated InferenceError messages).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from mcprouter.inference.engine import InferenceEngine

router = APIRouter(prefix="/api/v1/models", tags=["models"])


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


def get_inference_engine(request: Request) -> InferenceEngine:
    engine = getattr(request.app.state, "inference_engine", None)
    if not isinstance(engine, InferenceEngine):
        raise HTTPException(status_code=503, detail="inference engine is not configured")
    return engine


@router.get("/health", response_model=ModelsHealth)
def models_health(engine: InferenceEngine = Depends(get_inference_engine)) -> Any:
    return ModelsHealth.model_validate(engine.health())
