"""Deterministic DecisionModel + the protocol-validation wrapper every model runs behind."""

from __future__ import annotations

import math

import pytest

from mcprouter.inference.deterministic import DeterministicDecisionModel
from mcprouter.inference.errors import DecisionProtocolError
from mcprouter.inference.hash_backend import HashEmbeddingBackend
from mcprouter.inference.validation import ValidatedDecisionModel
from mcprouter.interfaces import ChoiceResult, DecisionModel, ScoreResult

FILE_Q = "read the file config.yaml from my project"


# ------------------------------------------------------------ deterministic
def test_deterministic_satisfies_protocol() -> None:
    m = DeterministicDecisionModel()
    assert isinstance(m, DecisionModel)
    assert m.name == "deterministic-v1"


@pytest.mark.parametrize("embedder", [None, HashEmbeddingBackend()])
def test_choice_picks_lexically_matching_option(embedder: HashEmbeddingBackend | None) -> None:
    m = DeterministicDecisionModel(embedder=embedder)
    options = ["communication", "files", "databases"]
    r = m.choice(FILE_Q, "Which domain does this task belong to?", options)
    assert r.option == "files"
    assert set(r.probabilities) == set(options)
    assert all(p > 0 for p in r.probabilities.values())
    assert math.isclose(sum(r.probabilities.values()), 1.0, abs_tol=1e-9)
    assert r.probabilities["files"] == max(r.probabilities.values())


def test_choice_without_signal_is_uniform_and_stable() -> None:
    r = DeterministicDecisionModel().choice("zzz qqq", "?", ["alpha", "beta"])
    assert r.option == "alpha"  # ties break to the first supplied option
    assert math.isclose(r.probabilities["alpha"], 0.5)


def test_choice_uses_embedding_similarity_when_supplied() -> None:
    class Aligned:
        """Puts the state on option B's axis; lexically there is no overlap at all."""

        name = "fake"

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] if t in ("state text", "option-b") else [0.0, 1.0] for t in texts]

    lexical = DeterministicDecisionModel().choice("state text", "?", ["option-a", "option-b"])
    assert lexical.option == "option-a"  # tie -> first
    embedded = DeterministicDecisionModel(embedder=Aligned()).choice(
        "state text", "?", ["option-a", "option-b"]
    )
    assert embedded.option == "option-b"


def test_score_scales_with_relevance() -> None:
    m = DeterministicDecisionModel()
    levels = ["irrelevant", "somewhat", "relevant", "exact"]
    hi = m.score(FILE_Q, "read_file: read the contents of a file in the project", levels)
    lo = m.score(FILE_Q, "send_message: post a chat message to a channel", levels)
    assert hi.level > lo.level
    for r in (hi, lo):
        assert len(r.probabilities) == len(levels)
        assert all(p > 0 for p in r.probabilities)
        assert math.isclose(sum(r.probabilities), 1.0, abs_tol=1e-9)
        assert r.probabilities[r.level] == max(r.probabilities)


def test_noul_thresholds_on_max_similarity() -> None:
    m = DeterministicDecisionModel()
    multi = "send_message: post a chat message\nread_file: read the contents of a file"
    assert m.noul(FILE_Q, multi) > 0.5  # max over candidate lines, not the average
    assert m.noul(FILE_Q, "send_message: post a chat message to a channel") < 0.5
    assert 0.0 < m.noul("", "") < 1.0


# --------------------------------------------------------------- validation
class _Hostile(DeterministicDecisionModel):
    """Returns a tool name it invented — exactly what FR-03 forbids."""

    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        return ChoiceResult(option="rm_rf_everything", probabilities={"rm_rf_everything": 1.0})


def test_wrapper_rejects_novel_option() -> None:
    raw = _Hostile().choice(FILE_Q, "?", ["files", "communication"])
    assert raw.option == "rm_rf_everything"  # unwrapped, the hostile answer gets through
    with pytest.raises(DecisionProtocolError):
        ValidatedDecisionModel(_Hostile()).choice(FILE_Q, "?", ["files", "communication"])


class _SneakyHostile(DeterministicDecisionModel):
    """Well-formed distribution over the real options, but a novel answer.

    Isolates the option-membership guard: the probability checks all pass here.
    """

    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        return ChoiceResult(
            option="exfiltrate", probabilities={o: 1 / len(options) for o in options}
        )


def test_wrapper_rejects_novel_option_even_with_valid_distribution() -> None:
    with pytest.raises(DecisionProtocolError, match="not in the supplied list"):
        ValidatedDecisionModel(_SneakyHostile()).choice(FILE_Q, "?", ["files", "communication"])


class _Mutator(DeterministicDecisionModel):
    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        options.append("injected")
        return ChoiceResult(option="injected", probabilities={o: 1 / len(options) for o in options})


def test_wrapper_shields_caller_options_and_catches_option_injection() -> None:
    opts = ["files", "communication"]
    with pytest.raises(DecisionProtocolError):
        ValidatedDecisionModel(_Mutator()).choice(FILE_Q, "?", opts)
    assert opts == ["files", "communication"]


@pytest.mark.parametrize(
    "probs",
    [
        {"files": 0.9},  # missing an option
        {"files": 0.9, "communication": 0.9},  # not a distribution
        {"files": float("nan"), "communication": 0.5},
        {"files": 1.2, "communication": -0.2},
        {"files": 0.5, "communication": 0.5, "extra": 0.0},
    ],
)
def test_wrapper_rejects_bad_choice_distributions(probs: dict[str, float]) -> None:
    class Bad(DeterministicDecisionModel):
        def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
            return ChoiceResult(option="files", probabilities=probs)

    with pytest.raises(DecisionProtocolError):
        ValidatedDecisionModel(Bad()).choice(FILE_Q, "?", ["files", "communication"])


@pytest.mark.parametrize(
    "result", [ScoreResult(level=3, probabilities=[0.5, 0.5]), ScoreResult(-1, [0.5, 0.5])]
)
def test_wrapper_rejects_bad_score(result: ScoreResult) -> None:
    class Bad(DeterministicDecisionModel):
        def score(self, state: str, question: str, levels: list[str]) -> ScoreResult:
            return result

    with pytest.raises(DecisionProtocolError):
        ValidatedDecisionModel(Bad()).score(FILE_Q, "?", ["no", "yes"])


@pytest.mark.parametrize("p", [1.5, -0.1, float("nan")])
def test_wrapper_rejects_bad_noul(p: float) -> None:
    class Bad(DeterministicDecisionModel):
        def noul(self, state: str, question: str) -> float:
            return p

    with pytest.raises(DecisionProtocolError):
        ValidatedDecisionModel(Bad()).noul(FILE_Q, "?")


@pytest.mark.parametrize("options", [[], ["a", "a"]])
def test_wrapper_rejects_malformed_option_lists(options: list[str]) -> None:
    with pytest.raises(ValueError):
        ValidatedDecisionModel(DeterministicDecisionModel()).choice(FILE_Q, "?", options)


def test_wrapper_passes_valid_results_through_and_keeps_name() -> None:
    w = ValidatedDecisionModel(DeterministicDecisionModel())
    assert isinstance(w, DecisionModel)
    assert w.name == "deterministic-v1"
    assert w.choice(FILE_Q, "?", ["communication", "files"]).option == "files"
