"""The shared in-process inference runtime (SPEC §7, §11; scoping #1, #3).

One GPU is shared by the embedding model and the decision model, so both live
behind ONE engine per process:

* Device: `auto` -> cuda if torch is importable and `torch.cuda.is_available()`,
  else cpu. An explicit `cuda` request on a box without CUDA degrades to cpu
  with a curated note (CPU fallback is a spec requirement, not an error).
* Lifecycle: nothing loads at import or construction. `load()` builds both
  backends once; `unload()` drops them (and empties the CUDA cache). The first
  request loads lazily if nobody called `load()`.
* Fallbacks: an optional backend that raises ModelUnavailableError degrades to
  hash-v1 / deterministic-v1 and the curated reason is reported in health.
  EmbeddingDimensionError and unknown backend names are configuration errors
  and raise.
* Concurrency: one semaphore caps in-flight inference calls. Capacity by mode
  (MODE_CONCURRENCY): performance=4 (models resident, concurrent requests),
  balanced=2 (default; bounded GPU contention), battery=1 (serialised, least
  peak memory/power). Resized live by `set_mode()`.
* Battery idle unload: in battery mode models are unloaded after
  `idle_unload_s` without inference. Two triggers, neither polls the GPU:
  (1) a lazy check on every request and health probe; (2) ONE one-shot
  `threading.Timer`, armed when a request completes and re-armed for the
  remaining time if activity happened meanwhile (never while a request is
  active), so memory is actually released while idle instead of waiting for
  the next request. A request counts as active from the moment it holds model
  references (including while it waits for a slot), and never runs on a model
  that idle unload has dropped.
* Loads run outside the state lock: health probes never block on a cold
  load, and concurrent first requests share one load (`_load_lock`). The next request reloads (a cold
  load costs seconds — the deliberate battery-mode trade-off).
* Validation: the decision model handed to callers is always wrapped in
  ValidatedDecisionModel; nothing unvalidated escapes the engine.

Routing fallback contract: any InferenceError from `decision_model()` means
"use `fallback_decision_model()` for this request and set fallback_used".
"""

from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from importlib import metadata
from typing import Any

from mcprouter.inference.deterministic import DeterministicDecisionModel
from mcprouter.inference.errors import InferenceError, ModelUnavailableError
from mcprouter.inference.hash_backend import HashEmbeddingBackend
from mcprouter.inference.validation import ValidatedDecisionModel
from mcprouter.interfaces import ChoiceResult, DecisionModel, EmbeddingBackend, ScoreResult
from mcprouter.settings import Settings

log = logging.getLogger(__name__)

MODE_CONCURRENCY: dict[str, int] = {"performance": 4, "balanced": 2, "battery": 1}
DEFAULT_IDLE_UNLOAD_S = 300.0
DEFAULT_EMBED_BATCH = 32
EMBEDDING_BACKENDS = ("hash", "bge")
DECISION_BACKENDS = ("deterministic", "laya")

EmbeddingLoader = Callable[[Settings, str], EmbeddingBackend]
DecisionLoader = Callable[[Settings, str, EmbeddingBackend], DecisionModel]


# ----------------------------------------------------------------- helpers
def resolve_device(requested: str) -> tuple[str, str | None]:
    """-> (device, curated note or None). Never imports torch if `cpu` is asked."""
    req = requested.strip().lower()
    if req == "cpu":
        return "cpu", None
    if req not in ("auto", "cuda") and not req.startswith("cuda:"):
        raise ValueError("device must be auto, cpu, cuda or cuda:<index>")
    try:
        import torch
    except ImportError:
        return "cpu", (None if req == "auto" else "cuda requested but torch is not installed")
    if torch.cuda.is_available():
        return ("cuda" if req == "auto" else req), None
    return "cpu", (None if req == "auto" else "cuda requested but unavailable; using cpu")


