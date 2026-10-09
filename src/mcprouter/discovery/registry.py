"""Server registration, lookup and guarded deletion.

All functions take a caller-owned ``Session`` and never commit: the caller
(router / service) owns the transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mcprouter.discovery.credentials import get_env, set_env
from mcprouter.mcpclient import InvalidTargetError, ServerTarget
from mcprouter.mcpclient.targets import TRANSPORTS, Transport
from mcprouter.models import ExecutionRecord, MCPServerRecord, MCPToolRecord, PolicyRule

NAME_MAX = 120  # MCPServerRecord.name String(120)


class RegistryError(Exception):
    """Curated; ``message`` is safe to return through the API."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidRegistrationError(RegistryError):
    pass


class DuplicateServerError(RegistryError):
    pass


class ServerNotFoundError(RegistryError):
    pass


class ServerInUseError(RegistryError):
    pass


class ServerDisabledError(RegistryError):
    pass


@dataclass(frozen=True)
class ServerRegistration:
    name: str
    transport: Transport
    endpoint: str | None = None
    command: tuple[str, ...] | None = None
    env: Mapping[str, str] = field(default_factory=dict, repr=False)  # credentials
    enabled: bool = True

    def target(self) -> ServerTarget:
        return ServerTarget(
            transport=self.transport, endpoint=self.endpoint, command=self.command, env=self.env
        )


def validate_registration(reg: ServerRegistration) -> ServerRegistration:
    name = reg.name.strip() if isinstance(reg.name, str) else ""
    if not name or len(name) > NAME_MAX:
        raise InvalidRegistrationError(f"server name must be 1-{NAME_MAX} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise InvalidRegistrationError("server name must not contain control characters")
    if reg.transport not in TRANSPORTS:
        raise InvalidRegistrationError("transport must be one of: " + ", ".join(TRANSPORTS))
    if reg.transport == "stdio" and reg.endpoint:
        raise InvalidRegistrationError("stdio servers take a command, not an endpoint")
    if reg.transport != "stdio" and (reg.command or reg.env):
        raise InvalidRegistrationError("HTTP servers take an endpoint, not a command or env")
    try:
        reg.target().validated()
    except InvalidTargetError as exc:
        raise InvalidRegistrationError(exc.message) from None
    return reg if name == reg.name else _renamed(reg, name)


def _renamed(reg: ServerRegistration, name: str) -> ServerRegistration:
    return ServerRegistration(
        name=name,
        transport=reg.transport,
        endpoint=reg.endpoint,
        command=reg.command,
        env=reg.env,
        enabled=reg.enabled,
    )


def register_server(session: Session, reg: ServerRegistration) -> MCPServerRecord:
    reg = validate_registration(reg)
    exists = session.scalar(select(MCPServerRecord.id).where(MCPServerRecord.name == reg.name))
    if exists is not None:
        raise DuplicateServerError("a server with this name already exists")
    rec = MCPServerRecord(
        id=str(uuid.uuid4()),
        name=reg.name,
        transport=reg.transport,
        endpoint=reg.endpoint,
        stdio_command=list(reg.command) if reg.command else None,
        enabled=reg.enabled,
        status="unknown",
    )
    session.add(rec)
    if reg.env:
        session.flush()  # FK target must exist before the credential row
        set_env(session, rec.id, dict(reg.env))
    return rec


def get_server(session: Session, server_id: str, *, lock: bool = False) -> MCPServerRecord:
    rec = session.get(MCPServerRecord, server_id, with_for_update=lock)
    if rec is None:
        raise ServerNotFoundError("server not found")
    return rec


def set_server_enabled(session: Session, server_id: str, enabled: bool) -> MCPServerRecord:
    rec = get_server(session, server_id, lock=True)
    rec.enabled = enabled
    return rec


def target_for(session: Session, server: MCPServerRecord) -> ServerTarget:
    """Build a connection target from a stored row (validated at point of use
    by the Connector, whatever wrote the row)."""
    transport = server.transport
    if transport not in TRANSPORTS:
        raise InvalidTargetError("unsupported transport")
    return ServerTarget(
        transport=transport,
        endpoint=server.endpoint,
        command=tuple(server.stdio_command) if server.stdio_command else None,
        env=get_env(session, server.id),
    )


def referencing_rule_count(session: Session, server_id: str) -> int:
    """Policy rules that name this server explicitly (``server_id`` match).

    Rules with ``server_id=None`` apply to any server and do not pin a
    specific one, so they don't block deletion. Only ``resource_kind='tool'``
    rules count: a skill rule's server_id is a skill-source id.
    """
    return int(
        session.scalar(
            select(func.count())
            .select_from(PolicyRule)
            .where(PolicyRule.server_id == server_id, PolicyRule.resource_kind == "tool")
        )
        or 0
    )


def has_audit_history(session: Session, server_id: str) -> bool:
    """Execution audit rows point at this server (no FK, so deleting would
    orphan them and cascade away its tool version history)."""
    return (
        session.scalar(
            select(ExecutionRecord.id).where(ExecutionRecord.server_id == server_id).limit(1)
        )
        is not None
    )


def delete_server(session: Session, server_id: str) -> None:
    server = get_server(session, server_id, lock=True)
    if referencing_rule_count(session, server_id):
        raise ServerInUseError(
            "server is referenced by policy rules; remove those rules before deleting it"
        )
    if has_audit_history(session, server_id):
        raise ServerInUseError("server has execution history; disable it instead of deleting it")
    session.delete(server)  # tools, versions (FK cascade) and credentials go with it


def tool_counts(session: Session, server_ids: list[str]) -> dict[str, int]:
    if not server_ids:
        return {}
    rows = session.execute(
        select(MCPToolRecord.server_id, func.count())
        .where(MCPToolRecord.server_id.in_(server_ids), MCPToolRecord.available.is_(True))
        .group_by(MCPToolRecord.server_id)
    )
    return {sid: int(n) for sid, n in rows}
