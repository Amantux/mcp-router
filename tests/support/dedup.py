"""Shared helpers moved out of tests/test_dedup.py (W0-2); not a test module."""

from __future__ import annotations

from tests.support.registry_fixtures import unit_vec

V_A = unit_vec((0, 1.0))


V_A2 = unit_vec((0, 0.99), (1, 0.14))  # cosine ~0.99 with V_A


V_FAR = unit_vec((5, 1.0))  # orthogonal
