"""Scheduled skill-source sync tick (wave-4 integrator item A1)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.discovery.loop import SyncLoop
from mcprouter.lifecycle import run_due_skill_syncs
from mcprouter.models import SkillSourceRecord
from mcprouter.settings import Settings
from mcprouter.skills import sources

from .conftest import requires_db

pytestmark = requires_db
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _src(s: Session, name: str, **kw: Any) -> str:
    rec = SkillSourceRecord(name=name, kind="directory", location="/tmp/x", **kw)
    s.add(rec)
    s.commit()
    return rec.id


def test_tick_honours_interval_enabled_and_last_synced(
    db: sessionmaker[Session], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[str] = []

    def fake_run_sync(s: Session, rec: SkillSourceRecord, _st: Settings) -> dict[str, Any]:
        synced.append(rec.id)
        rec.last_synced_at = clock[0]
        return {}

    clock = [T0]
    monkeypatch.setattr(sources, "run_sync", fake_run_sync)
    with db() as s:
        never = _src(s, "tick-never", sync_interval_s=600)
        fresh = _src(s, "tick-fresh", sync_interval_s=600, last_synced_at=T0)
        off = _src(s, "tick-off", sync_interval_s=600, enabled=False)
    hooked: list[str] = []
    attempts: dict[str, datetime] = {}

    def tick() -> list[str]:
        return run_due_skill_syncs(
            db, settings, lambda sid, _b: hooked.append(sid), clock[0], attempts
        )

    assert tick() == [never]  # fresh not due, disabled never
    assert synced == hooked == [never]
    clock[0] = T0 + timedelta(seconds=599)
    assert tick() == []
    clock[0] = T0 + timedelta(seconds=600)
    assert sorted(tick()) == sorted([never, fresh])
    assert off not in synced


def test_failed_sync_is_not_retried_every_tick(
    db: sessionmaker[Session], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def boom(s: Session, rec: SkillSourceRecord, _st: Settings) -> dict[str, Any]:
        calls.append(rec.id)
        raise RuntimeError("offline")

    monkeypatch.setattr(sources, "run_sync", boom)
    with db() as s:
        sid = _src(s, "tick-boom", sync_interval_s=600)
    attempts: dict[str, datetime] = {}
    assert sid in run_due_skill_syncs(db, settings, None, T0, attempts)
    assert sid not in run_due_skill_syncs(db, settings, None, T0 + timedelta(seconds=5), attempts)
    assert calls.count(sid) == 1


def test_sync_loop_calls_skill_tick() -> None:
    class Svc:
        def server_ids(self, _enabled: bool) -> list[str]:
            return []

    ticks: list[int] = []

    async def skill_tick() -> None:
        ticks.append(1)

    loop = SyncLoop(Svc(), skill_tick=skill_tick)  # type: ignore[arg-type]
    asyncio.run(loop.run_once(now=0.0))
    assert ticks == [1]
