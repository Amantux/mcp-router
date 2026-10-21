"""Stage 3 — tie-break and abstention (algorithm_design.md §5, requirements §5)."""

from typing import List, Optional

from .result import Decision
from .scoring import ScoredCandidate


def _explain(ranked: List[ScoredCandidate], tiebreak_applied: bool = False, tiebreak_rule: str = "") -> str:
    if not ranked:
        return "no candidates survived filtering/retrieval"
    lines = []
    top = ranked[0]
    drivers = sorted(top.features.items(), key=lambda kv: kv[1], reverse=True)[:3]
    driver_str = ", ".join(f"{k}={v:.2f}" for k, v in drivers)
    lines.append(f"selected {top.id} (score={top.score:.3f}); top drivers: {driver_str}")
    if tiebreak_applied:
        lines.append(f"near-tie resolved via tiebreak rule: {tiebreak_rule}")
    if len(ranked) > 1:
        runner_up = ranked[1]
        lines.append(f"runner-up {runner_up.id} (score={runner_up.score:.3f})")
    return "; ".join(lines)


def _tiebreak_winner(near_ties: List[ScoredCandidate], index) -> ScoredCandidate:
    def key(c: ScoredCandidate):
        entry = index.entries[c.id]
        return (
            0 if entry.availability.connected else 1,          # prefer connected
            _cost_rank(entry),                                  # prefer lower cost
            -entry.reliability.success_rate_30d,                # prefer higher success rate
        )

    return sorted(near_ties, key=key)[0]


def _cost_rank(entry) -> float:
    order = {"free": 0, "low": 1, "medium": 2, "high": 3}
    return order.get(entry.cost_profile.monetary_cost_class, 2)


def resolve(index, ranked: List[ScoredCandidate], query, epsilon: float = 0.03,
            min_confidence: float = 0.4) -> Decision:
    if not ranked:
        return Decision(selection=[], confidence=0.0,
                         rationale="empty shortlist: no candidates survived hard filter/retrieval",
                         abstained=True)

    top_score = ranked[0].score
    if top_score < min_confidence:
        return Decision(
            selection=[], confidence=top_score,
            rationale=f"abstained: top score {top_score:.3f} below confidence floor {min_confidence}",
            abstained=True,
            closest_candidates=[c.id for c in ranked[:5]],
        )

    near_ties = [c for c in ranked if top_score - c.score <= epsilon]

    if len(near_ties) > 1:
        if query.mode == "ranked_list":
            ids = [c.id for c in near_ties]
            return Decision(
                selection=ids, confidence=top_score,
                rationale=_explain(near_ties, tiebreak_applied=False),
            )
        winner = _tiebreak_winner(near_ties, index)
        return Decision(
            selection=[winner.id], confidence=top_score,
            rationale=_explain([winner] + [c for c in near_ties if c.id != winner.id],
                                tiebreak_applied=True,
                                tiebreak_rule="connected>cost>success_rate"),
            tiebreak_applied=True,
        )

    if query.mode == "ranked_list":
        top_k = ranked[: query.top_k]
        return Decision(selection=[c.id for c in top_k], confidence=top_score,
                         rationale=_explain(top_k))

    top = ranked[0]
    return Decision(selection=[top.id], confidence=top_score, rationale=_explain(ranked[:2]))
