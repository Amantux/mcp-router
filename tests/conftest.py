"""Shared fixtures. Integration tests need the compose db:

    docker compose up -d db

Unit tests that don't touch the DB must not require it.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from mcprouter.db import init_db, make_engine, make_session_factory
from mcprouter.settings import Settings

TEST_DB_URL = os.environ.get(
    "MCPR_DATABASE_URL",
    "postgresql+psycopg://mcprouter:mcprouter@localhost:5434/mcprouter",
)


def _db_available() -> bool:
    try:
        eng = make_engine(Settings(database_url=TEST_DB_URL))
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — availability probe; any failure means "skip"
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="compose db not running")

_CLEAN_TABLES = [
    "approval_requests",
    "eval_results",
    "execution_records",
    "routing_decisions",
    "duplicate_suggestions",
    "tool_versions",
    "policy_rules",
    "agent_principals",
    "mcp_tools",
    "mcp_server_credentials",
    "mcp_servers",
]


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(database_url=TEST_DB_URL)


@pytest.fixture()
def db(settings: Settings):  # noqa: ANN201 - sessionmaker
    engine = make_engine(settings)
    init_db(engine)
    factory = make_session_factory(engine)
    yield factory
    # Test-db row cleanup between tests (DELETE, not schema changes).
    with engine.connect() as conn:
        for t in _CLEAN_TABLES:
            conn.execute(text(f'DELETE FROM "{t}"'))
        conn.commit()
