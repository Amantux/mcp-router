"""Tests for the diagnostics endpoint (admin-gated)."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from mcprouter.api.deps_auth import configure_security
from mcprouter.api.routes_diag import install_diag
from mcprouter.limits import make_limiters
from mcprouter.settings import Settings
from tests.conftest import TEST_DB_URL, requires_db
from tests.support.analytics_app import ADMIN, H_ADMIN

pytestmark = requires_db


@pytest.fixture()
def diag_client(sec_db: sessionmaker) -> Iterator[TestClient]:
    app = FastAPI()
    app.state.settings = Settings(database_url=TEST_DB_URL)
    app.state.limiters = make_limiters(app.state.settings)
    app.state.engine = sec_db.kw["bind"]
    app.state.session_factory = sec_db
    configure_security(app, {"MCPR_ADMIN_TOKEN": ADMIN})
    install_diag(app)
    yield TestClient(app)


def test_diagnostics_endpoint(diag_client: TestClient) -> None:
    """The endpoint returns 200 with the expected check structure."""
    response = diag_client.get("/api/v1/diag", headers=H_ADMIN)

    assert response.status_code == 200
    data = response.json()

    assert "status" in data
    assert "checks" in data
    assert "duration_ms" in data

    assert data["status"] in ("healthy", "degraded", "unhealthy")

    assert isinstance(data["checks"], list)
    assert len(data["checks"]) > 0

    for check in data["checks"]:
        assert "name" in check
        assert "ok" in check
        assert "detail" in check
        assert isinstance(check["ok"], bool)

    assert isinstance(data["duration_ms"], (int, float))
    assert data["duration_ms"] >= 0


def test_diagnostics_checks_structure(diag_client: TestClient) -> None:
    """The core probes are present and every check carries a curated detail."""
    data = diag_client.get("/api/v1/diag", headers=H_ADMIN).json()
    names = {c["name"] for c in data["checks"]}

    assert {"config", "database", "migrations"}.issubset(names)
    for c in data["checks"]:
        assert isinstance(c["detail"], str) and c["detail"], c["name"]


def test_diagnostics_non_admin(diag_client: TestClient) -> None:
    """Without the admin token the endpoint must not leak anything."""
    response = diag_client.get("/api/v1/diag")

    assert response.status_code == 401


def test_diagnostics_health_status(diag_client: TestClient) -> None:
    """The aggregate status reflects the check results."""
    data = diag_client.get("/api/v1/diag", headers=H_ADMIN).json()
    checks = data["checks"]
    all_ok = all(c["ok"] for c in checks)
    any_fail = any(not c["ok"] for c in checks)

    if all_ok:
        assert data["status"] == "healthy"
    if any_fail:
        assert data["status"] in ("degraded", "unhealthy")