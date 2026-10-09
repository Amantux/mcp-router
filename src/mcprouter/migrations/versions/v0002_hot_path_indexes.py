"""ANN + hot-path indexes (D9, P-605, P-607).

* HNSW (`vector_cosine_ops`) on `mcp_tools.embedding` and `skills.embedding`
  (both `vector(384)`, fixed dimension) — ANN for the routing vector leg and
  nearest-neighbour lookups.
* `(agent_id, created_at DESC, id DESC)` on `routing_decisions` — the
  per-request "latest decision for this agent" lookup.
* `agent_principals (key_hash)` — indexed credential lookup (one query per
  auth instead of a full-table scan).

Built `CONCURRENTLY` (outside a transaction, `autocommit_block`) so an upgrade
on a populated database never takes a write-blocking lock. A concurrent build
that failed leaves an INVALID index behind; it is dropped and rebuilt rather
than skipped by IF NOT EXISTS.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None

# (index name, full CREATE statement). Literal SQL; no identifier composition.
INDEXES: tuple[tuple[str, str], ...] = (
    (
        "ix_mcp_tools_embedding_hnsw",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_mcp_tools_embedding_hnsw"
        " ON mcp_tools USING hnsw (embedding vector_cosine_ops)",
    ),
    (
        "ix_skills_embedding_hnsw",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_skills_embedding_hnsw"
        " ON skills USING hnsw (embedding vector_cosine_ops)",
    ),
    (
        "ix_routing_decisions_agent_latest",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_routing_decisions_agent_latest"
        " ON routing_decisions (agent_id, created_at DESC, id DESC)",
    ),
    (
        "ix_agent_principals_key_hash",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_agent_principals_key_hash"
        " ON agent_principals (key_hash)",
    ),
)

_DROP: dict[str, str] = {
    "ix_mcp_tools_embedding_hnsw": "DROP INDEX CONCURRENTLY IF EXISTS ix_mcp_tools_embedding_hnsw",
    "ix_skills_embedding_hnsw": "DROP INDEX CONCURRENTLY IF EXISTS ix_skills_embedding_hnsw",
    "ix_routing_decisions_agent_latest": (
        "DROP INDEX CONCURRENTLY IF EXISTS ix_routing_decisions_agent_latest"
    ),
    "ix_agent_principals_key_hash": "DROP INDEX CONCURRENTLY IF EXISTS ix_agent_principals_key_hash",
}

_INVALID_SQL = text(
    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
    " WHERE c.relname = :name AND NOT i.indisvalid"
)


def upgrade() -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        for name, create in INDEXES:
            if bind.execute(_INVALID_SQL, {"name": name}).first() is not None:
                op.execute(_DROP[name])  # leftover of a failed concurrent build
            op.execute(create)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _create in reversed(INDEXES):
            op.execute(_DROP[name])
