"""Gateway hardening over the REAL streamable-HTTP transport, both protocol eras.

Every test here talks to a uvicorn-served app on an OS-assigned port through
the installed mcp 2.x client (``tests.support.gateway.live``); the wire
recorder proves what crossed HTTP."""

from __future__ import annotations

from typing import Any

import mcp_types as types
import pytest
from mcp.shared.exceptions import MCPError

from mcprouter.gateway.server import INTERNAL_ERROR_MESSAGE
from tests.support.execution import add_rule
from tests.support.gateway import ERAS, alive, mcp_session
from tests.support.gateway import live as live  # noqa: F401 — fixture
from tests.support.gateway import world as world  # noqa: F401 — fixture

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
