"""Gateway hardening over the REAL streamable-HTTP transport, both protocol eras.

Every test here talks to a uvicorn-served app on an OS-assigned port through
the installed mcp 2.x client (``tests.support.gateway.live``); the wire
recorder proves what crossed HTTP."""

from __future__ import annotations

import contextlib
import dataclasses
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import anyio
import mcp_types as types
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from mcprouter.gateway.server import INTERNAL_ERROR_MESSAGE, TOO_MANY_SESSIONS
from tests.support.execution import add_rule
from tests.support.gateway import (
    ERAS,
    HANDSHAKE,
    MODERN,
    UNSUPPORTED_CALLS,
    _client,
    alive,
    mcp_session,
)
from tests.support.gateway import live as live  # noqa: F401 — fixture
from tests.support.gateway import world as world  # noqa: F401 — fixture
from tests.support.serve import run_app
from tests.support.wait import wait_for

from .conftest import requires_db

pytestmark = requires_db

CANARY = "password=hunter2"


# ------------------------------------------------ P-301 curated handler errors
@pytest.mark.parametrize("era", ERAS)
async def test_uncaught_handler_error_is_curated_on_the_wire(
    live: dict[str, Any], monkeypatch: pytest.MonkeyPatch, era: str
) -> None:
    """A handler exception carrying secret text never reaches the agent: the
    handshake-era SDK default would answer ``code=0, message=str(exc)``."""
    add_rule(live["db"], "alice")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(live["gw"], "visible_tools", boom)
    async with mcp_session(live["url"], "alice", era) as session:
        with pytest.raises(MCPError) as info:
            await session.list_tools()
        assert info.value.code == types.INTERNAL_ERROR
        assert info.value.message == INTERNAL_ERROR_MESSAGE
        assert "hunter2" not in repr(info.value.error)
        await alive(session, era)  # the server is not wedged
    assert (era, "tools/list", None) in live["wire"].calls
    assert b"hunter2" not in live["wire"].wire_text()


# ------------------------------------------------ P-304 per-agent session cap
INIT: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
ACCEPT = {"accept": "application/json, text/event-stream"}


def test_global_session_cap_comes_from_settings(world: dict[str, Any]) -> None:
    gw = world["gw"]
    assert gw.server.session_manager.max_sessions == gw._settings.mcp_max_sessions


async def test_per_agent_session_cap_refuses_only_that_agent(live: dict[str, Any]) -> None:
    gw, url = live["gw"], live["url"]
    gw._settings = dataclasses.replace(gw._settings, mcp_max_sessions_per_agent=2)
    owners = gw.server.session_manager._session_owners
    async with contextlib.AsyncExitStack() as stack:
        for _ in range(2):
            await stack.enter_async_context(mcp_session(url, "alice", HANDSHAKE))
        async with _client("alice") as http:
            third = await http.post(url, json=INIT, headers=ACCEPT)
        assert third.status_code == 429
        assert third.headers["retry-after"] == "60"
        assert third.json()["error"]["message"] == TOO_MANY_SESSIONS
        assert sum(o["client_id"] == "alice" for o in owners.values()) == 2  # none minted
        # Another agent is unaffected, and alice's open sessions keep working.
        async with mcp_session(url, "bob", HANDSHAKE) as bob:
            await alive(bob, HANDSHAKE)
        # The stateless modern era never mints a session: not capped.
        async with mcp_session(url, "alice", MODERN) as modern:
            await alive(modern, MODERN)
    # Closing alice's sessions (client DELETE) frees her budget.
    wait_for(lambda: not any(o["client_id"] == "alice" for o in owners.values()), timeout=10)
    async with mcp_session(url, "alice", HANDSHAKE) as again:
        await alive(again, HANDSHAKE)
    assert gw._opening == {}


async def test_concurrent_opens_never_overshoot_the_cap(live: dict[str, Any]) -> None:
    gw, url = live["gw"], live["url"]
    gw._settings = dataclasses.replace(gw._settings, mcp_max_sessions_per_agent=2)
    codes: list[int] = []
    async with _client("alice") as http:

        async def open_one() -> None:
            codes.append((await http.post(url, json=INIT, headers=ACCEPT)).status_code)

        async with anyio.create_task_group() as tg:
            for _ in range(8):
                tg.start_soon(open_one)
    owners = gw.server.session_manager._session_owners
    assert sorted(set(codes)) == [200, 429]
    assert 1 <= codes.count(200) <= 2
    assert sum(o["client_id"] == "alice" for o in owners.values()) == codes.count(200)


