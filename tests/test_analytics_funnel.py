"""Analytics core against the compose db: funnel, position bias, economy,
profiles, staleness, dedup evidence, rollup idempotency + merge."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.analytics import funnel as fn
from mcprouter.analytics.economy import economy_by_agent, overall
from mcprouter.analytics.profiles import off_funnel_selections, profiles
from mcprouter.analytics.rollup import RollupNotAllowed, recompute_day, recompute_range
from mcprouter.analytics.staleness import stale_report
from mcprouter.analytics.tokens import estimate_tokens, tool_token_map
from mcprouter.analytics.window import live_horizon, midnight, parse_window
from mcprouter.models import (
    MCPServerRecord,
    MCPToolRecord,
    ToolStatsDaily,
)

from .conftest import requires_db
from .test_analytics_support import NOW, World, add_decision, add_exec, build_world

pytestmark = requires_db

OLD_NOON = midnight((NOW - timedelta(days=5)).date()) + timedelta(hours=12)


@pytest.fixture()
def world(db: sessionmaker[Session]) -> World:
    return build_world(db)


def _w(label: str = "7d") -> fn.Window:
    return parse_window(label, NOW)


def _counts(c: fn.ToolCounts) -> tuple[int, int, int, int, int]:
    return (c.surfaced, c.selected, c.succeeded, c.failed, c.sum_rank)


# ------------------------------------------------------------------- funnel
def test_funnel_counts_and_rates(db: sessionmaker[Session], world: World) -> None:
    with db() as s:
        f = fn.merged_funnel(s, _w(), tool_token_map(s))
    assert _counts(f[world.A]) == (3, 1, 1, 0, 4)
    assert _counts(f[world.B]) == (2, 1, 0, 1, 3)
    assert _counts(f[world.C]) == (1, 1, 0, 0, 2)
    assert world.D not in f
    assert f[world.A].selection_rate == pytest.approx(1 / 3)
    assert f[world.A].success_rate == 1.0
    assert f[world.B].success_rate == 0.0
    assert f[world.A].avg_rank == pytest.approx(4 / 3)
    t = fn.totals(f)
    assert (t.surfaced, t.selected, t.succeeded, t.failed) == (6, 3, 1, 1)


def test_zero_denominators_are_none_not_errors() -> None:
    c = fn.ToolCounts()
    assert c.selection_rate is None and c.success_rate is None and c.avg_rank is None
    assert fn.ratio(1, 0) is None


def test_window_excludes_older_decisions(db: sessionmaker[Session], world: World) -> None:
    with db() as s:
        f = fn.merged_funnel(s, _w("4h"), tool_token_map(s))  # drops d4 (-5h)
    assert _counts(f[world.A]) == (2, 1, 1, 0, 3)
    assert world.C not in f


def test_null_attribution_only_world_is_graceful(db: sessionmaker[Session]) -> None:
    """Legacy-only executions (route_request_id NULL): nothing selected, no
    crash, coverage 0 — never mistaken for attributed traffic."""
    from .test_execution_support import seed

    cat = seed(db, [("github", "list_issues", "read")])
    a = cat.tools["github.list_issues"].id
    rid = add_decision(db, "alice", NOW - timedelta(minutes=5), [a])
    add_exec(db, "alice", a, "ok", None, NOW - timedelta(minutes=4))
    w = _w()
    with db() as s:
        f = fn.merged_funnel(s, w, tool_token_map(s))
        total, per = profiles(s, w)
        curve = fn.position_curve(s, w)
    assert _counts(f[a]) == (1, 0, 0, 0, 1)
    assert f[a].selection_rate == 0.0 and f[a].success_rate is None
    assert total.attempts == 1 and total.attributed == 0
    assert total.attribution_coverage == 0.0
    assert curve == []  # no attributed decision => no position evidence
    assert rid


def test_position_curve(db: sessionmaker[Session], world: World) -> None:
    with db() as s:
        curve = fn.position_curve(s, _w())
        a_curve = fn.position_curve(s, _w(), world.A)
    assert [(p.rank, p.shown, p.selected) for p in curve] == [(1, 3, 2), (2, 3, 1)]
    assert curve[0].rate == pytest.approx(2 / 3)
    # A: rank1 in d1 (selected) + d4 (not); rank2 in d2 (not)
    assert [(p.rank, p.shown, p.selected) for p in a_curve] == [(1, 2, 1), (2, 1, 0)]


def test_co_surfacing_and_pair_evidence(db: sessionmaker[Session], world: World) -> None:
    with db() as s:
        co = {c.tool_id: c for c in fn.co_surfaced(s, _w(), world.A)}
        ev = fn.pair_evidence(s, _w(), [(world.A, world.B), (world.A, world.D)])
    assert set(co) == {world.B, world.C}
    assert (co[world.B].co_surfaced, co[world.B].this_selected, co[world.B].other_selected) == (
        2,
        1,
        1,
    )
    assert ev[(world.A, world.B)] == fn.PairEvidence(2, 1, 1, 0)
    assert (world.A, world.D) not in ev


def test_wasted_exposure_threshold_and_order(db: sessionmaker[Session], world: World) -> None:
    with db() as s:
        f = fn.merged_funnel(s, _w(), tool_token_map(s))
    hits = fn.wasted_exposure(f, min_surfaced=2, max_selection_rate=0.4)
    assert [h.tool_id for h in hits] == [world.A]  # 1/3 < 0.4; B is 0.5
    assert fn.wasted_exposure(f, min_surfaced=4, max_selection_rate=1.0) == []


# ------------------------------------------------------------------ economy
def test_token_estimator_is_chars_over_four() -> None:
    rendered = '{"description":"","inputSchema":{"type":"object"},"name":"s.t"}'
    assert estimate_tokens("s", "t", "", None) == -(-len(rendered) // 4)
    # A non-object schema is rendered as the gateway renders it.
    assert estimate_tokens("s", "t", "", {"type": "string"}) == estimate_tokens("s", "t", "", {})


def test_context_economy_components(db: sessionmaker[Session], world: World) -> None:
    with db() as s:
        tok = tool_token_map(s)
        by_agent = economy_by_agent(s, _w(), tok)
    a, b, c, d = (tok[t] for t in (world.A, world.B, world.C, world.D))
    alice = by_agent["alice"]
    assert alice.served_decisions == 3 and alice.no_match_decisions == 0
    assert alice.exposed_tokens == (a + b) + (b + a) + (a + c)
    assert alice.catalog_tokens_per_decision == a + b + c + d  # current policy scope
    assert alice.catalog_tokens == 3 * (a + b + c + d)
    assert alice.tokens_not_sent == alice.catalog_tokens - alice.exposed_tokens
    assert alice.savings == pytest.approx(1 - alice.exposed_tokens / alice.catalog_tokens)
    bob = by_agent["bob"]  # no rules -> empty scope; only a no-match decision
    assert (bob.served_decisions, bob.no_match_decisions, bob.catalog_tokens) == (0, 1, 0)
    assert bob.savings is None
    tot = overall(by_agent)
    assert tot.exposed_tokens == alice.exposed_tokens
    assert tot.catalog_tokens == alice.catalog_tokens


def test_economy_unscored_when_scope_is_empty(db: sessionmaker[Session], world: World) -> None:
    add_decision(db, "carol", NOW - timedelta(minutes=1), [world.A])  # carol: no rules
    with db() as s:
        carol = economy_by_agent(s, _w(), tool_token_map(s))["carol"]
    assert (carol.served_decisions, carol.unscored_decisions, carol.exposed_tokens) == (0, 1, 0)
    assert carol.savings is None


# ------------------------------------------------------------------ profiles
def test_profiles(db: sessionmaker[Session], world: World) -> None:
    with db() as s:
        total, per = profiles(s, _w())
        off = off_funnel_selections(s, _w())
    assert (total.decisions, total.no_match, total.fallback) == (4, 1, 1)
    assert total.no_match_rate == 0.25 and total.fallback_rate == 0.25
    assert total.latency_p50_ms == pytest.approx(25.0)
    assert total.latency_p95_ms == pytest.approx(38.5)
    # attempts exclude 'started': 6 rows; 1 denied; 5 attributed (legacy is NULL)
    assert (total.attempts, total.denied, total.attributed) == (6, 1, 5)
    assert total.denial_rate == pytest.approx(1 / 6)
    assert off == 1  # C called after d1, which did not surface C
    alice, bob = per["alice"], per["bob"]
    assert (alice.decisions, alice.no_match, alice.surfaced, alice.selected) == (3, 0, 6, 3)
    assert alice.avg_surfaced_per_decision == 2.0
    assert (bob.decisions, bob.no_match, bob.attempts) == (1, 1, 0)
    assert bob.denial_rate is None and bob.avg_surfaced_per_decision is None


# ----------------------------------------------------------------- staleness
def test_staleness_suggestions(db: sessionmaker[Session], world: World) -> None:
    old = NOW - timedelta(days=60)
    with db() as s:
        s.execute(update(MCPToolRecord).values(created_at=old))
        s.execute(update(MCPServerRecord).values(created_at=old))
        s.commit()
        stale, never = stale_report(s, NOW, 30)
    assert [t.tool_id for t in stale] == [world.D]  # A/B/C surfaced recently
    assert stale[0].last_surfaced_at is None
    assert [n.server_name for n in never] == ["mystery"]
    with db() as s:  # a young catalog is never flagged
        s.execute(update(MCPToolRecord).values(created_at=NOW))
        s.execute(update(MCPServerRecord).values(created_at=NOW))
        s.commit()
        assert stale_report(s, NOW, 30) == ([], [])


# -------------------------------------------------------------------- rollup
def _old_world(db: sessionmaker[Session], world: World) -> tuple[fn.Window, date]:
    """Mirror the world 5 days back (outside the live horizon), at noon UTC
    so the few minutes of activity never straddle midnight."""
    day_old = OLD_NOON
    o1 = add_decision(db, "alice", day_old, [world.A, world.B])
    o2 = add_decision(db, "alice", day_old + timedelta(minutes=1), [world.B])
    add_exec(db, "alice", world.A, "ok", o1, day_old + timedelta(minutes=2))
    add_exec(db, "alice", world.B, "timeout", o2, day_old + timedelta(minutes=3))
    return _w("10d"), day_old.date()


def _rows(s: Session) -> list[tuple[object, ...]]:
    return [
        (
            r.tool_id,
            r.day,
            r.surfaced,
            r.selected,
            r.succeeded,
            r.failed,
            r.sum_rank,
            r.exposed_tokens,
        )
        for r in s.scalars(select(ToolStatsDaily).order_by(ToolStatsDaily.tool_id)).all()
    ]


def test_rollup_is_idempotent(db: sessionmaker[Session], world: World) -> None:
    """Mutation target: the DELETE in recompute_day (without it the second run
    hits the (tool_id, day) primary key)."""
    _, day = _old_world(db, world)
    with db() as s:
        first = recompute_day(s, day, NOW)
        s.commit()
        rows1 = _rows(s)
    with db() as s:
        second = recompute_day(s, day, NOW)
        s.commit()
        rows2 = _rows(s)
    assert first == second and first.tool_rows == 2
    assert rows1 == rows2 and len(rows1) == 2


def test_rollup_merge_equals_live(db: sessionmaker[Session], world: World) -> None:
    w, day = _old_world(db, world)
    with db() as s:
        tok = tool_token_map(s)
        live = fn.live_funnel(s, w.start, w.end, tok)
        recompute_range(s, day, 1, NOW)
        s.commit()
        assert fn.rolled_days(s, w.start, NOW) == [day]
        merged = fn.merged_funnel(s, w, tok)
    assert {k: _counts(v) for k, v in merged.items()} == {k: _counts(v) for k, v in live.items()}
    assert {k: v.exposed_tokens for k, v in merged.items()} == {
        k: v.exposed_tokens for k, v in live.items()
    }


def test_merge_reads_rollup_for_old_days_and_raw_for_recent(
    db: sessionmaker[Session], world: World
) -> None:
    w, day = _old_world(db, world)
    with db() as s:
        recompute_day(s, day, NOW)
        s.execute(
            update(ToolStatsDaily).where(ToolStatsDaily.tool_id == world.A).values(surfaced=1000)
        )
        s.commit()
        merged = fn.merged_funnel(s, w, tool_token_map(s))
    assert merged[world.A].surfaced == 1000 + 3  # rollup (tampered) + live recent


def test_rollup_refuses_live_window_days(db: sessionmaker[Session]) -> None:
    with db() as s:
        with pytest.raises(RollupNotAllowed):
            recompute_day(s, live_horizon(NOW), NOW)
        with pytest.raises(RollupNotAllowed):
            recompute_range(s, NOW.date(), 3, NOW)
        with pytest.raises(RollupNotAllowed):
            recompute_range(s, live_horizon(NOW) - timedelta(days=1), 0, NOW)
        assert s.scalars(select(ToolStatsDaily)).all() == []


def test_partial_first_day_is_computed_live(db: sessionmaker[Session], world: World) -> None:
    """A window starting mid-day must not use that day's whole rollup row."""
    _, day = _old_world(db, world)  # activity at (NOW - 5d) + 0..3 min
    with db() as s:
        recompute_day(s, day, NOW)
        s.commit()
        start = OLD_NOON + timedelta(minutes=1)  # mid-day, after o1
        assert day not in fn.rolled_days(s, start, NOW)
        w = fn.Window(start=start, end=NOW, label="custom")
        f = fn.merged_funnel(s, w, tool_token_map(s))
    assert f[world.B].surfaced == 2 + 1  # recent 2 + o2 only (o1 is before start)


