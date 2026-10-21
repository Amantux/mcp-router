"""Deterministic, dependency-free text -> sparse-vector embedding.

Production deployments swap this for a real embedding model (per
catalog_schema.md's `embedding.model` field / algorithm_design.md §3a) — the
rest of the pipeline only depends on the `Embedder` interface (`embed(text)
-> SparseVector`, `cosine(a, b) -> float`), so that swap touches nothing
else. This default keeps the implementation runnable with zero external
dependencies and fully deterministic (requirements §3 replayability).
"""

import hashlib
import math
import re
from typing import Dict

SparseVector = Dict[int, float]

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DEFAULT_DIM = 2048


def tokenize(text: str):
    return _TOKEN_RE.findall(text.lower())


def _bucket(token: str, dim: int) -> int:
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big") % dim


class HashingEmbedder:
    """Hashing-trick bag-of-words embedder with L2-normalized sparse output."""

    def __init__(self, dim: int = _DEFAULT_DIM):
        self.dim = dim

    def embed(self, text: str) -> SparseVector:
        counts: Dict[int, float] = {}
        for tok in tokenize(text):
            b = _bucket(tok, self.dim)
            counts[b] = counts.get(b, 0.0) + 1.0
        norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
        return {k: v / norm for k, v in counts.items()}

    @staticmethod
    def cosine(a: SparseVector, b: SparseVector) -> float:
        if not a or not b:
            return 0.0
        # iterate the smaller dict for speed
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b[k] for k, v in a.items() if k in b)


DEFAULT_EMBEDDER = HashingEmbedder()
