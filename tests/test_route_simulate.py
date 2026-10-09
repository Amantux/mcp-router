"""POST /api/v1/route/simulate (wave 2): admin-only routing AS a named agent,
with diagnostics. Never publishes exposure, never executes, never touches
the route cache; the decision row is marked `simulated/`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.app import create_app
from mcprouter.models import AgentPrincipal, ExecutionRecord, PolicyRule, RoutingDecisionRecord
from mcprouter.settings import Settings
from tests.support.routing_fakes import add_server, add_tool

from .conftest import TEST_DB_URL, requires_db

pytestmark = requires_db

ADMIN = "admin_" + "s" * 30
RO_KEY = "key-sim-ro-agent"
SF = sessionmaker[Session]


@pytest.fixture()
def env(db: SF) -> Iterator[dict[str, Any]]:
    settings = Settings(database_url=TEST_DB_URL, agent_keys=f"ro:{RO_KEY}")
    app = create_app(settings, env={"MCPR_ADMIN_TOKEN": ADMIN})
    factory = app.state.session_factory
    with factory() as s:
        gh = add_server(s, "github")
        slack = add_server(s, "slack")
        add_tool(s, gh, "search_issues", "Search issues", embedder=None, domain="development")
        add_tool(s, gh, "list_issues", "List open issues", embedder=None, domain="development")
        add_tool(
            s,
            gh,
            "create_issue",
            "Create an issue",
            embedder=None,
            domain="development",
            operation="write",
        )
        add_tool(s, slack, "search_messages", "Search issues in messages", embedder=None)
        s.add(PolicyRule(agent_id="ro", server_id=gh.id, max_operation="read"))
        s.commit()
    yield {
        "app": app,
        "db": factory,
        "admin": TestClient(app, headers={"Authorization": f"Bearer {ADMIN}"}),
        "agent": TestClient(app, headers={"Authorization": f"Bearer {RO_KEY}"}),
    }


BODY = {"agentId": "ro", "query": "search create list issues"}


def test_simulate_is_admin_only(env: dict[str, Any]) -> None:
    assert TestClient(env["app"]).post("/api/v1/route/simulate", json=BODY).status_code == 401
    assert env["agent"].post("/api/v1/route/simulate", json=BODY).status_code in (401, 403)
    assert env["admin"].post("/api/v1/route/simulate", json=BODY).status_code == 200


def test_unknown_agent_is_404(env: dict[str, Any]) -> None:
    r = env["admin"].post("/api/v1/route/simulate", json={**BODY, "agentId": "ghost"})
    assert r.status_code == 404


def test_validation_error_does_not_echo_the_query(env: dict[str, Any]) -> None:
    r = env["admin"].post(
        "/api/v1/route/simulate", json={"agentId": "ro", "query": "x\x00 sk-secret", "maxTools": 0}
    )
    assert r.status_code == 422
    assert "sk-secret" not in r.text


def test_simulate_shape_and_diagnostics(env: dict[str, Any]) -> None:
    r = env["admin"].post("/api/v1/route/simulate", json=BODY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "requestId",
        "agentId",
        "simulated",
        "tools",
        "skills",
        "noMatch",
        "fallbackUsed",
        "latencyMs",
        "modelVersion",
        "maxToolsApplied",
        "maxServersApplied",
        "maxSkillsApplied",
        "diagnostics",
    }
    assert body["simulated"] is True and body["agentId"] == "ro"
    assert body["modelVersion"].startswith("simulated/")
    routed = {(t["server"], t["tool"]) for t in body["tools"]}
    assert routed and routed <= {("github", "search_issues"), ("github", "list_issues")}
    assert set(body["tools"][0]) == {"toolId", "server", "tool", "score"}

    diag = body["diagnostics"]
    assert set(diag) == {"candidatesConsidered", "stages", "policyFiltered", "budgetClamps"}
    considered = {c["tool"] for c in diag["candidatesConsidered"]}
    assert considered <= {"search_issues", "list_issues"}
    assert set(diag["candidatesConsidered"][0]) == {
        "toolId",
        "server",
        "tool",
        "domain",
        "operation",
        "retrievalScore",
        "matchedOn",
        "kind",
    }
    assert all(p["kind"] == "tool" for p in diag["policyFiltered"])  # S2d: kind on entries
    reasons = {(p["server"], p["tool"]): p["reason"] for p in diag["policyFiltered"]}
    assert reasons[("github", "create_issue")] == "operation 'write' exceeds policy ceiling"
    assert reasons[("slack", "search_messages")] == "no matching policy rule"
    stages = [st["stage"] for st in diag["stages"]]
    assert stages[0] == "retrieval" and "score" in stages and "maxTools" in stages
    for st in diag["stages"]:
        assert set(st) == {"stage", "before", "after", "pruned", "detail"}
    clamps = {c["budget"]: c for c in diag["budgetClamps"]}
    assert set(clamps) == {"maxTools", "maxServers", "maxSkills"}
    assert set(clamps["maxTools"]) == {
        "budget",
        "requested",
        "principal",
        "globalCap",
        "applied",
        "clampedBy",
    }
    with env["db"]() as s:
        row = s.get(RoutingDecisionRecord, body["requestId"])
        assert row is not None and row.model_version.startswith("simulated/")


def test_simulate_reports_budget_clamps(env: dict[str, Any]) -> None:
    with env["db"]() as s:
        p = s.scalars(select(AgentPrincipal).where(AgentPrincipal.agent_id == "ro")).one()
        p.max_tools = 1
        p.max_servers = 1
        s.commit()
    body = env["admin"].post("/api/v1/route/simulate", json={**BODY, "maxTools": 5}).json()
    assert body["maxToolsApplied"] == 1 and body["maxServersApplied"] == 1
    assert len(body["tools"]) <= 1
    clamp = next(c for c in body["diagnostics"]["budgetClamps"] if c["budget"] == "maxTools")
    assert clamp == {
        "budget": "maxTools",
        "requested": 5,
        "principal": 1,
        "globalCap": 8,
        "applied": 1,
        "clampedBy": "principal",
    }


def test_simulate_never_publishes_executes_or_caches(env: dict[str, Any]) -> None:
    app, admin, agent = env["app"], env["admin"], env["agent"]
    for _ in range(2):
        r = admin.post("/api/v1/route/simulate", json=BODY)
        assert r.status_code == 200
    assert app.state.gateway.exposure.get("ro") is None  # no live session exposure
    with env["db"]() as s:
        assert s.scalar(select(func.count()).select_from(ExecutionRecord)) == 0
    # The cache was neither read nor written: the agent's first live route misses.
    live = agent.post("/api/v1/route", json={"query": BODY["query"]}).json()
    assert live["cached"] is False
    assert app.state.gateway.exposure.get("ro") is not None  # live /route does publish


def test_simulation_matches_the_live_route(env: dict[str, Any]) -> None:
    sim = env["admin"].post("/api/v1/route/simulate", json=BODY).json()
    live = env["agent"].post("/api/v1/route", json={"query": BODY["query"]}).json()
    assert [(t["server"], t["tool"]) for t in sim["tools"]] == [
        (t["server"], t["tool"]) for t in live["tools"]
    ]
