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

from mcprouter.models import (
    AgentPrincipal,
    MCPServerRecord,
    MCPToolRecord,
    PolicyRule,
    SkillRecord,
    SkillSourceRecord,
)

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


def rule_kind(rule: PolicyRule) -> str:
    """A rule's resource kind. NULL (a transient, unflushed rule) is "tool"."""
    return rule.resource_kind or "tool"


def rule_matches(
    rule: PolicyRule, agent_id: str, server_id: str, tool_name: str, kind: str = "tool"
) -> bool:
    """Kind-separated: a tool rule never matches a skill and vice versa. For a
    skill, `server_id` is the skill SOURCE id and `tool_name` the skill name."""
    if rule_kind(rule) != kind:
        return False
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

    return _decide(principal, tool.operation, tool.server_id, tool.name, "tool", rules)


def evaluate_skill(
    principal: AgentPrincipal,
    source: SkillSourceRecord,
    skill: SkillRecord,
    rules: Sequence[PolicyRule],
) -> Decision:
    """`evaluate` for an Agent Skill: only `resource_kind="skill"` rules match,
    keyed on the source id + skill-name glob; the ceiling applies to the
    skill's risk class (`operation`), with unknown escalated to execute.
    Score-free by construction, like `evaluate`."""
    if not principal.enabled:
        return Decision(False, False, "principal disabled")
    if skill.source_id != source.id:
        return Decision(False, False, "skill/source mismatch")
    return _decide(principal, skill.operation, skill.source_id, skill.name, "skill", rules)


def _decide(
    principal: AgentPrincipal,
    operation: str | None,
    container_id: str,
    name: str,
    kind: str,
    rules: Sequence[PolicyRule],
) -> Decision:
    op = effective_operation(operation)
    needed = OPERATION_RANK[op]
    matched = [r for r in rules if rule_matches(r, principal.agent_id, container_id, name, kind)]
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
