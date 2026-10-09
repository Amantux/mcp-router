"""MT-7 suite health: the test DB is isolated and wiped (P-501/P-502)."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, func, select, table, text
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.routes_setup import AppSetting
from mcprouter.models import MCPServerRecord, RouteFeedback
from tests.support import db as testdb

# ------------------------------------------------------------------ database


def test_guard_refuses_a_real_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCPR_ALLOW_ANY_DB", raising=False)
    for name in ("mcprouter", "prod", "contest", "mcpr_tests"):
        with pytest.raises(RuntimeError, match="refusing to wipe"):
            testdb.assert_disposable(f"postgresql+psycopg://u:p@h/{name}")
    for name in ("mcprouter_test_w3", "mcprouter_test_template", "x_test", "test"):
        testdb.assert_disposable(f"postgresql+psycopg://u:p@h/{name}")
    monkeypatch.setenv("MCPR_ALLOW_ANY_DB", "1")
    testdb.assert_disposable("postgresql+psycopg://u:p@h/mcprouter")


def test_tests_never_use_the_configured_database() -> None:
    base = testdb.BASE_URL.database
    assert testdb.WORKER_DB != base and testdb.TEMPLATE_DB != base
    assert testdb.WORKER_DB.startswith(f"{base}_test_w")
    testdb.assert_disposable(testdb.TEST_DB_URL)


def _tables_in_db(s: Session) -> set[str]:
    names = set(
        s.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        ).scalars()
    )
    names.discard("alembic_version")  # migration bookkeeping (E6), not app data
    return names


def test_wipe_list_covers_every_table_in_the_database(db: sessionmaker[Session]) -> None:
    with db() as s:
        assert _tables_in_db(s) == set(testdb.all_table_names())


def test_every_table_is_empty_after_wipe(db: sessionmaker[Session]) -> None:
    with db() as s:
        s.add(MCPServerRecord(name="health", transport="stdio", stdio_command=["true"]))
        s.add(AppSetting(key="health", value="1"))
        s.add(
            RouteFeedback(
                route_request_id="r",
                agent_id="a",
                target_kind="tool",
                target_id="t",
                helpful=True,
                source="agent",
            )
        )
        s.commit()
    engine = db.kw["bind"]
    assert isinstance(engine, Engine)
    testdb.wipe_all(engine)
    with db() as s:
        counts = {
            name: s.execute(select(func.count()).select_from(table(name))).scalar_one()
            for name in _tables_in_db(s)
        }
    assert set(counts.values()) == {0}, {k: v for k, v in counts.items() if v}
