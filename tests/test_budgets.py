"""Exposure budgets (wave 2): the clamp chain

    effective = min(request or principal default, principal cap, global cap)

for maxTools and the new maxServers. A request may LOWER a budget, never
raise it. Covers the pure helper, /api/v1/route and the MCP gateway."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.app import create_app
from mcprouter.gateway.server import FEEDBACK_TOOL, META_TOOL
from mcprouter.interfaces import RouteRequest
from mcprouter.models import AgentPrincipal
from mcprouter.routing.budgets import cap_servers, effective_budgets
from mcprouter.routing.pipeline import RoutePipeline
from mcprouter.routing.retriever import HybridRetriever
from mcprouter.routing.scope import AllowAllScope
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db
from .test_execution_support import add_rule
from .test_execution_support import sec_db_fixture as sec_db_fixture  # noqa: F401
from .test_gateway_mcp import _names
from .test_gateway_mcp import world as world  # noqa: F401
from .test_routing_fakes import FakeHashEmbedder, ScriptedDecisionModel, add_server, add_tool


def _p(max_tools: int = 8, max_servers: int | None = None) -> AgentPrincipal:
    return AgentPrincipal(agent_id="a", key_hash="", max_tools=max_tools, max_servers=max_servers)


# ------------------------------------------------------------------ helper
@pytest.mark.parametrize(
    ("requested", "principal", "global_cap", "applied", "clamped_by"),
    [
        (None, 5, 8, 5, None),  # omitted -> principal default (not a clamp)
        (None, 12, 8, 8, "global"),  # principal default cut by the global cap
        (3, 5, 8, 3, None),  # request lowers
        (7, 5, 8, 5, "principal"),  # request cannot raise past the principal
        (7, 12, 6, 6, "global"),  # ... nor past the global cap
        (20, 30, 8, 8, "global"),
    ],
)
def test_max_tools_clamp_chain(
    requested: int | None, principal: int, global_cap: int, applied: int, clamped_by: str | None
) -> None:
    b = effective_budgets(
        _p(max_tools=principal),
        Settings(max_exposed_tools=global_cap),
        requested_tools=requested,
    )
    assert b.max_tools == applied
    assert b.clamps[0].budget == "maxTools"
    assert b.clamps[0].clamped_by == clamped_by


@pytest.mark.parametrize(
    ("requested", "principal", "global_cap", "applied"),
    [
        (None, None, None, None),  # unlimited everywhere
        (2, None, None, 2),
        (None, 3, None, 3),
        (None, None, 4, 4),
        (5, 3, None, 3),  # request cannot raise past the principal
        (5, None, 2, 2),  # ... nor past the global cap
        (1, 3, 2, 1),
    ],
)
def test_max_servers_clamp_chain(
    requested: int | None, principal: int | None, global_cap: int | None, applied: int | None
) -> None:
    b = effective_budgets(
        _p(max_servers=principal),
        Settings(max_exposed_servers=global_cap),
        requested_servers=requested,
    )
    assert b.max_servers == applied


def test_cap_servers_keeps_rank_order_and_backfills() -> None:
    items = [("a", 1), ("b", 2), ("a", 3), ("c", 4), ("b", 5), ("a", 6)]
    assert cap_servers(items, lambda i: i[0], 2) == [
        ("a", 1),
        ("b", 2),
        ("a", 3),
        ("b", 5),
        ("a", 6),
    ]
    assert cap_servers(items, lambda i: i[0], None) == items
    assert cap_servers(items, lambda i: i[0], 1) == [("a", 1), ("a", 3), ("a", 6)]


def test_max_exposed_servers_setting_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_MAX_EXPOSED_SERVERS", "")
    assert Settings.from_env().max_exposed_servers is None  # empty string = unset
    monkeypatch.setenv("MCPR_MAX_EXPOSED_SERVERS", "3")
    assert Settings.from_env().max_exposed_servers == 3
    monkeypatch.setenv("MCPR_MAX_EXPOSED_SERVERS", "0")
    with pytest.raises(ValueError):
        Settings.from_env()


# ---------------------------------------------------------------- pipeline
@requires_db
def test_pipeline_caps_distinct_servers_in_rank_order(db: sessionmaker[Session]) -> None:
    emb = FakeHashEmbedder()
    with db() as s:
        for name in ("alpha", "beta", "gamma"):
            srv = add_server(s, name)
            for i in range(2):
                add_tool(s, srv, f"issue_tool_{i}", "issue tracker tool", embedder=emb)
        s.commit()
    pipe = RoutePipeline(
        db, HybridRetriever(db, emb), ScriptedDecisionModel(), Settings(database_url=TEST_DB_URL)
    )
    res = pipe.route(RouteRequest("issue tool", "a", 6, max_servers=2), AllowAllScope())
    assert len({t.server_name for t in res.tools}) == 2
    assert len(res.tools) == 4  # both tools of the two kept servers (back-filled)
    # The global cap applies even when the request carries none.
    pipe2 = RoutePipeline(
        db,
        HybridRetriever(db, emb),
        ScriptedDecisionModel(),
        Settings(database_url=TEST_DB_URL, max_exposed_servers=1),
    )
    res2 = pipe2.route(RouteRequest("issue tool", "a", 6), AllowAllScope())
    assert len({t.server_name for t in res2.tools}) == 1


# --------------------------------------------------------------- REST API
KEY = "key-budget-test-a1"


@pytest.fixture()
def api(db: sessionmaker[Session]) -> Iterator[tuple[TestClient, sessionmaker[Session]]]:
    settings = Settings(database_url=TEST_DB_URL, agent_keys=f"a1:{KEY}", max_exposed_tools=8)
    app = create_app(settings, env={})
    factory = app.state.session_factory
    with factory() as s:
        for name in ("alpha", "beta", "gamma"):
            srv = add_server(s, name)
            for i in range(3):
                add_tool(s, srv, f"issue_tool_{i}", "issue tracker tool", embedder=None)
        s.commit()
    add_rule(factory, "a1", max_operation="read")
    yield TestClient(app, headers={"Authorization": f"Bearer {KEY}"}), factory


def _set_principal(factory: sessionmaker[Session], **fields: Any) -> None:
    with factory() as s:
        p = s.scalars(select(AgentPrincipal).where(AgentPrincipal.agent_id == "a1")).one()
        for k, v in fields.items():
            setattr(p, k, v)
        s.commit()


@requires_db
def test_route_request_cannot_raise_past_principal_max_tools(
    api: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api
    _set_principal(factory, max_tools=2)
    r = client.post("/api/v1/route", json={"query": "issue tool", "maxTools": 6})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_tools_applied"] == 2
    assert len(body["tools"]) <= 2
    # A request may lower it.
    r = client.post("/api/v1/route", json={"query": "issue tool", "max_tools": 1})
    assert r.json()["max_tools_applied"] == 1
    assert len(r.json()["tools"]) <= 1


@requires_db
def test_route_max_servers_both_casings_and_principal_cap(
    api: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = api
    r = client.post("/api/v1/route", json={"query": "issue tracker tool", "maxServers": 1})
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["max_servers_applied"] == 1
    assert len({t["server"] for t in body["tools"]}) == 1
    r = client.post("/api/v1/route", json={"query": "issue tracker tool", "max_servers": 2})
    assert r.json()["max_servers_applied"] == 2
    # Principal cap: a request cannot raise past it.
    _set_principal(factory, max_servers=1)
    r = client.post("/api/v1/route", json={"query": "issue tracker tool", "maxServers": 3})
    body = r.json()
    assert body["max_servers_applied"] == 1
    assert len({t["server"] for t in body["tools"]}) == 1
    # Unset everywhere -> unlimited (null on the wire).
    _set_principal(factory, max_servers=None)
    r = client.post("/api/v1/route", json={"query": "issue tracker tool"})
    assert r.json()["max_servers_applied"] is None


@requires_db
def test_principal_crud_exposes_max_servers(db: sessionmaker[Session]) -> None:
    admin = "admin_" + "q" * 30
    app = create_app(Settings(database_url=TEST_DB_URL), env={"MCPR_ADMIN_TOKEN": admin})
    c = TestClient(app, headers={"Authorization": f"Bearer {admin}"})
    r = c.post("/api/v1/principals", json={"agentId": "budget-agent", "maxServers": 2})
    assert r.status_code == 201, r.text
    assert r.json()["maxServers"] == 2
    pid = r.json()["id"]
    r = c.patch(f"/api/v1/principals/{pid}", json={"maxServers": 3})
    assert r.json()["maxServers"] == 3
    r = c.patch(f"/api/v1/principals/{pid}", json={"maxServers": None})
    assert r.json()["maxServers"] is None  # explicit null clears the cap
    r = c.patch(f"/api/v1/principals/{pid}", json={"maxServers": 0})
    assert r.status_code == 422
    r = c.post("/api/v1/principals", json={"agentId": "budget-agent-2"})
    assert r.json()["maxServers"] is None


# ----------------------------------------------------------------- gateway
@requires_db
async def test_gateway_exposure_honours_principal_max_servers(world: dict[str, Any]) -> None:  # noqa: F811
    gw, cat, db = world["gw"], world["cat"], world["db"]
    add_rule(db, "alice", max_operation="read")
    with db() as s:
        p = s.get(AgentPrincipal, cat.principals["alice"].id)
        assert p is not None
        p.max_servers = 1
        s.commit()
        s.refresh(p)
        s.expunge(p)
    cat.principals["alice"] = p
    names = [n for n in await _names(gw, cat, "alice") if n not in (META_TOOL, FEEDBACK_TOOL)]
    assert names == ["files.read_file"]  # top-used server only
    await gw._find_tools(p, {"query": "issues"})
    req = world["route"].requests[-1]
    assert req.max_servers == 1


# ------------------------------------------------- lifecycle gap 6 (offline)
@requires_db
async def test_gateway_exposure_excludes_offline_servers(world: dict[str, Any]) -> None:  # noqa: F811
    """Integration gap 6: discovery's eligibility rule (server not offline)
    also applies to the gateway's exposure, for routed and default lists."""
    from mcprouter.models import MCPServerRecord

    gw, cat, db, route = world["gw"], world["cat"], world["db"], world["route"]
    add_rule(db, "alice", max_operation="read")
    before = await _names(gw, cat, "alice")
    assert "files.read_file" in before
    with db() as s:
        srv = s.scalars(select(MCPServerRecord).where(MCPServerRecord.name == "files")).one()
        srv.status = "offline"
        s.commit()
    assert "files.read_file" not in await _names(gw, cat, "alice")  # default list
    world["route"].picks = ["files.read_file", "github.list_issues"]
    await gw.apply_route("alice", route(RouteRequest("q", "alice", 8)))
    assert await _names(gw, cat, "alice") == [
        "github.list_issues",
        META_TOOL,
        FEEDBACK_TOOL,
    ]  # routed list


