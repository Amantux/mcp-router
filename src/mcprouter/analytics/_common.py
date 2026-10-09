"""Catalog metadata shared by the analytics service and feedback (P-609).

Lives here (not in service.py) so feedback can import it at module level
instead of through a function-local import that dodged an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from mcprouter.models import (
    SKILL_ID_PREFIX,
    MCPServerRecord,
    MCPToolRecord,
    SkillRecord,
    SkillSourceRecord,
)


@dataclass(frozen=True)
class CatalogMeta:
    name: str
    server: str
    enabled: bool


def meta(session: Session) -> dict[str, CatalogMeta]:
    """Catalog metadata keyed by funnel id. Skills resolve name from
    SkillRecord and "server" from their SkillSourceRecord; enabled is the
    source's enabled flag (skills have no per-skill toggle)."""
    out = {
        tid: CatalogMeta(name, srv, enabled)
        for tid, name, srv, enabled in session.execute(
            select(
                MCPToolRecord.id, MCPToolRecord.name, MCPServerRecord.name, MCPToolRecord.enabled
            ).join(MCPServerRecord, MCPServerRecord.id == MCPToolRecord.server_id)
        ).all()
    }
    for sid, name, src, enabled in session.execute(
        select(
            SkillRecord.id, SkillRecord.name, SkillSourceRecord.name, SkillSourceRecord.enabled
        ).join(SkillSourceRecord, SkillSourceRecord.id == SkillRecord.source_id)
    ).all():
        out[SKILL_ID_PREFIX + sid] = CatalogMeta(name, src, enabled)
    return out
