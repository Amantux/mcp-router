"""Dedup (FR-05): explainable pairwise suggestions, preferred-tool hint, and
review that only ever records a status — never disables/deletes a tool."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.dedup.detect import (
    DEFAULT_THRESHOLD,
    jaccard,
    preferred_tool,
    run_dedup,
)
from mcprouter.dedup.review import accept_suggestion, dismiss_suggestion, list_suggestions
from mcprouter.models import DuplicateSuggestion, MCPToolRecord
from mcprouter.registry.errors import InvalidArgument, InvalidTransition, SuggestionNotFound
from mcprouter.registry.schema import init_registry
from tests.support.dedup import (
    V_A,
    V_A2,
    V_FAR,
)
from tests.support.registry_fixtures import make_server, make_tool, props_schema, unit_vec

from .conftest import requires_db


# ----------------------------------------------------------------- pure
def test_jaccard() -> None:
    assert jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)
    assert jaccard(set(), set()) == 1.0  # two no-arg tools have identical input shapes
    assert jaccard({"a"}, set()) == 0.0


def _stub(**kw: object) -> MCPToolRecord:
    base: dict[str, object] = {
        "id": "x",
        "call_count": 0,
        "error_count": 0,
        "avg_latency_ms": None,
        "required_scopes": [],
    }
    base.update(kw)
    return MCPToolRecord(**base)


def test_preferred_by_success_rate_then_latency_then_scopes() -> None:
    good = _stub(id="g", call_count=100, error_count=1)
    bad = _stub(id="b", call_count=100, error_count=20)
    assert preferred_tool(good, bad)[0] == "g"
    assert preferred_tool(bad, good)[0] == "g"

    fast = _stub(id="f", call_count=50, avg_latency_ms=50.0)
    slow = _stub(id="s", call_count=50, avg_latency_ms=200.0)
    pid, why = preferred_tool(fast, slow)
    assert pid == "f" and "latency" in why

    narrow = _stub(id="n", required_scopes=["repo:read"])
    wide = _stub(id="w", required_scopes=["repo:read", "repo:write"])
    pid, why = preferred_tool(wide, narrow)
    assert pid == "n" and "scope" in why


def test_preferred_none_without_evidence() -> None:
    # Too few calls to trust the rates; no latency; equal scopes.
    a = _stub(id="a", call_count=3, error_count=0)
    b = _stub(id="b", call_count=3, error_count=3)
    assert preferred_tool(a, b) == (None, "")


# --------------------------------------------------------------- db-backed
@pytest.fixture()
def factory(db: sessionmaker[Session]) -> sessionmaker[Session]:
    init_registry(db.kw["bind"])
    return db


def _seed_pair(s: Session, **b_overrides: object) -> tuple[str, str]:
    gh = make_server(s, "github")
    gl = make_server(s, "gitlab")
    a = make_tool(
        s,
        gh,
        "list_issues",
        "List issues",
        domain="development",
        operation="read",
        input_schema=props_schema("owner", "repo"),
        embedding=V_A,
        embedding_backend="hash",
        call_count=100,
        error_count=1,
    )
    b_kw: dict[str, object] = {
        "domain": "development",
        "operation": "read",
        "input_schema": props_schema("owner", "repo"),
        "embedding": V_A2,
        "embedding_backend": "hash",
        "call_count": 100,
        "error_count": 30,
    }
    b_kw.update(b_overrides)
    b = make_tool(s, gl, "list_issues", "List project issues", **b_kw)
    s.commit()
    return a.id, b.id


@requires_db
def test_run_creates_explained_suggestion_with_preferred_tool(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        a_id, b_id = _seed_pair(s)
        result = run_dedup(s)
        s.commit()
    assert result.created == 1
    with factory() as s:
        (sug,) = s.scalars(select(DuplicateSuggestion)).all()
    assert {sug.tool_a_id, sug.tool_b_id} == {a_id, b_id}
    assert sug.tool_a_id < sug.tool_b_id  # normalised pair order
    assert sug.similarity >= DEFAULT_THRESHOLD
    assert sug.status == "open"
    assert sug.preferred_tool_id == a_id
    for needle in ("cosine", "name tokens", "input properties", "success rate"):
        assert needle in sug.rationale


@pytest.mark.parametrize(
    "override",
    [
        {"embedding": None, "embedding_backend": None},  # lacking embedding
        {"embedding_backend": "bge"},  # mismatched backends are never compared
        {"domain": "files"},  # different domain
        {"embedding": V_FAR},  # semantically unrelated
        {"operation": "execute"},  # different known execution behaviour
        {"embedding": unit_vec()},  # zero vector (cosine undefined)
    ],
)
@requires_db
def test_pairs_that_must_not_be_suggested(
    factory: sessionmaker[Session], override: dict[str, object]
) -> None:
    with factory() as s:
        _seed_pair(s, **override)
        result = run_dedup(s)
        s.commit()
        assert result.created == 0
        assert s.scalars(select(DuplicateSuggestion)).all() == []


@requires_db
def test_rerun_dedupes_open_and_respects_decided_pairs(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        _seed_pair(s)
        assert run_dedup(s).created == 1
        s.commit()
        again = run_dedup(s)
        s.commit()
    assert (again.created, again.refreshed) == (0, 1)
    with factory() as s:
        (sug,) = s.scalars(select(DuplicateSuggestion)).all()
        dismiss_suggestion(s, sug.id, justification="different APIs", actor="alice")
        s.commit()
        third = run_dedup(s)
        s.commit()
        assert (third.created, third.skipped_decided) == (0, 1)
        assert len(s.scalars(select(DuplicateSuggestion)).all()) == 1


@pytest.mark.parametrize("b_enabled", [True, False])
@requires_db
def test_accept_records_status_only_and_never_touches_tools(
    factory: sessionmaker[Session], b_enabled: bool
) -> None:
    """FR-05 guard: acceptance must never disable/delete/re-enable either tool.

    b (the NON-preferred tool) is enabled in one case — so a "disable the
    loser" bug is visible — and admin-disabled in the other, so a "re-enable"
    bug is visible too."""
    with factory() as s:
        a_id, b_id = _seed_pair(s, enabled=b_enabled)
        run_dedup(s)
        s.commit()
        (sug,) = s.scalars(select(DuplicateSuggestion)).all()
        before = {
            t.id: (t.enabled, t.available, t.updated_at) for t in s.scalars(select(MCPToolRecord))
        }
    with factory() as s:
        accept_suggestion(s, sug.id, actor="alice")
        s.commit()
    with factory() as s:
        after = {
            t.id: (t.enabled, t.available, t.updated_at) for t in s.scalars(select(MCPToolRecord))
        }
        assert after == before
        assert after[a_id][0] is True and after[b_id][0] is b_enabled
        assert sug.preferred_tool_id == a_id  # b really is the non-preferred one
        got = s.get(DuplicateSuggestion, sug.id)
        assert got is not None and got.status == "accepted"
        assert "Accepted by alice" in got.rationale


@requires_db
def test_dismiss_requires_justification_and_transitions_once(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        _seed_pair(s)
        run_dedup(s)
        s.commit()
        (sug,) = s.scalars(select(DuplicateSuggestion)).all()
        with pytest.raises(InvalidArgument):
            dismiss_suggestion(s, sug.id, justification="   ", actor="alice")
        dismiss_suggestion(s, sug.id, justification="Different auth\nmodel", actor="alice")
        s.commit()
        assert sug.status == "dismissed"
        assert "Dismissed by alice: Different auth model" in sug.rationale
        with pytest.raises(InvalidTransition):
            accept_suggestion(s, sug.id, actor="bob")
        with pytest.raises(SuggestionNotFound):
            accept_suggestion(s, "missing", actor="bob")


@requires_db
def test_list_suggestions_with_tool_refs(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        a_id, _ = _seed_pair(s)
        run_dedup(s)
        s.commit()
        page = list_suggestions(s, status="open")
        assert page.total == 1
        out = page.items[0]
        assert out.tool_a is not None and out.tool_b is not None
        assert {out.tool_a.server_name, out.tool_b.server_name} == {"github", "gitlab"}
        assert list_suggestions(s, status="accepted").total == 0
        with pytest.raises(InvalidArgument):
            list_suggestions(s, status="bogus")
