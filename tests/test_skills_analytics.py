"""Skills in the analytics funnel: "skill:<id>" entries in selected_tool_ids
are surfaced; ExecutionRecord(resource_kind="skill") rows attributed to the
owning agent's decision are activations."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.analytics import economy, service
from mcprouter.analytics import funnel as fn
from mcprouter.analytics import tokens as tokens_mod
from mcprouter.analytics.profiles import profiles
from mcprouter.analytics.rollup import recompute_day
from mcprouter.analytics.tokens import tool_token_map
from mcprouter.analytics.window import midnight, parse_window
from mcprouter.models import ExecutionRecord, PolicyRule, SkillRecord, SkillSourceRecord

from .conftest import requires_db
from .test_analytics_support import NOW, add_decision, add_exec
from .test_execution_support import sec_db_fixture  # noqa: F401 — registers the fixture

pytestmark = requires_db


def _skill(db: sessionmaker[Session], name: str = "pdf-fill", sid: str | None = None) -> str:
    with db() as s:
        src = SkillSourceRecord(name=f"src-{name}", kind="directory", location="/x")
        s.add(src)
        s.flush()
        sk = SkillRecord(
            id=sid,
            source_id=src.id,
            name=name,
            description="Fill PDF forms",
            body="b" * 400,
            relative_path=name,
            content_hash="0" * 64,
            manifest_hash="0" * 64,
            body_tokens_est=100,
        )
        s.add(sk)
        s.commit()
        return sk.id


def _activate(db: sessionmaker[Session], agent: str, skill_id: str, rrid: str | None) -> None:
    with db() as s:
        s.add(
            ExecutionRecord(
                agent_id=agent,
                tool_id=None,
                resource_kind="skill",
                skill_id=skill_id,
                outcome="ok",
                detail="",
                latency_ms=1.0,
                created_at=NOW - timedelta(minutes=3),
                route_request_id=rrid,
            )
        )
        s.commit()


def _funnel(db: sessionmaker[Session]) -> dict[str, fn.ToolCounts]:
    with db() as s:
        return fn.merged_funnel(s, parse_window("7d", NOW), tool_token_map(s))


def test_skill_surfaced_and_activated(db: sessionmaker[Session]) -> None:
    sid = _skill(db)
    key = f"skill:{sid}"
    r1 = add_decision(db, "alice", NOW - timedelta(minutes=5), ["t-x", key])
    add_decision(db, "alice", NOW - timedelta(minutes=5), [key])
    _activate(db, "alice", sid, r1)
    c = _funnel(db)[key]
    assert (c.surfaced, c.selected, c.succeeded, c.sum_rank) == (2, 1, 1, 3)
    with db() as s:
        curve = fn.position_curve(s, parse_window("7d", NOW), key)
    assert [(p.rank, p.shown, p.selected) for p in curve] == [(2, 1, 1)]


def test_other_agents_skill_activation_never_counts(db: sessionmaker[Session]) -> None:
    sid = _skill(db)
    key = f"skill:{sid}"
    r1 = add_decision(db, "alice", NOW - timedelta(minutes=5), [key])
    _activate(db, "mallory", sid, r1)
    _activate(db, "alice", sid, None)  # unattributed: graceful, never counted
    assert _funnel(db)[key].selected == 0


def test_tool_execution_cannot_poison_a_skill_row(db: sessionmaker[Session]) -> None:
    """A tool-kind execution whose tool_id is spelled "skill:<id>" is not a
    skill activation; a skill execution never credits a same-id tool."""
    sid = _skill(db, sid="sk-short")  # short id so "skill:<id>" fits tool_id(36)
    key = f"skill:{sid}"
    r1 = add_decision(db, "alice", NOW - timedelta(minutes=5), [key, sid])
    add_exec(db, "alice", key, "ok", r1, NOW - timedelta(minutes=4))
    assert _funnel(db)[key].selected == 0  # tool-kind row is not an activation
    _activate(db, "alice", sid, r1)
    f = _funnel(db)
    assert f[key].selected == 1  # only the skill-kind row
    assert f[sid].selected == 0  # skill activation does not credit bare id


def test_simulated_skill_decisions_excluded(db: sessionmaker[Session]) -> None:
    sid = _skill(db)
    key = f"skill:{sid}"
    r1 = add_decision(db, "alice", NOW - timedelta(minutes=5), [key], model_version="simulated/m")
    _activate(db, "alice", sid, r1)
    assert key not in _funnel(db)


def test_profiles_count_skill_activation_as_attributed(db: sessionmaker[Session]) -> None:
    sid = _skill(db)
    r1 = add_decision(db, "alice", NOW - timedelta(minutes=5), [f"skill:{sid}"])
    _activate(db, "alice", sid, r1)
    with db() as s:
        _, per = profiles(s, parse_window("7d", NOW))
    assert per["alice"].attributed == 1


def test_rollup_merge_equals_live_for_mixed_day(db: sessionmaker[Session]) -> None:
    sid = _skill(db)
    key = f"skill:{sid}"
    day = (NOW - timedelta(days=3)).date()
    at = midnight(day) + timedelta(hours=12)
    r1 = add_decision(db, "alice", at, ["t-a", key])
    add_exec(db, "alice", "t-a", "ok", r1, at + timedelta(minutes=1))
    with db() as s:
        s.add(
            ExecutionRecord(
                agent_id="alice",
                tool_id=None,
                resource_kind="skill",
                skill_id=sid,
                outcome="ok",
                detail="",
                latency_ms=1.0,
                created_at=at + timedelta(minutes=1),
                route_request_id=r1,
            )
        )
        s.commit()
    w = parse_window("7d", NOW)
    with db() as s:
        live = fn.live_funnel(s, w.start, w.end, tool_token_map(s))
    with db() as s:
        recompute_day(s, day, NOW)
        s.commit()
    with db() as s:
        recompute_day(s, day, NOW)  # idempotent
        s.commit()
    merged = _funnel(db)
    for k in ("t-a", key):
        assert (merged[k].surfaced, merged[k].selected) == (live[k].surfaced, live[k].selected)
    assert merged[key].selected == 1


def test_tool_table_kind_filter_and_skill_detail(db: sessionmaker[Session]) -> None:
    sid = _skill(db)
    key = f"skill:{sid}"
    add_decision(db, "alice", NOW - timedelta(minutes=5), [key])
    w = parse_window("7d", NOW)
    with db() as s:
        sk = service.tool_table(
            s, w, sort="surfaced", descending=True, limit=500, offset=0, kind="skill"
        )
        tl = service.tool_table(
            s, w, sort="surfaced", descending=True, limit=500, offset=0, kind="tool"
        )
        det = service.tool_detail(s, w, key)
    assert [r.tool_id for r in sk.items] == [key]
    assert sk.items[0].kind == "skill" and sk.items[0].tool_name == "pdf-fill"
    assert sk.items[0].server_name == "src-pdf-fill"
    assert all(r.kind == "tool" for r in tl.items) and key not in {r.tool_id for r in tl.items}
    assert det.tool.surfaced == 1 and det.tool.kind == "skill"


def test_route_accepts_skill_ids_and_kind(sec_db: sessionmaker[Session]) -> None:
    from .test_analytics_api import H_ADMIN, _app

    sid = _skill(sec_db)
    c = TestClient(_app(sec_db))
    r = c.get(f"/api/v1/analytics/tools/skill:{sid}", headers=H_ADMIN)
    assert r.status_code == 200 and r.json()["tool"]["kind"] == "skill"
    r = c.get("/api/v1/analytics/tools?kind=skill", headers=H_ADMIN)
    assert [i["toolId"] for i in r.json()["items"]] == [f"skill:{sid}"]
    assert c.get("/api/v1/analytics/tools?kind=bogus", headers=H_ADMIN).status_code == 422


def _econ(db: sessionmaker[Session], agent: str = "alice") -> economy.Economy:
    with db() as s:
        return economy.economy_by_agent(s, parse_window("7d", NOW), tool_token_map(s))[agent]


def test_economy_prices_skills_and_never_counts_unactivated_bodies(
    db: sessionmaker[Session],
) -> None:
    a = _skill(db, "pdf-fill")
    b = _skill(db, "xlsx-edit")
    with db() as s:
        s.add(PolicyRule(agent_id="alice", resource_kind="skill", max_operation="execute"))
        s.commit()
    r1 = add_decision(db, "alice", NOW - timedelta(minutes=5), [f"skill:{a}", f"skill:{b}"])
    _activate(db, "alice", a, r1)
    # Same skill surfaced again, NOT activated there: its body is not exposed
    # on this decision even though it was activated on r1.
    add_decision(db, "alice", NOW - timedelta(minutes=4), [f"skill:{a}"])
    meta = tokens_mod.skill_metadata_tokens("pdf-fill", "Fill PDF forms")
    meta_b = tokens_mod.skill_metadata_tokens("xlsx-edit", "Fill PDF forms")
    e = _econ(db)
    assert e.stale_ref_decisions == 0 and e.served_decisions == 2
    assert e.skill_metadata_tokens == 2 * meta + meta_b
    assert e.skill_body_tokens_exposed == 100  # only a's body, only on r1
    assert e.skill_body_tokens_not_sent == 200  # b on r1 + a on the 2nd decision
    assert e.exposed_tokens == 2 * meta + meta_b + 100
    assert e.catalog_tokens == 2 * (meta + meta_b)  # authorized skills' metadata


def test_economy_unknown_skill_id_is_still_stale(db: sessionmaker[Session]) -> None:
    add_decision(db, "alice", NOW - timedelta(minutes=5), ["skill:does-not-exist"])
    assert _econ(db).stale_ref_decisions == 1
