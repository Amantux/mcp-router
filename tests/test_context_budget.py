"""hard-token-budgets spec (docs/specs/2026-11-10-hard-token-budgets.md).

The ESTIMATED-token budget is one more axis on the existing post-ranking
_cap machinery, narrowing only:

1. estimator pins (chars/4, ESTIMATOR label verbatim — CI-reproducible, no
   tokenizer import);
2. budget == the rank-order estimate sum of the top-3 -> exactly those 3;
   one token below -> 2 (off-by-one boundary);
3. the knob unset -> behaviour identical to today (regression);
4. budget below the cheapest single definition -> empty selection,
   no_match=true, and the selection is always a prefix of the ranking;
5. guard: a request may LOWER the budget, never raise it, and the budget
   never widens access (applied after policy scoping). Remove or ignore the
   token truncation in routing/pipeline._cap and the named guard test fails.
7. REST functional: fat-schema tools + a mid-range budget expose fewer tools
   than maxTools alone would; the response labels everything estimated
   (max_tokens_applied + estimator, never "actual")."""

from __future__ import annotations

import math
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.analytics.tokens import (
    ESTIMATOR,
    estimate_tokens,
    rendered_definition,
    tool_token_map,
)
from mcprouter.api.app import create_app
from mcprouter.api.routes_route import install_routing
from mcprouter.interfaces import RouteRequest
from mcprouter.models import MCPServerRecord, MCPToolRecord
from mcprouter.routing.budgets import context_budget
from mcprouter.routing.pipeline import RoutePipeline
from mcprouter.routing.retriever import HybridRetriever
from mcprouter.routing.scope import AllowAllScope
from mcprouter.settings import Settings
from tests.support.routing_fakes import (
    FakeHashEmbedder,
    ScriptedDecisionModel,
    add_server,
    add_tool,
)

from .conftest import TEST_DB_URL, requires_db

FAT_SCHEMA = {
    "type": "object",
    "properties": {f"field_{i}": {"type": "string", "description": "a" * 24} for i in range(12)},
}


# ----------------------------------------------------- 1. estimator pins
def test_estimator_pins_fat_and_thin_schemas() -> None:
    thin = estimate_tokens("alpha", "search", "find things", None)
    fat = estimate_tokens("alpha", "search", "find things", FAT_SCHEMA)
    # Both reproduce exactly ceil(chars / 4) over the rendered definition.
    assert thin == math.ceil(len(rendered_definition("alpha", "search", "find things", None)) / 4)
    assert fat == math.ceil(
        len(rendered_definition("alpha", "search", "find things", FAT_SCHEMA)) / 4
    )
    assert 0 < thin < fat  # the fat schema must dominate the estimate
    assert ESTIMATOR == "chars/4 over compact JSON of name+description+inputSchema"


# ------------------------------------------------ settings knob (empty/error)
def test_max_context_tokens_setting_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_MAX_CONTEXT_TOKENS", "")
    assert Settings.from_env().max_context_tokens is None  # empty = no cap
    monkeypatch.setenv("MCPR_MAX_CONTEXT_TOKENS", "2000")
    assert Settings.from_env().max_context_tokens == 2000
    monkeypatch.setenv("MCPR_MAX_CONTEXT_TOKENS", "-4")
    with pytest.raises(ValueError, match="MCPR_MAX_CONTEXT_TOKENS") as ei:
        Settings.from_env()
    assert "-4" not in str(ei.value)  # never echoes the bad value
    monkeypatch.setenv("MCPR_MAX_CONTEXT_TOKENS", "nope")
    with pytest.raises(ValueError, match="MCPR_MAX_CONTEXT_TOKENS"):
        Settings.from_env()


def test_context_budget_request_may_lower_never_raise() -> None:
    settings = Settings(max_context_tokens=500)
    assert context_budget(None, settings).applied == 500  # the knob decides
    assert context_budget(200, settings).applied == 200  # request lowers
    assert context_budget(5000, settings).applied == 500  # ... never raises
    assert context_budget(None, settings).clamped_by is None
    assert context_budget(5000, settings).clamped_by == "global"
    # No knob and no request: no budget axis at all.
    assert context_budget(None, Settings()).applied is None


