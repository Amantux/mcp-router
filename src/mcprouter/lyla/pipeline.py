"""DecisionModel: the pluggable pipeline entry point.

Implements algorithm_design.md §7's `decide()`:

    C' = hard_filter(C, E, session)
    if len(C') <= K_coarse: S = C'
    else: S = fuse(ann_retrieve(...), lexical_retrieve(...))
    R = fine_score(S, Q, scorer)
    result = resolve(R, Q, Q.mode)
    if result.abstained or near_tie_unresolved:
        if caller_allows_arbitration: result = llm_arbitrate(...)
    log_decision(...)
    return result
"""

import hashlib
import time
from typing import Callable, Dict, List, Optional

from .cache import TTLCache
from .index import CatalogIndex
from .query import Environment, Query, SessionState
from .result import Decision
from .retrieval import coarse_retrieve, hard_filter
from .scoring import DEFAULT_WEIGHTS, fine_score
from .resolve import resolve


ArbitrationHook = Callable[[Query, Decision], Decision]


class DecisionModel:
    def __init__(self, index: CatalogIndex, k_coarse: int = 100, epsilon: float = 0.03,
                 min_confidence: float = 0.4, weights: Optional[Dict[str, float]] = None,
                 cache_ttl_seconds: float = 30.0,
                 llm_arbitrator: Optional[ArbitrationHook] = None):
        self.index = index
        self.k_coarse = k_coarse
        self.epsilon = epsilon
        self.min_confidence = min_confidence
        self.weights = weights or dict(DEFAULT_WEIGHTS)
        self.cache = TTLCache(ttl_seconds=cache_ttl_seconds)
        self.llm_arbitrator = llm_arbitrator
        self.decision_log: List[Dict] = []

    def _query_hash(self, query: Query) -> str:
        key = f"{query.intent}|{query.task_state}|{sorted(query.required_output_types)}|{query.mode}|{sorted(query.hints)}"
        return hashlib.blake2b(key.encode("utf-8"), digest_size=12).hexdigest()

    def decide(self, query: Query, env: Optional[Environment] = None,
               session: Optional[SessionState] = None) -> Decision:
        start = time.perf_counter()
        env = env or Environment()
        session = session or SessionState()

        catalog_version = self.index.version
        query_vec = self.index.embedder.embed(query.intent + " " + query.task_state)
        query_hash = self._query_hash(query)

        cached_shortlist = self.cache.get(session.session_id, query_hash, catalog_version)

        candidate_ids = hard_filter(self.index, env, session)

        if cached_shortlist is not None:
            shortlist = [cid for cid in cached_shortlist if cid in set(candidate_ids)]
        else:
            query_terms = query.intent.lower().split()
            shortlist, _, _ = coarse_retrieve(
                self.index, candidate_ids, query_vec, query_terms, self.k_coarse, hints=query.hints,
            )
            self.cache.put(session.session_id, query_hash, catalog_version, shortlist)

        ranked = fine_score(self.index, query_vec, query, shortlist, self.weights)
        decision = resolve(self.index, ranked, query, epsilon=self.epsilon,
                            min_confidence=self.min_confidence)

        if (decision.abstained or (len(decision.selection) > 1 and query.mode == "single")) and self.llm_arbitrator:
            decision = self.llm_arbitrator(query, decision)
            decision.used_arbitration = True

        decision.candidates_evaluated = len(candidate_ids)
        decision.shortlist_size = len(shortlist)
        decision.catalog_version = catalog_version
        decision.elapsed_ms = (time.perf_counter() - start) * 1000.0

        self.decision_log.append({
            "intent": query.intent,
            "selection": decision.selection,
            "confidence": decision.confidence,
            "abstained": decision.abstained,
            "catalog_version": catalog_version,
            "candidates_evaluated": decision.candidates_evaluated,
            "shortlist_size": decision.shortlist_size,
            "elapsed_ms": decision.elapsed_ms,
        })
        return decision
