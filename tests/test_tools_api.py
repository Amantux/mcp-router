"""HTTP surface for the catalog (/api/v1/tools) and dedup (/api/v1/dedup)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.app import create_app
from mcprouter.api.routes_dedup import router as dedup_router
from mcprouter.api.routes_tools import router as tools_router
from mcprouter.models import MCPToolRecord, ToolVersionRecord
from mcprouter.registry.schema import init_registry
from mcprouter.settings import Settings

from .conftest import requires_db
from .test_registry_fixtures import make_server, make_tool, props_schema, unit_vec

pytestmark = requires_db


def _client(settings: Settings) -> TestClient:
    # Integration adds these two include lines (+ init_registry) to app.py.
    app = create_app(settings)
    init_registry(app.state.engine)
    app.include_router(tools_router)
    app.include_router(dedup_router)
    return TestClient(app)


@pytest.fixture()
def client(db: sessionmaker[Session], settings: Settings) -> Iterator[TestClient]:
    yield _client(settings)


@pytest.fixture()
def ids(db: sessionmaker[Session], client: TestClient) -> dict[str, str]:
    with db() as s:
        gh = make_server(s, "github")
        gl = make_server(s, "gitlab")
        common = {
            "domain": "development",
            "operation": "read",
            "input_schema": props_schema("owner", "repo"),
            "embedding_backend": "hash",
        }
        a = make_tool(
            s,
            gh,
            "list_issues",
            "List issues in a repository",
            embedding=unit_vec((0, 1.0)),
            tags=["issues"],
            call_count=100,
            error_count=0,
            **common,
        )
        b = make_tool(
            s,
            gl,
            "list_issues",
            "List project issues",
            embedding=unit_vec((0, 0.99), (1, 0.14)),
            call_count=100,
            error_count=40,
            **common,
        )
        s.add(
            ToolVersionRecord(
                tool_id=a.id, version=1, schema_hash="h1", snapshot={}, change_kind="added"
            )
        )
        s.commit()
        return {"a": a.id, "b": b.id}


def test_list_search_camelcase_and_pagination(client: TestClient, ids: dict[str, str]) -> None:
    r = client.get("/api/v1/tools", params={"q": "repository issues", "limit": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and body["limit"] == 1 and len(body["items"]) == 1
    top = body["items"][0]
    assert top["id"] == ids["a"]  # matches both terms -> ranked first
    assert top["serverName"] == "github"
    assert top["rank"] > 0
    assert "inputSchema" in top and "requiredScopes" in top and "categories" in top
    assert top["stats"]["successRate"] == 1.0


def test_list_filters_via_camelcase_query_params(client: TestClient, ids: dict[str, str]) -> None:
    r = client.get("/api/v1/tools", params={"tags": "issues", "classificationReviewed": "false"})
    assert [t["id"] for t in r.json()["items"]] == [ids["a"]]
    assert client.get("/api/v1/tools", params={"operation": "nuke"}).status_code == 422


def test_detail_includes_version_history(client: TestClient, ids: dict[str, str]) -> None:
    r = client.get(f"/api/v1/tools/{ids['a']}")
    assert r.status_code == 200
    assert [v["changeKind"] for v in r.json()["versions"]] == ["added"]
    missing = client.get("/api/v1/tools/does-not-exist")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Tool not found."}


def test_patch_classification(
    client: TestClient, ids: dict[str, str], db: sessionmaker[Session]
) -> None:
    r = client.patch(
        f"/api/v1/tools/{ids['b']}/classification",
        json={"operation": "write", "categories": ["triage", "triage"], "requiredScopes": ["x"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["classificationReviewed"] is True
    assert body["categories"] == ["triage"]  # deduped
    assert body["requiredScopes"] == ["x"]
    with db() as s:
        t = s.get(MCPToolRecord, ids["b"])
        assert t is not None and t.capabilities == ["triage"] and t.operation == "write"


@pytest.mark.parametrize(
    "payload",
    [
        {"capabilities": ["x"]},  # wire name is `categories`
        {"operation": "nuke"},
        {"operation": None},
        {"tags": None},
        {"domain": "Not A Domain!"},
        {"tags": [""]},
    ],
)
def test_patch_classification_rejects_bad_payloads(
    client: TestClient, ids: dict[str, str], payload: dict[str, object]
) -> None:
    r = client.patch(f"/api/v1/tools/{ids['a']}/classification", json=payload)
    assert r.status_code == 422


def test_patch_domain_null_clears(client: TestClient, ids: dict[str, str]) -> None:
    r = client.patch(f"/api/v1/tools/{ids['a']}/classification", json={"domain": None})
    assert r.status_code == 200 and r.json()["domain"] is None


def test_enable_disable_audited_and_log_injection_scrubbed(
    client: TestClient,
    ids: dict[str, str],
    db: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with db() as s:
        t = s.get(MCPToolRecord, ids["a"])
        assert t is not None
        t.name = "evil\nFAKE tool.enable actor='root'"
        s.commit()
    with caplog.at_level(logging.INFO, logger="mcprouter.audit"):
        r = client.post(f"/api/v1/tools/{ids['a']}/disable")
        assert r.status_code == 200 and r.json()["enabled"] is False
        r = client.post(f"/api/v1/tools/{ids['a']}/enable")
        assert r.json()["enabled"] is True
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "mcprouter.audit"]
    assert len(lines) == 2
    assert lines[0].startswith("tool.disable ") and lines[1].startswith("tool.enable ")
    assert all("\n" not in line for line in lines)
    assert client.post("/api/v1/tools/nope/disable").status_code == 404


def test_dedup_flow(client: TestClient, ids: dict[str, str]) -> None:
    run = client.post("/api/v1/dedup/suggestions", json={})
    assert run.status_code == 200
    assert run.json()["created"] == 1
    listed = client.get("/api/v1/dedup/suggestions").json()
    assert listed["total"] == 1
    sug = listed["items"][0]
    assert sug["preferredToolId"] == ids["a"]
    assert {sug["toolA"]["serverName"], sug["toolB"]["serverName"]} == {"github", "gitlab"}

    sid = sug["id"]
    assert client.post(f"/api/v1/dedup/suggestions/{sid}/dismiss", json={}).status_code == 422
    blank = client.post(f"/api/v1/dedup/suggestions/{sid}/dismiss", json={"justification": "  "})
    assert blank.status_code == 400
    ok = client.post(
        f"/api/v1/dedup/suggestions/{sid}/dismiss", json={"justification": "different APIs"}
    )
    assert ok.status_code == 200 and ok.json()["status"] == "dismissed"
    again = client.post(f"/api/v1/dedup/suggestions/{sid}/accept")
    assert again.status_code == 409
    assert client.get("/api/v1/dedup/suggestions").json()["total"] == 0
    assert client.get("/api/v1/dedup/suggestions", params={"status": "all"}).json()["total"] == 1
    assert client.post("/api/v1/dedup/suggestions/nope/accept").status_code == 404
    assert client.post("/api/v1/dedup/suggestions", json={"threshold": 0.1}).status_code == 422


def test_dedup_accept_leaves_tools_enabled(client: TestClient, ids: dict[str, str]) -> None:
    client.post("/api/v1/dedup/suggestions", json={})
    sid = client.get("/api/v1/dedup/suggestions").json()["items"][0]["id"]
    r = client.post(f"/api/v1/dedup/suggestions/{sid}/accept")
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    for tid in ids.values():
        assert client.get(f"/api/v1/tools/{tid}").json()["enabled"] is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/tools"),
        ("GET", "/api/v1/tools/x"),
        ("PATCH", "/api/v1/tools/x/classification"),
        ("POST", "/api/v1/tools/x/enable"),
        ("POST", "/api/v1/tools/x/disable"),
        ("GET", "/api/v1/dedup/suggestions"),
        ("POST", "/api/v1/dedup/suggestions"),
        ("POST", "/api/v1/dedup/suggestions/x/accept"),
        ("POST", "/api/v1/dedup/suggestions/x/dismiss"),
    ],
)
def test_management_api_fails_closed_when_auth_is_configured(
    db: sessionmaker[Session], settings: Settings, method: str, path: str
) -> None:
    """With agent keys configured but no admin authenticator wired yet, every
    management endpoint refuses rather than silently running open."""
    from dataclasses import replace

    locked = _client(replace(settings, agent_keys="agent1:secret"))
    r = locked.request(method, path, json={"justification": "x"})
    assert r.status_code == 403
    assert "not configured" in r.json()["detail"]