def batched[T](items: Sequence[T], size: int) -> Iterator[list[T]]:
    """Consecutive chunks of at most `size` items, order preserved."""
    if size < 1:
        raise ValueError("batch size must be >= 1")
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def _current_rss_bytes() -> int | None:
    try:
        with open("/proc/self/statm", encoding="ascii") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _peak_rss_bytes() -> int | None:
    try:
        import resource
    except ImportError:  # native Windows; WSL2 is Linux and has it
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # Linux reports KiB


def memory_stats(device: str) -> dict[str, int | None]:
    """Process RSS always; CUDA allocator stats when on cuda. Never imports torch
    (reads it from sys.modules), so it is free on the zero-ML path."""
    stats: dict[str, int | None] = {
        "rssBytes": _current_rss_bytes(),
        "peakRssBytes": _peak_rss_bytes(),
    }
    torch: Any = sys.modules.get("torch")
    if device.startswith("cuda") and torch is not None and torch.cuda.is_available():
        idx = int(device.split(":", 1)[1]) if ":" in device else torch.cuda.current_device()
        stats["cudaAllocatedBytes"] = int(torch.cuda.memory_allocated(idx))
        stats["cudaReservedBytes"] = int(torch.cuda.memory_reserved(idx))
        stats["cudaMaxAllocatedBytes"] = int(torch.cuda.max_memory_allocated(idx))
        stats["cudaTotalBytes"] = int(torch.cuda.get_device_properties(idx).total_memory)
    return stats


def _pkg_version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


class ConcurrencyLimiter:
    """Counting semaphore with live resize and stats (threading.Semaphore can't resize)."""

    def __init__(self, capacity: int) -> None:
        self._cond = threading.Condition()
        self._capacity = capacity
        self._in_flight = 0
        self._waiting = 0
        self._acquired_total = 0
        self._peak = 0
        self._wait_s_total = 0.0

    def resize(self, capacity: int) -> None:
        with self._cond:
            self._capacity = capacity
            self._cond.notify_all()

    @contextmanager
    def slot(self) -> Iterator[None]:
        t0 = time.perf_counter()
        with self._cond:
            self._waiting += 1
            while self._in_flight >= self._capacity:
                self._cond.wait()
            self._waiting -= 1
            self._in_flight += 1
            self._acquired_total += 1
            self._peak = max(self._peak, self._in_flight)
            self._wait_s_total += time.perf_counter() - t0
        try:
            yield
        finally:
            with self._cond:
                self._in_flight -= 1
                self._cond.notify()

    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    def stats(self) -> dict[str, float | int]:
        with self._cond:
            n = self._acquired_total
            return {
                "capacity": self._capacity,
                "inFlight": self._in_flight,
                "waiting": self._waiting,
                "acquiredTotal": n,
                "peakInFlight": self._peak,
                "avgWaitMs": round(1000 * self._wait_s_total / n, 3) if n else 0.0,
            }


# ---------------------------------------------------------- default loaders
def _hub_cache(settings: Settings) -> str:
    return os.path.join(settings.models_cache_dir, "hub")


def default_embedding_loader(settings: Settings, device: str) -> EmbeddingBackend:
    from mcprouter.inference.bge_backend import BgeEmbeddingBackend

    return BgeEmbeddingBackend.load(
        settings.embedding_model_id, device=device, cache_dir=_hub_cache(settings)
    )


def default_decision_loader(
    settings: Settings, device: str, embedder: EmbeddingBackend
) -> DecisionModel:
    from mcprouter.inference.laya import LayaDecisionModel

    return LayaDecisionModel.load(
        settings.laya_model_id, device=device, cache_dir=_hub_cache(settings)
    )


# ------------------------------------------------------------------ facades
class _GatedEmbedder:
    """EmbeddingBackend facade: semaphore + idle bookkeeping + provenance check."""

    def __init__(self, engine: InferenceEngine, name: str) -> None:
        self._engine = engine
        self.name = name

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._engine.embed(texts, expect_backend=self.name)


