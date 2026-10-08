"""Routing / execution profiles: overall (overview cards) and per agent.

* no-match rate  = decisions that surfaced nothing / decisions
* fallback rate  = decisions with fallback_used / decisions
* denial rate    = ExecutionRecord outcome 'denied' / attempts, where attempts
  are all execution rows except the transient 'started' (window by the
  execution's created_at; attributed or not)
* attribution coverage = attempts whose route_request_id names an existing
  decision OF THE SAME AGENT / attempts
* off-funnel selections = attributed (decision, tool) pairs whose tool the
  decision did NOT surface (e.g. a tool exposed by an earlier route)
* budget fields are NULL: the per-decision max_tools actually applied is not
  persisted on RoutingDecisionRecord (see INTEGRATION notes); utilization is
  reported as surfaced-per-decision vs selected instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from mcprouter.analytics.economy import Economy
from mcprouter.analytics.funnel import ATT_CTE, SURF_CTE, ratio
from mcprouter.analytics.window import Window

_DECISIONS_SQL = text(
    """
SELECT d.agent_id,
       count(*) AS decisions,
       count(*) FILTER (
           WHERE NOT (jsonb_typeof(d.selected_tool_ids::jsonb) = 'array'
                      AND jsonb_array_length(d.selected_tool_ids::jsonb) > 0)
       ) AS no_match,
       count(*) FILTER (WHERE d.fallback_used) AS fallback,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY d.latency_ms) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY d.latency_ms) AS p95
FROM routing_decisions d
WHERE d.created_at >= :start AND d.created_at < :end
GROUP BY GROUPING SETS ((d.agent_id), ())
"""
)

_EXEC_SQL = text(
    """
SELECT x.agent_id,
       count(*) AS attempts,
       count(*) FILTER (WHERE x.outcome = 'denied') AS denied,
       count(*) FILTER (WHERE d.id IS NOT NULL) AS attributed
FROM execution_records x
LEFT JOIN routing_decisions d ON d.id = x.route_request_id AND d.agent_id = x.agent_id
WHERE x.created_at >= :start AND x.created_at < :end AND x.outcome <> 'started'
GROUP BY GROUPING SETS ((x.agent_id), ())
"""
)

_SELECTION_SQL = text(
    "WITH"
    + SURF_CTE
    + ","
    + ATT_CTE
    + """
SELECT s.agent_id, count(*) AS surfaced, count(a.decision_id) AS selected
FROM surf s
LEFT JOIN att a ON a.decision_id = s.decision_id AND a.tool_id = s.tool_id
GROUP BY GROUPING SETS ((s.agent_id), ())
"""
)

_OFF_FUNNEL_SQL = text(
    "WITH"
    + SURF_CTE
    + ","
    + ATT_CTE
    + """
SELECT count(*)
FROM att a
WHERE NOT EXISTS (
    SELECT 1 FROM surf s WHERE s.decision_id = a.decision_id AND s.tool_id = a.tool_id
)
"""
)

_DRIFT_SQL = text(
    """
SELECT v.change_kind, count(*)
FROM tool_versions v
WHERE v.recorded_at >= :start AND v.recorded_at < :end
GROUP BY v.change_kind
"""
)

DRIFT_KINDS = ("added", "schema", "metadata", "removed", "restored")

_ALL = "\x00all"  # grouping-set total key (never a real agent id: NUL is refused upstream)


@dataclass
class Profile:
    decisions: int = 0
    no_match: int = 0
    fallback: int = 0
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    attempts: int = 0
    denied: int = 0
    attributed: int = 0
    surfaced: int = 0
    selected: int = 0
    economy: Economy = field(default_factory=Economy)

    @property
    def no_match_rate(self) -> float | None:
        return ratio(self.no_match, self.decisions)

    @property
    def fallback_rate(self) -> float | None:
        return ratio(self.fallback, self.decisions)

    @property
    def denial_rate(self) -> float | None:
        return ratio(self.denied, self.attempts)

    @property
    def attribution_coverage(self) -> float | None:
        return ratio(self.attributed, self.attempts)

    @property
    def selection_rate(self) -> float | None:
        return ratio(self.selected, self.surfaced)

    @property
    def avg_surfaced_per_decision(self) -> float | None:
        return ratio(self.surfaced, self.decisions - self.no_match)


def _key(agent_id: str | None) -> str:
    return _ALL if agent_id is None else agent_id


def profiles(session: Session, window: Window) -> tuple[Profile, dict[str, Profile]]:
    """(overall, per-agent). Agents appear if they routed OR executed."""
    params = {"start": window.start, "end": window.end, "skip_days": []}
    acc: dict[str, Profile] = {}
    for agent, n, nm, fb, p50, p95 in session.execute(_DECISIONS_SQL, params).all():
        p = acc.setdefault(_key(agent), Profile())
        p.decisions, p.no_match, p.fallback = int(n), int(nm), int(fb)
        p.latency_p50_ms = None if p50 is None else float(p50)
        p.latency_p95_ms = None if p95 is None else float(p95)
    for agent, attempts, denied, attributed in session.execute(_EXEC_SQL, params).all():
        p = acc.setdefault(_key(agent), Profile())
        p.attempts, p.denied, p.attributed = int(attempts), int(denied), int(attributed)
    for agent, surfaced, selected in session.execute(_SELECTION_SQL, params).all():
        p = acc.setdefault(_key(agent), Profile())
        p.surfaced, p.selected = int(surfaced), int(selected)
    total = acc.pop(_ALL, Profile())
    return total, acc


def off_funnel_selections(session: Session, window: Window) -> int:
    params = {"start": window.start, "end": window.end, "skip_days": []}
    return int(session.execute(_OFF_FUNNEL_SQL, params).scalar_one())


def catalog_drift(session: Session, window: Window) -> dict[str, int]:
    counts = dict.fromkeys(DRIFT_KINDS, 0)
    for kind, n in session.execute(_DRIFT_SQL, {"start": window.start, "end": window.end}).all():
        if kind in counts:
            counts[kind] = int(n)
    return counts
