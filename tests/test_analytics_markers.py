"""Analytics x routing markers (wave-2 integration).

Budgets/cache write `routing_decisions` rows whose `model_version` carries a
column-free marker:

* ``simulated/<model>`` — an admin `/route/simulate`. Nothing was exposed to
  any agent, so these rows must be INVISIBLE to every analytics surface:
  funnel, economy, routing/agent profiles, position curve, co-surfacing,
  staleness, rollups and the Prometheus totals.
* ``cached/<model>`` — a route served from the route cache. The agent really
  was shown those tools, so these rows COUNT as real traffic.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.analytics import service
from mcprouter.analytics.funnel import LIVE_DECISION_SQL
from mcprouter.analytics.metrics import FunnelCollector
from mcprouter.analytics.rollup import recompute_day
from mcprouter.analytics.window import midnight, parse_window
from mcprouter.models import MCPServerRecord, MCPToolRecord
from mcprouter.routing.pipeline import CACHED_MARKER, SIMULATED_MARKER
from tests.support.analytics import NOW, World, add_decision, add_exec, build_world

from .conftest import requires_db

pytestmark = requires_db

M = timedelta(minutes=1)


@pytest.fixture()
def world(db: sessionmaker[Session]) -> World:
    return build_world(db)


def _metrics(factory: sessionmaker[Session]) -> dict[str, int] | None:
    c = FunnelCollector()  # unregistered: just the compute path
    c.bind(factory)
    c.refresh_now()
    return c._values


def _snapshot(factory: sessionmaker[Session], w: World) -> dict[str, Any]:
    win = parse_window("7d", NOW)
    with factory() as s:
        snap = {
            "overview": service.overview(s, win).model_dump(by_alias=True),
            "tools": service.tool_table(
                s, win, sort="surfaced", descending=True, limit=500, offset=0
            ).model_dump(by_alias=True),
            "detailA": service.tool_detail(s, win, w.A).model_dump(by_alias=True),
            "detailD": service.tool_detail(s, win, w.D).model_dump(by_alias=True),
            "agents": service.agent_profiles(s, win).model_dump(by_alias=True),
            "suggestions": service.suggestions(
                s, win, min_surfaced=1, max_selection_rate=1.0, stale_days=30
            ).model_dump(by_alias=True),
        }
    snap["metrics"] = _metrics(factory)
    return snap


def _age_mystery(factory: sessionmaker[Session], w: World) -> None:
    """Make tool D / server `mystery` old enough to be stale + never-routed."""
    old = NOW - timedelta(days=90)
    with factory() as s:
        s.execute(update(MCPToolRecord).where(MCPToolRecord.id == w.D).values(created_at=old))
        s.execute(
            update(MCPServerRecord).where(MCPServerRecord.name == "mystery").values(created_at=old)
        )
        s.commit()


def test_marker_constants_match_the_routing_track() -> None:
    """The analytics SQL literal must track the pipeline's marker."""
    assert SIMULATED_MARKER == "simulated/"
    assert f"'{SIMULATED_MARKER}'" in LIVE_DECISION_SQL
    assert CACHED_MARKER not in LIVE_DECISION_SQL  # cached rows are real traffic


