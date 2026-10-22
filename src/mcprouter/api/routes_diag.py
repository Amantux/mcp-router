"""Diagnostics API endpoint — GET /api/v1/diag (admin-token protected).

Returns aggregated health status for config, database, migrations and backends.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from mcprouter.api.deps_auth import require_admin
from mcprouter.diagnostics import run_diagnostics

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["diagnostics"], dependencies=[Depends(require_admin)])


async def get_inference_engine(request: Request) -> Any | None:
    """Dependency to get inference engine from app state."""
    return getattr(request.app.state, "inference_engine", None)


@router.get("/diag")
async def diagnostics(request: Request, inference: Any | None = Depends(get_inference_engine)) -> dict[str, Any]:
    """Get comprehensive health status for all components.

    Returns a single status field (healthy|degraded|unhealthy) along with
    per-check details. This endpoint is admin-protected like /metrics.

    Response shape:
        {
            "status": "healthy" | "degraded" | "unhealthy",
            "checks": [
                {"name": str, "ok": bool, "detail": str | None},
                ...
            ],
            "duration_ms": float
        }
    """
    # Get settings from app state
    settings = getattr(getattr(router, "app", None), "state", None)
    if settings is None:
        # Try to get from request
        try:
            settings = request.app.state.settings
        except AttributeError:
            settings = None

    if settings is None:
        return {
            "status": "unhealthy",
            "checks": [
                {"name": "config", "ok": False, "detail": "Settings not available"},
                {"name": "database", "ok": False, "detail": "Database not initialized"},
                {"name": "migrations", "ok": False, "detail": "Database not initialized"},
                {"name": "decisionBackend", "ok": False, "detail": "Inference engine not initialized"},
                {"name": "discovery", "ok": False, "detail": "Settings not available"},
            ],
            "duration_ms": 0.0,
        }

    # Run diagnostics
    result = await run_diagnostics(settings, inference)
    return result


def install_diag(app: FastAPI) -> None:
    """Register the diagnostics router."""
    app.include_router(router)
