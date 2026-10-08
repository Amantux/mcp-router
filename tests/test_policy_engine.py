"""Deterministic policy engine — deny by default (FR-07). Pure unit tests, no DB."""

from __future__ import annotations

import inspect

import pytest

from mcprouter.models import AgentPrincipal, MCPServerRecord, MCPToolRecord, PolicyRule
from mcprouter.policy.engine import Decision, effective_operation, evaluate


def _principal(agent_id: str = "alice", enabled: bool = True) -> AgentPrincipal:
    return AgentPrincipal(id="p-" + agent_id, agent_id=agent_id, key_hash="x", enabled=enabled)


def _server(sid: str = "srv-1", name: str = "github") -> MCPServerRecord:
    return MCPServerRecord(id=sid, name=name, transport="stdio", enabled=True)


def _tool(name: str = "list_issues", op: str = "read", sid: str = "srv-1") -> MCPToolRecord:
    return MCPToolRecord(id="t-" + name, server_id=sid, name=name, operation=op, schema_hash="h")


def _rule(
    agent_id: str = "alice",
    server_id: str | None = None,
    tool_name: str | None = None,
    max_operation: str = "read",
    requires_approval: bool = False,
    rid: str = "r1",
) -> PolicyRule:
    return PolicyRule(
        id=rid,
        agent_id=agent_id,
        server_id=server_id,
        tool_name=tool_name,
        max_operation=max_operation,
        requires_approval=requires_approval,
    )


def test_no_rules_denies() -> None:
    d = evaluate(_principal(), _server(), _tool(), [])
    assert d == Decision(allow=False, requires_approval=False, reason="no matching policy rule")


def test_matching_read_rule_allows_read_tool() -> None:
    d = evaluate(_principal(), _server(), _tool(op="read"), [_rule()])
    assert d.allow and not d.requires_approval
    assert d.rule_id == "r1"


def test_rule_for_other_agent_does_not_apply() -> None:
    d = evaluate(_principal("mallory"), _server(), _tool(), [_rule(agent_id="alice")])
    assert not d.allow


def test_agent_id_is_exact_not_glob() -> None:
    # A rule agent_id containing glob chars must not match other agents.
    d = evaluate(_principal("alice"), _server(), _tool(), [_rule(agent_id="*")])
    assert not d.allow


def test_server_scoped_rule() -> None:
    rules = [_rule(server_id="srv-1")]
    assert evaluate(_principal(), _server("srv-1"), _tool(sid="srv-1"), rules).allow
    assert not evaluate(_principal(), _server("srv-2"), _tool(sid="srv-2"), rules).allow


def test_server_id_compared_on_tool_not_only_server_arg() -> None:
    # Inconsistent (server, tool) pair: tool belongs to srv-2 but caller passes srv-1.
    d = evaluate(_principal(), _server("srv-1"), _tool(sid="srv-2"), [_rule(server_id="srv-1")])
    assert not d.allow
    assert d.reason == "tool/server mismatch"


@pytest.mark.parametrize(
    "pattern,name,ok",
    [
        ("list_*", "list_issues", True),
        ("list_*", "delete_issue", False),
        ("list_issues", "list_issues", True),
        ("list_issues", "LIST_ISSUES", False),  # case-sensitive on every OS
        ("*", "anything", True),
        ("list_?ssues", "list_issues", True),
    ],
)
def test_tool_glob(pattern: str, name: str, ok: bool) -> None:
    d = evaluate(_principal(), _server(), _tool(name=name), [_rule(tool_name=pattern)])
    assert d.allow is ok


@pytest.mark.parametrize(
    "ceiling,op,ok",
    [
        ("read", "read", True),
        ("read", "write", False),
        ("read", "execute", False),
        ("write", "read", True),
        ("write", "write", True),
        ("write", "execute", False),
        ("execute", "execute", True),
        ("execute", "write", True),
    ],
)
def test_operation_ceiling(ceiling: str, op: str, ok: bool) -> None:
    d = evaluate(_principal(), _server(), _tool(op=op), [_rule(max_operation=ceiling)])
    assert d.allow is ok


@pytest.mark.parametrize("ceiling", ["read", "write"])
def test_unknown_operation_is_treated_as_execute(ceiling: str) -> None:
    """A misclassified tool must fail closed (mutation-checked guard)."""
    d = evaluate(_principal(), _server(), _tool(op="unknown"), [_rule(max_operation=ceiling)])
    assert not d.allow
    assert "execute" in d.reason


@pytest.mark.parametrize("op", ["", "READ", "delete", "admin", None])
def test_unrecognized_operation_values_are_execute(op: str | None) -> None:
    assert effective_operation(op) == "execute"


def test_unknown_operation_allowed_only_under_execute_ceiling() -> None:
    d = evaluate(_principal(), _server(), _tool(op="unknown"), [_rule(max_operation="execute")])
    assert d.allow


@pytest.mark.parametrize("bad", ["", "EXECUTE", "admin", "unknown", "*"])
def test_invalid_rule_ceiling_grants_nothing(bad: str) -> None:
    d = evaluate(_principal(), _server(), _tool(op="read"), [_rule(max_operation=bad)])
    assert not d.allow


def test_disabled_principal_denied_even_with_rules() -> None:
    d = evaluate(_principal(enabled=False), _server(), _tool(), [_rule(max_operation="execute")])
    assert not d.allow
    assert d.reason == "principal disabled"


def test_requires_approval_on_matching_rule_forces_approval_even_for_reads() -> None:
    d = evaluate(_principal(), _server(), _tool(op="read"), [_rule(requires_approval=True)])
    assert d.allow and d.requires_approval


def test_requires_approval_is_sticky_across_rules() -> None:
    """A broad no-approval rule must not undo a narrower approval requirement."""
    rules = [
        _rule(tool_name="*", max_operation="execute", rid="broad"),
        _rule(tool_name="delete_*", max_operation="read", requires_approval=True, rid="narrow"),
    ]
    d = evaluate(_principal(), _server(), _tool(name="delete_repo", op="execute"), rules)
    assert d.allow and d.requires_approval


def test_approval_rule_for_other_tool_does_not_leak() -> None:
    rules = [
        _rule(tool_name="*", max_operation="execute", rid="broad"),
        _rule(tool_name="delete_*", requires_approval=True, rid="narrow"),
    ]
    d = evaluate(_principal(), _server(), _tool(name="list_issues"), rules)
    assert d.allow and not d.requires_approval


def test_rules_of_other_agents_in_list_are_ignored() -> None:
    rules = [_rule(agent_id="bob", max_operation="execute")]
    assert not evaluate(_principal("alice"), _server(), _tool(), rules).allow


def test_evaluate_signature_is_score_free() -> None:
    """Routing scores never widen access: policy takes no score input BY CONSTRUCTION."""
    params = set(inspect.signature(evaluate).parameters)
    assert params == {"principal", "server", "tool", "rules"}
    for name in params:
        assert "score" not in name and "rank" not in name and "confidence" not in name
