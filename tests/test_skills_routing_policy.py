"""Wave 4 S2: policy kind separation for Agent Skills (PolicyRule.resource_kind)."""

from __future__ import annotations

from mcprouter.interfaces import ToolCandidate
from mcprouter.models import (
    AgentPrincipal,
    MCPServerRecord,
    MCPToolRecord,
    PolicyRule,
    SkillRecord,
    SkillSourceRecord,
)
from mcprouter.policy.engine import evaluate, evaluate_skill
from mcprouter.policy.scope import PolicyScope

AGENT = "agent-a"
SID = "src-1"  # deliberately ALSO used as an MCP server id to prove kind separation


def _p() -> AgentPrincipal:
    return AgentPrincipal(agent_id=AGENT, enabled=True, max_tools=8, max_skills=3)


def _rule(kind: str | None, op: str = "execute", name: str | None = None) -> PolicyRule:
    return PolicyRule(
        id=f"r-{kind}",
        agent_id=AGENT,
        resource_kind=kind,
        server_id=SID,
        tool_name=name,
        max_operation=op,
        requires_approval=False,
    )


def _skill(op: str = "read", name: str = "pdf-fill") -> tuple[SkillSourceRecord, SkillRecord]:
    return SkillSourceRecord(id=SID, name="s"), SkillRecord(source_id=SID, name=name, operation=op)


def _tool(name: str = "pdf-fill") -> tuple[MCPServerRecord, MCPToolRecord]:
    return MCPServerRecord(id=SID, name="m"), MCPToolRecord(
        server_id=SID, name=name, operation="read"
    )


def test_tool_rule_never_matches_skill() -> None:
    src, sk = _skill()
    d = evaluate_skill(_p(), src, sk, [_rule("tool")])
    assert not d.allow and d.reason == "no matching policy rule"


def test_unflushed_rule_without_kind_is_a_tool_rule() -> None:
    src, sk = _skill()
    assert not evaluate_skill(_p(), src, sk, [_rule(None)]).allow
    srv, t = _tool()
    assert evaluate(_p(), srv, t, [_rule(None)]).allow


def test_skill_rule_never_matches_tool() -> None:
    srv, t = _tool()
    d = evaluate(_p(), srv, t, [_rule("skill")])
    assert not d.allow and d.reason == "no matching policy rule"


def test_skill_rule_matches_skill_with_name_glob_and_ceiling() -> None:
    src, sk = _skill("write")
    assert evaluate_skill(_p(), src, sk, [_rule("skill", "write", "pdf-*")]).allow
    assert not evaluate_skill(_p(), src, sk, [_rule("skill", "write", "docx-*")]).allow
    assert not evaluate_skill(_p(), src, sk, [_rule("skill", "read")]).allow


def test_unknown_skill_risk_is_execute() -> None:
    src, sk = _skill("unknown")
    assert not evaluate_skill(_p(), src, sk, [_rule("skill", "write")]).allow
    assert evaluate_skill(_p(), src, sk, [_rule("skill", "execute")]).allow


def test_skill_source_mismatch_denied() -> None:
    src, sk = _skill()
    sk.source_id = "other"
    assert not evaluate_skill(_p(), src, sk, [_rule("skill")]).allow


def _cand(kind: str) -> ToolCandidate:
    return ToolCandidate(
        tool_id="x", server_id=SID, server_name="s", tool_name="pdf-fill",
        description="", domain=None, operation="read", retrieval_score=1.0, kind=kind,
    )  # fmt: skip


def test_scope_filter_is_kind_aware_both_directions() -> None:
    only_skill = PolicyScope(_p(), [_rule("skill")])
    assert only_skill.permits(_cand("skill"))
    assert not only_skill.permits(_cand("tool"))
    only_tool = PolicyScope(_p(), [_rule("tool")])
    assert only_tool.permits(_cand("tool"))
    assert not only_tool.permits(_cand("skill"))


def test_server_ids_ignores_skill_rules_and_fingerprint_covers_kind() -> None:
    # A skill rule with NULL server_id must not widen MCP server scope to "all".
    wide_skill = _rule("skill")
    wide_skill.server_id = None
    assert PolicyScope(_p(), [wide_skill]).server_ids() == []
    # Same id, only the kind differs: the route cache must not serve one for the other.
    as_skill, as_tool = _rule("skill"), _rule("tool")
    as_tool.id = as_skill.id
    assert PolicyScope(_p(), [as_skill]).fingerprint() != PolicyScope(_p(), [as_tool]).fingerprint()
