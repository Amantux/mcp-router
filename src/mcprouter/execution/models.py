"""Gateway-owned tables, on their OWN metadata (models.py is a frozen contract).

`init_security_db(engine)` must be called after `db.init_db(engine)` (the
integrator wires it into create_app; Alembic adoption should fold this
metadata in alongside `models.Base.metadata`).

ApprovalRequest stores the RAW arguments server-side because the approved
call must run exactly what was requested (FR-07 "server-side credential
storage" covers this). They are never serialized out of the process — the API
returns only `summary` (redacted) — and are cleared once the request reaches a
terminal state.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Engine, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from mcprouter.models import utcnow


def _uuid() -> str:
    return str(uuid.uuid4())


class SecurityBase(DeclarativeBase):
    pass


# pending -> executing -> executed | failed ; pending -> denied | expired
APPROVAL_PENDING = "pending"
APPROVAL_EXECUTING = "executing"
APPROVAL_EXECUTED = "executed"
APPROVAL_FAILED = "failed"
APPROVAL_DENIED = "denied"
APPROVAL_EXPIRED = "expired"


class ApprovalRequest(SecurityBase):
    __tablename__ = "approval_requests"
    __table_args__ = (Index("ix_approval_agent_status", "agent_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(120))
    tool_id: Mapped[str] = mapped_column(String(36))
    server_id: Mapped[str] = mapped_column(String(36))
    # Schema the arguments were validated against; a changed schema voids the request.
    schema_hash: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # redacted
    status: Mapped[str] = mapped_column(String(16), default=APPROVAL_PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_record_id: Mapped[str | None] = mapped_column(String(36))
    # Redacted result preview for the requesting agent to poll.
    result_preview: Mapped[str | None] = mapped_column(Text)


def init_security_db(engine: Engine) -> None:
    SecurityBase.metadata.create_all(engine)
