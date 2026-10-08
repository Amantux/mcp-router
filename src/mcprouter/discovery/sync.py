"""Catalog sync: tools/list -> versioned MCPToolRecord rows (FR-01/FR-02).

Change detection per tool name, against the server's stored rows:

=============================  ===========  ====================================
observation                    change_kind  effect
=============================  ===========  ====================================
new name                       added        row created, version 1
was unavailable, listed again  restored     available=True, version+1
schema hash differs            schema       schema + hash updated, version+1
description/title/annotations  metadata     description updated, version+1
listed name missing            removed      available=False, version+1
identical                      —            untouched
=============================  ===========  ====================================

Every change appends one ToolVersionRecord whose ``snapshot`` is the full
tool definition at that version (camelCase, MCP wire-like). A failed
listing NEVER marks tools removed: an outage is a health event, not a
catalog change.

Tool identity: (server_id, name) -> stable ``MCPToolRecord.id`` across
versions, removals and restores.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

import anyio
import anyio.to_thread
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.discovery.hashing import metadata_fingerprint, schema_hash
from mcprouter.discovery.health import HealthTracker
from mcprouter.discovery.logsafe import scrub
from mcprouter.discovery.registry import (
    ServerDisabledError,
    ServerNotFoundError,
    get_server,
    target_for,
)
from mcprouter.mcpclient import (
    Connector,
    ConnectorError,
    ProtocolFailureError,
    ServerInfo,
    ServerTarget,
    ToolDescriptor,
)
from mcprouter.models import MCPServerRecord, MCPToolRecord, ToolVersionRecord, utcnow

log = logging.getLogger(__name__)

TOOL_NAME_MAX = 200  # MCPToolRecord.name String(200)
MAX_TOOLS_PER_SERVER = 5000  # a hostile/buggy server must not flood the catalog

ConnectorFactory = Callable[[MCPServerRecord, ServerTarget], Connector]


def default_connector_factory(server: MCPServerRecord, target: ServerTarget) -> Connector:
    del server
    return Connector(target)


@dataclass
class SyncReport:
    server_id: str
    added: list[str] = field(default_factory=list)
    schema_changed: list[str] = field(default_factory=list)
    metadata_changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    unchanged: int = 0
    skipped: int = 0  # invalid / duplicate / over-cap entries in the listing
    latency_ms: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(
            self.added
            or self.schema_changed
            or self.metadata_changed
            or self.removed
            or self.restored
        )


def snapshot(d: ToolDescriptor, shash: str) -> dict[str, Any]:
    return {
        "name": d.name,
        "title": d.title,
        "description": d.description,
        "inputSchema": d.input_schema,
        "annotations": d.annotations,
        "schemaHash": shash,
    }


def _no_nul(text: str) -> str:
    return text.replace("\x00", "")


def _record_snapshot(rec: MCPToolRecord) -> dict[str, Any]:
    """Snapshot for a removal: the last known definition."""
    return {
        "name": rec.name,
        "title": rec.title,
        "description": rec.description,
        "inputSchema": rec.input_schema,
        "annotations": rec.annotations,
        "schemaHash": rec.schema_hash,
    }


def _latest_snapshots(session: Session, tool_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Latest version snapshot per tool, one query (DISTINCT ON, PostgreSQL)."""
    if not tool_ids:
        return {}
    rows = session.scalars(
        select(ToolVersionRecord)
        .where(ToolVersionRecord.tool_id.in_(tool_ids))
        .order_by(ToolVersionRecord.tool_id, ToolVersionRecord.version.desc())
        .distinct(ToolVersionRecord.tool_id)
    )
    return {r.tool_id: r.snapshot for r in rows}


