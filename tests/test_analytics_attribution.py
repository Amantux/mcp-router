"""Wave-2 attribution seam: ExecutionManager.execute(route_request_id=...)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.execution.history import list_executions
from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.models import ExecutionRecord

from .conftest import requires_db
from .test_execution_support import (
    Catalog,
    FakeInvoker,
    add_rule,
    sec_db_fixture,  # noqa: F401 — registers the fixture
    seed,
)

pytestmark = requires_db

ARGS: dict[str, Any] = {"repo": "acme/widgets"}


@pytest.fixture()
def cat(sec_db: sessionmaker[Session]) -> Catalog:
    return seed(sec_db, [("github", "list_issues", "read"), ("shell", "run", "execute")])


def _mgr(factory: sessionmaker[Session], limit: int = 100) -> ExecutionManager:
    return ExecutionManager(
        factory, FakeInvoker(), timeout_s=2.0, limiter=SlidingWindowLimiter(limit)
    )


def _rrids(factory: sessionmaker[Session]) -> list[tuple[str, str | None]]:
    with factory() as s:
        return [(r.outcome, r.route_request_id) for r in s.scalars(select(ExecutionRecord))]


async def test_attribution_recorded_on_ok_call(sec_db: sessionmaker[Session], cat: Catalog) -> None:
    add_rule(sec_db, "alice")
    rid = str(uuid.uuid4())
    res = await _mgr(sec_db).execute(
        cat.principals["alice"], cat.tools["github.list_issues"], ARGS, route_request_id=rid
    )
    assert res.status == "ok"
    assert _rrids(sec_db) == [("ok", rid)]
    with sec_db() as s:
        assert list_executions(s).items[0].route_request_id == rid


async def test_attribution_recorded_on_refusals_and_approvals(
    sec_db: sessionmaker[Session], cat: Catalog
) -> None:
    add_rule(sec_db, "alice", tool_name="list_issues", requires_approval=True)
    mgr = _mgr(sec_db)
    rid = str(uuid.uuid4())
    p = cat.principals["alice"]
    await mgr.execute(p, cat.tools["shell.run"], ARGS, route_request_id=rid)  # denied
    await mgr.execute(p, "no-such-tool", ARGS, route_request_id=rid)  # unknown -> denied
    await mgr.execute(p, cat.tools["github.list_issues"], {"x": 1}, route_request_id=rid)
    await mgr.execute(p, cat.tools["github.list_issues"], ARGS, route_request_id=rid)
    got = sorted(_rrids(sec_db))
    assert got == sorted(
        [("denied", rid), ("denied", rid), ("invalid_args", rid), ("pending_approval", rid)]
    )


async def test_absent_attribution_is_null_legacy(
    sec_db: sessionmaker[Session], cat: Catalog
) -> None:
    add_rule(sec_db, "alice")
    await _mgr(sec_db).execute(cat.principals["alice"], cat.tools["github.list_issues"], ARGS)
    assert _rrids(sec_db) == [("ok", None)]


@pytest.mark.parametrize("bad", ["x" * 37, "a b", "id\nforged", "", 123])
async def test_malformed_attribution_is_dropped_not_fatal(
    sec_db: sessionmaker[Session], cat: Catalog, bad: Any
) -> None:
    add_rule(sec_db, "alice")
    res = await _mgr(sec_db).execute(
        cat.principals["alice"], cat.tools["github.list_issues"], ARGS, route_request_id=bad
    )
    assert res.status == "ok"
    assert _rrids(sec_db) == [("ok", None)]


def test_attribution_column_is_added_idempotently(sec_db: sessionmaker[Session]) -> None:
    """init_db on an existing table adds the column + index (and re-runs clean)."""
    from mcprouter.db import init_db

    engine = sec_db.kw["bind"]
    init_db(engine)
    init_db(engine)
    with engine.connect() as conn:
        cols = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'execution_records'"
            )
        ).scalars()
        assert "route_request_id" in set(cols)
        idx = conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'execution_records'")
        ).scalars()
        assert "ix_execution_records_route_request_id" in set(idx)
