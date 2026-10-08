"""Per-tool funnel: surfaced -> selected -> succeeded.

Definitions (all anchored on the DECISION's time, not the execution's):

* surfaced  — appearances of the tool in `RoutingDecisionRecord.selected_tool_ids`
  (what the router actually exposed; the list is stored in final rank order,
  so its 1-based index IS the rank — see `routing.pipeline.route`: ranked by
  score desc, ties by server/tool name, truncated to max_tools).
* retrieved — NOT AVAILABLE: retrieval candidates are not persisted, so the
  funnel starts at `surfaced` (proposal for the routing track in
  docs/INTEGRATION_NOTES-wave2-analytics.md).
* selected  — distinct (decision, tool) pairs with at least one attributed
  ExecutionRecord (`route_request_id` = decision id) for a tool that decision
  surfaced. Any terminal outcome counts as a selection (ok, error, timeout,
  denied, rate_limited, invalid_args, unavailable, pending_approval,
  cancelled); the transient `started` row does not. Repeated calls of one
  tool after one decision count once, so selected <= surfaced.
* succeeded — selected pairs with at least one `ok` outcome.
* failed    — selected pairs with an `error`/`timeout` and no `ok`.

Rates are `None` when the denominator is 0 (never a division by zero).
Unattributed executions (route_request_id NULL: legacy rows, approval
replays, callers that don't pass it) are NOT in the funnel.

All SQL here is literal text with bound values only.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from mcprouter.analytics.window import Window, live_horizon, midnight
from mcprouter.models import ToolStatsDaily

# Surfaced (decision, tool, rank) rows in [start, end), minus whole UTC days
# served from rollups. Non-array selected_tool_ids degrade to "nothing shown".
SURF_CTE = """
surf AS (
    SELECT d.id AS decision_id, d.agent_id, e.tool_id, e.rank::int AS rank
    FROM routing_decisions d
    CROSS JOIN LATERAL jsonb_array_elements_text(
        CASE WHEN jsonb_typeof(d.selected_tool_ids::jsonb) = 'array'
             THEN d.selected_tool_ids::jsonb ELSE '[]'::jsonb END
    ) WITH ORDINALITY AS e(tool_id, rank)
    WHERE d.created_at >= :start AND d.created_at < :end
      AND NOT ((d.created_at AT TIME ZONE 'UTC')::date = ANY(CAST(:skip_days AS date[])))
)"""

# Attributed (decision, tool) pairs for decisions in `surf`.
ATT_CTE = """
att AS (
    SELECT x.route_request_id AS decision_id, x.tool_id,
           bool_or(x.outcome = 'ok') AS any_ok,
           bool_or(x.outcome IN ('error', 'timeout')) AS any_fail
    FROM execution_records x
    WHERE x.route_request_id IN (SELECT decision_id FROM surf)
      AND x.tool_id IS NOT NULL
      AND x.outcome <> 'started'
    GROUP BY x.route_request_id, x.tool_id
)"""

_FUNNEL_SQL = text(
    "WITH"
    + SURF_CTE
    + ","
    + ATT_CTE
    + """
SELECT s.tool_id,
       count(*) AS surfaced,
       count(a.decision_id) AS selected,
       count(*) FILTER (WHERE a.any_ok) AS succeeded,
       count(*) FILTER (WHERE a.any_fail AND NOT a.any_ok) AS failed,
       coalesce(sum(s.rank), 0) AS sum_rank
FROM surf s
LEFT JOIN att a ON a.decision_id = s.decision_id AND a.tool_id = s.tool_id
GROUP BY s.tool_id
"""
)

_POSITION_SQL = text(
    "WITH"
    + SURF_CTE
    + ","
    + ATT_CTE
    + """,
acted AS (SELECT DISTINCT decision_id FROM att)
SELECT s.rank, count(*) AS shown, count(a.decision_id) AS selected
FROM surf s
JOIN acted ON acted.decision_id = s.decision_id
LEFT JOIN att a ON a.decision_id = s.decision_id AND a.tool_id = s.tool_id
WHERE CAST(:tool AS varchar) IS NULL OR s.tool_id = CAST(:tool AS varchar)
GROUP BY s.rank
ORDER BY s.rank
"""
)

_CO_SURFACED_SQL = text(
    "WITH"
    + SURF_CTE
    + ","
    + ATT_CTE
    + """
