"""Setup wizard backend: status/complete + admin gate."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete

from mcprouter.api.app import create_app
from mcprouter.api.routes_setup import SETUP_COMPLETED_KEY, AppSetting
from tests.test_edge_roundtrip import KEY, _drop_principal, _settings

ADMIN = "setup-admin-token-0123456789abcdef"
S = _settings(decision_backend="deterministic", embedding_backend="hash")


def _clear_flag() -> None:
    eng = create_engine(S.database_url)
    AppSetting.metadata.create_all(eng, checkfirst=True)
    with eng.begin() as conn:
        conn.execute(delete(AppSetting).where(AppSetting.key == SETUP_COMPLETED_KEY))
    eng.dispose()


@pytest.fixture
def client() -> Iterator[TestClient]:
    _clear_flag()
    app = create_app(S, env={"MCPR_AGENT_KEYS": f"edgebot:{KEY}", "MCPR_ADMIN_TOKEN": ADMIN})
    try:
        with TestClient(app) as c:
            yield c
    finally:
        _drop_principal(S)
        _clear_flag()


A = {"Authorization": f"Bearer {ADMIN}"}


def test_status_shape(client: TestClient) -> None:
    r = client.get("/api/v1/setup/status", headers=A)
    assert r.status_code == 200
    body = r.json()
    assert body["hasAdminToken"] is True and body["devMode"] is False
    assert body["backend"] == "deterministic"
    assert body["counts"]["principals"] >= 1  # edgebot bootstrapped
    assert body["needsSetup"] is False
    assert set(body["counts"]) == {"principals", "servers", "tools", "skillSources"}


def test_complete_is_idempotent(client: TestClient) -> None:
    first = client.post("/api/v1/setup/complete", headers=A).json()
    second = client.post("/api/v1/setup/complete", headers=A).json()
    assert first["completed"] and first["completedAt"] == second["completedAt"]
    assert (
        client.get("/api/v1/setup/status", headers=A).json()["completedAt"] == first["completedAt"]
    )


@pytest.mark.parametrize(
    "method,path", [("get", "/api/v1/setup/status"), ("post", "/api/v1/setup/complete")]
)
def test_admin_gate(client: TestClient, method: str, path: str) -> None:
    assert getattr(client, method)(path).status_code == 401
    agent = {"Authorization": f"Bearer {KEY}"}
    assert getattr(client, method)(path, headers=agent).status_code in (401, 403)


def test_dev_mode_open_or_fail_closed() -> None:
    app = create_app(S, env={})
    with TestClient(app) as c:
        r = c.get("/api/v1/setup/status")
    if r.status_code == 403:  # shared DB already has principals: fail closed
        return
    body = r.json()
    assert body["devMode"] is True and body["hasAdminToken"] is False
    assert body["needsSetup"] == (body["counts"]["principals"] == 0 and body["completedAt"] is None)
