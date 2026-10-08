"""Subsystem contracts. Six workstreams build against these — change them only
at integration, never unilaterally in a feature branch.

Design notes that are load-bearing:

* Laya is a NON-AUTOREGRESSIVE decision model (Choice/Score/Noul typed
  questions, calibrated probabilities, single forward pass). The DecisionModel
  protocol mirrors that exactly, which makes FR-03's "must not generate
  arbitrary tool names" true by construction: a decision model can only select
  among options the deterministic code supplies.
* The deterministic fallback implements the SAME protocol, so the routing
  pipeline is backend-agnostic and always works on CPU with zero ML deps.
* Policy filtering is NOT part of these protocols on purpose — it is plain
  code in the policy engine, runs after ranking, and cannot be swapped out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------- embeddings
@runtime_checkable
class EmbeddingBackend(Protocol):
    """Maps text -> fixed 384-dim vectors. Must be deterministic per backend."""

    name: str  # recorded in MCPToolRecord.embedding_backend

    def embed(self, texts: list[str]) -> list[list[float]]: ...


# ------------------------------------------------------------ decision model
@dataclass(frozen=True)
class ChoiceResult:
    option: str
    probabilities: dict[str, float]  # option -> calibrated probability


@dataclass(frozen=True)
class ScoreResult:
    level: int  # index into the ordered scale supplied
    probabilities: list[float]  # per-level


@runtime_checkable
class DecisionModel(Protocol):
    """Typed-question decision engine (Laya-shaped).

    `state` is the task/context text. Implementations MUST NOT mutate options
    and MUST return an option from the supplied list verbatim.
    """

    name: str  # e.g. "laya@<revision>" or "deterministic-v1"

    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult: ...

    def score(self, state: str, question: str, levels: list[str]) -> ScoreResult: ...

    def noul(self, state: str, question: str) -> float:
        """Probability for 'yes'. Used for no-match detection."""
        ...


# --------------------------------------------------------------- retrieval
@dataclass(frozen=True)
class ToolCandidate:
    tool_id: str
    server_id: str
    tool_name: str
    server_name: str
    description: str
    domain: str | None
    operation: str
    retrieval_score: float  # hybrid score, higher is better
    matched_on: list[str] = field(default_factory=list)  # e.g. ["vector", "keyword:issues"]


@runtime_checkable
class Retriever(Protocol):
    def retrieve(
        self,
        query: str,
        *,
        limit: int,
        server_ids: list[str] | None = None,
        enabled_only: bool = True,
    ) -> list[ToolCandidate]: ...


# ----------------------------------------------------------------- routing
@dataclass(frozen=True)
class RouteRequest:
    query: str
    agent_id: str
    max_tools: int
    allowed_servers: list[str] | None = None


@dataclass(frozen=True)
class RoutedTool:
    tool_id: str
    server_name: str
    tool_name: str
    score: float


@dataclass(frozen=True)
class RouteResult:
    request_id: str
    tools: list[RoutedTool]
    fallback_used: bool
    latency_ms: float
    model_version: str
    no_match: bool = False


# ------------------------------------------- appended: inference workstream
# Append-only addition (recorded in docs/INTEGRATION_NOTES-inference.md).
# Ranking N retrieval candidates is N Score questions over the same state.
# Laya answers all of them in ONE forward pass (measured on CPU: 8 batched
# ~352ms vs ~760ms sequential), so the batched form is part of the contract.
# Every engine-provided DecisionModel implements it.
@runtime_checkable
class BatchScoringDecisionModel(DecisionModel, Protocol):
    def score_batch(self, state: str, questions: list[str], levels: list[str]) -> list[ScoreResult]:
        """One ScoreResult per question, in order, all on the same `levels` scale."""
        ...
