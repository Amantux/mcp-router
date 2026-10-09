"""Route feedback: ownership, simulate exclusion, surfaced-only, upsert, note
scrub, rate limit, human source. Spoofing tests are mutation targets."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.analytics import feedback as fb
from mcprouter.models import RouteFeedback

from .conftest import requires_db
from .test_analytics_api import ADMIN, _app
from .test_analytics_support import NOW, add_decision
from .test_execution_support import KEYS, sec_db_fixture, seed  # noqa: F401 — registers the fixture

ALICE = {"Authorization": f"Bearer {KEYS['alice']}"}
BOB = {"Authorization": f"Bearer {KEYS['bob']}"}
H = {"Authorization": f"Bearer {ADMIN}"}
pytestmark = requires_db


@pytest.fixture()
def env(sec_db: sessionmaker[Session]) -> Iterator[tuple[TestClient, sessionmaker[Session]]]:
    fb.LIMITER._hits.clear()
    seed(sec_db, [])
    yield TestClient(_app(sec_db)), sec_db


def _url(rid: str) -> str:
    return f"/api/v1/route/{rid}/feedback"


def _rows(f: sessionmaker[Session], rid: str) -> list[RouteFeedback]:
    with f() as s:
        return list(s.scalars(select(RouteFeedback).where(RouteFeedback.route_request_id == rid)))


def test_agent_feedback_own_decision_upserts(env: tuple[TestClient, sessionmaker[Session]]) -> None:
    c, f = env
    d = add_decision(f, "alice", NOW, ["t.a", "skill:s1"])
    body = {"items": [{"id": "t.a", "helpful": True, "note": "ok\r\nX-Injected: 1"}]}
    r = c.post(_url(d), json=body, headers=ALICE)
    assert r.status_code == 200 and r.json() == {"recorded": 1, "source": "agent"}
    r = c.post(_url(d), json={"items": [{"id": "t.a", "helpful": False}]}, headers=ALICE)
    rows = _rows(f, d)
    assert len(rows) == 1 and rows[0].helpful is False and rows[0].agent_id == "alice"
    r = c.post(
        _url(d), json={"items": [{"kind": "skill", "id": "s1", "helpful": True}]}, headers=ALICE
    )
    assert r.status_code == 200
    assert {x.target_kind for x in _rows(f, d)} == {"tool", "skill"}


def test_other_agents_decision_is_404(env: tuple[TestClient, sessionmaker[Session]]) -> None:
    c, f = env
    d = add_decision(f, "alice", NOW, ["t.a"])
    r = c.post(_url(d), json={"items": [{"id": "t.a", "helpful": True}]}, headers=BOB)
    assert r.status_code == 404 and _rows(f, d) == []


def test_simulated_decision_is_404(env: tuple[TestClient, sessionmaker[Session]]) -> None:
    c, f = env
    d = add_decision(f, "alice", NOW, ["t.a"], model_version="simulated/x")
    for h in (ALICE, H):
        r = c.post(_url(d), json={"items": [{"id": "t.a", "helpful": True}]}, headers=h)
        assert r.status_code == 404


def test_unsurfaced_target_422(env: tuple[TestClient, sessionmaker[Session]]) -> None:
    c, f = env
    d = add_decision(f, "alice", NOW, ["t.a"])
    r = c.post(_url(d), json={"items": [{"id": "t.zzz", "helpful": True}]}, headers=ALICE)
    assert r.status_code == 422 and _rows(f, d) == []


def test_human_feedback_and_note_scrub(env: tuple[TestClient, sessionmaker[Session]]) -> None:
    c, f = env
    d = add_decision(f, "bob", NOW, ["t.a"])
    note = "line1\r\nline2 " + "x" * 900
    r = c.post(
        _url(d), json={"items": [{"id": "t.a", "helpful": True, "note": note[:2000]}]}, headers=H
    )
    assert r.json() == {"recorded": 1, "source": "human"}
    row = _rows(f, d)[0]
    assert row.agent_id == "bob" and row.source == "human"
    assert row.note is not None and len(row.note) <= 500
    assert "\r" not in row.note and "\n" not in row.note


def test_rate_limited(env: tuple[TestClient, sessionmaker[Session]]) -> None:
    c, f = env
    d = add_decision(f, "alice", NOW, ["t.a"])
    codes = [
        c.post(_url(d), json={"items": [{"id": "t.a", "helpful": True}]}, headers=ALICE).status_code
        for _ in range(fb.LIMITER.limit + 1)
    ]
    assert codes[-1] == 429 and codes[0] == 200


def test_404_probing_is_rate_limited(env: tuple[TestClient, sessionmaker[Session]]) -> None:
    c, _ = env
    body = {"items": [{"id": "t.a", "helpful": True}]}
    codes = [
        c.post(_url(f"nope-{i}"), json=body, headers=BOB).status_code
        for i in range(fb.LIMITER.limit + 1)
    ]
    assert codes[0] == 404 and codes[-1] == 429


def test_note_secret_redacted(env: tuple[TestClient, sessionmaker[Session]]) -> None:
    c, f = env
    d = add_decision(f, "alice", NOW, ["t.a"])
    note = "token Bearer abcdefghijklmnopqrstuvwxyz0123456789"
    c.post(_url(d), json={"items": [{"id": "t.a", "helpful": True, "note": note}]}, headers=ALICE)
    row = _rows(f, d)[0]
    assert row.note is not None and "abcdefghijklmnopqrstuvwxyz0123456789" not in row.note
