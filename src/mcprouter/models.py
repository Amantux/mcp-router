"""ORM models — the shared data contract for every subsystem.

Conventions:
- IDs are string UUIDs minted by the app (stable internal identifiers, FR-01).
- Timestamps are timezone-aware UTC.
- Embeddings live in pgvector columns; dimension fixed at 384 (BGE-small).
  The hash fallback backend projects into the same 384-dim space so the two
  are storage-compatible (never semantically comparable across backends —
  re-embed on backend change; `embedding_backend` column records provenance).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 384
# Skills share id space with tools in routing decisions, feedback, analytics
# and dedup suggestions as "skill:<skill id>" (RoutingDecisionRecord.
# selected_tool_ids and friends). The ONE spelling of that prefix.
SKILL_ID_PREFIX = "skill:"


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class MCPServerRecord(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    transport: Mapped[str] = mapped_column(String(20))  # stdio | streamable-http | sse
    # stdio: command + args (JSON); http/sse: endpoint URL.
    endpoint: Mapped[str | None] = mapped_column(Text)
    stdio_command: Mapped[list[str] | None] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(
        String(16), default="unknown"
    )  # healthy|degraded|offline|unknown
    server_version: Mapped[str | None] = mapped_column(String(64))
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tools: Mapped[list[MCPToolRecord]] = relationship(
        back_populates="server", cascade="all, delete-orphan"
    )


class MCPToolRecord(Base):
    __tablename__ = "mcp_tools"
    __table_args__ = (
        Index("ix_tools_server_name", "server_id", "name", unique=True),
        Index("ix_tools_domain", "domain"),
        Index("ix_tools_operation", "operation"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    # MCP Tool.title / .annotations as last listed (discovery writes them;
    # annotations.readOnlyHint/destructiveHint are classifier signals).
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    annotations: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    schema_hash: Mapped[str] = mapped_column(String(64))  # sha256 of canonical schema
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Classification (AI-generated, human-reviewable: `classification_reviewed`)
    domain: Mapped[str | None] = mapped_column(String(40))
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    operation: Mapped[str] = mapped_column(
        String(10), default="unknown"
    )  # read|write|execute|unknown
    required_scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    classification_reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Who wrote the current classification: a classifier name ("rules-v1",
    # "laya@...") or "human" (review/override). None = never classified.
    classification_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    # Stats
    call_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_latency_ms: Mapped[float | None] = mapped_column(Float)
    # Embedding
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    embedding_backend: Mapped[str | None] = mapped_column(String(20))
    embedding_text_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    server: Mapped[MCPServerRecord] = relationship(back_populates="tools")


class ToolVersionRecord(Base):
    """Append-only history: one row per observed schema/metadata change (FR-02)."""

    __tablename__ = "tool_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tool_id: Mapped[str] = mapped_column(ForeignKey("mcp_tools.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    schema_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)  # full tool def at this version
    change_kind: Mapped[str] = mapped_column(String(16))  # added|schema|metadata|removed|restored
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentPrincipal(Base):
    """A registered agent identity. Authorization is deny-by-default."""

    __tablename__ = "agent_principals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(120), unique=True)
    key_hash: Mapped[str] = mapped_column(String(128))  # sha256 of API key
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_tools: Mapped[int] = mapped_column(Integer, default=8)
    # Cap on DISTINCT servers in this agent's exposure; None = unlimited.
    max_servers: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Wave 4: cap on routed skills per request (skill bodies cost far more context than tool schemas).
    max_skills: Mapped[int] = mapped_column(Integer, default=3)


class PolicyRule(Base):
    """Per-agent allow rules. No matching allow rule => denied.

    Routing scores must never override these (FR-07): the policy filter runs
    AFTER ranking and is pure deterministic code.
    """

    __tablename__ = "policy_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(120), index=True)
    server_id: Mapped[str | None] = mapped_column(String(36))  # None = any server
    tool_name: Mapped[str | None] = mapped_column(String(200))  # None = any tool; glob allowed
    max_operation: Mapped[str] = mapped_column(String(10), default="read")  # read<write<execute
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Wave 4: which catalog this rule governs. Existing rows are tool rules; skill
    # rules are explicit (deny-by-default => no skill rule, no skills). For skill
    # rules, server_id holds the skill SOURCE id and tool_name the skill-name glob.
    resource_kind: Mapped[str] = mapped_column(
        String(8), default="tool", server_default="tool"
    )  # tool | skill (server default: migration 0001)


class RoutingDecisionRecord(Base):
    __tablename__ = "routing_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(120), index=True)
    query: Mapped[str] = mapped_column(Text)
    selected_tool_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    scores: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    model_version: Mapped[str] = mapped_column(String(80), default="")
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class ExecutionRecord(Base):
    """Audit trail for every tool execution attempt — allowed or refused."""

    __tablename__ = "execution_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(120), index=True)
    tool_id: Mapped[str | None] = mapped_column(String(36), index=True)
    server_id: Mapped[str | None] = mapped_column(String(36))
    outcome: Mapped[str] = mapped_column(String(16))  # ok|error|denied|timeout|rate_limited
    # Curated detail only — never raw upstream error bodies, never arguments
    # containing secrets (redaction happens before this row is written).
    detail: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    # Wave-2 analytics: the RoutingDecisionRecord.id (route request_id) this
    # call followed, when the caller knows it. NULL = unattributed (legacy rows,
    # approvals, direct calls) — analytics treats NULL as "not in the funnel".
    route_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # Wave-2 integration: who started the attempt when it was not the agent
    # itself ("admin" = admin impersonation via REST). NULL = the agent.
    initiated_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Wave 4: skill ACTIVATIONS (body served to an agent) are audited here too.
    resource_kind: Mapped[str] = mapped_column(
        String(8), default="tool", server_default="tool"
    )  # tool | skill (server default: migration 0001)
    skill_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)


class ToolStatsDaily(Base):
    """Per-tool, per-UTC-day funnel rollup (wave-2 analytics).

    Written ONLY by `analytics.rollup.recompute_day` (DELETE+INSERT per day,
    idempotent). `tool_id` has no FK on purpose: history outlives a deleted
    tool. The day is the DECISION's UTC day (executions are attributed to the
    decision they followed)."""

    __tablename__ = "tool_stats_daily"

    tool_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True, index=True)
    surfaced: Mapped[int] = mapped_column(Integer, default=0)
    selected: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    sum_rank: Mapped[int] = mapped_column(BigInteger, default=0)  # 1-based ranks summed
    exposed_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DuplicateSuggestion(Base):
    """Dedup findings for human review (FR-05). Never auto-applied."""

    __tablename__ = "duplicate_suggestions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tool_a_id: Mapped[str] = mapped_column(String(48), index=True)
    tool_b_id: Mapped[str] = mapped_column(String(48), index=True)
    similarity: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text, default="")
    preferred_tool_id: Mapped[str | None] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|accepted|dismissed
    resolved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ServerCredentialRecord(Base):
    """Server-side credential storage for stdio servers (FR-07).

    A separate table on purpose: secrets stay out of the `mcp_servers` row that
    every serializer touches. Values are never logged or returned by the API
    (only variable NAMES). Stored plaintext at rest — encryption-at-rest is an
    open gap (docs/history/INTEGRATION_NOTES-integration.md).
    """

    __tablename__ = "mcp_server_credentials"

    server_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mcp_servers.id", ondelete="CASCADE"), primary_key=True
    )
    env: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    def __repr__(self) -> str:  # never render values
        return (
            f"ServerCredentialRecord(server_id={self.server_id!r}, env=<{len(self.env)} redacted>)"
        )


# --------------------------------------------------------------------------
# Wave 4: Agent Skills (agentskills.io spec) — the same catalog/routing/policy
# machinery applied to SKILL.md directories. A skill source is to a skill what
# an MCP server is to a tool.
# --------------------------------------------------------------------------


class SkillSourceRecord(Base):
    """Where skills come from: a local directory tree or a git repository."""

    __tablename__ = "skill_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # directory | git
    location: Mapped[str] = mapped_column(Text)  # absolute path or https git URL
    git_ref: Mapped[str | None] = mapped_column(String(200))  # branch/tag; git only
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(
        String(16), default="unknown"
    )  # healthy|degraded|offline|unknown
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_commit: Mapped[str | None] = mapped_column(String(64))  # git only
    sync_interval_s: Mapped[int] = mapped_column(Integer, default=3600)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    skills: Mapped[list[SkillRecord]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class SkillRecord(Base):
    """One SKILL.md directory. Mirrors MCPToolRecord's classification/embedding
    columns so retrieval, dedup, policy and analytics treat both kinds alike.

    `operation` is the skill's RISK CLASS on the same read<write<execute scale:
    guidance-only = read; declares writes/sends = write; ships scripts/ or
    pre-approves executing tools (allowed-tools) = execute; undeterminable =
    unknown (policy treats unknown as execute — fail closed).
    """

    __tablename__ = "skills"
    __table_args__ = (
        Index("ix_skills_source_name", "source_id", "name", unique=True),
        Index("ix_skills_domain", "domain"),
        Index("ix_skills_operation", "operation"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_id: Mapped[str] = mapped_column(ForeignKey("skill_sources.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))  # spec: [a-z0-9-], 1..64, == dir name
    description: Mapped[str] = mapped_column(Text)  # spec: 1..1024
    body: Mapped[str] = mapped_column(Text, default="")  # SKILL.md after the frontmatter
    relative_path: Mapped[str] = mapped_column(Text)  # skill dir relative to the source root
    license: Mapped[str | None] = mapped_column(Text)
    compatibility: Mapped[str | None] = mapped_column(String(500))
    skill_metadata: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, default=list)  # split on whitespace
    # [{"path": "scripts/x.py", "size": 123, "sha256": "...", "kind": "script|reference|asset|other"}]
    resource_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    has_scripts: Mapped[bool] = mapped_column(Boolean, default=False)
    content_hash: Mapped[str] = mapped_column(String(64))  # sha256 of SKILL.md bytes
    manifest_hash: Mapped[str] = mapped_column(String(64))  # sha256 over sorted (path, sha256)
    body_tokens_est: Mapped[int] = mapped_column(Integer, default=0)  # chars/4, for budgets/economy
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Classification (same semantics as MCPToolRecord)
    domain: Mapped[str | None] = mapped_column(String(40))
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    operation: Mapped[str] = mapped_column(String(10), default="unknown")
    required_scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    classification_reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    classification_source: Mapped[str | None] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    # Ingest-time findings (never auto-applied): e.g. secret-shaped strings in the body.
    ingest_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Stats
    activation_count: Mapped[int] = mapped_column(Integer, default=0)
    # Embedding (same 384-dim space + provenance rules as tools)
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    embedding_backend: Mapped[str | None] = mapped_column(String(20))
    embedding_text_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    source: Mapped[SkillSourceRecord] = relationship(back_populates="skills")


class SkillVersionRecord(Base):
    """Append-only history of a skill: one row per observed content/manifest change."""

    __tablename__ = "skill_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    manifest_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)  # frontmatter + body + manifest
    change_kind: Mapped[str] = mapped_column(
        String(16)
    )  # added|content|resources|metadata|removed|restored
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RouteFeedback(Base):
    """Was a surfaced tool/skill helpful for a routing decision? FK-less like
    attribution: route_request_id is validated as owned (agent) at write time.
    One row per (decision, agent, target, source); a re-post upserts."""

    __tablename__ = "route_feedback"
    __table_args__ = (
        UniqueConstraint(
            "route_request_id",
            "agent_id",
            "target_kind",
            "target_id",
            "source",
            name="uq_route_feedback_target",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    route_request_id: Mapped[str] = mapped_column(String(36), index=True)
    agent_id: Mapped[str] = mapped_column(String(120), index=True)
    target_kind: Mapped[str] = mapped_column(String(10))  # tool | skill
    target_id: Mapped[str] = mapped_column(String(400))
    helpful: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(10))  # agent | human
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


# --------------------------------------------------------- side metadatas
class _SettingsBase(DeclarativeBase):
    pass


# Public spelling for modules outside models (routes_setup re-exports it).
SettingsBase = _SettingsBase


class AppSetting(_SettingsBase):
    """First-run wizard key/value flags (non-secret). Moved here from
    api/routes_setup.py (P-608); still on its OWN metadata (single-Base merge
    deferred), re-exported from routes_setup."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(String(2000))


# Imported at the bottom on purpose: neither module imports mcprouter.models,
# so there is no cycle, and every MetaData the app owns is listed in ONE place
# (migrations, init_db and the test cleanup helper all read this).
from mcprouter.eval.store import eval_metadata  # noqa: E402
from mcprouter.execution.models import SecurityBase  # noqa: E402

ALL_METADATA: tuple[MetaData, ...] = (
    Base.metadata,
    SecurityBase.metadata,
    eval_metadata,
    AppSetting.metadata,
)
