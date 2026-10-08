"""Thin connector over the official MCP Python SDK (mcp 2.x).

SDK facts this module relies on (verified against the installed mcp 2.3.0):

* ``mcp.Client`` fuses transport setup and the protocol handshake in
  ``__aenter__``; ``mode="auto"`` probes ``server/discover`` (2026-07-28) and
  falls back to the legacy ``initialize`` handshake. So ``connect()`` here
  opens the transport AND negotiates; ``initialize()`` is the idempotent
  accessor for the negotiated server info.
* ``ping`` is deprecated on 2026-era connections, so liveness checks use
  ``tools/list`` instead.
* Transport failures surface as (nested) ``ExceptionGroup``s from anyio task
  groups; they are flattened and curated by ``_curate``.

anyio rule: ``connect()`` and ``close()`` must run in the same task (the
SDK's cancel scopes are task-bound). ``async with Connector(...)`` does that.
"""

from __future__ import annotations

import logging
import os
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from typing import IO, Any, Self

import anyio
import anyio.abc
import httpx2
from mcp import Client, MCPError
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.mcpserver import MCPServer
from pydantic import ValidationError

from mcprouter.mcpclient.errors import (
    ConnectorError,
    ConnectorTimeoutError,
    NotConnectedError,
    ProtocolFailureError,
    ServerUnreachableError,
    SpawnFailedError,
)
from mcprouter.mcpclient.targets import ServerTarget

log = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_S = 15.0
DEFAULT_REQUEST_TIMEOUT_S = 30.0
MAX_LIST_PAGES = 100
CLOSE_TIMEOUT_S = 5.0  # a paginator that never ends must not hang discovery

# MCP JSON-RPC codes we map to specific curated messages.
_CONNECTION_CLOSED = -32000
_METHOD_NOT_FOUND = -32601


@dataclass(frozen=True)
class ServerInfo:
    name: str | None
    version: str | None
    protocol_version: str


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    description: str
    input_schema: dict[str, Any]
    title: str | None = None
    annotations: dict[str, Any] | None = None  # camelCase wire form, e.g. readOnlyHint


@dataclass(frozen=True)
class ToolCallResult:
    """Tool output. ``content`` is the tool's own output (data for the caller);
    ``is_error`` marks a tool-level failure reported in-band by the server."""

    is_error: bool
    content: list[dict[str, Any]]
    structured: Any = None


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        out: list[BaseException] = []
        for e in exc.exceptions:
            out.extend(_leaves(e))
        return out
    return [exc]


def _curate(exc: BaseException, *, phase: str, transport: str) -> ConnectorError:
    """Map any SDK/transport failure to a curated ConnectorError.

    Messages are fixed strings plus, at most, a numeric code. Never str(exc).
    """
    if isinstance(exc, ConnectorError):
        return exc
    leaves = _leaves(exc)
    # Prefer the most specific leaf: our own errors, then timeouts, then the rest.
    for leaf in leaves:
        if isinstance(leaf, ConnectorError):
            return leaf
    for leaf in leaves:
        if isinstance(leaf, TimeoutError):
            return ConnectorTimeoutError(f"server did not respond in time ({phase})")
    leaf = leaves[0]
    if transport == "stdio" and phase == "connect" and isinstance(leaf, OSError | ValueError):
        return SpawnFailedError("server command could not be started")
    if isinstance(leaf, httpx2.HTTPStatusError):
        return ServerUnreachableError(f"server returned HTTP {leaf.response.status_code}")
    if isinstance(leaf, httpx2.TransportError | OSError):
        return ServerUnreachableError("server is unreachable")
    if isinstance(leaf, MCPError):
        if leaf.code == _CONNECTION_CLOSED:
            return ServerUnreachableError("server closed the connection")
        if leaf.code == _METHOD_NOT_FOUND:
            return ProtocolFailureError(f"server does not support this request ({phase})")
        return ProtocolFailureError(f"server returned an MCP error (code {leaf.code})")
    if isinstance(leaf, ValidationError):
        return ProtocolFailureError(f"server sent a malformed response ({phase})")
    if isinstance(leaf, RuntimeError):
        return ProtocolFailureError("protocol negotiation failed")
    return ProtocolFailureError(f"unexpected connector failure ({phase})")


