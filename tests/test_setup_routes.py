"""Setup wizard backend: status/complete + admin gate."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, inspect

from mcprouter.api.app import create_app
from mcprouter.api.routes_setup import SETUP_COMPLETED_KEY, AppSetting
from tests.support.edge import KEY, _drop_principal, _settings

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


# S seeds `edgebot` from agent_keys; dev mode needs none configured at all.
S_KEYLESS = dataclasses.replace(S, agent_keys="")


def _zero_principals() -> None:
    from mcprouter.models import AgentPrincipal

    eng = create_engine(S.database_url)
    with eng.begin() as conn:
        conn.execute(delete(AgentPrincipal))
    eng.dispose()


def test_dev_mode_first_principal_without_admin_token_is_409(db: object) -> None:
    """D11 / P-205: fresh DB, no token -> 409 with the curated message, nothing
    created, and the admin API stays open (no lock-out)."""
    from mcprouter.api.routes_policy import FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN

    _clear_flag()
    _zero_principals()
    with TestClient(create_app(S_KEYLESS, env={})) as c:
        status = c.get("/api/v1/setup/status")
        assert status.status_code == 200
        assert status.json()["devMode"] is True and status.json()["needsSetup"] is True
        r = c.post("/api/v1/principals", json={"agentId": "first"})
        assert r.status_code == 409
        assert r.json()["detail"] == FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN
        assert "MCPR_ADMIN_TOKEN" in FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN
        listed = c.get("/api/v1/principals")
        assert listed.status_code == 200 and listed.json() == []  # still open


def test_first_principal_with_admin_token_is_201_and_admin_still_works(db: object) -> None:
    _clear_flag()
    _zero_principals()
    h = {"Authorization": f"Bearer {ADMIN}"}
    try:
        with TestClient(create_app(S_KEYLESS, env={"MCPR_ADMIN_TOKEN": ADMIN})) as c:
            r = c.post("/api/v1/principals", json={"agentId": "first"}, headers=h)
            assert r.status_code == 201 and r.json()["apiKey"]
            listed = c.get("/api/v1/principals", headers=h)
            assert listed.status_code == 200
            assert [p["agentId"] for p in listed.json()] == ["first"]
            assert c.get("/api/v1/setup/status", headers=h).status_code == 200
    finally:
        _zero_principals()


def test_init_db_creates_app_settings_idempotently(monkeypatch: pytest.MonkeyPatch) -> None:
    """app_settings comes from the migrations (0001), not a per-boot create_all."""
    from mcprouter.db import init_db

    def boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("init_db must not create_all; migrations own the schema")

    monkeypatch.setattr(AppSetting.metadata, "create_all", boom)
    eng = create_engine(S.database_url)
    try:
        init_db(eng)
        init_db(eng)  # re-running is a no-op, not an error
        assert "app_settings" in inspect(eng).get_table_names()
    finally:
        eng.dispose()


def test_routes_do_not_create_tables_per_request(client: TestClient) -> None:
    def boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("create_all called on the request path")

    with pytest.MonkeyPatch.context() as m:  # undone before the fixture's teardown
        m.setattr(AppSetting.metadata, "create_all", boom)
        assert client.get("/api/v1/setup/status", headers=A).status_code == 200
        assert client.post("/api/v1/setup/complete", headers=A).status_code == 200


def test_complete_survives_a_concurrent_winner(client: TestClient) -> None:
    """A racing POST that read 'not completed' must not IntegrityError-500."""
    from sqlalchemy.orm import Session

    first = client.post("/api/v1/setup/complete", headers=A).json()
    real_get = Session.get

    def stale_get(self: Session, entity: object, ident: object, **kw: object) -> object:
        if entity is AppSetting:  # simulate the loser's stale "row absent" read
            return None
        return real_get(self, entity, ident, **kw)  # type: ignore[call-overload]

    with pytest.MonkeyPatch.context() as m:
        m.setattr(Session, "get", stale_get)
        r = client.post("/api/v1/setup/complete", headers=A)
    assert r.status_code == 200
    assert r.json()["completedAt"] == first["completedAt"]  # first completion wins
