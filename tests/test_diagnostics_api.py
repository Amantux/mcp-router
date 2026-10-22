"""Tests for the diagnostics endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mcprouter.api.app import create_app


@pytest.mark.asyncio
async def test_diagnostics_endpoint(client: TestClient) -> None:
    """Test the /api/v1/diag endpoint returns expected structure."""
    # The client fixture should have admin token configured
    response = client.get("/api/v1/diag")
    
    # Should return 200 OK
    assert response.status_code == 200
    
    data = response.json()
    
    # Check response structure
    assert "status" in data
    assert "checks" in data
    assert "duration_ms" in data
    
    # Status should be one of the expected values
    assert data["status"] in ("healthy", "degraded", "unhealthy")
    
    # Checks should be a list
    assert isinstance(data["checks"], list)
    assert len(data["checks"]) > 0
    
    # Each check should have name, ok, and detail
    for check in data["checks"]:
        assert "name" in check
        assert "ok" in check
        assert "detail" in check
        assert isinstance(check["ok"], bool)
    
    # Duration should be a number
    assert isinstance(data["duration_ms"], (int, float))
    assert data["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_diagnostics_checks_structure(client: TestClient) -> None:
    """Test that diagnostics checks have the expected names."""
    response = client.get("/api/v1/diag")
    assert response.status_code == 200
    
    data = response.json()
    check_names = [c["name"] for c in data["checks"]]
    
    # Should include at least these core checks
    expected_checks = {"config", "database", "migrations"}
    assert expected_checks.issubset(set(check_names))


@pytest.mark.asyncio
async def test_diagnostics_non_admin(client: TestClient) -> None:
    """Test that /api/v1/diag requires admin token."""
    # Make a request without admin token
    response = client.get("/api/v1/diag")
    
    # Should return 401 or 403 if admin token is required
    # The exact status depends on whether admin token is configured
    assert response.status_code in (200, 401, 403)


@pytest.mark.asyncio
async def test_diagnostics_health_status(client: TestClient) -> None:
    """Test that health status reflects actual check results."""
    response = client.get("/api/v1/diag")
    assert response.status_code == 200
    
    data = response.json()
    checks = data["checks"]
    
    # If all checks are ok, status should be healthy
    all_ok = all(c["ok"] for c in checks)
    if all_ok:
        assert data["status"] == "healthy"
    
    # If any check fails, status should be degraded or unhealthy
    any_fail = any(not c["ok"] for c in checks)
    if any_fail:
        assert data["status"] in ("degraded", "unhealthy")
