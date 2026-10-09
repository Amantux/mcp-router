"""Bridge S3's `SkillPolicy` protocol onto the ONE policy engine.

`SkillExposure` asks `check(agent_id, skill, source)`; this loads the agent's
principal + rules fresh (a disable or rule edit takes effect on the next call)
and delegates to `policy.engine.evaluate_skill`. Fail-closed: an unknown or
disabled principal is denied, and a decision that would need approval is
denied too (skill activation has no approval flow).
"""

from __future__ import annotations

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
        with self._factory() as s:
            principal = s.scalars(
                select(AgentPrincipal).where(AgentPrincipal.agent_id == agent_id)
            ).first()
            if principal is None or not principal.enabled:
                return False, NOT_PERMITTED
            rules = list(s.scalars(select(PolicyRule).where(PolicyRule.agent_id == agent_id)).all())
        decision = evaluate_skill(principal, source, skill, rules)
        if not decision.allow or decision.requires_approval:
            return False, decision.reason or NOT_PERMITTED
        return True, decision.reason
