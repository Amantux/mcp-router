"""REST execute: POST /api/v1/tools/{toolId}/execute (wave-2 integration).

Contract (docs/INTEGRATION_NOTES-wave2-ui.md W1): 200 with `status` for every
manager outcome; HTTP codes only for transport/auth. Agent key -> runs as
itself; admin token -> must name agentId and runs under THAT agent's policy
(never wider), audited as impersonation and never attributed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api import deps_auth
from mcprouter.api.deps_auth import configure_security
from mcprouter.api.routes_execute import router
from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.models import ExecutionRecord
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db
from .test_execution_support import (
    KEYS,
    Catalog,
    FakeInvoker,
    add_rule,
    sec_db_fixture,  # noqa: F401 — registers the fixture
    seed,
)

pytestmark = requires_db

ADMIN = "admin_" + "e" * 40
H_ADMIN = {"Authorization": f"Bearer {ADMIN}"}
OK_ARGS: dict[str, Any] = {"repo": "acme/widgets", "limit": 5}
IMPERSONATION = "[admin-initiated via REST, impersonating '{}'] "

Env = tuple[TestClient, FakeInvoker, Catalog, ExecutionManager]


def _build(factory: sessionmaker[Session], env: dict[str, str]) -> tuple[FastAPI, FakeInvoker]:
    deps_auth._reset_dev_warning_for_tests()
    app = FastAPI()
    app.state.settings = Settings(database_url=TEST_DB_URL)
    app.state.engine = factory.kw["bind"]
    app.state.session_factory = factory
    configure_security(app, env)
    inv = FakeInvoker()
    app.state.execution_manager = ExecutionManager(
        factory, inv, timeout_s=2.0, limiter=SlidingWindowLimiter(100)
    )
    app.include_router(router)
    return app, inv


@pytest.fixture()
def env(sec_db: sessionmaker[Session]) -> Iterator[Env]:
    cat = seed(sec_db, [("github", "list_issues", "read"), ("shell", "run", "execute")])
    app, inv = _build(sec_db, {"MCPR_ADMIN_TOKEN": ADMIN})
    yield TestClient(app), inv, cat, app.state.execution_manager


def _agent(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {KEYS[name]}"}


def _url(cat: Catalog, stable: str) -> str:
    return f"/api/v1/tools/{cat.tools[stable].id}/execute"


def _records(factory: sessionmaker[Session]) -> list[ExecutionRecord]:
    with factory() as s:
        return list(s.scalars(select(ExecutionRecord).order_by(ExecutionRecord.created_at)).all())


# ------------------------------------------------------------- agent path
def test_agent_key_executes_as_itself(env: Env, sec_db: sessionmaker[Session]) -> None:
    c, inv, cat, _ = env
    add_rule(sec_db, "alice")
    r = c.post(
        _url(cat, "github.list_issues"), json={"arguments": OK_ARGS}, headers=_agent("alice")
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "status",
        "detail",
        "recordId",
        "approvalId",
        "errors",
        "result",
        "latencyMs",
    }
    assert body["status"] == "ok" and body["errors"] == [] and body["approvalId"] is None
    assert body["result"]["isError"] is False
    assert body["result"]["content"] == [{"type": "text", "text": "done"}]
    assert isinstance(body["latencyMs"], float)
    assert inv.calls == [("github", "list_issues", OK_ARGS)]
    (rec,) = _records(sec_db)
    assert rec.id == body["recordId"] and rec.agent_id == "alice" and rec.outcome == "ok"
    assert "admin-initiated" not in rec.detail and rec.route_request_id is None


def test_policy_denial_is_200_with_status_and_audited(
    env: Env, sec_db: sessionmaker[Session]
) -> None:
    """Mutation target: bypassing ExecutionManager.execute (no policy, no audit)."""
    c, inv, cat, _ = env
    r = c.post(_url(cat, "github.list_issues"), json={"arguments": OK_ARGS}, headers=_agent("bob"))
    assert r.status_code == 200
    assert r.json()["status"] == "denied" and r.json()["result"] is None
    assert inv.calls == []
    assert [(x.agent_id, x.outcome) for x in _records(sec_db)] == [("bob", "denied")]


def test_execute_flows_through_the_one_manager(
    env: Env, sec_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every REST execution is ExecutionManager.execute — no parallel path."""
    c, _, cat, mgr = env
    add_rule(sec_db, "alice")
    seen: list[tuple[str, str]] = []
    real = mgr.execute

    async def spy(principal: Any, tool: Any, arguments: Any, **kw: Any) -> Any:
        seen.append((principal.agent_id, tool if isinstance(tool, str) else tool.id))
        return await real(principal, tool, arguments, **kw)

    monkeypatch.setattr(mgr, "execute", spy)
    tid = cat.tools["github.list_issues"].id
    c.post(_url(cat, "github.list_issues"), json={"arguments": OK_ARGS}, headers=_agent("alice"))
    c.post(
        _url(cat, "github.list_issues") + "?agentId=bob",
        json={"arguments": OK_ARGS},
        headers=H_ADMIN,
    )
    assert seen == [("alice", tid), ("bob", tid)]


