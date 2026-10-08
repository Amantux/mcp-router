"""/api/v1/servers: wire shape, validation, credentials write-only, refresh,
curated upstream errors, guarded delete."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from testbed.fleet import generate_fleet
from testbed.harness import InprocFleet, stdio_command_for_index, stdio_env

from mcprouter.api.app import create_app
from mcprouter.api.routes_servers import router
from mcprouter.discovery import DiscoveryService
from mcprouter.models import PolicyRule
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db

pytestmark = requires_db
SECRET = "sk-live-TOPSECRET-42"
SF = sessionmaker[Session]


def _app(fleet: InprocFleet | None = None) -> FastAPI:
    app = create_app(Settings(database_url=TEST_DB_URL))
    app.include_router(router)  # the one include line app.py gets at integration
    if fleet is not None:
        app.state.discovery = DiscoveryService(
            app.state.session_factory, connector_factory=fleet.factory
        )
    return app


@pytest.fixture()
def fleet() -> InprocFleet:
    return InprocFleet(generate_fleet(3))


@pytest.fixture()
def client(db: SF, fleet: InprocFleet) -> Iterator[TestClient]:
    del db  # requested for per-test row cleanup
    with TestClient(_app(fleet)) as c:
        yield c


def _create_inproc(client: TestClient, fleet: InprocFleet, name: str) -> dict[str, object]:
    r = client.post(
        "/api/v1/servers",
        json={"name": name, "transport": "streamable-http", "endpoint": fleet.endpoint(name)},
    )
    assert r.status_code == 201, r.text
    body: dict[str, object] = r.json()
    return body


def test_create_and_list_wire_shape(client: TestClient, fleet: InprocFleet) -> None:
    body = _create_inproc(client, fleet, "github")
    assert set(body) == {
        "id",
        "name",
        "transport",
        "endpoint",
        "enabled",
        "status",
        "version",
        "lastDiscoveredAt",
        "lastHealthAt",
        "toolCount",
        "envNames",
    }
    assert body["status"] == "unknown" and body["toolCount"] == 0
    listed = client.get("/api/v1/servers").json()
    assert [s["name"] for s in listed] == ["github"]
    assert client.get(f"/api/v1/servers/{body['id']}").json()["id"] == body["id"]
    assert client.get("/api/v1/servers/nope").status_code == 404


def test_env_is_write_only(client: TestClient) -> None:
    r = client.post(
        "/api/v1/servers",
        json={
            "name": "gh",
            "transport": "stdio",
            "command": ["npx", "gh-mcp"],
            "env": {"GITHUB_TOKEN": SECRET},
        },
    )
    assert r.status_code == 201
    assert r.json()["envNames"] == ["GITHUB_TOKEN"]
    assert SECRET not in r.text
    assert SECRET not in client.get("/api/v1/servers").text


@pytest.mark.parametrize(
    ("payload", "code", "detail"),
    [
        (
            {"name": "a", "transport": "streamable-http", "endpoint": f"http://u:{SECRET}@h/mcp"},
            400,
            "endpoint URL must not contain credentials",
        ),
        (
            {"name": "a", "transport": "sse", "endpoint": "file:///etc/passwd"},
            400,
            "endpoint URL must use http or https",
        ),
        (
            {"name": "a", "transport": "stdio", "endpoint": "http://h/mcp", "command": ["x"]},
            400,
            "stdio servers take a command, not an endpoint",
        ),
        ({"name": "a", "transport": "websocket", "endpoint": "http://h/mcp"}, 422, None),
    ],
)
def test_create_validation(
    client: TestClient, payload: dict[str, object], code: int, detail: str | None
) -> None:
    r = client.post("/api/v1/servers", json=payload)
    assert r.status_code == code
    if detail is not None:
        assert r.json() == {"detail": detail}
    assert SECRET not in r.text


def test_duplicate_name_conflicts(client: TestClient, fleet: InprocFleet) -> None:
    _create_inproc(client, fleet, "github")
    r = client.post(
        "/api/v1/servers",
        json={"name": "github", "transport": "streamable-http", "endpoint": "http://h/mcp"},
    )
    assert r.status_code == 409


def test_refresh_discovers_tools(client: TestClient, fleet: InprocFleet) -> None:
    sid = _create_inproc(client, fleet, "github")["id"]
    r = client.post(f"/api/v1/servers/{sid}/refresh")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["added"]) == 12 and body["unchanged"] == 0
    assert body["server"]["status"] == "healthy" and body["server"]["toolCount"] == 12
    assert body["server"]["version"] == "1.0.0" and body["server"]["lastDiscoveredAt"]
    assert set(body) >= {"schemaChanged", "metadataChanged", "removed", "restored", "latencyMs"}
    again = client.post(f"/api/v1/servers/{sid}/refresh").json()
    assert again["added"] == [] and again["unchanged"] == 12


def test_refresh_unreachable_is_curated_502(client: TestClient, fleet: InprocFleet) -> None:
    sid = _create_inproc(client, fleet, "jenkins")["id"]
    fleet.down.add("jenkins")
    r = client.post(f"/api/v1/servers/{sid}/refresh")
    assert r.status_code == 502
    assert r.json() == {"detail": "discovery failed: server is unreachable"}
    assert client.get(f"/api/v1/servers/{sid}").json()["status"] == "offline"
    assert client.post("/api/v1/servers/missing/refresh").status_code == 404


def test_refresh_never_leaks_subprocess_stderr(db: SF, tmp_path: Path) -> None:
    del db
    script = tmp_path / "crash.py"
    script.write_text(f"import sys\nsys.stderr.write('{SECRET}\\n')\nsys.exit(1)\n")
    with TestClient(_app()) as c:  # default (real) connector factory
        sid = c.post(
            "/api/v1/servers",
            json={"name": "crashy", "transport": "stdio", "command": [sys.executable, str(script)]},
        ).json()["id"]
        r = c.post(f"/api/v1/servers/{sid}/refresh")
    assert r.status_code in (502, 504)
    assert SECRET not in r.text
    assert r.json()["detail"].startswith("discovery failed: ")


def test_refresh_real_stdio_server(db: SF) -> None:
    del db
    cmd = stdio_command_for_index(20, 13)  # notion
    with TestClient(_app()) as c:
        sid = c.post(
            "/api/v1/servers",
            json={"name": "notion", "transport": "stdio", "command": cmd, "env": stdio_env()},
        ).json()["id"]
        r = c.post(f"/api/v1/servers/{sid}/refresh")
    assert r.status_code == 200, r.text
    assert "search_pages" in r.json()["added"]


def test_import_endpoint(client: TestClient) -> None:
    r = client.post(
        "/api/v1/servers/import",
        json={
            "mcpServers": {
                "gh": {"command": "npx", "args": ["gh"], "env": {"TOKEN": SECRET}},
                "web": {"url": "https://mcp.example.com/mcp"},
                "bad": {"url": "ftp://x"},
            }
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(s["name"] for s in body["created"]) == ["gh", "web"]
    assert body["skipped"] == [{"name": "bad", "reason": "endpoint URL must use http or https"}]
    assert SECRET not in r.text
    assert client.post("/api/v1/servers/import", json={"nope": 1}).status_code == 400


def test_delete_guarded_by_policy_rules(db: SF, client: TestClient, fleet: InprocFleet) -> None:
    sid = str(_create_inproc(client, fleet, "github")["id"])
    client.post(f"/api/v1/servers/{sid}/refresh")
    with db() as s, s.begin():
        s.add(PolicyRule(agent_id="agent-1", server_id=sid))
    r = client.delete(f"/api/v1/servers/{sid}")
    assert r.status_code == 409
    assert "policy rules" in r.json()["detail"]
    with db() as s, s.begin():
        s.execute(PolicyRule.__table__.delete().where(PolicyRule.server_id == sid))
    assert client.delete(f"/api/v1/servers/{sid}").status_code == 204
    assert client.delete(f"/api/v1/servers/{sid}").status_code == 404
