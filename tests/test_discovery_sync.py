"""Catalog sync, change detection, versioning, health transitions, sync loop."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from testbed.fleet import (
    generate_fleet,
    with_description,
    with_extra_param,
    with_tool,
    without_tool,
)
from testbed.harness import InprocFleet

from mcprouter.discovery import (
    DiscoveryService,
    HealthPolicy,
    HealthTracker,
    ServerRegistration,
    SyncLoop,
    apply_listing,
    next_status,
    register_server,
)
from mcprouter.discovery.hashing import canonical_json, schema_hash
from mcprouter.mcpclient import ConnectorError, ToolDescriptor
from mcprouter.models import MCPServerRecord, MCPToolRecord, ToolVersionRecord

from .conftest import requires_db

SF = sessionmaker[Session]


# ------------------------------------------------------------------ hashing
def test_schema_hash_is_key_order_and_whitespace_independent() -> None:
    a = {"type": "object", "properties": {"b": {"type": "string"}, "a": {"type": "integer"}}}
    b = {"properties": {"a": {"type": "integer"}, "b": {"type": "string"}}, "type": "object"}
    assert schema_hash(a) == schema_hash(b)
    assert canonical_json(a) == canonical_json(b)
    assert schema_hash(a) != schema_hash({**a, "required": ["a"]})
    assert len(schema_hash(a)) == 64


# ------------------------------------------------------------------- health
@pytest.mark.parametrize(
    ("prev", "ok", "latency", "failures", "expected"),
    [
        ("unknown", True, 10.0, 0, "healthy"),
        ("offline", True, 10.0, 0, "healthy"),
        ("healthy", True, 5000.0, 0, "degraded"),
        ("healthy", False, None, 1, "degraded"),
        ("degraded", False, None, 2, "degraded"),
        ("degraded", False, None, 3, "offline"),
        ("unknown", False, None, 1, "offline"),
        ("offline", False, None, 1, "offline"),
    ],
)
def test_next_status(
    prev: str, ok: bool, latency: float | None, failures: int, expected: str
) -> None:
    assert (
        next_status(prev, ok=ok, latency_ms=latency, failures=failures, policy=HealthPolicy())
        == expected
    )


# --------------------------------------------------------------- fixtures
def _register(db: SF, fleet: InprocFleet) -> dict[str, str]:
    ids: dict[str, str] = {}
    with db() as s, s.begin():
        for name in fleet.specs:
            rec = register_server(
                s,
                ServerRegistration(
                    name=name, transport="streamable-http", endpoint=fleet.endpoint(name)
                ),
            )
            ids[name] = rec.id
    return ids


def _tool(db: SF, server_id: str, name: str) -> MCPToolRecord:
    with db() as s:
        rec = s.scalar(
            select(MCPToolRecord).where(
                MCPToolRecord.server_id == server_id, MCPToolRecord.name == name
            )
        )
        assert rec is not None
        return rec


def _versions(db: SF, tool_id: str) -> list[tuple[int, str]]:
    with db() as s:
        rows = s.scalars(
            select(ToolVersionRecord)
            .where(ToolVersionRecord.tool_id == tool_id)
            .order_by(ToolVersionRecord.version)
        )
        return [(r.version, r.change_kind) for r in rows]


def _server(db: SF, server_id: str) -> MCPServerRecord:
    with db() as s:
        rec = s.get(MCPServerRecord, server_id)
        assert rec is not None
        return rec


# ---------------------------------------------------------- change detection
@requires_db
async def test_full_lifecycle_added_schema_metadata_removed_restored(db: SF) -> None:
    spec = generate_fleet(1)[0]  # github, 12 tools
    fleet = InprocFleet([spec])
    sid = _register(db, fleet)["github"]
    svc = DiscoveryService(db, connector_factory=fleet.factory)

    # 1. first discovery: everything added at version 1
    r = await svc.sync_server(sid)
    assert sorted(r.added) == sorted(t.name for t in spec.tools)
    gi = _tool(db, sid, "get_issue")
    assert gi.version == 1 and gi.available
    assert gi.schema_hash == schema_hash(spec.tool("get_issue").input_schema())
    assert _versions(db, gi.id) == [(1, "added")]
    srv = _server(db, sid)
    assert srv.status == "healthy" and srv.last_discovered_at is not None
    assert srv.server_version == "1.0.0"
    tool_id = gi.id

    # 2. unchanged re-sync writes nothing
    r = await svc.sync_server(sid)
    assert not r.changed and r.unchanged == len(spec.tools)

    # 3. schema change
    fleet.specs["github"] = spec = with_extra_param(spec, "get_issue", "include_events")
    r = await svc.sync_server(sid)
    assert r.schema_changed == ["get_issue"] and r.unchanged == len(spec.tools) - 1
    assert _versions(db, tool_id)[-1] == (2, "schema")

    # 4. metadata change (description)
    fleet.specs["github"] = spec = with_description(spec, "get_issue", "Fetch one issue.")
    r = await svc.sync_server(sid)
    assert r.metadata_changed == ["get_issue"]
    assert _tool(db, sid, "get_issue").description == "Fetch one issue."

    # 5. metadata change via annotations only (readOnlyHint flips)
    flipped = replace(spec.tool("get_issue"), operation="write")
    fleet.specs["github"] = spec = replace(
        spec, tools=tuple(flipped if t.name == "get_issue" else t for t in spec.tools)
    )
    r = await svc.sync_server(sid)
    assert r.metadata_changed == ["get_issue"]

    # 6. removed: marked unavailable, history kept, id stable
    fleet.specs["github"] = spec_removed = without_tool(spec, "get_issue")
    r = await svc.sync_server(sid)
    assert r.removed == ["get_issue"]
    gi = _tool(db, sid, "get_issue")
    assert not gi.available and gi.id == tool_id
    r = await svc.sync_server(sid)  # staying removed is not another change
    assert not r.changed

    # 7. restored
    fleet.specs["github"] = with_tool(spec_removed, spec.tool("get_issue"))
    r = await svc.sync_server(sid)
    assert r.restored == ["get_issue"]
    gi = _tool(db, sid, "get_issue")
    assert gi.available and gi.id == tool_id
    assert _versions(db, tool_id) == [
        (1, "added"),
        (2, "schema"),
        (3, "metadata"),
        (4, "metadata"),
        (5, "removed"),
        (6, "restored"),
    ]
    with db() as s:
        snap = s.scalar(
            select(ToolVersionRecord.snapshot).where(
                ToolVersionRecord.tool_id == tool_id, ToolVersionRecord.version == 2
            )
        )
    assert snap is not None and "include_events" in snap["inputSchema"]["properties"]


@requires_db
def test_hostile_listing_entries_are_skipped(db: SF) -> None:
    fleet = InprocFleet(generate_fleet(1))
    sid = _register(db, fleet)["github"]
    schema: dict[str, Any] = {"type": "object", "properties": {}}
    listing = [
        ToolDescriptor("ok", "fine", schema),
        ToolDescriptor("ok", "duplicate name", schema),
        ToolDescriptor("x" * 201, "too long", schema),
        ToolDescriptor("", "empty", schema),
    ]
    with db() as s, s.begin():
        report = apply_listing(s, sid, listing, None)
    assert report.added == ["ok"] and report.skipped == 3
    assert _tool(db, sid, "ok").description == "fine"


# ------------------------------------------------------------ failures/health
@requires_db
async def test_outage_does_not_remove_tools_and_drives_health(db: SF) -> None:
    fleet = InprocFleet(generate_fleet(1))
    sid = _register(db, fleet)["github"]
    svc = DiscoveryService(
        db, connector_factory=fleet.factory, health=HealthTracker(HealthPolicy(offline_after=3))
    )
    await svc.sync_server(sid)

    fleet.down.add("github")
    with pytest.raises(ConnectorError) as ei:
        await svc.sync_server(sid)
    assert ei.value.message == "server is unreachable"
    assert _server(db, sid).status == "degraded"
    with db() as s:
        available = s.scalar(
            select(func.count())
            .select_from(MCPToolRecord)
            .where(MCPToolRecord.server_id == sid, MCPToolRecord.available.is_(True))
        )
    assert available == 12  # an outage is not a catalog change

    assert await svc.check_health(sid) == "degraded"
    assert await svc.check_health(sid) == "offline"

    fleet.down.clear()
    assert await svc.check_health(sid) == "healthy"
    assert _server(db, sid).last_health_at is not None


@requires_db
async def test_never_seen_server_failing_is_offline(db: SF) -> None:
    fleet = InprocFleet(generate_fleet(1))
    sid = _register(db, fleet)["github"]
    fleet.down.add("github")
    svc = DiscoveryService(db, connector_factory=fleet.factory)
    with pytest.raises(ConnectorError):
        await svc.sync_server(sid)
    assert _server(db, sid).status == "offline"


@requires_db
async def test_sync_all_isolates_failures(db: SF) -> None:
    fleet = InprocFleet(generate_fleet(6))
    ids = _register(db, fleet)
    fleet.down.add("jira")
    svc = DiscoveryService(db, connector_factory=fleet.factory, concurrency=3)
    results = await svc.sync_all()
    assert set(results) == set(ids.values())
    assert isinstance(results[ids["jira"]], ConnectorError)
    ok = [r for sid, r in results.items() if sid != ids["jira"]]
    assert all(not isinstance(r, ConnectorError) and r.added for r in ok)


@requires_db
async def test_disabled_servers_are_not_synced(db: SF) -> None:
    fleet = InprocFleet(generate_fleet(2))
    ids = _register(db, fleet)
    with db() as s, s.begin():
        srv = s.get(MCPServerRecord, ids["jenkins"])
        assert srv is not None
        srv.enabled = False
    results = await DiscoveryService(db, connector_factory=fleet.factory).sync_all()
    assert set(results) == {ids["github"]}


# ------------------------------------------------------------------ loop
@requires_db
async def test_sync_loop_schedules_sync_then_health(db: SF) -> None:
    fleet = InprocFleet(generate_fleet(2))
    ids = _register(db, fleet)
    svc = DiscoveryService(db, connector_factory=fleet.factory)
    loop = SyncLoop(svc, sync_interval_s=100.0, health_interval_s=10.0)

    synced, checked = await loop.run_once(now=1000.0)
    assert sorted(synced) == sorted(ids.values()) and checked == []
    synced, checked = await loop.run_once(now=1005.0)  # nothing due
    assert synced == [] and checked == []
    loop.set_interval(ids["github"], 1.0)  # per-server override
    synced, checked = await loop.run_once(now=1011.0)
    assert synced == [ids["github"]] and checked == [ids["jenkins"]]


@requires_db
async def test_sync_loop_start_stop(db: SF) -> None:
    import asyncio

    fleet = InprocFleet(generate_fleet(1))
    sid = _register(db, fleet)["github"]
    loop = SyncLoop(DiscoveryService(db, connector_factory=fleet.factory), tick_s=0.01)
    assert not loop.running  # nothing starts at construction/import
    await loop.start()
    with pytest.raises(RuntimeError):
        await loop.start()
    for _ in range(500):
        if _server(db, sid).last_discovered_at is not None:
            break
        await asyncio.sleep(0.01)
    await loop.stop()
    assert not loop.running
    assert _server(db, sid).status == "healthy"


# ------------------------------------------------- review fixes (B2 & minors)
@requires_db
async def test_nul_and_control_chars_from_upstream_do_not_break_sync(db: SF) -> None:
    specs = generate_fleet(3)
    evil = replace(
        specs[1],
        tools=(
            replace(specs[1].tools[0], description="bad\x00desc"),
            replace(specs[1].tools[1], name="evil\nname"),
            *specs[1].tools[2:],
        ),
    )
    fleet = InprocFleet([specs[0], evil, specs[2]])
    ids = _register(db, fleet)
    results = await DiscoveryService(db, connector_factory=fleet.factory).sync_all()
    assert all(not isinstance(r, ConnectorError) for r in results.values())
    r = results[ids["jenkins"]]
    assert not isinstance(r, ConnectorError) and r.skipped == 1
    assert _tool(db, ids["jenkins"], specs[1].tools[0].name).description == "baddesc"


@requires_db
async def test_unexpected_error_in_one_server_never_aborts_refresh(db: SF) -> None:
    fleet = InprocFleet(generate_fleet(3))
    ids = _register(db, fleet)

    def factory(server: Any, target: Any) -> Any:
        if server.name == "jenkins":
            raise RuntimeError("boom with secret-ish text")
        return fleet.factory(server, target)

    results = await DiscoveryService(db, connector_factory=factory).sync_all()
    bad = results[ids["jenkins"]]
    assert isinstance(bad, ConnectorError) and "secret" not in bad.message
    assert all(not isinstance(results[ids[n]], ConnectorError) for n in ("github", "jira"))


@requires_db
def test_apply_listing_for_deleted_server_is_not_found(db: SF) -> None:
    from mcprouter.discovery import ServerNotFoundError

    with db() as s, s.begin(), pytest.raises(ServerNotFoundError):
        apply_listing(s, "00000000-0000-0000-0000-000000000000", [], None)


@requires_db
async def test_disabled_server_is_never_synced_directly(db: SF) -> None:
    from mcprouter.discovery import ServerDisabledError

    fleet = InprocFleet(generate_fleet(1))
    sid = _register(db, fleet)["github"]
    with db() as s, s.begin():
        srv = s.get(MCPServerRecord, sid)
        assert srv is not None
        srv.enabled = False
    with pytest.raises(ServerDisabledError):
        await DiscoveryService(db, connector_factory=fleet.factory).sync_server(sid)


@requires_db
async def test_loop_marks_servers_before_awaiting_sync(db: SF) -> None:
    fleet = InprocFleet(generate_fleet(1))
    _register(db, fleet)
    svc = DiscoveryService(db, connector_factory=fleet.factory)
    loop = SyncLoop(svc, sync_interval_s=100.0, health_interval_s=1000.0)

    async def broken(**_: Any) -> Any:
        raise RuntimeError("pass failed")

    real = svc.sync_all
    svc.sync_all = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await loop.run_once(now=1000.0)
    svc.sync_all = real  # type: ignore[method-assign]
    synced, _ = await loop.run_once(now=1001.0)
    assert synced == []  # no re-sync storm after a failed pass


@requires_db
async def test_removal_snapshot_keeps_last_known_annotations(db: SF) -> None:
    spec = generate_fleet(1)[0]
    fleet = InprocFleet([spec])
    sid = _register(db, fleet)["github"]
    svc = DiscoveryService(db, connector_factory=fleet.factory)
    await svc.sync_server(sid)
    fleet.specs["github"] = without_tool(spec, "get_issue")
    await svc.sync_server(sid)
    tid = _tool(db, sid, "get_issue").id
    with db() as s:
        snap = s.scalar(
            select(ToolVersionRecord.snapshot).where(
                ToolVersionRecord.tool_id == tid, ToolVersionRecord.change_kind == "removed"
            )
        )
    assert snap is not None and snap["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
    }


# ------------------------------------------------- integration: ANALYZE
def _analyze_count(db: SF) -> int:
    from sqlalchemy import text

    with db() as s:
        return int(
            s.execute(
                text("SELECT analyze_count FROM pg_stat_user_tables WHERE relname = 'mcp_tools'")
            ).scalar_one()
        )


@requires_db
async def test_bulk_sync_refreshes_planner_statistics(db: SF) -> None:
    """Routing measured a ~7x slower vector leg on stale stats after bulk sync."""
    fleet = InprocFleet(generate_fleet(2))
    _register(db, fleet)
    svc = DiscoveryService(db, connector_factory=fleet.factory)
    before = _analyze_count(db)
    await svc.sync_all()
    assert _analyze_count(db) > before
    unchanged = _analyze_count(db)
    await svc.sync_all()  # nothing changed -> no ANALYZE
    assert _analyze_count(db) == unchanged
