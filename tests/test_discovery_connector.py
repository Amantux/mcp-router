"""Connector layer: real SDK round-trips over in-process, stdio and HTTP, plus
curated-error and point-of-use URL validation guards."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import MCPError
from mcp.server import Server
from mcp.server.mcpserver import MCPServer
from mcp.types import ListToolsResult, PaginatedRequestParams, Tool
from testbed.fleet import generate_fleet
from testbed.harness import dead_port, http_fleet, sse_server, stdio_command_for_index, stdio_env
from testbed.servers import build_server

from mcprouter.mcpclient import (
    Connector,
    ConnectorError,
    ConnectorTimeoutError,
    InvalidTargetError,
    NotConnectedError,
    ProtocolFailureError,
    ServerTarget,
    ServerUnreachableError,
    SpawnFailedError,
    validate_http_url,
)
from mcprouter.mcpclient import connector as connector_mod


# ------------------------------------------------------------ URL validation
@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.com/mcp",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://user:pw@example.com/mcp",
        "http://token@example.com/mcp",
        "http:///nohost",
        "http://example.com:99999/mcp",
        "http://example.com/mcp\r\nX-Injected: 1",
        " http://example.com/mcp",
    ],
)
def test_validate_http_url_rejects(url: str) -> None:
    with pytest.raises(InvalidTargetError):
        validate_http_url(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8600/x/mcp", "https://mcp.example.com/mcp"])
def test_validate_http_url_accepts(url: str) -> None:
    assert validate_http_url(url) == url


def test_connector_validates_url_at_point_of_use() -> None:
    """A target built WITHOUT going through the API (e.g. a DB row written by a
    seed script) is still refused before any I/O."""
    target = ServerTarget(transport="streamable-http", endpoint="http://admin:pw@10.0.0.1/mcp")
    with pytest.raises(InvalidTargetError) as ei:
        Connector(target)
    assert "pw" not in ei.value.message


def test_stdio_target_requires_command() -> None:
    with pytest.raises(InvalidTargetError):
        Connector(ServerTarget(transport="stdio", command=()))


def test_target_repr_hides_env() -> None:
    t = ServerTarget(transport="stdio", command=("x",), env={"API_KEY": "sk-live-SECRET"})
    assert "sk-live-SECRET" not in repr(t)


# ------------------------------------------------------------- round trips
async def test_inprocess_list_and_call() -> None:
    spec = generate_fleet(3)[0]  # github
    async with Connector(build_server(spec)) as c:
        info = await c.initialize()
        assert info.name == "github"
        assert info.version == "1.0.0"
        tools = await c.list_tools()
        assert [t.name for t in tools] == [t.name for t in spec.tools]
        search = next(t for t in tools if t.name == "search_issues")
        assert search.input_schema == spec.tool("search_issues").input_schema()
        assert search.annotations == {"readOnlyHint": True, "destructiveHint": False}
        res = await c.call_tool("search_issues", {"query": "bug"})
        assert not res.is_error
        assert '"ok": true' in res.content[0]["text"]


async def test_stdio_round_trip() -> None:
    cmd = stdio_command_for_index(20, 8)  # "files"
    target = ServerTarget(transport="stdio", command=tuple(cmd), env=stdio_env())
    async with Connector(target, connect_timeout_s=30) as c:
        info = await c.initialize()
        assert info.name == "files"
        names = {t.name for t in await c.list_tools()}
        assert {"read_file", "write_file"} <= names


async def test_streamable_http_round_trip() -> None:
    with http_fleet(2) as urls:
        target = ServerTarget(transport="streamable-http", endpoint=urls["jenkins"])
        async with Connector(target) as c:
            assert (await c.initialize()).name == "jenkins"
            assert "trigger_build" in {t.name for t in await c.list_tools()}
            res = await c.call_tool("list_runners", {})
            assert not res.is_error


async def test_legacy_sse_round_trip() -> None:
    with sse_server(generate_fleet(2)[1]) as url:
        async with Connector(
            ServerTarget(transport="sse", endpoint=url), connect_timeout_s=10
        ) as c:
            assert (await c.initialize()).name == "jenkins"
            assert len(await c.list_tools()) == 10


# ------------------------------------------------------------ curated errors
async def test_unreachable_http_is_curated() -> None:
    target = ServerTarget(
        transport="streamable-http", endpoint=f"http://127.0.0.1:{dead_port()}/mcp"
    )
    with pytest.raises(ServerUnreachableError) as ei:
        await Connector(target, connect_timeout_s=5).connect()
    assert ei.value.message == "server is unreachable"


async def test_missing_stdio_command_is_curated() -> None:
    target = ServerTarget(transport="stdio", command=("definitely-not-a-real-binary-xyz",))
    with pytest.raises(SpawnFailedError) as ei:
        await Connector(target).connect()
    assert "definitely-not" not in ei.value.message


async def test_stdio_stderr_never_leaks(tmp_path: Path) -> None:
    """A crashing server that prints a secret to stderr: the error we raise is
    a fixed curated message with no trace of the stderr text."""
    script = tmp_path / "crash.py"
    script.write_text("import sys\nsys.stderr.write('DB_PASSWORD=hunter2\\n')\nsys.exit(3)\n")
    target = ServerTarget(transport="stdio", command=(sys.executable, str(script)))
    with pytest.raises(ConnectorError) as ei:
        await Connector(target, connect_timeout_s=10).connect()
    assert "hunter2" not in ei.value.message
    assert "hunter2" not in str(ei.value)
    assert ei.value.kind in {"unreachable", "protocol"}


async def test_stdio_stderr_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Subprocess stderr goes to devnull, not to our process's stderr (where
    log shippers would collect it). The SDK default is ``sys.stderr``."""
    seen: list[object] = []
    real = connector_mod.stdio_client

    def spy(params: Any, errlog: Any = sys.stderr) -> Any:
        seen.append(errlog)
        return real(params, errlog=errlog)

    monkeypatch.setattr(connector_mod, "stdio_client", spy)
    target = ServerTarget(
        transport="stdio", command=tuple(stdio_command_for_index(1, 0)), env=stdio_env()
    )
    async with Connector(target, connect_timeout_s=30):
        pass
    assert len(seen) == 1
    assert getattr(seen[0], "name", None) == os.devnull


