"""First-run setup wizard backend (admin-gated; open only in dev mode).

`app_settings` is a tiny key/value table on its OWN metadata (not models.Base);
the ORM class lives in `models.py` (P-608) and is re-exported here. It is
never created on the request path. It holds only non-secret flags (setup completion); the
admin token and decision keys stay in env, never in the DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.deps_auth import SecurityConfig, require_admin
from mcprouter.models import (  # AppSetting/_SettingsBase re-exported (P-608 move)
    AgentPrincipal,
    AppSetting,
    MCPServerRecord,
    MCPToolRecord,
    SkillSourceRecord,
)
from mcprouter.models import SettingsBase as _SettingsBase

SETUP_COMPLETED_KEY = "setup.completed_at"


router = APIRouter(prefix="/api/v1/setup", tags=["setup"], dependencies=[Depends(require_admin)])


def _session(request: Request) -> Session:
    factory: sessionmaker[Session] = request.app.state.session_factory
    return factory()


def _count(session: Session, model: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    config: SecurityConfig = request.app.state.security
    with _session(request) as session:
        principals = _count(session, AgentPrincipal)
        completed = session.get(AppSetting, SETUP_COMPLETED_KEY)
        return {
            "needsSetup": principals == 0 and completed is None,
            "completedAt": completed.value if completed else None,
            "hasAdminToken": config.admin_token_hash is not None,
            "devMode": config.dev_mode_possible,
            "backend": request.app.state.settings.decision_backend,
            "counts": {
                "principals": principals,
                "servers": _count(session, MCPServerRecord),
                "tools": _count(session, MCPToolRecord),
                "skillSources": _count(session, SkillSourceRecord),
            },
        }


@router.post("/complete")
def complete(request: Request) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    with _session(request) as session:
        # Atomic and idempotent: the first completion time wins, and a concurrent
        # POST no-ops on the PK instead of a read-then-insert IntegrityError.
        session.execute(
            pg_insert(AppSetting)
            .values(key=SETUP_COMPLETED_KEY, value=now)
            .on_conflict_do_nothing(index_elements=[AppSetting.key])
        )
        session.commit()
        stored = session.scalar(
            select(AppSetting.value).where(AppSetting.key == SETUP_COMPLETED_KEY)
        )
    return {"completed": True, "completedAt": stored or now}


__all__ = ["SETUP_COMPLETED_KEY", "AppSetting", "_SettingsBase", "router"]