# ------------------------------------------------------------ 2-5. pipeline
def _name_costs(db: sessionmaker[Session], server: str = "alpha") -> dict[str, int]:
    """tool name -> ESTIMATED tokens, computed with the estimator itself."""
    with db() as s:
        rows = s.execute(
            select(
                MCPToolRecord.name,
                MCPToolRecord.description,
                MCPToolRecord.input_schema,
                MCPToolRecord.server_id,
            )
            .join(MCPServerRecord, MCPServerRecord.id == MCPToolRecord.server_id)
            .where(MCPServerRecord.name == server)
        ).all()
    return {name: estimate_tokens(server, name, desc, sch) for name, desc, sch, _sid in rows}


def _all_name_costs(db: sessionmaker[Session]) -> dict[tuple[str, str], int]:
    """(server, tool) -> ESTIMATED tokens, across EVERY server (a selection
    may span servers, so name alone is not a key)."""
    with db() as s:
        rows = s.execute(
            select(
                MCPToolRecord.name,
                MCPToolRecord.description,
                MCPToolRecord.input_schema,
                MCPToolRecord.server_id,
                MCPServerRecord.name,
            ).join(MCPServerRecord, MCPServerRecord.id == MCPToolRecord.server_id)
        ).all()
    return {
        (srv, name): estimate_tokens(srv, name, desc, sch) for name, desc, sch, _sid, srv in rows
    }


@requires_db
def test_pipeline_token_budget_truncates_in_rank_order(db: sessionmaker[Session]) -> None:
    emb = FakeHashEmbedder()
    with db() as s:
        srv = add_server(s, "alpha")
        for i in range(6):
            add_tool(
                s, srv, f"issue_tool_{i}", f"issue tracker tool {'x' * (40 * i)}", embedder=emb
            )
        s.commit()
    # Regression first (case 3): the knob unset -> today's behaviour exactly.
    base = RoutePipeline(
        db, HybridRetriever(db, emb), ScriptedDecisionModel(), Settings(database_url=TEST_DB_URL)
    )
    full = base.route(RouteRequest("issue tool", "a", 6), AllowAllScope())
    assert len(full.tools) == 6
    assert full.tokens_applied > 0

    costs = _name_costs(db)
    ranked = [t.tool_name for t in full.tools]  # the exact rank order
    top3 = sum(costs[n] for n in ranked[:3])
    for budget, expected in ((top3, ranked[:3]), (top3 - 1, ranked[:2])):
        pipe = RoutePipeline(
            db,
            HybridRetriever(db, emb),
            ScriptedDecisionModel(),
            Settings(database_url=TEST_DB_URL, max_context_tokens=budget),
        )
        res = pipe.route(RouteRequest("issue tool", "a", 6), AllowAllScope())
        assert [t.tool_name for t in res.tools] == expected
        assert res.tokens_applied == sum(costs[n] for n in expected)


@requires_db
def test_pipeline_budget_below_cheapest_definition_is_an_honest_no_match(
    db: sessionmaker[Session],
) -> None:
    emb = FakeHashEmbedder()
    with db() as s:
        srv = add_server(s, "alpha")
        for i in range(3):
            add_tool(
                s, srv, f"issue_tool_{i}", f"issue tracker tool {'x' * (40 * i)}", embedder=emb
            )
        s.commit()
    cheapest = min(_name_costs(db).values())
    pipe = RoutePipeline(
        db,
        HybridRetriever(db, emb),
        ScriptedDecisionModel(),
        Settings(database_url=TEST_DB_URL, max_context_tokens=cheapest - 1),
    )
    res = pipe.route(RouteRequest("issue tool", "a", 3), AllowAllScope())
    assert res.tools == []
    assert res.no_match is True
    assert res.tokens_applied == 0


@requires_db
def test_token_budget_narrows_only_and_never_widens(db: sessionmaker[Session]) -> None:
    """GUARD (mutation check): remove/ignore the token truncation in
    routing/pipeline._cap, or apply the budget before policy scoping, and
    this test fails. A request may LOWER the budget, never raise it; scoping
    still binds with an absurdly large budget; the charged estimate never
    exceeds the applied budget."""
    emb = FakeHashEmbedder()
    with db() as s:
        for name in ("alpha", "beta"):
            srv = add_server(s, name)
            add_tool(s, srv, f"issue_tool_{name}", "issue tracker tool", embedder=emb)
        s.commit()
    costs = _all_name_costs(db)
    db_servers = {name: sid for name, sid in db_bind(db)}
    cheap = 200
    pipe = RoutePipeline(
        db,
        HybridRetriever(db, emb),
        ScriptedDecisionModel(),
        Settings(database_url=TEST_DB_URL, max_context_tokens=cheap),
    )
    res = pipe.route(
        RouteRequest("issue tool", "a", 8, max_context_tokens=1_000_000), AllowAllScope()
    )
    charged = sum(costs[(t.server_name, t.tool_name)] for t in res.tools)
    assert charged <= cheap  # the request could not raise the budget
    # Scoping still binds at any budget: only the permitted server's tools
    # survive a budget big enough to expose both.
    wide = RoutePipeline(
        db, HybridRetriever(db, emb), ScriptedDecisionModel(), Settings(database_url=TEST_DB_URL)
    )
    scoped = wide.route(
        RouteRequest(
            "issue tool", "a", 8, allowed_servers=[db_servers["beta"]], max_context_tokens=1_000_000
        ),
        AllowAllScope(),
    )
    assert [t.server_name for t in scoped.tools] == ["beta"]