SELECT o.tool_id,
       count(*) AS co_surfaced,
       count(at.decision_id) AS this_selected,
       count(ao.decision_id) AS other_selected
FROM surf t
JOIN surf o ON o.decision_id = t.decision_id AND o.tool_id <> t.tool_id
LEFT JOIN att at ON at.decision_id = t.decision_id AND at.tool_id = t.tool_id
LEFT JOIN att ao ON ao.decision_id = o.decision_id AND ao.tool_id = o.tool_id
WHERE t.tool_id = :tool
GROUP BY o.tool_id
ORDER BY count(*) DESC, o.tool_id
LIMIT :limit
"""
)

_PAIR_SQL = text(
    "WITH"
    + SURF_CTE
    + ","
    + ATT_CTE
    + """,
pairs AS (
    SELECT p.a_id, p.b_id
    FROM unnest(CAST(:a_ids AS varchar[]), CAST(:b_ids AS varchar[])) AS p(a_id, b_id)
)
SELECT p.a_id, p.b_id,
       count(*) AS co_surfaced,
       count(aa.decision_id) AS a_selected,
       count(ab.decision_id) AS b_selected,
       count(*) FILTER (WHERE aa.decision_id IS NOT NULL AND ab.decision_id IS NOT NULL)
           AS both_selected
