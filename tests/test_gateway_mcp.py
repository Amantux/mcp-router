"""FR-06 gateway: per-agent dynamic exposure over a real MCP endpoint.

Handler-level tests drive the low-level Server handlers with a real
ServerRequestContext; end-to-end tests run uvicorn on 127.0.0.1:8641 and talk
to it with the installed mcp 2.x client (both protocol eras)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import anyio
import mcp_types as types
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.subscriptions import listen
from mcp.server.context import ServerRequestContext
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError
from sqlalchemy import select

from mcprouter.gateway.server import (
    FEEDBACK_TOOL,
    META_TOOL,
)
from mcprouter.interfaces import RouteRequest
from mcprouter.models import ExecutionRecord, MCPServerRecord, MCPToolRecord
from tests.support.execution import (
    add_rule,
)
from tests.support.gateway import (
    _client,
    _ctx,
    _names,
)
from tests.support.gateway import world as world  # noqa: F401 — fixture

from .conftest import requires_db

pytestmark = requires_db

PORT = 8641
URL = f"http://127.0.0.1:{PORT}/mcp"


# ----------------------------------------------------------- list_tools
async def test_default_exposure_is_top_used_authorized_tools(world: dict[str, Any]) -> None:
    gw, cat, db = world["gw"], world["cat"], world["db"]
    add_rule(db, "alice", max_operation="read")
    assert await _names(gw, cat, "alice") == [
        "files.read_file",
        "github.search_code",
        "github.list_issues",
        META_TOOL,
        FEEDBACK_TOOL,
    ]


async def test_no_rules_means_only_meta_tool(world: dict[str, Any]) -> None:
    assert await _names(world["gw"], world["cat"], "alice") == [META_TOOL, FEEDBACK_TOOL]


async def test_exposure_is_capped(world: dict[str, Any]) -> None:
    gw, cat, db = world["gw"], world["cat"], world["db"]
    add_rule(db, "alice", max_operation="execute")
    with db() as s:
        p = s.get(type(cat.principals["alice"]), cat.principals["alice"].id)
        assert p is not None
        p.max_tools = 2
        s.commit()
        s.refresh(p)
        s.expunge(p)
    cat.principals["alice"] = p
    assert (
        len(await _names(gw, cat, "alice")) == 2 + 2
    )  # + router.find_tools, router.feedback (outside the cap)  # + meta tool


async def test_routed_exposure_is_refiltered_through_policy(world: dict[str, Any]) -> None:
    """Defense in depth: the route returned an execute tool alice may not use;
    it must not appear even though routing 'selected' it."""
    gw, cat, db, route = world["gw"], world["cat"], world["db"], world["route"]
    add_rule(db, "alice", max_operation="write")
    await gw.apply_route("alice", route(RouteRequest("q", "alice", 8)))
    assert await _names(gw, cat, "alice") == [
        "github.list_issues",
        "github.create_issue",
        META_TOOL,
        FEEDBACK_TOOL,
    ]


async def test_exposure_is_per_agent(world: dict[str, Any]) -> None:
    gw, cat, db, route = world["gw"], world["cat"], world["db"], world["route"]
    add_rule(db, "alice", max_operation="execute")
    add_rule(db, "bob", max_operation="read")
    await gw.apply_route("alice", route(RouteRequest("q", "alice", 8)))
    bob = await _names(gw, cat, "bob")
    assert bob == [
        "files.read_file",
        "github.search_code",
        "github.list_issues",
        META_TOOL,
        FEEDBACK_TOOL,
    ]


async def test_disabled_tool_not_listed(world: dict[str, Any]) -> None:
    gw, cat, db = world["gw"], world["cat"], world["db"]
    add_rule(db, "alice")
    with db() as s:
        t = s.get(MCPToolRecord, cat.tools["files.read_file"].id)
        assert t is not None
        t.enabled = False
        s.commit()
    assert "files.read_file" not in await _names(gw, cat, "alice")


async def test_approval_tools_are_annotated(world: dict[str, Any]) -> None:
    gw, cat, db = world["gw"], world["cat"], world["db"]
    add_rule(db, "alice", tool_name="read_file", requires_approval=True)
    res = await gw._on_list_tools(_ctx(gw, cat, "alice"), None)
    tool = next(t for t in res.tools if t.name == "files.read_file")
    assert "approval" in (tool.description or "")


async def test_list_result_is_private_and_uncached(world: dict[str, Any]) -> None:
    gw, cat = world["gw"], world["cat"]
    res = await gw._on_list_tools(_ctx(gw, cat, "alice"), None)
    assert res.cache_scope == "private" and res.ttl_ms == 0


async def test_unauthenticated_context_fails_closed(world: dict[str, Any]) -> None:
    gw = world["gw"]
    ctx = ServerRequestContext(
        session=None,  # type: ignore[arg-type]
        lifespan_context={},
        protocol_version="2025-11-25",
        method="tools/list",
        request=None,
    )
    with pytest.raises(MCPError):
        await gw._on_list_tools(ctx, None)


# ------------------------------------------------------------ call_tool
async def test_call_dispatches_through_execution_manager(world: dict[str, Any]) -> None:
    gw, cat, db, inv = world["gw"], world["cat"], world["db"], world["inv"]
    add_rule(db, "alice")
    res = await gw._on_call_tool(
        _ctx(gw, cat, "alice"),
        types.CallToolRequestParams(name="github.list_issues", arguments={"repo": "a/b"}),
    )
    assert res.is_error is False
    assert inv.calls == [("github", "list_issues", {"repo": "a/b"})]
    with db() as s:
        assert [r.outcome for r in s.scalars(select(ExecutionRecord)).all()] == ["ok"]


async def test_routed_but_unauthorized_tool_cannot_be_called(world: dict[str, Any]) -> None:
    gw, cat, db, inv, route = world["gw"], world["cat"], world["db"], world["inv"], world["route"]
    add_rule(db, "alice", max_operation="read")
    await gw.apply_route("alice", route(RouteRequest("q", "alice", 8)))
    res = await gw._on_call_tool(
        _ctx(gw, cat, "alice"),
        types.CallToolRequestParams(name="shell.run_command", arguments={"repo": "x"}),
    )
    assert res.is_error is True
    assert inv.calls == []


async def test_unknown_tool_is_refused_and_audited(world: dict[str, Any]) -> None:
    gw, cat, db, inv = world["gw"], world["cat"], world["db"], world["inv"]
    add_rule(db, "alice", max_operation="execute")
    res = await gw._on_call_tool(
        _ctx(gw, cat, "alice"), types.CallToolRequestParams(name="nope.nothing", arguments={})
    )
    assert res.is_error is True and inv.calls == []
    with db() as s:
        assert [r.outcome for r in s.scalars(select(ExecutionRecord)).all()] == ["denied"]


async def test_ambiguous_dotted_names_are_refused(world: dict[str, Any]) -> None:
    """'a.b'+'c' and 'a'+'b.c' both render as 'a.b.c': refuse rather than guess."""
    gw, cat, db, inv = world["gw"], world["cat"], world["db"], world["inv"]
    with db() as s:
        for server, tool in (("a.b", "c"), ("a", "b.c")):
            srv = MCPServerRecord(name=server, transport="stdio")
            s.add(srv)
            s.flush()
            s.add(MCPToolRecord(server_id=srv.id, name=tool, schema_hash="h", operation="read"))
        s.commit()
    add_rule(db, "alice", max_operation="execute")
    res = await gw._on_call_tool(
        _ctx(gw, cat, "alice"), types.CallToolRequestParams(name="a.b.c", arguments={})
    )
    assert res.is_error is True and inv.calls == []


async def test_pending_approval_is_reported_not_executed(world: dict[str, Any]) -> None:
    gw, cat, db, inv = world["gw"], world["cat"], world["db"], world["inv"]
    add_rule(db, "alice", requires_approval=True)
    res = await gw._on_call_tool(
        _ctx(gw, cat, "alice"),
        types.CallToolRequestParams(name="github.list_issues", arguments={"repo": "a/b"}),
    )
    assert res.is_error is True and "Approval required" in res.content[0].text  # type: ignore[union-attr]
    assert inv.calls == []


# ------------------------------------------------------------ find_tools
async def test_find_tools_redacts_query_and_updates_exposure(world: dict[str, Any]) -> None:
    gw, cat, db, route = world["gw"], world["cat"], world["db"], world["route"]
    add_rule(db, "alice", max_operation="write")
    secret = "ghp_" + "Q" * 36
    res = await gw._on_call_tool(
        _ctx(gw, cat, "alice"),
        types.CallToolRequestParams(name=META_TOOL, arguments={"query": f"use {secret} to file"}),
    )
    assert res.is_error is False
    assert secret not in route.requests[-1].query
    assert route.requests[-1].agent_id == "alice"
    text = res.content[0].text  # type: ignore[union-attr]
    assert "github.create_issue" in text and "shell.run_command" not in text


async def test_find_tools_router_failure_is_curated(world: dict[str, Any]) -> None:
    gw, cat, route = world["gw"], world["cat"], world["route"]
    route.fail = True
    res = await gw._on_call_tool(
        _ctx(gw, cat, "alice"),
        types.CallToolRequestParams(name=META_TOOL, arguments={"query": "x"}),
    )
    assert res.is_error is True
    assert "leakme" not in res.content[0].text  # type: ignore[union-attr]
    assert gw.exposure.get("alice") is None


@pytest.mark.parametrize("args", [{}, {"query": ""}, {"query": 3}, {"query": "x", "extra": 1}])
async def test_find_tools_validates_arguments(world: dict[str, Any], args: dict[str, Any]) -> None:
    gw, cat, route = world["gw"], world["cat"], world["route"]
    res = await gw._on_call_tool(
        _ctx(gw, cat, "alice"), types.CallToolRequestParams(name=META_TOOL, arguments=args)
    )
    assert res.is_error is True and route.requests == []


async def test_list_changed_bus_is_per_agent(world: dict[str, Any]) -> None:
    gw, route = world["gw"], world["route"]
    seen: dict[str, int] = {"alice": 0, "bob": 0}
    for agent in seen:
        bus, _ = gw._bus(agent)
        bus.subscribe(lambda _e, a=agent: seen.__setitem__(a, seen[a] + 1))
    await gw.apply_route("alice", route(RouteRequest("q", "alice", 8)))
    assert seen == {"alice": 1, "bob": 0}
    # Same selection again: no change, no notification.
    await gw.apply_route("alice", route(RouteRequest("q", "alice", 8)))
    assert seen == {"alice": 1, "bob": 0}


# ------------------------------------------------------------ end-to-end
@pytest.fixture()
def served(world: dict[str, Any]) -> Iterator[dict[str, Any]]:
    config = uvicorn.Config(world["app"], host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    yield world
    server.should_exit = True
    t.join(10)


async def test_e2e_requires_authentication(served: dict[str, Any]) -> None:
    async with _client(None) as http:
        r = await http.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401
    async with create_mcp_http_client(headers={"Authorization": "Bearer wrong"}) as http:
        r = await http.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401


async def test_e2e_handshake_list_call_and_list_changed(served: dict[str, Any]) -> None:
    db, inv = served["db"], served["inv"]
    add_rule(db, "alice", max_operation="write")
    changed = anyio.Event()

    async def on_message(msg: Any) -> None:
        if isinstance(msg, types.ToolListChangedNotification):
            changed.set()

    async with (
        _client("alice") as http,
        streamable_http_client(URL, http_client=http) as (r, w),
        ClientSession(r, w, message_handler=on_message) as session,
    ):
        init = await session.initialize()
        assert init.capabilities.tools is not None and init.capabilities.tools.list_changed
        names = [t.name for t in (await session.list_tools()).tools]
        assert names == [
            "files.read_file",
            "github.search_code",
            "github.list_issues",
            "github.create_issue",
            META_TOOL,
            FEEDBACK_TOOL,
        ]
        res = await session.call_tool("github.list_issues", {"repo": "a/b"})
        assert res.is_error is False and inv.calls == [("github", "list_issues", {"repo": "a/b"})]
        denied = await session.call_tool("shell.run_command", {"repo": "a/b"})
        assert denied.is_error is True and len(inv.calls) == 1
        await anyio.sleep(0.3)  # let the standalone GET stream attach
        await session.call_tool(META_TOOL, {"query": "file an issue"})
        with anyio.fail_after(5):
            await changed.wait()
        names = [t.name for t in (await session.list_tools()).tools]
        assert names == ["github.list_issues", "github.create_issue", META_TOOL, FEEDBACK_TOOL]


async def test_e2e_session_is_bound_to_its_creator(served: dict[str, Any]) -> None:
    """Agent B presenting agent A's Mcp-Session-Id gets 404, not A's session."""
    add_rule(served["db"], "alice")
    async with (
        _client("alice") as http,
        streamable_http_client(URL, http_client=http) as (r, w),
        ClientSession(r, w) as session,
    ):
        await session.initialize()
        await session.list_tools()
        sid = next(iter(served["gw"]._legacy["alice"]))
        async with _client("bob") as bob:
            resp = await bob.post(
                URL,
                json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
                headers={
                    "mcp-session-id": sid,
                    "mcp-protocol-version": "2025-11-25",
                    "accept": "application/json, text/event-stream",
                },
            )
            assert resp.status_code == 404


async def test_e2e_modern_listen_receives_only_own_changes(served: dict[str, Any]) -> None:
    gw, route = served["gw"], served["route"]
    add_rule(served["db"], "alice", max_operation="write")
    async with (
        _client("alice") as http,
        streamable_http_client(URL, http_client=http) as (r, w),
        ClientSession(r, w) as session,
    ):
        await session.discover()
        names = [t.name for t in (await session.list_tools()).tools]
        assert META_TOOL in names
        async with listen(session, tools_list_changed=True) as sub:
            await gw.apply_route("bob", route(RouteRequest("q", "bob", 8)))
            leaked: list[Any] = []
            with anyio.move_on_after(0.7):
                leaked.append(await sub.__anext__())
            assert leaked == [], "bob's re-route was announced on alice's stream"
            await gw.apply_route("alice", route(RouteRequest("q", "alice", 8)))
            with anyio.fail_after(5):
                event = await sub.__anext__()
            assert type(event).__name__ == "ToolsListChanged"
        names = [t.name for t in (await session.list_tools()).tools]
        assert names == ["github.list_issues", "github.create_issue", META_TOOL, FEEDBACK_TOOL]


# ------------------------------------------------- adversarial-pass findings
async def test_catalog_tool_cannot_shadow_meta_tool(world: dict[str, Any]) -> None:
    """A discovered server named 'router' with a 'find_tools' tool must not
    produce a duplicate (uncallable) tool name next to the meta tool."""
    gw, cat, db = world["gw"], world["cat"], world["db"]
    with db() as s:
        srv = MCPServerRecord(name="router", transport="stdio")
        s.add(srv)
        s.flush()
        s.add(
            MCPToolRecord(
                server_id=srv.id,
                name="find_tools",
                schema_hash="h",
                operation="read",
                call_count=999,
            )
        )
        s.commit()
    add_rule(db, "alice")
    names = await _names(gw, cat, "alice")
    assert names.count(META_TOOL) == 1


async def test_non_http_scope_is_rejected(world: dict[str, Any]) -> None:
    from mcprouter.gateway.server import _AuthASGI

    reached: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        reached.append(scope["type"])

    sent: list[Any] = []

    async def send(msg: Any) -> None:
        sent.append(msg)

    async def receive() -> Any:
        return {"type": "websocket.connect"}

    app = _AuthASGI(inner, world["gw"])
    await app({"type": "websocket", "headers": [], "path": "/mcp"}, receive, send)
    assert reached == []


async def test_stalled_sessions_are_notified_concurrently(world: dict[str, Any]) -> None:
    """Review SF-2: N stalled legacy sessions must cost ~one notify timeout,
    not N of them (a /route caller blocks on this)."""
    from collections import OrderedDict

    from mcprouter.gateway import server as gw_mod

    class Stalled:
        async def send_tool_list_changed(self) -> None:
            await anyio.sleep(60)

    gw = world["gw"]
    gw._legacy["alice"] = OrderedDict((f"s{i}", Stalled()) for i in range(5))
    t0 = time.perf_counter()
    await gw.notify_tools_changed("alice")
    assert time.perf_counter() - t0 < gw_mod.NOTIFY_TIMEOUT_S * 2
    assert not gw._legacy["alice"]  # all pruned as dead


async def test_notify_failure_prunes_session_and_never_fails_find_tools(
    world: dict[str, Any],
) -> None:
    """P-303: a session whose send raises a non-Broken* exception is pruned,
    the other sessions are still notified, and router.find_tools succeeds."""
    from collections import OrderedDict

    sent: list[str] = []

    class Raising:
        async def send_tool_list_changed(self) -> None:
            raise RuntimeError("no back channel")

    class Healthy:
        async def send_tool_list_changed(self) -> None:
            sent.append("healthy")

    gw, cat, db = world["gw"], world["cat"], world["db"]
    add_rule(db, "alice", max_operation="write")
    gw._legacy["alice"] = OrderedDict([("bad", Raising()), ("good", Healthy())])
    res = await gw._on_call_tool(
        _ctx(gw, cat, "alice"),
        types.CallToolRequestParams(name=META_TOOL, arguments={"query": "file an issue"}),
    )
    assert res.is_error is False
    assert sent == ["healthy"]
    assert list(gw._legacy["alice"]) == ["good"]
    assert gw.exposure.get("alice") is not None


# ------------------------------------------------ P-307 reserved names + meta args
def _variant(world: dict[str, Any], *, routed: bool, skills: Any) -> Any:
    from mcprouter.gateway.server import GatewayServer

    app = world["app"]
    return GatewayServer(
        session_factory=world["db"],
        security=app.state.security,
        settings=app.state.settings,
        manager=world["gw"]._manager,
        route_fn=world["route"] if routed else None,
        skills=skills,
    )


class _NoSkills:
    """Skills stand-in: any use is a side effect the test forbids."""

    def __getattr__(self, attr: str) -> Any:
        raise AssertionError(f"skills touched: {attr}")


@pytest.mark.parametrize("routed", [True, False])
@pytest.mark.parametrize("with_skills", [True, False])
async def test_reserved_catalog_names_are_never_listed(
    world: dict[str, Any], routed: bool, with_skills: bool
) -> None:
    from mcprouter.gateway.server import RESERVED_TOOL_NAMES

    db, cat = world["db"], world["cat"]
    with db() as s:
        srv = MCPServerRecord(name="router", transport="stdio")
        s.add(srv)
        s.flush()
        for name in sorted(t.split(".", 1)[1] for t in RESERVED_TOOL_NAMES):
            s.add(
                MCPToolRecord(
                    server_id=srv.id, name=name, schema_hash="h", operation="read", call_count=999
                )
            )
        s.commit()
    add_rule(db, "alice")
    gw = _variant(world, routed=routed, skills=_NoSkills() if with_skills else None)
    res = await gw._on_list_tools(_ctx(gw, cat, "alice"), None)
    names = [t.name for t in res.tools]
    assert len(names) == len(set(names)), names
    assert not any(
        t.name.startswith("router.") for t, _s, _a in gw.visible_tools(cat.principals["alice"])
    )
    expected_meta = ({META_TOOL, FEEDBACK_TOOL} if routed else set()) | (
        {"router.activate_skill", "router.read_skill_resource"} if with_skills else set()
    )
    assert {n for n in names if n.startswith("router.")} == expected_meta


BAD_META_ARGS = [
    (META_TOOL, {"query": "x" * 2001}),
    (META_TOOL, {"query": ""}),
    (FEEDBACK_TOOL, {"items": []}),
    (FEEDBACK_TOOL, {"items": [{"name": "x", "helpful": True, "kind": "bogus"}]}),
    (FEEDBACK_TOOL, {"items": [{"name": "x" * 301, "helpful": True}]}),
    (FEEDBACK_TOOL, {"items": [{"name": "x", "helpful": True}] * 51}),
    (FEEDBACK_TOOL, {"requestId": "r" * 37, "items": [{"name": "x", "helpful": True}]}),
    (FEEDBACK_TOOL, {"items": [{"name": "x", "helpful": True, "extra": 1}]}),
    ("router.activate_skill", {"name": ""}),
    ("router.activate_skill", {"name": "a/b", "path": "x"}),
    ("router.read_skill_resource", {"name": "a/b"}),
    ("router.read_skill_resource", {"name": "a/b", "path": "p" * 1025}),
]


@pytest.mark.parametrize(("tool", "args"), BAD_META_ARGS, ids=lambda v: str(v)[:24])
async def test_schema_violating_meta_args_are_refused_without_side_effect(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tool: str, args: dict[str, Any]
) -> None:
    from mcprouter.analytics import feedback as fb

    def no_feedback(*_a: Any, **_k: Any) -> int:
        raise AssertionError("feedback recorded")

    monkeypatch.setattr(fb, "record_feedback", no_feedback)
    gw = _variant(world, routed=True, skills=_NoSkills())
    res = await gw._on_call_tool(
        _ctx(gw, world["cat"], "alice"), types.CallToolRequestParams(name=tool, arguments=args)
    )
    assert res.is_error is True
    assert res.content[0].text.startswith("Refused: invalid arguments")  # type: ignore[union-attr]
    assert world["route"].requests == []
    assert "x" * 50 not in res.content[0].text  # type: ignore[union-attr]  # values never echoed
