"""Wave-4 S2g: synthetic_v1 skill + mixed cases are well-formed against the
seeded fixture (pure — no DB)."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

import pytest

from mcprouter.eval import runner
from mcprouter.eval.dataset import EvalCase, load_named
from mcprouter.eval.synthetic_catalog import CATALOG, NEAR_DUPLICATE_SKILLS, SKILLS
from mcprouter.interfaces import RoutedTool, RouteResult
from mcprouter.routing.scope import StaticScope

_TOOLS = {f"{srv}/{t[0]}" for srv, (_, _, _, tools) in CATALOG.items() for t in tools}
_SKILLS = {sk.ref: sk for sk in SKILLS}


def _cases() -> list[EvalCase]:
    return load_named("synthetic_v1")


def test_every_referenced_tool_and_skill_exists_in_the_fixture() -> None:
    for c in _cases():
        assert set(c.expected_tools) | set(c.forbidden_tools) <= _TOOLS, c.id
        refs = set(c.expected_skills) | set(c.forbidden_skills)
        assert all("/" in r for r in refs), f"{c.id}: skill refs must be source/name"
        assert refs <= set(_SKILLS), c.id
        for srv in c.allowed_servers or ():
            assert srv in CATALOG, c.id


def test_case_counts_by_class() -> None:
    cases = _cases()
    skill_only = [c for c in cases if c.kinds == ("skill",)]
    mixed = [c for c in cases if c.expected_tools and c.expected_skills]
    assert len(skill_only) >= 20
    assert len(mixed) >= 10
    assert sum(len(c.expected_skills) == 2 for c in skill_only) >= 4  # ambiguous by design
    assert sum(bool(c.forbidden_skills) for c in skill_only) >= 4
    assert len({c.id for c in cases}) == len(cases)


def test_ambiguous_skill_cases_name_a_near_duplicate_pair() -> None:
    pairs = {frozenset(p) for p in NEAR_DUPLICATE_SKILLS}
    for c in _cases():
        if c.category == "skill_ambiguous":
            assert frozenset(c.expected_skills) in pairs, c.id


def test_forbidden_skills_are_outside_the_agents_scope() -> None:
    """readonly-agent has a read ceiling: everything it is forbidden is a
    write/execute skill, and nothing it is expected to get is."""
    for c in _cases():
        if c.agent_id == "readonly-agent" and "skill" in c.kinds:
            assert all(_SKILLS[r].operation != "read" for r in c.forbidden_skills), c.id
            assert all(_SKILLS[r].operation == "read" for r in c.expected_skills), c.id


# ------------------------------------------------- runner metrics, fake route
_WRONG_SKILL = "community-skills/csv-cleanup"
_WRONG_TOOL = "shell/run_command"


class _FakeRoute:
    """Stands in for RoutePipeline: answers each query from its case with
    MIXED kinds (a skill ranked before the tool, so kinds must be scored
    separately), with scripted misses/leaks the metrics must pick up."""

    def __init__(self, cases: list[EvalCase]) -> None:
        self.by_query = {c.query: c for c in cases}

    def route(self, req: Any, scope: Any) -> RouteResult:
        c = self.by_query[req.query]
        skills = list(c.expected_skills)
        tools = list(c.expected_tools[:1])
        if c.id in ("s01", "s02"):  # top-1 miss, still recalled at rank 2
            skills = [_WRONG_SKILL, *skills]
        elif c.id in ("s03", "mx03"):  # no skill at all
            skills = []
        elif c.id == "s12":  # ambiguous pair, only one of two recalled
            skills = skills[:1]
        elif c.id == "s16":  # readonly-agent shown an execute skill
            skills = list(c.forbidden_skills)
        if c.id in ("mx01", "mx02"):  # right skill, wrong tool first
            tools = [_WRONG_TOOL, *tools]
        routed = [_rt(r, "skill") for r in skills[:1]]
        routed += [_rt(r, "tool") for r in tools] + [_rt(r, "skill") for r in skills[1:]]
        return RouteResult("rid", routed, False, 1.0, "fake")


def _rt(ref: str, kind: str) -> RoutedTool:
    container, _, name = ref.partition("/")
    return RoutedTool(f"id-{ref}", container, name, 0.9, kind=kind)


def test_runner_skill_and_mixed_metrics_from_new_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    cases = [c for c in _cases() if "skill" in c.kinds]
    monkeypatch.setattr(runner, "resolve_server_names", lambda s, names: (names, []))
    outcomes = runner.run_cases(
        _FakeRoute(cases),  # type: ignore[arg-type]
        cases,
        session_factory=lambda: nullcontext(None),  # type: ignore[arg-type,return-value]
        scope_resolver=lambda agent_id: StaticScope(),
    )
    m = runner.compute_metrics(outcomes)
    # 32 positive skill cases (20 skill-only + 12 mixed); misses s01 s02 s03 mx03.
    assert m["skills"]["positive_cases"] == 32
    assert m["skills"]["top1_accuracy"] == pytest.approx(28 / 32)
    # recall: s03 and mx03 contribute 0, s12 contributes 0.5.
    assert m["skills"]["top5_recall"] == pytest.approx(29.5 / 32)
    assert m["skills"]["unauthorized_skill_exposures"] == 1
    # mixed: mx01/mx02 wrong tool first, mx03 no skill.
    assert m["mixed"] == {"cases": 12, "both_top1_rate": pytest.approx(9 / 12)}
    # A skill ranked above the tool is not a "wrong tool": only mx01/mx02 miss.
    assert m["top1_accuracy"] == pytest.approx(10 / 12)
