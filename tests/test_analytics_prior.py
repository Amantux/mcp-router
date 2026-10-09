"""Usage prior bounds (pure; no DB)."""

from __future__ import annotations

import itertools
import math

import pytest

from mcprouter.analytics.prior import DEFAULT_CAP, usage_prior
from mcprouter.analytics.window import InvalidWindow, live_horizon, parse_window
from mcprouter.settings import Settings

_VALUES = [0, 1, 2, 5, 10, 199, 200, 201, 10_000, -1, -100, 0.5, math.nan, math.inf, -math.inf]


@pytest.mark.parametrize("cap", [DEFAULT_CAP, 0.2, 1.0])
def test_prior_never_exceeds_cap_and_never_negative(cap: float) -> None:
    for selected, surfaced in itertools.product(_VALUES, _VALUES):
        p = usage_prior(selected, surfaced, enabled=True, cap=cap)
        assert 0.0 <= p <= cap, (selected, surfaced, p)
        assert not math.isnan(p)


def test_cold_tool_gets_exactly_zero() -> None:
    assert usage_prior(0, 0, enabled=True) == 0.0
    assert usage_prior(5, 0, enabled=True) == 0.0  # inconsistent input, still 0
    assert usage_prior(0, 50, enabled=True) == 0.0  # surfaced, never picked


def test_disabled_is_exactly_zero() -> None:
    assert usage_prior(100, 100, enabled=False) == 0.0


def test_prior_reaches_cap_only_at_saturation_and_is_monotone_in_evidence() -> None:
    assert usage_prior(200, 200, enabled=True) == pytest.approx(DEFAULT_CAP)
    assert usage_prior(10_000, 10_000, enabled=True) == pytest.approx(DEFAULT_CAP)
    few = usage_prior(2, 2, enabled=True)
    many = usage_prior(100, 100, enabled=True)
    assert 0.0 < few < many < DEFAULT_CAP


def test_bad_cap_is_zero() -> None:
    assert usage_prior(10, 10, enabled=True, cap=-1.0) == 0.0
    assert usage_prior(10, 10, enabled=True, cap=math.nan) == 0.0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", False),
        ("  ", False),
        ("0", False),
        ("false", False),
        ("1", True),
        ("TRUE", True),
        ("yes", True),
        (" on ", True),
    ],
)
def test_prior_enabled_env(raw: str, expected: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Promoted to Settings (integration): one strict parser, empty = unset."""
    monkeypatch.setenv("MCPR_USAGE_PRIOR_ENABLED", raw)
    assert Settings.from_env().usage_prior_enabled is expected
    monkeypatch.delenv("MCPR_USAGE_PRIOR_ENABLED")
    assert Settings.from_env().usage_prior_enabled is False
    monkeypatch.setenv("MCPR_USAGE_PRIOR_ENABLED", "maybe")
    with pytest.raises(ValueError):
        Settings.from_env()


# ------------------------------------------------------------------ windows
def test_window_parsing() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 10, 8, 12, tzinfo=UTC)
    assert parse_window("24h", now).start == now - timedelta(hours=24)
    assert parse_window("7d", now).start == now - timedelta(days=7)
    for bad in ["0d", "7w", "d", "-1d", "366d", "1d;", "10000d"]:
        with pytest.raises(InvalidWindow):
            parse_window(bad, now)
    assert live_horizon(now).isoformat() == "2026-10-06"