# ----------------------------------------------------------- review fixes
def test_cross_agent_attribution_is_ignored(db: sessionmaker[Session], world: World) -> None:
    """bob claims alice's decision id: no credit to the funnel or coverage."""
    rid = add_decision(db, "alice", NOW - timedelta(minutes=7), [world.D])
    add_exec(db, "bob", world.D, "ok", rid, NOW - timedelta(minutes=6))
    with db() as s:
        f = fn.merged_funnel(s, _w(), tool_token_map(s))
        _, per = profiles(s, _w())
    assert _counts(f[world.D]) == (1, 0, 0, 0, 1)
    assert per["bob"].attempts == 1 and per["bob"].attributed == 0


def test_deleting_a_server_does_not_inflate_savings(
    db: sessionmaker[Session], world: World
) -> None:
    with db() as s:
        before = economy_by_agent(s, _w(), tool_token_map(s))["alice"]
        s.execute(delete(MCPServerRecord).where(MCPServerRecord.name == "shell"))  # drops C
        s.commit()
        tok = tool_token_map(s)
        after = economy_by_agent(s, _w(), tok)["alice"]
    assert before.stale_ref_decisions == 0 and before.served_decisions == 3
    assert after.stale_ref_decisions == 1 and after.served_decisions == 2  # d4 surfaced C
    # Only d1 + d2 ([A, B] each) are priced; d4 (surfaced the deleted C) is out.
    assert after.exposed_tokens == 2 * (tok[world.A] + tok[world.B])
    assert after.catalog_tokens == 2 * (after.catalog_tokens_per_decision or 0)
    assert after.savings is not None and after.savings < 1.0


