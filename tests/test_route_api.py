"""POST /api/v1/route — SPEC §9 wire contract."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcprouter.api.app import create_app
from mcprouter.api.routes_route import install_routing
from mcprouter.routing.pipeline import RoutePipeline
from mcprouter.routing.retriever import HybridRetriever
from mcprouter.routing.scope import StaticScope
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db
from .test_routing_fakes import (
    ExplodingDecisionModel,
    FakeHashEmbedder,
    ScriptedDecisionModel,
    add_server,
    add_tool,
)

pytestmark = requires_db


KEYS = {"a1": "key-a1-route-test", "ro-agent": "key-ro-route-test"}


def _app(model: object = None, **install_kw: object) -> FastAPI:
    settings = Settings(
        database_url=TEST_DB_URL, agent_keys=",".join(f"{a}:{k}" for a, k in KEYS.items())
    )
    app = create_app(settings, env={})
    factory = app.state.session_factory
    pipeline = RoutePipeline(
        factory,
        HybridRetriever(factory, FakeHashEmbedder()),
        model or ScriptedDecisionModel(),  # type: ignore[arg-type]
        settings,
    )
    install_routing(app, pipeline, **install_kw)  # type: ignore[arg-type]
    return app


def _client(app: FastAPI, agent: str = "a1") -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {KEYS[agent]}"})


@pytest.fixture()
def seeded(db) -> Iterator[dict[str, str]]:  # noqa: ANN001 — conftest sessionmaker
    emb = FakeHashEmbedder()
    with db() as s:
        gh = add_server(s, "github")
        slack = add_server(s, "slack")
        add_tool(s, gh, "search_issues", "Search issues", embedder=emb, domain="development")
        add_tool(
            s,
            gh,
            "create_issue",
            "Create an issue",
            embedder=emb,
            domain="development",
            operation="write",
        )
        add_tool(
            s,
            slack,
            "send_message",
            "Send a message",
            embedder=emb,
            domain="communication",
            operation="write",
        )
        ids = {"github": gh.id, "slack": slack.id}
        s.commit()
    yield ids


def test_route_happy_path_wire_shape(seeded: dict[str, str]) -> None:
    c = _client(_app())
    r = c.post("/api/v1/route", json={"query": "search issues", "agent_id": "a1", "max_tools": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"request_id", "tools", "fallback_used", "latency_ms", "no_match"}
    assert body["fallback_used"] is False and body["no_match"] is False
    assert len(body["tools"]) <= 2
    assert set(body["tools"][0]) == {"server", "tool", "score"}
    assert body["tools"][0] == {
        "server": "github",
        "tool": "search_issues",
        "score": body["tools"][0]["score"],
    }
    assert body["latency_ms"] > 0


def test_max_tools_optional_defaults_to_setting(seeded: dict[str, str]) -> None:
    r = _client(_app()).post("/api/v1/route", json={"query": "issue", "agent_id": "a1"})
    assert r.status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "", "agent_id": "a1"},
        {"query": "   \n\t", "agent_id": "a1"},
        {"agent_id": "a1"},
        {"query": "x", "agent_id": ""},
        {"query": "x", "agent_id": "a1", "max_tools": 0},
        {"query": "x" * 5000, "agent_id": "a1"},
    ],
)
def test_invalid_input_is_422(seeded: dict[str, str], payload: dict[str, object]) -> None:
    assert _client(_app()).post("/api/v1/route", json=payload).status_code == 422


def test_unknown_allowed_servers_are_a_400_not_ignored(seeded: dict[str, str]) -> None:
    """Ignoring an unknown name would turn ["githb"] into "no restriction" —
    silently WIDENING what the caller asked for. Report it instead."""
    c = _client(_app())
    r = c.post(
        "/api/v1/route",
        json={"query": "send message", "agent_id": "a1", "allowed_servers": ["githb"]},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["unknown_servers"] == ["githb"]
    r2 = c.post(
        "/api/v1/route",
        json={"query": "send message", "agent_id": "a1", "allowed_servers": ["github"]},
    )
    assert r2.status_code == 200
    assert {t["server"] for t in r2.json()["tools"]} == {"github"}


def test_scope_resolver_seam_receives_agent_and_narrows(seeded: dict[str, str]) -> None:
    seen: list[str] = []

    def resolver(agent_id: str) -> StaticScope:
        seen.append(agent_id)
        return StaticScope(max_operation="read")

    c = _client(_app(scope_resolver=resolver), "ro-agent")
    r = c.post("/api/v1/route", json={"query": "create issue message", "agent_id": "ro-agent"})
    assert seen == ["ro-agent"]
    assert [t["tool"] for t in r.json()["tools"]] == ["search_issues"]


def test_fallback_flag_on_the_wire(seeded: dict[str, str]) -> None:
    r = _client(_app(ExplodingDecisionModel())).post(
        "/api/v1/route", json={"query": "search issues", "agent_id": "a1"}
    )
    assert r.status_code == 200 and r.json()["fallback_used"] is True


def test_unconfigured_pipeline_is_503(seeded: dict[str, str]) -> None:
    app = _app()
    app.state.route_pipeline = None
    r = _client(app).post("/api/v1/route", json={"query": "x", "agent_id": "a1"})
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "find\u0000issues", "agent_id": "a1"},
        {"query": "x", "agent_id": "a\u0000"},
        {"query": "x", "agent_id": "a1", "allowed_servers": ["git\u0000hub"]},
        {"query": "x", "agent_id": "a1", "allowed_servers": ["g" * 500]},
    ],
)
def test_control_chars_and_oversized_names_are_422(
    seeded: dict[str, str], payload: dict[str, object]
) -> None:
    assert _client(_app()).post("/api/v1/route", json=payload).status_code == 422


def test_422_does_not_echo_the_query(seeded: dict[str, str]) -> None:
    secret = "sk-SECRET-" + "x" * 5000
    r = _client(_app()).post("/api/v1/route", json={"query": secret, "agent_id": "a1"})
    assert r.status_code == 422
    assert "sk-SECRET" not in r.text and len(r.text) < 2000
    assert r.json()["detail"][0]["loc"] == ["body", "query"]


def test_out_of_scope_server_name_indistinguishable_from_unknown(seeded: dict[str, str]) -> None:
    c = _client(_app(scope_resolver=lambda a: StaticScope(servers=(seeded["github"],))))
    r = c.post(
        "/api/v1/route",
        json={"query": "send message", "agent_id": "a1", "allowed_servers": ["slack"]},
    )
    assert r.status_code == 400 and r.json()["detail"]["unknown_servers"] == ["slack"]


# ------------------------------------------------- integration: identity
def test_unauthenticated_route_is_401(seeded: dict[str, str]) -> None:
    r = TestClient(_app()).post("/api/v1/route", json={"query": "search issues"})
    assert r.status_code == 401


def test_body_agent_id_must_match_the_authenticated_agent(seeded: dict[str, str]) -> None:
    """Gateway requirement: identity comes from the credential. A body naming
    another agent is spoofing -> 403; absent or equal is fine."""
    c = _client(_app(), "a1")
    spoof = c.post("/api/v1/route", json={"query": "search issues", "agent_id": "ro-agent"})
    assert spoof.status_code == 403
    assert c.post("/api/v1/route", json={"query": "search issues"}).status_code == 200
    same = c.post("/api/v1/route", json={"query": "search issues", "agent_id": "a1"})
    assert same.status_code == 200


def test_routes_for_the_authenticated_agent_not_the_body(seeded: dict[str, str]) -> None:
    seen: list[str] = []

    def resolver(agent_id: str) -> StaticScope:
        seen.append(agent_id)
        return StaticScope()

    _client(_app(scope_resolver=resolver), "ro-agent").post(
        "/api/v1/route", json={"query": "search issues"}
    )
    assert seen == ["ro-agent"]


def test_camel_case_body_is_accepted(seeded: dict[str, str]) -> None:
    r = _client(_app()).post(
        "/api/v1/route",
        json={
            "query": "search issues",
            "agentId": "a1",
            "maxTools": 1,
            "allowedServers": ["github"],
        },
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["tools"]) == 1 and r.json()["tools"][0]["server"] == "github"


def test_query_is_redacted_before_the_model_and_the_decision_row(
    seeded: dict[str, str],
    db,  # noqa: ANN001 — conftest sessionmaker
) -> None:
    from sqlalchemy import select

    from mcprouter.models import RoutingDecisionRecord

    model = ScriptedDecisionModel()
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    r = _client(_app(model)).post("/api/v1/route", json={"query": f"search issues token={secret}"})
    assert r.status_code == 200
    assert model.calls, "model was consulted"
    assert all(secret not in state for _, state, _, _ in model.calls)
    with db() as s:
        rec = s.scalars(
            select(RoutingDecisionRecord).where(RoutingDecisionRecord.id == r.json()["request_id"])
        ).one()
    assert secret not in rec.query and rec.agent_id == "a1"
