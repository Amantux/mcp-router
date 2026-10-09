"""P-606 / D10: one limiter registry per app, keyed (surface, agent)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.analytics import feedback as fb
from mcprouter.limits import (
    FEEDBACK_LIMIT_PER_MIN,
    SURFACES,
    LimiterRegistry,
    SurfaceLimiter,
    make_limiters,
)
from mcprouter.settings import Settings

from .conftest import requires_db


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _settings(**kw: object) -> Settings:
    return Settings(**{"rate_limit_per_agent_per_min": 3, "decision_rate_limit_per_min": 2, **kw})  # type: ignore[arg-type]


def _drain(lim: SurfaceLimiter, key: str) -> int:
    n = 0
    while lim.try_acquire(key):
        n += 1
        assert n < 10_000
    return n


def test_budgets_come_from_settings_per_surface() -> None:
    reg = make_limiters(_settings())
    assert {s: reg.budget(s) for s in SURFACES} == {
        "execute": 3,
        "skills": 3,
        "route": 3,
        "decision": 2,
        "feedback": FEEDBACK_LIMIT_PER_MIN,
    }
    with pytest.raises(KeyError, match="unknown rate-limit surface"):
        reg.surface("nope")


def test_two_registries_never_share_budget() -> None:
    a, b = make_limiters(_settings()), make_limiters(_settings())
    assert a is not b
    assert _drain(a.surface("execute"), "alice") == 3
    assert b.try_acquire("execute", "alice") is True  # other app: untouched budget


def test_surfaces_and_agents_are_independent() -> None:
    reg = make_limiters(_settings())
    assert _drain(reg.surface("execute"), "alice") == 3
    assert reg.try_acquire("route", "alice") is True  # per-surface, not pooled
    assert reg.try_acquire("execute", "bob") is True  # per-agent
    assert _drain(reg.surface("decision"), "alice") == 2


def test_window_slides_and_idle_keys_are_pruned() -> None:
    clock = FakeClock()
    reg = LimiterRegistry({"execute": 1}, clock=clock, prune_every=1_000_000)
    for i in range(50):
        assert reg.try_acquire("execute", f"agent-{i}")
    assert reg.try_acquire("execute", "agent-0") is False
    assert len(reg) == 50
    clock.t += 61
    assert reg.prune() == 50 and len(reg) == 0
    assert reg.try_acquire("execute", "agent-0") is True  # window slid


def test_pruning_runs_automatically() -> None:
    clock = FakeClock()
    reg = LimiterRegistry({"execute": 5}, clock=clock, prune_every=10)
    for i in range(9):
        reg.try_acquire("execute", f"old-{i}")
    clock.t += 61
    reg.try_acquire("execute", "new")  # 10th call prunes the 9 idle keys
    assert len(reg) == 1


def test_execution_manager_draws_from_the_registry() -> None:
    from mcprouter.execution.manager import ExecutionManager

    reg = make_limiters(_settings())
    mgr = ExecutionManager.from_settings(_settings(), None, None, limiters=reg)  # type: ignore[arg-type]
    assert isinstance(mgr._limiter, SurfaceLimiter) and mgr._limiter.surface == "execute"
    assert _drain(mgr._limiter, "alice") == 3
    assert reg.try_acquire("execute", "alice") is False  # same budget


@requires_db
def test_feedback_31st_post_is_rate_limited_per_registry(db: sessionmaker[Session]) -> None:
    item = fb.FeedbackItem(kind="tool", id="t1", helpful=True)
    a, b = make_limiters(_settings()), make_limiters(_settings())
    with db() as s:
        for _ in range(FEEDBACK_LIMIT_PER_MIN):
            with pytest.raises(fb.FeedbackNotFound):  # admitted, then the lookup 404s
                fb.record_feedback(
                    s,
                    request_id="missing",
                    items=[item],
                    source="human",
                    agent_id=None,
                    principal="admin",
                    limiter=a.surface("feedback"),
                )
        with pytest.raises(fb.FeedbackRateLimited):
            fb.record_feedback(
                s,
                request_id="missing",
                items=[item],
                source="human",
                agent_id=None,
                principal="admin",
                limiter=a.surface("feedback"),
            )
        with pytest.raises(fb.FeedbackNotFound):  # another app's budget is its own
            fb.record_feedback(
                s,
                request_id="missing",
                items=[item],
                source="human",
                agent_id=None,
                principal="admin",
                limiter=b.surface("feedback"),
            )


@requires_db
def test_each_app_gets_its_own_registry(settings: Settings) -> None:
    from mcprouter.api.app import create_app

    a, b = create_app(settings, env={}), create_app(settings, env={})
    assert isinstance(a.state.limiters, LimiterRegistry)
    assert a.state.limiters is not b.state.limiters


@requires_db
def test_app_wires_every_surface_to_its_registry(settings: Settings) -> None:
    """Integration (wave 6): manager, skills, gateway route/feedback all draw
    from app.state.limiters - no private limiter survives in create_app."""
    from mcprouter.api.app import create_app

    app = create_app(settings, env={})
    reg = app.state.limiters
    gw = app.state.gateway
    wired = {
        "execute": app.state.execution_manager._limiter,
        "skills": app.state.skill_exposure._limiter,
        "route": gw._route_limiter,
        "feedback": gw._feedback_limiter,
    }
    for surface, lim in wired.items():
        assert isinstance(lim, SurfaceLimiter), surface
        assert lim._registry is reg and lim.surface == surface
