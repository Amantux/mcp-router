"""Serve the System One decision wire shape from this router's own decider.

Pure translation: a validated request plus a DecisionModel in, the documented
answer shapes out (choice: choice/confidence/probabilities; score: expectation
over level indices, probabilities keyed "0".."n-1", legend; noul: noul). Score
questions sharing a scale go through one ``score_batch`` call.
"""

from __future__ import annotations

from typing import Any

from mcprouter.inference.adapters import DeadlineDecisionModel

MAX_STATE_CHARS = 32_768
MAX_QUESTIONS = 32
MAX_OPTIONS = 255
MAX_INSTRUCTION_CHARS = 4_096


class SystemOneRequestError(ValueError):
    """Curated 422 message; never echoes request content."""


def _options(key: str, qtype: str, criteria: Any) -> list[str]:
    if isinstance(criteria, dict):
        opts = list(criteria.keys())
    elif isinstance(criteria, list):
        opts = criteria
    else:
        raise SystemOneRequestError(f"question {key!r}: {qtype} criteria must be a list or object")
    if not 2 <= len(opts) <= MAX_OPTIONS:
        raise SystemOneRequestError(f"question {key!r}: needs 2..{MAX_OPTIONS} criteria")
    if not all(isinstance(o, str) and 0 < len(o) <= MAX_INSTRUCTION_CHARS for o in opts):
        raise SystemOneRequestError(f"question {key!r}: criteria must be non-empty strings")
    if len(set(opts)) != len(opts):
        raise SystemOneRequestError(f"question {key!r}: criteria must be unique")
    return [str(o) for o in opts]


def parse_questions(questions: dict[str, Any]) -> dict[str, tuple[str, str, list[str]]]:
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise SystemOneRequestError(f"questions: send 1..{MAX_QUESTIONS} questions")
    out: dict[str, tuple[str, str, list[str]]] = {}
    for key, q in questions.items():
        if not isinstance(q, dict):
            raise SystemOneRequestError(f"question {key!r}: must be an object")
        qtype, text = q.get("type"), q.get("instructions")
        if not isinstance(text, str) or not 0 < len(text) <= MAX_INSTRUCTION_CHARS:
            raise SystemOneRequestError(f"question {key!r}: instructions must be 1..4096 chars")
        if qtype == "noul":
            out[key] = ("noul", text, [])
        elif qtype in ("choice", "score"):
            out[key] = (qtype, text, _options(key, qtype, q.get("criteria")))
        else:
            raise SystemOneRequestError(f"question {key!r}: type must be choice, score or noul")
    return out


def answer(
    model: DeadlineDecisionModel, state: str, parsed: dict[str, tuple[str, str, list[str]]]
) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    scales: dict[tuple[str, ...], list[str]] = {}
    for key, (qtype, text, opts) in parsed.items():
        if qtype == "choice":
            r = model.choice(state, text, opts)
            answers[key] = {
                "type": "choice",
                "choice": r.option,
                "confidence": r.probabilities.get(r.option, 0.0),
                "probabilities": dict(r.probabilities),
            }
        elif qtype == "noul":
            answers[key] = {"type": "noul", "noul": float(model.noul(state, text))}
        else:
            scales.setdefault(tuple(opts), []).append(key)
    for levels, keys in scales.items():
        results = model.score_batch(state, [parsed[k][1] for k in keys], list(levels))
        for key, sr in zip(keys, results, strict=True):
            answers[key] = {
                "type": "score",
                "score": float(sum(i * p for i, p in enumerate(sr.probabilities))),
                "probabilities": {str(i): p for i, p in enumerate(sr.probabilities)},
                "legend": {str(i): lv for i, lv in enumerate(levels)},
            }
    return {k: answers[k] for k in parsed}


def approx_input_tokens(state: str, parsed: dict[str, tuple[str, str, list[str]]]) -> int:
    chars = len(state) + sum(len(t) + sum(len(o) for o in opts) for _, t, opts in parsed.values())
    return max(1, chars // 4)