def test_position_curve_ignores_off_funnel_only_decisions(
    db: sessionmaker[Session], world: World
) -> None:
    with db() as s:
        base = fn.position_curve(s, _w())
    rid = add_decision(db, "alice", NOW - timedelta(minutes=8), [world.B])
    add_exec(db, "alice", world.D, "ok", rid, NOW - timedelta(minutes=7))  # D not surfaced
    with db() as s:
        assert fn.position_curve(s, _w()) == base


def test_concurrent_rollups_serialize(db: sessionmaker[Session], world: World) -> None:
    """Two overlapping runs for the same day: the advisory lock makes the
    second wait for the first instead of hitting the primary key."""
    import threading

    _, day = _old_world(db, world)
    errors: list[BaseException] = []
    s1 = db()
    recompute_range(s1, day, 1, NOW)  # holds the lock, uncommitted

    def second() -> None:
        try:
            with db() as s2:
                recompute_range(s2, day, 1, NOW)
                s2.commit()
        except BaseException as exc:  # noqa: BLE001 — surfaced via the list
            errors.append(exc)

    t = threading.Thread(target=second)
    t.start()
    t.join(0.5)
    assert t.is_alive()  # blocked on the lock
    s1.commit()
    s1.close()
    t.join(10)
    assert not t.is_alive() and errors == []
    with db() as s:
        assert len(_rows(s)) == 2
