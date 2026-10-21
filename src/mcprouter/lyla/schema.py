"""CatalogEntry — the per-candidate record Lyla scores, per catalog_schema.md.

Only the fields the decision model actually consumes are modeled as first-
class attributes; everything else from catalog_schema.md's full JSON shape
can be carried in ``extra`` without the scoring code needing to change.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class CostProfile:
    latency_class: str = "fast"          # "fast" | "medium" | "slow"
    p50_ms: float = 50.0
    p99_ms: float = 200.0
    token_cost_class: str = "low"
    monetary_cost_class: str = "free"


@dataclass
class Reliability:
    success_rate_30d: float = 1.0
    invocations_30d: int = 0
    last_failure_at: Optional[str] = None


@dataclass
class Availability:
    requires_auth: bool = False
    connected: bool = True                 # fast-changing, session-overlay in prod
    platform_constraints: List[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class CatalogEntry:
    id: str
    type: str = "mcp_tool"                 # "mcp_tool" | "skill"
    name: str = ""
    description: str = ""
    capability_tags: List[str] = field(default_factory=list)
    domain_tags: List[str] = field(default_factory=list)
    input_types: List[str] = field(default_factory=list)
    output_types: List[str] = field(default_factory=list)   # modality.output
    cost_profile: CostProfile = field(default_factory=CostProfile)
    reliability: Reliability = field(default_factory=Reliability)
    availability: Availability = field(default_factory=Availability)
    example_intents: List[str] = field(default_factory=list)
    entry_version: int = 1
    deprecated: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def embedding_text(self) -> str:
        """Text used to build this entry's embedding (catalog_schema.md §2)."""
        parts = [self.name, self.description]
        parts.extend(self.capability_tags)
        parts.extend(self.domain_tags)
        parts.extend(self.example_intents)
        return " ".join(p for p in parts if p)