FROM pairs p
JOIN surf sa ON sa.tool_id = p.a_id
JOIN surf sb ON sb.decision_id = sa.decision_id AND sb.tool_id = p.b_id
LEFT JOIN att aa ON aa.decision_id = sa.decision_id AND aa.tool_id = p.a_id
LEFT JOIN att ab ON ab.decision_id = sb.decision_id AND ab.tool_id = p.b_id
GROUP BY p.a_id, p.b_id
"""
)


def ratio(num: float, den: float) -> float | None:
    return None if den == 0 else num / den


@dataclass
class ToolCounts:
    surfaced: int = 0
    selected: int = 0
    succeeded: int = 0
    failed: int = 0
    sum_rank: int = 0
    exposed_tokens: int = 0

    def add(self, other: ToolCounts) -> None:
        self.surfaced += other.surfaced
        self.selected += other.selected
        self.succeeded += other.succeeded
        self.failed += other.failed
        self.sum_rank += other.sum_rank
        self.exposed_tokens += other.exposed_tokens

    @property
    def selection_rate(self) -> float | None:
        return ratio(self.selected, self.surfaced)

    @property
    def success_rate(self) -> float | None:
        return ratio(self.succeeded, self.selected)

    @property
    def avg_rank(self) -> float | None:
        return ratio(self.sum_rank, self.surfaced)


def _params(start: datetime, end: datetime, skip_days: Iterable[date]) -> dict[str, object]:
    return {"start": start, "end": end, "skip_days": list(skip_days)}


def live_funnel(
    session: Session,
    start: datetime,
    end: datetime,
    tokens: dict[str, int],
    *,
    skip_days: Iterable[date] = (),
) -> dict[str, ToolCounts]:
    """Raw-table funnel over [start, end) excluding `skip_days` (UTC days).
    exposed_tokens = surfaced x the tool's CURRENT estimated tokens (0 for a
    tool no longer in the catalog)."""
    out: dict[str, ToolCounts] = {}
    for tid, surfaced, selected, succeeded, failed, sum_rank in session.execute(
        _FUNNEL_SQL, _params(start, end, skip_days)
    ).all():
        n = int(surfaced)
        out[tid] = ToolCounts(
            surfaced=n,
            selected=int(selected),
            succeeded=int(succeeded),
            failed=int(failed),
            sum_rank=int(sum_rank),
            exposed_tokens=n * tokens.get(tid, 0),
        )
    return out


def rolled_days(session: Session, start: datetime, now: datetime) -> list[date]:
    """Whole UTC days fully inside [start, horizon) that have rollup rows.

    A day without rows is computed live — correct whether it had no traffic
    or was simply never rolled up (no marker table needed)."""
    horizon = live_horizon(now)
    first = start.date() if midnight(start.date()) >= start else start.date() + timedelta(days=1)
    if first >= horizon:
        return []
    D = ToolStatsDaily
    return list(
        session.scalars(
            select(D.day).where(D.day >= first, D.day < horizon).distinct().order_by(D.day)
        ).all()
    )


def rollup_funnel(session: Session, days: list[date]) -> dict[str, ToolCounts]:
    out: dict[str, ToolCounts] = {}
    if not days:
        return out
    for r in session.scalars(select(ToolStatsDaily).where(ToolStatsDaily.day.in_(days))).all():
        acc = out.setdefault(r.tool_id, ToolCounts())
        acc.add(
            ToolCounts(
                surfaced=r.surfaced,
                selected=r.selected,
                succeeded=r.succeeded,
                failed=r.failed,
                sum_rank=r.sum_rank,
                exposed_tokens=r.exposed_tokens,
            )
        )
    return out


def merged_funnel(
    session: Session, window: Window, tokens: dict[str, int]
) -> dict[str, ToolCounts]:
    """Rollups for whole days older than the live horizon, raw for the rest."""
    days = rolled_days(session, window.start, window.end)
    out = rollup_funnel(session, days)
    for tid, counts in live_funnel(
        session, window.start, window.end, tokens, skip_days=days
    ).items():
        out.setdefault(tid, ToolCounts()).add(counts)
    return out


def totals(funnel: dict[str, ToolCounts]) -> ToolCounts:
    acc = ToolCounts()
    for c in funnel.values():
        acc.add(c)
    return acc


# ------------------------------------------------------------ position bias
@dataclass(frozen=True)
class RankPoint:
    rank: int
    shown: int
    selected: int

    @property
    def rate(self) -> float | None:
        return ratio(self.selected, self.shown)


def position_curve(session: Session, window: Window, tool_id: str | None = None) -> list[RankPoint]:
    """P(selected | rank) over ATTRIBUTED decisions only (decisions with at
    least one attributed execution): a decision the agent never acted on — or
    whose calls were not attributed — says nothing about rank preference.
    Raw tables (not rollups): per-rank counts are not rolled up."""
    params = _params(window.start, window.end, ()) | {"tool": tool_id}
    return [
        RankPoint(int(rank), int(shown), int(sel))
        for rank, shown, sel in session.execute(_POSITION_SQL, params).all()
    ]


# --------------------------------------------------------------- co-surfacing
@dataclass(frozen=True)
class CoSurfaced:
    tool_id: str
    co_surfaced: int
    this_selected: int
    other_selected: int


def co_surfaced(
    session: Session, window: Window, tool_id: str, limit: int = 10
) -> list[CoSurfaced]:
    params = _params(window.start, window.end, ()) | {"tool": tool_id, "limit": limit}
    return [
        CoSurfaced(tid, int(co), int(ts), int(os_))
        for tid, co, ts, os_ in session.execute(_CO_SURFACED_SQL, params).all()
    ]


@dataclass(frozen=True)
class PairEvidence:
    co_surfaced: int = 0
    a_selected: int = 0
    b_selected: int = 0
    both_selected: int = 0


def pair_evidence(
    session: Session, window: Window, pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], PairEvidence]:
    """Co-surfacing of each (a, b): decisions that exposed BOTH, and which of
    the two the agent then selected. Pairs never co-surfaced are absent."""
    if not pairs:
        return {}
    params = _params(window.start, window.end, ()) | {
        "a_ids": [a for a, _ in pairs],
        "b_ids": [b for _, b in pairs],
    }
    return {
        (a, b): PairEvidence(int(co), int(sa), int(sb), int(both))
        for a, b, co, sa, sb, both in session.execute(_PAIR_SQL, params).all()
    }


# ------------------------------------------------------------ wasted exposure
@dataclass(frozen=True)
class Wasted:
    tool_id: str
    counts: ToolCounts = field(default_factory=ToolCounts)


def wasted_exposure(
    funnel: dict[str, ToolCounts], *, min_surfaced: int, max_selection_rate: float
) -> list[Wasted]:
    """Tools surfaced >= min_surfaced whose selection rate is below the
    threshold — context spent on tools agents don't pick. Surfaced desc."""
    hits = [
        Wasted(tid, c)
        for tid, c in funnel.items()
        if c.surfaced >= min_surfaced and (c.selection_rate or 0.0) < max_selection_rate
    ]
    hits.sort(key=lambda w: (-w.counts.surfaced, w.tool_id))
    return hits
