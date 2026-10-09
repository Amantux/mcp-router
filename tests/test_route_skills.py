"""S2d item 1: skills flow through the routing pipeline with their own budget
(maxSkills), never consume maxServers, carry kind, are recorded as
"skill:<id>", and are re-authorized on every route-cache hit."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.interfaces import RouteRequest, ToolCandidate
from mcprouter.models import RoutingDecisionRecord
from mcprouter.routing.pipeline import RoutePipeline
from mcprouter.routing.retriever import HybridRetriever
from mcprouter.routing.scope import AllowAllScope
from mcprouter.routing.trace import RouteTrace
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db
from .test_routing_fakes import (
    FakeHashEmbedder,
    ScriptedDecisionModel,
    add_server,
    add_skill,
    add_tool,
)

SF = sessionmaker[Session]
Q = "fill pdf form"


@dataclass
class NamedScope:
    """Fixed fingerprint while grants change (revocation the key can't see)."""

    allowed: set[str]
    max_skills: int | None = None
    calls: list[str] = field(default_factory=list)

    @property
    def _principal(self) -> Any:
        return SimpleNamespace(max_skills=self.max_skills)

    def server_ids(self) -> list[str] | None:
        return None

    def permits(self, candidate: ToolCandidate) -> bool:
        return candidate.tool_name in self.allowed

    def fingerprint(self) -> str:
        return "fixed"


ALL = {"pdf_fill", "pdf-fill", "pdf-form", "other_pdf"}


@pytest.fixture()
def world(db: SF) -> Iterator[dict[str, Any]]:
    emb = FakeHashEmbedder()
    with db() as s:
        gh = add_server(s, "docs")
        add_tool(s, gh, "pdf_fill", "fill pdf form fields", embedder=emb)
        other = add_server(s, "other")
        add_tool(s, other, "other_pdf", "fill pdf form quickly", embedder=emb)
        add_skill(s, "pdf-fill", "fill pdf form guide", embedder=emb)
        add_skill(s, "pdf-form", "pdf form fill how-to", embedder=emb)
        s.commit()
    model = ScriptedDecisionModel()
    settings = Settings(database_url=TEST_DB_URL, max_exposed_skills=3)
    pipe = RoutePipeline(db, HybridRetriever(db, emb), model, settings)
    yield {"db": db, "model": model, "pipe": pipe}


def _kinds(res: Any) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"tool": set(), "skill": set()}
    for t in res.tools:
        out[t.kind].add(t.tool_name)
    return out


@requires_db
def test_skills_are_routed_with_kind_prefixed_question_and_decision_id(
    world: dict[str, Any],
) -> None:
    pipe, model, db = world["pipe"], world["model"], world["db"]
    res = pipe.route(RouteRequest(Q, "a", 4), AllowAllScope())
    k = _kinds(res)
    assert k["skill"] == {"pdf-fill", "pdf-form"} and "pdf_fill" in k["tool"]
    assert any(str(c[2]).startswith("[skill]") for c in model.calls if c[0] == "score")
    with db() as s:
        row = s.get(RoutingDecisionRecord, res.request_id)
        assert row is not None
        skill_ids = {f"skill:{t.tool_id}" for t in res.tools if t.kind == "skill"}
        tool_ids = {t.tool_id for t in res.tools if t.kind == "tool"}
        assert set(row.selected_tool_ids) == skill_ids | tool_ids


@requires_db
def test_request_may_lower_never_raise_the_skills_budget(world: dict[str, Any]) -> None:
    pipe = world["pipe"]
    scope = NamedScope(allowed=ALL, max_skills=1)
    assert pipe.skills_budget(RouteRequest(Q, "a", 4, max_skills=5), scope).applied == 1
    res = pipe.route(RouteRequest(Q, "a", 4, max_skills=5), scope)
    assert len(_kinds(res)["skill"]) == 1
    res0 = pipe.route(RouteRequest(Q, "b", 4, max_skills=0), NamedScope(allowed=ALL, max_skills=2))
    assert _kinds(res0)["skill"] == set() and _kinds(res0)["tool"]


@requires_db
def test_skills_do_not_consume_server_budget(world: dict[str, Any]) -> None:
    res = world["pipe"].route(RouteRequest(Q, "a", 4, max_servers=1), AllowAllScope())
    k = _kinds(res)
    assert len(k["skill"]) == 2  # two distinct sources, yet maxServers=1
    assert len(k["tool"]) == 1  # tools still limited to one MCP server


@requires_db
def test_kinds_filter_narrows(world: dict[str, Any]) -> None:
    res = world["pipe"].route(RouteRequest(Q, "a", 4, kinds=("tool",)), AllowAllScope())
    assert _kinds(res)["skill"] == set() and _kinds(res)["tool"]


@requires_db
def test_cache_key_includes_max_skills_and_kinds(world: dict[str, Any]) -> None:
    pipe = world["pipe"]
    pipe.route(RouteRequest(Q, "a", 4), AllowAllScope())
    assert pipe.route(RouteRequest(Q, "a", 4), AllowAllScope()).cached
    assert not pipe.route(RouteRequest(Q, "a", 4, max_skills=1), AllowAllScope()).cached
    assert not pipe.route(RouteRequest(Q, "a", 4, kinds=("tool",)), AllowAllScope()).cached