class _GatedDecisionModel:
    """DecisionModel facade over the engine's validated model."""

    def __init__(self, engine: InferenceEngine, name: str) -> None:
        self._engine = engine
        self.name = name

    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        return self._engine.choice(state, question, options)

    def score(self, state: str, question: str, levels: list[str]) -> ScoreResult:
        return self._engine.score(state, question, levels)

    def score_batch(self, state: str, questions: list[str], levels: list[str]) -> list[ScoreResult]:
        return self._engine.score_batch(state, questions, levels)

    def noul(self, state: str, question: str) -> float:
        return self._engine.noul(state, question)


# ------------------------------------------------------------------- engine
class InferenceEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        idle_unload_s: float = DEFAULT_IDLE_UNLOAD_S,
        embed_batch_size: int = DEFAULT_EMBED_BATCH,
        embedding_loader: EmbeddingLoader | None = None,
        decision_loader: DecisionLoader | None = None,
        clock: Callable[[], float] = time.monotonic,
        idle_timer: bool = True,
    ) -> None:
        if settings.operating_mode not in MODE_CONCURRENCY:
            raise ValueError(f"operating mode must be one of {sorted(MODE_CONCURRENCY)}")
        self._settings = settings
        self._mode = settings.operating_mode
        self._idle_unload_s = idle_unload_s
        self._embed_batch = embed_batch_size
        self._embedding_loader = embedding_loader or default_embedding_loader
        self._decision_loader = decision_loader or default_decision_loader
        self._clock = clock
        self._idle_timer_enabled = idle_timer
        self._lock = threading.RLock()  # guards published state; never held during a load
        self._load_lock = threading.Lock()  # serialises loads; health/requests never wait on it
        self._active = 0  # requests between ref-grab and completion (incl. waiting for a slot)
        self._limiter = ConcurrencyLimiter(MODE_CONCURRENCY[self._mode])
        self._embedder: EmbeddingBackend | None = None
        self._decider: ValidatedDecisionModel | None = None
        self._fallback = ValidatedDecisionModel(DeterministicDecisionModel())
        self._device = "cpu"
        self._device_note: str | None = None
        self._emb_info: dict[str, Any] = {}
        self._dec_info: dict[str, Any] = {}
        self._last_used = clock()
        self._idle_unloads = 0
        self._timer: threading.Timer | None = None

    # -------------------------------------------------------------- lifecycle
    @property
    def loaded(self) -> bool:
        return self._embedder is not None and self._decider is not None

    def load(self) -> None:
        """Build both backends once. Runs OUTSIDE `_lock` (a cold load can take
        seconds, or minutes on a slow download), so health probes and the
        bookkeeping of in-flight requests never wait on it; the result is
        published atomically under `_lock`."""
        with self._load_lock:
            if self.loaded:
                return
            s = self._settings
            if s.embedding_backend not in EMBEDDING_BACKENDS:
                raise ValueError(f"embedding backend must be one of {EMBEDDING_BACKENDS}")
            if s.decision_backend not in DECISION_BACKENDS:
                raise ValueError(f"decision backend must be one of {DECISION_BACKENDS}")
            wants_ml = s.embedding_backend == "bge" or s.decision_backend == "laya"
            # Zero-ML config: never import torch just to pick a device.
            device, device_note = resolve_device(s.device) if wants_ml else ("cpu", None)

            t0 = time.perf_counter()
            embedder: EmbeddingBackend = HashEmbeddingBackend()
            emb_reason: str | None = None
            if s.embedding_backend == "bge":
                try:
                    embedder = self._embedding_loader(s, device)
                except ModelUnavailableError as exc:
                    emb_reason = str(exc)  # our own curated message, never upstream text
                    log.warning("embedding backend degraded to hash-v1: %s", emb_reason)
            emb_info = self._describe(embedder, s.embedding_backend, emb_reason, t0)

            t0 = time.perf_counter()
            ml_embedder = None if isinstance(embedder, HashEmbeddingBackend) else embedder
            decider: DecisionModel = DeterministicDecisionModel(embedder=ml_embedder)
            dec_reason: str | None = None
            if s.decision_backend == "laya":
                try:
                    decider = self._decision_loader(s, device, embedder)
                except ModelUnavailableError as exc:
                    dec_reason = str(exc)
                    log.warning("decision backend degraded to deterministic-v1: %s", dec_reason)
            dec_info = self._describe(decider, s.decision_backend, dec_reason, t0)

            with self._lock:
                self._device, self._device_note = device, device_note
                self._emb_info, self._dec_info = emb_info, dec_info
                self._embedder = embedder
                self._decider = ValidatedDecisionModel(decider)
                self._last_used = self._clock()

    def unload(self) -> None:
        with self._lock:
            self._cancel_timer()
            self._embedder = None
            self._decider = None
            gc.collect()
            torch: Any = sys.modules.get("torch")
            if torch is not None and self._device.startswith("cuda") and torch.cuda.is_available():
                torch.cuda.empty_cache()

    @staticmethod
    def _describe(model: Any, requested: str, reason: str | None, t0: float) -> dict[str, Any]:
        return {
            "backend": model.name,
            "requested": requested,
            "modelId": getattr(model, "model_id", None),
            "revision": getattr(model, "revision", None),
            "precision": getattr(model, "precision", None)
            or ("float16" if getattr(model, "fp16", False) else "float32"),
            "fallbackUsed": reason is not None,
            "fallbackReason": reason,
            "loadSeconds": round(time.perf_counter() - t0, 3),
        }

    # ------------------------------------------------------------------ modes
    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in MODE_CONCURRENCY:
            raise ValueError(f"operating mode must be one of {sorted(MODE_CONCURRENCY)}")
        with self._lock:
            self._mode = mode
            self._limiter.resize(MODE_CONCURRENCY[mode])
            if mode == "battery":
                self._arm_timer(self._idle_unload_s)
            else:
                self._cancel_timer()

    def check_idle(self) -> bool:
        """Lazy idle check (no polling). Returns True if it unloaded."""
        with self._lock:
            if (
                self._mode == "battery"
                and self.loaded
                and self._active == 0
                and self._clock() - self._last_used >= self._idle_unload_s
            ):
                self.unload()
                self._idle_unloads += 1
                log.info("battery mode: models unloaded after %.0fs idle", self._idle_unload_s)
                return True
            return False

    def _arm_timer(self, delay: float) -> None:
        if not self._idle_timer_enabled or self._timer is not None:
            return
        timer = threading.Timer(max(delay, 0.0), self._on_timer)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_timer(self) -> None:
        with self._lock:
            # A timer that was cancelled while blocked on the lock must not clear
            # (and thereby orphan) the timer that replaced it.
            if self._timer is not threading.current_thread():
                return
            self._timer = None
            if self._mode != "battery" or not self.loaded:
                return
            if self._active:
                return  # busy: the request's completion re-arms; no polling while busy
            if not self.check_idle():
                remaining = self._idle_unload_s - (self._clock() - self._last_used)
                self._arm_timer(max(remaining, 0.05))

    # ------------------------------------------------------------- requests
    @contextmanager
    def _request(self) -> Iterator[tuple[EmbeddingBackend, ValidatedDecisionModel]]:
        while True:
            with self._lock:
                self.check_idle()
                if self._embedder is not None and self._decider is not None:
                    embedder, decider = self._embedder, self._decider
                    # Counted as active from the moment it holds refs (including
                    # while it waits for a limiter slot), and its start is a use:
                    # idle unload can never pull models from under a request.
                    self._active += 1
                    self._last_used = self._clock()
                    break
            self.load()  # outside _lock; raises on configuration errors
        try:
            with self._limiter.slot():
                yield embedder, decider
        finally:
            with self._lock:
                self._active -= 1
                self._last_used = self._clock()
                if self._mode == "battery":
                    self._arm_timer(self._idle_unload_s)

    def embed(self, texts: list[str], *, expect_backend: str | None = None) -> list[list[float]]:
        with self._request() as (embedder, _):
            if expect_backend is not None and embedder.name != expect_backend:
                raise InferenceError(
                    "embedding backend changed since this handle was issued; re-acquire it"
                )
            out: list[list[float]] = []
            for chunk in batched(texts, self._embed_batch):
                out.extend(embedder.embed(chunk))
            return out

    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        with self._request() as (_, decider):
            return decider.choice(state, question, options)

    def score(self, state: str, question: str, levels: list[str]) -> ScoreResult:
        with self._request() as (_, decider):
            return decider.score(state, question, levels)

    def score_batch(self, state: str, questions: list[str], levels: list[str]) -> list[ScoreResult]:
        with self._request() as (_, decider):
            return decider.score_batch(state, questions, levels)

    def noul(self, state: str, question: str) -> float:
        with self._request() as (_, decider):
            return decider.noul(state, question)

    # -------------------------------------------------------------- handles
    def embedding_backend(self) -> _GatedEmbedder:
        """EmbeddingBackend for callers (pipeline, retrieval). Name = live provenance."""
        with self._lock:
            if not self.loaded:
                self.load()
            assert self._embedder is not None
            return _GatedEmbedder(self, self._embedder.name)

    def decision_model(self) -> _GatedDecisionModel:
        with self._lock:
            if not self.loaded:
                self.load()
            assert self._decider is not None
            return _GatedDecisionModel(self, self._decider.name)

    def fallback_decision_model(self) -> ValidatedDecisionModel:
        """Always-available deterministic model for per-request fallback."""
        return self._fallback

    def _embedder_raw(self) -> EmbeddingBackend | None:
        """Test seam: the unwrapped embedding backend."""
        return self._embedder

    # --------------------------------------------------------------- health
    def health(self, *, check_idle: bool = True) -> dict[str, Any]:
        if check_idle:
            self.check_idle()
        with self._lock:
            loaded = self.loaded
            emb: dict[str, Any] = dict(self._emb_info) if loaded else {"backend": None}
            dec: dict[str, Any] = dict(self._dec_info) if loaded else {"backend": None}
            emb["requested"] = self._settings.embedding_backend
            dec["requested"] = self._settings.decision_backend
            degraded = bool(emb.get("fallbackUsed") or dec.get("fallbackUsed") or self._device_note)
            return {
                "status": "degraded" if degraded else "ok",
                "loaded": loaded,
                "mode": self._mode,
                "requestedDevice": self._settings.device,
                "device": self._device,
                "deviceNote": self._device_note,
                "embedding": emb,
                "decision": dec,
                "concurrency": self._limiter.stats(),
                "memory": memory_stats(self._device),
                "idleUnload": {
                    "enabled": self._mode == "battery",
                    "idleSeconds": self._idle_unload_s,
                    "idleUnloads": self._idle_unloads,
                    "secondsSinceLastUse": round(self._clock() - self._last_used, 3),
                },
                "versions": {
                    "torch": _pkg_version("torch"),
                    "transformers": _pkg_version("transformers"),
                    "sentenceTransformers": _pkg_version("sentence-transformers"),
                    "laya": _pkg_version("laya"),
                },
            }


# ------------------------------------------------------ process singleton
_PROCESS_ENGINE: InferenceEngine | None = None
_PROCESS_LOCK = threading.Lock()


def get_process_engine(settings: Settings) -> InferenceEngine:
    """The one engine for this process (first caller's settings win). Does not load."""
    global _PROCESS_ENGINE
    with _PROCESS_LOCK:
        if _PROCESS_ENGINE is None:
            _PROCESS_ENGINE = InferenceEngine(settings)
        return _PROCESS_ENGINE


def reset_process_engine() -> None:
    """Unload and forget the process engine (tests, shutdown)."""
    global _PROCESS_ENGINE
    with _PROCESS_LOCK:
        if _PROCESS_ENGINE is not None:
            _PROCESS_ENGINE.unload()
        _PROCESS_ENGINE = None
