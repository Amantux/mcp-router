"""MCP client connector layer (stdio, streamable HTTP, legacy SSE best-effort)."""

from mcprouter.mcpclient.connector import (
    Connector,
    ServerInfo,
    ToolCallResult,
    ToolDescriptor,
)
from mcprouter.mcpclient.errors import (
    ConnectorError,
    ConnectorTimeoutError,
    InvalidTargetError,
    NotConnectedError,
    ProtocolFailureError,
    ServerUnreachableError,
    SpawnFailedError,
)
from mcprouter.mcpclient.targets import ServerTarget, validate_http_url

__all__ = [
    "Connector",
    "ConnectorError",
    "ConnectorTimeoutError",
    "InvalidTargetError",
    "NotConnectedError",
    "ProtocolFailureError",
    "ServerInfo",
    "ServerTarget",
    "ServerUnreachableError",
    "SpawnFailedError",
    "ToolCallResult",
    "ToolDescriptor",
    "validate_http_url",
]