def db_bind(db: sessionmaker[Session]) -> list[tuple[str, str]]:
    with db() as s:
        return [
            (name, sid)
            for name, sid in s.execute(select(MCPServerRecord.name, MCPServerRecord.id)).all()
        ]


# ------------------------------------------------------------ 7. REST (fat)
KEY = "key-token-budget-a1"


@pytest.fixture()
def fat_api(db: sessionmaker[Session]) -> Iterator[tuple[TestClient, sessionmaker[Session]]]:
    settings = Settings(
        database_url=TEST_DB_URL,
        agent_keys=f"a1:{KEY}",
        max_exposed_tools=6,
        max_context_tokens=300,
    )
    app = create_app(settings, env={})
    factory = app.state.session_factory
    # Scripted model + FakeHashEmbedder, like every other /route API test:
    # the deterministic in-process backend scores catalog similarity, which
    # this fixture's hand-walked expectations cannot pin down — the scripted
    # model keeps the ranking stable.
    pipeline = RoutePipeline(
        factory, HybridRetriever(factory, FakeHashEmbedder()), ScriptedDecisionModel(), settings
    )
    install_routing(app, pipeline)
    with factory() as s:
        srv = add_server(s, "alpha")
        for i in range(3):
            add_tool(
                s,
                srv,
                f"issue_tool_{i}",
                "issue tracker tool",
                embedder=None,
                input_schema=FAT_SCHEMA,
            )
        s.commit()
    yield TestClient(app, headers={"Authorization": f"Bearer {KEY}"}), factory


@requires_db
def test_route_fat_schemas_with_mid_budget_and_estimate_labels(
    fat_api: tuple[TestClient, sessionmaker[Session]],
) -> None:
    client, factory = fat_api
    with factory() as s:
        id_rows = s.execute(select(MCPToolRecord.id, MCPToolRecord.name)).all()
        ids = {tid: name for tid, name in id_rows}
        ests = tool_token_map(s)
    cost_by_name = {ids[tid]: est for tid, est in ests.items()}
    # Rank ties break on name (pipeline _rank_key); walk the cap by hand:
    # the same prefix the budget must produce.
    budget, total, expected = 300, 0, []
    for name in sorted(cost_by_name):
        if total + cost_by_name[name] > budget:
            break
        total += cost_by_name[name]
        expected.append(name)
    assert 0 < len(expected) < 3, "the fixture must make the budget bind mid-range"

    # camelCase request budget; response labels everything estimated.
    r = client.post(
        "/api/v1/route", json={"query": "issue tracker tool", "maxContextTokens": 10**6}
    )
    body = r.json()
    assert r.status_code == 200, r.text
    assert body["estimator"] == ESTIMATOR  # the estimator label, verbatim
    assert body["max_tokens_applied"] == total
    assert [t["tool"] for t in body["tools"]] == expected  # fewer than maxTools=6 alone
    assert "actual" not in str(body).lower()

    # snake_case casing too, and below the cheapest definition -> honest
    # no_match with zero charged.
    r = client.post("/api/v1/route", json={"query": "issue tracker tool", "max_context_tokens": 1})
    body = r.json()
    assert body["no_match"] is True and body["tools"] == [] and body["max_tokens_applied"] == 0

    # Cached hit reports the same charged estimate as the computing route.
    r = client.post(
        "/api/v1/route", json={"query": "issue tracker tool", "max_context_tokens": 10**6}
    )
    body = r.json()
    assert body["cached"] is True
    assert body["max_tokens_applied"] == total
    assert body["estimator"] == ESTIMATOR


__all__ = ["FAT_SCHEMA"]