def test_invalid_args_and_unknown_tool_are_200(env: Env, sec_db: sessionmaker[Session]) -> None:
    c, inv, cat, _ = env
    add_rule(sec_db, "alice")
    r = c.post(
        _url(cat, "github.list_issues"),
        json={"arguments": {"repo": 5, "nope": "x"}},
        headers=_agent("alice"),
    )
    assert r.status_code == 200 and r.json()["status"] == "invalid_args"
    assert r.json()["errors"] and all("5" not in e for e in r.json()["errors"])
    r = c.post("/api/v1/tools/no-such-tool/execute", json={}, headers=_agent("alice"))
    assert r.status_code == 200 and r.json()["status"] == "denied"
    assert inv.calls == []


def test_pending_approval_returns_approval_id(env: Env, sec_db: sessionmaker[Session]) -> None:
    c, inv, cat, _ = env
    add_rule(sec_db, "alice", requires_approval=True)
    r = c.post(
        _url(cat, "github.list_issues"), json={"arguments": OK_ARGS}, headers=_agent("alice")
    )
    assert r.status_code == 200
    assert r.json()["status"] == "pending_approval" and r.json()["approvalId"]
    assert inv.calls == []


def test_agent_route_request_id_is_recorded(env: Env, sec_db: sessionmaker[Session]) -> None:
    c, _, cat, _ = env
    add_rule(sec_db, "alice")
    rrid = str(uuid.uuid4())
    r = c.post(
        _url(cat, "github.list_issues"),
        json={"arguments": OK_ARGS, "routeRequestId": rrid},
        headers=_agent("alice"),
    )
    assert r.json()["status"] == "ok"
    assert [x.route_request_id for x in _records(sec_db)] == [rrid]


def test_transport_errors_use_http_codes(env: Env) -> None:
    c, inv, cat, _ = env
    url = _url(cat, "github.list_issues")
    assert c.post(url, json={"arguments": OK_ARGS}).status_code == 401
    bad = {"Authorization": "Bearer not-a-real-key"}
    assert c.post(url, json={"arguments": OK_ARGS}, headers=bad).status_code == 401
    assert c.post(url, json={"arguments": [1]}, headers=_agent("alice")).status_code == 422
    assert c.post(url, json={"bogus": 1}, headers=_agent("alice")).status_code == 422
    assert inv.calls == []


def test_agent_key_cannot_name_another_agent(env: Env, sec_db: sessionmaker[Session]) -> None:
    c, inv, cat, _ = env
    add_rule(sec_db, "bob")
    url = _url(cat, "github.list_issues")
    r = c.post(url + "?agentId=bob", json={"arguments": OK_ARGS}, headers=_agent("alice"))
    assert r.status_code == 403
    r = c.post(url, json={"arguments": OK_ARGS, "agentId": "bob"}, headers=_agent("alice"))
    assert r.status_code == 403
    # Naming yourself is fine.
    add_rule(sec_db, "alice")
    r = c.post(url + "?agentId=alice", json={"arguments": OK_ARGS}, headers=_agent("alice"))
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert len(inv.calls) == 1 and all(x.agent_id == "alice" for x in _records(sec_db))


