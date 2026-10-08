"""Server-side credential storage for stdio servers (FR-07).

WORKAROUND (see docs/INTEGRATION_NOTES-discovery.md): ``models.py`` has no
column for a stdio server's environment, and this workstream may not edit
it. Credentials live in their own table instead — which is also the right
long-term shape: secrets stay out of the row every serializer touches.
Proposed for adoption into ``models.py`` at integration.

The table is registered on the shared ``Base`` metadata, so ``init_db()``
creates it as long as this module is imported first (the servers router
imports it).

Values are credentials: never logged, never returned by the API (only the
variable NAMES are exposed). Stored plaintext at rest — see notes.
"""

from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from mcprouter.models import Base


class ServerCredentialRecord(Base):
    __tablename__ = "mcp_server_credentials"

    server_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mcp_servers.id", ondelete="CASCADE"), primary_key=True
    )
    env: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    def __repr__(self) -> str:  # never render values
        return (
            f"ServerCredentialRecord(server_id={self.server_id!r}, env=<{len(self.env)} redacted>)"
        )


def get_env(session: Session, server_id: str) -> dict[str, str]:
    rec = session.get(ServerCredentialRecord, server_id)
    return dict(rec.env) if rec else {}


def env_names(session: Session, server_ids: list[str]) -> dict[str, list[str]]:
    """Variable NAMES per server (safe to expose) — one query for many servers."""
    if not server_ids:
        return {}
    rows = session.scalars(
        select(ServerCredentialRecord).where(ServerCredentialRecord.server_id.in_(server_ids))
    )
    return {r.server_id: sorted(r.env) for r in rows}


def set_env(session: Session, server_id: str, env: dict[str, str]) -> None:
    rec = session.get(ServerCredentialRecord, server_id)
    if rec is None:
        session.add(ServerCredentialRecord(server_id=server_id, env=dict(env)))
    else:
        rec.env = dict(env)
