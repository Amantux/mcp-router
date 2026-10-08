"""Shared inference runtime: device resolution, lifecycle, concurrency, modes, health."""

from __future__ import annotations

import threading
import time
import types
from collections.abc import Callable
from typing import Any

import pytest

from mcprouter.inference import engine as engine_mod
from mcprouter.inference.deterministic import DeterministicDecisionModel
from mcprouter.inference.engine import (
    MODE_CONCURRENCY,
    InferenceEngine,
    batched,
    get_process_engine,
    memory_stats,
    reset_process_engine,
    resolve_device,
)
from mcprouter.inference.errors import (
    DecisionProtocolError,
    EmbeddingDimensionError,
    InferenceError,
    ModelUnavailableError,
)
from mcprouter.inference.hash_backend import HashEmbeddingBackend
from mcprouter.interfaces import (
    BatchScoringDecisionModel,
    ChoiceResult,
    DecisionModel,
    EmbeddingBackend,
)
from mcprouter.settings import Settings


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class CountingEmbedder:
    def __init__(self, name: str = "fake-ml") -> None:
        self.name = name
        self.calls: list[int] = []
        self._inner = HashEmbeddingBackend()

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        return self._inner.embed(texts)


class Loaders:
    """Injectable loaders that count how often the engine builds models."""

    def __init__(
        self,
        *,
        embed_exc: Exception | None = None,
        decide_exc: Exception | None = None,
        decider: Callable[[], DecisionModel] | None = None,
    ) -> None:
        self.embed_loads = 0
        self.decide_loads = 0
        self.embed_exc = embed_exc
        self.decide_exc = decide_exc
        self.decider = decider or DeterministicDecisionModel
        self.devices: list[str] = []

    def embedding(self, settings: Settings, device: str) -> EmbeddingBackend:
        self.embed_loads += 1
        self.devices.append(device)
        if self.embed_exc:
            raise self.embed_exc
        return CountingEmbedder()

    def decision(
        self, settings: Settings, device: str, embedder: EmbeddingBackend
    ) -> DecisionModel:
        self.decide_loads += 1
        if self.decide_exc:
            raise self.decide_exc
        return self.decider()


def make(
    loaders: Loaders | None = None, *, mode: str = "balanced", **kw: Any
) -> tuple[InferenceEngine, Loaders]:
    ld = loaders or Loaders()
    eng = InferenceEngine(
        Settings(
            embedding_backend="bge", decision_backend="laya", operating_mode=mode, device="cpu"
        ),
        embedding_loader=ld.embedding,
        decision_loader=ld.decision,
        **kw,
    )
    return eng, ld


# ------------------------------------------------------------------ device
def test_resolve_device_on_cpu_box() -> None:
    assert resolve_device("cpu") == ("cpu", None)
    assert resolve_device("auto") == ("cpu", None)
    dev, note = resolve_device("cuda")
    assert dev == "cpu" and note == "cuda requested but unavailable; using cpu"


def test_resolve_device_prefers_cuda_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: True))
    monkeypatch.setitem(__import__("sys").modules, "torch", fake)
    assert resolve_device("auto") == ("cuda", None)
    assert resolve_device("cuda:0") == ("cuda:0", None)
    assert resolve_device("cpu") == ("cpu", None)


def test_resolve_device_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        resolve_device("tpu")


# --------------------------------------------------------------- lifecycle
def test_construction_never_loads_models() -> None:
    eng, ld = make()
    assert (ld.embed_loads, ld.decide_loads) == (0, 0)
    assert eng.health()["loaded"] is False


def test_load_is_once_and_unload_releases() -> None:
    eng, ld = make()
    eng.load()
    eng.load()
    assert (ld.embed_loads, ld.decide_loads) == (1, 1)
    assert ld.devices == ["cpu"]
    h = eng.health()
    assert h["loaded"] is True
    assert h["embedding"]["backend"] == "fake-ml" and not h["embedding"]["fallbackUsed"]
    assert h["decision"]["backend"] == "deterministic-v1"
    eng.unload()
    assert eng.health()["loaded"] is False
    eng.load()
    assert ld.embed_loads == 2


def test_first_request_loads_lazily() -> None:
    eng, ld = make()
    eng.embed(["x"])
    assert ld.embed_loads == 1


def test_unavailable_ml_backends_fall_back_with_curated_reason() -> None:
    eng, _ = make(
        Loaders(
            embed_exc=ModelUnavailableError("embedding model unavailable: extra not installed"),
            decide_exc=ModelUnavailableError("decision model could not be loaded"),
        )
    )
    eng.load()
    h = eng.health()
    assert h["status"] == "degraded"
    assert h["embedding"] == h["embedding"] | {
        "backend": "hash-v1",
        "requested": "bge",
        "fallbackUsed": True,
        "fallbackReason": "embedding model unavailable: extra not installed",
    }
    assert h["decision"]["backend"] == "deterministic-v1"
    assert h["decision"]["fallbackReason"] == "decision model could not be loaded"
    assert eng.choice(
        "read the file a.txt", "Which domain?", ["communication", "files"]
    ).option == ("files")


def test_wrong_dimension_fails_loudly_instead_of_degrading() -> None:
    eng, _ = make(Loaders(embed_exc=EmbeddingDimensionError("768-dim")))
    with pytest.raises(EmbeddingDimensionError):
        eng.load()


def test_unknown_backend_names_are_config_errors() -> None:
    eng = InferenceEngine(Settings(embedding_backend="word2vec"))
    with pytest.raises(ValueError):
        eng.load()
    with pytest.raises(ValueError):
        InferenceEngine(Settings(operating_mode="turbo"))


