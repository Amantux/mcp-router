"""Wave-4 integration wiring guards: the real app builds SkillExposure with the
kind-aware policy bridge (not deny-all, not a fake), routes resolve in the
right order, and the bridge fails closed for unknown/disabled principals."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.app import create_app
from mcprouter.models import AgentPrincipal, PolicyRule, SkillRecord, SkillSourceRecord
from mcprouter.policy.skill_bridge import EngineSkillPolicy
from mcprouter.settings import Settings
from tests.conftest import TEST_DB_URL, requires_db

pytestmark = requires_db


def _seed(db: sessionmaker[Session], *, enabled: bool, rule: bool) -> tuple[str, str]:
    with db() as s:
        src = SkillSourceRecord(name="wiring", kind="directory", location="/nonexistent")
        s.add(src)
        s.flush()
        sk = SkillRecord(
            source_id=src.id,
            name="wiring-skill",
            description="d",
            body="b",
            relative_path="wiring-skill",
            content_hash="0" * 64,
            manifest_hash="0" * 64,
            operation="read",
        )
        s.add(sk)
        s.add(AgentPrincipal(agent_id="wa", key_hash="x", enabled=enabled))
        if rule:
            s.add(
                PolicyRule(
                    agent_id="wa", server_id=None, max_operation="read", resource_kind="skill"
                )
            )
        s.commit()
        return sk.id, src.id


def _check(db: sessionmaker[Session], agent: str, sk_id: str, src_id: str) -> bool:
    with db() as s:
        sk, src = s.get(SkillRecord, sk_id), s.get(SkillSourceRecord, src_id)
        assert sk is not None and src is not None
        return EngineSkillPolicy(db).check(agent, sk, src)[0]


def test_bridge_allows_ruled_enabled_agent(db: sessionmaker[Session]) -> None:
    sk, src = _seed(db, enabled=True, rule=True)
    assert _check(db, "wa", sk, src) is True


def test_bridge_denies_without_skill_rule(db: sessionmaker[Session]) -> None:
    sk, src = _seed(db, enabled=True, rule=False)
    assert _check(db, "wa", sk, src) is False


def test_bridge_denies_disabled_and_unknown_principal(db: sessionmaker[Session]) -> None:
    sk, src = _seed(db, enabled=False, rule=True)
    assert _check(db, "wa", sk, src) is False
    assert _check(db, "nobody", sk, src) is False


def test_app_wires_exposure_with_engine_bridge_and_route_order(db: sessionmaker[Session]) -> None:
    app = create_app(Settings(database_url=TEST_DB_URL), env={})
    exp = app.state.skill_exposure
    assert isinstance(exp._policy, EngineSkillPolicy)
    assert callable(app.state.skill_post_sync)
    from mcprouter.api import routes_skill_sources, routes_skills

    included = [getattr(r, "original_router", r) for r in app.routes]
    assert routes_skill_sources.router in included
    # agent_router BEFORE the admin router: /skills/bundle must not hit /{skill_id}.
    assert included.index(routes_skills.agent_router) < included.index(routes_skills.router)