# ------------------------------------------------ P-305 no listing per call
async def test_modern_tools_call_runs_no_tools_list(live: dict[str, Any]) -> None:
    gw, inv = live["gw"], live["inv"]
    add_rule(live["db"], "alice", max_operation="write")
    calls: list[str] = []
    real = gw.visible_tools

    def spy(principal: Any) -> Any:
        calls.append(principal.agent_id)
        return real(principal)

    gw.visible_tools = spy
    async with mcp_session(live["url"], "alice", MODERN) as session:
        res = await session.call_tool("github.list_issues", {"repo": "a/b"})
    assert res.is_error is False
    assert inv.calls == [("github", "list_issues", {"repo": "a/b"})]
    # The client may list tools itself (output-schema lookup); every listing
    # must be one the client asked for on the wire, none server-initiated.
    wire = live["wire"].calls
    assert len(calls) == wire.count((MODERN, "tools/list", None))
    assert (MODERN, "tools/call", "github.list_issues") in wire


# ------------------------------------------------ P-306 credential-bound sessions
async def test_rotated_key_cannot_reuse_the_old_session(live: dict[str, Any]) -> None:
    from mcprouter.api.deps_auth import hash_key
    from mcprouter.models import AgentPrincipal

    url, db = live["url"], live["db"]
    add_rule(db, "alice")
    new_key = "key_alice_rotated_" + "r" * 30
    async with _client("alice") as http:
        opened = await http.post(url, json=INIT, headers=ACCEPT)
        assert opened.status_code == 200
        sid = opened.headers["mcp-session-id"]
        same = {**ACCEPT, "mcp-session-id": sid, "mcp-protocol-version": "2025-11-25"}
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        assert (await http.post(url, json=ping, headers=same)).status_code == 200  # control
    with db() as s:
        p = s.query(AgentPrincipal).filter_by(agent_id="alice").one()
        p.key_hash = hash_key(new_key)
        s.commit()
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {new_key}"}) as http:
        r = await http.post(url, json=ping, headers=same)
    assert r.status_code == 404
    async with _client("alice") as http:  # the old key is gone altogether
        assert (await http.post(url, json=ping, headers=same)).status_code == 401


@pytest.mark.parametrize("version", [*HANDSHAKE_PROTOCOL_VERSIONS, None])
async def test_cap_applies_to_every_session_opening_version(
    live: dict[str, Any], version: str | None
) -> None:
    """Review SF-3: an initialize CARRYING a handshake protocol-version header
    also mints a session, so it is capped like a header-less one."""
    gw, url = live["gw"], live["url"]
    gw._settings = dataclasses.replace(gw._settings, mcp_max_sessions_per_agent=1)
    headers = {**ACCEPT, **({"mcp-protocol-version": version} if version else {})}
    async with _client("alice") as http:
        assert (await http.post(url, json=INIT, headers=headers)).status_code == 200
        assert (await http.post(url, json=INIT, headers=headers)).status_code == 429


async def test_modern_version_never_mints_a_session(live: dict[str, Any]) -> None:
    gw, url = live["gw"], live["url"]
    gw._settings = dataclasses.replace(gw._settings, mcp_max_sessions_per_agent=1)
    for _ in range(3):
        async with mcp_session(url, "alice", MODERN) as session:
            await alive(session, MODERN)
    assert (MODERN, "server/discover", None) in live["wire"].calls
    assert not gw.server.session_manager._session_owners
    assert all(code != 429 for _m, code in live["wire"].statuses)


