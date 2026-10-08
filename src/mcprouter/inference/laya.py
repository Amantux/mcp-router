"""Adapter: convaiinnovations/laya -> the DecisionModel protocol.

Empirically established 2026-10-08 (model card + installed `laya==0.4.0`
source + a CPU run; see docs/INTEGRATION_NOTES-inference.md):

* Laya ships its OWN pip package (`pip install laya`, Apache-2.0). The runtime
  lives in the package; the Hub repo supplies only weights/config/tokenizer
  (`rl_agent_config.json`, `model.safetensors`, `tokenizer/*`, `encoder/*`).
  No repo Python is ever executed (no trust_remote_code).
* API: `laya.Agent(path, device=...)` then
  `agent.predict(state, {qid: {"type": "choice"|"score"|"noul",
  "instructions": str, "criteria": ...}})["answers"][qid]`. All questions in
  one call share ONE forward pass (used by `score_batch`).
* choice answer: {"choice": label, "probabilities": {label: p}} — labels from a
  list `criteria` are the keys verbatim. score answer: {"score": expected
  value, "probabilities": {"0": p, ...}}. noul answer: {"noul": p_yes}.
  Probabilities are rounded to 4 dp; we renormalise the rounding residue.
* CUDA: Laya autocasts to fp16/bf16 itself; nothing to `.half()` here.

Choices made on that evidence:

* Weights are fetched at the reviewed commit SHA that `laya.revisions.
  PINNED_REVISIONS` publishes (opt-in in laya; always-on here).
* `noul_mode="choice"` (default): the model card's Honest Limits section says
  native `noul` can follow its own true/false labels instead of the state on
  the English checkpoint (laya#156) and recommends a two-option choice with
  neutral keys. Our CPU probe agreed: native noul saturated to exactly 0.0 on
  negatives; the choice form gave unsaturated, correctly ordered values.
  `noul_mode="native"` is kept for comparison on target data.
* score().level is the ARGMAX level (protocol: an index into the scale), not
  Laya's expected-value `score`.
"""

from __future__ import annotations

import logging
import time
import warnings
from typing import Any

from mcprouter.inference.errors import DecisionRuntimeError, ModelUnavailableError
from mcprouter.interfaces import ChoiceResult, ScoreResult

log = logging.getLogger(__name__)

LAYA_DEFAULT_MODEL_ID = "convaiinnovations/laya"
LAYA_FILES = ("rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*")
NOUL_MODES = ("choice", "native")
_NOUL_CRITERIA = {"A": "yes", "B": "no"}


def _renormalise(probs: list[float]) -> list[float]:
    s = sum(probs)
    if s <= 0.0:
        raise DecisionRuntimeError("decision model returned an empty distribution")
    return [p / s for p in probs]


