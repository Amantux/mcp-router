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


# Columns adopted into models.py at integration (v0.1). `create_all` never
# alters an existing table, so a database created before the adoption gets
# them here — additive and idempotent. This is a bridge, NOT a migration
# system: the Alembic baseline is deferred (docs/INTEGRATION_NOTES-integration.md).
# Literal statements (no identifier composition at all).
_ADDITIVE_COLUMNS: tuple[str, ...] = (
    "ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS title TEXT",
    "ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS annotations JSON",
    "ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS classification_source VARCHAR(80)",
    "ALTER TABLE duplicate_suggestions ADD COLUMN IF NOT EXISTS resolved_by VARCHAR(120)",
    "ALTER TABLE duplicate_suggestions ADD COLUMN IF NOT EXISTS resolved_at"
    " TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE duplicate_suggestions ADD COLUMN IF NOT EXISTS resolution_note TEXT",
    # wave 2 (budgets): per-principal distinct-server cap, NULL = unlimited.
    "ALTER TABLE agent_principals ADD COLUMN IF NOT EXISTS max_servers INTEGER",
    # Wave-2 analytics: execution -> routing-decision attribution.
    "ALTER TABLE execution_records ADD COLUMN IF NOT EXISTS route_request_id VARCHAR(36)",
    # Wave 4 (skills): kind-awareness on shared tables.
    "ALTER TABLE agent_principals ADD COLUMN IF NOT EXISTS max_skills INTEGER DEFAULT 3",
    "ALTER TABLE policy_rules ADD COLUMN IF NOT EXISTS resource_kind VARCHAR(8) DEFAULT 'tool'",
    "ALTER TABLE execution_records ADD COLUMN IF NOT EXISTS resource_kind VARCHAR(8) DEFAULT 'tool'",
    "ALTER TABLE execution_records ADD COLUMN IF NOT EXISTS skill_id VARCHAR(36)",
    "CREATE INDEX IF NOT EXISTS ix_execution_records_skill_id ON execution_records (skill_id)",
    "CREATE INDEX IF NOT EXISTS ix_execution_records_route_request_id"
    " ON execution_records (route_request_id)",
    # Wave-2 integration: structured provenance of admin-impersonated attempts.
    "ALTER TABLE execution_records ADD COLUMN IF NOT EXISTS initiated_by VARCHAR(16)",
)


def init_db(engine: Engine) -> None:
    """The ONE schema-init path (create_all-style; Alembic deferred).

    Creates every table registered on `Base` plus the side metadatas owned by
    the gateway (`approval_requests`) and eval (`eval_results`) tracks."""
    from mcprouter.eval.store import eval_metadata
    from mcprouter.execution.models import SecurityBase

    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    eval_metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        for stmt in _ADDITIVE_COLUMNS:
            conn.execute(text(stmt))


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