@requires_db
def test_cache_hit_drops_revoked_skill(world: dict[str, Any]) -> None:
    pipe = world["pipe"]
    scope = NamedScope(allowed=set(ALL))
    first = pipe.route(RouteRequest(Q, "a", 4), scope)
    assert "pdf-fill" in _kinds(first)["skill"]
    scope.allowed.discard("pdf-fill")  # skill rule revoked; fingerprint unchanged
    seen: list[Any] = []
    real = pipe._revalidate

    def spy(*a: Any) -> Any:
        seen.append(out := real(*a))
        return out

    pipe._revalidate = spy
    second = pipe.route(RouteRequest(Q, "a", 4), scope)
    # The second route HIT the cache and revalidation rejected it (a miss would
    # never call _revalidate, so this cannot pass by recomputing from scratch).
    assert seen == [None]
    assert "pdf-fill" not in _kinds(second)["skill"]


@requires_db
def test_trace_records_per_kind_counts_and_skills_clamp(world: dict[str, Any]) -> None:
    trace = RouteTrace()
    world["pipe"].route(
        RouteRequest(Q, "a", 4, max_skills=1), NamedScope(allowed=ALL, max_skills=2), trace=trace
    )
    retrieval = next(st for st in trace.stages if st.stage == "retrieval")
    assert retrieval.detail["afterSkills"] == 2 and retrieval.detail["afterTools"] == 2
    skills = next(st for st in trace.stages if st.stage == "maxSkills")
    assert (skills.before, skills.after, skills.detail["limit"]) == (2, 1, 1)
    assert trace.skill_budget is not None and trace.skill_budget.applied == 1


def _policy_scope(max_skills: int) -> Any:
    from mcprouter.models import AgentPrincipal, PolicyRule
    from mcprouter.policy.scope import PolicyScope

    principal = AgentPrincipal(
        id="p1", agent_id="a", key_hash="h", enabled=True, max_tools=8, max_skills=max_skills
    )
    rules = [
        PolicyRule(id="r1", agent_id="a", server_id=None, max_operation="execute"),
        PolicyRule(
            id="r2",
            agent_id="a",
            server_id=None,
            max_operation="execute",
            resource_kind="skill",
        ),
    ]
    return PolicyScope(principal, rules)


@requires_db
@pytest.mark.parametrize("cap", [0, 1])
def test_uncached_scope_keeps_the_principal_skills_budget(world: dict[str, Any], cap: int) -> None:
    from mcprouter.routing.scope import UncachedScope

    pipe = world["pipe"]
    direct = pipe.route(RouteRequest(Q, "a", 4), _policy_scope(cap))
    wrapped = pipe.route(RouteRequest(Q, "a", 4), UncachedScope(_policy_scope(cap)))
    assert len(_kinds(direct)["skill"]) == cap
    assert len(_kinds(wrapped)["skill"]) == cap  # was the global cap (3) before
    assert (
        pipe.skills_budget(RouteRequest(Q, "a", 4), UncachedScope(_policy_scope(cap))).applied
        == cap
    )


@dataclass
class OpaqueScope:
    """A ScopeFilter that cannot say what its principal may see."""

    def server_ids(self) -> list[str] | None:
        return None

    def permits(self, candidate: ToolCandidate) -> bool:
        return True


@requires_db
def test_undeterminable_principal_budget_fails_closed(world: dict[str, Any]) -> None:
    res = world["pipe"].route(RouteRequest(Q, "a", 4), OpaqueScope())
    assert _kinds(res)["skill"] == set()  # never the global cap
    assert _kinds(res)["tool"]  # tools still route


def _stage(trace: RouteTrace, name: str) -> Any:
    return next(st for st in trace.stages if st.stage == name)


def _assert_kind_keyed(trace: RouteTrace, scores: dict[str, float]) -> None:
    skill_ids = {c.tool_id for c in trace.candidates if c.kind == "skill"}
    assert skill_ids  # the fixture's skills reached the scored set
    assert all(f"skill:{sid}" in scores and sid not in scores for sid in skill_ids)


@requires_db
def test_trace_score_keys_match_decision_ids(world: dict[str, Any]) -> None:
    trace = RouteTrace()
    world["pipe"].route(RouteRequest(Q, "a", 4), AllowAllScope(), trace=trace)
    scores = _stage(trace, "score").detail["scores"]
    _assert_kind_keyed(trace, scores)


@requires_db
def test_fallback_trace_score_keys_match_decision_ids(world: dict[str, Any]) -> None:
    def boom(*_: Any) -> Any:
        raise RuntimeError("model down")

    world["model"].score_fn = boom
    world["model"].choice_fn = boom
    trace = RouteTrace()
    res = world["pipe"].route(RouteRequest(Q, "a", 4), AllowAllScope(), trace=trace)
    assert res.fallback_used
    scores = _stage(trace, "fallback").detail["scores"]
    _assert_kind_keyed(trace, scores)


@dataclass
class KindsSpy:
    inner: Any
    kinds: list[tuple[str, ...]] = field(default_factory=list)

    def retrieve(self, query: str, **kw: Any) -> Any:
        self.kinds.append(tuple(kw.get("kinds", ())))
        return self.inner.retrieve(query, **kw)


@requires_db
def test_zero_skills_budget_does_not_retrieve_skills(world: dict[str, Any]) -> None:
    pipe = world["pipe"]
    spy = KindsSpy(pipe._retriever)
    pipe._retriever = spy
    res = pipe.route(RouteRequest(Q, "a", 4), NamedScope(allowed=set(ALL), max_skills=0))
    assert spy.kinds == [("tool",)]
    assert _kinds(res)["skill"] == set()
    assert not any("skill" in str(c[3]) for c in world["model"].calls)
