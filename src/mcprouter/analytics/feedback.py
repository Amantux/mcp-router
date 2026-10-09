"""Route feedback: was a surfaced tool/skill helpful? (docs/analytics.md)

THE one implementation: REST (api/routes_feedback.py) calls `record_feedback`.
The MCP meta-tool `router.feedback` is NOT wired yet; it must call this too.

Trust rules:
* source=agent: the decision must be the caller's OWN live (non-simulated)
  decision, else FeedbackNotFound (404 -- never reveal other agents' ids).
* source=human (admin): any live decision; agent_id is taken from the decision.
* a target must have been SURFACED by that decision (no voting on strangers).
* note: <= 500 chars, secrets redacted, CR/LF/control chars neutralized.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from mcprouter.execution.ratelimit import KeyedLimiter, SlidingWindowLimiter
from mcprouter.execution.redaction import scrub_log
from mcprouter.limits import FEEDBACK_LIMIT_PER_MIN
from mcprouter.models import RouteFeedback, RoutingDecisionRecord

NOTE_MAX = 500
MAX_ITEMS = 50
SKILL_PREFIX = "skill:"


class FeedbackError(Exception):
    """Curated, client-safe message (never raw exception text)."""


class FeedbackNotFound(FeedbackError):
    pass


class FeedbackInvalid(FeedbackError):
    pass


class FeedbackRateLimited(FeedbackError):
    pass


@dataclass(frozen=True)
class FeedbackItem:
    helpful: bool
    kind: Literal["tool", "skill"] | None = None
    id: str | None = None
    name: str | None = None
    note: str | None = None


# Bidi overrides/isolates (U+202A-202E, U+2066-2069) can visually reorder a
# note in the UI ("trojan source"); strip at write so every reader is safe.
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")


def clean_note(note: str | None) -> str | None:
    if note is None or not note.strip():
        return None
    return _BIDI.sub("", scrub_log(note.strip()))[:NOTE_MAX]


# The second limiter implementation is gone (P-606): feedback uses the app's
# registry surface ``app.state.limiters.surface("feedback")`` (30/min per
# principal), passed in by the callers. LIMITER is only the fallback for a
# caller that passes none yet (routes_feedback / gateway until E2/E3 adopt
# the registry); it is a plain SlidingWindowLimiter with the same budget.
LIMITER = SlidingWindowLimiter(FEEDBACK_LIMIT_PER_MIN, 60.0)


def _resolve(item: FeedbackItem, surfaced: list[str], names: dict[str, str]) -> str:
    if item.id:
        tid = item.id
        if item.kind == "skill" and not tid.startswith(SKILL_PREFIX):
            tid = SKILL_PREFIX + tid
        if tid in surfaced:
            return tid
    elif item.name:
        hits = [t for t in surfaced if names.get(t) == item.name]
        if item.kind:
            hits = [t for t in hits if (t.startswith(SKILL_PREFIX)) == (item.kind == "skill")]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise FeedbackInvalid("name is ambiguous for this decision; pass id")
    raise FeedbackInvalid("target was not surfaced by this decision")


def record_feedback(
    session: Session,
    *,
    request_id: str,
    items: list[FeedbackItem],
    source: Literal["agent", "human"],
    agent_id: str | None,
    principal: str,
    limiter: KeyedLimiter | None = None,
) -> int:
    """Validate + upsert; returns rows written. agent_id is required for
    source=agent (the ownership check) and ignored for human."""
    if not items or len(items) > MAX_ITEMS:
        raise FeedbackInvalid(f"items must contain 1..{MAX_ITEMS} entries")
    # Before any lookup: 404 probing is rate-limited too.
    if not (LIMITER if limiter is None else limiter).try_acquire(principal):
        raise FeedbackRateLimited("too many feedback posts; retry in a minute")
    if len(request_id) > 36:
        raise FeedbackNotFound("decision not found")
    q = select(RoutingDecisionRecord).where(
        RoutingDecisionRecord.id == request_id,
        ~RoutingDecisionRecord.model_version.startswith("simulated/"),
    )
    if source == "agent":
        if not agent_id:
            raise FeedbackNotFound("decision not found")
        q = q.where(RoutingDecisionRecord.agent_id == agent_id)
    d = session.scalar(q)
    if d is None:
        raise FeedbackNotFound("decision not found")
    surfaced = [t for t in (d.selected_tool_ids or []) if isinstance(t, str)]
    from mcprouter.analytics.service import _meta  # local: service imports analytics

    names = {t: m.name for t, m in _meta(session).items() if t in surfaced and m.name}
    rows = {}
    for it in items:
        tid = _resolve(it, surfaced, names)
        rows[tid] = {
            "route_request_id": d.id,
            "agent_id": d.agent_id,
            "target_kind": "skill" if tid.startswith(SKILL_PREFIX) else "tool",
            "target_id": tid,
            "helpful": bool(it.helpful),
            "note": clean_note(it.note),
            "source": source,
        }
    for row in rows.values():
        stmt = insert(RouteFeedback).values(**row)
        session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_route_feedback_target",
                # created_at is refreshed on upsert: it means "when this verdict was
                # last given", so a changed vote counts in the window it was changed.
                set_={
                    "helpful": stmt.excluded.helpful,
                    "note": stmt.excluded.note,
                    "created_at": func.now(),
                },
            )
        )
    session.commit()
    return len(rows)
