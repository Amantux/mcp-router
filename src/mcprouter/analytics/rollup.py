"""tool_stats_daily rollup: idempotent per-day recompute.

`recompute_day(day)` runs the SAME funnel SQL as the live path over
`[day 00:00 UTC, day+1 00:00 UTC)` and replaces that day's rows with
DELETE + INSERT inside the caller's transaction (no TRUNCATE; other days are
never touched). Running it twice yields identical rows.

Only whole days strictly before the live horizon (now - 48h, floored to UTC
midnight) may be rolled up: attributed executions can arrive after their
decision's day, and the 48h live window is what absorbs them. A late
execution arriving after its day was rolled up is only counted once that day
is recomputed. `exposed_tokens` is frozen at recompute time (tool
definitions can change later).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from mcprouter.analytics.funnel import live_funnel
from mcprouter.analytics.tokens import tool_token_map
from mcprouter.analytics.window import live_horizon, midnight
from mcprouter.models import ToolStatsDaily

MAX_DAYS_PER_RUN = 90
ROLLUP_LOCK_KEY = 0x6D637072_726F6C6C  # "mcpr" "roll"; transaction-scoped advisory lock


class RollupNotAllowed(ValueError):
    """Curated: the message is safe to return to the caller."""


@dataclass(frozen=True)
class DayResult:
    day: date
    tool_rows: int


def recompute_day(session: Session, day: date, now: datetime) -> DayResult:
    if day >= live_horizon(now):
        raise RollupNotAllowed(
            "day is inside the live window (last 48h); only older days are rolled up."
        )
    start = midnight(day)
    funnel = live_funnel(session, start, start + timedelta(days=1), tool_token_map(session))
    session.execute(delete(ToolStatsDaily).where(ToolStatsDaily.day == day))
    session.add_all(
        ToolStatsDaily(
            tool_id=tid,
            day=day,
            surfaced=c.surfaced,
            selected=c.selected,
            succeeded=c.succeeded,
            failed=c.failed,
            sum_rank=c.sum_rank,
            exposed_tokens=c.exposed_tokens,
            computed_at=now,
        )
        for tid, c in sorted(funnel.items())
    )
    session.flush()
    return DayResult(day=day, tool_rows=len(funnel))


def recompute_range(session: Session, last_day: date, days: int, now: datetime) -> list[DayResult]:
    """`days` consecutive days ending at `last_day` (inclusive), oldest first."""
    if not 1 <= days <= MAX_DAYS_PER_RUN:
        raise RollupNotAllowed(f"days must be between 1 and {MAX_DAYS_PER_RUN}.")
    if last_day >= live_horizon(now):  # validate before touching any day
        raise RollupNotAllowed(
            "day is inside the live window (last 48h); only older days are rolled up."
        )
    # Serialize concurrent runs (cron + manual click): DELETE+INSERT under READ
    # COMMITTED would otherwise race into the (tool_id, day) primary key.
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ROLLUP_LOCK_KEY})
    first = last_day - timedelta(days=days - 1)
    return [recompute_day(session, first + timedelta(days=i), now) for i in range(days)]


def default_last_day(now: datetime) -> date:
    """The newest day eligible for rollup."""
    return live_horizon(now) - timedelta(days=1)
