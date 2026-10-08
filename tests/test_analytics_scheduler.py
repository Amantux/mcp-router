"""Daily rollup loop (MCPR_ANALYTICS_ROLLUP_ENABLED) + the promoted settings."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.analytics.rollup import default_last_day
from mcprouter.analytics.scheduler import CATCH_UP_DAYS, RollupLoop
from mcprouter.analytics.window import midnight
from mcprouter.api.app import create_app
from mcprouter.models import ToolStatsDaily
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db
from .test_analytics_support import NOW, add_decision, add_exec, build_world

SF = sessionmaker[Session]


def test_rollup_setting_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_ANALYTICS_ROLLUP_ENABLED", "")
    assert Settings.from_env().analytics_rollup_enabled is False  # empty string = unset
    for raw in ("1", "true", "YES", " on "):
        monkeypatch.setenv("MCPR_ANALYTICS_ROLLUP_ENABLED", raw)
        assert Settings.from_env().analytics_rollup_enabled is True
    monkeypatch.setenv("MCPR_ANALYTICS_ROLLUP_ENABLED", "maybe")
    with pytest.raises(ValueError):
        Settings.from_env()
    assert Settings().analytics_rollup_enabled is False
    assert Settings().usage_prior_enabled is False


@requires_db
def test_run_once_rolls_up_the_newest_eligible_days(db: SF) -> None:
    w = build_world(db)
    last = default_last_day(NOW)
    noon = midnight(last) + timedelta(hours=12)
    d = add_decision(db, "alice", noon, [w.A])
    add_exec(db, "alice", w.A, "ok", d, noon + timedelta(minutes=1))
    too_old = midnight(last - timedelta(days=CATCH_UP_DAYS)) + timedelta(hours=12)
    add_decision(db, "alice", too_old, [w.B])

    results = RollupLoop(db, clock=lambda: NOW).run_once()
    assert [r.day for r in results] == [
        last - timedelta(days=i) for i in reversed(range(CATCH_UP_DAYS))
    ]
    with db() as s:
        rows = s.scalars(select(ToolStatsDaily)).all()
    assert [(r.day, r.tool_id, r.surfaced, r.selected) for r in rows] == [(last, w.A, 1, 1)]


@requires_db
def test_failed_pass_does_not_kill_the_loop(db: SF, monkeypatch: pytest.MonkeyPatch) -> None:
    import anyio

    loop = RollupLoop(db, interval_s=0.01)

    def boom() -> list[Any]:
        raise RuntimeError("db down")

    monkeypatch.setattr(loop, "run_once", boom)

    async def drive() -> None:
        await loop.start()
        for _ in range(200):
            if loop.passes >= 3:
                break
            await anyio.sleep(0.01)
        assert loop.running
        await loop.stop()

    anyio.run(drive)
    assert loop.passes >= 3 and not loop.running


@requires_db
def test_create_app_starts_and_stops_rollup_loop_when_enabled(
    db: SF, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation target: the lifespan start (and explicit stop) of the loop."""
    stopped: list[bool] = []
    real_stop = RollupLoop.stop

    async def spy_stop(self: RollupLoop) -> None:
        stopped.append(self.running)
        await real_stop(self)

    monkeypatch.setattr(RollupLoop, "stop", spy_stop)
    app = create_app(Settings(database_url=TEST_DB_URL, analytics_rollup_enabled=True), env={})
    with TestClient(app):
        loop: Any = app.state.rollup_loop
        assert isinstance(loop, RollupLoop) and loop.running
        deadline = time.monotonic() + 10
        while loop.passes < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert loop.passes >= 1  # the first pass runs at startup
    assert stopped == [True]
    assert not loop.running


@requires_db
def test_create_app_does_not_start_rollup_loop_by_default(db: SF) -> None:
    app = create_app(Settings(database_url=TEST_DB_URL), env={})
    with TestClient(app):
        assert app.state.rollup_loop is None
