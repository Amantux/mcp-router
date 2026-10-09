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
     expected level in [0,1] blended with the retrieval score. ONE
     `score_batch` call when the model offers it (Laya: one forward pass).
  e. Noul "does any of these tools fit?" — p(yes) below
     `route_confidence_floor` => no_match with no tools (SPEC §10).

Question shape (decision-model contract, fixed at integration): `state` is
always the TASK (the query); the candidate being asked about goes in the
QUESTION — one tool per Score question, one tool per line of the Noul
question. That is what `score_batch(state, questions, levels)` and the
deterministic-v1 model assume (it compares `state` against the question /
each question line), and how the inference bench drives Laya.
  f. Budgets: cap the DISTINCT servers represented (`max_servers`, in rank
     order; slots freed by a skipped server are back-filled from deeper
     ranks), then truncate to clamp(max_tools, 1, max_exposed_tools). The
     API computes both with routing.budgets (request may lower, never raise).

Deterministic failure path (FR-06): ANY exception or contract-violating answer
from the decision model => retrieval-score-only ranking, `fallback_used=True`.
Retrieval/DB errors are NOT swallowed — they are not model failures.

Every decision is persisted as a RoutingDecisionRecord (id = request_id).

Route cache (routing/cache.py): a hit skips stages a-e but is re-validated
against current eligibility + `scope.permits` before it is returned (never
skips authorization); its decision row is marked `model_version =
"cached/<model>"`.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.generation import catalog_generation, policy_generation
from mcprouter.interfaces import (
    BatchScoringDecisionModel,
    DecisionModel,
    Retriever,
    RoutedTool,
    RouteRequest,
    RouteResult,
    ScopeFilter,
    ToolCandidate,
)
from mcprouter.models import (
    MCPServerRecord,
    MCPToolRecord,
    RoutingDecisionRecord,
    SkillRecord,
    SkillSourceRecord,
)
from mcprouter.routing.budgets import BudgetClamp, cap_servers, clamp_budget
from mcprouter.routing.cache import CachedRoute, CachedTool, RouteCache, normalize_query
from mcprouter.routing.retriever import eligibility_filters, skill_eligibility_filters
from mcprouter.routing.scope import principal_skill_cap
from mcprouter.routing.trace import PolicyFiltered, RouteTrace
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
ROUTABLE_KINDS = ("tool", "skill")
SCOPE_OVERFETCH = 3  # retrieve extra so per-tool scope drops don't starve stage b
_KNOWN_OPS = ("read", "write", "execute")
_DESC_MAX = 400  # bound model input per candidate
_NOUL_DESC_MAX = 120  # per line of the no-match question
# RoutingDecisionRecord.model_version prefix for a decision served from the
# route cache (column-free convention: "cached/<model that made it>").
CACHED_MARKER = "cached/"
# ... and for an admin simulation (/route/simulate): "simulated/<model>".
SIMULATED_MARKER = "simulated/"


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
        self._cache = RouteCache.from_settings(
            settings.route_cache_size, settings.route_cache_ttl_s
        )

    @property
    def model_name(self) -> str:
        return self._model.name

    def route(
        self, request: RouteRequest, scope: ScopeFilter, *, trace: RouteTrace | None = None
    ) -> RouteResult:
        """`request.allowed_servers` are server IDS (the API resolves names).

        `trace` = admin SIMULATION (routing/trace.py): diagnostics are
        collected, the route cache is bypassed, and the decision row is marked
        `simulated/`. The routed set is computed exactly as for a live route."""
        t0 = time.perf_counter()
        request_id = str(uuid.uuid4())
        max_tools = min(max(request.max_tools, 1), max(self._settings.max_exposed_tools, 1))
        # Same clamp chain as the API (routing.budgets): the global cap still
        # applies to a RouteRequest built by any other caller.
        max_servers = _min_set(request.max_servers, self._settings.max_exposed_servers)
        skill_clamp = self.skills_budget(request, scope)
        max_skills = skill_clamp.applied or 0
        kinds = _route_kinds(request)
        if max_skills == 0:
            # A zero skills budget retrieves no skills: no candidate slots or
            # model questions spent on items that could never be routed.
            kinds = tuple(k for k in kinds if k != "skill")
        if trace is not None:
            trace.skill_budget = skill_clamp

        # Route cache: may skip retrieval + model calls, NEVER authorization —
        # `_revalidate` re-runs eligibility and scope.permits on every hit.
        base_key = (
            None
            if trace is not None
            else self._cache_key(request, scope, max_tools, max_servers, max_skills, kinds)
        )
        if base_key is not None and self._cache is not None:
            # The model name is read at lookup AND at store time: an engine
            # that lazy-loads during this route changes it ("x (not loaded)").
            key = (*base_key, self._model.name)
            hit = self._cache.get(key)
            if hit is not None:
                cached_tools = self._revalidate(hit, request, scope)
                if cached_tools is not None:
                    latency_ms = (time.perf_counter() - t0) * 1000.0
                    model_version = f"{CACHED_MARKER}{hit.model_version}"[:80]
                    self._persist(
                        request, request_id, cached_tools, model_version, False, latency_ms
                    )
                    return RouteResult(
                        request_id=request_id,
                        tools=cached_tools,
                        fallback_used=False,
                        latency_ms=latency_ms,
                        model_version=model_version,
                        no_match=hit.no_match or not cached_tools,
                        cached=True,
                    )
                # Something it held is no longer eligible/permitted: recompute.
                self._cache.discard(key)

        candidates = self._scoped_candidates(request, scope, kinds, trace)
        fallback = False
        no_match = False
        model_version = self._model.name[:80]  # RoutingDecisionRecord.model_version
        if not candidates:
            ranked: list[_Scored] = []
            no_match = True
        else:
            try:
                ranked, no_match = self._decide(
                    request.query, candidates, max_tools, max_servers, max_skills, trace
                )
            except Exception as exc:  # noqa: BLE001 — FR-06: ANY model failure => fallback
                log.warning(
                    "decision model failed (%s); deterministic retrieval fallback request_id=%s",
                    type(exc).__name__,
                    request_id,
                )
                fallback = True
                model_version = f"retrieval-fallback/{self._model.name}"[:80]
                ranked = [_Scored(c, c.retrieval_score) for c in candidates]
                ranked.sort(key=_rank_key)
                if trace is not None:
                    # A failure may land mid-stage: drop partial model stages.
                    trace.stages = [st for st in trace.stages if st.stage == "retrieval"]
                    trace.stage(
                        "fallback",
                        len(candidates),
                        len(ranked),
                        error=type(exc).__name__,
                        scores={_ckey(r.cand): round(r.score, 6) for r in ranked},
                    )

        # Budgets: cap DISTINCT servers in rank order first (slots freed by a
        # skipped server are back-filled from deeper ranks), then the count.
        selected = [] if no_match else _cap(ranked, max_tools, max_servers, max_skills)
        if trace is not None and not no_match:
            _trace_budgets(trace, ranked, max_tools, max_servers, max_skills)
        # Defence in depth: re-assert scope on what is actually exposed.
        selected = [r for r in selected if scope.permits(r.cand)]
        tools = [
            RoutedTool(
                tool_id=r.cand.tool_id,
                server_name=r.cand.server_name,
                tool_name=r.cand.tool_name,
                score=round(r.score, 6),
                kind=r.cand.kind,
            )
            for r in selected
        ]
        if base_key is not None and self._cache is not None and not fallback:
            # Fallback results are never cached: a transient model timeout
            # must not pin a degraded ranking for the TTL.
            self._cache.put(
                (*base_key, self._model.name),
                CachedRoute(
                    tools=tuple(CachedTool(t.tool_id, t.score, t.kind) for t in tools),
                    no_match=no_match or not tools,
                    model_version=model_version,
                ),
            )
        if trace is not None:
            model_version = f"{SIMULATED_MARKER}{model_version}"[:80]
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

    # ------------------------------------------------------------- cache
    def skills_budget(self, request: RouteRequest, scope: ScopeFilter) -> BudgetClamp:
        """Applied skills budget: min(request, principal, global) — a request
        may lower, never raise. `.applied` is item 2's max_skills_applied."""
        return clamp_budget(
            "maxSkills",
            request.max_skills,
            _principal_max_skills(scope),
            self._settings.max_exposed_skills,
        )

    def _cache_key(
        self,
        request: RouteRequest,
        scope: ScopeFilter,
        max_tools: int,
        max_servers: int | None,
        max_skills: int,
        kinds: tuple[str, ...],
    ) -> tuple[object, ...] | None:
        if self._cache is None:
            return None
        fingerprint = getattr(scope, "fingerprint", None)
        fp = fingerprint() if callable(fingerprint) else None
        if not isinstance(fp, str):
            return None  # unknown ScopeFilter: cannot be keyed safely -> no caching
        allowed = (
            None if request.allowed_servers is None else tuple(sorted(set(request.allowed_servers)))
        )
        return (
            request.agent_id,
            normalize_query(request.query),
            fp,
            catalog_generation(),
            policy_generation(),
            max_tools,
            max_servers,
            allowed,
            max_skills,
            kinds,
        )

    def _revalidate(
        self, hit: CachedRoute, request: RouteRequest, scope: ScopeFilter
    ) -> list[RoutedTool] | None:
        """Re-authorize a cache hit against CURRENT state: catalog eligibility
        (fresh rows: enabled/available/server enabled/not offline) and the
        CURRENT scope (server ids + `permits` on the freshly loaded
        operation). Returns None if any cached tool no longer passes."""
        if not hit.tools:
            return []
        ids = [t.tool_id for t in hit.tools if t.kind == "tool"]
        skill_ids = [t.tool_id for t in hit.tools if t.kind == "skill"]
        with self._factory() as s:
            skill_rows = s.execute(
                select(
                    SkillRecord.id,
                    SkillRecord.source_id,
                    SkillRecord.name,
                    SkillSourceRecord.name,
                    SkillRecord.description,
                    SkillRecord.domain,
                    SkillRecord.operation,
                )
                .join(SkillSourceRecord, SkillSourceRecord.id == SkillRecord.source_id)
                .where(SkillRecord.id.in_(skill_ids), *skill_eligibility_filters())
            ).all()
            rows = s.execute(
                select(
                    MCPToolRecord.id,
                    MCPToolRecord.server_id,
                    MCPToolRecord.name,
                    MCPServerRecord.name,
                    MCPToolRecord.description,
                    MCPToolRecord.domain,
                    MCPToolRecord.operation,
                )
                .join(MCPServerRecord, MCPServerRecord.id == MCPToolRecord.server_id)
                .where(MCPToolRecord.id.in_(ids), *eligibility_filters())
            ).all()
        by_id = {("tool", r[0]): r for r in rows}
        by_id.update({("skill", r[0]): r for r in skill_rows})
        server_ids = _permitted_server_ids(request, scope)
        permitted = None if server_ids is None else set(server_ids)
        out: list[RoutedTool] = []
        for ct in hit.tools:
            row = by_id.get((ct.kind, ct.tool_id))
            if row is None:
                return None
            cand = ToolCandidate(
                tool_id=row[0],
                server_id=row[1],
                tool_name=row[2],
                server_name=row[3],
                description=row[4] or "",
                domain=row[5],
                operation=row[6],
                retrieval_score=0.0,
                kind=ct.kind,
            )
            if ct.kind == "tool" and permitted is not None and cand.server_id not in permitted:
                return None
            if not scope.permits(cand):
                return None
            out.append(
                RoutedTool(cand.tool_id, cand.server_name, cand.tool_name, ct.score, ct.kind)
            )
        return out

    # ----------------------------------------------------------- stage a
    def _scoped_candidates(
        self,
        request: RouteRequest,
        scope: ScopeFilter,
        kinds: tuple[str, ...] = ("tool",),
        trace: RouteTrace | None = None,
    ) -> list[ToolCandidate]:
        server_ids = _permitted_server_ids(request, scope)
        limit = self._settings.retrieval_candidates
        raw = self._retriever.retrieve(
            request.query, limit=limit * SCOPE_OVERFETCH, server_ids=server_ids, kinds=kinds
        )
        # Enforce server scope HERE too: never trust an injected Retriever to
        # honour server_ids (e.g. `if server_ids:` would read [] as "all").
        permitted_ids = None if server_ids is None else set(server_ids)
        kept = [
            c
            for c in raw
            if c.kind in kinds
            # Server scope is MCP-only: a skill's server_id is its source id.
            and (c.kind != "tool" or permitted_ids is None or c.server_id in permitted_ids)
            and scope.permits(c)
        ]
        out = kept[:limit]
        if trace is not None:
            self._trace_retrieval(trace, request, scope, raw, kept, out, permitted_ids, kinds)
        return out

    def _trace_retrieval(
        self,
        trace: RouteTrace,
        request: RouteRequest,
        scope: ScopeFilter,
        raw: list[ToolCandidate],
        kept: list[ToolCandidate],
        out: list[ToolCandidate],
        permitted_ids: set[str] | None,
        kinds: tuple[str, ...] = ("tool",),
    ) -> None:
        """Simulation only. Server-level scope is pushed into SQL, so tools on
        out-of-scope servers never reach `raw`; one extra retrieval WITHOUT
        the scope's server restriction (the request's own allowed_servers
        still apply) surfaces them, with the policy engine's reason."""
        explain = getattr(scope, "explain", None)

        def reason(c: ToolCandidate) -> str:
            if callable(explain):
                text = explain(c)
                if isinstance(text, str) and text:
                    return text
            if c.kind == "tool" and permitted_ids is not None and c.server_id not in permitted_ids:
                return "server not in scope"
            return "denied by scope"

        seen: set[str] = set()
        denied: list[ToolCandidate] = []
        extra = self._retriever.retrieve(
            request.query,
            limit=self._settings.retrieval_candidates * SCOPE_OVERFETCH,
            server_ids=request.allowed_servers,
            kinds=kinds,
        )
        kept_ids = {_ckey(c) for c in kept}
        for c in [*raw, *extra]:
            if c.kind not in kinds or _ckey(c) in kept_ids or _ckey(c) in seen:
                continue
            seen.add(_ckey(c))
            denied.append(c)
            trace.policy_filtered.append(PolicyFiltered(c, reason(c)))
        trace.candidates = list(out)
        trace.stage(
            "retrieval",
            len(raw),
            len(out),
            [c for c in raw if _ckey(c) not in kept_ids] + kept[len(out) :],
            limit=self._settings.retrieval_candidates,
            policyFiltered=len(denied),
            **_kind_counts(raw, out),
        )

    # -------------------------------------------------------- stages b-e
    def _decide(
        self,
        query: str,
        cands: list[ToolCandidate],
        max_tools: int,
        max_servers: int | None = None,
        max_skills: int = 0,
        trace: RouteTrace | None = None,
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
            before = cands
            cands = [c for c in cands if c.domain is None or c.domain in keep]
            if trace is not None:
                trace.stage(
                    "domain",
                    len(before),
                    len(cands),
                    [c for c in before if c not in cands],
                    options=domains,
                    chosen=res.option,
                    probabilities=dict(res.probabilities),
                    kept=sorted(keep),
                )

        # c. operation (soft)
        weights = dict.fromkeys((_ckey(c) for c in cands), 1.0)
        ops = sorted({c.operation for c in cands if c.operation in _KNOWN_OPS})
        if len(ops) > 1:
            res = m.choice(query, OPERATION_QUESTION, list(ops))
            _check_choice(res.option, res.probabilities, ops)
            for c in cands:
                if c.operation in _KNOWN_OPS and c.operation != res.option:
                    weights[_ckey(c)] *= OPERATION_MISMATCH_WEIGHT
            if trace is not None:
                trace.stage(
                    "operation",
                    len(cands),
                    len(cands),
                    options=ops,
                    chosen=res.option,
                    probabilities=dict(res.probabilities),
                    downweighted=[tid for tid, w in weights.items() if w != 1.0],
                    weight=OPERATION_MISMATCH_WEIGHT,
                )

        # d. per-candidate relevance: state = task, one question per candidate
        questions = [_tool_question(c) for c in cands]
        levels = list(RELEVANCE_LEVELS)
        if isinstance(m, BatchScoringDecisionModel):
            results = m.score_batch(query, questions, levels)
            if len(results) != len(questions):
                raise ModelContractError("score_batch returned the wrong number of results")
        else:
            results = [m.score(query, q, levels) for q in questions]
        top_level = len(RELEVANCE_LEVELS) - 1
        scored: list[_Scored] = []
        for c, sr in zip(cands, results, strict=True):
            probs = sr.probabilities
            if len(probs) != len(RELEVANCE_LEVELS) or not all(_is_prob(p) for p in probs):
                raise ModelContractError("score probabilities malformed")
            total = sum(probs) or 1.0
            relevance = sum(i * p for i, p in enumerate(probs)) / (top_level * total)
            blended = MODEL_WEIGHT * relevance + (1.0 - MODEL_WEIGHT) * c.retrieval_score
            scored.append(_Scored(c, blended * weights[_ckey(c)]))
        scored.sort(key=_rank_key)
        if trace is not None:
            trace.stage(
                "score",
                len(cands),
                len(scored),
                scores={_ckey(r.cand): round(r.score, 6) for r in scored},
            )

        # e. no-match detection over what would actually be exposed
        shown = "\n".join(
            f"- {r.cand.server_name}/{r.cand.tool_name}: {r.cand.description[:_NOUL_DESC_MAX]}"
            for r in _cap(scored, max_tools, max_servers, max_skills)
        )
        p_yes = m.noul(query, f"{NOUL_QUESTION}\n{shown}")
        if not _is_prob(p_yes):
            raise ModelContractError("noul probability malformed")
        no_match = p_yes < self._settings.route_confidence_floor
        if trace is not None:
            trace.stage(
                "noMatch",
                len(scored),
                0 if no_match else len(scored),
                pYes=round(p_yes, 6),
                floor=self._settings.route_confidence_floor,
                noMatch=no_match,
            )
        return scored, no_match

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
            # Decision rows are telemetry: don't make the agent wait on the WAL
            # fsync. Worst case on a DB-server crash is losing the last
            # ~wal_writer_delay*3 of decision rows — never routing correctness.
            s.execute(text("SET LOCAL synchronous_commit TO OFF"))
            s.add(
                RoutingDecisionRecord(
                    id=request_id,
                    agent_id=request.agent_id,
                    query=request.query,
                    selected_tool_ids=[_decision_id(t) for t in tools],
                    scores={_decision_id(t): t.score for t in tools},
                    model_version=model_version,
                    fallback_used=fallback,
                    latency_ms=latency_ms,
                )
            )
            s.commit()


def _permitted_server_ids(request: RouteRequest, scope: ScopeFilter) -> list[str] | None:
    """Scope's server ids intersected with the request's allowed_servers
    (None = no server-level restriction)."""
    scope_ids = scope.server_ids()
    if scope_ids is None:
        return request.allowed_servers
    if request.allowed_servers is None:
        return list(scope_ids)
    allowed = set(request.allowed_servers)
    return [sid for sid in scope_ids if sid in allowed]


def _trace_budgets(
    trace: RouteTrace,
    ranked: list[_Scored],
    max_tools: int,
    max_servers: int | None,
    max_skills: int = 0,
) -> None:
    skills = [r for r in ranked if r.cand.kind == "skill"]
    ranked = [r for r in ranked if r.cand.kind != "skill"]
    capped = cap_servers(ranked, lambda r: r.cand.server_id, max_servers)
    kept = {id(r) for r in capped}
    trace.stage(
        "maxServers",
        len(ranked),
        len(capped),
        [r.cand for r in ranked if id(r) not in kept],
        limit=max_servers,
    )
    trace.stage(
        "maxTools",
        len(capped),
        min(len(capped), max_tools),
        [r.cand for r in capped[max_tools:]],
        limit=max_tools,
    )
    trace.stage(
        "maxSkills",
        len(skills),
        min(len(skills), max_skills),
        [r.cand for r in skills[max_skills:]],
        limit=max_skills,
    )


def _cap(
    ranked: list[_Scored], max_tools: int, max_servers: int | None, max_skills: int = 0
) -> list[_Scored]:
    """Per-kind budgets in rank order: tools via maxServers -> maxTools (MCP
    servers only), skills via maxSkills alone; merged back in rank order."""
    tools = [r for r in ranked if r.cand.kind != "skill"]
    keep = {id(r) for r in cap_servers(tools, lambda r: r.cand.server_id, max_servers)[:max_tools]}
    keep |= {id(r) for r in [r for r in ranked if r.cand.kind == "skill"][:max_skills]}
    return [r for r in ranked if id(r) in keep]


def _route_kinds(request: RouteRequest) -> tuple[str, ...]:
    """Tools + skills always (settings-independent); request.kinds may narrow."""
    want = request.kinds
    return tuple(k for k in ROUTABLE_KINDS if want is None or k in want)


def _principal_max_skills(scope: ScopeFilter) -> int | None:
    return principal_skill_cap(scope)  # fail-closed resolver (routing.scope)


def _kind_counts(raw: list[ToolCandidate], out: list[ToolCandidate]) -> dict[str, int]:
    return {
        f"{side}{kind.capitalize()}s": sum(1 for c in cs if c.kind == kind)
        for side, cs in (("before", raw), ("after", out))
        for kind in ROUTABLE_KINDS
    }


def _ckey(c: ToolCandidate) -> str:
    return c.tool_id if c.kind == "tool" else f"skill:{c.tool_id}"


def _decision_id(t: RoutedTool) -> str:
    return t.tool_id if t.kind == "tool" else f"skill:{t.tool_id}"


def _min_set(*values: int | None) -> int | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


def _rank_key(r: _Scored) -> tuple[float, str, str]:
    # Ties break on catalog names, not random ids: reproducible evals.
    return (-r.score, r.cand.server_name, r.cand.tool_name)


def _tool_question(c: ToolCandidate) -> str:
    desc = c.description[:_DESC_MAX]
    if c.kind == "skill":
        return (
            f"[skill] {SCORE_QUESTION}\nSkill: {c.server_name}/{c.tool_name}\nDescription: {desc}"
        )
    return f"{SCORE_QUESTION}\nTool: {c.server_name}/{c.tool_name}\nDescription: {desc}"


def _is_prob(p: float) -> bool:
    return isinstance(p, (int, float)) and not math.isnan(p) and 0.0 <= p <= 1.0


def _check_choice(option: str, probs: dict[str, float], options: list[str]) -> None:
    if option not in options:
        raise ModelContractError("choice returned an option it was not offered")
    if not all(_is_prob(p) for p in probs.values()):
        raise ModelContractError("choice probabilities malformed")
