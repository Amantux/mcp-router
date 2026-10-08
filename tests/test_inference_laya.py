"""Laya adapter. Unit tests use a fake agent that returns the REAL answer shapes
captured from laya==0.4.0 on CPU; the @slow tests load the real checkpoint.

Run the slow ones with:  MCPR_RUN_SLOW=1 HF_HOME=<cache> pytest -q -m slow tests/
"""

from __future__ import annotations

import math
import os
import sys
import time
from typing import Any

import pytest

from mcprouter.inference.errors import DecisionProtocolError, DecisionRuntimeError
from mcprouter.inference.errors import ModelUnavailableError as Unavailable
from mcprouter.inference.laya import LayaDecisionModel
from mcprouter.inference.validation import ValidatedDecisionModel
from mcprouter.interfaces import BatchScoringDecisionModel, DecisionModel

slow = pytest.mark.skipif(
    not os.environ.get("MCPR_RUN_SLOW"), reason="slow: set MCPR_RUN_SLOW=1 (downloads/loads models)"
)


class FakeAgent:
    """Mimics laya.Agent.predict's documented/observed output (probabilities rounded to 4 dp)."""

    def __init__(self, *, novel: bool = False, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.novel = novel
        self.fail = fail

    def predict(self, state: str, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(questions)
        if self.fail:
            raise RuntimeError("CUDA out of memory /secret/path/model.safetensors")
        answers: dict[str, Any] = {}
        for qid, q in questions.items():
            crit = q.get("criteria")
            if q["type"] == "choice":
                keys = list(crit) if isinstance(crit, list) else list(crit.keys())
                k = len(keys)
                probs = {
                    key: round((0.7 if i == 0 else 0.3 / (k - 1)), 4) for i, key in enumerate(keys)
                }
                choice = "invented_tool" if self.novel else keys[0]
                answers[qid] = {"type": "choice", "choice": choice, "probabilities": probs}
            elif q["type"] == "score":
                k = len(crit)
                p = [0.1 / (k - 1)] * k
                p[-1] = 0.9
                answers[qid] = {
                    "type": "score",
                    "score": sum(i * x for i, x in enumerate(p)),
                    "probabilities": {str(i): round(x, 4) for i, x in enumerate(p)},
                }
            else:
                answers[qid] = {"type": "noul", "noul": 0.0}
        return {"model": "laya-rl-agent", "answers": answers}


def model(agent: FakeAgent, **kw: Any) -> LayaDecisionModel:
    return LayaDecisionModel(
        agent, model_id="convaiinnovations/laya", revision="55cf4c4ebb4e" + "x" * 28, **kw
    )


def test_adapter_satisfies_protocols_and_names_revision() -> None:
    m = model(FakeAgent())
    assert isinstance(m, DecisionModel) and isinstance(m, BatchScoringDecisionModel)
    assert m.name == "laya@55cf4c4ebb4e"


def test_choice_maps_options_verbatim_and_renormalises() -> None:
    agent = FakeAgent()
    opts = ["files", "communication", "databases"]
    r = model(agent).choice("read a.txt", "Which domain?", opts)
    assert agent.calls[0]["q"] == {
        "type": "choice",
        "instructions": "Which domain?",
        "criteria": opts,
    }
    assert r.option == "files"
    assert list(r.probabilities) == opts
    assert math.isclose(sum(r.probabilities.values()), 1.0, abs_tol=1e-12)


def test_score_level_is_argmax_index() -> None:
    r = model(FakeAgent()).score("s", "How relevant?", ["none", "some", "high"])
    assert r.level == 2 and len(r.probabilities) == 3


def test_score_batch_is_one_forward_pass() -> None:
    agent = FakeAgent()
    out = model(agent).score_batch("s", ["q1", "q2", "q3"], ["no", "yes"])
    assert len(agent.calls) == 1 and len(agent.calls[0]) == 3
    assert [r.level for r in out] == [1, 1, 1]
    assert model(agent).score_batch("s", [], ["no", "yes"]) == []


def test_noul_defaults_to_neutral_two_option_choice() -> None:
    agent = FakeAgent()
    p = model(agent).noul("read a file", "Is this about files?")
    q = agent.calls[0]["q"]
    assert q["type"] == "choice" and q["criteria"] == {"A": "yes", "B": "no"}
    assert math.isclose(p, 0.7, abs_tol=1e-9)


def test_noul_native_mode() -> None:
    agent = FakeAgent()
    assert model(agent, noul_mode="native").noul("s", "q?") == 0.0
    assert agent.calls[0]["q"]["type"] == "noul"
    with pytest.raises(ValueError):
        model(agent, noul_mode="bogus")


def test_runtime_failure_is_typed_and_curated() -> None:
    with pytest.raises(DecisionRuntimeError) as ei:
        model(FakeAgent(fail=True)).choice("s", "q", ["a", "b"])
    assert "secret" not in str(ei.value) and "CUDA" not in str(ei.value)


def test_validation_catches_a_novel_option_from_the_real_adapter() -> None:
    raw = model(FakeAgent(novel=True)).choice("s", "q", ["a", "b"])
    assert raw.option == "invented_tool"  # the adapter passes the model's word through...
    with pytest.raises(DecisionProtocolError):  # ...and the engine's wrapper refuses it
        ValidatedDecisionModel(model(FakeAgent(novel=True))).choice("s", "q", ["a", "b"])


def test_load_without_laya_installed_raises_typed_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "laya", None)  # import laya -> ImportError
    with pytest.raises(Unavailable, match="not installed"):
        LayaDecisionModel.load()


# ------------------------------------------------------------------- slow
@pytest.fixture(scope="module")
def real_laya() -> LayaDecisionModel:
    t0 = time.perf_counter()
    m = LayaDecisionModel.load(device="cpu")
    print(f"\n[laya] loaded {m.name} on {m.device} in {time.perf_counter() - t0:.2f}s")
    return m


@pytest.mark.slow
@slow
def test_real_laya_choice_on_cpu(real_laya: LayaDecisionModel) -> None:
    guarded = ValidatedDecisionModel(real_laya)
    options = ["files", "communication"]
    t0 = time.perf_counter()
    r = guarded.choice("read the file config.yaml from my project", "Which domain?", options)
    ms = (time.perf_counter() - t0) * 1000
    print(f"[laya] choice -> {r.option} {r.probabilities} in {ms:.1f}ms")
    assert r.option in options
    assert r.option == "files"
    assert math.isclose(sum(r.probabilities.values()), 1.0, abs_tol=1e-6)


@pytest.mark.slow
@slow
def test_real_laya_score_noul_and_batch(real_laya: LayaDecisionModel) -> None:
    guarded = ValidatedDecisionModel(real_laya)
    state = "list my open github issues"
    levels = ["irrelevant", "partly relevant", "relevant", "exact match"]
    hi, lo = guarded.score_batch(
        state,
        [
            "How relevant is the tool 'list_issues: list GitHub issues for a repository'?",
            "How relevant is the tool 'send_email: send an email message'?",
        ],
        levels,
    )
    print(f"[laya] score hi={hi.level} lo={lo.level}")
    assert hi.level > lo.level
    yes = guarded.noul(state, "Can the tool 'list_issues: list GitHub issues' do this task?")
    no = guarded.noul(state, "Can the tool 'book_flight: book an airline flight' do this task?")
    print(f"[laya] noul yes={yes:.3f} no={no:.3f}")
    assert yes > 0.5 > no


class Scripted:
    """Agent returning a fixed (malformed) payload."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def predict(self, state: str, questions: dict[str, Any]) -> Any:
        return self.payload


@pytest.mark.parametrize(
    ("payload", "call"),
    [
        ({"answers": {}}, "choice"),  # missing q
        ({"answers": {"q": {"probabilities": {"a": 0.5, "b": 0.5}}}}, "choice"),  # no choice
        ({"answers": {"q": {"choice": "a", "probabilities": {"a": "x", "b": 0.5}}}}, "choice"),
        ({"answers": {"q": None}}, "choice"),
        ({"answers": {"q0": {"probabilities": {"0": 0.5, "1": 0.5}}}}, "score_batch"),  # no q1
        ({"answers": {"q": {"probabilities": {"0": 1.0}}}}, "score"),  # missing level 1
        ({"answers": {"q": {"type": "choice"}}}, "noul"),
        ({"answers": {"q": {}}}, "noul_native"),
        (None, "choice"),
    ],
)
def test_malformed_answers_become_curated_runtime_errors(payload: Any, call: str) -> None:
    m = model(Scripted(payload))  # type: ignore[arg-type]
    if call == "noul_native":
        m = model(Scripted(payload), noul_mode="native")  # type: ignore[arg-type]
    calls = {
        "choice": lambda: m.choice("s", "q", ["a", "b"]),
        "score": lambda: m.score("s", "q", ["no", "yes"]),
        "score_batch": lambda: m.score_batch("s", ["q0", "q1"], ["no", "yes"]),
        "noul": lambda: m.noul("s", "q"),
        "noul_native": lambda: m.noul("s", "q"),
    }
    with pytest.raises(DecisionRuntimeError) as ei:
        calls[call]()
    assert str(ei.value) in {
        "decision model returned a malformed answer",
        "decision model inference failed",
        "decision model returned no answers",
    }


def test_load_warning_log_is_crlf_scrubbed(caplog: pytest.LogCaptureFixture) -> None:
    from mcprouter.inference.laya import _scrub

    assert _scrub("a\r\nforged line\rb\n") == "a  forged line b "
