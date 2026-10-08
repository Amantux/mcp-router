"""Catalog lifecycle (integration gaps 1 and 4): post-sync classify + embed
on manual refresh and in the SyncLoop; SyncLoop started by create_app behind
MCPR_SYNC_ENABLED."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker
from testbed.fleet import generate_fleet, with_description
from testbed.harness import InprocFleet

from mcprouter.api.app import create_app
from mcprouter.discovery import DiscoveryService, ServerRegistration, SyncLoop, register_server
from mcprouter.discovery.sync import SyncReport
from mcprouter.inference.hash_backend import HashEmbeddingBackend
from mcprouter.lifecycle import make_post_sync_hook
from mcprouter.models import MCPToolRecord
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db

SF = sessionmaker[Session]


def _register(db: SF, fleet: InprocFleet) -> dict[str, str]:
    ids: dict[str, str] = {}
    with db() as s, s.begin():
        for name in fleet.specs:
            ids[name] = register_server(
                s,
                ServerRegistration(
                    name=name, transport="streamable-http", endpoint=fleet.endpoint(name)
                ),
            ).id
    return ids


def _tools(db: SF, server_id: str) -> list[MCPToolRecord]:
    with db() as s:
        return list(s.scalars(select(MCPToolRecord).where(MCPToolRecord.server_id == server_id)))


def _assert_classified_and_embedded(db: SF, server_id: str) -> None:
    tools = _tools(db, server_id)
    assert tools
    assert all(t.classification_source == "rules-v1" for t in tools)
    assert any(t.operation != "unknown" for t in tools)
    backend = HashEmbeddingBackend().name  # the default (zero-ML) backend
    assert all(t.embedding is not None and t.embedding_backend == backend for t in tools)


@requires_db
async def test_sync_server_runs_post_sync_classify_and_embed(db: SF) -> None:
    fleet = InprocFleet([generate_fleet(1)[0]])
    sid = next(iter(_register(db, fleet).values()))
    hook = make_post_sync_hook(db, HashEmbeddingBackend())
    svc = DiscoveryService(db, connector_factory=fleet.factory, post_sync=hook)
    await svc.sync_server(sid)
    _assert_classified_and_embedded(db, sid)


@requires_db
async def test_post_sync_respects_classification_reviewed(db: SF) -> None:
    spec = generate_fleet(1)[0]
    fleet = InprocFleet([spec])
    sid = _register(db, fleet)[spec.name]
    svc = DiscoveryService(
        db,
        connector_factory=fleet.factory,
        post_sync=make_post_sync_hook(db, HashEmbeddingBackend()),
    )
    await svc.sync_server(sid)
    name = spec.tools[0].name
    with db() as s:
        s.execute(
            update(MCPToolRecord)
            .where(MCPToolRecord.server_id == sid, MCPToolRecord.name == name)
            .values(
                operation="write",
                domain="human-domain",
                classification_reviewed=True,
                classification_source="human",
            )
        )
        s.commit()
    fleet.specs[spec.name] = with_description(spec, name, "Delete everything forever.")
    report = await svc.sync_server(sid)
    assert report.metadata_changed == [name]
    tool = next(t for t in _tools(db, sid) if t.name == name)
    assert (tool.operation, tool.domain, tool.classification_source) == (
        "write",
        "human-domain",
        "human",
    )


@requires_db
async def test_sync_loop_runs_the_hook_once_per_pass(db: SF) -> None:
    fleet = InprocFleet(generate_fleet(3))
    ids = _register(db, fleet)
    calls: list[int] = []
    inner = make_post_sync_hook(db, HashEmbeddingBackend())

    def hook(reports: Sequence[SyncReport]) -> None:
        calls.append(len(reports))
        inner(reports)

    svc = DiscoveryService(db, connector_factory=fleet.factory, post_sync=hook)
    loop = SyncLoop(svc)
    await loop.run_once(now=1000.0)
    assert calls == [3]
    for sid in ids.values():
        _assert_classified_and_embedded(db, sid)
    await loop.run_once(now=2000.0)  # unchanged catalog: no hook
    assert calls == [3]


@requires_db
async def test_hook_failure_never_fails_the_refresh(db: SF) -> None:
    fleet = InprocFleet([generate_fleet(1)[0]])
    sid = next(iter(_register(db, fleet).values()))

    def boom(reports: Sequence[SyncReport]) -> None:
        raise RuntimeError("classifier exploded password=hunter2")

    svc = DiscoveryService(db, connector_factory=fleet.factory, post_sync=boom)
    report = await svc.sync_server(sid)
    assert report.added


@requires_db
def test_manual_refresh_endpoint_classifies_and_embeds(db: SF) -> None:
    """Gap 1 through the real app wiring: create_app installs the hook."""
    app = create_app(Settings(database_url=TEST_DB_URL), env={})
    fleet = InprocFleet([generate_fleet(1)[0]])
    sid = next(iter(_register(app.state.session_factory, fleet).values()))
    app.state.discovery._connector_factory = fleet.factory  # in-process fleet, no sockets
    r = TestClient(app).post(f"/api/v1/servers/{sid}/refresh")
    assert r.status_code == 200, r.text
    _assert_classified_and_embedded(app.state.session_factory, sid)


# ------------------------------------------------------------ gap 4: loop
def test_sync_enabled_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_SYNC_ENABLED", "")
    assert Settings.from_env().sync_enabled is False  # empty string = unset
    for raw in ("true", "1", "YES", "on"):
        monkeypatch.setenv("MCPR_SYNC_ENABLED", raw)
        assert Settings.from_env().sync_enabled is True
    for raw in ("false", "0", "no", "off"):
        monkeypatch.setenv("MCPR_SYNC_ENABLED", raw)
        assert Settings.from_env().sync_enabled is False
    monkeypatch.setenv("MCPR_SYNC_ENABLED", "maybe")
    with pytest.raises(ValueError):
        Settings.from_env()


@requires_db
def test_create_app_starts_and_stops_sync_loop_when_enabled(
    db: SF, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[bool] = []
    real_stop = SyncLoop.stop

    async def spy_stop(self: SyncLoop) -> None:
        stopped.append(self.running)  # still running when the lifespan stops it
        await real_stop(self)

    monkeypatch.setattr(SyncLoop, "stop", spy_stop)
    app = create_app(Settings(database_url=TEST_DB_URL, sync_enabled=True), env={})
    with TestClient(app):
        loop: Any = app.state.sync_loop
        assert isinstance(loop, SyncLoop) and loop.running
    assert stopped == [True]  # stopped explicitly in lifespan shutdown
    assert not loop.running


@requires_db
def test_create_app_does_not_start_sync_loop_by_default(db: SF) -> None:
    app = create_app(Settings(database_url=TEST_DB_URL), env={})
    with TestClient(app):
        assert app.state.sync_loop is None
