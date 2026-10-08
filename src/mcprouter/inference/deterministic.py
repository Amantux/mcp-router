"""Zero-ML DecisionModel fallback (name "deterministic-v1").

Same protocol as Laya, so routing never branches on which backend is live.
Similarity is a weighted-bag cosine over word unigrams (light plural
stemming, stopwords dropped) and character trigrams, optionally blended 50/50
with embedding cosine when an EmbeddingBackend is supplied.

* choice(): softmax(similarity(state, option) / T) over the supplied options.
* score():  relevance r = similarity(state, question), scaled to the level
  axis; probabilities are a discretised Gaussian around r*(k-1).
* noul():   sigmoid around a fixed threshold on the MAX similarity between the
  state and any line of `question` (one candidate per line -> "does anything
  match?" for no-match detection).

Probabilities are strictly positive and sum to 1, but they are heuristic
("calibrated-ish"), not fitted: `fallback_used` flags them on the wire
(scoping #10). Ties resolve to the first supplied option (stable).
"""

from __future__ import annotations

import math

from mcprouter.inference.hash_backend import tokenize
from mcprouter.interfaces import ChoiceResult, EmbeddingBackend, ScoreResult

_STOPWORDS = frozenset(
    "a an and are as at be by can do for from i in into is it me my of on or our please "
    "some that the this to with you your".split()
)
_TRIGRAM_W = 0.25
_CHOICE_TEMPERATURE = 0.1
_SCORE_FULL_SCALE = 0.5  # lexical cosine at/above this maps to the top level
_SCORE_SIGMA = 0.6
_NOUL_THRESHOLD = 0.15
_NOUL_SLOPE = 0.04
_P_FLOOR = 1e-4


def _stem(tok: str) -> str:
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _bag(text: str) -> dict[str, float]:
    bag: dict[str, float] = {}
    for raw in tokenize(text):
        if raw in _STOPWORDS:
            continue
        tok = _stem(raw)
        bag["u:" + tok] = bag.get("u:" + tok, 0.0) + 1.0
        padded = f"#{tok}#"
        for i in range(len(padded) - 2):
            key = "c:" + padded[i : i + 3]
            bag[key] = bag.get(key, 0.0) + _TRIGRAM_W
    return bag


def _bag_cos(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(w * b[k] for k, w in a.items() if k in b)
    na = math.sqrt(sum(w * w for w in a.values()))
    nb = math.sqrt(sum(w * w for w in b.values()))
    return dot / (na * nb)


def _vec_cos(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)


def _softmax(xs: list[float]) -> list[float]:
    """Softmax with a probability floor, so no option is ever exactly 0 (long
    score scales would otherwise underflow) and the result still sums to 1."""
    m = max(xs)
    ex = [max(math.exp(x - m), _P_FLOOR) for x in xs]
    s = sum(ex)
    return [e / s for e in ex]


def _argmax(xs: list[float]) -> int:
    best = 0
    for i, x in enumerate(xs):
        if x > xs[best]:
            best = i
    return best


class DeterministicDecisionModel:
    """DecisionModel with zero ML dependencies."""

    name = "deterministic-v1"

    def __init__(self, embedder: EmbeddingBackend | None = None) -> None:
        self._embedder = embedder

    def _similarities(self, state: str, targets: list[str]) -> list[float]:
        sb = _bag(state)
        lex = [_bag_cos(sb, _bag(t)) for t in targets]
        if self._embedder is None:
            return lex
        vecs = self._embedder.embed([state, *targets])
        emb = [max(0.0, _vec_cos(vecs[0], v)) for v in vecs[1:]]
        return [0.5 * x + 0.5 * y for x, y in zip(lex, emb, strict=True)]

    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        sims = self._similarities(state, options)
        probs = _softmax([s / _CHOICE_TEMPERATURE for s in sims])
        best = _argmax(probs)
        return ChoiceResult(
            option=options[best],
            probabilities={o: p for o, p in zip(options, probs, strict=True)},
        )

    def score(self, state: str, question: str, levels: list[str]) -> ScoreResult:
        k = len(levels)
        if k == 1:
            return ScoreResult(level=0, probabilities=[1.0])
        (rel,) = self._similarities(state, [question])
        center = min(1.0, rel / _SCORE_FULL_SCALE) * (k - 1)
        logits = [-((i - center) ** 2) / (2 * _SCORE_SIGMA**2) for i in range(k)]
        probs = _softmax(logits)
        return ScoreResult(level=_argmax(probs), probabilities=probs)

    def score_batch(self, state: str, questions: list[str], levels: list[str]) -> list[ScoreResult]:
        return [self.score(state, q, levels) for q in questions]

    def noul(self, state: str, question: str) -> float:
        segments = [s for s in question.splitlines() if s.strip()] or [question]
        best = max(self._similarities(state, segments))
        z = (best - _NOUL_THRESHOLD) / _NOUL_SLOPE
        p = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))
        return min(1.0 - _P_FLOOR, max(_P_FLOOR, p))
