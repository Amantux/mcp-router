"""Dedup review carries read-only routing evidence (wave-2 analytics)."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.dedup.review import accept_suggestion, list_suggestions
from mcprouter.models import DuplicateSuggestion

from .conftest import requires_db
from .test_analytics_support import World, build_world

pytestmark = requires_db


@pytest.fixture()
def world(db: sessionmaker[Session]) -> World:
    return build_world(db)


def test_dedup_suggestions_carry_usage_evidence(db: sessionmaker[Session], world: World) -> None:
    a, b = sorted((world.A, world.B))
    with db() as s:
        s.add(DuplicateSuggestion(tool_a_id=a, tool_b_id=b, similarity=0.9, rationale="r"))
        s.commit()
        page = list_suggestions(s, status="open")
    ev = page.items[0].usage_evidence
    assert ev is not None
    assert (ev.co_surfaced, ev.tool_a_selected, ev.tool_b_selected, ev.both_selected) == (
        2,
        1,
        1,
        0,
    )
    surfaced = {world.A: 3, world.B: 2}
    assert (ev.tool_a_surfaced, ev.tool_b_surfaced) == (surfaced[a], surfaced[b])
    wire = page.items[0].model_dump(by_alias=True)
    assert set(wire["usageEvidence"]) == {
        "window",
        "toolASurfaced",
        "toolBSurfaced",
        "coSurfaced",
        "toolASelected",
        "toolBSelected",
        "bothSelected",
    }


def test_evidence_only_on_open_suggestions(db: sessionmaker[Session], world: World) -> None:
    a, b = sorted((world.A, world.B))
    with db() as s:
        sug = DuplicateSuggestion(tool_a_id=a, tool_b_id=b, similarity=0.9, rationale="r")
        s.add(sug)
        s.commit()
        accept_suggestion(s, sug.id, actor="admin")
        s.commit()
        page = list_suggestions(s, status="accepted")
    assert page.items[0].usage_evidence is None


def test_evidence_failure_degrades_to_null(
    db: sessionmaker[Session], world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import OperationalError

    import mcprouter.dedup.review as review

    def boom(*a: object, **k: object) -> object:
        raise OperationalError("SELECT", {}, Exception("statement timeout"))

    monkeypatch.setattr(review, "pair_evidence", boom)
    a, b = sorted((world.A, world.B))
    with db() as s:
        s.add(DuplicateSuggestion(tool_a_id=a, tool_b_id=b, similarity=0.9, rationale="r"))
        s.commit()
        page = list_suggestions(s, status="open")
    assert page.total == 1 and page.items[0].usage_evidence is None
