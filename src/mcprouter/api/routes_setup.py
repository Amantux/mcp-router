"""First-run setup wizard backend (admin-gated; open only in dev mode).

`app_settings` is a tiny key/value table on its OWN metadata (not models.Base),
created once by `db.init_db` (checkfirst) so it needs no Alembic step, never
touches the routing schema, and is never created on the request path. It holds only non-secret flags (setup completion); the
admin token and decision keys stay in env, never in the DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import String, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from mcprouter.api.deps_auth import SecurityConfig, require_admin
from mcprouter.models import AgentPrincipal, MCPServerRecord, MCPToolRecord, SkillSourceRecord

SETUP_COMPLETED_KEY = "setup.completed_at"


class _SettingsBase(DeclarativeBase):
    pass


class AppSetting(_SettingsBase):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(String(2000))


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


__all__ = ["SETUP_COMPLETED_KEY", "AppSetting", "router"]
