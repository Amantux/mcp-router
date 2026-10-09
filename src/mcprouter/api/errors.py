"""App-wide exception handlers (wave-6 W0-3 seam, filled by E2 / P-204).

FastAPI's default 422 echoes each error's ``input`` (and ``ctx``), so a
validation failure on ``POST /servers`` would hand back the ``env`` secrets
the caller sent, into any proxy or client log of response bodies. Every
``/api/`` path gets a curated 422 instead: ``loc``, a capped ``msg`` and
``type`` only. Paths outside ``/api/`` keep FastAPI's default.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

API_PREFIX = "/api/"
MAX_ERRORS = 20
MAX_MSG = 200


async def curated_validation_error(request: Request, exc: Exception) -> Response:
    """422 WITHOUT echoing input on every ``/api/`` path."""
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registration guard
        raise exc
    if not request.url.path.startswith(API_PREFIX):
        return await request_validation_exception_handler(request, exc)
    detail = [
        {
            "loc": list(e.get("loc", ())),
            "msg": str(e.get("msg", ""))[:MAX_MSG],
            "type": e.get("type"),
        }
        for e in exc.errors()[:MAX_ERRORS]
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


def install_error_handlers(app: FastAPI) -> None:
    """Register app-wide exception handlers (idempotent)."""
    app.add_exception_handler(RequestValidationError, curated_validation_error)