def test_engine_always_validates_the_decision_model() -> None:
    class Hostile(DeterministicDecisionModel):
        def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
            return ChoiceResult(
                option="invented", probabilities={o: 1 / len(options) for o in options}
            )

    eng, _ = make(Loaders(decider=Hostile))
    with pytest.raises(DecisionProtocolError):
        eng.choice("s", "q", ["a", "b"])
    with pytest.raises(DecisionProtocolError):
        eng.decision_model().choice("s", "q", ["a", "b"])


def test_decision_model_facade_is_protocol_complete() -> None:
    eng, _ = make()
    dm = eng.decision_model()
    assert isinstance(dm, DecisionModel) and isinstance(dm, BatchScoringDecisionModel)
    assert dm.name == "deterministic-v1"
    out = dm.score_batch("read a file", ["read_file: read a file", "send_message"], ["no", "yes"])
    assert [r.level for r in out] == [1, 0]
    fb = eng.fallback_decision_model()
    assert fb.name == "deterministic-v1" and isinstance(fb, BatchScoringDecisionModel)


def test_embedder_facade_refuses_stale_provenance() -> None:
    eng, ld = make()
    emb = eng.embedding_backend()
    assert isinstance(emb, EmbeddingBackend) and emb.name == "fake-ml"
    eng.unload()
    ld.embed_exc = ModelUnavailableError("gone")  # reload will fall back to hash-v1
    with pytest.raises(InferenceError):
        emb.embed(["x"])  # vectors would be stored under the wrong backend name


# ------------------------------------------------------------- concurrency
@pytest.mark.parametrize("mode", ["performance", "balanced", "battery"])
def test_semaphore_caps_concurrent_inference(mode: str) -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    class Slow(DeterministicDecisionModel):
        def noul(self, state: str, question: str) -> float:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return 0.5

    eng, _ = make(Loaders(decider=Slow), mode=mode)
    eng.load()
    threads = [threading.Thread(target=eng.noul, args=("s", "q")) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == MODE_CONCURRENCY[mode]
    stats = eng.health()["concurrency"]
    assert stats["capacity"] == MODE_CONCURRENCY[mode]
    assert stats["acquiredTotal"] == 8 and stats["inFlight"] == 0
    assert stats["peakInFlight"] == MODE_CONCURRENCY[mode]


def test_mode_concurrency_values() -> None:
    assert MODE_CONCURRENCY == {"performance": 4, "balanced": 2, "battery": 1}


def test_set_mode_resizes_semaphore() -> None:
    eng, _ = make(mode="performance")
    assert eng.health()["concurrency"]["capacity"] == 4
    eng.set_mode("battery")
    h = eng.health()
    assert h["mode"] == "battery" and h["concurrency"]["capacity"] == 1
    with pytest.raises(ValueError):
        eng.set_mode("turbo")


# ------------------------------------------------------- battery idle unload
def test_battery_mode_unloads_after_idle_on_lazy_check() -> None:
    clock = FakeClock()
    eng, ld = make(mode="battery", idle_unload_s=10.0, clock=clock, idle_timer=False)
    eng.embed(["x"])
    clock.t += 5
    eng.check_idle()
    assert eng.health()["loaded"] is True  # not idle long enough
    clock.t += 6
    h = eng.health()  # the health probe performs the lazy check too
    assert h["loaded"] is False and h["idleUnload"]["idleUnloads"] == 1
    eng.embed(["y"])  # next request reloads
    assert ld.embed_loads == 2


def test_non_battery_modes_never_idle_unload() -> None:
    clock = FakeClock()
    eng, _ = make(mode="performance", idle_unload_s=1.0, clock=clock, idle_timer=False)
    eng.embed(["x"])
    clock.t += 3600
    eng.check_idle()
    assert eng.health()["loaded"] is True


def test_battery_idle_timer_frees_models_without_a_request() -> None:
    eng, _ = make(mode="battery", idle_unload_s=0.15)
    eng.embed(["x"])
    deadline = time.monotonic() + 3.0
    while eng.health(check_idle=False)["loaded"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert eng.health(check_idle=False)["loaded"] is False
    eng.set_mode("balanced")  # cancels any pending timer


# ----------------------------------------------------------------- helpers
def test_embed_batches_and_preserves_order() -> None:
    eng, _ = make(embed_batch_size=32)
    eng.load()
    texts = [f"tool number {i}" for i in range(70)]
    out = eng.embed(texts)
    inner = eng._embedder_raw()
    assert isinstance(inner, CountingEmbedder)
    assert inner.calls == [32, 32, 6]
    assert out == HashEmbeddingBackend().embed(texts)


def test_batched_helper() -> None:
    assert list(batched([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(batched([], 3)) == []
    with pytest.raises(ValueError):
        list(batched([1], 0))


def test_memory_stats_cpu() -> None:
    m = memory_stats("cpu")
    assert m["rssBytes"] is None or m["rssBytes"] > 0
    assert m["peakRssBytes"] is None or m["peakRssBytes"] > 0
    assert "cudaAllocatedBytes" not in m


def test_health_exposes_no_paths_or_secrets() -> None:
    eng = InferenceEngine(
        Settings(
            agent_keys="agent:supersecret",
            database_url="postgresql+psycopg://u:dbpass@h/db",
            models_cache_dir="/very/private/cache",
        )
    )
    eng.load()
    blob = repr(eng.health())
    for needle in ("supersecret", "dbpass", "/very/private", "postgresql"):
        assert needle not in blob


def test_process_engine_is_a_singleton() -> None:
    reset_process_engine()
    try:
        a = get_process_engine(Settings())
        b = get_process_engine(Settings(operating_mode="battery"))
        assert a is b
        assert engine_mod._PROCESS_ENGINE is a
    finally:
        reset_process_engine()
