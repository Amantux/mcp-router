"""PolicyScope / policy_scope_resolver: the routing pre-filter is backed by
`policy.engine.evaluate` (one policy implementation), deny by default."""

from __future__ import annotations

from mcprouter.api.deps_auth import SecurityConfig
from mcprouter.interfaces import ToolCandidate
from mcprouter.models import AgentPrincipal, PolicyRule
from mcprouter.policy.scope import DenyAllScope, PolicyScope, policy_scope_resolver

from .conftest import requires_db


def _cand(server_id: str, name: str, operation: str) -> ToolCandidate:
    return ToolCandidate(
        tool_id=f"{server_id}-{name}",
        server_id=server_id,
        tool_name=name,
        server_name=server_id,
        description="",
        domain=None,
        operation=operation,
        retrieval_score=1.0,
    )


def _principal(enabled: bool = True) -> AgentPrincipal:
    return AgentPrincipal(id="p1", agent_id="a1", key_hash="h", enabled=enabled, max_tools=8)


def test_read_rule_on_one_server() -> None:
    rules = [PolicyRule(id="r1", agent_id="a1", server_id="s1", max_operation="read")]
    scope = PolicyScope(_principal(), rules)
    assert scope.server_ids() == ["s1"]
    assert scope.permits(_cand("s1", "read_file", "read"))
    assert not scope.permits(_cand("s1", "write_file", "write"))
    assert not scope.permits(_cand("s1", "mystery", "unknown"))  # unknown => execute
    assert not scope.permits(_cand("s2", "read_file", "read"))


def test_no_rules_and_disabled_principal_deny_everything() -> None:
    assert PolicyScope(_principal(), []).server_ids() == []
    assert not PolicyScope(_principal(), []).permits(_cand("s1", "t", "read"))
    rules = [PolicyRule(id="r1", agent_id="a1", server_id=None, max_operation="execute")]
    off = PolicyScope(_principal(enabled=False), rules)
    assert off.server_ids() == [] and not off.permits(_cand("s1", "t", "read"))


def test_any_server_rule_means_no_server_restriction() -> None:
    rules = [
        PolicyRule(id="r1", agent_id="a1", server_id="s1", max_operation="read"),
        PolicyRule(
            id="r2", agent_id="a1", server_id=None, tool_name="search_*", max_operation="read"
        ),
    ]
    scope = PolicyScope(_principal(), rules)
    assert scope.server_ids() is None
    assert scope.permits(_cand("s9", "search_issues", "read"))
    assert not scope.permits(_cand("s9", "delete_repo", "read"))


@requires_db
def test_resolver_reads_current_rules_and_denies_unknown_agents(db) -> None:  # noqa: ANN001
    config = SecurityConfig(agent_keys_configured=True, admin_token_hash=None, max_exposed_tools=8)
    resolve = policy_scope_resolver(db, config)
    assert isinstance(resolve("nobody"), DenyAllScope)
    with db() as s:
        s.add(AgentPrincipal(agent_id="a1", key_hash="h"))
        s.add(PolicyRule(agent_id="a1", server_id="s1", max_operation="read"))
        s.commit()
    scope = resolve("a1")
    assert scope.server_ids() == ["s1"]
    assert scope.permits(_cand("s1", "read_file", "read"))
    assert not scope.permits(_cand("s1", "write_file", "write"))


@requires_db
def test_dev_principal_only_in_dev_mode(db) -> None:  # noqa: ANN001
    """Review N-1: 'dev' without a row is the synthetic principal ONLY while dev
    mode is active (no keys, no admin token, no principals)."""
    open_cfg = SecurityConfig(
        agent_keys_configured=False, admin_token_hash=None, max_exposed_tools=8
    )
    assert isinstance(policy_scope_resolver(db, open_cfg)("dev"), PolicyScope)
    locked = SecurityConfig(agent_keys_configured=True, admin_token_hash=None, max_exposed_tools=8)
    assert isinstance(policy_scope_resolver(db, locked)("dev"), DenyAllScope)
