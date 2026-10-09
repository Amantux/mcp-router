"""HTTP surface for the catalog (/api/v1/tools) and dedup (/api/v1/dedup)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.app import create_app
from mcprouter.models import MCPToolRecord, ToolVersionRecord
from mcprouter.settings import Settings
from tests.support.registry_fixtures import make_server, make_tool, props_schema, unit_vec

from .conftest import requires_db

pytestmark = requires_db


def _client(settings: Settings) -> TestClient:
    # create_app wires init_registry + the tools/dedup routers (integration).
    return TestClient(create_app(settings, env={}))


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


def _open_suggestion(client: TestClient) -> dict[str, object]:
    assert client.post("/api/v1/dedup/suggestions", json={}).status_code == 200
    item: dict[str, object] = client.get("/api/v1/dedup/suggestions").json()["items"][0]
    return item


def _stored_preference(db: sessionmaker[Session], sid: str) -> str | None:
    from mcprouter.models import DuplicateSuggestion

    with db() as s:
        row = s.get(DuplicateSuggestion, sid)
        assert row is not None
        return row.preferred_tool_id


def test_dedup_accept_persists_reviewer_pick(
    client: TestClient, ids: dict[str, str], db: sessionmaker[Session]
) -> None:
    """D12 / P-201: the scanner prefers A; the reviewer picks B and B is stored."""
    sug = _open_suggestion(client)
    assert sug["preferredToolId"] == ids["a"]
    r = client.post(
        f"/api/v1/dedup/suggestions/{sug['id']}/accept", json={"preferredToolId": ids["b"]}
    )
    assert r.status_code == 200
    assert r.json()["preferredToolId"] == ids["b"]
    assert _stored_preference(db, str(sug["id"])) == ids["b"]


def test_dedup_accept_rejects_foreign_preference(
    client: TestClient, ids: dict[str, str], db: sessionmaker[Session]
) -> None:
    sug = _open_suggestion(client)
    url = f"/api/v1/dedup/suggestions/{sug['id']}/accept"
    r = client.post(url, json={"preferredToolId": "not-in-pair"})
    assert r.status_code == 422
    assert "toolA or toolB" in r.json()["detail"]
    assert client.post(url, json={"bogus": 1}).status_code == 422  # extra="forbid"
    # Nothing changed: still open with the scanner's pick.
    assert _stored_preference(db, str(sug["id"])) == ids["a"]
    assert client.get("/api/v1/dedup/suggestions").json()["items"][0]["status"] == "open"


def test_dedup_accept_empty_body_keeps_scanner_pick(
    client: TestClient, ids: dict[str, str], db: sessionmaker[Session]
) -> None:
    sug = _open_suggestion(client)
    r = client.post(f"/api/v1/dedup/suggestions/{sug['id']}/accept", json={})
    assert r.status_code == 200 and r.json()["preferredToolId"] == ids["a"]
    assert _stored_preference(db, str(sug["id"])) == ids["a"]


def test_dedup_run_reports_truncated_field(client: TestClient, ids: dict[str, str]) -> None:
    """P-201/P-603: `truncated` is on the wire (null until the detector reports it)."""
    body = client.post("/api/v1/dedup/suggestions", json={}).json()
    assert "truncated" in body and body["truncated"] in (None, False, True)


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
