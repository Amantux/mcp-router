"""Route cache (wave 2): bounded LRU + TTL, generation invalidation, and the
CRITICAL invariant — a cache hit may skip retrieval and model calls, never
authorization. Every hit is re-validated against current eligibility and the
current scope before it is returned."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker
from testbed.fleet import generate_fleet, with_description
from testbed.harness import InprocFleet

from mcprouter import generation
from mcprouter.api.app import create_app
from mcprouter.discovery import DiscoveryService, ServerRegistration, register_server
from mcprouter.interfaces import RouteRequest, ToolCandidate
from mcprouter.models import (
    AgentPrincipal,
    MCPServerRecord,
    MCPToolRecord,
    PolicyRule,
    RoutingDecisionRecord,
)
from mcprouter.policy.scope import PolicyScope
from mcprouter.routing.cache import (
    ROUTE_CACHE_EVICTIONS,
    ROUTE_CACHE_HITS,
    CachedRoute,
    QueryEmbeddingCache,
    RouteCache,
)
from mcprouter.routing.pipeline import RoutePipeline
from mcprouter.routing.retriever import HybridRetriever
from mcprouter.routing.scope import AllowAllScope
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db
from .test_routing_fakes import (
    ExplodingDecisionModel,
    FakeHashEmbedder,
    ScriptedDecisionModel,
    add_server,
    add_tool,
)

SF = sessionmaker[Session]


# --------------------------------------------------------------- unit: LRU
def _entry(tag: str) -> CachedRoute:
    return CachedRoute(tools=(), no_match=True, model_version=tag)


def test_lru_evicts_least_recently_used() -> None:
    before = ROUTE_CACHE_EVICTIONS.labels(reason="capacity")._value.get()
    c = RouteCache(size=2, ttl_s=60)
    c.put("a", _entry("a"))
    c.put("b", _entry("b"))
    assert c.get("a") is not None  # a is now most recent
    c.put("c", _entry("c"))
    assert c.get("b") is None
    assert c.get("a") is not None and c.get("c") is not None
    assert ROUTE_CACHE_EVICTIONS.labels(reason="capacity")._value.get() == before + 1


def test_ttl_expires_entries() -> None:
    now = [100.0]
    c = RouteCache(size=10, ttl_s=60, clock=lambda: now[0])
    c.put("k", _entry("k"))
    hits = ROUTE_CACHE_HITS._value.get()
    assert c.get("k") is not None
    assert ROUTE_CACHE_HITS._value.get() == hits + 1
    now[0] += 60.0
    assert c.get("k") is None
    assert len(c) == 0


def test_zero_disables() -> None:
    assert RouteCache.from_settings(0, 60) is None
    assert RouteCache.from_settings(10, 0) is None
    assert RouteCache.from_settings(10, 60) is not None


def test_cache_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_ROUTE_CACHE_TTL_S", "")
    monkeypatch.setenv("MCPR_ROUTE_CACHE_SIZE", "")
    s = Settings.from_env()
    assert (s.route_cache_ttl_s, s.route_cache_size) == (60.0, 1024)
    monkeypatch.setenv("MCPR_ROUTE_CACHE_SIZE", "0")
    assert Settings.from_env().route_cache_size == 0


def test_query_embedding_cache_keys_on_text_and_backend() -> None:
    calls: list[tuple[str, str]] = []
    c = QueryEmbeddingCache(size=2)

    def compute(backend: str) -> Any:
        return lambda t: calls.append((t, backend)) or [0.0]

    c.get_or_compute("q", "hash", compute("hash"))
    c.get_or_compute("q", "hash", compute("hash"))
    c.get_or_compute("q", "bge", compute("bge"))  # different backend: never reused
    assert calls == [("q", "hash"), ("q", "bge")]


# --------------------------------------------------------- pipeline (db)
@dataclass
class MutableScope:
    """A ScopeFilter whose fingerprint never changes while its grants do —
    models a revocation the cache key cannot see (e.g. the commit->bump
    window). Only the post-cache policy pass can catch it."""

    allowed: set[str]
    calls: list[str] = field(default_factory=list)

    def server_ids(self) -> list[str] | None:
        return None

    def permits(self, candidate: ToolCandidate) -> bool:
        return candidate.tool_name in self.allowed

    def fingerprint(self) -> str:
        return "fixed"


@pytest.fixture()
def world(db: SF) -> Iterator[dict[str, Any]]:
    emb = FakeHashEmbedder()
    with db() as s:
        gh = add_server(s, "github")
        add_tool(s, gh, "search_issues", "Search issues", embedder=emb)
        add_tool(s, gh, "list_issues", "List issues", embedder=emb)
        s.commit()
    model = ScriptedDecisionModel()
    pipe = RoutePipeline(db, HybridRetriever(db, emb), model, Settings(database_url=TEST_DB_URL))
    yield {"db": db, "emb": emb, "model": model, "pipe": pipe}


def _names(res: Any) -> set[str]:
    return {t.tool_name for t in res.tools}


@requires_db
def test_second_identical_route_is_a_cache_hit(world: dict[str, Any]) -> None:
    pipe, model, emb, db = world["pipe"], world["model"], world["emb"], world["db"]
    first = pipe.route(RouteRequest("search  issues", "a", 4), AllowAllScope())
    n_model, n_emb = len(model.calls), emb.calls
    second = pipe.route(RouteRequest("search issues", "a", 4), AllowAllScope())  # ws-normalized
    assert not first.cached and second.cached
    assert [t.tool_id for t in second.tools] == [t.tool_id for t in first.tools]
    assert len(model.calls) == n_model and emb.calls == n_emb  # retrieval + model skipped
    assert second.request_id != first.request_id
    with db() as s:
        row = s.get(RoutingDecisionRecord, second.request_id)
        assert row is not None and row.model_version == "cached/scripted-test"


@requires_db
def test_key_includes_budgets_agent_and_allowed_servers(world: dict[str, Any]) -> None:
    pipe = world["pipe"]
    pipe.route(RouteRequest("search issues", "a", 4), AllowAllScope())
    assert not pipe.route(RouteRequest("search issues", "a", 1), AllowAllScope()).cached
    assert not pipe.route(RouteRequest("search issues", "b", 4), AllowAllScope()).cached
    assert not pipe.route(
        RouteRequest("search issues", "a", 4, max_servers=1), AllowAllScope()
    ).cached
    assert not pipe.route(
        RouteRequest("search issues", "a", 4, allowed_servers=[]), AllowAllScope()
    ).cached


@requires_db
def test_cache_hit_reapplies_policy_when_rule_revoked(world: dict[str, Any]) -> None:
    """THE invariant: a grant revoked between a miss and a hit (cache key
    unchanged) must not be served from the cache."""
    pipe = world["pipe"]
    scope = MutableScope(allowed={"search_issues", "list_issues"})
    first = pipe.route(RouteRequest("search issues", "a", 4), scope)
    assert "search_issues" in _names(first)
    scope.allowed.discard("search_issues")  # revoked; fingerprint unchanged
    second = pipe.route(RouteRequest("search issues", "a", 4), scope)
    assert "search_issues" not in _names(second)
    assert not second.cached  # invalid hit discarded and recomputed


@requires_db
def test_cache_hit_rechecks_fresh_operation_against_policy(world: dict[str, Any]) -> None:
    """Real PolicyScope (read ceiling). A tool reclassified to execute with no
    generation bump (the commit->bump window) is not served from the cache."""
    pipe, db = world["pipe"], world["db"]
    principal = AgentPrincipal(agent_id="ro", key_hash="", enabled=True, max_tools=8)
    rules = [PolicyRule(id="r1", agent_id="ro", max_operation="read")]
    first = pipe.route(RouteRequest("search issues", "ro", 4), PolicyScope(principal, rules))
    assert "search_issues" in _names(first)
    with db() as s:
        s.execute(
            update(MCPToolRecord)
            .where(MCPToolRecord.name == "search_issues")
            .values(operation="execute")
        )
        s.commit()
    second = pipe.route(RouteRequest("search issues", "ro", 4), PolicyScope(principal, rules))
    assert "search_issues" not in _names(second)


@requires_db
def test_cache_hit_rechecks_eligibility(world: dict[str, Any]) -> None:
    pipe, db = world["pipe"], world["db"]
    pipe.route(RouteRequest("search issues", "a", 4), AllowAllScope())
    with db() as s:
        s.execute(
            update(MCPToolRecord).where(MCPToolRecord.name == "search_issues").values(enabled=False)
        )
        s.execute(update(MCPServerRecord).values(status="healthy"))
        s.commit()
    assert "search_issues" not in _names(
        pipe.route(RouteRequest("search issues", "a", 4), AllowAllScope())
    )


@requires_db
def test_cache_hit_rechecks_offline_server(world: dict[str, Any]) -> None:
    pipe, db = world["pipe"], world["db"]
    assert pipe.route(RouteRequest("search issues", "a", 4), AllowAllScope()).tools
    with db() as s:
        s.execute(update(MCPServerRecord).values(status="offline"))
        s.commit()
    assert not pipe.route(RouteRequest("search issues", "a", 4), AllowAllScope()).tools


@requires_db
def test_generation_bumps_invalidate(world: dict[str, Any]) -> None:
    pipe = world["pipe"]
    req = RouteRequest("search issues", "a", 4)
    pipe.route(req, AllowAllScope())
    assert pipe.route(req, AllowAllScope()).cached
    generation.bump_catalog()
    assert not pipe.route(req, AllowAllScope()).cached
    assert pipe.route(req, AllowAllScope()).cached
    generation.bump_policy()
    assert not pipe.route(req, AllowAllScope()).cached


@requires_db
def test_fallback_results_are_not_cached(world: dict[str, Any]) -> None:
    db, emb = world["db"], world["emb"]
    pipe = RoutePipeline(
        db, HybridRetriever(db, emb), ExplodingDecisionModel(), Settings(database_url=TEST_DB_URL)
    )
    req = RouteRequest("search issues", "a", 4)
    assert pipe.route(req, AllowAllScope()).fallback_used
    again = pipe.route(req, AllowAllScope())
    assert again.fallback_used and not again.cached


@requires_db
def test_scope_without_fingerprint_is_never_cached(world: dict[str, Any]) -> None:
    class Opaque:
        def server_ids(self) -> list[str] | None:
            return None

        def permits(self, candidate: ToolCandidate) -> bool:
            return True

    pipe = world["pipe"]
    pipe.route(RouteRequest("search issues", "a", 4), Opaque())
    assert not pipe.route(RouteRequest("search issues", "a", 4), Opaque()).cached


@requires_db
def test_cache_disabled_by_zero_size(world: dict[str, Any]) -> None:
    db, emb = world["db"], world["emb"]
    pipe = RoutePipeline(
        db,
        HybridRetriever(db, emb),
        ScriptedDecisionModel(),
        Settings(database_url=TEST_DB_URL, route_cache_size=0),
    )
    req = RouteRequest("search issues", "a", 4)
    pipe.route(req, AllowAllScope())
    assert not pipe.route(req, AllowAllScope()).cached


def test_policy_scope_fingerprint_tracks_rules_and_enabled() -> None:
    p = AgentPrincipal(agent_id="x", key_hash="", enabled=True, max_tools=8)
    r1 = PolicyRule(id="1", agent_id="x", max_operation="read")
    r2 = PolicyRule(id="1", agent_id="x", max_operation="write")
    base = PolicyScope(p, [r1]).fingerprint()
    assert base == PolicyScope(p, [r1]).fingerprint()
    assert base != PolicyScope(p, [r2]).fingerprint()
    assert base != PolicyScope(p, []).fingerprint()
    off = AgentPrincipal(agent_id="x", key_hash="", enabled=False, max_tools=8)
    assert base != PolicyScope(off, [r1]).fingerprint()


# ------------------------------------------------------ API + bump sites
@requires_db
def test_enabling_a_tool_invalidates_cached_routes_via_api(db: SF) -> None:
    """Dev mode (no keys, no admin token): /route as `dev`, admin API open."""
    app = create_app(Settings(database_url=TEST_DB_URL), env={})
    factory = app.state.session_factory
    with factory() as s:
        gh = add_server(s, "github")
        add_tool(s, gh, "search_issues", "Search issues", embedder=None)
        hidden = add_tool(s, gh, "search_code", "Search code issues", embedder=None, enabled=False)
        hidden_id = hidden.id
        s.add(PolicyRule(agent_id="dev", max_operation="read"))
        s.commit()
    c = TestClient(app)
    body = {"query": "search issues code"}
    first = c.post("/api/v1/route", json=body).json()
    assert {t["tool"] for t in first["tools"]} == {"search_issues"} and not first["cached"]
    assert c.post("/api/v1/route", json=body).json()["cached"]
    assert c.post(f"/api/v1/tools/{hidden_id}/enable").status_code == 200
    third = c.post("/api/v1/route", json=body).json()
    assert not third["cached"]
    assert {t["tool"] for t in third["tools"]} == {"search_issues", "search_code"}


@requires_db
def test_policy_routes_bump_policy_generation(db: SF) -> None:
    admin = "admin_" + "c" * 30
    app = create_app(Settings(database_url=TEST_DB_URL), env={"MCPR_ADMIN_TOKEN": admin})
    c = TestClient(app, headers={"Authorization": f"Bearer {admin}"})
    g0 = generation.policy_generation()
    pid = c.post("/api/v1/principals", json={"agentId": "gen-agent"}).json()["id"]
    rid = c.post("/api/v1/policy-rules", json={"agentId": "gen-agent"}).json()["id"]
    c.patch(f"/api/v1/policy-rules/{rid}", json={"maxOperation": "write"})
    c.patch(f"/api/v1/principals/{pid}", json={"maxTools": 3})
    c.delete(f"/api/v1/policy-rules/{rid}")
    c.delete(f"/api/v1/principals/{pid}")
    assert generation.policy_generation() == g0 + 6


@requires_db
async def test_discovery_sync_bumps_catalog_generation_only_on_change(db: SF) -> None:
    spec = generate_fleet(1)[0]
    fleet = InprocFleet([spec])
    with db() as s, s.begin():
        sid = register_server(
            s,
            ServerRegistration(
                name=spec.name, transport="streamable-http", endpoint=fleet.endpoint(spec.name)
            ),
        ).id
    svc = DiscoveryService(db, connector_factory=fleet.factory)
    g0 = generation.catalog_generation()
    await svc.sync_server(sid)  # everything added
    g1 = generation.catalog_generation()
    assert g1 > g0
    await svc.sync_server(sid)  # unchanged
    assert generation.catalog_generation() == g1
    fleet.specs[spec.name] = with_description(spec, spec.tools[0].name, "Changed.")
    await svc.sync_server(sid)
    assert generation.catalog_generation() > g1


@requires_db
def test_health_crossing_offline_bumps_catalog_generation(db: SF) -> None:
    with db() as s:
        srv = add_server(s, "flaky", status="healthy")
        sid = srv.id
        s.commit()
    svc = DiscoveryService(db)
    g0 = generation.catalog_generation()
    for _ in range(5):  # enough failures to reach offline
        svc._set_status(sid, ok=False, latency_ms=0.0)
    with db() as s:
        assert s.scalar(select(MCPServerRecord.status).where(MCPServerRecord.id == sid)) == (
            "offline"
        )
    g1 = generation.catalog_generation()
    assert g1 == g0 + 1  # one crossing, not one per failure
    svc._set_status(sid, ok=True, latency_ms=1.0)
    assert generation.catalog_generation() == g1 + 1


@requires_db
def test_cache_hit_rechecks_scope_server_ids(world: dict[str, Any]) -> None:
    """Server-level scope narrowed with the fingerprint unchanged."""

    @dataclass
    class ServerScope:
        servers: list[str] | None = None

        def server_ids(self) -> list[str] | None:
            return self.servers

        def permits(self, candidate: ToolCandidate) -> bool:
            return True

        def fingerprint(self) -> str:
            return "fixed-servers"

    pipe = world["pipe"]
    scope = ServerScope()
    assert pipe.route(RouteRequest("search issues", "a", 4), scope).tools
    scope.servers = []
    assert not pipe.route(RouteRequest("search issues", "a", 4), scope).tools


@requires_db
def test_evaluate_never_serves_from_the_route_cache(db: SF) -> None:
    """A re-run within the TTL must measure the pipeline, not the cache."""
    from mcprouter.api.routes_route import install_routing
    from mcprouter.eval.synthetic_catalog import seed_synthetic_catalog, synthetic_scope_resolver

    settings = Settings(database_url=TEST_DB_URL)
    app = create_app(settings, env={})
    factory = app.state.session_factory
    emb = FakeHashEmbedder()
    with factory() as s:
        seed_synthetic_catalog(s, emb)
        s.commit()
    pipeline = RoutePipeline(
        factory, HybridRetriever(factory, emb), ScriptedDecisionModel(), settings
    )
    install_routing(app, pipeline, scope_resolver=synthetic_scope_resolver(factory))
    c = TestClient(app)
    for _ in range(2):
        assert c.post("/api/v1/route/evaluate", json={"dataset": "synthetic_v1"}).status_code == 200
    with factory() as s:
        versions = set(s.scalars(select(RoutingDecisionRecord.model_version)))
    assert versions and not any(v.startswith("cached/") for v in versions)
