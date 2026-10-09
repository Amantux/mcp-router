"""Wave-4 S2e: eval skill matching on "source/name" + forbidden_skills."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcprouter.eval.dataset import DatasetError, parse_jsonl
from mcprouter.eval.runner import CaseOutcome, compute_metrics


def _case(**kw: object) -> str:
    base: dict[str, object] = {"id": "c1", "query": "fill the pdf", "category": "skill"}
    base.update(kw)
    return json.dumps(base)


def test_forbidden_skills_parsed_and_validated() -> None:
    (case,) = parse_jsonl(_case(expected_skills=["a/pdf"], forbidden_skills=["b/pdf"]))
    assert case.forbidden_skills == ("b/pdf",)
    with pytest.raises(DatasetError):
        parse_jsonl(_case(forbidden_skills="nope"))


def _skills(expected: list[str], returned: list[str], forbidden: list[str] | None = None) -> Any:
    (case,) = parse_jsonl(_case(expected_skills=expected, forbidden_skills=forbidden or []))
    out = CaseOutcome(
        case=case,
        returned=[],
        no_match=False,
        fallback_used=False,
        latency_ms=1.0,
        returned_skills=returned,
    )
    return compute_metrics([out])["skills"]


def test_qualified_match_requires_the_right_source() -> None:
    assert _skills(["anthropic/pdf"], ["anthropic/pdf"])["top1_accuracy"] == 1.0
    assert _skills(["anthropic/pdf"], ["team/pdf"])["top1_accuracy"] == 0.0
    # bare names stay accepted (documented as ambiguous: any source matches)
    assert _skills(["pdf"], ["team/pdf"])["top1_accuracy"] == 1.0


def test_forbidden_skill_shown_counts_as_unauthorized_exposure() -> None:
    m = _skills(["anthropic/pdf"], ["anthropic/pdf", "team/deploy"], ["team/deploy"])
    assert m["unauthorized_skill_exposures"] == 1
    clean = _skills(["anthropic/pdf"], ["anthropic/pdf"], ["team/deploy"])
    assert clean["unauthorized_skill_exposures"] == 0
