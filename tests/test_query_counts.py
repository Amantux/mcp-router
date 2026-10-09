"""E6 performance guards: bounded dedup, statement counts and index use
(P-603, P-604, P-605, P-607). Counts come from a cursor listener, plans from
EXPLAIN — never from timing."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

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
