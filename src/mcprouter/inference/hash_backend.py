"""Always-on fallback embedding backend: zero ML imports, stdlib only.

Signed feature hashing ("hashing trick") into EMBEDDING_DIM buckets:

* features = word unigrams (w=1.0), word bigrams (w=0.5) and padded character
  trigrams of each word (w=0.25; gives "file" ~ "files" ~ "read_file" overlap
  without a stemmer);
* each feature -> sha256 -> bucket = first 4 bytes mod DIM, sign = bit 0 of
  byte 4 (signed hashing keeps collisions unbiased);
* L2-normalised, so cosine == dot product (pgvector `<=>` ready).

Deterministic across processes and machines (sha256, not Python's salted
`hash()`). Lexical only: it narrows by shared vocabulary, it does not know
synonyms — that is what the optional BGE backend is for. Vectors from this
backend are never compared with BGE vectors (provenance in
`MCPToolRecord.embedding_backend`).
"""

from __future__ import annotations

import hashlib
import math
import re

from mcprouter.models import EMBEDDING_DIM

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_UNIGRAM_W = 1.0
_BIGRAM_W = 0.5
_TRIGRAM_W = 0.25


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens; `_`, `-`, `.`, `/` and camelCase split."""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return _TOKEN_RE.findall(spaced.lower())


def _features(text: str) -> list[tuple[str, float]]:
    toks = tokenize(text)
    feats: list[tuple[str, float]] = [("u:" + t, _UNIGRAM_W) for t in toks]
    feats += [("b:" + a + "_" + b, _BIGRAM_W) for a, b in zip(toks, toks[1:], strict=False)]
    for t in toks:
        padded = f"#{t}#"
        feats += [("c:" + padded[i : i + 3], _TRIGRAM_W) for i in range(len(padded) - 2)]
    if not feats:
        # Empty/whitespace text still gets a deterministic unit vector: a zero
        # vector makes cosine distance NaN in pgvector.
        feats = [("e:<empty>", 1.0)]
    return feats


class HashEmbeddingBackend:
    """EmbeddingBackend implementation; name recorded as provenance."""

    name = "hash-v1"
    dim = EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * EMBEDDING_DIM
        for feat, weight in _features(text):
            digest = hashlib.sha256(feat.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[bucket] += sign * weight
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:  # every feature cancelled out by sign collisions (pathological)
            vec[0] = 1.0
            return vec
        return [x / norm for x in vec]
