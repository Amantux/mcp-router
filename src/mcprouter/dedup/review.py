"""Human review of duplicate suggestions.

FR-05 guard: accepting or dismissing a suggestion ONLY records the decision on
the DuplicateSuggestion row. It never disables, deletes, or edits either tool;
acting on an accepted suggestion (e.g. disabling the non-preferred tool) is a
separate, explicit admin action via POST /api/v1/tools/{id}/disable.

The resolution (actor + justification) is appended to `rationale` because the
model has no resolution columns yet (see docs/history/INTEGRATION_NOTES-registry.md).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from mcprouter.analytics.funnel import merged_funnel, pair_evidence
from mcprouter.analytics.window import parse_window
from mcprouter.models import (
    SKILL_ID_PREFIX,
    DuplicateSuggestion,
    MCPServerRecord,
    MCPToolRecord,
    SkillRecord,
    SkillSourceRecord,
    utcnow,
)
from mcprouter.registry.audit import audit
from mcprouter.registry.catalog import MAX_LIMIT
from mcprouter.registry.errors import (
    InvalidArgument,
    InvalidTransition,
    RegistryError,
    SuggestionNotFound,
)
from mcprouter.registry.wire import (
    SuggestionEvidence,
    SuggestionOut,
    SuggestionToolRef,
    suggestion_out,
)

STATUSES = ("open", "accepted", "dismissed")
MAX_JUSTIFICATION = 2000
EVIDENCE_WINDOW = "30d"

log = logging.getLogger(__name__)


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
    items = [suggestion_out(r, refs) for r in rows]
    evidence = _usage_evidence(session, [r for r in rows if r.status == "open"])
    items = [
        it.model_copy(update={"usage_evidence": evidence[it.id]}) if it.id in evidence else it
        for it in items
    ]
    return SuggestionPage(items=items, total=int(total), limit=limit, offset=offset)


def _usage_evidence(
    session: Session, rows: list[DuplicateSuggestion]
) -> dict[str, SuggestionEvidence]:
    """Read-only routing evidence (wave-2 analytics) for open pairs."""
    if not rows:
        return {}
    window = parse_window(EVIDENCE_WINDOW, utcnow())
    involved = sorted({r.tool_a_id for r in rows} | {r.tool_b_id for r in rows})
    try:
        # SAVEPOINT: evidence is optional decoration; an analytics failure
        # (e.g. statement timeout) degrades to usageEvidence=null instead of
        # failing the core review listing.
        with session.begin_nested():
            funnel = merged_funnel(session, window, {}, only=involved)
            pairs = pair_evidence(session, window, [(r.tool_a_id, r.tool_b_id) for r in rows])
    except SQLAlchemyError as exc:
        log.warning("dedup.usage_evidence_failed exc_type=%s", type(exc).__name__)
        return {}
    out: dict[str, SuggestionEvidence] = {}
    for r in rows:
        ev = pairs.get((r.tool_a_id, r.tool_b_id))
        fa, fb = funnel.get(r.tool_a_id), funnel.get(r.tool_b_id)
        out[r.id] = SuggestionEvidence(
            window=EVIDENCE_WINDOW,
            tool_a_surfaced=fa.surfaced if fa else 0,
            tool_b_surfaced=fb.surfaced if fb else 0,
            co_surfaced=ev.co_surfaced if ev else 0,
            tool_a_selected=ev.a_selected if ev else 0,
            tool_b_selected=ev.b_selected if ev else 0,
            both_selected=ev.both_selected if ev else 0,
        )
    return out


def _tool_refs(session: Session, rows: list[DuplicateSuggestion]) -> dict[str, SuggestionToolRef]:
    """Resolve both kinds: bare ids are tools (serverName = server), "skill:<id>"
    refs are skills (serverName = skill source name; id keeps the prefix)."""
    refs = {r.tool_a_id for r in rows} | {r.tool_b_id for r in rows}
    tool_ids = {x for x in refs if not x.startswith(SKILL_ID_PREFIX)}
    skill_ids = {x[len(SKILL_ID_PREFIX) :] for x in refs if x.startswith(SKILL_ID_PREFIX)}
    out = {
        tid: SuggestionToolRef(id=tid, name=name, server_name=srv, enabled=enabled)
        for tid, name, srv, enabled in session.execute(
            select(
                MCPToolRecord.id, MCPToolRecord.name, MCPServerRecord.name, MCPToolRecord.enabled
            )
            .join(MCPServerRecord, MCPServerRecord.id == MCPToolRecord.server_id)
            .where(MCPToolRecord.id.in_(tool_ids))
        ).all()
    }
    if skill_ids:
        for sid, name, src, enabled in session.execute(
            select(SkillRecord.id, SkillRecord.name, SkillSourceRecord.name, SkillRecord.enabled)
            .join(SkillSourceRecord, SkillSourceRecord.id == SkillRecord.source_id)
            .where(SkillRecord.id.in_(skill_ids))
        ).all():
            ref = f"{SKILL_ID_PREFIX}{sid}"
            out[ref] = SuggestionToolRef(id=ref, name=name, server_name=src, enabled=enabled)
    return out


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


class InvalidPreference(RegistryError):
    """D12: the preferred tool must be one side of the pair."""

    status_code = 422

    def __init__(self) -> None:
        super().__init__("preferredToolId must be toolA or toolB of this suggestion.")


def accept_suggestion(
    session: Session,
    suggestion_id: str,
    *,
    actor: str,
    preferred_tool_id: str | None = None,
) -> DuplicateSuggestion:
    """Accept a suggestion. `preferred_tool_id` (D12) records the reviewer's
    pick; None keeps the scanner's pick (backward compatible)."""
    sug = _load_open(session, suggestion_id)
    if preferred_tool_id is not None:
        if preferred_tool_id not in (sug.tool_a_id, sug.tool_b_id):
            raise InvalidPreference()
        sug.preferred_tool_id = preferred_tool_id
    sug.status = "accepted"
    sug.resolved_by = _one_line(actor)[:120]
    sug.resolved_at = utcnow()
    sug.rationale = f"{sug.rationale}\nAccepted by {_one_line(actor)}."
    session.flush()
    audit(
        "dedup.accept",
        actor=actor,
        suggestion_id=sug.id,
        preferred_tool_id=sug.preferred_tool_id,
    )
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
    sug.resolved_by = _one_line(actor)[:120]
    sug.resolved_at = utcnow()
    sug.resolution_note = note
    sug.rationale = f"{sug.rationale}\nDismissed by {_one_line(actor)}: {note}"
    session.flush()
    audit("dedup.dismiss", actor=actor, suggestion_id=sug.id, justification=note)
    return sug
