"""Savings ESTIMATES vs MEASURED latency (docs/analytics.md).

Unconfigured rates must yield null estimates -- never a 0 that reads as measured.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.analytics.economy import SavingsBasis
from mcprouter.settings import Settings

from .conftest import requires_db
from .test_analytics_api import ADMIN, _app
from .test_analytics_support import World, build_world
from .test_execution_support import sec_db_fixture  # noqa: F401 — registers the fixture

H = {"Authorization": f"Bearer {ADMIN}"}


def test_basis_unconfigured_is_null_not_zero() -> None:
    b = SavingsBasis()
    assert b.time_saved_ms(12_345) is None
    assert b.cost_saved(12_345) is None
    # a configured rate with zero tokens IS a real 0 (distinguishable from null)
    assert SavingsBasis(prefill_ms_per_1k_tokens=1.0).time_saved_ms(0) == 0


def test_basis_exact_arithmetic() -> None:
    b = SavingsBasis(prefill_ms_per_1k_tokens=50.0, price_per_1k_input_tokens=0.003)
    assert b.time_saved_ms(2_000) == pytest.approx(100.0)
    assert b.cost_saved(2_000) == pytest.approx(0.006)


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_PREFILL_MS_PER_1K_TOKENS", "40")
    monkeypatch.setenv("MCPR_PRICE_PER_1K_INPUT_TOKENS", "0.002")
    monkeypatch.setenv("MCPR_CURRENCY", " eur ")
    s = Settings.from_env()
    assert (s.prefill_ms_per_1k_tokens, s.price_per_1k_input_tokens, s.currency) == (
        40.0,
        0.002,
        "EUR",
    )
    assert Settings().prefill_ms_per_1k_tokens == 0 and Settings().currency == "USD"


@pytest.fixture()
def world(sec_db: sessionmaker[Session]) -> Iterator[tuple[sessionmaker[Session], World]]:
    yield sec_db, build_world(sec_db)


@requires_db
def test_overview_unconfigured_nulls(world: tuple[sessionmaker[Session], World]) -> None:
    factory, _ = world
    body = TestClient(_app(factory)).get("/api/v1/analytics/overview", headers=H).json()
    e = body["contextEconomy"]
    assert e["tokensNotSent"] > 0  # there IS something to estimate from
    assert e["estimatedTimeSavedMs"] is None
    assert e["estimatedCostSaved"] is None and e["currency"] is None
    assert e["assumptions"] == {
        "prefillMsPer1kTokens": None,
        "pricePer1kInputTokens": None,
        "estimator": e["estimator"],
    }
    m = body["measured"]
    assert set(m) == {
        "routeLatencyP50Ms",
        "routeLatencyP95Ms",
        "executionLatencyP50Ms",
        "executionLatencyP95Ms",
    }
    assert m["routeLatencyP50Ms"] == body["routing"]["latencyP50Ms"]
    assert m["executionLatencyP50Ms"] is not None


@requires_db
def test_overview_and_agents_configured(world: tuple[sessionmaker[Session], World]) -> None:
    factory, _ = world
    app = _app(factory)
    app.state.settings = Settings(
        prefill_ms_per_1k_tokens=50.0, price_per_1k_input_tokens=0.003, currency="EUR"
    )
    c = TestClient(app)
    e = c.get("/api/v1/analytics/overview", headers=H).json()["contextEconomy"]
    n = e["tokensNotSent"]
    assert e["estimatedTimeSavedMs"] == pytest.approx(n * 50.0 / 1000)
    assert e["estimatedCostSaved"] == pytest.approx(n * 0.003 / 1000)
    assert e["currency"] == "EUR"
    assert e["assumptions"]["prefillMsPer1kTokens"] == 50.0
    assert e["assumptions"]["pricePer1kInputTokens"] == 0.003
    agents = c.get("/api/v1/analytics/agents", headers=H).json()
    rows = agents.get("agents", agents.get("items", []))
    assert rows
    for row in rows:
        ae = row["contextEconomy"]
        assert ae["estimatedTimeSavedMs"] == pytest.approx(ae["tokensNotSent"] * 50.0 / 1000)
