"""E6 performance guards: bounded dedup, statement counts and index use
(P-603, P-604, P-605, P-607). Counts come from a cursor listener, plans from
EXPLAIN — never from timing."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.dedup.detect import run_dedup
from mcprouter.models import DuplicateSuggestion
from tests.support.dedup import V_A
from tests.support.registry_fixtures import make_server, make_tool, props_schema

from .conftest import requires_db

pytestmark = requires_db


@contextmanager
def count_statements(
    engine: Engine, keep: Callable[[str], bool] = lambda _s: True
) -> Iterator[list[str]]:
    seen: list[str] = []

    def listener(conn, cursor, statement, params, context, executemany):  # noqa: ANN001, ANN202
        if keep(statement):
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", listener)


def _plan(s: Session, sql: str, params: dict[str, Any] | None = None) -> str:
    return "\n".join(r[0] for r in s.execute(text("EXPLAIN " + sql), params or {}).all())


# ------------------------------------------------------------------ P-603
def _seed_identical_tools(s: Session, n: int) -> list[str]:
    srv = make_server(s, "dupsrv")
    ids = [
        make_tool(
            s,
            srv,
            f"list_items_{i:03d}",
            "List items",
            domain="development",
            operation="read",
            input_schema=props_schema("owner"),
            embedding=V_A,
            embedding_backend="hash",
        ).id
        for i in range(n)
    ]
    s.commit()
    return ids


def test_dedup_is_capped_and_reports_truncation(db: sessionmaker[Session]) -> None:
    with db() as s:
        _seed_identical_tools(s, 400)  # 79,800 candidate pairs
        run = run_dedup(s, max_pairs=50)
        s.commit()
        assert run.truncated is True
        assert run.pairs_considered <= 50
        assert run.created == 50
        assert s.query(DuplicateSuggestion).count() == 50


def test_dedup_under_the_cap_is_not_truncated(db: sessionmaker[Session]) -> None:
    with db() as s:
        _seed_identical_tools(s, 5)  # 10 pairs
        run = run_dedup(s, max_pairs=10)
        assert (run.created, run.truncated) == (10, False)


def test_hnsw_index_serves_a_nearest_neighbour_query(db: sessionmaker[Session]) -> None:
    with db() as s:
        _seed_identical_tools(s, 20)
        s.execute(text("SET LOCAL enable_seqscan = off"))
        plan = _plan(
            s,
            "SELECT id FROM mcp_tools ORDER BY embedding <=> CAST(:q AS vector) LIMIT 5",
            {"q": str(V_A)},
        )
        s.rollback()
    assert "ix_mcp_tools_embedding_hnsw" in plan, plan


# ------------------------------------------------------------------ P-604
def test_dedup_upsert_is_set_based(db: sessionmaker[Session]) -> None:
    """200 pairs: one prior SELECT + one INSERT ... ON CONFLICT, on the first
    run (all created) and on a re-run (all refreshed); decided pairs skipped."""
    from mcprouter.dedup.detect import _bulk_upsert, _Pair

    engine = db.kw["bind"]
    pairs = [_Pair(f"a{i:03d}", f"b{i:03d}", 0.9, "why", None) for i in range(200)]
    with db() as s:
        with count_statements(engine) as first:
            assert _bulk_upsert(s, pairs, refresh_preferred=True) == (200, 0, 0)
        s.execute(
            text("UPDATE duplicate_suggestions SET status = 'dismissed' WHERE tool_a_id = 'a000'")
        )
        with count_statements(engine) as second:
            assert _bulk_upsert(s, pairs, refresh_preferred=True) == (0, 199, 1)
        s.rollback()
    assert len(first) <= 3, first
    assert len(second) <= 3, second


def test_dedup_statement_count_does_not_grow_with_pairs(db: sessionmaker[Session]) -> None:
    engine = db.kw["bind"]
    counts = []
    for n in (5, 20):  # 10 vs 190 pairs
        with db() as s:
            _seed_identical_tools(s, n)
            with count_statements(engine) as seen:
                run_dedup(s)
            s.rollback()
            counts.append(len(seen))
        with db() as s:
            s.execute(text("DELETE FROM mcp_servers WHERE name = 'dupsrv'"))
            s.execute(text("DELETE FROM duplicate_suggestions"))
            s.commit()
    assert counts[0] == counts[1], counts


# ------------------------------------------------------------------ P-605
def _skills(n: int) -> list[tuple[Any, Any]]:
    from mcprouter.models import SkillRecord, SkillSourceRecord

    src = SkillSourceRecord(id="src-1", name="src", kind="directory", location="/x")
    return [
        (SkillRecord(id=f"sk{i}", source_id="src-1", name=f"skill-{i}", operation="read"), src)
        for i in range(n)
    ]


@pytest.mark.parametrize("n", [3, 10])
def test_skill_policy_check_many_is_two_queries(db: sessionmaker[Session], n: int) -> None:
    from mcprouter.models import AgentPrincipal, PolicyRule
    from mcprouter.policy.skill_bridge import EngineSkillPolicy

    with db() as s:
        s.add(AgentPrincipal(agent_id="alice", key_hash="h"))
        s.add(
            PolicyRule(
                agent_id="alice",
                resource_kind="skill",
                server_id="src-1",
                tool_name="skill-*",
                max_operation="read",
            )
        )
        s.commit()
    policy = EngineSkillPolicy(db)
    with count_statements(
        db.kw["bind"], lambda st: st.lstrip().upper().startswith("SELECT")
    ) as seen:
        verdicts = policy.check_many("alice", _skills(n))
    assert [ok for ok, _ in verdicts] == [True] * n
    assert len(seen) == 2, seen
    assert (
        policy.check_many("nobody", _skills(2))
        == [(False, "skill not permitted for this agent")] * 2
    )


def test_latest_decision_lookup_uses_the_composite_index(db: sessionmaker[Session]) -> None:
    with db() as s:
        s.execute(
            text(
                "INSERT INTO routing_decisions (id, agent_id, query, selected_tool_ids,"
                " scores, model_version, fallback_used, created_at, latency_ms)"
                " SELECT 'rd-' || g, 'agent-' || (g % 50), 'q', '[]', '{}', 'm', false,"
                " now() - (g || ' seconds')::interval, 1.0 FROM generate_series(1, 5000) g"
            )
        )
        s.execute(text("ANALYZE routing_decisions"))
        plan = _plan(
            s,
            "SELECT selected_tool_ids FROM routing_decisions WHERE agent_id = 'agent-7'"
            " AND NOT model_version LIKE 'simulated/%'"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
        )
        s.rollback()
    assert "ix_routing_decisions_agent_latest" in plan, plan
    assert "Sort" not in plan, plan


def test_staleness_scan_timeout_is_curated_and_contained(
    db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcprouter.analytics import staleness

    monkeypatch.setattr(staleness, "STATEMENT_TIMEOUT", "50ms")
    monkeypatch.setattr(staleness, "_LAST_SURFACED_SQL", text("SELECT 'x', now() FROM pg_sleep(2)"))
    with db() as s:
        before = s.execute(text("SELECT current_setting('statement_timeout')")).scalar()
        with pytest.raises(staleness.StalenessTimeout) as info:
            staleness.last_surfaced(s)
        assert "timed out" in str(info.value)
        # The caller's transaction survives and keeps its own timeout.
        assert s.execute(text("SELECT current_setting('statement_timeout')")).scalar() == before
        s.rollback()


def test_skill_ingest_refreshes_planner_statistics(
    db: sessionmaker[Session], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcprouter.models import SkillSourceRecord
    from mcprouter.settings import Settings
    from mcprouter.skills import ingest

    calls: list[object] = []
    real = ingest.analyze_skills

    def counting(s: Any) -> Any:
        calls.append(s)
        return real(s)

    monkeypatch.setattr(ingest, "analyze_skills", counting)
    d = tmp_path / "alpha"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: alpha\ndescription: Alpha skill\n---\nbody\n")
    with db() as s:
        src = SkillSourceRecord(name="stats-src", kind="directory", location=str(tmp_path))
        s.add(src)
        s.flush()
        rep = ingest.sync_source(s, src, tmp_path, Settings())
        assert rep["added"] == 1
        assert len(calls) == 1
        ingest.sync_source(s, src, tmp_path, Settings())  # no change -> no ANALYZE
        assert len(calls) == 1
        s.rollback()


# ------------------------------------------------------------------ P-607
def test_agent_auth_is_one_indexed_query(db: sessionmaker[Session]) -> None:
    from mcprouter.auth import SecurityConfig, hash_key, resolve_principal
    from mcprouter.models import AgentPrincipal

    with db() as s:
        s.add_all(
            AgentPrincipal(agent_id=f"agent-{i}", key_hash=hash_key(f"key-{i}")) for i in range(50)
        )
        s.commit()
    config = SecurityConfig(agent_keys_configured=True, admin_token_hash=None, max_exposed_tools=8)
    with db() as s, count_statements(db.kw["bind"]) as seen:
        p = resolve_principal(s, config, "Bearer key-7")
    assert p.agent_id == "agent-7"
    assert len(seen) == 1, seen
    with db() as s:
        s.execute(text("SET LOCAL enable_seqscan = off"))
        plan = _plan(s, "SELECT id FROM agent_principals WHERE key_hash = :h", {"h": hash_key("x")})
        s.rollback()
    assert "ix_agent_principals_key_hash" in plan, plan


def test_a_key_shared_by_two_principals_names_no_one(db: sessionmaker[Session]) -> None:
    from mcprouter.auth import AuthenticationError, SecurityConfig, hash_key, resolve_principal
    from mcprouter.models import AgentPrincipal

    with db() as s:
        s.add_all(
            [
                AgentPrincipal(agent_id="twin-a", key_hash=hash_key("shared")),
                AgentPrincipal(agent_id="twin-b", key_hash=hash_key("shared")),
            ]
        )
        s.commit()
    config = SecurityConfig(agent_keys_configured=True, admin_token_hash=None, max_exposed_tools=8)
    with db() as s, pytest.raises(AuthenticationError):
        resolve_principal(s, config, "Bearer shared")


def test_admin_token_comes_from_settings() -> None:
    from mcprouter.auth import SecurityConfig, hash_key
    from mcprouter.settings import Settings

    tok, other = "a" * 40, "b" * 40
    assert SecurityConfig.build(Settings(admin_token=tok), {}).admin_token_hash == hash_key(tok)
    built = SecurityConfig.build(Settings(admin_token=tok), {"MCPR_ADMIN_TOKEN": other})
    assert built.admin_token_hash == hash_key(tok)  # settings wins
    fallback = SecurityConfig.build(Settings(), {"MCPR_ADMIN_TOKEN": other})
    assert fallback.admin_token_hash == hash_key(other)  # env mapping as fallback
    assert SecurityConfig.build(Settings(), {}).admin_token_hash is None
    # A blank settings token must not mask a real env token (would open dev mode).
    blank = SecurityConfig.build(Settings(admin_token="  "), {"MCPR_ADMIN_TOKEN": other})
    assert blank.admin_token_hash == hash_key(other) and not blank.dev_mode_possible