def test_simulated_decisions_are_invisible_to_all_analytics(
    db: sessionmaker[Session], world: World
) -> None:
    """Mutation target: LIVE_DECISION_SQL in every decision-reading query."""
    _age_mystery(db, world)
    before = _snapshot(db, world)
    names = {i["toolId"] for i in before["suggestions"]["staleTools"]} | {
        i["serverName"] for i in before["suggestions"]["neverRoutedServers"]
    }
    assert world.D in names and "mystery" in names  # precondition

    sim = f"{SIMULATED_MARKER}deterministic-v1"
    # A served simulation (incl. the never-surfaced tool D), a no-match
    # simulation for a fresh agent, and a simulated fallback.
    s1 = add_decision(db, "alice", NOW - 30 * M, [world.D, world.A, world.B], model_version=sim)
    add_decision(db, "carol", NOW - 31 * M, [], model_version=sim, latency_ms=999.0)
    add_decision(
        db,
        "alice",
        NOW - 32 * M,
        [world.C],
        fallback=True,
        model_version=f"{SIMULATED_MARKER}retrieval-fallback/x",
    )
    # Someone replays the simulation's request id as an attribution: it must
    # not turn into a selection either.
    add_exec(db, "alice", world.A, "ok", s1, NOW - 29 * M)
    after = _snapshot(db, world)

    # Executions themselves are real attempts (the call did happen); only
    # their ATTRIBUTION to a simulated decision is void.
    b_ex, a_ex = before["overview"]["executions"], after["overview"]["executions"]
    assert a_ex["attempts"] == b_ex["attempts"] + 1
    assert a_ex["attributed"] == b_ex["attributed"]
    for snap in (before, after):
        snap["overview"]["executions"] = None
        for agent in snap["agents"]["items"]:
            if agent["agentId"] == "alice":
                agent["attempts"] = agent["denialRate"] = agent["attributionCoverage"] = None
    assert after == before


def test_simulated_decisions_are_not_rolled_up(db: sessionmaker[Session], world: World) -> None:
    old_noon = midnight((NOW - timedelta(days=5)).date()) + timedelta(hours=12)
    sim = add_decision(
        db, "alice", old_noon, [world.A], model_version=f"{SIMULATED_MARKER}deterministic-v1"
    )
    add_exec(db, "alice", world.A, "ok", sim, old_noon + M)
    with db() as s:
        res = recompute_day(s, old_noon.date(), NOW)
        s.commit()
    assert res.tool_rows == 0


def test_cached_decisions_count_as_real_traffic(db: sessionmaker[Session], world: World) -> None:
    before = _snapshot(db, world)
    cached = add_decision(
        db, "alice", NOW - 30 * M, [world.A], model_version=f"{CACHED_MARKER}deterministic-v1"
    )
    add_exec(db, "alice", world.A, "ok", cached, NOW - 29 * M)
    after = _snapshot(db, world)

    bf, af = before["overview"]["funnel"], after["overview"]["funnel"]
    assert (af["surfaced"], af["selected"], af["succeeded"]) == (
        bf["surfaced"] + 1,
        bf["selected"] + 1,
        bf["succeeded"] + 1,
    )
    assert (
        after["overview"]["routing"]["decisions"] == before["overview"]["routing"]["decisions"] + 1
    )
    assert (
        after["overview"]["executions"]["attributed"]
        == before["overview"]["executions"]["attributed"] + 1
    )
    assert (
        after["overview"]["contextEconomy"]["servedDecisions"]
        == before["overview"]["contextEconomy"]["servedDecisions"] + 1
    )
    assert after["metrics"] is not None and before["metrics"] is not None
    assert after["metrics"]["surfaced"] == before["metrics"]["surfaced"] + 1
    assert after["metrics"]["selected"] == before["metrics"]["selected"] + 1
    alice = {a["agentId"]: a for a in after["agents"]["items"]}["alice"]
    alice0 = {a["agentId"]: a for a in before["agents"]["items"]}["alice"]
    assert alice["decisions"] == alice0["decisions"] + 1


def test_admin_impersonated_attempts_are_not_agent_behaviour(
    db: sessionmaker[Session], world: World
) -> None:
    """Mutation target: `x.initiated_by IS NULL` in profiles._EXEC_SQL."""
    from mcprouter.models import ExecutionRecord

    before = _snapshot(db, world)
    with db() as s:
        for outcome in ("denied", "denied", "ok"):
            s.add(
                ExecutionRecord(
                    agent_id="alice",
                    tool_id=world.C,
                    outcome=outcome,
                    detail="[admin-initiated, impersonating 'alice'] x",
                    latency_ms=1.0,
                    created_at=NOW - 5 * M,
                    initiated_by="admin",
                )
            )
        s.commit()
    after = _snapshot(db, world)
    assert after["overview"]["executions"] == before["overview"]["executions"]
    assert after["agents"] == before["agents"]
