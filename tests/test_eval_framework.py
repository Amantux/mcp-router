"""Eval framework: dataset parsing, metric definitions, persistence, endpoint."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.app import create_app
from mcprouter.api.routes_route import install_routing
from mcprouter.eval.dataset import DatasetError, available_datasets, load_named, parse_jsonl
from mcprouter.eval.runner import CaseOutcome, compute_metrics
from mcprouter.eval.store import ensure_eval_table
from mcprouter.eval.synthetic_catalog import seed_synthetic_catalog, synthetic_scope_resolver
from mcprouter.routing.pipeline import RoutePipeline
from mcprouter.routing.retriever import HybridRetriever
from mcprouter.settings import Settings
from tests.support.routing_fakes import ExplodingDecisionModel, FakeHashEmbedder

from .conftest import TEST_DB_URL, requires_db


def _case(**kw: object) -> str:
    base: dict[str, object] = {"id": "c1", "query": "q", "category": "direct"}
    base.update(kw)
    return json.dumps(base)


def test_parse_valid_and_defaults() -> None:
    cases = parse_jsonl(_case(expected_tools=["github/search_issues"]) + "\n\n")
    assert len(cases) == 1
    c = cases[0]
    assert c.expected_tools == ("github/search_issues",)
    assert c.allowed_servers is None and c.expect_no_match is False and c.expect_denied is False
    assert c.agent_id == "eval-agent"


@pytest.mark.parametrize(
    "line",
    [
        "{not json",
        _case(query=""),
        _case(expected_tools=["no_slash"]),
        _case(expect_no_match=True, expected_tools=["a/b"]),
        _case(expect_denied=True),  # denied case must name forbidden tools
        _case(surprise_field=1),
        json.dumps({"id": "x", "query": "q"}),  # missing category
    ],
)
def test_parse_rejects_malformed(line: str) -> None:
    with pytest.raises(DatasetError):
        parse_jsonl(line)


def test_duplicate_case_ids_rejected() -> None:
    with pytest.raises(DatasetError):
        parse_jsonl(_case(expected_tools=["a/b"]) + "\n" + _case(expected_tools=["a/b"]))


def test_named_dataset_lookup_is_a_whitelist() -> None:
    assert "synthetic_v1" in available_datasets()
    for bad in ["../../etc/passwd", "synthetic_v1.jsonl", "/abs", "nope"]:
        with pytest.raises(DatasetError):
            load_named(bad)


def test_synthetic_v1_meets_spec_coverage() -> None:
    cases = load_named("synthetic_v1")
    assert len(cases) >= 60
    cats = {c.category for c in cases}
    assert {
        "ambiguous",
        "overlapping",
        "unavailable",
        "unauthorized",
        "multistep",
        "no_match",
    } <= cats
    assert any(c.expect_denied for c in cases)
    assert any(len(c.expected_tools) > 1 and c.category == "multistep" for c in cases)
    assert sum(c.expect_no_match for c in cases) >= 5


def _o(case_line: str, tools: list[str], no_match: bool = False, lat: float = 1.0) -> CaseOutcome:
    (case,) = parse_jsonl(case_line)
    return CaseOutcome(
        case=case, returned=tools, no_match=no_match, fallback_used=False, latency_ms=lat
    )


def test_metric_definitions() -> None:
    outs = [
        _o(_case(id="a", expected_tools=["s/x"]), ["s/x", "s/y"]),  # top1 hit
        _o(_case(id="b", expected_tools=["s/x"]), ["s/y", "s/x"]),  # wrong top1, recall hit
        _o(_case(id="c", expected_tools=["s/x", "s/z"], category="multistep"), ["s/z"]),
        _o(_case(id="d", expected_tools=["s/x"]), [], no_match=True),  # false no-match
        _o(_case(id="e", expect_no_match=True, category="no_match"), [], no_match=True),
        _o(_case(id="f", expect_no_match=True, category="no_match"), ["s/q"]),
        _o(
            _case(id="g", expect_denied=True, forbidden_tools=["s/w"], category="unauthorized"),
            ["s/w"],
        ),  # unauthorized exposure!
        _o(_case(id="h", forbidden_tools=["s/off"], category="unavailable"), ["s/x"]),
    ]
    m = compute_metrics(outs)
    assert m["positive_cases"] == 4
    assert m["top1_accuracy"] == pytest.approx(2 / 4)  # a, c
    assert m["top5_recall"] == pytest.approx((1 + 1 + 0.5 + 0) / 4)
    assert m["wrong_tool_rate"] == pytest.approx(1 / 4)  # b
    assert m["false_no_match_rate"] == pytest.approx(1 / 4)  # d
    assert m["no_match_accuracy"] == pytest.approx(1 / 2)
    assert m["unauthorized_exposures"] == 1 and m["denied_accuracy"] == 0.0
    assert m["unavailable_exposures"] == 0
    assert m["latency_ms"]["p50"] == 1.0
    assert m["by_category"]["multistep"]["top1_accuracy"] == 1.0


@requires_db
def test_evaluate_endpoint_runs_and_persists(db: sessionmaker[Session]) -> None:
    settings = Settings(database_url=TEST_DB_URL)
    app = create_app(settings, env={})
    factory = app.state.session_factory
    emb = FakeHashEmbedder()
    with factory() as s:
        seed_synthetic_catalog(s, emb)
        s.commit()
    pipeline = RoutePipeline(
        factory, HybridRetriever(factory, emb), ExplodingDecisionModel(), settings
    )
    install_routing(app, pipeline, scope_resolver=synthetic_scope_resolver(factory))
    c = TestClient(app)
    try:
        r = c.post("/api/v1/route/evaluate", json={"dataset": "synthetic_v1"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["case_count"] >= 60 and body["dataset"] == "synthetic_v1"
        # Tools-only catalog: skill-only cases are no_match and never reach
        # the model, so only the tool-exercising share falls back.
        cases = load_named("synthetic_v1")
        tool_share = sum("tool" in c.kinds for c in cases) / len(cases)
        assert body["metrics"]["fallback_rate"] == pytest.approx(tool_share)
        assert tool_share < 1.0  # the dataset does carry skill-only cases
        # Security invariant holds even on the dumbest backend.
        assert body["metrics"]["unauthorized_exposures"] == 0
        with factory() as s:
            row = s.execute(
                text("SELECT dataset, case_count FROM eval_results WHERE id = :i"),
                {"i": body["id"]},
            ).one()
        assert tuple(row) == ("synthetic_v1", body["case_count"])
        assert c.post("/api/v1/route/evaluate", json={"dataset": "../x"}).status_code == 404
    finally:
        with factory() as s:
            s.execute(text("DELETE FROM eval_results"))
            s.commit()


@requires_db
def test_eval_table_created_idempotently(db: sessionmaker[Session]) -> None:
    from mcprouter.db import make_engine

    eng = make_engine(Settings(database_url=TEST_DB_URL))
    ensure_eval_table(eng)
    ensure_eval_table(eng)


def test_errored_cases_count_against_rates_not_dropped() -> None:
    (case,) = parse_jsonl(_case(expected_tools=["s/x"]))
    m = compute_metrics(
        [
            CaseOutcome(
                case=case,
                returned=[],
                no_match=True,
                fallback_used=False,
                latency_ms=0.0,
                error="unknown allowed_servers",
            ),
        ]
    )
    assert m["positive_cases"] == 1 and m["top1_accuracy"] == 0.0
    assert m["errored_cases"] == ["c1"]
