"""P-209: 404 on unknown principals/rules, 410 on an expired approval, and
the 1 MiB body cap (413) on management/routing routes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.app import create_app
from mcprouter.api.body_limit import MAX_BODY_BYTES
from mcprouter.execution.models import ApprovalRequest
from mcprouter.settings import Settings

from .conftest import requires_db

pytestmark = requires_db

ADMIN = "admin_" + "n" * 40
H = {"Authorization": f"Bearer {ADMIN}"}


@pytest.fixture()
def client(db: sessionmaker[Session], settings: Settings) -> Iterator[TestClient]:
    yield TestClient(create_app(settings, env={"MCPR_ADMIN_TOKEN": ADMIN}), headers=H)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/principals/nope", None),
        ("PATCH", "/api/v1/principals/nope", {"maxTools": 3}),
        ("DELETE", "/api/v1/principals/nope", None),
        ("POST", "/api/v1/principals/nope/rotate-key", None),
        ("PATCH", "/api/v1/policy-rules/nope", {"maxOperation": "write"}),
        ("DELETE", "/api/v1/policy-rules/nope", None),
        ("POST", "/api/v1/approvals/nope/deny", None),
    ],
)
def test_unknown_principal_rule_or_approval_is_404(
    client: TestClient, method: str, path: str, body: dict[str, object] | None
) -> None:
    r = client.request(method, path, json=body)
    assert r.status_code == 404, r.text


def _approval(db: sessionmaker[Session], status: str, expires_in: timedelta) -> str:
    now = datetime.now(UTC)
    with db() as s:
        row = ApprovalRequest(
            agent_id="a",
            tool_id="t",
            server_id="s",
            schema_hash="h",
            arguments={},
            summary={},
            status=status,
            created_at=now - timedelta(hours=2),
            expires_at=now + expires_in,
        )
        s.add(row)
        s.commit()
        return row.id


def test_approve_expired_is_410(client: TestClient, db: sessionmaker[Session]) -> None:
    pending_past = _approval(db, "pending", timedelta(hours=-1))
    r = client.post(f"/api/v1/approvals/{pending_past}/approve")
    assert (r.status_code, r.json()["detail"]) == (410, "approval expired")
    # Now marked expired: a second approve is still 410, deny is "already decided".
    assert client.post(f"/api/v1/approvals/{pending_past}/approve").status_code == 410
    assert client.post(f"/api/v1/approvals/{pending_past}/deny").status_code == 409


def test_deny_pending_past_expiry_records_the_denial(
    client: TestClient, db: sessionmaker[Session]
) -> None:
    """Pinned as-is: deny has no expiry branch (refusing is always safe)."""
    aid = _approval(db, "pending", timedelta(hours=-1))
    r = client.post(f"/api/v1/approvals/{aid}/deny")
    assert r.status_code == 200 and r.json()["status"] == "denied"


@pytest.mark.parametrize(
    "path", ["/api/v1/servers/import", "/api/v1/route", "/api/v1/policy-rules"]
)
def test_body_over_cap_is_413(client: TestClient, path: str) -> None:
    big = b'{"x":"' + b"a" * (MAX_BODY_BYTES + 1) + b'"}'
    r = client.post(path, content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413, r.text


def test_principal_get_by_id_happy(client: TestClient) -> None:
    """Deterministic 2xx for GET /principals/{id} (MT-1 must not depend on order)."""
    created = client.post("/api/v1/principals", json={"agentId": "getter"})
    assert created.status_code == 201
    pid = created.json()["id"]
    r = client.get(f"/api/v1/principals/{pid}")
    assert r.status_code == 200 and r.json()["agentId"] == "getter"
    assert "apiKey" not in r.json()
