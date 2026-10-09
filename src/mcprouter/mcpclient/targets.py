"""Connection targets + point-of-use validation.

URLs are validated HERE, when a connector is built, not only where a server
is registered: registrations also arrive via config import and direct DB
writes (seed scripts, other subsystems) that bypass API validation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from mcprouter.mcpclient.errors import InvalidTargetError
from mcprouter.net_policy import ForbiddenAddressError, resolve_and_check

Transport = Literal["stdio", "streamable-http", "sse"]
TRANSPORTS: tuple[Transport, ...] = ("stdio", "streamable-http", "sse")


def validate_http_url(url: str) -> str:
    """Return ``url`` if it is an http(s) URL with a host and no userinfo that
    is not, and does not resolve to, a link-local/metadata address
    (net_policy; private LAN ranges stay allowed).

    Userinfo is refused because credentials in a URL leak into logs, caches
    and error messages; supply them via server-side credential storage.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidTargetError("endpoint URL is required")
    if any(c in url for c in "\r\n\t ") or url != url.strip():
        raise InvalidTargetError("endpoint URL must not contain whitespace")
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        raise InvalidTargetError("endpoint URL is malformed") from None
    if parts.scheme not in ("http", "https"):
        raise InvalidTargetError("endpoint URL must use http or https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise InvalidTargetError("endpoint URL must not contain credentials")
    if not parts.hostname:
        raise InvalidTargetError("endpoint URL must include a host")
    try:
        resolve_and_check(parts.hostname)
    except ForbiddenAddressError as exc:  # curated constant message
        raise InvalidTargetError(str(exc)) from None
    return url


@dataclass(frozen=True)
class ServerTarget:
    """Everything needed to reach one MCP server.

    ``env`` holds credentials: excluded from ``repr`` so it can never ride
    along into a log line or traceback by accident.
    """

    transport: Transport
    endpoint: str | None = None  # streamable-http / sse
    command: tuple[str, ...] | None = None  # stdio: argv
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    cwd: str | None = None

    def validated(self) -> ServerTarget:
        if self.transport not in TRANSPORTS:
            raise InvalidTargetError("unsupported transport")
        if self.transport == "stdio":
            if not self.command or not self.command[0].strip():
                raise InvalidTargetError("stdio server requires a command")
            if any("\x00" in part for part in self.command):
                raise InvalidTargetError("stdio command must not contain NUL bytes")
            for k, v in self.env.items():
                if not isinstance(k, str) or not isinstance(v, str) or "\x00" in k + v or "=" in k:
                    raise InvalidTargetError("stdio env must map names to string values")
        else:
            validate_http_url(self.endpoint or "")
        return self