# ------------------------------------------------------------- admin path
def test_admin_without_agent_id_is_400(env: Env, sec_db: sessionmaker[Session]) -> None:
    c, inv, cat, _ = env
    r = c.post(_url(cat, "github.list_issues"), json={"arguments": OK_ARGS}, headers=H_ADMIN)
    assert r.status_code == 400 and "agentId" in r.json()["detail"]
    assert inv.calls == [] and _records(sec_db) == []


def test_admin_unknown_agent_is_404_and_mismatch_is_400(
    env: Env, sec_db: sessionmaker[Session]
) -> None:
    c, inv, cat, _ = env
    url = _url(cat, "github.list_issues")
    assert c.post(url + "?agentId=ghost", json={}, headers=H_ADMIN).status_code == 404
    # The synthetic dev principal exists only in dev mode, never by name.
    assert c.post(url + "?agentId=dev", json={}, headers=H_ADMIN).status_code == 404
    r = c.post(url + "?agentId=alice", json={"agentId": "bob"}, headers=H_ADMIN)
    assert r.status_code == 400
    assert inv.calls == [] and _records(sec_db) == []


def test_admin_impersonation_is_evaluated_under_the_agents_policy(
    env: Env, sec_db: sessionmaker[Session]
) -> None:
    """Mutation target: impersonation must not skip/widen policy evaluation."""
    c, inv, cat, _ = env
    add_rule(sec_db, "alice", max_operation="read")
    # bob has no rules at all: deny by default, even via the admin token.
    r = c.post(
        _url(cat, "github.list_issues") + "?agentId=bob",
        json={"arguments": OK_ARGS},
        headers=H_ADMIN,
    )
    assert r.status_code == 200 and r.json()["status"] == "denied"
    # alice is read-only: an execute-class tool stays denied via the admin token.
    r = c.post(
        _url(cat, "shell.run") + "?agentId=alice",
        json={"arguments": {"repo": "x"}},
        headers=H_ADMIN,
    )
    assert r.status_code == 200 and r.json()["status"] == "denied"
    assert "ceiling" in r.json()["detail"]
    assert inv.calls == []
    recs = _records(sec_db)
    assert [(x.agent_id, x.outcome) for x in recs] == [("bob", "denied"), ("alice", "denied")]
    assert recs[0].detail.startswith(IMPERSONATION.format("bob"))
    assert recs[1].detail.startswith(IMPERSONATION.format("alice"))


def test_admin_impersonation_ok_is_audited_and_never_attributed(
    env: Env, sec_db: sessionmaker[Session]
) -> None:
    c, inv, cat, _ = env
    add_rule(sec_db, "alice")
    r = c.post(
        _url(cat, "github.list_issues"),
        json={"arguments": OK_ARGS, "agentId": "alice", "routeRequestId": str(uuid.uuid4())},
        headers=H_ADMIN,
    )
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert inv.calls == [("github", "list_issues", OK_ARGS)]
    (rec,) = _records(sec_db)
    assert rec.agent_id == "alice" and rec.outcome == "ok"
    assert rec.detail.startswith(IMPERSONATION.format("alice"))
    assert rec.route_request_id is None  # an admin trying a tool is not a selection
    assert "admin-initiated" not in r.json()["detail"]  # audit-only annotation


# ------------------------------------------------------------- dev mode
def test_dev_mode_runs_as_dev_under_deny_by_default(sec_db: sessionmaker[Session]) -> None:
    seed(sec_db, [("github", "list_issues", "read")], agents=())
    app, inv = _build(sec_db, {})
    c = TestClient(app)
    with sec_db() as s:
        from mcprouter.models import MCPToolRecord

        tid = s.scalars(select(MCPToolRecord.id)).one()
    r = c.post(f"/api/v1/tools/{tid}/execute", json={"arguments": OK_ARGS})
    assert r.status_code == 200 and r.json()["status"] == "denied"
    assert inv.calls == [] and [x.agent_id for x in _records(sec_db)] == ["dev"]
