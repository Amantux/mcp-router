"""Inference runtime (SPEC §7).

Import-safety contract: NOTHING in this package imports torch, transformers,
sentence-transformers, laya, numpy or huggingface_hub at module scope. Those
are imported lazily inside the `load()` paths of the optional backends, so the
router runs with zero ML dependencies installed (hash embeddings +
deterministic decisions). tests/test_inference_hash.py enforces this in a
subprocess whose import system raises on those modules.
"""

from mcprouter.inference.errors import (
    DecisionProtocolError,
    EmbeddingDimensionError,
    InferenceError,
    ModelUnavailableError,
)

__all__ = [
    "DecisionProtocolError",
    "EmbeddingDimensionError",
    "InferenceError",
    "ModelUnavailableError",
]
