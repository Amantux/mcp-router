"""Feedback analytics: funnel/overview/agent/wasted counts + the prior blend."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.analytics import funnel as fn
from mcprouter.analytics import service
from mcprouter.analytics.feedback_stats import (
    feedback_by_agent,
    feedback_by_target,
    feedback_coverage,
)
from mcprouter.analytics.window import parse_window
from mcprouter.models import RouteFeedback
from tests.support.analytics import NOW, add_decision

from .conftest import requires_db


def _fb(
    factory: sessionmaker[Session], rid: str, agent: str, tid: str, helpful: bool, source: str
) -> None:
    with factory() as s:
        s.add(
            RouteFeedback(
                route_request_id=rid,
                agent_id=agent,
                target_kind="skill" if tid.startswith("skill:") else "tool",
                target_id=tid,
                helpful=helpful,
                note="free text never exposed",
                source=source,
            )
        )
        s.commit()


def _world(db: sessionmaker[Session]) -> dict[str, str]:
    at = NOW - timedelta(hours=1)
    d1 = add_decision(db, "a1", at, ["fbX", "fbY"])
    d2 = add_decision(db, "a1", at, ["fbX"])
    d3 = add_decision(db, "a2", at, ["fbX"])
    sim = add_decision(db, "a1", at, ["fbX"], model_version="simulated/m")
    add_decision(db, "a2", at, ["fbY"])  # no feedback
    _fb(db, d1, "a1", "fbX", True, "agent")
    _fb(db, d1, "a1", "fbY", False, "agent")
    _fb(db, d2, "a1", "fbX", False, "agent")
    _fb(db, d2, "a1", "fbX", True, "human")  # human counts too
    _fb(db, d3, "a1", "fbX", True, "agent")  # a1 judging a2's decision: IGNORED
    _fb(db, sim, "a1", "fbX", True, "agent")  # simulated: IGNORED
    _fb(db, d3, "a2", "fbX", False, "human")  # human on a2's decision: counts
    return {"d1": d1, "d2": d2, "d3": d3}


@requires_db
def test_feedback_counts_guarded(db: sessionmaker[Session]) -> None:
    _world(db)
    w = parse_window("7d", NOW)
    with db() as s:
        t = feedback_by_target(s, w)
        a = feedback_by_agent(s, w)
        cov = feedback_coverage(s, w)
    assert (t["fbX"].helpful, t["fbX"].unhelpful) == (2, 2)
    assert (t["fbY"].helpful, t["fbY"].unhelpful) == (0, 1)
    assert t["fbX"].helpful_rate == 0.5
    assert (a["a1"].items, a["a2"].items) == (4, 1)
    assert cov == pytest.approx(3 / 4)  # 3 of 4 live decisions have feedback


@requires_db
def test_wire_rows_overview_agents_and_wasted(db: sessionmaker[Session]) -> None:
    _world(db)
    w = parse_window("7d", NOW)
    with db() as s:
        page = service.tool_table(s, w, sort="surfaced", descending=True, limit=500, offset=0)
        rows = {r.tool_id: r for r in page.items}
        ov = service.overview(s, w)
        agents = {p.agent_id: p for p in service.agent_profiles(s, w).items}
        sug = service.suggestions(s, w, min_surfaced=1, max_selection_rate=1.0, stale_days=30)
    assert (rows["fbX"].feedback_helpful, rows["fbX"].feedback_unhelpful) == (2, 2)
    assert rows["fbX"].helpful_rate == 0.5
    dumped = rows["fbX"].model_dump(by_alias=True)
    assert {"feedbackHelpful", "feedbackUnhelpful", "helpfulRate"} <= set(dumped)
    assert "note" not in str(dumped)
    assert ov.feedback.items == 5 and ov.feedback.helpful_rate == pytest.approx(2 / 5)
    assert ov.feedback.coverage == pytest.approx(3 / 4)
    assert agents["a1"].feedback_items == 4 and agents["a1"].helpful_rate == 0.5
    assert agents["a2"].feedback_items == 1 and agents["a2"].helpful_rate == 0.0
    ours = [x for x in sug.wasted_exposure if x.tool_id in {"fbX", "fbY"}]
    # fbX: 2 unhelpful beats fbY: 1 unhelpful, regardless of surfaced
    assert [(x.tool_id, x.unhelpful) for x in ours] == [("fbX", 2), ("fbY", 1)]


def test_no_feedback_row_is_null_rate() -> None:
    from mcprouter.analytics.feedback_stats import FeedbackCounts

    assert FeedbackCounts().helpful_rate is None


def test_wasted_ordering_unhelpful_then_surfaced_then_rate() -> None:
    f = {
        "a": fn.ToolCounts(surfaced=10, selected=0),
        "b": fn.ToolCounts(surfaced=50, selected=1),
        "c": fn.ToolCounts(surfaced=50, selected=0),
        "d": fn.ToolCounts(surfaced=5, selected=0),
    }
    hits = fn.wasted_exposure(f, min_surfaced=1, max_selection_rate=0.5, unhelpful={"d": 3, "a": 1})
    assert [(h.tool_id, h.unhelpful) for h in hits] == [("d", 3), ("a", 1), ("c", 0), ("b", 0)]


@requires_db
def test_human_row_counts_regardless_of_its_agent_id(db: sessionmaker[Session]) -> None:
    d = add_decision(db, "fb-solo", NOW - timedelta(hours=1), ["fbZ"])
    _fb(db, d, "someone-else", "fbZ", False, "human")
    _fb(db, d, "someone-else", "fbZ", True, "agent")  # foreign agent: ignored
    with db() as s:
        t = feedback_by_target(s, parse_window("7d", NOW))
    assert (t["fbZ"].helpful, t["fbZ"].unhelpful) == (0, 1)
