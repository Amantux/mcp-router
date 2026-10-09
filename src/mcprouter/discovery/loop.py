"""Background catalog-sync + health loop.

Nothing starts at import. ``await loop.start()`` spawns ONE asyncio task on
the running loop; ``await loop.stop()`` cancels and awaits it. Tests (and
any deployment that wants manual refresh only) simply never call start().

Scheduling (per server, process-local, monotonic clock):
* sync   every ``sync_interval_s`` (override per server id via ``intervals``),
         first sync immediately on start;
* health every ``health_interval_s`` between syncs.
A server deleted or disabled mid-run is simply skipped next tick.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping

import anyio
import anyio.to_thread

from mcprouter.discovery.logsafe import scrub
from mcprouter.discovery.sync import DiscoveryService, SyncReport

log = logging.getLogger(__name__)


class SyncLoop:
    def __init__(
        self,
        service: DiscoveryService,
        *,
        sync_interval_s: float = 300.0,
        health_interval_s: float = 60.0,
        tick_s: float = 1.0,
        intervals: Mapping[str, float] | None = None,
        skill_tick: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self._service = service
        self._sync_interval_s = sync_interval_s
        self._health_interval_s = health_interval_s
        self._tick_s = tick_s
        self._intervals = dict(intervals or {})
        self._skill_tick = skill_tick
        self._last_sync: dict[str, float] = {}
        self._last_health: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None
        self.ticks = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def set_interval(self, server_id: str, seconds: float) -> None:
        self._intervals[server_id] = seconds

    async def start(self) -> None:
        if self.running:
            raise RuntimeError("sync loop already running")
        self._task = asyncio.get_running_loop().create_task(self._run(), name="mcpr-sync-loop")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 — the loop must survive any one bad tick
                log.warning("sync loop tick failed: %s", type(exc).__name__)
            self.ticks += 1
            await asyncio.sleep(self._tick_s)

    async def run_once(self, now: float | None = None) -> tuple[list[str], list[str]]:
        """One scheduling pass. Returns (synced ids, health-checked ids)."""
        now = time.monotonic() if now is None else now
        ids = await anyio.to_thread.run_sync(self._service.server_ids, True)
        to_sync = [
            sid
            for sid in ids
            if now - self._last_sync.get(sid, float("-inf"))
            >= self._intervals.get(sid, self._sync_interval_s)
        ]
        to_check = [
            sid
            for sid in ids
            if sid not in to_sync
            and now - max(self._last_health.get(sid, float("-inf")), self._last_sync.get(sid, 0.0))
            >= self._health_interval_s
        ]
        if to_sync:
            # Mark BEFORE awaiting: a failed/cancelled pass must not cause every
            # server to be re-synced on every tick.
            for sid in to_sync:
                self._last_sync[sid] = now
            results = await self._service.sync_all(server_ids=to_sync)
            for sid in to_sync:
                if not isinstance(results.get(sid), SyncReport):
                    log.info("scheduled sync failed for server id %s", scrub(sid))
        if to_check:
            for sid in to_check:
                self._last_health[sid] = now
            await self._service.check_all(to_check)
        if self._skill_tick is not None:  # skill sources (own intervals, in the DB)
            await self._skill_tick()
        return to_sync, to_check
