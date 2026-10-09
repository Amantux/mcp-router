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
from mcp.shared.exceptions import MCPError

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
    assert sorted(set(codes)) in ([200, 429], [429])
    assert codes.count(200) <= 2
    assert sum(o["client_id"] == "alice" for o in owners.values()) == codes.count(200)
