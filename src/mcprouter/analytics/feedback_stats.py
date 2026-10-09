"""Read-side feedback aggregates (docs/analytics.md "Feedback").

Counts only — notes are free text and are never exposed by analytics.

A route_feedback row counts when:
* its decision is LIVE (simulated decisions are excluded, same marker as the
  funnel) and was created inside the window (decision time, not verdict time);
* source=human: always (an admin may judge any agent's decision);
* source=agent: only when the row's agent_id OWNS the decision — the same
  ownership guard execution attribution uses.

Feedback is not rolled up: it is always read from the raw tables, so it
covers what raw retention still holds.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from mcprouter.analytics.funnel import LIVE_DECISION_SQL, ratio
from mcprouter.analytics.window import Window

_BASE = (
    """
FROM route_feedback f
JOIN routing_decisions d ON d.id = f.route_request_id
WHERE d.created_at >= :start AND d.created_at < :end
  AND (f.source = 'human' OR f.agent_id = d.agent_id)
  AND """
    + LIVE_DECISION_SQL
)

_BY_TARGET_SQL = text(
    "SELECT f.target_id, count(*) FILTER (WHERE f.helpful),"
    " count(*) FILTER (WHERE NOT f.helpful)" + _BASE + " GROUP BY f.target_id"
)
_BY_AGENT_SQL = text(
    "SELECT d.agent_id, count(*) FILTER (WHERE f.helpful),"
    " count(*) FILTER (WHERE NOT f.helpful)" + _BASE + " GROUP BY d.agent_id"
)
_COVERED_SQL = text("SELECT count(DISTINCT d.id)" + _BASE)
_LIVE_DECISIONS_SQL = text(
    "SELECT count(*) FROM routing_decisions d"
    " WHERE d.created_at >= :start AND d.created_at < :end AND " + LIVE_DECISION_SQL
)


@dataclass(frozen=True)
class FeedbackCounts:
    helpful: int = 0
    unhelpful: int = 0

    @property
    def items(self) -> int:
        return self.helpful + self.unhelpful

    @property
    def helpful_rate(self) -> float | None:
        return ratio(self.helpful, self.items)


def _params(w: Window) -> dict[str, object]:
    return {"start": w.start, "end": w.end}


def feedback_by_target(session: Session, window: Window) -> dict[str, FeedbackCounts]:
    """Keyed by funnel id (tool id, or "skill:<id>")."""
    rows = session.execute(_BY_TARGET_SQL, _params(window)).all()
    return {tid: FeedbackCounts(int(h), int(u)) for tid, h, u in rows}


def feedback_by_agent(session: Session, window: Window) -> dict[str, FeedbackCounts]:
    """Keyed by the DECISION's agent (human verdicts land on that agent)."""
    rows = session.execute(_BY_AGENT_SQL, _params(window)).all()
    return {aid: FeedbackCounts(int(h), int(u)) for aid, h, u in rows}


def feedback_coverage(session: Session, window: Window) -> float | None:
    """Live decisions with >= 1 counted feedback row / live decisions."""
    covered = int(session.execute(_COVERED_SQL, _params(window)).scalar_one())
    live = int(session.execute(_LIVE_DECISIONS_SQL, _params(window)).scalar_one())
    return ratio(covered, live)
