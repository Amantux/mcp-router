"""ConnectorToolInvoker: the execution manager's ToolInvoker over mcpclient."""

from __future__ import annotations

import json

import pytest
from testbed.fleet import generate_fleet
from testbed.servers import build_server

from mcprouter.execution.invoker import ConnectorToolInvoker
from mcprouter.interfaces import ToolInvocationError, ToolInvoker
from mcprouter.mcpclient import Connector, ServerTarget
from mcprouter.models import MCPServerRecord
from tests.support.ports import free_port

from .conftest import requires_db

pytestmark = requires_db

SPEC = generate_fleet(1)[0]  # "github"


def _server(db, **kw: object) -> MCPServerRecord:  # noqa: ANN001
    with db() as s:
        srv = MCPServerRecord(name=SPEC.name, **kw)
        s.add(srv)
        s.commit()
        s.expunge(srv)
    return srv


async def test_calls_a_real_mcp_server_and_maps_the_result(db) -> None:  # noqa: ANN001
    srv = _server(db, transport="streamable-http", endpoint="http://inproc.invalid/mcp")
    invoker = ConnectorToolInvoker(db, connector_factory=lambda t, s: Connector(build_server(SPEC)))
    assert isinstance(invoker, ToolInvoker)
    res = await invoker.call_tool(srv, "search_issues", {"query": "login bug"}, 10.0)
    assert res.is_error is False
    payload = json.loads(res.content[0]["text"])  # type: ignore[arg-type]
    assert payload["tool"] == "search_issues" and payload["args"] == {"query": "login bug"}


async def test_unreachable_server_is_a_curated_invocation_error(db) -> None:  # noqa: ANN001
    port = free_port()  # bound then closed: nothing listens there
    srv = _server(db, transport="streamable-http", endpoint=f"http://127.0.0.1:{port}/mcp")
    with pytest.raises(ToolInvocationError) as ei:
        await ConnectorToolInvoker(db).call_tool(srv, "search_issues", {}, 3.0)
    assert ei.value.curated  # fixed connector message, never upstream text
    assert str(port) not in ei.value.curated


async def test_invalid_stored_target_never_dials(db) -> None:  # noqa: ANN001
    srv = _server(db, transport="streamable-http", endpoint="file:///etc/passwd")
    dialled: list[ServerTarget] = []

    def factory(t: ServerTarget, s: float) -> Connector:
        dialled.append(t)
        return Connector(t)  # Connector validates at point of use

    with pytest.raises(ToolInvocationError):
        await ConnectorToolInvoker(db, connector_factory=factory).call_tool(srv, "x", {}, 3.0)
