"""Tests for config snapshot export/import endpoints.

Coverage:
1. Round-trip: export then import recreates same configuration
2. Secret safety: export never includes credential values or key material
3. Idempotency: second import skips duplicates
4. Version validation: wrong snapshot_version returns 400
5. Imported principal's key authenticates successfully
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mcprouter.api.app import create_app
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db

pytestmark = requires_db

ADMIN = "admin-token-config-snapshot-" + "x" * 16
AGENT_KEY = "agent-key-config-snapshot-" + "y" * 16


@pytest.fixture()
def app_with_data(db) -> tuple[TestClient, str]:
    """Create an app with sample data to export."""
    app = create_app(
        Settings(database_url=TEST_DB_URL, agent_keys=f"agent1:{AGENT_KEY}"),
        env={"MCPR_ADMIN_TOKEN": ADMIN},
    )
    client = TestClient(app)

    # Create sample server
    resp = client.post(
        "/api/v1/servers",
        json={
            "name": "test-server",
            "transport": "stdio",
            "command": ["echo", "hello"],
            "enabled": True,
        },
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 201, resp.json()
    server_id = resp.json()["id"]

    # Create sample principal
    resp = client.post(
        "/api/v1/policy/principals",
        json={"agent_id": "test-agent", "enabled": True, "max_tools": 5},
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 201, resp.json()

    # Create sample policy rule
    resp = client.post(
        "/api/v1/policy/rules",
        json={
            "agent_id": "test-agent",
            "serverId": server_id,
            "toolName": "echo",
            "maxOperation": "read",
        },
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 201, resp.json()

    # Create sample skill source
    resp = client.post(
        "/api/v1/skill-sources",
        json={
            "name": "test-skill-source",
            "kind": "directory",
            "location": "/tmp/skills",
            "enabled": True,
        },
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 201, resp.json()

    return client, ADMIN


def test_export_snapshot_returns_all_config(app_with_data: tuple[TestClient, str]) -> None:
    """GET /api/v1/config/snapshot exports servers, principals, rules, skill sources."""
    client, admin_token = app_with_data

    resp = client.get("/api/v1/config/snapshot", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.json()

    data = resp.json()
    assert data["snapshot_version"] == 1
    assert "exported_at" in data
    assert len(data["servers"]) >= 1
    assert any(s["name"] == "test-server" for s in data["servers"])
    assert len(data["principals"]) >= 1
    assert any(p["agent_id"] == "test-agent" for p in data["principals"])
    assert len(data["policy_rules"]) >= 1
    assert len(data["skill_sources"]) >= 1


def test_export_snapshot_excludes_credentials_and_keys(
    app_with_data: tuple[TestClient, str],
) -> None:
    """Export must never include credential values or key material."""
    client, admin_token = app_with_data

    # Create a server with env credentials
    resp = client.post(
        "/api/v1/servers",
        json={
            "name": "secret-server",
            "transport": "stdio",
            "command": ["echo", "test"],
            "env": {"API_KEY": "secret123", "TOKEN": "super-secret"},
            "enabled": True,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.json()

    # Export and verify no credential values appear
    resp = client.get("/api/v1/config/snapshot", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200, resp.json()

    payload = resp.json()
    export_str = str(payload)

    # Credential VALUES should never appear
    assert "secret123" not in export_str
    assert "super-secret" not in export_str

    # Agent key material should never appear (agent1's key is AGENT_KEY)
    assert AGENT_KEY not in export_str


def test_import_snapshot_creates_new_config(db) -> None:
    """POST /api/v1/config/snapshot/import creates configuration in empty stack."""
    app = create_app(
        Settings(database_url=TEST_DB_URL, agent_keys=f"agent1:{AGENT_KEY}"),
        env={"MCPR_ADMIN_TOKEN": ADMIN},
    )
    client = TestClient(app)

    # Prepare export payload
    export_payload = {
        "snapshot_version": 1,
        "servers": [
            {"name": "imported-server", "transport": "stdio", "command": ["echo", "imported"]}
        ],
        "principals": [
            {"agent_id": "imported-agent", "enabled": True, "max_tools": 10, "max_servers": 5}
        ],
        "policy_rules": [
            {
                "agent_id": "imported-agent",
                "server_id": None,
                "tool_name": "*",
                "max_operation": "read",
            }
        ],
        "skill_sources": [
            {"name": "imported-source", "kind": "directory", "location": "/tmp/imported"}
        ],
    }

    resp = client.post(
        "/api/v1/config/snapshot/import",
        json=export_payload,
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 200, resp.json()

    data = resp.json()
    assert len(data["servers"]) == 1
    assert data["servers"][0]["status"] == "created"
    assert data["servers"][0]["name"] == "imported-server"

    assert len(data["principals"]) == 1
    assert data["principals"][0]["status"] == "created"
    assert data["principals"][0]["name"] == "imported-agent"

    # New principal should have a key
    assert "api_keys" in data
    assert "imported-agent" in data["api_keys"]
    new_key = data["api_keys"]["imported-agent"]
    assert new_key.startswith("mcpr_")

    # Verify the key authenticates
    resp = client.post(
        "/api/v1/route",
        json={
            "agentId": "imported-agent",
            "apiKey": new_key,
            "query": "test",
        },
    )
    assert resp.status_code == 200, resp.json()


def test_import_snapshot_is_idempotent(db) -> None:
    """Second import skips duplicates instead of creating new records."""
    app = create_app(
        Settings(database_url=TEST_DB_URL, agent_keys=f"agent1:{AGENT_KEY}"),
        env={"MCPR_ADMIN_TOKEN": ADMIN},
    )
    client = TestClient(app)

    # First import
    export_payload = {
        "snapshot_version": 1,
        "servers": [
            {"name": "idempotent-server", "transport": "stdio", "command": ["echo", "one"]}
        ],
        "principals": [
            {"agent_id": "idempotent-agent", "enabled": True, "max_tools": 5}
        ],
        "policy_rules": [
            {
                "agent_id": "idempotent-agent",
                "server_id": None,
                "tool_name": "*",
                "max_operation": "read",
            }
        ],
        "skill_sources": [
            {"name": "idempotent-source", "kind": "directory", "location": "/tmp/one"}
        ],
    }

    resp = client.post(
        "/api/v1/config/snapshot/import",
        json=export_payload,
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 200, resp.json()
    first_data = resp.json()

    # Second import (same data)
    resp = client.post(
        "/api/v1/config/snapshot/import",
        json=export_payload,
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 200, resp.json()
    second_data = resp.json()

    # All items should be skipped on second import
    for category in ["servers", "principals", "policy_rules", "skill_sources"]:
        for item in second_data[category]:
            assert item["status"] == "skipped", f"{category}: {item}"


def test_import_snapshot_rejects_wrong_version(db) -> None:
    """Wrong snapshot_version returns 400 with clear message."""
    app = create_app(
        Settings(database_url=TEST_DB_URL, agent_keys=f"agent1:{AGENT_KEY}"),
        env={"MCPR_ADMIN_TOKEN": ADMIN},
    )
    client = TestClient(app)

    # Wrong version
    resp = client.post(
        "/api/v1/config/snapshot/import",
        json={"snapshot_version": 999},
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert "Unsupported snapshot version" in data["detail"]
    assert "999" in data["detail"]
    assert "1" in data["detail"]

    # Missing version
    resp = client.post(
        "/api/v1/config/snapshot/import",
        json={},
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 422  # Pydantic validation


def test_import_snapshot_returns_keys_only_once(db) -> None:
    """Newly created principals return their key exactly once."""
    app = create_app(
        Settings(database_url=TEST_DB_URL, agent_keys=f"agent1:{AGENT_KEY}"),
        env={"MCPR_ADMIN_TOKEN": ADMIN},
    )
    client = TestClient(app)

    export_payload = {
        "snapshot_version": 1,
        "servers": [],
        "principals": [
            {"agent_id": "once-only-agent", "enabled": True, "max_tools": 3}
        ],
        "policy_rules": [],
        "skill_sources": [],
    }

    resp = client.post(
        "/api/v1/config/snapshot/import",
        json=export_payload,
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 200, resp.json()

    data = resp.json()
    assert len(data["principals"]) == 1
    assert data["principals"][0]["status"] == "created"
    assert "api_keys" in data
    assert "once-only-agent" in data["api_keys"]

    key = data["api_keys"]["once-only-agent"]
    assert key.startswith("mcpr_")
    assert len(key) > 30  # mcpr_ + base64(32 bytes)

    # Verify key authenticates
    resp = client.post(
        "/api/v1/route",
        json={"agentId": "once-only-agent", "apiKey": key, "query": "test"},
    )
    assert resp.status_code == 200, resp.json()

    # Wrong key should fail
    resp = client.post(
        "/api/v1/route",
        json={"agentId": "once-only-agent", "apiKey": "wrong-key", "query": "test"},
    )
    assert resp.status_code == 401


def test_import_snapshot_with_existing_data(db) -> None:
    """Import into populated stack skips existing items."""
    app = create_app(
        Settings(database_url=TEST_DB_URL, agent_keys=f"agent1:{AGENT_KEY}"),
        env={"MCPR_ADMIN_TOKEN": ADMIN},
    )
    client = TestClient(app)

    # Create existing data
    resp = client.post(
        "/api/v1/servers",
        json={
            "name": "existing-server",
            "transport": "stdio",
            "command": ["echo", "existing"],
        },
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 201, resp.json()

    resp = client.post(
        "/api/v1/policy/principals",
        json={"agent_id": "existing-agent", "enabled": True},
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 201, resp.json()

    # Import with some overlapping names
    export_payload = {
        "snapshot_version": 1,
        "servers": [
            {"name": "existing-server", "transport": "stdio", "command": ["echo", "one"]},
            {"name": "new-server", "transport": "stdio", "command": ["echo", "two"]},
        ],
        "principals": [
            {"agent_id": "existing-agent", "enabled": True, "max_tools": 5},
            {"agent_id": "new-agent", "enabled": True, "max_tools": 10},
        ],
        "policy_rules": [],
        "skill_sources": [],
    }

    resp = client.post(
        "/api/v1/config/snapshot/import",
        json=export_payload,
        headers={"Authorization": f"Bearer {ADMIN}"},
    )
    assert resp.status_code == 200, resp.json()

    data = resp.json()

    # Existing items should be skipped
    existing_server = next(s for s in data["servers"] if s["name"] == "existing-server")
    assert existing_server["status"] == "skipped"
    assert "already exists" in existing_server["reason"]

    existing_agent = next(p for p in data["principals"] if p["name"] == "existing-agent")
    assert existing_agent["status"] == "skipped"
    assert "already exists" in existing_agent["reason"]

    # New items should be created
    new_server = next(s for s in data["servers"] if s["name"] == "new-server")
    assert new_server["status"] == "created"

    new_agent = next(p for p in data["principals"] if p["name"] == "new-agent")
    assert new_agent["status"] == "created"
    assert "api_keys" in data
    assert "new-agent" in data["api_keys"]
