"""`interfaces.ToolInvoker` over the discovery track's MCP client connector.

The execution manager is the only caller. One connection per call (no pooling
in v0.1 — a stdio server is spawned per call); the target is rebuilt from the
stored row on every call and validated at point of use by the Connector.

Failures leave as `ToolInvocationError(<curated>)`: the connector's own fixed
message (`ConnectorError.message`), never upstream text or `str(exc)`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.discovery.registry import target_for
from mcprouter.interfaces import ToolCallResult, ToolInvocationError
from mcprouter.mcpclient import Connector, ConnectorError, ServerTarget
from mcprouter.models import MCPServerRecord

ConnectorFactory = Callable[[ServerTarget, float], Connector]

MAX_CONNECT_TIMEOUT_S = 15.0


def default_connector_factory(target: ServerTarget, timeout_s: float) -> Connector:
    return Connector(
        target,
        connect_timeout_s=min(timeout_s, MAX_CONNECT_TIMEOUT_S),
        request_timeout_s=timeout_s,
    )


class ConnectorToolInvoker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        connector_factory: ConnectorFactory = default_connector_factory,
    ) -> None:
        self._factory = session_factory
        self._connector_factory = connector_factory

    def _target(self, server: MCPServerRecord) -> ServerTarget:
        with self._factory() as s:
            return target_for(s, server)  # loads stdio env (credentials) server-side

    async def call_tool(
        self,
        server: object,
        tool_name: str,
        arguments: dict[str, object],
        timeout_s: float,
    ) -> ToolCallResult:
        if not isinstance(server, MCPServerRecord):
            raise ToolInvocationError("invalid server record")
        try:
            target = await anyio.to_thread.run_sync(self._target, server)
            # connect() and close() run in this same task (anyio requirement).
            async with self._connector_factory(target, timeout_s) as conn:
                res = await conn.call_tool(tool_name, dict(arguments), timeout_s=timeout_s)
        except ConnectorError as exc:
            raise ToolInvocationError(exc.message) from None
        content: list[dict[str, Any]] = res.content
        structured = res.structured if isinstance(res.structured, dict) else None
        return ToolCallResult(
            content=[dict(c) for c in content],
            is_error=res.is_error,
            structured_content=structured,
        )
