"""Daily rollup loop (opt-in: MCPR_ANALYTICS_ROLLUP_ENABLED, default off).

Started by the app lifespan when enabled. Each pass recomputes the newest
eligible days (`default_last_day(now)` = live horizon - 1, and the
`CATCH_UP_DAYS - 1` days before it) through the same `recompute_range` the
`POST /api/v1/analytics/rollup` endpoint uses — idempotent, serialized by the
advisory lock, so a concurrent cron call or manual click is safe. Catch-up
days absorb short outages; a day never rolled up is still computed live
(correct, only slower), so missing passes never produce wrong numbers.

"Yesterday" is NOT eligible: the newest 48h stay live so late attributed
executions land; the newest rollable day is the one before the horizon.

The alternative (when this is off) is an external cron:
`POST /api/v1/analytics/rollup` with the admin token, once a day.

A failing pass logs the exception TYPE only and the loop keeps going.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime

import anyio.to_thread
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.analytics.rollup import DayResult, default_last_day, recompute_range
from mcprouter.models import utcnow

log = logging.getLogger(__name__)

INTERVAL_S = 24 * 3600.0
CATCH_UP_DAYS = 3


class RollupLoop:
    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        interval_s: float = INTERVAL_S,
        days: int = CATCH_UP_DAYS,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._factory = factory
        self._interval_s = interval_s
        self._days = days
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self.passes = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def run_once(self) -> list[DayResult]:
        """One synchronous pass (thread body; also used by tests)."""
        now = self._clock()
        with self._factory() as s:
            results = recompute_range(s, default_last_day(now), self._days, now)
            s.commit()
        log.info(
            "analytics.rollup_pass days=%d tool_rows=%d",
            len(results),
            sum(r.tool_rows for r in results),
        )
        return results

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.get_running_loop().create_task(
            self._run(), name="mcpr-analytics-rollup"
        )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await anyio.to_thread.run_sync(self.run_once)
            except Exception as exc:  # noqa: BLE001 — a failed pass must not kill the loop
                log.warning("analytics.rollup_failed exc_type=%s", type(exc).__name__)
            self.passes += 1
            await asyncio.sleep(self._interval_s)
