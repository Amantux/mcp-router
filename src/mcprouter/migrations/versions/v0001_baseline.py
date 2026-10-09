"""Baseline: the schema as of 0.6 (D5), generated from ALL_METADATA, frozen.

Generated once by alembic autogenerate against an empty database, then frozen:
later model changes get NEW revisions, never edits here. `create_tables` and
`create_indexes` take `if_not_exists` so the legacy bridge
(`migrations/legacy_bridge.py`) can bring a pre-0.6 database to exactly this
shape before it is stamped 0001.

Revision ID: 0001
Revises: (none)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None

EXTENSION_SQL = "CREATE EXTENSION IF NOT EXISTS vector"

# Expression / unique indexes that were created outside create_all before 0.6
# (registry/schema.py, routing/retriever.py). Literal copies frozen here; the
# query templates in those modules must stay byte-identical (tests assert the
# planner uses each index).
EXPRESSION_INDEX_SQL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_tools_fts ON mcp_tools USING GIN ((setweight(to_tsvector('english'::regconfig, translate(regexp_replace(name, '([a-z0-9])([A-Z])', '\\1 \\2', 'g'), '_.-/', '    ')), 'A') || setweight(to_tsvector('english'::regconfig, coalesce(description, '')), 'B') || setweight(to_tsvector('english'::regconfig, translate(tags::text, '[]\",_-', '       ')), 'C')))",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_dup_pair ON duplicate_suggestions (tool_a_id, tool_b_id)",
    "CREATE INDEX IF NOT EXISTS ix_tools_routing_fts ON mcp_tools USING GIN ((setweight(to_tsvector('english', replace(name, '_', ' ')), 'A') || setweight(to_tsvector('english', coalesce(tags::text, '')), 'B') || setweight(to_tsvector('english', coalesce(description, '')), 'C')))",
    "CREATE INDEX IF NOT EXISTS ix_skills_routing_fts ON skills USING GIN ((setweight(to_tsvector('english', replace(name, '-', ' ')), 'A') || setweight(to_tsvector('english', coalesce(tags::text, '')), 'B') || setweight(to_tsvector('english', coalesce(description, '')), 'C') || setweight(to_tsvector('english', left(coalesce(body, ''), 2048)), 'D')))",
)


def create_tables(*, if_not_exists: bool) -> None:
    op.create_table(
        "agent_principals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=120), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("max_tools", sa.Integer(), nullable=False),
        sa.Column("max_servers", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_skills", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "duplicate_suggestions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tool_a_id", sa.String(length=48), nullable=False),
        sa.Column("tool_b_id", sa.String(length=48), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("preferred_tool_id", sa.String(length=48), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resolved_by", sa.String(length=120), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "execution_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=120), nullable=False),
        sa.Column("tool_id", sa.String(length=36), nullable=True),
        sa.Column("server_id", sa.String(length=36), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("route_request_id", sa.String(length=36), nullable=True),
        sa.Column("initiated_by", sa.String(length=16), nullable=True),
        sa.Column(
            "resource_kind", sa.String(length=8), server_default=sa.text("'tool'"), nullable=False
        ),
        sa.Column("skill_id", sa.String(length=36), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("transport", sa.String(length=20), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("stdio_command", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("server_version", sa.String(length=64), nullable=True),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "policy_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=120), nullable=False),
        sa.Column("server_id", sa.String(length=36), nullable=True),
        sa.Column("tool_name", sa.String(length=200), nullable=True),
        sa.Column("max_operation", sa.String(length=10), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "resource_kind", sa.String(length=8), server_default=sa.text("'tool'"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "route_feedback",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("route_request_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=120), nullable=False),
        sa.Column("target_kind", sa.String(length=10), nullable=False),
        sa.Column("target_id", sa.String(length=400), nullable=False),
        sa.Column("helpful", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "route_request_id",
            "agent_id",
            "target_kind",
            "target_id",
            "source",
            name="uq_route_feedback_target",
        ),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "routing_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=120), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("selected_tool_ids", sa.JSON(), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("model_version", sa.String(length=80), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "skill_sources",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column("git_ref", sa.String(length=200), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_commit", sa.String(length=64), nullable=True),
        sa.Column("sync_interval_s", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "tool_stats_daily",
        sa.Column("tool_id", sa.String(length=48), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("surfaced", sa.Integer(), nullable=False),
        sa.Column("selected", sa.Integer(), nullable=False),
        sa.Column("succeeded", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("sum_rank", sa.BigInteger(), nullable=False),
        sa.Column("exposed_tokens", sa.BigInteger(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tool_id", "day"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "mcp_server_credentials",
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("env", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("server_id"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "mcp_tools",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("annotations", sa.JSON(), nullable=True),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("domain", sa.String(length=40), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("operation", sa.String(length=10), nullable=False),
        sa.Column("required_scopes", sa.JSON(), nullable=False),
        sa.Column("classification_reviewed", sa.Boolean(), nullable=False),
        sa.Column("classification_source", sa.String(length=80), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("call_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("avg_latency_ms", sa.Float(), nullable=True),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.Column("embedding_backend", sa.String(length=20), nullable=True),
        sa.Column("embedding_text_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "skills",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("license", sa.Text(), nullable=True),
        sa.Column("compatibility", sa.String(length=500), nullable=True),
        sa.Column("skill_metadata", sa.JSON(), nullable=False),
        sa.Column("allowed_tools", sa.JSON(), nullable=False),
        sa.Column("resource_manifest", sa.JSON(), nullable=False),
        sa.Column("has_scripts", sa.Boolean(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("body_tokens_est", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("domain", sa.String(length=40), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("operation", sa.String(length=10), nullable=False),
        sa.Column("required_scopes", sa.JSON(), nullable=False),
        sa.Column("classification_reviewed", sa.Boolean(), nullable=False),
        sa.Column("classification_source", sa.String(length=80), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("ingest_flags", sa.JSON(), nullable=False),
        sa.Column("activation_count", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.Column("embedding_backend", sa.String(length=20), nullable=True),
        sa.Column("embedding_text_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["skill_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("skill_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("change_kind", sa.String(length=16), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "tool_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tool_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("change_kind", sa.String(length=16), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tool_id"], ["mcp_tools.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=120), nullable=False),
        sa.Column("tool_id", sa.String(length=36), nullable=False),
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_record_id", sa.String(length=36), nullable=True),
        sa.Column("result_preview", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "eval_results",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dataset", sa.String(length=120), nullable=False),
        sa.Column("model_version", sa.String(length=80), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("cases", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=if_not_exists,
    )
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", sa.String(length=2000), nullable=False),
        sa.PrimaryKeyConstraint("key"),
        if_not_exists=if_not_exists,
    )


def create_indexes(*, if_not_exists: bool) -> None:
    op.create_index(
        op.f("ix_duplicate_suggestions_tool_a_id"),
        "duplicate_suggestions",
        ["tool_a_id"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_duplicate_suggestions_tool_b_id"),
        "duplicate_suggestions",
        ["tool_b_id"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_execution_records_agent_id"),
        "execution_records",
        ["agent_id"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_execution_records_created_at"),
        "execution_records",
        ["created_at"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_execution_records_route_request_id"),
        "execution_records",
        ["route_request_id"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_execution_records_skill_id"),
        "execution_records",
        ["skill_id"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_execution_records_tool_id"),
        "execution_records",
        ["tool_id"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_policy_rules_agent_id"),
        "policy_rules",
        ["agent_id"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_route_feedback_agent_id"),
        "route_feedback",
        ["agent_id"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_route_feedback_created_at"),
        "route_feedback",
        ["created_at"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_route_feedback_route_request_id"),
        "route_feedback",
        ["route_request_id"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_routing_decisions_agent_id"),
        "routing_decisions",
        ["agent_id"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_routing_decisions_created_at"),
        "routing_decisions",
        ["created_at"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_tool_stats_daily_day"),
        "tool_stats_daily",
        ["day"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        "ix_tools_domain", "mcp_tools", ["domain"], unique=False, if_not_exists=if_not_exists
    )
    op.create_index(
        "ix_tools_operation", "mcp_tools", ["operation"], unique=False, if_not_exists=if_not_exists
    )
    op.create_index(
        "ix_tools_server_name",
        "mcp_tools",
        ["server_id", "name"],
        unique=True,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        "ix_skills_domain", "skills", ["domain"], unique=False, if_not_exists=if_not_exists
    )
    op.create_index(
        "ix_skills_operation", "skills", ["operation"], unique=False, if_not_exists=if_not_exists
    )
    op.create_index(
        "ix_skills_source_name",
        "skills",
        ["source_id", "name"],
        unique=True,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_skill_versions_skill_id"),
        "skill_versions",
        ["skill_id"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_tool_versions_tool_id"),
        "tool_versions",
        ["tool_id"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        "ix_approval_agent_status",
        "approval_requests",
        ["agent_id", "status"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_eval_results_created_at"),
        "eval_results",
        ["created_at"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    op.create_index(
        op.f("ix_eval_results_dataset"),
        "eval_results",
        ["dataset"],
        unique=False,
        if_not_exists=if_not_exists,
    )
    for stmt in EXPRESSION_INDEX_SQL:
        op.execute(stmt)


def upgrade() -> None:
    op.execute(EXTENSION_SQL)
    create_tables(if_not_exists=False)
    create_indexes(if_not_exists=False)


def downgrade() -> None:
    # Dropping the baseline drops every table: never automatic. Restore a backup.
    raise RuntimeError("0001 is the baseline and cannot be downgraded; restore a backup instead")
