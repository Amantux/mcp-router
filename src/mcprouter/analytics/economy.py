"""Context economy — the headline "tokens not sent" number, with its parts.

Per routing decision that surfaced at least one tool ("served"):

    exposed_tokens = sum(est_tokens(t) for t in decision.selected_tool_ids)
    catalog_tokens = sum(est_tokens(t) for t in the agent's AUTHORIZED catalog)
    tokens_not_sent = catalog_tokens - exposed_tokens

and aggregated:  savings = 1 - sum(exposed) / sum(catalog).

Counterfactual being measured: without the router, the agent's context
would carry its whole authorized catalog for that turn. Honest
approximations, all surfaced on the wire so the number can be audited:

* AUTHORIZED CATALOG = CURRENT scope, not scope-at-decision-time: the
  agent's current principal + PolicyRules evaluated with the ONE policy
  implementation (`policy.engine.evaluate`) over tools that are currently
  enabled + available on enabled servers. Rule or catalog changes therefore
  re-price old decisions. An agent id without a principal row (e.g. the dev
  agent) is evaluated as an enabled principal against its rules; an agent
  with an empty current scope has catalog 0 and its decisions are reported
  as `unscoredDecisions`, excluded from the ratio.
* Token counts are the chars/4 ESTIMATE from `analytics.tokens` over
  CURRENT tool definitions (a deleted tool counts 0 exposed tokens).
* No-match decisions (nothing surfaced) are EXCLUDED from savings — a miss
  is not a saving — and counted separately.
* Raw tables only (no rollup): per-agent catalogs are not rolled up.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from mcprouter.analytics.funnel import SURF_CTE, ratio
from mcprouter.analytics.tokens import ESTIMATOR
from mcprouter.analytics.window import Window
from mcprouter.models import AgentPrincipal, MCPServerRecord, MCPToolRecord, PolicyRule
from mcprouter.policy.engine import evaluate

CATALOG_BASIS = "current_policy_scope"

_AGENT_TOOL_SQL = text(
    "WITH"
    + SURF_CTE
    + """
SELECT s.agent_id, s.tool_id, count(*) AS n
FROM surf s
GROUP BY s.agent_id, s.tool_id
"""
)

_AGENT_DECISIONS_SQL = text(
    """
SELECT d.agent_id,
       count(*) AS decisions,
       count(*) FILTER (
           WHERE jsonb_typeof(d.selected_tool_ids::jsonb) = 'array'
             AND jsonb_array_length(d.selected_tool_ids::jsonb) > 0
       ) AS served
FROM routing_decisions d
WHERE d.created_at >= :start AND d.created_at < :end
GROUP BY d.agent_id
"""
)


@dataclass
class Economy:
    served_decisions: int = 0
    unscored_decisions: int = 0  # served, but the agent's current catalog is empty
    no_match_decisions: int = 0
    exposed_tokens: int = 0
    catalog_tokens: int = 0
    catalog_tokens_per_decision: int | None = None  # per agent only

    @property
    def tokens_not_sent(self) -> int:
        return self.catalog_tokens - self.exposed_tokens

    @property
    def savings(self) -> float | None:
        r = ratio(self.exposed_tokens, self.catalog_tokens)
        return None if r is None else 1.0 - r

    def add(self, other: Economy) -> None:
        self.served_decisions += other.served_decisions
        self.unscored_decisions += other.unscored_decisions
        self.no_match_decisions += other.no_match_decisions
        self.exposed_tokens += other.exposed_tokens
        self.catalog_tokens += other.catalog_tokens


def agent_catalog_tokens(
    session: Session, agent_ids: list[str], tokens: dict[str, int]
) -> dict[str, int]:
    """agent_id -> estimated tokens of its CURRENT authorized catalog."""
    if not agent_ids:
        return {}
    pairs = session.execute(
        select(MCPToolRecord, MCPServerRecord)
        .join(MCPServerRecord, MCPServerRecord.id == MCPToolRecord.server_id)
        .where(MCPToolRecord.enabled, MCPToolRecord.available, MCPServerRecord.enabled)
    ).all()
    principals = {
        p.agent_id: p
        for p in session.scalars(
            select(AgentPrincipal).where(AgentPrincipal.agent_id.in_(agent_ids))
        ).all()
    }
    rules: dict[str, list[PolicyRule]] = defaultdict(list)
    for r in session.scalars(select(PolicyRule).where(PolicyRule.agent_id.in_(agent_ids))).all():
        rules[r.agent_id].append(r)
    out: dict[str, int] = {}
    for agent in agent_ids:
        principal = principals.get(agent) or AgentPrincipal(
            id="", agent_id=agent, key_hash="", enabled=True
        )
        agent_rules = rules.get(agent, [])
        out[agent] = (
            sum(
                tokens.get(t.id, 0)
                for t, srv in pairs
                if evaluate(principal, srv, t, agent_rules).allow
            )
            if agent_rules
            else 0
        )
    return out


def economy_by_agent(
    session: Session, window: Window, tokens: dict[str, int]
) -> dict[str, Economy]:
    params = {"start": window.start, "end": window.end, "skip_days": []}
    decisions = {
        a: (int(n), int(served))
        for a, n, served in session.execute(_AGENT_DECISIONS_SQL, params).all()
    }
    exposed: dict[str, int] = defaultdict(int)
    for agent, tid, n in session.execute(_AGENT_TOOL_SQL, params).all():
        exposed[agent] += int(n) * tokens.get(tid, 0)
    catalogs = agent_catalog_tokens(session, sorted(decisions), tokens)
    out: dict[str, Economy] = {}
    for agent, (n, served) in decisions.items():
        per = catalogs.get(agent, 0)
        scored = per > 0
        out[agent] = Economy(
            served_decisions=served if scored else 0,
            unscored_decisions=0 if scored else served,
            no_match_decisions=n - served,
            exposed_tokens=exposed[agent] if scored else 0,
            catalog_tokens=served * per,
            catalog_tokens_per_decision=per,
        )
    return out


def overall(by_agent: dict[str, Economy]) -> Economy:
    acc = Economy()
    for e in by_agent.values():
        acc.add(e)
    return acc


__all__ = ["CATALOG_BASIS", "ESTIMATOR", "Economy", "economy_by_agent", "overall"]
