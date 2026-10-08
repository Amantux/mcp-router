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


def _app(model: object = None, **install_kw: object) -> FastAPI:
    settings = Settings(database_url=TEST_DB_URL)
    app = create_app(settings)
    factory = app.state.session_factory
    pipeline = RoutePipeline(
        factory,
        HybridRetriever(factory, FakeHashEmbedder()),
        model or ScriptedDecisionModel(),  # type: ignore[arg-type]
        settings,
    )
    install_routing(app, pipeline, **install_kw)  # type: ignore[arg-type]
    return app


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
    c = TestClient(_app())
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
    r = TestClient(_app()).post("/api/v1/route", json={"query": "issue", "agent_id": "a1"})
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
    assert TestClient(_app()).post("/api/v1/route", json=payload).status_code == 422


def test_unknown_allowed_servers_are_a_400_not_ignored(seeded: dict[str, str]) -> None:
    """Ignoring an unknown name would turn ["githb"] into "no restriction" —
    silently WIDENING what the caller asked for. Report it instead."""
    c = TestClient(_app())
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

    c = TestClient(_app(scope_resolver=resolver))
    r = c.post("/api/v1/route", json={"query": "create issue message", "agent_id": "ro-agent"})
    assert seen == ["ro-agent"]
    assert [t["tool"] for t in r.json()["tools"]] == ["search_issues"]


def test_fallback_flag_on_the_wire(seeded: dict[str, str]) -> None:
    r = TestClient(_app(ExplodingDecisionModel())).post(
        "/api/v1/route", json={"query": "search issues", "agent_id": "a1"}
    )
    assert r.status_code == 200 and r.json()["fallback_used"] is True


def test_unconfigured_pipeline_is_503(seeded: dict[str, str]) -> None:
    from mcprouter.api.routes_route import router

    app = create_app(Settings(database_url=TEST_DB_URL))
    app.include_router(router)
    r = TestClient(app).post("/api/v1/route", json={"query": "x", "agent_id": "a1"})
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
    assert TestClient(_app()).post("/api/v1/route", json=payload).status_code == 422


def test_422_does_not_echo_the_query(seeded: dict[str, str]) -> None:
    secret = "sk-SECRET-" + "x" * 5000
    r = TestClient(_app()).post("/api/v1/route", json={"query": secret, "agent_id": "a1"})
    assert r.status_code == 422
    assert "sk-SECRET" not in r.text and len(r.text) < 2000
    assert r.json()["detail"][0]["loc"] == ["body", "query"]


def test_out_of_scope_server_name_indistinguishable_from_unknown(seeded: dict[str, str]) -> None:
    c = TestClient(_app(scope_resolver=lambda a: StaticScope(servers=(seeded["github"],))))
    r = c.post(
        "/api/v1/route",
        json={"query": "send message", "agent_id": "a1", "allowed_servers": ["slack"]},
    )
    assert r.status_code == 400 and r.json()["detail"]["unknown_servers"] == ["slack"]
