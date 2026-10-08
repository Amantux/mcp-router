"""Lazy engine handles for long-lived consumers (the routing pipeline and its
retriever), plus the per-call decision deadline.

* `EngineEmbedder` / the decision handle re-acquire the engine's current
  backend on every call, so a battery-mode unload/reload (or a reload onto a
  different backend) never strands a stale handle, and nothing loads at app
  construction (the lifespan loads; the first request loads lazily otherwise).
* `DeadlineDecisionModel` bounds every decision-model question. A sync model
  call cannot be interrupted, so it runs on a daemon worker thread and the
  caller stops waiting at the deadline: a HUNG model becomes
  `DecisionRuntimeError` (an `InferenceError`) and the routing pipeline takes
  its deterministic fallback (FR-06). Abandoned calls are bounded by
  `max_in_flight`; past it every call fails fast instead of piling up threads.
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, TypeVar

from mcprouter.inference.engine import InferenceEngine
from mcprouter.inference.errors import DecisionRuntimeError
from mcprouter.interfaces import ChoiceResult, DecisionModel, ScoreResult

T = TypeVar("T")

DEFAULT_MAX_IN_FLIGHT = 16
# Absolute monotonic deadline for the current request (set by serve.answer); every
# question shares it, so N questions cannot take N x timeout.
REQUEST_DEADLINE: ContextVar[float | None] = ContextVar("mcpr_decision_deadline", default=None)


class EngineEmbedder:
    """EmbeddingBackend over the engine's live backend (provenance-checked)."""

    def __init__(self, engine: InferenceEngine) -> None:
        self._engine = engine

    @property
    def name(self) -> str:
        return self._engine.embedding_backend().name

    @name.setter
    def name(self, value: str) -> None:  # protocol attribute; the engine owns it
        raise AttributeError("EngineEmbedder.name is derived from the engine")

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._engine.embedding_backend().embed(texts)


class DeadlineDecisionModel:
    """DecisionModel (with score_batch) whose every question has a deadline."""

    def __init__(
        self,
        provider: Callable[[], DecisionModel],
        timeout_s: float,
        *,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
        name_fn: Callable[[], str] | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("decision timeout must be > 0")
        self._provider = provider
        # `name` is read on the request thread OUTSIDE the deadline, so it must
        # never trigger a (possibly seconds-long) model load.
        self._name_fn = name_fn or (lambda: provider().name)
        self._timeout_s = timeout_s
        self._max_in_flight = max_in_flight
        self._in_flight = 0
        self._lock = threading.Lock()

    @classmethod
    def for_engine(cls, engine: InferenceEngine, timeout_s: float) -> DeadlineDecisionModel:
        requested = engine.requested_decision_backend

        def name() -> str:
            return engine.published_decision_name() or f"{requested} (not loaded)"

        return cls(engine.decision_model, timeout_s, name_fn=name)

    @property
    def name(self) -> str:
        return self._name_fn()

    @name.setter
    def name(self, value: str) -> None:  # protocol attribute; the provider owns it
        raise AttributeError("DeadlineDecisionModel.name is derived from the model")

    def _call(self, fn: Callable[[DecisionModel], T]) -> T:
        wait_s = self._timeout_s
        deadline = REQUEST_DEADLINE.get()
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DecisionRuntimeError("decision request deadline exceeded")
            wait_s = min(wait_s, remaining)
        with self._lock:
            if self._in_flight >= self._max_in_flight:
                raise DecisionRuntimeError("decision model is not responding")
            self._in_flight += 1
        box: dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                box["value"] = fn(self._provider())
            except BaseException as exc:  # noqa: BLE001 — re-raised in the caller's thread
                box["error"] = exc
            finally:
                with self._lock:
                    self._in_flight -= 1
                done.set()

        # Carry the caller's context (request deadline, decision hop) into the worker.
        ctx = contextvars.copy_context()
        threading.Thread(target=ctx.run, args=(run,), name="decision-call", daemon=True).start()
        if not done.wait(wait_s):
            raise DecisionRuntimeError(f"decision model timed out after {wait_s:g}s")
        if "error" in box:
            raise box["error"]
        value: T = box["value"]
        return value

    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        return self._call(lambda m: m.choice(state, question, options))

    def score(self, state: str, question: str, levels: list[str]) -> ScoreResult:
        return self._call(lambda m: m.score(state, question, levels))

    def score_batch(self, state: str, questions: list[str], levels: list[str]) -> list[ScoreResult]:
        def run(m: DecisionModel) -> list[ScoreResult]:
            batch = getattr(m, "score_batch", None)
            if batch is not None:
                out: list[ScoreResult] = batch(state, questions, levels)
                return out
            return [m.score(state, q, levels) for q in questions]

        return self._call(run)

    def noul(self, state: str, question: str) -> float:
        return self._call(lambda m: m.noul(state, question))
