"""Routing pre-filter (`interfaces.ScopeFilter`) backed by the ONE policy
implementation, `policy.engine.evaluate`.

Routing narrows within this scope and never widens it; the gateway re-runs
`evaluate` on exposure and the execution manager on every call, so this is a
pre-filter for ranking, not the authorization boundary. Deny by default: an
unknown or disabled agent, or one with no rules, gets an empty scope.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.deps_auth import (
    DEV_AGENT_ID,
    SecurityConfig,
    _dev_mode_active,
    dev_principal,
)
from mcprouter.interfaces import ScopeFilter, ToolCandidate
from mcprouter.models import AgentPrincipal, MCPServerRecord, MCPToolRecord, PolicyRule
from mcprouter.policy.engine import evaluate

ScopeResolver = Callable[[str], ScopeFilter]


class DenyAllScope:
    def server_ids(self) -> list[str] | None:
        return []

    def permits(self, candidate: ToolCandidate) -> bool:
        return False


class PolicyScope:
    """A principal's rules, evaluated per candidate with `policy.engine.evaluate`."""

    def __init__(self, principal: AgentPrincipal, rules: Sequence[PolicyRule]) -> None:
        self._principal = principal
        self._rules = list(rules)

    def server_ids(self) -> list[str] | None:
        if not self._principal.enabled:
            return []
        mine = [r for r in self._rules if r.agent_id == self._principal.agent_id]
        if any(r.server_id is None for r in mine):
            return None  # some rule spans every server; per-tool checks still apply
        return sorted({r.server_id for r in mine if r.server_id is not None})

    def permits(self, candidate: ToolCandidate) -> bool:
        # Transient (never-added) records carrying exactly the fields evaluate reads.
        server = MCPServerRecord(id=candidate.server_id, name=candidate.server_name)
        tool = MCPToolRecord(
            server_id=candidate.server_id,
            name=candidate.tool_name,
            operation=candidate.operation,
        )
        return evaluate(self._principal, server, tool, self._rules).allow


def policy_scope_resolver(
    session_factory: sessionmaker[Session], security: SecurityConfig
) -> ScopeResolver:
    """agent_id -> PolicyScope from the CURRENT principal row and rules."""

    def resolve(agent_id: str) -> ScopeFilter:
        with session_factory() as s:
            principal = s.scalars(
                select(AgentPrincipal).where(AgentPrincipal.agent_id == agent_id)
            ).one_or_none()
            if principal is None:
                if agent_id != DEV_AGENT_ID or not _dev_mode_active(s, security):
                    return DenyAllScope()
                # The synthetic dev principal has no row (dev mode only); it is
                # still subject to deny-by-default rules.
                principal = dev_principal(security)
            rules = list(s.scalars(select(PolicyRule).where(PolicyRule.agent_id == agent_id)))
            s.expunge_all()
        return PolicyScope(principal, rules)

    return resolve
