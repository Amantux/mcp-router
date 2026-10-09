"""REST: principal + rule CRUD (admin) and approvals (admin approve, agent poll)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api import deps_auth
from mcprouter.api.deps_auth import configure_security, hash_key
from mcprouter.api.routes_policy import router
from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.models import AgentPrincipal, ExecutionRecord, PolicyRule
from mcprouter.settings import Settings
from tests.support.execution import (
    KEYS,
    Catalog,
    FakeInvoker,
    add_rule,
    seed,
)

from .conftest import TEST_DB_URL, requires_db

pytestmark = requires_db

ADMIN = "admin_" + "q" * 40
H_ADMIN = {"Authorization": f"Bearer {ADMIN}"}


@pytest.fixture()
def env(sec_db: sessionmaker[Session]) -> Iterator[tuple[TestClient, FakeInvoker, Catalog]]:
    deps_auth._reset_dev_warning_for_tests()
    cat = seed(sec_db, [("github", "list_issues", "read"), ("shell", "run", "execute")])
    app = FastAPI()
    app.state.settings = Settings(database_url=TEST_DB_URL)
    app.state.engine = sec_db.kw["bind"]
    app.state.session_factory = sec_db
    configure_security(app, {"MCPR_ADMIN_TOKEN": ADMIN})
    inv = FakeInvoker()
    app.state.execution_manager = ExecutionManager(
        sec_db, inv, timeout_s=2.0, limiter=SlidingWindowLimiter(100)
    )
    app.include_router(router)
    yield TestClient(app), inv, cat


def _agent(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {KEYS[name]}"}


# ---------------------------------------------------------------- authz
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/principals"),
        ("POST", "/api/v1/principals"),
        ("GET", "/api/v1/policy-rules"),
        ("POST", "/api/v1/policy-rules"),
        ("GET", "/api/v1/approvals"),
        ("POST", "/api/v1/approvals/x/approve"),
        ("POST", "/api/v1/approvals/x/deny"),
    ],
)
def test_admin_routes_reject_agent_keys_and_anonymous(
    env: tuple[TestClient, FakeInvoker, Catalog], method: str, path: str
) -> None:
    c, _, _ = env
    assert c.request(method, path, json={}).status_code == 401
    assert c.request(method, path, json={}, headers=_agent("alice")).status_code == 401


# ------------------------------------------------------------ principals
def test_create_principal_returns_key_exactly_once(
    env: tuple[TestClient, FakeInvoker, Catalog], sec_db: sessionmaker[Session]
) -> None:
    c, _, _ = env
    r = c.post("/api/v1/principals", json={"agentId": "carol", "maxTools": 5}, headers=H_ADMIN)
    assert r.status_code == 201
    body = r.json()
    key = body["apiKey"]
    assert body["agentId"] == "carol" and body["maxTools"] == 5 and "keyHash" not in body
    with sec_db() as s:
        row = s.scalars(select(AgentPrincipal).where(AgentPrincipal.agent_id == "carol")).one()
        assert row.key_hash == hash_key(key) and key not in row.key_hash
    listing = c.get("/api/v1/principals", headers=H_ADMIN)
    assert key not in listing.text and "keyHash" not in listing.text
    assert "carol" in listing.text
    one = c.get(f"/api/v1/principals/{body['id']}", headers=H_ADMIN)
    assert key not in one.text and "apiKey" not in one.json()


def test_duplicate_principal_conflicts(env: tuple[TestClient, FakeInvoker, Catalog]) -> None:
    c, _, _ = env
    assert (
        c.post("/api/v1/principals", json={"agentId": "alice"}, headers=H_ADMIN).status_code == 409
    )


@pytest.mark.parametrize("agent_id", ["", "a b", "x" * 121, "evil\nlog"])
def test_principal_agent_id_validated(
    env: tuple[TestClient, FakeInvoker, Catalog], agent_id: str
) -> None:
    c, _, _ = env
    r = c.post("/api/v1/principals", json={"agentId": agent_id}, headers=H_ADMIN)
    assert r.status_code == 422


def test_rotate_key_invalidates_old(env: tuple[TestClient, FakeInvoker, Catalog]) -> None:
    c, _, cat = env
    pid = cat.principals["alice"].id
    r = c.post(f"/api/v1/principals/{pid}/rotate-key", headers=H_ADMIN)
    assert r.status_code == 200
    new_key = r.json()["apiKey"]
    assert c.get("/api/v1/me", headers=_agent("alice")).status_code == 401
    me = c.get("/api/v1/me", headers={"Authorization": f"Bearer {new_key}"})
    assert me.json()["agentId"] == "alice"


def test_disable_principal(env: tuple[TestClient, FakeInvoker, Catalog]) -> None:
    c, _, cat = env
    pid = cat.principals["alice"].id
    r = c.patch(f"/api/v1/principals/{pid}", json={"enabled": False}, headers=H_ADMIN)
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert c.get("/api/v1/me", headers=_agent("alice")).status_code == 401


def test_delete_principal_also_deletes_its_rules(
    env: tuple[TestClient, FakeInvoker, Catalog], sec_db: sessionmaker[Session]
) -> None:
    """No rule resurrection: re-creating an agent_id must not inherit old grants."""
    c, _, cat = env
    add_rule(sec_db, "alice", max_operation="execute")
    add_rule(sec_db, "bob")
    assert (
        c.delete(f"/api/v1/principals/{cat.principals['alice'].id}", headers=H_ADMIN).status_code
        == 204
    )
    with sec_db() as s:
        assert [r.agent_id for r in s.scalars(select(PolicyRule)).all()] == ["bob"]


# ------------------------------------------------------------------ rules
def test_rule_crud(env: tuple[TestClient, FakeInvoker, Catalog]) -> None:
    c, _, cat = env
    body = {
        "agentId": "alice",
        "serverId": cat.servers["github"].id,
        "toolName": "list_*",
        "maxOperation": "read",
        "requiresApproval": False,
    }
    r = c.post("/api/v1/policy-rules", json=body, headers=H_ADMIN)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert c.get("/api/v1/policy-rules?agentId=alice", headers=H_ADMIN).json()[0]["id"] == rid
    assert c.get("/api/v1/policy-rules?agentId=bob", headers=H_ADMIN).json() == []
    p = c.patch(f"/api/v1/policy-rules/{rid}", json={"requiresApproval": True}, headers=H_ADMIN)
    assert p.json()["requiresApproval"] is True
    assert c.delete(f"/api/v1/policy-rules/{rid}", headers=H_ADMIN).status_code == 204
    assert c.get("/api/v1/policy-rules", headers=H_ADMIN).json() == []


@pytest.mark.parametrize(
    "patch",
    [
        {"maxOperation": "admin"},
        {"maxOperation": "unknown"},
        {"agentId": "nobody"},
        {"serverId": "00000000-0000-0000-0000-000000000000"},
        {"toolName": ""},
        {"toolName": "x" * 201},
    ],
)
def test_rule_validation(
    env: tuple[TestClient, FakeInvoker, Catalog], patch: dict[str, str]
) -> None:
    c, _, _ = env
    body = {"agentId": "alice", "maxOperation": "read"} | patch
    assert c.post("/api/v1/policy-rules", json=body, headers=H_ADMIN).status_code == 422


def test_rule_for_dev_agent_allowed_without_principal(
    env: tuple[TestClient, FakeInvoker, Catalog],
) -> None:
    c, _, _ = env
    r = c.post("/api/v1/policy-rules", json={"agentId": "dev"}, headers=H_ADMIN)
    assert r.status_code == 201


# -------------------------------------------------------------- approvals
async def _pending(cat: Catalog, c: TestClient, sec_db: sessionmaker[Session]) -> str:
    add_rule(sec_db, "alice", requires_approval=True)
    mgr: ExecutionManager = c.app.state.execution_manager  # type: ignore[attr-defined]
    res = await mgr.execute(
        cat.principals["alice"], cat.tools["github.list_issues"], {"repo": "acme/w"}
    )
    assert res.approval_id
    return res.approval_id


async def test_approve_flow_executes_once(
    env: tuple[TestClient, FakeInvoker, Catalog], sec_db: sessionmaker[Session]
) -> None:
    c, inv, cat = env
    aid = await _pending(cat, c, sec_db)
    listing = c.get("/api/v1/approvals?status=pending", headers=H_ADMIN).json()
    assert [a["id"] for a in listing] == [aid]
    assert listing[0]["summary"]["tool"] == "github.list_issues"
    assert "arguments" not in listing[0]  # only the redacted summary leaves
    r = c.post(f"/api/v1/approvals/{aid}/approve", headers=H_ADMIN)
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert c.post(f"/api/v1/approvals/{aid}/approve", headers=H_ADMIN).status_code == 409
    assert len(inv.calls) == 1


async def test_agent_cannot_approve_own_request(
    env: tuple[TestClient, FakeInvoker, Catalog], sec_db: sessionmaker[Session]
) -> None:
    c, inv, cat = env
    aid = await _pending(cat, c, sec_db)
    assert c.post(f"/api/v1/approvals/{aid}/approve", headers=_agent("alice")).status_code == 401
    assert inv.calls == []


async def test_deny_then_approve_refused(
    env: tuple[TestClient, FakeInvoker, Catalog], sec_db: sessionmaker[Session]
) -> None:
    c, inv, cat = env
    aid = await _pending(cat, c, sec_db)
    assert c.post(f"/api/v1/approvals/{aid}/deny", headers=H_ADMIN).status_code == 200
    assert c.post(f"/api/v1/approvals/{aid}/approve", headers=H_ADMIN).status_code == 409
    assert inv.calls == []
    with sec_db() as s:
        assert "denied" in [r.outcome for r in s.scalars(select(ExecutionRecord)).all()]


def test_approve_unknown_is_404(env: tuple[TestClient, FakeInvoker, Catalog]) -> None:
    c, _, _ = env
    assert c.post("/api/v1/approvals/nope/approve", headers=H_ADMIN).status_code == 404


async def test_agent_polls_only_own_approval(
    env: tuple[TestClient, FakeInvoker, Catalog], sec_db: sessionmaker[Session]
) -> None:
    c, _, cat = env
    aid = await _pending(cat, c, sec_db)
    mine = c.get(f"/api/v1/me/approvals/{aid}", headers=_agent("alice"))
    assert mine.status_code == 200 and mine.json()["status"] == "pending"
    assert c.get(f"/api/v1/me/approvals/{aid}", headers=_agent("bob")).status_code == 404
    c.post(f"/api/v1/approvals/{aid}/approve", headers=H_ADMIN)
    done = c.get(f"/api/v1/me/approvals/{aid}", headers=_agent("alice")).json()
    assert done["status"] == "executed" and done["resultPreview"] == "done"


def test_missing_execution_manager_is_503(sec_db: sessionmaker[Session]) -> None:
    app = FastAPI()
    app.state.settings = Settings(database_url=TEST_DB_URL)
    app.state.engine = sec_db.kw["bind"]
    app.state.session_factory = sec_db
    configure_security(app, {"MCPR_ADMIN_TOKEN": ADMIN})
    app.include_router(router)
    assert TestClient(app).get("/api/v1/approvals", headers=H_ADMIN).status_code == 503


def test_rotate_key_survives_a_failing_stream_hook(
    env: tuple[TestClient, FakeInvoker, Catalog], caplog: pytest.LogCaptureFixture
) -> None:
    """Closing the revoked credential's MCP streams is best effort: if the hook
    raises AFTER the commit, the one-time new key must still be returned."""

    class _BrokenGateway:
        def end_stale_streams_threadsafe(self, agent_id: str) -> None:
            raise RuntimeError("password=hunter2 must never be logged")

    c, _, cat = env
    app = cast(Any, c.app)  # TestClient.app is typed as a bare ASGI callable
    app.state.gateway = _BrokenGateway()
    try:
        pid = cat.principals["alice"].id
        with caplog.at_level("WARNING"):
            r = c.post(f"/api/v1/principals/{pid}/rotate-key", headers=H_ADMIN)
        assert r.status_code == 200
        new_key = r.json()["apiKey"]
        assert (
            c.get("/api/v1/me", headers={"Authorization": f"Bearer {new_key}"}).status_code == 200
        )
        assert "revoke_streams_failed" in caplog.text
        assert "hunter2" not in caplog.text
    finally:
        del app.state.gateway
