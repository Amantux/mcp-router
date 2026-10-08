"""Deterministic authorization engine (FR-07). Deny by default.

`evaluate` is a pure function of (principal, server, tool, rules). It takes no
routing score, rank, or confidence input BY CONSTRUCTION — routing can choose
what to *show*, never what is *allowed* (`test_evaluate_signature_is_score_free`
pins the signature).

Semantics:

* A rule MATCHES when `rule.agent_id == principal.agent_id` (exact string, no
  glob), `rule.server_id` is None or equals the tool's server, and
  `rule.tool_name` is None or `fnmatchcase(tool.name, rule.tool_name)`.
* A matching rule PERMITS when the tool's effective operation is within its
  ceiling (read < write < execute). The tool's `operation` is the classified
  one; anything other than read/write/execute — including `unknown` — is
  treated as EXECUTE so a misclassified tool fails closed. A rule whose
  `max_operation` is not one of read/write/execute permits nothing.
* `requires_approval` is STICKY: if ANY matching rule requires approval, an
  allowed call requires approval, even when a broader rule would allow it
  outright. A narrow "this needs a human" rule must not be undone by a wide one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase

from mcprouter.models import AgentPrincipal, MCPServerRecord, MCPToolRecord, PolicyRule

OPERATION_RANK: dict[str, int] = {"read": 0, "write": 1, "execute": 2}
MOST_RESTRICTED = "execute"


@dataclass(frozen=True)
class Decision:
    allow: bool
    requires_approval: bool
    reason: str  # curated; safe to return to the caller and to audit
    rule_id: str | None = None


def effective_operation(operation: str | None) -> str:
    """Classified operation, with anything unrecognized escalated to execute."""
    if operation in OPERATION_RANK:
        return operation
    return MOST_RESTRICTED


def rule_matches(rule: PolicyRule, agent_id: str, server_id: str, tool_name: str) -> bool:
    if rule.agent_id != agent_id:
        return False
    if rule.server_id is not None and rule.server_id != server_id:
        return False
    return rule.tool_name is None or fnmatchcase(tool_name, rule.tool_name)


def evaluate(
    principal: AgentPrincipal,
    server: MCPServerRecord,
    tool: MCPToolRecord,
    rules: Sequence[PolicyRule],
) -> Decision:
    if not principal.enabled:
        return Decision(False, False, "principal disabled")
    if tool.server_id != server.id:
        return Decision(False, False, "tool/server mismatch")

    op = effective_operation(tool.operation)
    needed = OPERATION_RANK[op]
    matched = [r for r in rules if rule_matches(r, principal.agent_id, tool.server_id, tool.name)]
    if not matched:
        return Decision(False, False, "no matching policy rule")

    permitting = [
        r
        for r in matched
        if r.max_operation in OPERATION_RANK and OPERATION_RANK[r.max_operation] >= needed
    ]
    if not permitting:
        return Decision(False, False, f"operation '{op}' exceeds policy ceiling")

    approval = any(r.requires_approval for r in matched)
    # Deterministic attribution: the first permitting rule by id.
    chosen = min(permitting, key=lambda r: r.id or "")
    reason = "allowed; approval required" if approval else "allowed"
    return Decision(True, approval, reason, chosen.id)
