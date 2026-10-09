"""BGE backend. Unit tests cover the guards; @slow loads the real model on CPU.

Run with:  MCPR_RUN_SLOW=1 HF_HOME=<cache> pytest -q -m slow tests/
"""

from __future__ import annotations

import math
import os
import sys
from typing import Any, cast

import pytest

from mcprouter.inference import bge_backend
from mcprouter.inference.bge_backend import BgeEmbeddingBackend, backend_name_for
from mcprouter.inference.errors import EmbeddingDimensionError, ModelUnavailableError
from mcprouter.interfaces import EmbeddingBackend

slow = pytest.mark.skipif(
    not os.environ.get("MCPR_RUN_SLOW"), reason="slow: set MCPR_RUN_SLOW=1 (downloads/loads models)"
)


def test_backend_name_fits_provenance_column() -> None:
    assert backend_name_for("BAAI/bge-small-en-v1.5") == "bge-small-en-v1.5"
    assert len(backend_name_for("org/" + "x" * 64)) == 20


def test_missing_extra_is_typed_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(ModelUnavailableError, match="not installed"):
        BgeEmbeddingBackend.load()


def test_wrong_dimension_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("sentence_transformers")

    class Wide:
        def __init__(self, *a: Any, **kw: Any) -> None:
            self.kw = kw

        def half(self) -> None: ...
        def eval(self) -> None: ...
        def get_embedding_dimension(self) -> int:
            return 768

    import sentence_transformers

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", Wide)
    with pytest.raises(EmbeddingDimensionError):
        BgeEmbeddingBackend.load(device="cpu")


def test_load_pins_revision_and_never_trusts_remote_code(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("sentence_transformers")
    seen: dict[str, Any] = {}

    class Boom:
        def __init__(self, *a: Any, **kw: Any) -> None:
            seen.update(kw)
            raise OSError("401 for https://huggingface.co token=hf_secret")

    import sentence_transformers

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", Boom)
    with pytest.raises(ModelUnavailableError) as ei:
        BgeEmbeddingBackend.load(device="cpu")
    assert seen["revision"] == bge_backend.BGE_PINNED_REVISIONS["BAAI/bge-small-en-v1.5"]
    assert seen["trust_remote_code"] is False
    assert "hf_secret" not in str(ei.value)


@pytest.fixture(scope="module")
def real_bge() -> BgeEmbeddingBackend:
    cache = os.path.join(os.environ.get("HF_HOME", "./models-cache"), "hub")
    return BgeEmbeddingBackend.load(device="cpu", cache_dir=cache)


@pytest.mark.slow
@slow
def test_real_bge_on_cpu(real_bge: BgeEmbeddingBackend) -> None:
    assert isinstance(real_bge, EmbeddingBackend)
    assert real_bge.name == "bge-small-en-v1.5" and not real_bge.fp16
    q, near, far = real_bge.embed(
        ["open a document on disk", "read_file: read file contents", "send_email: send an email"]
    )
    assert len(q) == 384 and math.isclose(sum(x * x for x in q), 1.0, rel_tol=1e-4)

    def cos(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    # Semantic, not lexical: "document on disk" shares no token with "read_file".
    assert cos(q, near) > cos(q, far)
    assert real_bge.embed(["same text"]) == real_bge.embed(["same text"])


def test_post_construct_failure_degrades_with_curated_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("sentence_transformers")

    class OomOnProbe:
        def __init__(self, *a: Any, **kw: Any) -> None: ...
        def half(self) -> None: ...
        def eval(self) -> None: ...
        def get_embedding_dimension(self) -> int:
            return 384

        def encode(self, *a: Any, **kw: Any) -> Any:
            raise RuntimeError("CUDA out of memory at /secret/path")

    import sentence_transformers

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", OomOnProbe)
    with pytest.raises(ModelUnavailableError) as ei:
        BgeEmbeddingBackend.load(device="cpu")
    assert "secret" not in str(ei.value)


def test_runtime_embed_failure_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("torch")
    from mcprouter.inference.errors import EmbeddingRuntimeError

    class Broken:
        def encode(self, *a: Any, **kw: Any) -> Any:
            raise RuntimeError("driver error /secret/path")

    be = BgeEmbeddingBackend(
        cast(Any, Broken()), model_id="BAAI/bge-small-en-v1.5", revision=None, device="cpu"
    )
    with pytest.raises(EmbeddingRuntimeError) as ei:
        be.embed(["x"])
    assert "secret" not in str(ei.value)
