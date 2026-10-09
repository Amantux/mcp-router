"""Eval: expected_skills + kinds; skill and mixed metrics computed separately."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcprouter.eval.dataset import DatasetError, parse_jsonl
from mcprouter.eval.runner import CaseOutcome, compute_metrics, run_cases
from mcprouter.interfaces import RoutedTool, RouteResult


def _line(**kw: Any) -> str:
    return json.dumps({"id": kw.pop("id", "c1"), "query": "q", "category": "x", **kw})


def test_dataset_parses_expected_skills_and_derives_kinds() -> None:
    (c,) = parse_jsonl(_line(expected_skills=["pdf-fill"]))
    assert c.expected_skills == ("pdf-fill",) and c.kinds == ("skill",)
    (m,) = parse_jsonl(_line(expected_tools=["fs/read"], expected_skills=["pdf-fill"]))
    assert m.kinds == ("tool", "skill")
    with pytest.raises(DatasetError):
        parse_jsonl(_line(expected_skills=["pdf-fill"], kinds=["widget"]))
    with pytest.raises(DatasetError):
        parse_jsonl(_line(expect_no_match=True, expected_skills=["pdf-fill"]))


def _rt(name: str, kind: str, server: str = "s") -> RoutedTool:
    return RoutedTool(tool_id=name, server_name=server, tool_name=name, score=1.0, kind=kind)


class FakePipeline:
    def __init__(self, by_query: dict[str, list[RoutedTool]]) -> None:
        self.by_query = by_query

    def route(self, req: Any, scope: Any) -> RouteResult:
        return RouteResult(
            request_id="r", tools=self.by_query[req.query], fallback_used=False,
            no_match=not self.by_query[req.query], latency_ms=1.0, model_version="fake",
        )  # fmt: skip


def test_runner_splits_kinds_and_scores_skills_separately() -> None:
    cases = parse_jsonl(
        "\n".join(
            [
                json.dumps(
                    {"id": "s1", "query": "a", "category": "skill", "expected_skills": ["pdf-fill"]}
                ),
                json.dumps(
                    {"id": "s2", "query": "b", "category": "skill", "expected_skills": ["git-log"]}
                ),
                json.dumps(
                    {
                        "id": "m1",
                        "query": "c",
                        "category": "mixed",
                        "expected_tools": ["s/read"],
                        "expected_skills": ["pdf-fill"],
                    }
                ),
            ]
        )  # fmt: skip
    )
    fake = FakePipeline(
        {
            # skill ranked above a tool must not break tool top-1 for the mixed case
            "a": [_rt("read", "tool"), _rt("pdf-fill", "skill")],
            "b": [_rt("pdf-fill", "skill")] + [_rt(f"x{i}", "skill") for i in range(5)]
            + [_rt("git-log", "skill")],
            "c": [_rt("pdf-fill", "skill"), _rt("read", "tool")],
        }
    )  # fmt: skip
    outs = run_cases(
        fake,  # type: ignore[arg-type]
        cases,
        session_factory=None,  # type: ignore[arg-type]
        scope_resolver=lambda _a: None,  # type: ignore[arg-type, return-value]
    )
    assert outs[0].returned == ["s/read"] and outs[0].returned_skills == ["s/pdf-fill"]
    m = compute_metrics(outs)
    sk = m["skills"]
    assert sk["positive_cases"] == 3
    assert sk["top1_accuracy"] == pytest.approx(2 / 3)  # s1 + m1; s2 rank-7
    assert sk["top5_recall"] == pytest.approx(2 / 3)
    assert m["mixed"]["cases"] == 1 and m["mixed"]["both_top1_rate"] == 1.0
    assert m["top1_accuracy"] == 1.0  # tool metrics see only tool-kind results
    assert m["positive_cases"] == 1


def test_errored_skill_case_counts_as_miss() -> None:
    (c,) = parse_jsonl(_line(expected_skills=["pdf-fill"]))
    out = CaseOutcome(c, [], True, False, 0.0, error="boom")
    m = compute_metrics([out])
    assert m["skills"]["positive_cases"] == 1 and m["skills"]["top1_accuracy"] == 0.0
