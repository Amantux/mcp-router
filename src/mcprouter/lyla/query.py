"""Request-side types: Query, Environment constraints, per-session state.

Mirrors requirements.md §2 (inputs) and §5 (repeated-failure rule).
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class Query:
    intent: str
    task_state: str = ""                   # bounded working-context summary
    required_output_types: List[str] = field(default_factory=list)
    mode: str = "single"                    # "single" | "ranked_list"
    top_k: int = 5
    hints: List[str] = field(default_factory=list)   # caller pre-suggested ids


@dataclass
class Environment:
    platform: str = "cli"
    denylist: Set[str] = field(default_factory=set)
    allowlist: Set[str] = field(default_factory=set)
    budget_ceiling_ms: Optional[float] = None


@dataclass
class SessionState:
    """Per-agent-session overlay (catalog_schema.md §3 tier C).

    Deliberately never merged into the shared catalog so one session's
    failures can't leak into another session's scoring (requirements §4).
    """

    session_id: str = "default"
    failed_this_task: Dict[str, int] = field(default_factory=dict)  # id -> fail count
    retry_limit: int = 1

    def record_failure(self, candidate_id: str) -> None:
        self.failed_this_task[candidate_id] = self.failed_this_task.get(candidate_id, 0) + 1

    def retry_allowed(self, candidate_id: str) -> bool:
        return self.failed_this_task.get(candidate_id, 0) < self.retry_limit