async def test_sessions_of_a_rotated_key_do_not_use_the_new_keys_budget(
    live: dict[str, Any],
) -> None:
    """Review SF-1: the cap is per credential; a leaked key holding the
    agent's budget cannot lock the agent out after rotation."""
    from mcprouter.api.deps_auth import hash_key
    from mcprouter.models import AgentPrincipal

    gw, url, db = live["gw"], live["url"], live["db"]
    gw._settings = dataclasses.replace(gw._settings, mcp_max_sessions_per_agent=1)
    new_key = "key_alice_rotated_" + "r" * 30
    async with _client("alice") as http:
        assert (await http.post(url, json=INIT, headers=ACCEPT)).status_code == 200
        assert (await http.post(url, json=INIT, headers=ACCEPT)).status_code == 429
    with db() as s:
        s.query(AgentPrincipal).filter_by(agent_id="alice").one().key_hash = hash_key(new_key)
        s.commit()
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {new_key}"}) as http:
        assert (await http.post(url, json=INIT, headers=ACCEPT)).status_code == 200


async def test_bearer_scheme_case_is_one_credential(live: dict[str, Any]) -> None:
    """Review N-1: `Bearer k` and `bearer k` are the same key, so the same session."""
    from tests.support.execution import KEYS

    url = live["url"]
    async with create_mcp_http_client(headers={"Authorization": f"Bearer {KEYS['alice']}"}) as h:
        opened = await h.post(url, json=INIT, headers=ACCEPT)
    sid = opened.headers["mcp-session-id"]
    ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
    same = {**ACCEPT, "mcp-session-id": sid, "mcp-protocol-version": "2025-11-25"}
    async with create_mcp_http_client(headers={"Authorization": f"bearer {KEYS['alice']}"}) as h:
        assert (await h.post(url, json=ping, headers=same)).status_code == 200


# ------------------------------------------------ P-308 unsupported methods pinned
@pytest.mark.filterwarnings("ignore::mcp.shared.exceptions.MCPDeprecationWarning")
@pytest.mark.parametrize("era", ERAS)
@pytest.mark.parametrize("method", sorted(UNSUPPORTED_CALLS))
async def test_unsupported_method_is_32601_and_server_stays_usable(
    live: dict[str, Any], era: str, method: str
) -> None:
    async with mcp_session(live["url"], "alice", era) as session:
        with pytest.raises(MCPError) as info:
            await UNSUPPORTED_CALLS[method](session)
        assert info.value.code == types.METHOD_NOT_FOUND
        await alive(session, era)
    assert (era, method, None) in live["wire"].calls


@pytest.mark.parametrize("era", ERAS)
async def test_resource_templates_list_is_empty(live: dict[str, Any], era: str) -> None:
    async with mcp_session(live["url"], "alice", era) as session:
        res = await session.list_resource_templates()
    assert res.resource_templates == []
    assert (era, "resources/templates/list", None) in live["wire"].calls


# ------------------------------------------------ P-309 transport security + body cap
MIB = 1024 * 1024


