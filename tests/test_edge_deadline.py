"""Edge: ONE shared deadline model (bounded threads) + a per-request deadline."""

from __future__ import annotations

import threading
import time

import pytest

from mcprouter.inference import serve
from mcprouter.inference.adapters import DeadlineDecisionModel
from mcprouter.inference.errors import InferenceError
from tests.edge_app_helpers import AUTH, PATH, edge_client


class _Wedged:
    name = "wedged"

    def __init__(self, release: threading.Event) -> None:
        self.release = release

    def noul(self, state: str, question: str) -> float:
        self.release.wait(30)
        return 0.0


def test_edge_uses_one_shared_model_and_fails_fast_at_the_cap() -> None:
    release = threading.Event()
    with edge_client() as (app, client):
        assert isinstance(app.state.decision_model, DeadlineDecisionModel)
        app.state.decision_model = DeadlineDecisionModel(
            lambda: _Wedged(release), 0.1, max_in_flight=2
        )
        body = {"state": "s", "questions": {"q0": {"type": "noul", "instructions": "x"}}}
        try:
            before = threading.active_count()
            codes, elapsed = [], []
            for _ in range(3):
                t0 = time.monotonic()
                codes.append(client.post(PATH, json=body, headers=AUTH).status_code)
                elapsed.append(time.monotonic() - t0)
            leaked = threading.active_count() - before
        finally:
            release.set()
    print(f"thread-leak: before={before} leaked={leaked} cap=2 codes={codes}")
    assert codes == [503, 503, 503]
    assert leaked <= 2
    assert elapsed[2] < 0.1  # the cap trips immediately instead of queueing


class _Slow:
    name = "slow"

    def noul(self, state: str, question: str) -> float:
        time.sleep(0.06)
        return 0.5


def test_answer_has_one_deadline_per_request_not_per_question() -> None:
    model = DeadlineDecisionModel(lambda: _Slow(), 0.1)
    parsed = {f"q{i}": ("noul", "x", []) for i in range(3)}
    with pytest.raises(InferenceError):
        serve.answer(model, "s", parsed, deadline_s=0.1)
    assert serve.answer(model, "s", parsed, deadline_s=5.0)["q2"]["noul"] == 0.5
