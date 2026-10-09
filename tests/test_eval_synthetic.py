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
    # Only tools are seeded here: a skill-only case (kinds=("skill",)) has no
    # candidates, so it is a no_match that never reaches the model and the
    # fallback never runs. Every case that exercises tools must fall back.
    cases = load_named("synthetic_v1")
    tool_share = sum("tool" in c.kinds for c in cases) / len(cases)
    assert m["fallback_rate"] == pytest.approx(tool_share if backend == "fallback" else 0.0)
    assert all(o.no_match and not o.returned for o in outcomes if o.case.kinds == ("skill",))


@pytest.mark.parametrize("backend", ["fallback", "flat-scripted"])
def test_synthetic_v1_baseline_with_skills(
    db: sessionmaker[Session], settings: object, backend: str
) -> None:
    """Same deterministic pipeline, tools AND skills seeded: skills/mixed
    quality is REPORTED; skill exposure past policy is asserted zero."""
    from mcprouter.eval.synthetic_catalog import seed_synthetic_skills

    emb = FakeHashEmbedder()
    with db() as s:
        seed_synthetic_catalog(s, emb)
        seed_synthetic_skills(s, emb)
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
    print(
        f"\n[synthetic_v1+skills / {backend}] "
        + json.dumps({k: m[k] for k in ("skills", "mixed")}, indent=1, sort_keys=True)
        + f" tools.top1_accuracy={m['top1_accuracy']}"
    )
    assert m["errored_cases"] == []
    assert m["case_count"] >= 60
    assert m["unauthorized_exposures"] == 0
    assert m["unavailable_exposures"] == 0
    assert m["skills"]["unauthorized_skill_exposures"] == 0


def test_synthetic_skills_fixture_seeds_ground_truth(db: sessionmaker[Session]) -> None:
    """Wave-4 skills fixture: deterministic ids, reviewed ground-truth
    classification that the rule classifier agrees with, required hazards."""
    from sqlalchemy import select

    from mcprouter.eval.synthetic_catalog import (
        NEAR_DUPLICATE_SKILLS,
        SKILLS,
        _stable_id,
        seed_synthetic_skills,
        skill_classification_mismatches,
    )
    from mcprouter.models import SkillRecord

    assert skill_classification_mismatches() == []
    assert len(SKILLS) >= 12
    assert {sk.domain for sk in SKILLS} >= {
        "communication",
        "databases",
        "development",
        "files",
        "productivity",
    }
    refs = {sk.ref for sk in SKILLS}
    assert all(a in refs and b in refs for a, b in NEAR_DUPLICATE_SKILLS)
    assert any(sk.operation == "execute" and sk.has_scripts for sk in SKILLS)
    with db() as s:
        seed_synthetic_skills(s, FakeHashEmbedder())
        s.commit()
        rows = s.scalars(select(SkillRecord)).all()
    # Deterministic: ids are uuid5 of the qualified ref, not random.
    assert len(rows) == len(SKILLS)
    assert {r.id for r in rows} == {_stable_id("skill", sk.ref) for sk in SKILLS}
    by_name = {sk.name: sk for sk in SKILLS}
    for r in rows:
        assert r.classification_reviewed and r.operation == by_name[r.name].operation
        assert r.body_tokens_est == len(r.body) // 4 and r.embedding is not None
