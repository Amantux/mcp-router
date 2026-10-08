"""/api/v1/servers — registration, config import, refresh, guarded delete.

Thin router: validation of shape here (pydantic), everything else in
``mcprouter.discovery``. Wire shapes are camelCase (SPEC §8 MCPServer plus
additive fields). Errors are curated messages from typed domain exceptions;
upstream/subprocess text never reaches a response.

Credentials: ``env`` is write-only. Responses expose only ``envNames``.

Include at integration (app.py):
    from mcprouter.api.routes_servers import router as servers_router
    app.include_router(servers_router)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import anyio.to_thread
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.discovery import (
    DiscoveryService,
    DuplicateServerError,
    InvalidRegistrationError,
    ServerDisabledError,
    ServerInUseError,
    ServerNotFoundError,
    ServerRegistration,
    SyncReport,
    delete_server,
    import_config,
    register_server,
)
from mcprouter.discovery.credentials import env_names
from mcprouter.discovery.registry import get_server, tool_counts
from mcprouter.mcpclient import ConnectorError
from mcprouter.models import MCPServerRecord

router = APIRouter(prefix="/api/v1/servers", tags=["servers"])

_DUPLICATE = "a server with this name already exists"


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ServerIn(_Wire):
    name: str = Field(min_length=1, max_length=120)
    transport: Literal["stdio", "streamable-http", "sse"]
    endpoint: str | None = Field(default=None, max_length=2048)
    command: list[str] | None = Field(default=None, min_length=1, max_length=64)
    env: dict[str, str] | None = None  # write-only credentials
    enabled: bool = True


class ServerOut(_Wire):
    id: str
    name: str
    transport: str
    endpoint: str | None
    enabled: bool
    status: str
    version: str | None
    last_discovered_at: datetime | None
    last_health_at: datetime | None
    tool_count: int
    env_names: list[str]


class ImportSkip(_Wire):
    name: str
    reason: str


class ImportOut(_Wire):
    created: list[ServerOut]
    skipped: list[ImportSkip]


class RefreshOut(_Wire):
    server: ServerOut
    added: list[str]
    schema_changed: list[str]
    metadata_changed: list[str]
    removed: list[str]
    restored: list[str]
    unchanged: int
    skipped: int
    latency_ms: float


# ----------------------------------------------------------------- helpers
def _factory(request: Request) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    return factory


def _service(request: Request) -> DiscoveryService:
    """One DiscoveryService per app (shared health tracker). Integration may
    pre-set ``app.state.discovery`` (e.g. with the SyncLoop's service)."""
    svc: DiscoveryService | None = getattr(request.app.state, "discovery", None)
    if svc is None:
        svc = DiscoveryService(_factory(request))
        request.app.state.discovery = svc
    return svc


def _views(session: Session, servers: list[MCPServerRecord]) -> list[ServerOut]:
    ids = [s.id for s in servers]
    counts, names = tool_counts(session, ids), env_names(session, ids)  # 2 queries total
    return [
        ServerOut(
            id=s.id,
            name=s.name,
            transport=s.transport,
            endpoint=redact_endpoint(s.endpoint),
            enabled=s.enabled,
            status=s.status,
            version=s.server_version,
            last_discovered_at=s.last_discovered_at,
            last_health_at=s.last_health_at,
            tool_count=counts.get(s.id, 0),
            env_names=names.get(s.id, []),
        )
        for s in servers
    ]


def redact_endpoint(url: str | None) -> str | None:
    """Query strings commonly carry API keys (``?api_key=...``): never echo
    them. (Secrets embedded in the PATH can't be detected generically.)"""
    if not url:
        return url
    parts = urlsplit(url)
    if not parts.query and not parts.fragment:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "redacted", ""))


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "server not found")


# ------------------------------------------------------------------ routes
@router.get("", response_model=list[ServerOut])
def list_servers(request: Request) -> list[ServerOut]:
    with _factory(request)() as s:
        servers = list(s.scalars(select(MCPServerRecord).order_by(MCPServerRecord.name)))
        return _views(s, servers)


@router.get("/{server_id}", response_model=ServerOut)
def get_server_view(server_id: str, request: Request) -> ServerOut:
    with _factory(request)() as s:
        try:
            return _views(s, [get_server(s, server_id)])[0]
        except ServerNotFoundError:
            raise _not_found() from None


@router.post("", response_model=ServerOut, status_code=status.HTTP_201_CREATED)
def create_server(body: ServerIn, request: Request) -> ServerOut:
    reg = ServerRegistration(
        name=body.name,
        transport=body.transport,
        endpoint=body.endpoint,
        command=tuple(body.command) if body.command else None,
        env=body.env or {},
        enabled=body.enabled,
    )
    try:
        with _factory(request)() as s, s.begin():
            rec = register_server(s, reg)
            s.flush()
            return _views(s, [rec])[0]
    except InvalidRegistrationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from None
    except (DuplicateServerError, IntegrityError):
        # IntegrityError: lost a race with a concurrent create of the same name.
        raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE) from None


@router.post("/import", response_model=ImportOut)
def import_servers(body: dict[str, Any], request: Request) -> ImportOut:
    """Body: a Claude-Desktop-style ``{"mcpServers": {...}}`` document."""
    try:
        with _factory(request)() as s, s.begin():
            rep = import_config(s, body)
            s.flush()
            created = _views(s, rep.created)
    except InvalidRegistrationError as exc:  # whole-document format errors
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from None
    except IntegrityError:  # lost a race with a concurrent create/import
        raise HTTPException(status.HTTP_409_CONFLICT, _DUPLICATE) from None
    return ImportOut(
        created=created, skipped=[ImportSkip(name=n, reason=r) for n, r in rep.skipped]
    )


_CONNECTOR_STATUS = {
    "invalid_target": status.HTTP_400_BAD_REQUEST,
    "timeout": status.HTTP_504_GATEWAY_TIMEOUT,
}


@router.post("/{server_id}/refresh", response_model=RefreshOut)
async def refresh_server(server_id: str, request: Request) -> RefreshOut:
    svc = _service(request)
    try:
        report: SyncReport = await svc.sync_server(server_id)
    except ServerNotFoundError:
        raise _not_found() from None
    except ServerDisabledError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.message) from None
    except ConnectorError as exc:
        code = _CONNECTOR_STATUS.get(exc.kind, status.HTTP_502_BAD_GATEWAY)
        raise HTTPException(code, f"discovery failed: {exc.message}") from None

    def _view() -> ServerOut:
        with _factory(request)() as s:
            return _views(s, [get_server(s, server_id)])[0]

    try:
        view = await anyio.to_thread.run_sync(_view)
    except ServerNotFoundError:  # deleted concurrently
        raise _not_found() from None
    return RefreshOut(
        server=view,
        added=report.added,
        schema_changed=report.schema_changed,
        metadata_changed=report.metadata_changed,
        removed=report.removed,
        restored=report.restored,
        unchanged=report.unchanged,
        skipped=report.skipped,
        latency_ms=round(report.latency_ms, 2),
    )


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_server(server_id: str, request: Request) -> Response:
    try:
        with _factory(request)() as s, s.begin():
            delete_server(s, server_id)
    except ServerNotFoundError:
        raise _not_found() from None
    except ServerInUseError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.message) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