# ------------------------------------------- analytics seam (route_request_id)
def test_accepts_kwarg_feature_detect() -> None:
    from mcprouter.gateway.server import _accepts_kwarg

    def old(principal: object, tool: object, arguments: object) -> None: ...
    def new(
        principal: object, tool: object, arguments: object, *, route_request_id: str | None = None
    ) -> None: ...
    def loose(principal: object, **kw: object) -> None: ...

    assert _accepts_kwarg(old, "route_request_id") is False
    assert _accepts_kwarg(new, "route_request_id") is True
    assert _accepts_kwarg(loose, "route_request_id") is True


@requires_db
async def test_gateway_passes_last_route_request_id_when_supported(world: dict[str, Any]) -> None:  # noqa: F811
    import mcp_types as types

    from mcprouter.execution.manager import ExecutionManager, ExecutionResult
    from mcprouter.execution.ratelimit import SlidingWindowLimiter
    from mcprouter.gateway.server import ROUTE_REQUEST_ID_KWARG, _accepts_kwarg

    from .test_gateway_mcp import _ctx

    gw, cat, db, route = world["gw"], world["cat"], world["db"], world["route"]
    add_rule(db, "alice", max_operation="read")
    seen: list[str | None] = []

    class Recording(ExecutionManager):
        async def execute(  # type: ignore[override]
            self, principal: Any, tool: Any, arguments: Any, *, route_request_id: str | None = None
        ) -> ExecutionResult:
            seen.append(route_request_id)
            return await super().execute(principal, tool, arguments)

    base = gw._manager
    rec = Recording(db, base._invoker, timeout_s=2.0, limiter=SlidingWindowLimiter(1000))
    gw._manager = rec
    gw._manager_takes_route_id = _accepts_kwarg(rec.execute, ROUTE_REQUEST_ID_KWARG)
    params = types.CallToolRequestParams(name="github.list_issues", arguments={})
    await gw._on_call_tool(_ctx(gw, cat, "alice"), params)
    assert seen == [None]  # no route yet -> no kwarg passed
    result = route(RouteRequest("q", "alice", 8))
    await gw.apply_route("alice", result)
    await gw._on_call_tool(_ctx(gw, cat, "alice"), params)
    assert seen[-1] == result.request_id
