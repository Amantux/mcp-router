"""Output types: a scored Candidate and the final Decision envelope.

Mirrors requirements.md §3 (outputs: selection, confidence, rationale,
abstain signal, latency/cost metadata).
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Candidate:
    id: str
    score: float
    features: Dict[str, float] = field(default_factory=dict)


@dataclass
class Decision:
    selection: List[str]                    # candidate ids, [] if abstained
    confidence: float
    rationale: str
    abstained: bool = False
    tiebreak_applied: bool = False
    used_arbitration: bool = False
    candidates_evaluated: int = 0
    shortlist_size: int = 0
    elapsed_ms: float = 0.0
    catalog_version: int = 0
    closest_candidates: List[str] = field(default_factory=list)
