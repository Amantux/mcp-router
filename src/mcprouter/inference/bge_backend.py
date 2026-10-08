"""BGE-small sentence-transformers embedding backend (optional [inference] extra).

* torch / sentence-transformers are imported inside `load()` only.
* Revision pinned to a verified commit by default (supply chain: the Hub's
  `main` can move under us); `trust_remote_code` is always False.
* FP16 only on CUDA (`model.half()`); CPU stays FP32 (CPU half is slow and
  lossy). Inference runs under `torch.inference_mode()`.
* The output width is checked at load — both the declared dimension and a
  real forward pass. A mismatch raises EmbeddingDimensionError, which the
  engine does NOT degrade on: wrong-width vectors must never reach pgvector.
* Vectors are L2-normalised (BGE is trained for cosine), so pgvector's cosine
  distance and a plain dot product agree.

Cache layout: `cache_dir` is a Hugging Face hub cache directory; the engine
passes `<MCPR_MODELS_CACHE_DIR>/hub` (the same layout HF_HOME uses).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from mcprouter.inference.errors import EmbeddingDimensionError, ModelUnavailableError
from mcprouter.models import EMBEDDING_DIM

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)

BGE_DEFAULT_MODEL_ID = "BAAI/bge-small-en-v1.5"
# Verified by download + dimension check on 2026-10-08.
BGE_PINNED_REVISIONS: dict[str, str] = {
    "BAAI/bge-small-en-v1.5": "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
}
_NAME_MAX = 20  # MCPToolRecord.embedding_backend is String(20)


def backend_name_for(model_id: str) -> str:
    """Provenance name stored per vector: the repo's basename, column-width safe."""
    return model_id.rsplit("/", 1)[-1][:_NAME_MAX]


class BgeEmbeddingBackend:
    """EmbeddingBackend over a loaded SentenceTransformer."""

    def __init__(
        self,
        model: SentenceTransformer,
        *,
        model_id: str,
        revision: str | None,
        device: str,
        batch_size: int = 32,
    ) -> None:
        self._model = model
        self.name = backend_name_for(model_id)
        self.model_id = model_id
        self.revision = revision
        self.device = device
        self.fp16 = device.startswith("cuda")
        self.batch_size = batch_size

    @classmethod
    def load(
        cls,
        model_id: str = BGE_DEFAULT_MODEL_ID,
        *,
        device: str = "cpu",
        cache_dir: str | None = None,
        revision: str | None = None,
        batch_size: int = 32,
    ) -> BgeEmbeddingBackend:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ModelUnavailableError(
                "embedding model unavailable: the [inference] extra is not installed"
            ) from exc

        pinned = revision or BGE_PINNED_REVISIONS.get(model_id)
        try:
            model = SentenceTransformer(
                model_id,
                device=device,
                cache_folder=cache_dir,
                revision=pinned,
                trust_remote_code=False,
            )
        except Exception as exc:  # noqa: BLE001 — any load failure degrades; curated message
            log.warning("embedding model load failed (%s)", type(exc).__name__)
            raise ModelUnavailableError("embedding model could not be loaded") from exc

        if device.startswith("cuda"):
            model.half()
        model.eval()

        declared = model.get_embedding_dimension()
        if declared != EMBEDDING_DIM:
            raise EmbeddingDimensionError(
                f"embedding model produces {declared}-dim vectors; the catalog requires "
                f"{EMBEDDING_DIM}"
            )
        backend = cls(
            model, model_id=model_id, revision=pinned, device=device, batch_size=batch_size
        )
        (probe,) = backend.embed(["dimension probe"])
        if len(probe) != EMBEDDING_DIM:
            raise EmbeddingDimensionError(
                f"embedding model forward produced {len(probe)}-dim vectors; the catalog "
                f"requires {EMBEDDING_DIM}"
            )
        return backend

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        import numpy as np
        import torch

        with torch.inference_mode():
            out = self._model.encode(
                texts,
                batch_size=self.batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        # FP16 on CUDA comes back as float16; store float32 like every other backend.
        arr = np.asarray(cast(Any, out), dtype=np.float32)
        if arr.ndim != 2 or arr.shape[1] != EMBEDDING_DIM:
            raise EmbeddingDimensionError(f"embedding forward returned shape {tuple(arr.shape)}")
        return cast(list[list[float]], arr.tolist())
