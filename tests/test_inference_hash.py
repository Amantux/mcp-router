"""Hash embedding backend (always-on fallback) + zero-ML import safety."""

from __future__ import annotations

import math

from mcprouter.inference.hash_backend import HashEmbeddingBackend
from mcprouter.interfaces import EmbeddingBackend
from mcprouter.models import EMBEDDING_DIM


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_hash_backend_satisfies_protocol_and_shape() -> None:
    be = HashEmbeddingBackend()
    assert isinstance(be, EmbeddingBackend)
    assert be.name == "hash-v1"
    vecs = be.embed(["read a file from disk", "send a slack message", ""])
    assert len(vecs) == 3
    for v in vecs:
        assert len(v) == EMBEDDING_DIM
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-9)


def test_hash_backend_is_deterministic_across_instances() -> None:
    a = HashEmbeddingBackend().embed(["List open GitHub issues"])
    b = HashEmbeddingBackend().embed(["List open GitHub issues"])
    assert a == b


def test_hash_backend_similarity_tracks_lexical_overlap() -> None:
    be = HashEmbeddingBackend()
    q, near, far = be.embed(
        ["read the contents of a file", "read_file: read file contents from disk", "post message"]
    )
    assert _cos(q, near) > _cos(q, far)
    assert _cos(q, near) > 0.3


def test_hash_backend_empty_input_is_unit_not_zero() -> None:
    # A zero vector makes pgvector cosine distance NaN; empty text must still be unit-norm.
    (v,) = HashEmbeddingBackend().embed(["   "])
    assert any(x != 0.0 for x in v)
