"""Run synthetic_v1 end-to-end and print the honest baseline.

Backends: in-fence FakeHashEmbedder (lexical, NOT semantic) and
  * ExplodingDecisionModel  -> the FR-06 deterministic retrieval fallback
                               (THE pre-Laya baseline), and
  * a flat ScriptedDecisionModel (uniform probabilities, picks the first
    option) -> exercises the full hierarchical path with a deliberately
    uninformed classifier.
Quality numbers are REPORTED, not asserted (no tuning to flatter them). The
security invariants ARE asserted: zero unauthorized / unavailable exposures,
whatever the backend.

Run with `-s` to see the metrics.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.eval.dataset import load_named
from mcprouter.eval.runner import compute_metrics, run_cases
from mcprouter.eval.synthetic_catalog import seed_synthetic_catalog, synthetic_scope_resolver
from mcprouter.routing.pipeline import RoutePipeline
from mcprouter.routing.retriever import HybridRetriever

from .conftest import requires_db
from .test_routing_fakes import ExplodingDecisionModel, FakeHashEmbedder, ScriptedDecisionModel

pytestmark = requires_db


@pytest.mark.parametrize("backend", ["fallback", "flat-scripted"])
def test_synthetic_v1_baseline(db: sessionmaker[Session], settings: object, backend: str) -> None:
    emb = FakeHashEmbedder()
    with db() as s:
        seed_synthetic_catalog(s, emb)
        s.commit()
    model = ExplodingDecisionModel() if backend == "fallback" else ScriptedDecisionModel()
    pipeline = RoutePipeline(db, HybridRetriever(db, emb), model, settings)  # type: ignore[arg-type]
    outcomes = run_cases(
        pipeline,
        load_named("synthetic_v1"),
        session_factory=db,
        scope_resolver=synthetic_scope_resolver(db),
    )
    m = compute_metrics(outcomes)
    summary = {k: v for k, v in m.items() if k != "by_category"}
    print(f"\n[synthetic_v1 / {backend}] " + json.dumps(summary, indent=1, sort_keys=True))
    print(
        "by_category top1: "
        + json.dumps({c: v["top1_accuracy"] for c, v in m["by_category"].items()})
    )
    assert m["errored_cases"] == []
    assert m["case_count"] >= 60
    assert m["unauthorized_exposures"] == 0
    assert m["unavailable_exposures"] == 0
    assert m["fallback_rate"] == (1.0 if backend == "fallback" else 0.0)
