"""Hierarchical routing pipeline (FR-03/FR-04/FR-06) against scripted models."""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.interfaces import ChoiceResult, RouteRequest, ScopeFilter, ScoreResult, ToolCandidate
from mcprouter.models import RoutingDecisionRecord
from mcprouter.routing.pipeline import RELEVANCE_LEVELS, RoutePipeline
from mcprouter.routing.retriever import HybridRetriever
from mcprouter.routing.scope import AllowAllScope
from mcprouter.settings import Settings

from .conftest import requires_db
from .test_routing_fakes import (
    ExplodingDecisionModel,
    FakeHashEmbedder,
    ScriptedDecisionModel,
    add_server,
    add_tool,
    pick,
)

pytestmark = requires_db


@pytest.fixture()
def catalog(db: sessionmaker[Session]) -> dict[str, str]:
    emb = FakeHashEmbedder()
    ids: dict[str, str] = {}
    with db() as s:
        gh = add_server(s, "github")
        slack = add_server(s, "slack")
        fs = add_server(s, "filesystem")
        for srv, name, desc, dom, op in [
            (gh, "search_issues", "Search issues in a repository", "development", "read"),
            (gh, "create_issue", "Create a new issue in a repository", "development", "write"),
            (gh, "close_issue", "Close an issue in a repository", "development", "write"),
            (
                slack,
                "send_message",
                "Send a message to a channel about issues",
                "communication",
                "write",
            ),
            (slack, "search_messages", "Search messages in channels", "communication", "read"),
            (fs, "read_file", "Read a file from disk", "files", "read"),
            (fs, "delete_file", "Delete a file from disk", "files", "write"),
        ]:
            ids[name] = add_tool(s, srv, name, desc, embedder=emb, domain=dom, operation=op).id
        ids["github"], ids["slack"], ids["filesystem"] = gh.id, slack.id, fs.id
        s.commit()
    return ids


def _pipeline(db: sessionmaker[Session], model: object, **settings_kw: object) -> RoutePipeline:
    settings = replace(Settings(), **settings_kw)  # type: ignore[arg-type]
    return RoutePipeline(db, HybridRetriever(db, FakeHashEmbedder()), model, settings)  # type: ignore[arg-type]


def _req(q: str, max_tools: int = 5, allowed: list[str] | None = None) -> RouteRequest:
    return RouteRequest(query=q, agent_id="agent-1", max_tools=max_tools, allowed_servers=allowed)


def _prefer(tool: str, level: int = 4) -> object:
    """Score fn: `tool` gets `level`, everything else level 0."""

    def fn(state: str, question: str, levels: list[str]) -> ScoreResult:
        lv = level if f"/{tool}\n" in state else 0
        probs = [0.0] * len(levels)
        probs[lv] = 1.0
        return ScoreResult(level=lv, probabilities=probs)

    return fn


def test_domain_choice_only_over_present_domains_and_prunes(
    db: sessionmaker[Session], catalog: dict[str, str]
) -> None:
    model = ScriptedDecisionModel(choice_fn=pick("development", p=0.95))
    res = _pipeline(db, model).route(_req("issue repository message file"), AllowAllScope())
    domain_calls = [c for c in model.calls if c[0] == "choice" and "domain" in c[2]]
    assert len(domain_calls) == 1
    # Options are the DISTINCT domains among retrieved candidates — never the catalog.
    assert set(domain_calls[0][3]) <= {"development", "communication", "files"}
    assert len(domain_calls[0][3]) == len(set(domain_calls[0][3]))
    # Pruned: only development tools were scored.
    scored = [c[1] for c in model.calls if c[0] == "score"]
    assert scored and all("github/" in st for st in scored)
    assert {t.server_name for t in res.tools} == {"github"}


def test_single_domain_skips_domain_choice(
    db: sessionmaker[Session], catalog: dict[str, str]
) -> None:
    model = ScriptedDecisionModel()
    _pipeline(db, model).route(_req("issue", allowed=[catalog["github"]]), AllowAllScope())
    assert not any(c[0] == "choice" and "domain" in c[2] for c in model.calls)


def test_wrong_operation_is_downweighted_not_dropped(
    db: sessionmaker[Session], catalog: dict[str, str]
) -> None:
    """Classifier says 'read' but the only right tool is a write tool: it must
    still be returned (soft pruning), just penalised relative to equal peers."""
    model = ScriptedDecisionModel(choice_fn=pick("read", p=0.9), score_fn=_prefer("create_issue"))
    res = _pipeline(db, model).route(
        _req("create issue repository", allowed=[catalog["github"]]), AllowAllScope()
    )
    names = [t.tool_name for t in res.tools]
    assert "create_issue" in names
    assert names[0] == "create_issue"  # relevance still dominates
    flat = ScriptedDecisionModel(choice_fn=pick("read", p=0.9))
    res2 = _pipeline(db, flat).route(
        _req("issue repository", allowed=[catalog["github"]]), AllowAllScope()
    )
    by = {t.tool_name: t.score for t in res2.tools}
    assert by["search_issues"] > by["close_issue"]  # read beats write when relevance ties


