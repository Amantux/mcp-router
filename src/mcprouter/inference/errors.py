"""Typed inference errors. Messages are curated (safe to surface at an API
boundary): they never carry filesystem paths, upstream exception text, or
credentials."""

from __future__ import annotations


class InferenceError(Exception):
    """Base class for every error raised by mcprouter.inference."""


class ModelUnavailableError(InferenceError):
    """An optional ML backend cannot be used here (extra not installed, weights
    not downloadable, load failed). The engine catches this and degrades to the
    hash / deterministic fallback, recording the curated reason."""


class EmbeddingDimensionError(InferenceError):
    """A loaded embedding model does not produce EMBEDDING_DIM vectors.

    Deliberately NOT a ModelUnavailableError: storing wrong-width vectors would
    corrupt the catalog, so this is a configuration error that fails loudly
    instead of silently degrading."""


class DecisionRuntimeError(InferenceError):
    """A loaded ML decision model failed while answering (e.g. CUDA OOM that
    the runtime could not absorb). Callers fall back to the deterministic
    model for that request and set `fallback_used`."""


class DecisionProtocolError(InferenceError):
    """A DecisionModel returned something the protocol forbids (an option not
    in the supplied list, probabilities that are not a distribution, a level
    out of range). The result is discarded, never forwarded to routing."""
