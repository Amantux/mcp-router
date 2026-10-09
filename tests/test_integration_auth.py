"""Management-API admin gate on the INTEGRATED app (discovery-track blocker).

Every management endpoint sits behind the gateway's ONE admin dependency
(`deps_auth.require_admin`): no credential -> 401, an agent key -> 401 (an
agent key is never an admin credential), the admin token -> through, and no
admin token configured outside dev mode -> 403 (fail closed).

Mutation evidence (docs/history/INTEGRATION_NOTES-integration.md): removing
`dependencies=[Depends(require_admin)]` from `routes_servers.router` makes
`test_management_endpoints_reject_missing_and_agent_credentials[POST /api/v1/servers]`
(and the other servers cases) fail.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcprouter.api.app import create_app
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db

pytestmark = requires_db

ADMIN = "admin-token-integration-" + "x" * 16
AGENT_KEY = "agent-key-integration-" + "y" * 16

STDIO_BODY = {"name": "evil", "transport": "stdio", "command": ["/bin/sh", "-c", "id"]}

# (method, path, json body) — one per management surface.
MANAGEMENT = [
    ("GET", "/api/v1/servers", None),
    ("POST", "/api/v1/servers", STDIO_BODY),
    ("POST", "/api/v1/servers/import", {"mcpServers": {}}),
    ("GET", "/api/v1/servers/00000000-0000-0000-0000-000000000000", None),
    ("POST", "/api/v1/servers/00000000-0000-0000-0000-000000000000/refresh", None),
    ("DELETE", "/api/v1/servers/00000000-0000-0000-0000-000000000000", None),
    ("PATCH", "/api/v1/servers/00000000-0000-0000-0000-000000000000", {"enabled": False}),
    ("GET", "/api/v1/tools", None),
    ("GET", "/api/v1/dedup/suggestions", None),
    ("GET", "/api/v1/models/health", None),
    ("GET", "/api/v1/executions", None),
    ("POST", "/api/v1/route/evaluate", {"dataset": "synthetic_v1"}),
    ("GET", "/api/v1/policy-rules", None),
]
IDS = [f"{m} {p}" for m, p, _ in MANAGEMENT]


@pytest.fixture()
def app(db) -> FastAPI:  # noqa: ANN001 — conftest sessionmaker (cleans principals)
    return create_app(
        Settings(database_url=TEST_DB_URL, agent_keys=f"agent1:{AGENT_KEY}"),
        env={"MCPR_ADMIN_TOKEN": ADMIN},
    )


def _call(c: TestClient, method: str, path: str, body: object, token: str | None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return c.request(method, path, json=body, headers=headers).status_code


@pytest.mark.parametrize(("method", "path", "body"), MANAGEMENT, ids=IDS)
def test_management_endpoints_reject_missing_and_agent_credentials(
    app: FastAPI, method: str, path: str, body: object
) -> None:
    c = TestClient(app)
    assert _call(c, method, path, body, None) == 401
    assert _call(c, method, path, body, AGENT_KEY) == 401
    assert _call(c, method, path, body, "not-the-admin-token") == 401


@pytest.mark.parametrize(("method", "path", "body"), MANAGEMENT, ids=IDS)
def test_admin_token_passes_the_gate(app: FastAPI, method: str, path: str, body: object) -> None:
    status = _call(TestClient(app), method, path, body, ADMIN)
    assert status not in (401, 403), status


def test_stdio_registration_is_not_reachable_without_admin(app: FastAPI) -> None:
    """The concrete exploit: register a stdio command, then refresh it."""
    c = TestClient(app)
    assert c.post("/api/v1/servers", json=STDIO_BODY).status_code == 401
    assert c.get("/api/v1/servers", headers={"Authorization": f"Bearer {ADMIN}"}).json() == []


def test_fails_closed_when_no_admin_token_outside_dev_mode(db) -> None:  # noqa: ANN001
    app = create_app(Settings(database_url=TEST_DB_URL, agent_keys=f"agent1:{AGENT_KEY}"), env={})
    c = TestClient(app)
    for method, path, body in MANAGEMENT:
        assert _call(c, method, path, body, None) == 403, path
        assert _call(c, method, path, body, AGENT_KEY) == 403, path
