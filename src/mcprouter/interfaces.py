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


# ------------------------------------------------- execution (gateway seams)
# Appended by the gateway/security workstream. The execution manager is the
# ONLY caller of a ToolInvoker; nothing else may invoke an upstream tool.
@dataclass(frozen=True)
class ToolCallResult:
    """SDK-agnostic tool result. `content` holds MCP content blocks in wire
    form (e.g. {"type": "text", "text": "..."})."""

    content: list[dict[str, object]]
    is_error: bool = False
    structured_content: dict[str, object] | None = None


class ToolInvocationError(Exception):
    """Raised by a ToolInvoker for an upstream failure. `curated` is the only
    text that leaves the process — never the upstream body or str(exc)."""

    def __init__(self, curated: str) -> None:
        super().__init__(curated)
        self.curated = curated


@runtime_checkable
class ToolInvoker(Protocol):
    """Calls one tool on one upstream MCP server. Implementations must honour
    `timeout_s` themselves; the execution manager also enforces it."""

    async def call_tool(
        self,
        server: object,  # mcprouter.models.MCPServerRecord (kept loose: no ORM import here)
        tool_name: str,
        arguments: dict[str, object],
        timeout_s: float,
    ) -> ToolCallResult: ...


@runtime_checkable
class RouteFn(Protocol):
    """The routing pipeline as the gateway sees it (sync; called in a worker
    thread). Must already scope results to `request.agent_id`; the gateway
    re-applies policy regardless (defense in depth)."""

    def __call__(self, request: RouteRequest) -> RouteResult: ...