def test_score_blend_ranks_and_scores_are_bounded(
    db: sessionmaker[Session], catalog: dict[str, str]
) -> None:
    model = ScriptedDecisionModel(score_fn=_prefer("close_issue"))
    res = _pipeline(db, model).route(
        _req("issue repository", allowed=[catalog["github"]]), AllowAllScope()
    )
    assert res.tools[0].tool_name == "close_issue"
    assert all(0.0 <= t.score <= 1.0 for t in res.tools)
    assert not res.fallback_used and not res.no_match
    assert res.model_version == "scripted-test"
    levels = [c[3] for c in model.calls if c[0] == "score"][0]
    assert list(levels) == RELEVANCE_LEVELS and len(levels) == 5


def test_noul_below_confidence_floor_returns_no_match(
    db: sessionmaker[Session], catalog: dict[str, str]
) -> None:
    model = ScriptedDecisionModel(noul_fn=lambda s, q: 0.2)
    res = _pipeline(db, model, route_confidence_floor=0.35).route(
        _req("what is the weather in paris"), AllowAllScope()
    )
    assert res.no_match is True and res.tools == []
    ok = ScriptedDecisionModel(noul_fn=lambda s, q: 0.5)
    res2 = _pipeline(db, ok, route_confidence_floor=0.35).route(
        _req("search issues"), AllowAllScope()
    )
    assert res2.no_match is False and res2.tools


@pytest.mark.parametrize(("asked", "expected"), [(0, 1), (-3, 1), (2, 2), (500, 3)])
def test_max_tools_clamped(
    db: sessionmaker[Session], catalog: dict[str, str], asked: int, expected: int
) -> None:
    res = _pipeline(db, ScriptedDecisionModel(), max_exposed_tools=3).route(
        _req("issue repository message file disk channel", max_tools=asked), AllowAllScope()
    )
    assert len(res.tools) == expected


def test_exploding_model_falls_back_to_retrieval_ranking(
    db: sessionmaker[Session], catalog: dict[str, str]
) -> None:
    res = _pipeline(db, ExplodingDecisionModel()).route(_req("search issues"), AllowAllScope())
    assert res.fallback_used is True and res.no_match is False
    assert res.tools and res.tools[0].tool_name == "search_issues"
    scores = [t.score for t in res.tools]
    assert scores == sorted(scores, reverse=True)
    assert "exploding-test" in res.model_version and "fallback" in res.model_version


def test_contract_violating_model_output_falls_back(
    db: sessionmaker[Session], catalog: dict[str, str]
) -> None:
    """A model returning an option it was not offered is a model failure, not
    a routing decision: deterministic fallback, never a fabricated choice."""

    class Liar(ScriptedDecisionModel):
        def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
            return ChoiceResult(option="made_up_tool", probabilities={"made_up_tool": 1.0})

    res = _pipeline(db, Liar()).route(_req("issue repository message file"), AllowAllScope())
    assert res.fallback_used is True


@dataclass
class _DenyWrites:
    servers: list[str] | None = None

    def server_ids(self) -> list[str] | None:
        return self.servers

    def permits(self, candidate: ToolCandidate) -> bool:
        return candidate.operation == "read"


def test_scope_prefilter_hides_denied_tools_even_if_model_loves_them(
    db: sessionmaker[Session], catalog: dict[str, str]
) -> None:
    scope: ScopeFilter = _DenyWrites()
    model = ScriptedDecisionModel(score_fn=_prefer("delete_file"))
    res = _pipeline(db, model).route(_req("delete the file from disk"), scope)
    assert "delete_file" not in {t.tool_name for t in res.tools}
    # The model never even saw the denied tool.
    assert not any("/delete_file\n" in c[1] for c in model.calls)


def test_scope_server_ids_intersect_with_allowed_servers(
    db: sessionmaker[Session], catalog: dict[str, str]
) -> None:
    scope = _DenyWrites(servers=[catalog["github"]])
    res = _pipeline(db, ScriptedDecisionModel()).route(
        _req("search messages issues", allowed=[catalog["github"], catalog["slack"]]), scope
    )
    assert {t.server_name for t in res.tools} == {"github"}
    none = _pipeline(db, ScriptedDecisionModel()).route(
        _req("search messages", allowed=[catalog["slack"]]), scope
    )
    assert none.tools == [] and none.no_match is True


def test_decision_is_persisted(db: sessionmaker[Session], catalog: dict[str, str]) -> None:
    res = _pipeline(db, ScriptedDecisionModel()).route(_req("search issues"), AllowAllScope())
    with db() as s:
        rec = s.get(RoutingDecisionRecord, res.request_id)
        assert rec is not None
        assert rec.agent_id == "agent-1" and rec.query == "search issues"
        assert rec.selected_tool_ids == [t.tool_id for t in res.tools]
        assert rec.scores == {t.tool_id: t.score for t in res.tools}
        assert rec.model_version == "scripted-test" and rec.fallback_used is False
        assert rec.latency_ms == res.latency_ms > 0.0


def test_scoring_is_bounded_by_retrieval_candidates(
    db: sessionmaker[Session], catalog: dict[str, str]
) -> None:
    model = ScriptedDecisionModel()
    _pipeline(db, model, retrieval_candidates=3).route(
        _req("issue repository message file disk channel"), AllowAllScope()
    )
    assert len([c for c in model.calls if c[0] == "score"]) <= 3
