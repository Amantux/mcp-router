"""PolicyScope exposes principal_max_skills() publicly (B5)."""

from __future__ import annotations

from types import SimpleNamespace

from mcprouter.policy.scope import PolicyScope
from mcprouter.routing.scope import principal_skill_cap


def _scope(v: object) -> PolicyScope:
    p = SimpleNamespace(agent_id="a", enabled=True, max_skills=v)
    return PolicyScope(p, [])  # type: ignore[arg-type]


def test_public_cap_and_fail_closed() -> None:
    assert _scope(3).principal_max_skills() == 3
    assert principal_skill_cap(_scope(None)) is None
    assert principal_skill_cap(_scope("x")) == 0
    assert principal_skill_cap(SimpleNamespace(_principal=SimpleNamespace(max_skills=9))) == 0