def apply_listing(
    session: Session,
    server_id: str,
    tools: list[ToolDescriptor],
    info: ServerInfo | None,
    *,
    now: datetime | None = None,
) -> SyncReport:
    """Reconcile one server's stored catalog with a fresh listing. No commit.

    Locks the server row (SELECT ... FOR UPDATE) so concurrent syncs of the
    same server — from another worker or process — serialize instead of
    racing on the (server_id, name) unique index.
    """
    now = now or utcnow()
    report = SyncReport(server_id=server_id)
    server = session.get(MCPServerRecord, server_id, with_for_update=True)
    if server is None:
        raise ServerNotFoundError("server not found")
    existing = {
        t.name: t
        for t in session.scalars(select(MCPToolRecord).where(MCPToolRecord.server_id == server_id))
    }
    prev_snaps = _latest_snapshots(session, [t.id for t in existing.values()])

    seen: set[str] = set()
    # Version rows are added after the tool rows are flushed: there is no ORM
    # relationship between the two, so unit-of-work can't order the inserts.
    pending: list[ToolVersionRecord] = []
    for d in tools:
        if len(seen) >= MAX_TOOLS_PER_SERVER:
            report.skipped += 1
            continue
        if (
            not d.name
            or len(d.name) > TOOL_NAME_MAX
            or d.name in seen
            or any(ord(c) < 32 or ord(c) == 127 for c in d.name)
        ):
            report.skipped += 1
            continue
        seen.add(d.name)
        # PostgreSQL text cannot hold NUL; a hostile server must not be able
        # to fail the whole transaction with one.
        d = replace(
            d,
            description=_no_nul(d.description),
            title=_no_nul(d.title) if d.title is not None else None,
        )
        shash = schema_hash(d.input_schema)
        snap = snapshot(d, shash)
        rec = existing.get(d.name)
        if rec is None:
            rec = MCPToolRecord(
                id=str(uuid.uuid4()),
                server_id=server_id,
                name=d.name,
                description=d.description,
                title=d.title,
                annotations=d.annotations,
                input_schema=d.input_schema,
                schema_hash=shash,
                version=1,
                available=True,
            )
            session.add(rec)
            pending.append(_version(rec, "added", snap, now))
            report.added.append(d.name)
            continue

        prev = prev_snaps.get(rec.id)
        prev_meta = (
            metadata_fingerprint(
                prev.get("description", ""), prev.get("title"), prev.get("annotations")
            )
            if prev is not None
            else metadata_fingerprint(rec.description, None, None)
        )
        kind: str | None
        if not rec.available:
            kind = "restored"
        elif rec.schema_hash != shash:
            kind = "schema"
        elif prev_meta != metadata_fingerprint(d.description, d.title, d.annotations):
            kind = "metadata"
        else:
            kind = None
        if kind is None:
            report.unchanged += 1
            continue
        rec.version += 1
        rec.description = d.description
        rec.title = d.title
        rec.annotations = d.annotations
        rec.input_schema = d.input_schema
        rec.schema_hash = shash
        rec.available = True
        pending.append(_version(rec, kind, snap, now))
        {
            "restored": report.restored,
            "schema": report.schema_changed,
            "metadata": report.metadata_changed,
        }[kind].append(d.name)

    for name, rec in existing.items():
        if name not in seen and rec.available:
            rec.available = False
            rec.version += 1
            pending.append(
                _version(rec, "removed", prev_snaps.get(rec.id) or _record_snapshot(rec), now)
            )
            report.removed.append(name)

    session.flush()
    session.add_all(pending)

    if info is not None and info.version:
        server.server_version = _no_nul(info.version)[:64]
    server.last_discovered_at = now
    if report.skipped:
        log.warning(
            "discovery skipped %d invalid/duplicate tool entries from server %s",
            report.skipped,
            scrub(server.name),
        )
    return report


def _version(
    rec: MCPToolRecord, kind: str, snap: dict[str, Any], now: datetime
) -> ToolVersionRecord:
    return ToolVersionRecord(
        id=str(uuid.uuid4()),
        tool_id=rec.id,
        version=rec.version,
        schema_hash=rec.schema_hash,
        snapshot=snap,
        change_kind=kind,
        recorded_at=now,
    )