class LayaDecisionModel:
    """DecisionModel over a loaded `laya.Agent`."""

    def __init__(
        self,
        agent: Any,
        *,
        model_id: str,
        revision: str | None,
        noul_mode: str = "choice",
        load_seconds: float | None = None,
        load_warnings: int = 0,
    ) -> None:
        if noul_mode not in NOUL_MODES:
            raise ValueError(f"noul_mode must be one of {NOUL_MODES}")
        self._agent = agent
        self.model_id = model_id
        self.revision = revision
        self.noul_mode = noul_mode
        self.name = f"laya@{(revision or 'unpinned')[:12]}"
        self.device = str(getattr(agent, "device", "cpu"))
        self.precision = str(getattr(agent, "dtype", "float32")).removeprefix("torch.")
        self.load_seconds = load_seconds
        self.load_warnings = load_warnings

    @classmethod
    def load(
        cls,
        model_id: str = LAYA_DEFAULT_MODEL_ID,
        *,
        device: str = "cpu",
        cache_dir: str | None = None,
        revision: str | None = None,
        subfolder: str | None = None,
        noul_mode: str = "choice",
    ) -> LayaDecisionModel:
        try:
            import laya  # type: ignore[import-untyped]  # ships no py.typed
            from huggingface_hub import snapshot_download
            from laya.revisions import PINNED_REVISIONS  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ModelUnavailableError(
                "decision model unavailable: the 'laya' package is not installed"
            ) from exc

        pin: str | None = revision or PINNED_REVISIONS.get(model_id)
        prefix = f"{subfolder}/" if subfolder else ""
        t0 = time.perf_counter()
        try:
            # Same allow_patterns online and offline: without them an offline call
            # reports the partial snapshot as incomplete.
            local_dir = snapshot_download(
                model_id,
                revision=pin,
                cache_dir=cache_dir,
                allow_patterns=[prefix + f for f in LAYA_FILES],
            )
        except Exception as exc:  # noqa: BLE001 — network/cache failure degrades; curated message
            log.warning("decision model download failed (%s)", type(exc).__name__)
            raise ModelUnavailableError("decision model weights could not be downloaded") from exc
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                agent = laya.Agent(local_dir, device=device, subfolder=subfolder)
        except Exception as exc:  # noqa: BLE001 — any load failure degrades; curated message
            log.warning("decision model load failed (%s)", type(exc).__name__)
            raise ModelUnavailableError("decision model could not be loaded") from exc
        for w in caught:
            # e.g. "checkpoint ships invalid temperatures ... using choice:11+ -> 0.5"
            log.warning("laya load warning: %s", str(w.message).replace("\n", " ")[:300])
        return cls(
            agent,
            model_id=model_id,
            revision=pin,
            noul_mode=noul_mode,
            load_seconds=time.perf_counter() - t0,
            load_warnings=len(caught),
        )

    # ------------------------------------------------------------ internals
    def _predict(self, state: str, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        try:
            out = self._agent.predict(state, questions)
            answers = out["answers"]
        except Exception as exc:  # noqa: BLE001 — runtime failure -> typed, curated
            log.warning("decision model inference failed (%s)", type(exc).__name__)
            raise DecisionRuntimeError("decision model inference failed") from exc
        if not isinstance(answers, dict):
            raise DecisionRuntimeError("decision model returned no answers")
        return answers

    @staticmethod
    def _score_from(answer: dict[str, Any], k: int) -> ScoreResult:
        raw = answer["probabilities"]
        probs = _renormalise([float(raw[str(i)]) for i in range(k)])
        level = max(range(k), key=lambda i: probs[i])
        return ScoreResult(level=level, probabilities=probs)

    # ------------------------------------------------------------- protocol
    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        ans = self._predict(
            state, {"q": {"type": "choice", "instructions": question, "criteria": list(options)}}
        )["q"]
        raw = ans["probabilities"]
        try:
            probs = _renormalise([float(raw[o]) for o in options])
        except KeyError as exc:
            raise DecisionRuntimeError("decision model answered with unknown options") from exc
        # Returned verbatim from OUR list (validation re-checks this upstream).
        chosen = str(ans["choice"])
        return ChoiceResult(
            option=chosen, probabilities={o: p for o, p in zip(options, probs, strict=True)}
        )

    def score(self, state: str, question: str, levels: list[str]) -> ScoreResult:
        ans = self._predict(
            state, {"q": {"type": "score", "instructions": question, "criteria": list(levels)}}
        )["q"]
        return self._score_from(ans, len(levels))

    def score_batch(self, state: str, questions: list[str], levels: list[str]) -> list[ScoreResult]:
        """Every question answered in ONE forward pass (Laya's native batching)."""
        if not questions:
            return []
        qs = {
            f"q{i}": {"type": "score", "instructions": q, "criteria": list(levels)}
            for i, q in enumerate(questions)
        }
        answers = self._predict(state, qs)
        return [self._score_from(answers[f"q{i}"], len(levels)) for i in range(len(questions))]

    def noul(self, state: str, question: str) -> float:
        if self.noul_mode == "native":
            ans = self._predict(state, {"q": {"type": "noul", "instructions": question}})["q"]
            return float(ans["noul"])
        ans = self._predict(
            state, {"q": {"type": "choice", "instructions": question, "criteria": _NOUL_CRITERIA}}
        )["q"]
        yes, no = _renormalise([float(ans["probabilities"]["A"]), float(ans["probabilities"]["B"])])
        return yes