class Connector:
    """One connection to one MCP server.

    Use as ``async with Connector(target) as c: await c.list_tools()``.
    """

    def __init__(
        self,
        target: ServerTarget | MCPServer | Server[Any],
        *,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        # Validate at point of use — before any I/O, whatever path built the target.
        self._target = target.validated() if isinstance(target, ServerTarget) else target
        self._transport = target.transport if isinstance(target, ServerTarget) else "inproc"
        self._connect_timeout_s = connect_timeout_s
        self._request_timeout_s = request_timeout_s
        self._client: Client | None = None
        self._stack: AsyncExitStack | None = None
        self._tg: anyio.abc.TaskGroup | None = None
        self._stop: anyio.Event | None = None
        self._info: ServerInfo | None = None
        self._errlog: IO[str] | None = None

    # ------------------------------------------------------------ lifecycle
    def _transport_cm(self) -> AbstractAsyncContextManager[Any] | MCPServer | Server[Any]:
        t = self._target
        if isinstance(t, MCPServer | Server):
            return t
        if t.transport == "stdio":
            assert t.command is not None  # guaranteed by validated()
            # Subprocess stderr is untrusted and may contain secrets: discard it
            # rather than forwarding it anywhere near an API response or log.
            self._errlog = open(os.devnull, "w")  # noqa: SIM115 — closed in close()
            params = StdioServerParameters(
                command=t.command[0], args=list(t.command[1:]), env=dict(t.env) or None, cwd=t.cwd
            )
            return stdio_client(params, errlog=self._errlog)
        assert t.endpoint is not None
        if t.transport == "sse":
            return sse_client(t.endpoint, timeout=self._connect_timeout_s)
        return streamable_http_client(t.endpoint)

    async def connect(self) -> ServerInfo:
        """Open the transport and negotiate (the SDK fuses both).

        The SDK client opens long-lived task groups on entry, so it must not be
        entered inside a timeout scope that exits first. It therefore lives in a
        dedicated runner task; the connect timeout only bounds the wait for it
        to become ready.
        """
        if self._stack is not None:
            raise NotConnectedError("connector is already connected")
        stack = AsyncExitStack()
        tg = await stack.enter_async_context(anyio.create_task_group())
        ready, self._stop = anyio.Event(), anyio.Event()
        stop = self._stop
        failure: list[BaseException] = []
        client = Client(self._transport_cm(), cache=None, mode="auto")

        async def runner() -> None:
            try:
                async with client:
                    self._client = client
                    ready.set()
                    await stop.wait()
            except Exception as exc:  # noqa: BLE001 — stored, then curated by connect()
                failure.append(exc)
            finally:
                self._client = None
                ready.set()

        tg.start_soon(runner)
        try:
            with anyio.fail_after(self._connect_timeout_s):
                await ready.wait()
        except TimeoutError as exc:
            failure.append(exc)
        except BaseException:
            # Outer cancellation (or anything else) mid-connect: unwind the task
            # group we entered so anyio's scope stack stays intact.
            await self._unwind(stack, tg, cancel=True)
            raise
        if failure or self._client is None:
            await self._unwind(stack, tg, cancel=True)
            exc0: BaseException = failure[0] if failure else RuntimeError("closed during connect")
            err = _curate(exc0, phase="connect", transport=self._transport)
            log.info("mcp connect failed: kind=%s exc_type=%s", err.kind, type(exc0).__name__)
            raise err from exc0
        self._stack, self._tg = stack, tg
        info = client.server_info
        self._info = ServerInfo(
            name=info.name if info else None,
            version=(info.version or None) if info else None,
            protocol_version=client.protocol_version,
        )
        return self._info

    async def initialize(self) -> ServerInfo:
        """Idempotent: connects (transport + handshake) on first call."""
        if self._info is None:
            return await self.connect()
        return self._info

    async def _unwind(
        self, stack: AsyncExitStack, tg: anyio.abc.TaskGroup, *, cancel: bool
    ) -> None:
        """Exit the runner's task group, bounded and immune to outer cancels.

        The group's OWN scope is shielded and given a deadline (a new scope
        around the exit would break anyio's LIFO scope rule). The deadline
        bounds teardown — e.g. a legacy server's session-terminate request,
        which the SDK gives a 300s read timeout.
        """
        scope = tg.cancel_scope
        scope.shield = True
        if cancel:
            scope.cancel()
        else:
            scope.deadline = anyio.current_time() + CLOSE_TIMEOUT_S
        try:
            await stack.aclose()
        except Exception as exc:  # noqa: BLE001 — teardown noise; nothing to curate
            log.debug("mcp close raised %s (ignored)", type(exc).__name__)
        self._client = None
        self._close_errlog()

    async def close(self) -> None:
        stack, tg = self._stack, self._tg
        self._stack = self._tg = None
        self._info = None
        if self._stop is not None:
            self._stop.set()
        if stack is not None and tg is not None:
            await self._unwind(stack, tg, cancel=False)
        else:
            self._client = None
            self._close_errlog()

    def _close_errlog(self) -> None:
        if self._errlog is not None:
            self._errlog.close()
            self._errlog = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _require(self) -> Client:
        if self._client is None:
            raise NotConnectedError("connector is not connected")
        return self._client

    # ------------------------------------------------------------- requests
    async def list_tools(self) -> list[ToolDescriptor]:
        client = self._require()
        tools: list[ToolDescriptor] = []
        cursor: str | None = None
        seen: set[str] = set()
        try:
            with anyio.fail_after(self._request_timeout_s):
                for _ in range(MAX_LIST_PAGES):
                    page = await client.list_tools(cursor=cursor)
                    for t in page.tools:
                        tools.append(
                            ToolDescriptor(
                                name=t.name,
                                description=t.description or "",
                                input_schema=dict(t.input_schema),
                                title=t.title,
                                annotations=(
                                    t.annotations.model_dump(
                                        mode="json", by_alias=True, exclude_none=True
                                    )
                                    if t.annotations
                                    else None
                                ),
                            )
                        )
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                    if cursor in seen:
                        # A partial listing must never reach apply_listing: it
                        # would mark the unlisted tools removed.
                        raise ProtocolFailureError(
                            "server repeated a pagination cursor (tools/list)"
                        )
                    seen.add(cursor)
                else:
                    raise ProtocolFailureError("server paginated without end (tools/list)")
        except Exception as exc:  # noqa: BLE001 — curated below
            raise _curate(exc, phase="tools/list", transport=self._transport) from exc
        return tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *, timeout_s: float | None = None
    ) -> ToolCallResult:
        client = self._require()
        try:
            with anyio.fail_after(timeout_s or self._request_timeout_s):
                res = await client.call_tool(name, arguments or {})
        except Exception as exc:  # noqa: BLE001 — curated below
            raise _curate(exc, phase="tools/call", transport=self._transport) from exc
        return ToolCallResult(
            is_error=res.is_error,
            content=[
                c.model_dump(mode="json", by_alias=True, exclude_none=True) for c in res.content
            ],
            structured=res.structured_content,
        )
