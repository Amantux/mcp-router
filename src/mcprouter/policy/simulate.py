"""Policy dry-run simulation (FR-07).

Evaluates a candidate policy rule against an agent's catalog and recent
execution history without persisting any changes.

The simulate() function computes:
- newly_permitted: pairs (tool/skill) that would become allowed due to the candidate
- approval_added: pairs already allowed but would require approval under the candidate
- recent: execution history replay over a lookback window

All outputs are read-only: no database writes, no policy generation bump.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from mcprouter.models import (
    AgentPrincipal,
    ExecutionRecord,
    MCPServerRecord,
    MCPToolRecord,
    PolicyRule,
    SkillRecord,
    SkillSourceRecord,
)
from mcprouter.policy.engine import Decision, evaluate, evaluate_skill


def _evaluate_with_rules(
    principal: AgentPrincipal,
    server: MCPServerRecord,
    tool: MCPToolRecord,
    rules: Sequence[PolicyRule],
) -> Decision:
    """Wrapper around evaluate for testing."""
    return evaluate(principal, server, tool, rules)


def _evaluate_skill_with_rules(
    principal: AgentPrincipal,
    source: SkillSourceRecord,
    skill: SkillRecord,
    rules: Sequence[PolicyRule],
) -> Decision:
    """Wrapper around evaluate_skill for testing."""
    return evaluate_skill(principal, source, skill, rules)


def _pairs_for_agent(
    s,
    principal: AgentPrincipal,
) -> tuple[list[tuple[MCPServerRecord, MCPToolRecord]], list[tuple[SkillSourceRecord, SkillRecord]]]:
    """Return (tool_pairs, skill_pairs) for the agent's accessible catalog."""
    # Tools: all tools from servers the agent can access
    # For simplicity, we include all tools from all enabled servers
    tool_pairs: list[tuple[MCPServerRecord, MCPToolRecord]] = []
    servers = s.scalars(
        select(MCPServerRecord).where(MCPServerRecord.enabled == True)  # noqa: E712
    ).all()
    for srv in servers:
        tools = s.scalars(
            select(MCPToolRecord).where(
                MCPToolRecord.server_id == srv.id, MCPToolRecord.enabled == True  # noqa: E712
            )
        ).all()
        for tool in tools:
            tool_pairs.append((srv, tool))

    # Skills: all skills from skill sources the agent can access
    skill_pairs: list[tuple[SkillSourceRecord, SkillRecord]] = []
    sources = s.scalars(
        select(SkillSourceRecord).where(SkillSourceRecord.enabled == True)  # noqa: E712
    ).all()
    for src in sources:
        skills = s.scalars(
            select(SkillRecord).where(
                SkillRecord.source_id == src.id, SkillRecord.enabled == True  # noqa: E712
            )
        ).all()
        for skill in skills:
            skill_pairs.append((src, skill))

    return tool_pairs, skill_pairs


def _current_decisions(
    principal: AgentPrincipal,
    tool_pairs: list[tuple[MCPServerRecord, MCPToolRecord]],
    skill_pairs: list[tuple[SkillSourceRecord, SkillRecord]],
    existing_rules: Sequence[PolicyRule],
) -> dict[str, Decision]:
    """Compute current decisions for all tool/skill pairs."""
    decisions: dict[str, Decision] = {}

    # Tools
    for server, tool in tool_pairs:
        decision = _evaluate_with_rules(principal, server, tool, existing_rules)
        key = f"tool:{tool.id}"
        decisions[key] = decision

    # Skills
    for source, skill in skill_pairs:
        decision = _evaluate_skill_with_rules(principal, source, skill, existing_rules)
        key = f"skill:{skill.id}"
        decisions[key] = decision

    return decisions


def _candidate_decisions(
    principal: AgentPrincipal,
    tool_pairs: list[tuple[MCPServerRecord, MCPToolRecord]],
    skill_pairs: list[tuple[SkillSourceRecord, SkillRecord]],
    existing_rules: Sequence[PolicyRule],
    candidate_rule: PolicyRule,
) -> dict[str, Decision]:
    """Compute decisions with candidate rule added."""
    combined_rules = list(existing_rules) + [candidate_rule]
    decisions: dict[str, Decision] = {}

    # Tools
    for server, tool in tool_pairs:
        decision = _evaluate_with_rules(principal, server, tool, combined_rules)
        key = f"tool:{tool.id}"
        decisions[key] = decision

    # Skills
    for source, skill in skill_pairs:
        decision = _evaluate_skill_with_rules(principal, source, skill, combined_rules)
        key = f"skill:{skill.id}"
        decisions[key] = decision

    return decisions


