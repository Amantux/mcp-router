"""Sync SQLAlchemy 2.0 + psycopg3 + pgvector.

Sync, not async, on purpose: boring reliability; FastAPI runs sync endpoints
in its threadpool, and the routing hot path's latency budget is dominated by
inference, not the driver. Revisit only with evidence.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.settings import Settings


def make_engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url, pool_pre_ping=True)


def init_db(engine: Engine) -> None:
    """The ONE schema-init path: Alembic `upgrade head` under an advisory lock,
    with the one-time legacy bridge for pre-0.6 databases (mcprouter.migrate).

    Zero DDL when the database is already at head. Raises
    `migrate.SchemaVersionError` (curated) when the database is NEWER than this
    build."""
    from mcprouter.migrate import upgrade_to_head

    upgrade_to_head(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
