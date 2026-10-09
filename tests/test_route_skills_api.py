"""S2d item 2: /route and /route/simulate wire shapes for skills.

`tools` stays tools-only; skills arrive in a separate `skills` list with
their own applied budget. Request accepts maxSkills/max_skills and `kinds`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.app import create_app
from mcprouter.models import PolicyRule
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db
from .test_routing_fakes import FakeHashEmbedder, add_server, add_skill, add_tool

pytestmark = requires_db

ADMIN = "admin_" + "k" * 30
KEY = "key-skills-api-agent"
SF = sessionmaker[Session]
Q = "fill pdf form"


@pytest.fixture()
def env(db: SF) -> Iterator[dict[str, Any]]:
    settings = Settings(database_url=TEST_DB_URL, agent_keys=f"sk:{KEY}")
    app = create_app(settings, env={"MCPR_ADMIN_TOKEN": ADMIN})
    emb = FakeHashEmbedder()
    with app.state.session_factory() as s:
        srv = add_server(s, "docs")
        add_tool(s, srv, "fill_pdf_form", "Fill a pdf form", embedder=None)
        for i, name in enumerate(("pdf-form-filler", "pdf-form-helper")):
            sk = add_skill(s, name, "Fill pdf form fields", embedder=emb)
            sk.body_tokens_est = 100 + i
        s.add(PolicyRule(agent_id="sk", server_id=None, max_operation="read"))
        s.add(
            PolicyRule(agent_id="sk", server_id=None, max_operation="read", resource_kind="skill")
        )
        s.commit()
    yield {
        "admin": TestClient(app, headers={"Authorization": f"Bearer {ADMIN}"}),
        "agent": TestClient(app, headers={"Authorization": f"Bearer {KEY}"}),
    }


def _route(env: dict[str, Any], **extra: Any) -> Any:
    return env["agent"].post("/api/v1/route", json={"query": Q, **extra})


def test_tools_exclude_skills_and_skills_have_shape(env: dict[str, Any]) -> None:
    r = _route(env)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [t["tool"] for t in body["tools"]] == ["fill_pdf_form"]
    assert all(set(t) == {"server", "tool", "score"} for t in body["tools"])
    assert body["skills"], body
    assert {s["skill"] for s in body["skills"]} == {"pdf-form-filler", "pdf-form-helper"}
    for s in body["skills"]:
        assert set(s) == {"source", "skill", "score", "bodyTokensEst"}
        assert s["source"] == f"src-{s['skill']}"
        assert s["bodyTokensEst"] in (100, 101)
    assert body["max_skills_applied"] == 3


def test_max_skills_lowers_but_cannot_raise(env: dict[str, Any]) -> None:
    low = _route(env, max_skills=1).json()
    assert low["max_skills_applied"] == 1
    assert len(low["skills"]) == 1
    # Principal cap (1) below the global cap (3): a request of 50 must land on
    # the principal's 1, so neither ceiling can be the one silently skipped.
    principals = env["admin"].get("/api/v1/principals").json()
    pid = next(p["id"] for p in principals if p["agentId"] == "sk")
    r = env["admin"].patch(f"/api/v1/principals/{pid}", json={"maxSkills": 1})
    assert r.status_code == 200, r.text
    high = _route(env, maxSkills=50).json()  # camelCase alias
    assert high["max_skills_applied"] == 1
    assert len(high["skills"]) == 1


def test_kinds_narrow(env: dict[str, Any]) -> None:
    only_tools = _route(env, kinds=["tool"]).json()
    assert only_tools["skills"] == []
    assert only_tools["tools"]
    only_skills = _route(env, kinds=["skill"]).json()
    assert only_skills["tools"] == []
    assert only_skills["skills"]


@pytest.mark.parametrize("kinds", [["prompt"], ["tool", "bogus"], []])
def test_invalid_kinds_is_422(env: dict[str, Any], kinds: list[str]) -> None:
    assert _route(env, kinds=kinds).status_code == 422


def test_simulate_kind_fields_and_skill_clamp(env: dict[str, Any]) -> None:
    r = env["admin"].post(
        "/api/v1/route/simulate", json={"agentId": "sk", "query": Q, "maxSkills": 1}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [t["tool"] for t in body["tools"]] == ["fill_pdf_form"]
    assert len(body["skills"]) == 1
    assert set(body["skills"][0]) == {"skillId", "source", "skill", "score", "bodyTokensEst"}
    assert body["maxSkillsApplied"] == 1
    diag = body["diagnostics"]
    kinds = {c["kind"] for c in diag["candidatesConsidered"]}
    assert kinds == {"tool", "skill"}
    pruned = [p for st in diag["stages"] for p in st["pruned"]]
    assert pruned and all(p["kind"] in ("tool", "skill") for p in pruned)
    assert any(p["kind"] == "skill" for p in pruned)  # maxSkills=1 pruned one
    clamp = next(c for c in diag["budgetClamps"] if c["budget"] == "maxSkills")
    assert clamp == {
        "budget": "maxSkills",
        "requested": 1,
        "principal": 3,
        "globalCap": 3,
        "applied": 1,
        "clampedBy": None,
    }
