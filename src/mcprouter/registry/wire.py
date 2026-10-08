"""camelCase wire shapes (SPEC §8) for the catalog and dedup APIs.

Mapping decision: SPEC `MCPTool.categories[]` <-> `MCPToolRecord.capabilities`
(JSON list). `domain` (FR-04 hierarchy level) is a separate scalar on the wire.
`capabilities` never appears on the wire; PATCH accepts `categories` only.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from mcprouter.models import DuplicateSuggestion, MCPToolRecord, ToolVersionRecord

Operation = Literal["read", "write", "execute", "unknown"]
_DOMAIN_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_MAX_LIST = 50
_MAX_ITEM = 100


class Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ToolStatsOut(Wire):
    call_count: int
    error_count: int
    avg_latency_ms: float | None
    success_rate: float | None  # None until the tool has been called


class ToolOut(Wire):
    id: str
    server_id: str
    server_name: str
    name: str
    description: str
    input_schema: dict[str, Any]
    schema_hash: str
    version: int
    domain: str | None
    categories: list[str]  # <- MCPToolRecord.capabilities
    tags: list[str]
    operation: str
    required_scopes: list[str]
    classification_reviewed: bool
    enabled: bool
    available: bool
    stats: ToolStatsOut
    embedding_backend: str | None
    created_at: datetime
    updated_at: datetime
    rank: float | None = None  # keyword-search relevance; only set when `q` was given


class ToolVersionOut(Wire):
    version: int
    schema_hash: str
    change_kind: str
    recorded_at: datetime
    snapshot: dict[str, Any]


class ToolDetailOut(ToolOut):
    versions: list[ToolVersionOut]


class ToolPageOut(Wire):
    items: list[ToolOut]
    total: int
    limit: int
    offset: int


def _clean_list(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        v = v.strip()
        if not v:
            raise ValueError("list items must be non-empty strings")
        if len(v) > _MAX_ITEM:
            raise ValueError(f"list items must be at most {_MAX_ITEM} characters")
        if v not in out:
            out.append(v)
    return out


class ClassificationPatchIn(Wire):
    """Human classification override. Omitted fields are left unchanged; an
    empty body approves the current (auto) classification as-is. Either way the
    record becomes `classificationReviewed=true`."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    domain: str | None = None
    categories: list[str] | None = Field(default=None, max_length=_MAX_LIST)
    tags: list[str] | None = Field(default=None, max_length=_MAX_LIST)
    operation: Operation | None = None
    required_scopes: list[str] | None = Field(default=None, max_length=_MAX_LIST)

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str | None) -> str | None:
        if v is not None and not _DOMAIN_RE.match(v):
            raise ValueError("domain must be lowercase [a-z0-9_-], 1-40 chars, starting a-z")
        return v

    # Validators run only for values the client SENT (defaults are not
    # validated), so `None` here means an explicit null: rejected, because
    # these columns are NOT NULL. Only `domain` may be cleared with null.
    @field_validator("categories", "tags", "required_scopes")
    @classmethod
    def _lists(cls, v: list[str] | None) -> list[str]:
        if v is None:
            raise ValueError("must be a list (use [] to clear), not null")
        return _clean_list(v)

    @field_validator("operation")
    @classmethod
    def _op_not_null(cls, v: str | None) -> str:
        if v is None:
            raise ValueError("operation cannot be null; use 'unknown'")
        return v

    def to_fields(self) -> dict[str, Any]:
        """Only the fields the client actually sent (explicit null domain clears it)."""
        sent = self.model_fields_set
        out: dict[str, Any] = {}
        for name in ("domain", "categories", "tags", "operation", "required_scopes"):
            if name in sent:
                out[name] = getattr(self, name)
        return out


def success_rate(tool: MCPToolRecord) -> float | None:
    if not tool.call_count:
        return None
    return (tool.call_count - tool.error_count) / tool.call_count


def tool_out(tool: MCPToolRecord, *, server_name: str, rank: float | None = None) -> ToolOut:
    return ToolOut(
        id=tool.id,
        server_id=tool.server_id,
        server_name=server_name,
        name=tool.name,
        description=tool.description,
        input_schema=tool.input_schema or {},
        schema_hash=tool.schema_hash,
        version=tool.version,
        domain=tool.domain,
        categories=list(tool.capabilities or []),
        tags=list(tool.tags or []),
        operation=tool.operation,
        required_scopes=list(tool.required_scopes or []),
        classification_reviewed=tool.classification_reviewed,
        enabled=tool.enabled,
        available=tool.available,
        stats=ToolStatsOut(
            call_count=tool.call_count,
            error_count=tool.error_count,
            avg_latency_ms=tool.avg_latency_ms,
            success_rate=success_rate(tool),
        ),
        embedding_backend=tool.embedding_backend,
        created_at=tool.created_at,
        updated_at=tool.updated_at,
        rank=rank,
    )


def version_out(v: ToolVersionRecord) -> ToolVersionOut:
    return ToolVersionOut(
        version=v.version,
        schema_hash=v.schema_hash,
        change_kind=v.change_kind,
        recorded_at=v.recorded_at,
        snapshot=v.snapshot or {},
    )


class SuggestionToolRef(Wire):
    id: str
    name: str
    server_name: str
    enabled: bool


class SuggestionOut(Wire):
    id: str
    tool_a: SuggestionToolRef | None  # None if the tool has since been removed
    tool_b: SuggestionToolRef | None
    similarity: float
    rationale: str
    preferred_tool_id: str | None
    status: str
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None
    created_at: datetime


class SuggestionPageOut(Wire):
    items: list[SuggestionOut]
    total: int
    limit: int
    offset: int


class DedupRunIn(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    threshold: float | None = Field(default=None, ge=0.5, le=1.0)


class DedupRunOut(Wire):
    pairs_considered: int
    created: int
    refreshed: int
    skipped_decided: int


class DismissIn(Wire):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    justification: str = Field(min_length=1, max_length=2000)


def suggestion_out(sug: DuplicateSuggestion, refs: dict[str, SuggestionToolRef]) -> SuggestionOut:
    return SuggestionOut(
        id=sug.id,
        tool_a=refs.get(sug.tool_a_id),
        tool_b=refs.get(sug.tool_b_id),
        similarity=sug.similarity,
        rationale=sug.rationale,
        preferred_tool_id=sug.preferred_tool_id,
        status=sug.status,
        resolved_by=sug.resolved_by,
        resolved_at=sug.resolved_at,
        resolution_note=sug.resolution_note,
        created_at=sug.created_at,
    )
