"""Hierarchical routing pipeline (FR-03 / FR-04 / FR-06).

route(request, scope) — never one big classification over the catalog:

  a. Hybrid retrieval of `retrieval_candidates` tools, restricted to the
     caller's pre-authorized scope (server ids pushed into SQL; per-tool
     `permits` applied before any model call, so a denied tool is never even
     shown to the decision model and no score can surface it).
  b. Domain Choice over the DISTINCT domains among candidates (skipped when
     there is one). Prune to the chosen domain plus any domain whose
     probability is within DOMAIN_KEEP_RATIO of it — calibrated probabilities
     make this a cheap multi-label escape hatch for cross-domain workflows.
     Unclassified (domain=None) candidates are kept: there is nothing to judge
     them on.
  c. Operation Choice over operations present (skipped when one). SOFT:
     mismatching candidates are multiplied by OPERATION_MISMATCH_WEIGHT, not
     dropped. A classifier error on the operation axis (e.g. "close the bug"
     read as `read`) must not hide the only right tool; down-weighting still
     lets a confident relevance score win.
  d. Score per surviving candidate on a 5-level ordered relevance scale;
     expected level in [0,1] blended with the retrieval score.
  e. Noul "does any of these tools fit?" — p(yes) below
     `route_confidence_floor` => no_match with no tools (SPEC §10).
  f. Truncate to clamp(max_tools, 1, max_exposed_tools).

Deterministic failure path (FR-06): ANY exception or contract-violating answer
from the decision model => retrieval-score-only ranking, `fallback_used=True`.
Retrieval/DB errors are NOT swallowed — they are not model failures.

Every decision is persisted as a RoutingDecisionRecord (id = request_id).
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from mcprouter.interfaces import (
    DecisionModel,
    Retriever,
    RoutedTool,
    RouteRequest,
    RouteResult,
    ScopeFilter,
    ToolCandidate,
)
from mcprouter.models import RoutingDecisionRecord
from mcprouter.settings import Settings

log = logging.getLogger(__name__)

RELEVANCE_LEVELS = [
    "not relevant",
    "slightly relevant",
    "somewhat relevant",
    "relevant",
    "highly relevant",
]
DOMAIN_QUESTION = "Which domain does this task belong to?"
OPERATION_QUESTION = "Which kind of operation does this task need: read, write or execute?"
SCORE_QUESTION = "How relevant is this tool to the task?"
NOUL_QUESTION = "Does any of these tools fit the task?"

DOMAIN_KEEP_RATIO = 0.5
OPERATION_MISMATCH_WEIGHT = 0.5
MODEL_WEIGHT = 0.7  # final = MODEL_WEIGHT*relevance + (1-MODEL_WEIGHT)*retrieval
SCOPE_OVERFETCH = 3  # retrieve extra so per-tool scope drops don't starve stage b
_KNOWN_OPS = ("read", "write", "execute")
_DESC_MAX = 400  # bound model input per candidate


class ModelContractError(RuntimeError):
    """The decision model answered outside the protocol (treated as a failure)."""


@dataclass
class _Scored:
    cand: ToolCandidate
    score: float


class RoutePipeline:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        retriever: Retriever,
        model: DecisionModel,
        settings: Settings,
    ) -> None:
        self._factory = session_factory
        self._retriever = retriever
        self._model = model
        self._settings = settings

    @property
    def model_name(self) -> str:
        return self._model.name

    def route(self, request: RouteRequest, scope: ScopeFilter) -> RouteResult:
        """`request.allowed_servers` are server IDS (the API resolves names)."""
        t0 = time.perf_counter()
        request_id = str(uuid.uuid4())
        max_tools = min(max(request.max_tools, 1), max(self._settings.max_exposed_tools, 1))

        candidates = self._scoped_candidates(request, scope)
        fallback = False
        no_match = False
        model_version = self._model.name
        if not candidates:
            ranked: list[_Scored] = []
            no_match = True
        else:
            try:
                ranked, no_match = self._decide(request.query, candidates, max_tools)
            except Exception as exc:  # noqa: BLE001 — FR-06: ANY model failure => fallback
                log.warning(
                    "decision model failed (%s); deterministic retrieval fallback request_id=%s",
                    type(exc).__name__,
                    request_id,
                )
                fallback = True
                model_version = f"retrieval-fallback/{self._model.name}"[:80]
                ranked = [_Scored(c, c.retrieval_score) for c in candidates]
                ranked.sort(key=lambda r: (-r.score, r.cand.tool_id))

        selected = [] if no_match else ranked[:max_tools]
        # Defence in depth: re-assert scope on what is actually exposed.
        selected = [r for r in selected if scope.permits(r.cand)]
        tools = [
            RoutedTool(
                tool_id=r.cand.tool_id,
                server_name=r.cand.server_name,
                tool_name=r.cand.tool_name,
                score=round(r.score, 6),
            )
            for r in selected
        ]
        latency_ms = (time.perf_counter() - t0) * 1000.0
        self._persist(request, request_id, tools, model_version, fallback, latency_ms)
        return RouteResult(
            request_id=request_id,
            tools=tools,
            fallback_used=fallback,
            latency_ms=latency_ms,
            model_version=model_version,
            no_match=no_match or not tools,
        )

    # ----------------------------------------------------------- stage a
    def _scoped_candidates(self, request: RouteRequest, scope: ScopeFilter) -> list[ToolCandidate]:
        scope_ids = scope.server_ids()
        server_ids: list[str] | None
        if scope_ids is None:
            server_ids = request.allowed_servers
        elif request.allowed_servers is None:
            server_ids = list(scope_ids)
        else:
            allowed = set(request.allowed_servers)
            server_ids = [sid for sid in scope_ids if sid in allowed]
        limit = self._settings.retrieval_candidates
        raw = self._retriever.retrieve(
            request.query, limit=limit * SCOPE_OVERFETCH, server_ids=server_ids
        )
        return [c for c in raw if scope.permits(c)][:limit]

    # -------------------------------------------------------- stages b-e
    def _decide(
        self, query: str, cands: list[ToolCandidate], max_tools: int
    ) -> tuple[list[_Scored], bool]:
        m = self._model
        # b. domain (hard prune, with a calibrated multi-label margin)
        domains = sorted({c.domain for c in cands if c.domain})
        if len(domains) > 1:
            res = m.choice(query, DOMAIN_QUESTION, list(domains))
            _check_choice(res.option, res.probabilities, domains)
            p_top = res.probabilities.get(res.option, 0.0)
            keep = {
                d
                for d in domains
                if d == res.option or res.probabilities.get(d, 0.0) >= DOMAIN_KEEP_RATIO * p_top
            }
            cands = [c for c in cands if c.domain is None or c.domain in keep]

        # c. operation (soft)
        weights = dict.fromkeys((c.tool_id for c in cands), 1.0)
        ops = sorted({c.operation for c in cands if c.operation in _KNOWN_OPS})
        if len(ops) > 1:
            res = m.choice(query, OPERATION_QUESTION, list(ops))
            _check_choice(res.option, res.probabilities, ops)
            for c in cands:
                if c.operation in _KNOWN_OPS and c.operation != res.option:
                    weights[c.tool_id] *= OPERATION_MISMATCH_WEIGHT

        # d. per-candidate relevance (one batchable list of states)
        states = [_tool_state(query, c) for c in cands]
        top_level = len(RELEVANCE_LEVELS) - 1
        scored: list[_Scored] = []
        for c, state in zip(cands, states, strict=True):
            sr = m.score(state, SCORE_QUESTION, list(RELEVANCE_LEVELS))
            probs = sr.probabilities
            if len(probs) != len(RELEVANCE_LEVELS) or not all(_is_prob(p) for p in probs):
                raise ModelContractError("score probabilities malformed")
            total = sum(probs) or 1.0
            relevance = sum(i * p for i, p in enumerate(probs)) / (top_level * total)
            blended = MODEL_WEIGHT * relevance + (1.0 - MODEL_WEIGHT) * c.retrieval_score
            scored.append(_Scored(c, blended * weights[c.tool_id]))
        scored.sort(key=lambda r: (-r.score, r.cand.tool_id))

        # e. no-match detection over what would actually be exposed
        shown = "\n".join(f"- {r.cand.server_name}/{r.cand.tool_name}" for r in scored[:max_tools])
        p_yes = m.noul(f"Task: {query}\nTools:\n{shown}", NOUL_QUESTION)
        if not _is_prob(p_yes):
            raise ModelContractError("noul probability malformed")
        return scored, p_yes < self._settings.route_confidence_floor

    # ------------------------------------------------------------ persist
    def _persist(
        self,
        request: RouteRequest,
        request_id: str,
        tools: list[RoutedTool],
        model_version: str,
        fallback: bool,
        latency_ms: float,
    ) -> None:
        with self._factory() as s:
            s.add(
                RoutingDecisionRecord(
                    id=request_id,
                    agent_id=request.agent_id,
                    query=request.query,
                    selected_tool_ids=[t.tool_id for t in tools],
                    scores={t.tool_id: t.score for t in tools},
                    model_version=model_version,
                    fallback_used=fallback,
                    latency_ms=latency_ms,
                )
            )
            s.commit()


def _tool_state(query: str, c: ToolCandidate) -> str:
    desc = c.description[:_DESC_MAX]
    return f"Task: {query}\nTool: {c.server_name}/{c.tool_name}\nDescription: {desc}"


def _is_prob(p: float) -> bool:
    return isinstance(p, (int, float)) and not math.isnan(p) and 0.0 <= p <= 1.0


def _check_choice(option: str, probs: dict[str, float], options: list[str]) -> None:
    if option not in options:
        raise ModelContractError("choice returned an option it was not offered")
    if not all(_is_prob(p) for p in probs.values()):
        raise ModelContractError("choice probabilities malformed")
