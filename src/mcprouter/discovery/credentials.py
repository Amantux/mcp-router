"""Server-side credential storage for stdio servers (FR-07).

The table itself (`ServerCredentialRecord`, `mcp_server_credentials`) lives in
`mcprouter.models` (adopted at integration); this module keeps the access
helpers. Values are credentials: never logged, never returned by the API
(only the variable NAMES are exposed).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from mcprouter.models import ServerCredentialRecord

__all__ = ["ServerCredentialRecord", "env_names", "get_env", "set_env"]


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
