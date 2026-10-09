"""Shared helpers moved out of tests/test_gateway_skills.py (W0-2); not a test module."""

from __future__ import annotations

from mcprouter.interfaces import RoutedTool, RouteResult


def _route(rid: str, skills: list[str]) -> RouteResult:
    tools = [RoutedTool(s, "", "", 1.0, kind="skill") for s in skills]
    return RouteResult(rid, tools, False, 1.0, "m")
