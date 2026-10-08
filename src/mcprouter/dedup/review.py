"""Human review of duplicate suggestions.

FR-05 guard: accepting or dismissing a suggestion ONLY records the decision on
the DuplicateSuggestion row. It never disables, deletes, or edits either tool;
acting on an accepted suggestion (e.g. disabling the non-preferred tool) is a
separate, explicit admin action via POST /api/v1/tools/{id}/disable.

The resolution (actor + justification) is appended to `rationale` because the
model has no resolution columns yet (see INTEGRATION_NOTES-registry.md).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mcprouter.models import DuplicateSuggestion, MCPServerRecord, MCPToolRecord
from mcprouter.registry.audit import audit
from mcprouter.registry.catalog import MAX_LIMIT
from mcprouter.registry.errors import InvalidArgument, InvalidTransition, SuggestionNotFound
from mcprouter.registry.wire import SuggestionOut, SuggestionToolRef, suggestion_out

STATUSES = ("open", "accepted", "dismissed")
MAX_JUSTIFICATION = 2000


@dataclass(frozen=True)
class SuggestionPage:
    items: list[SuggestionOut]
    total: int
    limit: int
    offset: int


def list_suggestions(
    session: Session, *, status: str | None = "open", limit: int = 50, offset: int = 0
) -> SuggestionPage:
    if status is not None and status not in STATUSES:
        raise InvalidArgument("status must be one of open, accepted, dismissed.")
    if not 1 <= limit <= MAX_LIMIT:
        raise InvalidArgument(f"limit must be between 1 and {MAX_LIMIT}.")
    if offset < 0:
        raise InvalidArgument("offset must be >= 0.")
    D = DuplicateSuggestion
    conds = [] if status is None else [D.status == status]
    total = session.execute(select(func.count()).select_from(D).where(*conds)).scalar_one()
    rows = list(
        session.scalars(
            select(D)
            .where(*conds)
            .order_by(D.similarity.desc(), D.created_at.desc(), D.id)
            .limit(limit)
            .offset(offset)
        )
    )
    refs = _tool_refs(session, rows)
    return SuggestionPage(
        items=[suggestion_out(r, refs) for r in rows], total=int(total), limit=limit, offset=offset
    )


def _tool_refs(session: Session, rows: list[DuplicateSuggestion]) -> dict[str, SuggestionToolRef]:
    tool_ids = {r.tool_a_id for r in rows} | {r.tool_b_id for r in rows}
    return {
        tid: SuggestionToolRef(id=tid, name=name, server_name=srv, enabled=enabled)
        for tid, name, srv, enabled in session.execute(
            select(
                MCPToolRecord.id, MCPToolRecord.name, MCPServerRecord.name, MCPToolRecord.enabled
            )
            .join(MCPServerRecord, MCPServerRecord.id == MCPToolRecord.server_id)
            .where(MCPToolRecord.id.in_(tool_ids))
        ).all()
    }


def to_out(session: Session, sug: DuplicateSuggestion) -> SuggestionOut:
    return suggestion_out(sug, _tool_refs(session, [sug]))


def _load_open(session: Session, suggestion_id: str) -> DuplicateSuggestion:
    sug = session.get(DuplicateSuggestion, suggestion_id, with_for_update=True)
    if sug is None:
        raise SuggestionNotFound()
    if sug.status != "open":
        raise InvalidTransition(f"Suggestion is already {sug.status}.")
    return sug


def _one_line(s: str) -> str:
    return " ".join(s.split())


def accept_suggestion(session: Session, suggestion_id: str, *, actor: str) -> DuplicateSuggestion:
    sug = _load_open(session, suggestion_id)
    sug.status = "accepted"
    sug.rationale = f"{sug.rationale}\nAccepted by {_one_line(actor)}."
    session.flush()
    audit("dedup.accept", actor=actor, suggestion_id=sug.id)
    return sug


def dismiss_suggestion(
    session: Session, suggestion_id: str, *, justification: str, actor: str
) -> DuplicateSuggestion:
    note = _one_line(justification)
    if not note:
        raise InvalidArgument("A justification is required to dismiss a suggestion.")
    if len(note) > MAX_JUSTIFICATION:
        raise InvalidArgument(f"Justification must be at most {MAX_JUSTIFICATION} characters.")
    sug = _load_open(session, suggestion_id)
    sug.status = "dismissed"
    sug.rationale = f"{sug.rationale}\nDismissed by {_one_line(actor)}: {note}"
    session.flush()
    audit("dedup.dismiss", actor=actor, suggestion_id=sug.id, justification=note)
    return sug
