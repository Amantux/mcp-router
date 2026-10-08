"""Server-name -> id resolution shared by the route API and the eval runner."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from mcprouter.models import MCPServerRecord


def resolve_server_names(s: Session, names: list[str]) -> tuple[list[str], list[str]]:
    """Returns (ids in input order, unknown names in input order, de-duplicated)."""
    wanted = list(dict.fromkeys(names))
    if not wanted:
        return [], []
    stmt = select(MCPServerRecord.name, MCPServerRecord.id).where(MCPServerRecord.name.in_(wanted))
    # Not dict(result): Result has .keys() (column names), so dict() misreads it.
    rows = {name: sid for name, sid in s.execute(stmt).tuples()}
    return [rows[n] for n in wanted if n in rows], [n for n in wanted if n not in rows]
