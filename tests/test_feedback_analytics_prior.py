"""usage_prior feedback blend: bounded by the same cap; no feedback = unchanged."""

from __future__ import annotations

import math

import pytest

from mcprouter.analytics.prior import DEFAULT_CAP, usage_prior

GRID = [0.0, 1.0, 3.0, 50.0, 1e6, -5.0, math.nan, math.inf]


@pytest.mark.parametrize("cap", [DEFAULT_CAP, 0.2, 1.0])
def test_blended_prior_bounded(cap: float) -> None:
    for sel in GRID:
        for surf in GRID:
            for hr in [None, 0.0, 0.5, 1.0, -3.0, 7.0, math.nan]:
                p = usage_prior(sel, surf, enabled=True, cap=cap, helpful_rate=hr)
                assert 0.0 <= p <= cap


def test_no_feedback_unchanged_and_cold_zero() -> None:
    for sel, surf in [(3, 10), (0, 5), (10, 10)]:
        assert usage_prior(sel, surf, enabled=True) == usage_prior(
            sel, surf, enabled=True, helpful_rate=None
        )
    assert usage_prior(0, 0, enabled=True, helpful_rate=1.0) == 0.0
    assert usage_prior(5, 10, enabled=False, helpful_rate=1.0) == 0.0


def test_feedback_moves_prior_both_ways_within_cap() -> None:
    base = usage_prior(5, 50, enabled=True)
    good = usage_prior(5, 50, enabled=True, helpful_rate=1.0)
    bad = usage_prior(5, 50, enabled=True, helpful_rate=0.0)
    assert bad <= base < good <= DEFAULT_CAP
    # feedback-only signal (never selected): bounded by the feedback weight
    only = usage_prior(0, 200, enabled=True, helpful_rate=1.0)
    assert 0.0 < only <= DEFAULT_CAP


def test_out_of_range_helpful_rate_is_clamped_like_its_bound() -> None:
    assert usage_prior(5, 50, enabled=True, helpful_rate=7.0) == usage_prior(
        5, 50, enabled=True, helpful_rate=1.0
    )
    assert usage_prior(50, 50, enabled=True, helpful_rate=-3.0) == usage_prior(
        50, 50, enabled=True, helpful_rate=0.0
    )
