"""Curated connector errors.

Every failure that crosses the connector boundary is one of these, carrying
a short, fixed message chosen by US — never upstream text. Raw subprocess
stderr, upstream HTTP bodies, and MCP error messages from remote servers are
attacker-influenced and can echo credentials (a DSN, a token in a URL), so
they stop here. The original exception stays chained (``__cause__``) for
server-side debugging only.
"""

from __future__ import annotations

from typing import Literal

ErrorKind = Literal[
    "invalid_target",  # rejected before any I/O (bad URL, empty command ...)
    "spawn_failed",  # stdio command could not be started
    "unreachable",  # network-level failure / connection closed by peer
    "timeout",
    "protocol",  # server answered, but not with something we can use
    "not_connected",  # API misuse: call before connect / after close
]


class ConnectorError(Exception):
    """Base class. ``message`` is safe to return through an API boundary."""

    kind: ErrorKind = "protocol"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidTargetError(ConnectorError):
    kind: ErrorKind = "invalid_target"


class SpawnFailedError(ConnectorError):
    kind: ErrorKind = "spawn_failed"


class ServerUnreachableError(ConnectorError):
    kind: ErrorKind = "unreachable"


class ConnectorTimeoutError(ConnectorError):
    kind: ErrorKind = "timeout"


class ProtocolFailureError(ConnectorError):
    kind: ErrorKind = "protocol"


class NotConnectedError(ConnectorError):
    kind: ErrorKind = "not_connected"
