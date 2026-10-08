"""Protocol enforcement around ANY DecisionModel.

FR-03 says the decision model must never invent tool names. The protocol makes
that true by construction only if every implementation obeys it, so this
wrapper checks rather than trusts: the option must be one of the supplied
strings verbatim, probabilities must be a distribution over exactly the
supplied options/levels, `noul` must be a probability. Anything else raises
DecisionProtocolError and the result is discarded.

The wrapped model receives a COPY of the option list, so a misbehaving model
cannot mutate the caller's list (and an option it "adds" to its copy is still
rejected, because membership is checked against the caller's original).

The engine always hands routing a ValidatedDecisionModel — never a raw one.
"""

from __future__ import annotations

import math

from mcprouter.inference.errors import DecisionProtocolError
from mcprouter.interfaces import (
    BatchScoringDecisionModel,
    ChoiceResult,
    DecisionModel,
    ScoreResult,
)


def _sum_tolerance(k: int) -> float:
    # Laya rounds each probability to 4 dp; allow that rounding residue plus slack.
    return 1e-3 + 5e-5 * k


def _is_prob(p: object) -> bool:
    return (
        isinstance(p, int | float)
        and not isinstance(p, bool)
        and math.isfinite(p)
        and 0.0 <= p <= 1.0
    )


def _check_distribution(probs: list[float], what: str) -> None:
    for p in probs:
        if not _is_prob(p):
            raise DecisionProtocolError(f"{what}: probability outside [0, 1]")
    if abs(sum(probs) - 1.0) > _sum_tolerance(len(probs)):
        raise DecisionProtocolError(f"{what}: probabilities do not sum to 1")


def _check_labels(labels: list[str], kind: str) -> None:
    """Caller errors (ValueError, deliberately not an InferenceError): falling
    back to another model cannot fix a malformed question. Options must be
    unique — pass qualified ids (e.g. "server/tool"), not bare tool names."""
    if not labels:
        raise ValueError(f"at least one {kind} is required")
    if len(set(labels)) != len(labels):
        raise ValueError(f"{kind}s must be unique")


def validate_choice(result: ChoiceResult, options: list[str]) -> ChoiceResult:
    if result.option not in options:
        raise DecisionProtocolError("choice: returned option is not in the supplied list")
    if set(result.probabilities) != set(options):
        raise DecisionProtocolError("choice: probabilities are not keyed by exactly the options")
    _check_distribution(list(result.probabilities.values()), "choice")
    return result


def validate_score(result: ScoreResult, levels: list[str]) -> ScoreResult:
    if type(result.level) is not int or not 0 <= result.level < len(levels):
        raise DecisionProtocolError("score: level index out of range")
    if len(result.probabilities) != len(levels):
        raise DecisionProtocolError("score: one probability per level is required")
    _check_distribution(list(result.probabilities), "score")
    return result


def validate_noul(p: float) -> float:
    if not _is_prob(p):
        raise DecisionProtocolError("noul: probability outside [0, 1]")
    return p


class ValidatedDecisionModel:
    """DecisionModel decorator that enforces the protocol on every answer."""

    def __init__(self, inner: DecisionModel) -> None:
        self.inner = inner
        self.name = inner.name

    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        _check_labels(options, "option")
        original = list(options)
        result = self.inner.choice(state, question, list(original))
        return validate_choice(result, original)

    def score(self, state: str, question: str, levels: list[str]) -> ScoreResult:
        _check_labels(levels, "level")
        original = list(levels)
        result = self.inner.score(state, question, list(original))
        return validate_score(result, original)

    def score_batch(self, state: str, questions: list[str], levels: list[str]) -> list[ScoreResult]:
        """Batched Score; uses the inner model's native batching when it has one."""
        _check_labels(levels, "level")
        original = list(levels)
        if isinstance(self.inner, BatchScoringDecisionModel):
            results = self.inner.score_batch(state, list(questions), list(original))
        else:
            results = [self.inner.score(state, q, list(original)) for q in questions]
        if len(results) != len(questions):
            raise DecisionProtocolError("score_batch: one result per question is required")
        return [validate_score(r, original) for r in results]

    def noul(self, state: str, question: str) -> float:
        return validate_noul(self.inner.noul(state, question))
