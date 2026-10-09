"""Bridge S3's `SkillPolicy` protocol onto the ONE policy engine.

`SkillExposure` asks `check(agent_id, skill, source)`; this loads the agent's
principal + rules fresh (a disable or rule edit takes effect on the next call)
and delegates to `policy.engine.evaluate_skill`. `check_many` answers a whole
bundle with ONE session and two queries (principal + rules are identical for
every skill of one agent). Fail-closed: an unknown or
disabled principal is denied, and a decision that would need approval is
denied too (skill activation has no approval flow).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.models import AgentPrincipal, PolicyRule, SkillRecord, SkillSourceRecord
from mcprouter.policy.engine import evaluate_skill

NOT_PERMITTED = "skill not permitted for this agent"


class EngineSkillPolicy:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    def check(
        self, agent_id: str, skill: SkillRecord, source: SkillSourceRecord
    ) -> tuple[bool, str]:
        return self.check_many(agent_id, [(skill, source)])[0]

    def check_many(
        self, agent_id: str, items: Sequence[tuple[SkillRecord, SkillSourceRecord]]
    ) -> list[tuple[bool, str]]:
        """One verdict per (skill, source), in order; 2 queries total."""
        if not items:
            return []
        with self._factory() as s:
            principal = s.scalars(
                select(AgentPrincipal).where(AgentPrincipal.agent_id == agent_id)
            ).first()
            if principal is None or not principal.enabled:
                return [(False, NOT_PERMITTED)] * len(items)
            rules = list(s.scalars(select(PolicyRule).where(PolicyRule.agent_id == agent_id)).all())
        out: list[tuple[bool, str]] = []
        for skill, source in items:
            decision = evaluate_skill(principal, source, skill, rules)
            if not decision.allow or decision.requires_approval:
                out.append((False, decision.reason or NOT_PERMITTED))
            else:
                out.append((True, decision.reason))
        return out
