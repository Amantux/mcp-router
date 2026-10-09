"""Pre-0.6 bridge: bring a database built by the old create_all-style
`init_db` (no `alembic_version`) to exactly the 0001 baseline, once.

The caller (`mcprouter.migrate`) then stamps it `0001` and upgrades to head,
so this runs at most once per database. Steps, in the order the old boot
path applied them:

1. `CREATE EXTENSION IF NOT EXISTS vector`;
2. the 0001 tables with IF NOT EXISTS (tables a 0.5 build never created,
   e.g. `route_feedback`, `app_settings`);
3. `_ADDITIVE_COLUMNS`, moved here VERBATIM from `db.py` (columns adopted
   into models after a table first shipped, the resource_kind backfill and
   the VARCHAR(48) widenings);
4. `_RECONCILE` (nullability drift the old bridge left; see below);
5. the 0001 indexes with IF NOT EXISTS (after step 3: some index columns
   that step adds).
"""

from __future__ import annotations

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text

# Columns adopted into models.py at integration (v0.1). `create_all` never
# alters an existing table, so a database created before the adoption gets
# them here — additive and idempotent. Until 0.6 this ran on EVERY boot from
# db.py (docs/history/INTEGRATION_NOTES-integration.md); it now runs once per
# legacy database, before the stamp. Statements are verbatim from db.py.
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
    # Backfill + harden resource_kind (idempotent: re-running is a no-op).
    "ALTER TABLE policy_rules ALTER COLUMN resource_kind SET DEFAULT 'tool'",
    "UPDATE policy_rules SET resource_kind = 'tool' WHERE resource_kind IS NULL",
    "ALTER TABLE policy_rules ALTER COLUMN resource_kind SET NOT NULL",
    "ALTER TABLE execution_records ALTER COLUMN resource_kind SET DEFAULT 'tool'",
    "UPDATE execution_records SET resource_kind = 'tool' WHERE resource_kind IS NULL",
    "ALTER TABLE execution_records ALTER COLUMN resource_kind SET NOT NULL",
    "ALTER TABLE execution_records ADD COLUMN IF NOT EXISTS skill_id VARCHAR(36)",
    "CREATE INDEX IF NOT EXISTS ix_execution_records_skill_id ON execution_records (skill_id)",
    "CREATE INDEX IF NOT EXISTS ix_execution_records_route_request_id"
    " ON execution_records (route_request_id)",
    # Wave-2 integration: structured provenance of admin-impersonated attempts.
    "ALTER TABLE execution_records ADD COLUMN IF NOT EXISTS initiated_by VARCHAR(16)",
    # Wave-4 (owner decision): these columns may hold "skill:<uuid>" (42 chars).
    # Widening VARCHAR is metadata-only on Postgres and idempotent (re-running
    # TYPE VARCHAR(48) on a VARCHAR(48) column is a no-op).
    "ALTER TABLE duplicate_suggestions ALTER COLUMN tool_a_id TYPE VARCHAR(48)",
    "ALTER TABLE duplicate_suggestions ALTER COLUMN tool_b_id TYPE VARCHAR(48)",
    "ALTER TABLE duplicate_suggestions ALTER COLUMN preferred_tool_id TYPE VARCHAR(48)",
    "ALTER TABLE tool_stats_daily ALTER COLUMN tool_id TYPE VARCHAR(48)",
)

# NOT from db.py: reconciles drift the old bridge left behind, so a bridged
# database compares equal to the 0001 baseline. `max_skills` was added
# nullable (DEFAULT 3 backfills every existing row) while the model is NOT
# NULL; found by comparing a real wave-4 database against the metadata.
_RECONCILE: tuple[str, ...] = (
    "UPDATE agent_principals SET max_skills = 3 WHERE max_skills IS NULL",
    "ALTER TABLE agent_principals ALTER COLUMN max_skills SET NOT NULL",
)


def run_bridge(connection: Connection) -> None:
    """Apply the bridge inside the caller's transaction (commit is the caller's)."""
    from mcprouter.migrations.versions import v0001_baseline as baseline

    ctx = MigrationContext.configure(connection)
    with Operations.context(ctx):
        connection.execute(text(baseline.EXTENSION_SQL))
        baseline.create_tables(if_not_exists=True)
        for stmt in _ADDITIVE_COLUMNS + _RECONCILE:
            connection.execute(text(stmt))
        baseline.create_indexes(if_not_exists=True)
