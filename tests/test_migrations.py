"""MT-8 (migrations half): Alembic baseline, legacy bridge, steady-state boot,
concurrency, unknown revision, one-step downgrade (D5, P-601).

Each test gets its OWN scratch database (`mcpr_mig_<pid>_<n>`) on the test
server, created and removed here, so schema surgery never touches the shared
test database.
"""

from __future__ import annotations

import itertools
import os
import threading
from collections.abc import Iterator

import psycopg
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from psycopg import sql
from sqlalchemy import Engine, create_engine, event, make_url, text

from mcprouter import migrate
from mcprouter.db import init_db
from mcprouter.execution.models import SecurityBase
from mcprouter.models import ALL_METADATA, Base
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db

pytestmark = requires_db

_seq = itertools.count()
_DDL_PREFIXES = ("CREATE", "ALTER", "DROP", "UPDATE", "TRUNCATE", "COMMENT", "ANALYZE", "GRANT")


def _admin_conninfo() -> str:
    url = make_url(TEST_DB_URL).set(drivername="postgresql", database="postgres")
    return url.render_as_string(hide_password=False)


@pytest.fixture()
def scratch_url() -> Iterator[str]:
    name = f"mcpr_mig_{os.getpid()}_{next(_seq)}"
    with psycopg.connect(_admin_conninfo(), autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_url(TEST_DB_URL).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with psycopg.connect(_admin_conninfo(), autocommit=True) as c:
            c.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture()
def scratch(scratch_url: str) -> Iterator[Engine]:
    eng = create_engine(scratch_url)
    try:
        yield eng
    finally:
        eng.dispose()


def _diff(eng: Engine) -> list[object]:
    with eng.connect() as c:
        ctx = MigrationContext.configure(
            c,
            opts={
                "compare_type": True,
                "compare_server_default": True,
                "include_object": migrate.include_object,
            },
        )
        # alembic accepts a sequence of MetaData at runtime; its stub says one.
        return list(compare_metadata(ctx, list(ALL_METADATA)))  # type: ignore[arg-type]


def _revision(eng: Engine) -> str | None:
    with eng.connect() as c:
        return migrate.current_revision(c)


def _valid_indexes(eng: Engine) -> set[str]:
    with eng.connect() as c:
        return set(
            c.execute(
                text(
                    "SELECT cl.relname FROM pg_index i JOIN pg_class cl ON cl.oid = i.indexrelid"
                    " WHERE i.indisvalid"
                )
            ).scalars()
        )


def _build_legacy(eng: Engine) -> None:
    """A database as an older create_all-era build left it: no alembic_version,
    no side tables, bridged columns missing, narrow id columns, no raw-SQL
    indexes, plus rows the bridge must backfill."""
    with eng.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(eng)
    SecurityBase.metadata.create_all(eng)
    with eng.begin() as c:
        for stmt in (
            "ALTER TABLE mcp_tools DROP COLUMN title",
            "ALTER TABLE execution_records DROP COLUMN skill_id",
            "ALTER TABLE execution_records DROP COLUMN initiated_by",
            "ALTER TABLE execution_records DROP COLUMN route_request_id",
            "ALTER TABLE agent_principals DROP COLUMN max_skills",
            "ALTER TABLE policy_rules DROP COLUMN resource_kind",
            "ALTER TABLE duplicate_suggestions ALTER COLUMN tool_a_id TYPE VARCHAR(36)",
            "DROP TABLE route_feedback",
        ):
            c.execute(text(stmt))
        c.execute(
            text(
                "INSERT INTO agent_principals (id, agent_id, key_hash, enabled, max_tools,"
                " created_at) VALUES ('p1', 'alice', 'h', true, 5, now())"
            )
        )
        c.execute(
            text(
                "INSERT INTO policy_rules (id, agent_id, max_operation, requires_approval,"
                " created_at) VALUES ('r1', 'alice', 'read', false, now())"
            )
        )


def test_fresh_upgrade_head_matches_metadata(scratch: Engine) -> None:
    init_db(scratch)
    assert _revision(scratch) == migrate.head_revision() == "0002"
    assert _diff(scratch) == []
    assert migrate.MIGRATION_ONLY_INDEXES <= _valid_indexes(scratch)


def test_legacy_database_is_bridged_stamped_and_equal(scratch: Engine) -> None:
    _build_legacy(scratch)
    with scratch.connect() as c:
        assert migrate.schema_state(c) == "legacy"
    init_db(scratch)
    assert _revision(scratch) == "0002"
    assert _diff(scratch) == []
    assert migrate.MIGRATION_ONLY_INDEXES <= _valid_indexes(scratch)
    with scratch.connect() as c:
        assert c.execute(text("SELECT max_skills FROM agent_principals")).scalar() == 3
        assert c.execute(text("SELECT resource_kind FROM policy_rules")).scalar() == "tool"


def _statements_during(fn: object, needle: str) -> list[str]:
    seen: list[str] = []

    def listener(conn, cursor, statement, params, context, executemany):  # noqa: ANN001, ANN202
        if needle in statement:
            seen.append(statement)

    event.listen(Engine, "before_cursor_execute", listener)
    try:
        assert callable(fn)
        fn()
    finally:
        event.remove(Engine, "before_cursor_execute", listener)
    return seen


def _ddl_during(fn: object) -> list[str]:
    seen: list[str] = []

    def listener(conn, cursor, statement, params, context, executemany):  # noqa: ANN001, ANN202
        if statement.lstrip().upper().startswith(_DDL_PREFIXES):
            seen.append(statement)

    event.listen(Engine, "before_cursor_execute", listener)
    try:
        assert callable(fn)
        fn()
    finally:
        event.remove(Engine, "before_cursor_execute", listener)
    return seen


@pytest.mark.parametrize("start", ["fresh", "legacy"])
def test_second_boot_issues_zero_ddl(scratch: Engine, scratch_url: str, start: str) -> None:
    from mcprouter.api.app import create_app

    if start == "legacy":
        _build_legacy(scratch)
    settings = Settings(database_url=scratch_url)
    first = _ddl_during(lambda: create_app(settings, env={}))
    assert first, "the first boot must migrate"
    second = _ddl_during(lambda: create_app(settings, env={}))
    assert second == []
    # ...and takes no migration lock either (the at-head fast path).
    locks = _statements_during(lambda: create_app(settings, env={}), "pg_try_advisory_lock")
    assert locks == []


def test_concurrent_init_db_both_succeed(scratch_url: str) -> None:
    engines = [create_engine(scratch_url) for _ in range(2)]
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def boot(eng: Engine) -> None:
        try:
            barrier.wait()
            init_db(eng)
        except BaseException as exc:  # noqa: BLE001 — collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=boot, args=(e,)) for e in engines]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    try:
        assert errors == []
        assert _revision(engines[0]) == "0002"
        assert _diff(engines[0]) == []
    finally:
        for e in engines:
            e.dispose()


def test_unknown_revision_is_a_curated_fatal(
    scratch: Engine,
    scratch_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_db(scratch)
    with scratch.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = '9999'"))
    with pytest.raises(migrate.SchemaVersionError) as info:
        init_db(scratch)
    msg = str(info.value)
    assert msg.startswith("database schema is newer than this build")
    assert "9999" in msg
    password = make_url(scratch_url).password
    assert password and password not in msg and "postgresql" not in msg
    # The CLI reports it as FATAL with exit 1 (no traceback, no DSN).
    monkeypatch.setenv("MCPR_DATABASE_URL", scratch_url)
    assert migrate.main(["upgrade"]) == 1
    # Last line: an in-process root handler (E1's configure_logging, installed by
    # any earlier create_app) may have echoed alembic's INFO lines first.
    err = capsys.readouterr().err
    assert (
        err.strip().splitlines()[-1].startswith("FATAL: database schema is newer than this build")
    )
    assert password not in err


def test_downgrade_one_step_and_back(scratch: Engine) -> None:
    init_db(scratch)
    hnsw = {"ix_mcp_tools_embedding_hnsw", "ix_skills_embedding_hnsw"}
    assert hnsw <= _valid_indexes(scratch)
    assert migrate.downgrade(scratch, "-1") == "0001"
    assert not (hnsw & _valid_indexes(scratch))
    assert _revision(scratch) == "0001"
    init_db(scratch)
    assert _revision(scratch) == "0002"
    assert hnsw <= _valid_indexes(scratch)
    assert _diff(scratch) == []


@pytest.mark.parametrize("target", ["base", "-2"])
def test_downgrade_below_baseline_is_refused_before_anything_runs(
    scratch: Engine, target: str
) -> None:
    init_db(scratch)
    with pytest.raises(migrate.BaselineDowngradeRefused):
        migrate.downgrade(scratch, target)
    assert _revision(scratch) == "0002"  # nothing ran, not even 0002's downgrade
    assert {"ix_mcp_tools_embedding_hnsw"} <= _valid_indexes(scratch)


def test_invalid_leftover_index_is_rebuilt(scratch: Engine, scratch_url: str) -> None:
    """A failed CREATE INDEX CONCURRENTLY leaves an INVALID index that
    IF NOT EXISTS would skip; 0002 must drop and rebuild it."""
    init_db(scratch)
    migrate.downgrade(scratch, "-1")
    blocker = psycopg.connect(
        make_url(scratch_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    builder = psycopg.connect(
        make_url(scratch_url).set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
    )
    try:
        blocker.execute("LOCK TABLE mcp_tools IN ROW EXCLUSIVE MODE")  # an open writer
        builder.execute("SET lock_timeout = '200ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            builder.execute(
                "CREATE INDEX CONCURRENTLY ix_mcp_tools_embedding_hnsw"
                " ON mcp_tools USING hnsw (embedding vector_cosine_ops)"
            )
    finally:
        blocker.rollback()
        blocker.close()
        builder.close()
    assert "ix_mcp_tools_embedding_hnsw" not in _valid_indexes(scratch)  # INVALID leftover
    init_db(scratch)
    assert _revision(scratch) == "0002"
    assert "ix_mcp_tools_embedding_hnsw" in _valid_indexes(scratch)


def test_side_table_only_database_takes_the_bridge(scratch: Engine) -> None:
    from mcprouter.eval.store import eval_metadata

    eval_metadata.create_all(scratch)
    with scratch.connect() as c:
        assert migrate.schema_state(c) == "legacy"
    init_db(scratch)
    assert _revision(scratch) == "0002"
    assert _diff(scratch) == []


def test_cli_upgrade_current_heads(
    scratch_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MCPR_DATABASE_URL", scratch_url)
    assert migrate.main(["current"]) == 0
    assert capsys.readouterr().out.strip() == "(none)"
    assert migrate.main([]) == 0  # default command = upgrade
    assert capsys.readouterr().out.strip() == "0002"
    assert migrate.main(["heads"]) == 0
    assert capsys.readouterr().out.strip() == "0002"
    assert migrate.main(["downgrade", "-1"]) == 0
    assert capsys.readouterr().out.strip() == "0001"
    assert migrate.main(["downgrade", "base"]) == 1
    last = capsys.readouterr().err.strip().splitlines()[-1]
    assert last.startswith("FATAL: refusing to downgrade below the baseline")


def test_every_metadata_table_is_in_the_baseline() -> None:
    """ALL_METADATA is the one list: a table on a fifth MetaData would be
    invisible to migrations, init_db and test cleanup alike."""
    from mcprouter.migrations.versions import v0001_baseline  # noqa: F401 - importable

    tables = {t for md in ALL_METADATA for t in md.tables}
    assert {"app_settings", "approval_requests", "eval_results", "route_feedback"} <= tables
    assert len(ALL_METADATA) == 4


def test_cli_driver_error_prints_fatal_without_dsn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreachable server is a driver error, not one of the curated
    migration errors: the CLI must still end on a FATAL line (the entrypoint
    relies on it) and never echo the DSN or its password."""
    monkeypatch.setenv("MCPR_DATABASE_URL", "postgresql+psycopg://mcprouter:s3cretpw@127.0.0.1:1/x")
    assert migrate.main(["upgrade"]) == 1
    err = capsys.readouterr().err
    last = err.strip().splitlines()[-1]
    assert last.startswith("FATAL: database error during migration (")
    assert "s3cretpw" not in err and "127.0.0.1:1" not in err
