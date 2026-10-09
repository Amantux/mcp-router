"""Wave-4 S2g: synthetic_v1 skill + mixed cases are well-formed against the
seeded fixture (pure — no DB)."""

from __future__ import annotations

from mcprouter.eval.dataset import EvalCase, load_named
from mcprouter.eval.synthetic_catalog import CATALOG, NEAR_DUPLICATE_SKILLS, SKILLS

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
