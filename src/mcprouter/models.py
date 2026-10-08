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
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBEDDING_DIM = 384


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


class DuplicateSuggestion(Base):
    """Dedup findings for human review (FR-05). Never auto-applied."""

    __tablename__ = "duplicate_suggestions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tool_a_id: Mapped[str] = mapped_column(String(36), index=True)
    tool_b_id: Mapped[str] = mapped_column(String(36), index=True)
    similarity: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text, default="")
    preferred_tool_id: Mapped[str | None] = mapped_column(String(36))
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
    open gap (docs/INTEGRATION_NOTES-integration.md).
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
