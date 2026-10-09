"""Alembic environment.

Normal path: `mcprouter.migrate` passes an open Connection in
`config.attributes["connection"]` (it already holds the migration advisory
lock). Developer CLI path (`alembic -c alembic.ini ...`): no connection is
passed, so one is built from `Settings.from_env()` (MCPR_DATABASE_URL).
Online only; offline SQL generation is not supported.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

from mcprouter.migrate import include_object
from mcprouter.models import ALL_METADATA

config = context.config


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=list(ALL_METADATA),
        include_object=include_object,
        # 0002+ use CREATE INDEX CONCURRENTLY inside autocommit blocks.
        transaction_per_migration=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    from mcprouter.db import make_engine
    from mcprouter.settings import Settings

    engine = make_engine(Settings.from_env())
    try:
        with engine.connect() as conn:
            _run(conn)
            conn.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline (--sql) migrations are not supported; run against a database")
run_migrations_online()
