"""Gateway hardening over the REAL streamable-HTTP transport, both protocol eras.

Every test here talks to a uvicorn-served app on an OS-assigned port through
the installed mcp 2.x client (``tests.support.gateway.live``); the wire
recorder proves what crossed HTTP."""

from __future__ import annotations

import contextlib
import dataclasses
from typing import Any

import anyio
import mcp_types as types
import pytest
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from mcprouter.gateway.server import INTERNAL_ERROR_MESSAGE, TOO_MANY_SESSIONS
from tests.support.execution import add_rule
from tests.support.gateway import ERAS, HANDSHAKE, MODERN, _client, alive, mcp_session
from tests.support.gateway import live as live  # noqa: F401 — fixture
from tests.support.gateway import world as world  # noqa: F401 — fixture
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
INIT = {
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