@pytest.fixture()
def lan(world: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """The world's gateway re-mounted with MCPR_ALLOWED_HOSTS=router.lan."""
    from fastapi import FastAPI

    from mcprouter.gateway.server import build_gateway, gateway_transport_security

    app = FastAPI()
    app.state.settings = world["app"].state.settings
    app.state.session_factory = world["db"]
    app.state.security = world["app"].state.security
    gw = build_gateway(
        app,
        manager=world["gw"]._manager,
        route_fn=world["route"],
        transport_security=gateway_transport_security(("router.lan",)),
    )
    port, stop = run_app(app)
    try:
        yield {**world, "gw": gw, "url": f"http://127.0.0.1:{port}/mcp", "port": port}
    finally:
        stop()


@pytest.mark.parametrize(
    ("host", "origin", "status"),
    [
        ("evil.example", None, 421),
        (None, "https://evil.example", 403),
        ("router.lan", None, 200),
        ("router.lan:8400", "http://router.lan:8400", 200),
        ("127.0.0.1:{port}", "http://localhost:{port}", 200),
    ],
)
async def test_mcp_host_and_origin_allowlist(
    lan: dict[str, Any], host: str | None, origin: str | None, status: int
) -> None:
    headers = dict(ACCEPT)
    if host:
        headers["host"] = host.format(port=lan["port"])
    if origin:
        headers["origin"] = origin.format(port=lan["port"])
    async with _client("alice") as http:
        r = await http.post(lan["url"], json=INIT, headers=headers)
    assert r.status_code == status
    owners = lan["gw"].server.session_manager._session_owners
    assert len(owners) == (1 if status == 200 else 0)


def test_sdk_body_cap_equals_the_app_cap() -> None:
    from mcprouter.api.body_limit import MAX_BODY_BYTES
    from mcprouter.gateway.server import MAX_MCP_BODY_BYTES

    assert MAX_MCP_BODY_BYTES == MAX_BODY_BYTES


def _padded_init(size: int) -> bytes:
    """An initialize request of exactly ``size`` bytes (padding in clientInfo)."""
    base = json.dumps(
        {**INIT, "params": {**INIT["params"], "clientInfo": {"name": "", "version": "0"}}}
    )
    pad = size - len(base.encode())
    return base.replace('"name": ""', '"name": "' + "x" * pad + '"').encode()


@pytest.mark.parametrize("chunked", [False, True], ids=["declared", "chunked"])
async def test_oversized_mcp_body_is_413_and_mints_no_session(
    live: dict[str, Any], chunked: bool
) -> None:
    body = _padded_init(2 * MIB)

    async def chunks() -> AsyncIterator[bytes]:
        for i in range(0, len(body), 128 * 1024):
            yield body[i : i + 128 * 1024]

    headers = {**ACCEPT, "content-type": "application/json"}
    async with _client("alice") as http:
        r = await http.post(live["url"], content=chunks() if chunked else body, headers=headers)
    assert r.status_code == 413
    assert not live["gw"].server.session_manager._session_owners


async def test_body_just_under_the_cap_is_accepted(live: dict[str, Any]) -> None:
    body = _padded_init(MIB - 1)
    assert len(body) == MIB - 1
    headers = {**ACCEPT, "content-type": "application/json"}
    async with _client("alice") as http:
        r = await http.post(live["url"], content=body, headers=headers)
    assert r.status_code == 200


# ------------------------------------------------ P-310 exposure snapshot + stream lifetime
def test_routed_skills_pair_is_always_from_one_route(world: dict[str, Any]) -> None:
    """Readers on worker threads never see route A's request id with route
    B's skills while the loop flips between the two routes."""
    import threading

    from mcprouter.interfaces import RoutedTool, RouteResult

    gw = world["gw"]
    routes = {
        rid: RouteResult(rid, [RoutedTool(s, "", "", 1.0, kind="skill")], False, 1.0, "m")
        for rid, s in (("rA", "skA"), ("rB", "skB"))
    }
    valid = {((), None), (("skA",), "rA"), (("skB",), "rB")}
    seen: set[Any] = set()
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            seen.add(gw._routed_skills("alice"))

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()

    async def flip() -> None:
        for i in range(400):
            await gw.apply_route("alice", routes["rA" if i % 2 else "rB"])

    try:
        anyio.run(flip)
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert seen <= valid, seen - valid
    assert len(seen & valid) >= 2


async def test_deleted_session_is_never_notified(live: dict[str, Any]) -> None:
    gw = live["gw"]
    add_rule(live["db"], "alice")
    async with mcp_session(live["url"], "alice", HANDSHAKE) as session:
        await session.list_tools()  # tracked for notifications
        (sid,) = list(gw._legacy["alice"])
    # The client's DELETE ran on exit; the gateway forgot the session.
    assert sid not in gw._legacy.get("alice", {})
    assert (("DELETE", 200) in live["wire"].statuses) or ("DELETE", 204) in live["wire"].statuses


async def _set_principal(db: Any, agent: str, **fields: Any) -> None:
    from mcprouter.models import AgentPrincipal

    with db() as s:
        row = s.query(AgentPrincipal).filter_by(agent_id=agent).one()
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()


async def test_disabled_principal_streams_are_closed_at_fan_out(live: dict[str, Any]) -> None:
    from mcp.client.subscriptions import listen

    from mcprouter.interfaces import RouteRequest

    gw, route, db = live["gw"], live["route"], live["db"]
    add_rule(db, "alice")
    async with (
        mcp_session(live["url"], "alice", HANDSHAKE) as legacy,
        mcp_session(live["url"], "alice", MODERN) as modern,
    ):
        await legacy.list_tools()
        (sid,) = list(gw._legacy["alice"])
        async with listen(modern, tools_list_changed=True) as sub:
            await _set_principal(db, "alice", enabled=False)
            await gw.apply_route("alice", route(RouteRequest("q", "alice", 8)))
            with anyio.fail_after(5):
                leftovers = [event async for event in sub]
        assert leftovers == []  # closed, nothing announced to a disabled agent
        assert sid not in gw.server.session_manager._server_instances
        assert "alice" not in gw._legacy


async def test_rotated_key_session_is_closed_at_fan_out(live: dict[str, Any]) -> None:
    from mcprouter.api.deps_auth import hash_key
    from mcprouter.interfaces import RouteRequest

    gw, route, db = live["gw"], live["route"], live["db"]
    add_rule(db, "alice")
    async with mcp_session(live["url"], "alice", HANDSHAKE) as legacy:
        await legacy.list_tools()
        (sid,) = list(gw._legacy["alice"])
        await _set_principal(db, "alice", key_hash=hash_key("key_alice_rotated_" + "r" * 30))
        await gw.apply_route("alice", route(RouteRequest("q", "alice", 8)))
        assert sid not in gw.server.session_manager._server_instances
        assert sid not in gw._legacy.get("alice", {})
    # bob (untouched) keeps his sessions
    async with mcp_session(live["url"], "bob", HANDSHAKE) as bob:
        await bob.list_tools()
        await gw.apply_route("bob", route(RouteRequest("q", "bob", 8)))
        assert list(gw._legacy["bob"])


ROTATED = "key_alice_rotated_" + "r" * 30


async def test_listen_streams_are_bound_to_the_credential(live: dict[str, Any]) -> None:
    """Re-review B-1: a rotated-away key's modern listen streams neither
    exhaust the new key's 16-stream budget nor get notified; they close at
    the next fan-out while the new key's stream is notified."""
    from mcp.client.subscriptions import listen

    from mcprouter.api.deps_auth import hash_key
    from mcprouter.interfaces import RouteRequest

    gw, route, db = live["gw"], live["route"], live["db"]
    add_rule(db, "alice")
    async with contextlib.AsyncExitStack() as stack:
        old = await stack.enter_async_context(mcp_session(live["url"], "alice", MODERN))
        old_subs = [
            await stack.enter_async_context(listen(old, tools_list_changed=True)) for _ in range(16)
        ]
        await _set_principal(db, "alice", key_hash=hash_key(ROTATED))
        async with (
            create_mcp_http_client(headers={"Authorization": f"Bearer {ROTATED}"}) as http,
            streamable_http_client(live["url"], http_client=http) as (r, w),
            ClientSession(r, w) as new,
        ):
            await new.discover()
            async with listen(new, tools_list_changed=True) as sub:  # not locked out
                await gw.apply_route("alice", route(RouteRequest("q", "alice", 8)))
                with anyio.fail_after(5):
                    event = await sub.__anext__()
                assert type(event).__name__ == "ToolsListChanged"
                with anyio.fail_after(5):
                    leftovers = [[e async for e in s] for s in old_subs]
        assert leftovers == [[]] * 16  # closed, never told about the change


async def test_revocation_hook_closes_streams_without_a_route_change(
    live: dict[str, Any],
) -> None:
    """Re-review SF-A: rotation followed by an UNCHANGED route, and the
    admin-side end_stale_streams hook, both close the old key's session."""
    from mcprouter.api.deps_auth import hash_key
    from mcprouter.interfaces import RouteRequest

    gw, route, db = live["gw"], live["route"], live["db"]
    add_rule(db, "alice")
    result = route(RouteRequest("q", "alice", 8))
    await gw.apply_route("alice", result)
    sessions = gw.server.session_manager._server_instances
    async with mcp_session(live["url"], "alice", HANDSHAKE) as first:
        await first.list_tools()
        (sid,) = list(gw._legacy["alice"])
        await _set_principal(db, "alice", key_hash=hash_key(ROTATED))
        assert await gw.apply_route("alice", result) is False  # unchanged set
        assert sid not in sessions
    async with mcp_session(live["url"], "bob", HANDSHAKE) as bob:
        await bob.list_tools()
        (bob_sid,) = list(gw._legacy["bob"])
        await _set_principal(db, "bob", enabled=False)
        await gw.end_stale_streams("bob")  # what an admin disable should call
        assert bob_sid not in sessions
