"""Run an eval dataset against the live pipeline and compute SPEC §10 metrics.

Metric definitions (positive case = non-empty expected_tools):
  top1_accuracy        positive cases whose rank-1 tool is in expected_tools
  top5_recall          mean over positive cases of |expected ∩ top5| / |expected|
  wrong_tool_rate      positive cases whose rank-1 tool exists and is NOT expected
  false_no_match_rate  positive cases answered with no tools
  no_match_accuracy    expect_no_match cases answered with no_match=true
  denied_accuracy      expect_denied cases exposing none of their forbidden tools
  unauthorized_exposures  expect_denied cases exposing a forbidden tool (MUST be 0)
  unavailable_exposures   other cases exposing a forbidden tool (MUST be 0)
  latency_ms           p50/p95/p99 of the pipeline's measured latency
  fallback_rate        share of cases where the deterministic fallback ran
Rates over an empty denominator are null, never a flattering 1.0.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from mcprouter.eval.dataset import EvalCase
from mcprouter.interfaces import RouteRequest, ScopeFilter
from mcprouter.routing.pipeline import RoutePipeline
from mcprouter.routing.servers import resolve_server_names

DEFAULT_EVAL_MAX_TOOLS = 5  # top-5 recall needs at least five exposed slots


@dataclass(frozen=True)
class CaseOutcome:
    case: EvalCase
    returned: list[str]  # "server/tool", rank order
    no_match: bool
    fallback_used: bool
    latency_ms: float
    error: str | None = None
    returned_skills: list[str] = field(default_factory=list)  # skill names, rank order


def run_cases(
    pipeline: RoutePipeline,
    cases: list[EvalCase],
    *,
    session_factory: sessionmaker[Session],
    scope_resolver: Callable[[str], ScopeFilter],
    max_tools: int = DEFAULT_EVAL_MAX_TOOLS,
) -> list[CaseOutcome]:
    outcomes: list[CaseOutcome] = []
    for case in cases:
        allowed_ids: list[str] | None = None
        if case.allowed_servers is not None:
            with session_factory() as s:
                allowed_ids, unknown = resolve_server_names(s, list(case.allowed_servers))
            if unknown:
                outcomes.append(
                    CaseOutcome(case, [], True, False, 0.0, error="unknown allowed_servers")
                )
                continue
        res = pipeline.route(
            RouteRequest(
                query=case.query,
                agent_id=case.agent_id,
                max_tools=max_tools,
                allowed_servers=allowed_ids,
            ),
            scope_resolver(case.agent_id),
        )
        outcomes.append(
            CaseOutcome(
                case=case,
                # Kinds are scored separately: tool metrics see only tools, so a
                # skill ranked first does not count as a "wrong tool".
                returned=[f"{t.server_name}/{t.tool_name}" for t in res.tools if t.kind != "skill"],
                returned_skills=[t.tool_name for t in res.tools if t.kind == "skill"],
                no_match=res.no_match,
                fallback_used=res.fallback_used,
                latency_ms=res.latency_ms,
            )
        )
    return outcomes


def _rate(num: float, den: int) -> float | None:
    return None if den == 0 else num / den


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(s)))  # nearest-rank
    return s[rank - 1]


def compute_metrics(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    ok = [o for o in outcomes if o.error is None]
    # Errored cases (returned=[]) stay IN every denominator: a case that
    # cannot run against this catalog is a miss, not a silently smaller test.
    m = _core(outcomes)
    m["case_count"] = len(outcomes)
    m["errored_cases"] = [o.case.id for o in outcomes if o.error is not None]
    lat = [o.latency_ms for o in ok]
    m["latency_ms"] = {
        "p50": _percentile(lat, 50),
        "p95": _percentile(lat, 95),
        "p99": _percentile(lat, 99),
    }
    m["fallback_rate"] = _rate(sum(o.fallback_used for o in ok), len(ok))
    cats = sorted({o.case.category for o in outcomes})
    m["skills"] = _skill_core(outcomes)
    m["mixed"] = _mixed_core(outcomes)
    m["by_category"] = {c: _core([o for o in outcomes if o.case.category == c]) for c in cats}
    return m


def _core(outs: list[CaseOutcome]) -> dict[str, Any]:
    pos = [o for o in outs if o.case.expected_tools]
    top1 = wrong = false_nm = 0
    recall = 0.0
    for o in pos:
        expected = set(o.case.expected_tools)
        if o.returned and o.returned[0] in expected:
            top1 += 1
        elif o.returned:
            wrong += 1
        else:
            false_nm += 1
        recall += len(expected & set(o.returned[:5])) / len(expected)
    nm = [o for o in outs if o.case.expect_no_match]
    denied = [o for o in outs if o.case.expect_denied]
    leaked = [o for o in outs if set(o.case.forbidden_tools) & set(o.returned)]
    unauthorized = sum(1 for o in leaked if o.case.expect_denied)
    return {
        "positive_cases": len(pos),
        "top1_accuracy": _rate(top1, len(pos)),
        "top5_recall": _rate(recall, len(pos)),
        "wrong_tool_rate": _rate(wrong, len(pos)),
        "false_no_match_rate": _rate(false_nm, len(pos)),
        "no_match_cases": len(nm),
        "no_match_accuracy": _rate(sum(o.no_match for o in nm), len(nm)),
        "denied_cases": len(denied),
        "denied_accuracy": _rate(len(denied) - unauthorized, len(denied)),
        "unauthorized_exposures": unauthorized,
        "unavailable_exposures": len(leaked) - unauthorized,
    }


def _skill_core(outs: list[CaseOutcome]) -> dict[str, Any]:
    """Skill top-1/top-5 over cases with expected_skills; errored cases
    (returned_skills=[]) stay in the denominator as misses."""
    pos = [o for o in outs if o.case.expected_skills]
    top1 = 0
    recall = 0.0
    for o in pos:
        expected = set(o.case.expected_skills)
        top1 += bool(o.returned_skills) and o.returned_skills[0] in expected
        recall += len(expected & set(o.returned_skills[:5])) / len(expected)
    return {
        "positive_cases": len(pos),
        "top1_accuracy": _rate(top1, len(pos)),
        "top5_recall": _rate(recall, len(pos)),
    }


def _mixed_core(outs: list[CaseOutcome]) -> dict[str, Any]:
    """Cases expecting both kinds: each kind's rank-1 must be an expected one."""
    mixed = [o for o in outs if o.case.expected_tools and o.case.expected_skills]
    both = sum(
        1
        for o in mixed
        if o.returned
        and o.returned[0] in o.case.expected_tools
        and o.returned_skills
        and o.returned_skills[0] in o.case.expected_skills
    )
    return {"cases": len(mixed), "both_top1_rate": _rate(both, len(mixed))}


def case_rows(outcomes: list[CaseOutcome]) -> list[dict[str, Any]]:
    return [
        {
            "id": o.case.id,
            "category": o.case.category,
            "expected": list(o.case.expected_tools),
            "returned": o.returned,
            "expected_skills": list(o.case.expected_skills),
            "returned_skills": o.returned_skills,
            "no_match": o.no_match,
            "fallback_used": o.fallback_used,
            "latency_ms": round(o.latency_ms, 3),
            "error": o.error,
        }
        for o in outcomes
    ]
