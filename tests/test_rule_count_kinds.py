"""Delete guards count only their own resource_kind (B4)."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from mcprouter.discovery.registry import referencing_rule_count
from mcprouter.models import PolicyRule
from mcprouter.skills.sources import referencing_rules

from .conftest import requires_db

pytestmark = requires_db


def test_rule_counts_are_kind_scoped(db: sessionmaker[Session]) -> None:
    sid = str(uuid4())
    with db() as s:
        s.add(PolicyRule(agent_id="rk", server_id=sid, max_operation="read", resource_kind="skill"))
        s.commit()
        assert referencing_rule_count(s, sid) == 0
        assert referencing_rules(s, sid) == 1
        s.add(PolicyRule(agent_id="rk", server_id=sid, max_operation="read", resource_kind="tool"))
        s.commit()
        assert referencing_rule_count(s, sid) == 1
        assert referencing_rules(s, sid) == 1
