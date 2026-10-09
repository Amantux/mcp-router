"""eval runner forwards each case's kinds to RouteRequest (item B2)."""

from __future__ import annotations

from typing import Any

from mcprouter.eval.dataset import EvalCase
from mcprouter.eval.runner import run_cases
from mcprouter.interfaces import RouteResult


class _Pipe:
    def __init__(self) -> None:
        self.kinds: list[Any] = []

    def route(self, req: Any, _scope: Any) -> RouteResult:
        self.kinds.append(req.kinds)
        return RouteResult("rr", [], False, 1.0, "m")


def test_case_kinds_reach_the_route_request() -> None:
    pipe = _Pipe()
    cases = [
        EvalCase("a", "q", "c", kinds=("skill",)),
        EvalCase("b", "q", "c", kinds=("tool", "skill")),
    ]
    run_cases(pipe, cases, session_factory=None, scope_resolver=lambda _a: None)  # type: ignore[arg-type]
    assert pipe.kinds == [("skill",), ("tool", "skill")]
