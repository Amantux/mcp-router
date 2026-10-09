"""Admin-only routing diagnostics collected by `RoutePipeline.route(...,
trace=RouteTrace())` for POST /api/v1/route/simulate.

Passing a trace puts the pipeline in SIMULATION mode: the route cache is
neither read nor written, the decision row is marked `model_version =
"simulated/<model>"`, and the caller (the simulate endpoint) never publishes
exposure or executes anything. A trace is never collected for live routes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcprouter.interfaces import ToolCandidate
from mcprouter.routing.budgets import BudgetClamp


@dataclass
class StageTrace:
    # retrieval | domain | operation | score | noMatch | fallback | maxServers | maxTools
    stage: str
    before: int
    after: int
    pruned: list[ToolCandidate] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)  # JSON-safe values only


@dataclass(frozen=True)
class PolicyFiltered:
    candidate: ToolCandidate
    reason: str  # curated: the policy engine's Decision.reason or a fixed string


@dataclass
class RouteTrace:
    # What the decision model was shown (after the scope pre-filter + limit).
    candidates: list[ToolCandidate] = field(default_factory=list)
    policy_filtered: list[PolicyFiltered] = field(default_factory=list)
    stages: list[StageTrace] = field(default_factory=list)
    # S2d: the applied skills budget (simulate's budgetClamps maxSkills row).
    skill_budget: BudgetClamp | None = None

    def stage(
        self,
        name: str,
        before: int,
        after: int,
        pruned: list[ToolCandidate] | None = None,
        **detail: Any,
    ) -> None:
        self.stages.append(StageTrace(name, before, after, list(pruned or []), detail))
