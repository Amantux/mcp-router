"""Stage 2 — fine scoring over the bounded shortlist (algorithm_design.md §4).

Only reached for the shortlist (fixed size K_coarse), so cost here never
grows with catalog size N.
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .index import CatalogIndex
from .query import Query
from .schema import CatalogEntry
from .text import SparseVector

DEFAULT_WEIGHTS: Dict[str, float] = {
    "sim_semantic": 0.02,        # very reduced - structural features dominate
    "tag_overlap": 5.0,          # structural/capability fit - primary signal
    "io_fit": 4.0,               # structural I/O type match - primary signal
    "historical_success_rate": 1.0,
    "cost_penalty": -0.8,
    "already_connected_bonus": 0.4,
    "rrf_fusion_rank": 1.2,
}

# Calibration bias (requirements §7 leaves "calibrated probability vs. relative
# score" open; we center the sigmoid input around the "typical/neutral"
# candidate — sim_semantic=0 (blank description), tag_overlap=0.5, io_fit=1,
# success_rate=1, connected=1, mid rrf rank, no cost — so confidence separates
# on structural features and blank description only reduces score by ~1.5x
# instead of 2-2.5x).
#
# Bias adjusted from -5.35 to -7.66 to compensate for higher structural weights
# (tag_overlap=5.0, io_fit=4.0) while keeping typical candidate score ~0.61.
# Updated bias for sim_semantic=0.02, tag_overlap=5.0, io_fit=4.0
DEFAULT_BIAS: float = -(
    DEFAULT_WEIGHTS["tag_overlap"] * 0.5
    + DEFAULT_WEIGHTS["io_fit"]
    + DEFAULT_WEIGHTS["historical_success_rate"]
    + DEFAULT_WEIGHTS["cost_penalty"] * 0.5
    + DEFAULT_WEIGHTS["already_connected_bonus"]
    + DEFAULT_WEIGHTS["rrf_fusion_rank"] * 0.5
) + 0.445  # adjustment to keep typical score ~0.61

_COST_CLASS_VALUE = {"free": 0.0, "low": 0.25, "medium": 0.5, "high": 1.0}
_LATENCY_CLASS_VALUE = {"fast": 0.0, "medium": 0.5, "slow": 1.0}


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _jaccard(a, b) -> float:
    a, b = set(a), set(b)
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _io_fit(entry: CatalogEntry, query: Query) -> float:
    if not query.required_output_types:
        return 1.0
    if not entry.output_types:
        return 0.5  # unknown, partial credit rather than a hard exclude
    overlap = set(query.required_output_types) & set(entry.output_types)
    return 1.0 if overlap else 0.0


def _cost_penalty(entry: CatalogEntry) -> float:
    cp = entry.cost_profile
    return 0.5 * _COST_CLASS_VALUE.get(cp.monetary_cost_class, 0.5) + \
        0.5 * _LATENCY_CLASS_VALUE.get(cp.latency_class, 0.5)


def extract_features(index: CatalogIndex, query_vec: SparseVector, query: Query,
                      cand_id: str, fused_rank: int, shortlist_size: int) -> Dict[str, float]:
    entry = index.entries[cand_id]
    cand_vec = index.embeddings.get(cand_id, {})
    query_terms = set(w.lower() for w in query.intent.split())
    cand_tags = set(t.lower() for t in entry.capability_tags + entry.domain_tags)

    rrf_rank_feature = 1.0 - (fused_rank / max(shortlist_size, 1))

    return {
        "sim_semantic": index.embedder.cosine(query_vec, cand_vec),
        "tag_overlap": _jaccard(query_terms, cand_tags),
        "io_fit": _io_fit(entry, query),
        "historical_success_rate": entry.reliability.success_rate_30d,
        "cost_penalty": _cost_penalty(entry),
        "already_connected_bonus": 1.0 if entry.availability.connected else 0.0,
        "rrf_fusion_rank": rrf_rank_feature,
    }


def heuristic_score(features: Dict[str, float], weights: Optional[Dict[str, float]] = None,
                     bias: float = DEFAULT_BIAS) -> float:
    weights = weights or DEFAULT_WEIGHTS
    # Cap sim_semantic at 0 to prevent negative similarity from dragging down score
    # when descriptions are blank or mismatched
    capped_features = dict(features)
    if "sim_semantic" in capped_features:
        capped_features["sim_semantic"] = max(0.0, capped_features["sim_semantic"])
    
    # Structural score (tag_overlap + io_fit weighted, normalized to [0, 1])
    struct_score = capped_features.get("tag_overlap", 0) * 2.0 + capped_features.get("io_fit", 0) * 2.0
    struct_score = min(1.0, struct_score / 4.0)
    
    # Semantic score (capped at 0)
    sem_score = capped_features.get("sim_semantic", 0)
    
    # Other features (success_rate, connected, rrf_rank) - provide baseline
    other_score = (
        capped_features.get("historical_success_rate", 0.5) * 0.2 +
        capped_features.get("already_connected_bonus", 0) * 0.2 +
        capped_features.get("rrf_fusion_rank", 0.5) * 0.2
    )
    
    # Base score from other features (minimum when structural is 0)
    base_score = other_score
    
    # Final score: base + structural boost (capped at 1.0)
    # Structural features provide ~40% boost on top of baseline
    final_score = min(1.0, base_score + struct_score * 0.4)
    return final_score


@dataclass
class ScoredCandidate:
    id: str
    score: float
    features: Dict[str, float] = field(default_factory=dict)


def fine_score(index: CatalogIndex, query_vec: SparseVector, query: Query,
               shortlist: List[str], weights: Optional[Dict[str, float]] = None) -> List[ScoredCandidate]:
    out = []
    n = len(shortlist)
    for rank, cid in enumerate(shortlist):
        feats = extract_features(index, query_vec, query, cid, rank, n)
        score = heuristic_score(feats, weights)
        out.append(ScoredCandidate(id=cid, score=score, features=feats))
    out.sort(key=lambda c: c.score, reverse=True)
    return out