async def test_upstream_mcp_error_text_is_not_echoed() -> None:
    """A server's JSON-RPC error message is upstream text (it can echo a DSN);
    only the numeric code survives curation."""
    srv = MCPServer("leaky", version="1")

    @srv.tool(name="boom", description="fails")
    def boom() -> str:
        raise MCPError(-32603, "connect failed: postgres://app:S3CRET@db:5432/prod")

    async with Connector(srv) as c:
        with pytest.raises(ProtocolFailureError) as ei:
            await c.call_tool("boom", {})
    assert "S3CRET" not in ei.value.message
    assert ei.value.message == "server returned an MCP error (code -32603)"


async def test_hung_server_times_out(tmp_path: Path) -> None:
    script = tmp_path / "hang.py"
    script.write_text("import time\ntime.sleep(60)\n")
    target = ServerTarget(transport="stdio", command=(sys.executable, str(script)))
    with pytest.raises(ConnectorTimeoutError):
        await Connector(target, connect_timeout_s=1).connect()


async def test_calls_before_connect_are_refused() -> None:
    c = Connector(build_server(generate_fleet(1)[0]))
    with pytest.raises(NotConnectedError):
        await c.list_tools()


def _paging_server(next_cursor: Callable[[str | None], str | None]) -> Server[Any]:
    """Low-level server whose tools/list pagination we control."""

    async def on_list_tools(ctx: Any, params: PaginatedRequestParams | None) -> ListToolsResult:
        cur = params.cursor if params else None
        tool = Tool(name=f"t_{cur or 'start'}", input_schema={"type": "object"})
        return ListToolsResult(tools=[tool], next_cursor=next_cursor(cur))

    return Server("pager", on_list_tools=on_list_tools)


async def test_pagination_cycle_fails_instead_of_partial_listing() -> None:
    srv = _paging_server(lambda cur: {"A": "B", "B": "A"}.get(cur or "", "A"))
    async with Connector(srv) as c:
        with pytest.raises(ProtocolFailureError, match="pagination cursor"):
            await c.list_tools()


async def test_endless_pagination_fails_instead_of_partial_listing() -> None:
    srv = _paging_server(lambda cur: str(int(cur or "0") + 1))
    async with Connector(srv) as c:
        with pytest.raises(ProtocolFailureError, match="without end"):
            await c.list_tools()


async def test_finite_pagination_collects_every_page() -> None:
    srv = _paging_server(lambda cur: None if cur == "3" else str(int(cur or "0") + 1))
    async with Connector(srv) as c:
        assert [t.name for t in await c.list_tools()] == ["t_start", "t_1", "t_2", "t_3"]


async def test_outer_cancel_during_connect_leaves_scopes_intact(tmp_path: Path) -> None:
    script = tmp_path / "hang.py"
    script.write_text("import time\ntime.sleep(60)\n")
    c = Connector(
        ServerTarget(transport="stdio", command=(sys.executable, str(script))),
        connect_timeout_s=30,
    )
    with anyio.move_on_after(0.5) as scope:
        await c.connect()
    assert scope.cancelled_caught
    # The scope stack must be usable afterwards (a corrupted stack raises here).
    with anyio.move_on_after(0.1):
        await anyio.sleep(1)
    async with Connector(build_server(generate_fleet(1)[0])) as ok:
        assert (await ok.initialize()).name == "github"