def _compute_delta(
    current: dict[str, Decision],
    candidate: dict[str, Decision],
) -> tuple[list[dict], list[dict]]:
    """Compute newly_permitted and approval_added from current vs candidate decisions.

    Returns:
        (newly_permitted, approval_added)
        - newly_permitted: allow false->true
        - approval_added: allow true, requires_approval false->true
    """
    newly_permitted: list[dict] = []
    approval_added: list[dict] = []

    all_keys = set(current.keys()) | set(candidate.keys())

    for key in all_keys:
        current_dec = current.get(key)
        candidate_dec = candidate.get(key)

        # If no decision in candidate, skip (shouldn't happen with full catalog)
        if candidate_dec is None:
            continue

        # If currently denied, check if candidate allows
        if current_dec is None or not current_dec.allow:
            if candidate_dec.allow:
                newly_permitted.append({"id": key, "requires_approval": candidate_dec.requires_approval})
        else:
            # Currently allowed: check if approval requirement changed
            if current_dec.allow and not current_dec.requires_approval:
                if candidate_dec.requires_approval:
                    approval_added.append({"id": key, "reason": candidate_dec.reason})

    return newly_permitted, approval_added


def _replay_recent_history(
    s,
    principal: AgentPrincipal,
    existing_rules: Sequence[PolicyRule],
    candidate_rule: PolicyRule,
    window_days: int,
) -> dict:
    """Replay recent execution records with current and candidate rules.

    Returns counts for attempts, would_permit, of_which_approval,
    currently_denied_now_permitted.
    """
    cutoff = datetime.now(UTC) - timedelta(days=window_days)

    # Get execution records for this agent within the window
    records = s.scalars(
        select(ExecutionRecord)
        .where(
            ExecutionRecord.agent_id == principal.agent_id,
            ExecutionRecord.created_at >= cutoff,
        )
        .order_by(ExecutionRecord.created_at.desc())
    ).all()

    # Build rule sets
    combined_rules = list(existing_rules) + [candidate_rule]

    # Counters
    attempts = len(records)
    would_permit = 0
    of_which_approval = 0
    currently_denied_now_permitted = 0

    for record in records:
        # Determine if this record is for a tool or skill
        if record.resource_kind == "skill" and record.skill_id:
            # Skill execution
            skill = s.get(SkillRecord, record.skill_id)
            if skill:
                source = s.get(SkillSourceRecord, skill.source_id)
                if source:
                    current_dec = _evaluate_skill_with_rules(principal, source, skill, existing_rules)
                    candidate_dec = _evaluate_skill_with_rules(principal, source, skill, combined_rules)
                else:
                    continue
            else:
                continue
        else:
            # Tool execution
            tool = s.get(MCPToolRecord, record.tool_id)
            if tool:
                server = s.get(MCPServerRecord, tool.server_id)
                if server:
                    current_dec = _evaluate_with_rules(principal, server, tool, existing_rules)
                    candidate_dec = _evaluate_with_rules(principal, server, tool, combined_rules)
                else:
                    continue
            else:
                continue

        # Count logic
        if candidate_dec.allow:
            would_permit += 1
            if candidate_dec.requires_approval:
                of_which_approval += 1

        # Currently denied (no matching rule or exceeds ceiling) but candidate allows
        if current_dec is None or not current_dec.allow:
            if candidate_dec.allow:
                currently_denied_now_permitted += 1

    return {
        "attempts": attempts,
        "would_permit": would_permit,
        "of_which_approval": of_which_approval,
        "currently_denied_now_permitted": currently_denied_now_permitted,
    }


def simulate(
    s,
    principal: AgentPrincipal,
    candidate_rule: PolicyRule,
    window_days: int = 7,
) -> dict:
    """Simulate applying a candidate policy rule.

    Args:
        s: SQLAlchemy session
        principal: The agent principal to simulate for
        candidate_rule: The candidate policy rule to evaluate
        window_days: Lookback window for execution history replay (default 7)

    Returns:
        Dict with keys:
        - newly_permitted: list of tools/skills that would become permitted
        - approval_added: list of tools/skills that would require approval
        - recent: dict with execution history replay counts
    """
    # Get all catalog pairs for the agent
    tool_pairs, skill_pairs = _pairs_for_agent(s, principal)

    # Get existing rules for this agent (excluding the candidate if it's already saved)
    existing_rules = s.scalars(
        select(PolicyRule).where(PolicyRule.agent_id == principal.agent_id)
    ).all()

    # Filter out the candidate if it's already in existing_rules (by id)
    if candidate_rule.id:
        existing_rules = [r for r in existing_rules if r.id != candidate_rule.id]

    # Compute decisions
    current = _current_decisions(principal, tool_pairs, skill_pairs, existing_rules)
    candidate = _candidate_decisions(principal, tool_pairs, skill_pairs, existing_rules, candidate_rule)

    # Compute delta
    newly_permitted, approval_added = _compute_delta(current, candidate)

    # Replay recent history
    recent = _replay_recent_history(s, principal, existing_rules, candidate_rule, window_days)

    return {
        "newly_permitted": newly_permitted,
        "approval_added": approval_added,
        "recent": recent,
    }