class DiscoveryService:
    """Async orchestration: connect -> list -> reconcile, plus health probes.

    DB work runs in worker threads (sync SQLAlchemy, scoping decision 4);
    network work runs on the event loop with bounded concurrency.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        connector_factory: ConnectorFactory = default_connector_factory,
        health: HealthTracker | None = None,
        concurrency: int = 16,
    ) -> None:
        self._sf = session_factory
        self._connector_factory = connector_factory
        self.health = health or HealthTracker()
        self._concurrency = concurrency
        self._locks: dict[str, anyio.Lock] = {}

    # ------------------------------------------------------------- helpers
    def _load_server(self, server_id: str) -> MCPServerRecord:
        with self._sf() as s:
            return get_server(s, server_id)

    def _load(self, server_id: str) -> tuple[MCPServerRecord, ServerTarget]:
        with self._sf() as s:
            server = get_server(s, server_id)
            return server, target_for(s, server)

    def _set_status(self, server_id: str, *, ok: bool, latency_ms: float) -> str:
        with self._sf() as s, s.begin():
            server = s.get(MCPServerRecord, server_id, with_for_update=True)
            if server is None:
                return "offline"
            if ok:
                server.status = self.health.success(server_id, server.status, latency_ms)
            else:
                server.status = self.health.failure(server_id, server.status)
            server.last_health_at = utcnow()
            return server.status

    async def _fetch(
        self, server: MCPServerRecord, target: ServerTarget
    ) -> tuple[ServerInfo, list[ToolDescriptor], float]:
        t0 = time.perf_counter()
        async with self._connector_factory(server, target) as conn:
            info = await conn.initialize()
            tools = await conn.list_tools()
        return info, tools, (time.perf_counter() - t0) * 1000.0

    # ----------------------------------------------------------------- API
    async def sync_server(self, server_id: str) -> SyncReport:
        """Discover one server. Raises ``ConnectorError`` (curated) when the
        server can't be listed — after recording the health failure — and
        ``ServerDisabledError`` for a disabled server (never spawned/dialled)."""
        lock = self._locks.setdefault(server_id, anyio.Lock())
        async with lock:
            server = await anyio.to_thread.run_sync(self._load_server, server_id)
            if not server.enabled:
                raise ServerDisabledError("server is disabled")
            try:
                server, target = await anyio.to_thread.run_sync(self._load, server_id)
                info, tools, latency = await self._fetch(server, target)
            except ConnectorError as exc:
                await anyio.to_thread.run_sync(
                    lambda: self._set_status(server_id, ok=False, latency_ms=0.0)
                )
                log.info("discovery failed for server id %s: %s", scrub(server_id), exc.kind)
                raise

            def _apply() -> SyncReport:
                with self._sf() as s, s.begin():
                    report = apply_listing(s, server_id, tools, info)
                    srv = s.get(MCPServerRecord, server_id)
                    assert srv is not None  # locked inside apply_listing
                    srv.status = self.health.success(server_id, srv.status, latency)
                    srv.last_health_at = srv.last_discovered_at
                report.latency_ms = latency
                return report

            try:
                return await anyio.to_thread.run_sync(_apply)
            except ServerNotFoundError:
                raise
            except Exception as exc:  # noqa: BLE001 — curated; DB/driver text never escapes
                log.warning(
                    "catalog update failed for server id %s: %s",
                    scrub(server_id),
                    type(exc).__name__,
                )
                raise ProtocolFailureError(
                    "server sent tool data that could not be stored"
                ) from exc

    async def check_health(self, server_id: str) -> str:
        """Cheap liveness probe (initialize + tools/list; ``ping`` is deprecated
        on 2026-era MCP). Does not touch the catalog."""
        try:
            server, target = await anyio.to_thread.run_sync(self._load, server_id)
            _, _, latency = await self._fetch(server, target)
        except ConnectorError:
            return await anyio.to_thread.run_sync(
                lambda: self._set_status(server_id, ok=False, latency_ms=0.0)
            )
        return await anyio.to_thread.run_sync(
            lambda: self._set_status(server_id, ok=True, latency_ms=latency)
        )

    def server_ids(self, enabled_only: bool = True) -> list[str]:
        with self._sf() as s:
            q = select(MCPServerRecord.id).order_by(MCPServerRecord.name)
            if enabled_only:
                q = q.where(MCPServerRecord.enabled.is_(True))
            return list(s.scalars(q))

    async def sync_all(
        self, *, enabled_only: bool = True, server_ids: list[str] | None = None
    ) -> dict[str, SyncReport | ConnectorError]:
        """Full catalog refresh with bounded concurrency. Per-server failures
        are returned, not raised: one dead server never aborts the refresh."""
        ids = (
            server_ids
            if server_ids is not None
            else await anyio.to_thread.run_sync(self.server_ids, enabled_only)
        )
        results: dict[str, SyncReport | ConnectorError] = {}
        limiter = anyio.Semaphore(self._concurrency)

        async def one(sid: str) -> None:
            async with limiter:
                try:
                    results[sid] = await self.sync_server(sid)
                except ConnectorError as exc:
                    results[sid] = exc
                except (ServerNotFoundError, ServerDisabledError):
                    pass  # deleted/disabled while the refresh was running
                except Exception as exc:  # noqa: BLE001 — one server never aborts the refresh
                    log.warning("sync of server id %s failed: %s", scrub(sid), type(exc).__name__)
                    results[sid] = ProtocolFailureError("discovery failed unexpectedly")

        async with anyio.create_task_group() as tg:
            for sid in ids:
                tg.start_soon(one, sid)
        return results

    async def check_all(self, server_ids: list[str]) -> dict[str, str]:
        """Health-probe many servers with bounded concurrency."""
        results: dict[str, str] = {}
        limiter = anyio.Semaphore(self._concurrency)

        async def one(sid: str) -> None:
            async with limiter:
                try:
                    results[sid] = await self.check_health(sid)
                except ServerNotFoundError:
                    pass
                except Exception as exc:  # noqa: BLE001 — one probe never aborts the pass
                    log.warning(
                        "health check of server id %s failed: %s", scrub(sid), type(exc).__name__
                    )

        async with anyio.create_task_group() as tg:
            for sid in server_ids:
                tg.start_soon(one, sid)
        return results
