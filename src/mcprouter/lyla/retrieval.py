"""Stage 0 (hard filter) and Stage 1 (coarse retrieval, ANN + lexical, RRF-fused).

Pseudocode source: algorithm_design.md §2-3. Stage 0 is the only stage that
scans the full candidate pool, and it does so with O(1) field checks per
candidate (no embeddings, no scoring model) — this is what keeps the
pipeline's cost flat as the catalog grows to 10k+.
"""

from typing import Dict, List, Optional, Tuple

from .index import CatalogIndex
from .query import Environment, SessionState
from .text import SparseVector


def hard_filter(index: CatalogIndex, env: Environment, session: SessionState) -> List[str]:
    out: List[str] = []
    for cand_id, entry in index.entries.items():
        if entry.deprecated or not entry.availability.enabled:
            continue
        if entry.availability.requires_auth and not entry.availability.connected:
            continue
        if entry.availability.platform_constraints and env.platform not in entry.availability.platform_constraints:
            continue
        if cand_id in env.denylist:
            continue
        if env.allowlist and cand_id not in env.allowlist:
            continue
        if cand_id in session.failed_this_task and not session.retry_allowed(cand_id):
            continue
        out.append(cand_id)
    return out


def ann_retrieve(index: CatalogIndex, candidate_ids: List[str], query_vec: SparseVector, k: int) -> List[str]:
    """Cosine-similarity rank over the given candidate subset.

    A real deployment swaps this for an HNSW/FAISS ANN index (O(log N)
    query time, incremental insert) per algorithm_design.md §3a; this
    brute-force implementation is correctness-equivalent (exact, not
    approximate) and is cheap enough once `candidate_ids` is already
    hard-filtered to a bounded subset.
    """
    cosine = index.embedder.cosine
    scored = []
    for cid in candidate_ids:
        vec = index.embeddings.get(cid)
        if vec is None:
            continue
        scored.append((cid, cosine(query_vec, vec)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return [cid for cid, _ in scored[:k]]


def lexical_retrieve(index: CatalogIndex, candidate_ids: List[str], query_terms: List[str], k: int) -> List[str]:
    """Simple term-overlap/BM25-lite rank, bounded to candidate_ids."""
    candidate_set = set(candidate_ids)
    term_set = set(query_terms)
    scored: List[Tuple[str, float]] = []
    for cid in candidate_ids:
        entry = index.entries[cid]
        tag_terms = {t.lower() for t in entry.capability_tags + entry.domain_tags}
        name_terms = set(entry.name.lower().split())
        desc_terms = set(entry.description.lower().split())
        overlap = len(term_set & tag_terms) * 2.0 + len(term_set & name_terms) * 1.5 + len(term_set & desc_terms) * 1.0
        if overlap > 0:
            scored.append((cid, overlap))
    scored.sort(key=lambda t: t[1], reverse=True)
    return [cid for cid, _ in scored[:k]]


def fuse(ann_list: List[str], lexical_list: List[str], k_coarse: int, k_const: float = 60.0) -> List[str]:
    """Reciprocal Rank Fusion (algorithm_design.md §3c)."""
    scores: Dict[str, float] = {}
    for rank, cid in enumerate(ann_list):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k_const + rank)
    for rank, cid in enumerate(lexical_list):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k_const + rank)
    ranked = sorted(scores.items(), key=lambda t: t[1], reverse=True)
    return [cid for cid, _ in ranked[:k_coarse]]


def coarse_retrieve(index: CatalogIndex, candidate_ids: List[str], query_vec: SparseVector,
                     query_terms: List[str], k_coarse: int, hints: Optional[List[str]] = None) -> Tuple[List[str], List[str], List[str]]:
    """Returns (shortlist, ann_list, lexical_list) for rationale/debugging."""
    ann_list = ann_retrieve(index, candidate_ids, query_vec, k_coarse)
    lexical_list = lexical_retrieve(index, candidate_ids, query_terms, k_coarse)
    if hints:
        candidate_set = set(candidate_ids)
        hinted = [h for h in hints if h in candidate_set]
        # caller hints get a rank-0 placement bonus in both lists (soft boost,
        # not a bypass — they still have to survive fine scoring).
        ann_list = hinted + [c for c in ann_list if c not in hinted]
        lexical_list = hinted + [c for c in lexical_list if c not in hinted]
    shortlist = fuse(ann_list, lexical_list, k_coarse)
    return shortlist, ann_list, lexical_list
