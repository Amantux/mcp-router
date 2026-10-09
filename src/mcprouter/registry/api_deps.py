"""Re-export façade (wave-6 P-206). The FastAPI glue moved to
``mcprouter.api.deps`` (sessions), ``mcprouter.api.acting`` (admin actor) and
``mcprouter.api.errors`` (curated registry errors). Old imports keep resolving;
new code imports from ``mcprouter.api`` directly."""

from __future__ import annotations

from mcprouter.api.acting import ADMIN_ACTOR, DEV_ACTOR
from mcprouter.api.acting import admin_actor as require_admin
from mcprouter.api.deps import get_session
from mcprouter.api.errors import curated_errors

__all__ = ["ADMIN_ACTOR", "DEV_ACTOR", "curated_errors", "get_session", "require_admin"]
