"""Sync SQLAlchemy 2.0 + psycopg3 + pgvector.

Sync, not async, on purpose: boring reliability; FastAPI runs sync endpoints
in its threadpool, and the routing hot path's latency budget is dominated by
inference, not the driver. Revisit only with evidence.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.models import Base
from mcprouter.settings import Settings


def make_engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url, pool_pre_ping=True)


def init_db(engine: Engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    s = factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
