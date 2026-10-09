"""resource_kind is backfilled, defaulted and NOT NULL; init is idempotent (B3)."""

from __future__ import annotations

from sqlalchemy import text

from mcprouter.db import init_db, make_engine
from mcprouter.settings import Settings

from .conftest import requires_db

pytestmark = requires_db


def test_resource_kind_not_null_with_default(settings: Settings) -> None:
    eng = make_engine(settings)
    try:
        init_db(eng)
        init_db(eng)  # second run must be a no-op, not an error
        with eng.connect() as c:
            rows = c.execute(
                text(
                    "SELECT table_name, is_nullable, column_default FROM information_schema.columns"
                    " WHERE column_name = 'resource_kind'"
                    " AND table_name IN ('policy_rules', 'execution_records')"
                )
            ).all()
    finally:
        eng.dispose()
    assert {r[0] for r in rows} == {"policy_rules", "execution_records"}
    for _t, nullable, default in rows:
        assert nullable == "NO"
        assert "tool" in (default or "")
