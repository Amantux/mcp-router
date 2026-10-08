"""Per-call decision-model deadline (routing flag): a HUNG model becomes an
InferenceError and the pipeline takes its deterministic fallback (FR-06)."""

from __future__ import annotations

import threading
import time

import pytest

from mcprouter.inference.adapters import DeadlineDecisionModel
from mcprouter.inference.errors import DecisionRuntimeError, InferenceError
from mcprouter.interfaces import ChoiceResult, RouteRequest, ScoreResult
from mcprouter.routing.pipeline import RoutePipeline
from mcprouter.routing.retriever import HybridRetriever
from mcprouter.routing.scope import AllowAllScope
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db
from .test_routing_fakes import FakeHashEmbedder, ScriptedDecisionModel, add_server, add_tool


class SleepingModel:
    """Valid answers, but only after `delay` seconds (a hung backend)."""

    name = "sleeping-test"

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.release = threading.Event()

    def _sleep(self) -> None:
        self.release.wait(self.delay)

    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        self._sleep()
        return ChoiceResult(options[0], {o: 1.0 / len(options) for o in options})

    def score(self, state: str, question: str, levels: list[str]) -> ScoreResult:
        self._sleep()
        return ScoreResult(len(levels) - 1, [0.0] * (len(levels) - 1) + [1.0])

    def noul(self, state: str, question: str) -> float:
        self._sleep()
        return 0.9


def test_hung_model_raises_inference_error_at_the_deadline() -> None:
    hung = SleepingModel(delay=30)
    m = DeadlineDecisionModel(lambda: hung, timeout_s=0.2)
    t0 = time.perf_counter()
    with pytest.raises(DecisionRuntimeError) as ei:
        m.noul("state", "q?")
    assert isinstance(ei.value, InferenceError)
    assert time.perf_counter() - t0 < 2.0
    hung.release.set()


def test_fast_model_passes_through_and_errors_propagate() -> None:
    m = DeadlineDecisionModel(lambda: ScriptedDecisionModel(), timeout_s=1.0)
    assert m.noul("s", "q") == 0.9
    assert m.name == "scripted-test"
    assert [r.level for r in m.score_batch("s", ["a", "b"], ["no", "yes"])] == [0, 0]

    class Boom(ScriptedDecisionModel):
        def noul(self, state: str, question: str) -> float:
            raise ValueError("caller bug")

    with pytest.raises(ValueError):
        DeadlineDecisionModel(lambda: Boom(), timeout_s=1.0).noul("s", "q")


def test_abandoned_calls_are_bounded() -> None:
    hung = SleepingModel(delay=30)
    m = DeadlineDecisionModel(lambda: hung, timeout_s=0.05, max_in_flight=2)
    for _ in range(2):
        with pytest.raises(DecisionRuntimeError):
            m.noul("s", "q")
    t0 = time.perf_counter()
    with pytest.raises(DecisionRuntimeError, match="not responding"):
        m.noul("s", "q")  # fails fast: no third stranded thread
    assert time.perf_counter() - t0 < 0.05
    hung.release.set()


@requires_db
def test_pipeline_falls_back_when_the_model_hangs(db) -> None:  # noqa: ANN001
    emb = FakeHashEmbedder()
    with db() as s:
        gh = add_server(s, "github")
        add_tool(s, gh, "search_issues", "Search issues", embedder=emb, domain="development")
        s.commit()
    hung = SleepingModel(delay=30)
    settings = Settings(database_url=TEST_DB_URL)
    pipeline = RoutePipeline(
        db,
        HybridRetriever(db, emb),
        DeadlineDecisionModel(lambda: hung, timeout_s=0.2),
        settings,
    )
    t0 = time.perf_counter()
    res = pipeline.route(
        RouteRequest(query="search issues", agent_id="a", max_tools=3), AllowAllScope()
    )
    elapsed = time.perf_counter() - t0
    hung.release.set()
    assert res.fallback_used is True
    assert [t.tool_name for t in res.tools] == ["search_issues"]
    assert elapsed < 3.0
