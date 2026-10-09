"""Analytics REST: admin auth on every route, exact wire shapes, rollup
endpoint, Prometheus collector idempotency."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.analytics.window import live_horizon
from mcprouter.api import deps_auth
from mcprouter.api.deps_auth import configure_security
from mcprouter.api.routes_analytics import get_now, install_analytics
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db
from .test_analytics_support import NOW, World, add_decision, build_world
from .test_execution_support import KEYS, sec_db_fixture  # noqa: F401 — registers the fixture

pytestmark = requires_db

ADMIN = "admin_" + "z" * 40
H_ADMIN = {"Authorization": f"Bearer {ADMIN}"}

ROUTES = [
    ("GET", "/api/v1/analytics/overview"),
    ("GET", "/api/v1/analytics/tools"),
    ("GET", "/api/v1/analytics/tools/some-id"),
    ("GET", "/api/v1/analytics/agents"),
    ("GET", "/api/v1/analytics/suggestions"),
    ("POST", "/api/v1/analytics/rollup"),
]


def _app(factory: sessionmaker[Session]) -> FastAPI:
    deps_auth._reset_dev_warning_for_tests()
    app = FastAPI()
    app.state.settings = Settings(database_url=TEST_DB_URL)
    app.state.engine = factory.kw["bind"]
    app.state.session_factory = factory
    configure_security(app, {"MCPR_ADMIN_TOKEN": ADMIN})
    install_analytics(app)
    app.dependency_overrides[get_now] = lambda: NOW
    return app


@pytest.fixture()
def env(sec_db: sessionmaker[Session]) -> Iterator[tuple[TestClient, World]]:
    world = build_world(sec_db)
    yield TestClient(_app(sec_db)), world


# ---------------------------------------------------------------- auth
@pytest.mark.parametrize("method,path", ROUTES)
def test_analytics_routes_require_admin(
    env: tuple[TestClient, World], method: str, path: str
) -> None:
    """Mutation target: the router-level Depends(require_admin)."""
    c, _ = env
    assert c.request(method, path).status_code == 401
    agent = {"Authorization": f"Bearer {KEYS['alice']}"}
    assert c.request(method, path, headers=agent).status_code == 401
    assert c.request(method, path, headers=H_ADMIN).status_code in (200, 404)


# ---------------------------------------------------------------- shapes
def test_overview_wire_shape(env: tuple[TestClient, World]) -> None:
    c, _ = env
    r = c.get("/api/v1/analytics/overview", headers=H_ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "window",
        "contextEconomy",
        "funnel",
        "routing",
        "executions",
        "positionCurve",
        "catalogDrift",
        "skills",
    }
    assert set(body["window"]) == {"label", "start", "end"} and body["window"]["label"] == "7d"
    assert set(body["contextEconomy"]) == {
        "servedDecisions",
        "unscoredDecisions",
        "staleRefDecisions",
        "noMatchDecisions",
        "exposedTokens",
        "catalogTokens",
        "tokensNotSent",
        "savings",
        "catalogTokensPerDecision",
        "skillMetadataTokens",
        "skillBodyTokensExposed",
        "skillBodyTokensNotSent",
        "estimator",
        "catalogBasis",
    }
    assert body["contextEconomy"]["catalogTokensPerDecision"] is None  # per-agent only
    assert body["funnel"] == {
        "surfaced": 6,
        "selected": 3,
        "succeeded": 1,
        "failed": 1,
        "selectionRate": 0.5,
        "successRate": pytest.approx(1 / 3),
    }
    assert set(body["routing"]) == {
        "decisions",
        "noMatch",
        "noMatchRate",
        "fallback",
        "fallbackRate",
        "latencyP50Ms",
        "latencyP95Ms",
    }
    assert body["executions"] == {
        "attempts": 6,
        "denied": 1,
        "denialRate": pytest.approx(1 / 6),
        "attributed": 5,
        "attributionCoverage": pytest.approx(5 / 6),
        "offFunnelSelections": 1,
    }
    assert body["positionCurve"][0] == {
        "rank": 1,
        "shown": 3,
        "selected": 2,
        "rate": pytest.approx(2 / 3),
    }
    assert set(body["catalogDrift"]) == {"added", "schema", "metadata", "removed", "restored"}
    assert set(body["skills"]) == {"surfaced", "activated", "activationRate", "bodyTokensNotSent"}


def test_tools_table_sort_and_zero_rows(env: tuple[TestClient, World]) -> None:
    c, w = env
    r = c.get("/api/v1/analytics/tools?sort=surfaced&order=desc", headers=H_ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4  # D included with zeros
    ids = [i["toolId"] for i in body["items"]]
    assert ids[:3] == [w.A, w.B, w.C] and ids[3] == w.D
    assert set(body["items"][0]) == {
        "toolId",
        "kind",
        "toolName",
        "serverName",
        "enabled",
        "tokens",
        "surfaced",
        "selected",
        "succeeded",
        "failed",
        "selectionRate",
        "successRate",
        "avgRank",
        "exposedTokens",
    }
    d = body["items"][3]
    assert d["surfaced"] == 0 and d["selectionRate"] is None and d["avgRank"] is None
    # None sorts last in BOTH directions
    asc = c.get("/api/v1/analytics/tools?sort=selectionRate&order=asc", headers=H_ADMIN).json()
    assert asc["items"][-1]["toolId"] == w.D and asc["items"][0]["toolId"] == w.A
    page = c.get("/api/v1/analytics/tools?limit=1&offset=1", headers=H_ADMIN).json()
    assert [i["toolId"] for i in page["items"]] == [w.B]


def test_tool_detail_and_404(env: tuple[TestClient, World]) -> None:
    c, w = env
    r = c.get(f"/api/v1/analytics/tools/{w.A}", headers=H_ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"window", "tool", "positionCurve", "coSurfaced"}
    assert body["tool"]["surfaced"] == 3
    assert {x["toolId"] for x in body["coSurfaced"]} == {w.B, w.C}
    assert set(body["coSurfaced"][0]) == {
        "toolId",
        "toolName",
        "serverName",
        "coSurfaced",
        "thisSelected",
        "otherSelected",
        "kind",
    }
    missing = c.get("/api/v1/analytics/tools/nope", headers=H_ADMIN)
    assert missing.status_code == 404 and missing.json() == {"detail": "Unknown tool."}


def test_agents_shape(env: tuple[TestClient, World]) -> None:
    c, _ = env
    items = c.get("/api/v1/analytics/agents", headers=H_ADMIN).json()["items"]
    assert [i["agentId"] for i in items] == ["alice", "bob"]
    alice = items[0]
    assert alice["budgetTools"] is None and alice["budgetUtilization"] is None
    assert alice["contextEconomy"]["catalogTokensPerDecision"] > 0
    assert set(alice) >= {
        "noMatchRate",
        "fallbackRate",
        "denialRate",
        "attributionCoverage",
        "avgSurfacedPerDecision",
        "latencyP50Ms",
        "latencyP95Ms",
    }


def test_suggestions_shape(env: tuple[TestClient, World]) -> None:
    c, w = env
    r = c.get(
        "/api/v1/analytics/suggestions?minSurfaced=2&maxSelectionRate=0.4&staleDays=30",
        headers=H_ADMIN,
    )
    assert r.status_code == 200
    body = r.json()
    assert [x["toolId"] for x in body["wastedExposure"]] == [w.A]
    assert set(body) == {
        "window",
        "minSurfaced",
        "maxSelectionRate",
        "staleDays",
        "wastedExposure",
        "staleTools",
        "neverRoutedServers",
    }


@pytest.mark.parametrize(
    "query", ["window=0d", "window=7w", "window=366d", "window=x", "sort=bogus", "limit=0"]
)
def test_bad_params_are_422(env: tuple[TestClient, World], query: str) -> None:
    c, _ = env
    assert c.get(f"/api/v1/analytics/tools?{query}", headers=H_ADMIN).status_code == 422


# ---------------------------------------------------------------- rollup
def test_rollup_endpoint(env: tuple[TestClient, World]) -> None:
    c, w = env
    old = NOW - timedelta(days=4)
    add_decision(c.app.state.session_factory, "alice", old, [w.A])  # type: ignore[attr-defined]
    r = c.post("/api/v1/analytics/rollup", headers=H_ADMIN, json={"days": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["liveHorizon"] == live_horizon(NOW).isoformat()
    assert [d["day"] for d in body["days"]] == [
        (live_horizon(NOW) - timedelta(days=i)).isoformat() for i in (3, 2, 1)
    ]
    assert sum(d["toolRows"] for d in body["days"]) == 1
    again = c.post("/api/v1/analytics/rollup", headers=H_ADMIN, json={"days": 3}).json()
    assert again == body
    refused = c.post(
        "/api/v1/analytics/rollup", headers=H_ADMIN, json={"day": NOW.date().isoformat()}
    )
    assert refused.status_code == 400
    assert "live window" in refused.json()["detail"]
    assert c.post("/api/v1/analytics/rollup", headers=H_ADMIN, json={"bogus": 1}).status_code == 422


# ---------------------------------------------------------------- metrics
def _sample(name: str) -> float:
    for line in generate_latest(REGISTRY).decode().splitlines():
        if line.startswith(name + " "):
            return float(line.split()[1])
    raise AssertionError(f"{name} not exposed")


def test_prometheus_collector_registers_once_and_reads_db(
    sec_db: sessionmaker[Session],
) -> None:
    from mcprouter.analytics.metrics import install_metrics

    build_world(sec_db)
    _app(sec_db)
    _app(sec_db)  # re-created app: must not raise "Duplicated timeseries"
    install_metrics(sec_db).refresh_now()
    assert _sample("mcpr_analytics_tools_surfaced_total") == 6
    assert _sample("mcpr_analytics_tools_selected_total") == 3
    assert _sample("mcpr_analytics_tools_succeeded_total") == 1


def test_window_is_echoed(env: tuple[TestClient, World]) -> None:
    c, _ = env
    body = c.get("/api/v1/analytics/agents?window=24h", headers=H_ADMIN).json()
    start = datetime.fromisoformat(body["window"]["start"])
    assert NOW - start == timedelta(hours=24)


def test_install_on_the_real_app_factory(sec_db: sessionmaker[Session]) -> None:
    """The one-line integration (`install_analytics(app)` after create_app's
    session factory exists) works on the real app; admin-gated like the rest."""
    from mcprouter.analytics.metrics import install_metrics
    from mcprouter.api.app import create_app

    app = create_app(Settings(database_url=TEST_DB_URL), env={"MCPR_ADMIN_TOKEN": ADMIN})
    install_analytics(app)
    with TestClient(app) as c:
        assert c.get("/api/v1/analytics/overview").status_code == 401
        r = c.get("/api/v1/analytics/overview?window=24h", headers=H_ADMIN)
        assert r.status_code == 200 and r.json()["routing"]["decisions"] == 0
        install_metrics(app.state.session_factory).refresh_now()
        assert "mcpr_analytics_tools_surfaced_total" in c.get("/metrics/").text


def test_collect_never_queries_the_db_on_the_calling_thread() -> None:
    """prometheus_client's ASGI app collects ON THE EVENT LOOP; collect() must
    only read the snapshot and hand the refresh to a background thread."""
    import threading

    from mcprouter.analytics.metrics import FunnelCollector

    callers: list[int] = []
    done = threading.Event()

    class _Factory:
        def __call__(self) -> Session:
            callers.append(threading.get_ident())
            done.set()
            raise RuntimeError("db down")

    col = FunnelCollector()
    col.bind(_Factory())  # type: ignore[arg-type]
    assert list(col.collect()) == []  # no snapshot yet, no blocking query
    assert done.wait(5)
    assert callers and threading.get_ident() not in callers


def test_rollup_rejects_absurd_day(env: tuple[TestClient, World]) -> None:
    c, _ = env
    r = c.post("/api/v1/analytics/rollup", headers=H_ADMIN, json={"day": "0001-01-01", "days": 2})
    assert r.status_code == 422
