"""Import Claude-Desktop-style MCP client configs (FR-01).

Accepted shape::

    {"mcpServers": {
        "<name>": {"command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": "..."}},
        "<name>": {"url": "https://host/mcp"},                      # streamable HTTP
        "<name>": {"url": "https://host/sse", "type": "sse"},       # legacy SSE
        "<name>": {..., "disabled": true}
    }}

``env`` values are credentials: stored server-side, never logged, never
echoed in an error. Each entry is validated independently — one bad entry
is reported and skipped, the rest import. Entries carrying HTTP ``headers``
are refused (not silently stripped): the connector cannot send them yet and
importing the server without its auth would only produce a broken entry.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from mcprouter.discovery.logsafe import scrub
from mcprouter.discovery.registry import (
    InvalidRegistrationError,
    RegistryError,
    ServerRegistration,
    register_server,
    validate_registration,
)
from mcprouter.mcpclient.targets import Transport
from mcprouter.models import MCPServerRecord

log = logging.getLogger(__name__)

MAX_ENTRIES = 500

_HTTP_TYPES: dict[str, Transport] = {
    "http": "streamable-http",
    "streamable-http": "streamable-http",
    "streamablehttp": "streamable-http",
    "sse": "sse",
}


@dataclass(frozen=True)
class ImportedServer:
    """A parsed entry, or the curated reason it was rejected."""

    name: str
    registration: ServerRegistration | None = None
    error: str | None = None


@dataclass
class ImportReport:
    created: list[MCPServerRecord] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (name, curated reason)


class ConfigFormatError(InvalidRegistrationError):
    pass


def _parse_entry(name: str, raw: Any) -> ServerRegistration:
    if not isinstance(raw, Mapping):
        raise InvalidRegistrationError("entry must be an object")
    if raw.get("headers"):
        raise InvalidRegistrationError("HTTP headers are not supported yet; entry not imported")
    disabled = raw.get("disabled", False)
    if not isinstance(disabled, bool):
        raise InvalidRegistrationError("disabled must be true or false")
    enabled = not disabled
    kind = str(raw.get("type") or raw.get("transport") or "").strip().lower()
    if "url" in raw:
        transport = _HTTP_TYPES.get(kind or "http")
        if transport is None:
            raise InvalidRegistrationError("unsupported transport type for a url entry")
        url = raw["url"]
        if not isinstance(url, str):
            raise InvalidRegistrationError("url must be a string")
        return ServerRegistration(name=name, transport=transport, endpoint=url, enabled=enabled)
    if "command" in raw:
        if kind not in ("", "stdio"):
            raise InvalidRegistrationError("unsupported transport type for a command entry")
        command, args, env = raw["command"], raw.get("args", []), raw.get("env", {})
        if not isinstance(command, str) or not command.strip():
            raise InvalidRegistrationError("command must be a non-empty string")
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise InvalidRegistrationError("args must be a list of strings")
        if not isinstance(env, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise InvalidRegistrationError("env must map names to string values")
        return ServerRegistration(
            name=name,
            transport="stdio",
            command=(command, *args),
            env=dict(env),
            enabled=enabled,
        )
    raise InvalidRegistrationError("entry needs either a command or a url")


def parse_mcp_servers_config(config: str | Mapping[str, Any]) -> list[ImportedServer]:
    """Parse + validate every entry. Never raises for a single bad entry."""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            raise ConfigFormatError("config is not valid JSON") from None
    if not isinstance(config, Mapping) or not isinstance(config.get("mcpServers"), Mapping):
        raise ConfigFormatError('config must be an object with an "mcpServers" object')
    entries = config["mcpServers"]
    if len(entries) > MAX_ENTRIES:
        raise ConfigFormatError(f"config has more than {MAX_ENTRIES} servers")
    out: list[ImportedServer] = []
    for name, raw in entries.items():
        try:
            reg = validate_registration(_parse_entry(str(name), raw))
            out.append(ImportedServer(name=reg.name, registration=reg))
        except InvalidRegistrationError as exc:
            out.append(ImportedServer(name=str(name), error=exc.message))
    return out


def import_config(session: Session, config: str | Mapping[str, Any]) -> ImportReport:
    """Register every valid entry; existing names are skipped, never overwritten.
    No commit — the caller owns the transaction."""
    report = ImportReport()
    for item in parse_mcp_servers_config(config):
        if item.registration is None:
            report.skipped.append((item.name, item.error or "invalid entry"))
            continue
        try:
            with session.begin_nested():
                report.created.append(register_server(session, item.registration))
        except RegistryError as exc:
            report.skipped.append((item.name, exc.message))
    # Names only — never env values, commands or URLs (which may carry tokens).
    log.info(
        "config import: %d registered, %d skipped (%s)",
        len(report.created),
        len(report.skipped),
        ", ".join(scrub(n) for n, _ in report.skipped[:20]),
    )
    return report
