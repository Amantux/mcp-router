"""Catalog service over MCPToolRecord (FR-02): search/filter/pagination,
classification storage + human review, enable/disable, detail with history.

Keyword search is Postgres full-text search over name + description + tags
(GIN expression index `ix_tools_fts`, see registry/schema.py). Query terms are
OR-ed (`plainto_tsquery` with `&` rewritten to `|`) so one matching term is
enough to be a candidate, and results are ordered by `ts_rank_cd` (more
matching terms / name hits rank higher) — never alphabetically alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from typing import cast as tcast

from sqlalchemy import CursorResult, cast, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from mcprouter.models import MCPServerRecord, MCPToolRecord, ToolVersionRecord
from mcprouter.registry.audit import audit
from mcprouter.registry.classify import OPERATIONS, Classification, ToolClassifier
from mcprouter.registry.errors import InvalidArgument, ToolNotFound
from mcprouter.registry.schema import tsv_sql

MAX_LIMIT = 200
_TSQUERY_SQL = "replace(plainto_tsquery('english'::regconfig, :q)::text, ' & ', ' | ')::tsquery"


def _fts_where_sql(qualifier: str = "") -> str:
    return f"{tsv_sql(qualifier)} @@ {_TSQUERY_SQL}"


def _rank_sql(qualifier: str) -> str:
    return f"ts_rank_cd({tsv_sql(qualifier)}, {_TSQUERY_SQL})"


# ------------------------------------------------------------------ search
@dataclass(frozen=True)
class ToolFilter:
    q: str | None = None
    domain: str | None = None
    operation: str | None = None
    tags: list[str] | None = None  # tool must carry ALL of these tags
    server_id: str | None = None
    enabled: bool | None = None
    available: bool | None = None
    classification_reviewed: bool | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True)
class ToolHit:
    tool: MCPToolRecord
    server_name: str
    rank: float | None


@dataclass(frozen=True)
class ToolPage:
    items: list[ToolHit]
    total: int
    limit: int
    offset: int


def search_tools(session: Session, f: ToolFilter) -> ToolPage:
    if not 1 <= f.limit <= MAX_LIMIT:
        raise InvalidArgument(f"limit must be between 1 and {MAX_LIMIT}.")
    if f.offset < 0:
        raise InvalidArgument("offset must be >= 0.")
    if f.operation is not None and f.operation not in OPERATIONS:
        raise InvalidArgument("operation must be one of read, write, execute, unknown.")

    T = MCPToolRecord
    conds: list[Any] = []
    q = (f.q or "").strip()
    params: dict[str, Any] = {}
    if q:
        conds.append(text(_fts_where_sql("mcp_tools.")))
        params["q"] = q
    if f.domain is not None:
        conds.append(T.domain == f.domain)
    if f.operation is not None:
        conds.append(T.operation == f.operation)
    if f.tags:
        conds.append(cast(T.tags, JSONB).contains(list(f.tags)))
    if f.server_id is not None:
        conds.append(T.server_id == f.server_id)
    if f.enabled is not None:
        conds.append(T.enabled.is_(f.enabled))
    if f.available is not None:
        conds.append(T.available.is_(f.available))
    if f.classification_reviewed is not None:
        conds.append(T.classification_reviewed.is_(f.classification_reviewed))

    total = session.execute(select(func.count()).select_from(T).where(*conds), params).scalar_one()

    stmt = select(T, MCPServerRecord.name).join(MCPServerRecord, MCPServerRecord.id == T.server_id)
    if q:
        rank = text(_rank_sql("mcp_tools."))
        stmt = stmt.add_columns(rank).order_by(
            text(f"{_rank_sql('mcp_tools.')} DESC"), T.call_count.desc(), T.name, T.id
        )
    else:
        stmt = stmt.order_by(T.name, T.id)
    stmt = stmt.where(*conds).limit(f.limit).offset(f.offset)

    items: list[ToolHit] = []
    for row in session.execute(stmt, params):
        items.append(ToolHit(tool=row[0], server_name=row[1], rank=float(row[2]) if q else None))
    return ToolPage(items=items, total=int(total), limit=f.limit, offset=f.offset)


# ------------------------------------------------------------------ detail
@dataclass(frozen=True)
class ToolDetail:
    tool: MCPToolRecord
    server_name: str
    versions: list[ToolVersionRecord] = field(default_factory=list)


def _load(session: Session, tool_id: str) -> MCPToolRecord:
    tool = session.get(MCPToolRecord, tool_id)
    if tool is None:
        raise ToolNotFound()
    return tool


def server_name_of(session: Session, server_id: str) -> str:
    return session.execute(
        select(MCPServerRecord.name).where(MCPServerRecord.id == server_id)
    ).scalar_one()


def get_tool_detail(session: Session, tool_id: str) -> ToolDetail:
    tool = _load(session, tool_id)
    versions = list(
        session.scalars(
            select(ToolVersionRecord)
            .where(ToolVersionRecord.tool_id == tool_id)
            .order_by(ToolVersionRecord.version.desc(), ToolVersionRecord.recorded_at.desc())
        )
    )
    return ToolDetail(
        tool=tool, server_name=server_name_of(session, tool.server_id), versions=versions
    )


# ---------------------------------------------------------- classification
# Wire field name -> column. `categories` (SPEC §8) is stored in `capabilities`.
_CLASSIFICATION_COLUMNS = {
    "domain": "domain",
    "categories": "capabilities",
    "tags": "tags",
    "operation": "operation",
    "required_scopes": "required_scopes",
}


@dataclass(frozen=True)
class ClassificationUpdate:
    """Fields to override (wire names, snake_case). Empty = approve as-is."""

    fields: dict[str, Any]


def update_classification(
    session: Session, tool_id: str, upd: ClassificationUpdate, *, actor: str
) -> MCPToolRecord:
    """Human override: wins over any automatic classification, forever
    (sets classification_reviewed=True, which apply_auto_classification honours)."""
    unknown = set(upd.fields) - set(_CLASSIFICATION_COLUMNS)
    if unknown:
        raise InvalidArgument("Unsupported classification field.")
    op = upd.fields.get("operation")
    if "operation" in upd.fields and op not in OPERATIONS:
        raise InvalidArgument("operation must be one of read, write, execute, unknown.")
    tool = _load(session, tool_id)
    for wire_name, value in upd.fields.items():
        setattr(tool, _CLASSIFICATION_COLUMNS[wire_name], value)
    tool.classification_reviewed = True
    tool.classification_source = "human"
    session.flush()
    audit(
        "tool.classification.override",
        actor=actor,
        tool_id=tool.id,
        tool=tool.name,
        fields=",".join(sorted(upd.fields)) or "(approve)",
    )
    return tool


def apply_auto_classification(
    session: Session, tool_id: str, c: Classification, *, source: str | None = None
) -> bool:
    """Write an AUTOMATIC classification. Returns False (and changes nothing)
    when a human has reviewed the record.

    The guard is in the UPDATE's WHERE clause, not a Python check on a loaded
    object, so a classifier that read the tool before a human reviewed it
    still cannot overwrite the human's values.
    Capabilities are only written when the classifier supplies some.
    """
    if c.operation not in OPERATIONS:
        raise InvalidArgument("operation must be one of read, write, execute, unknown.")
    values: dict[str, Any] = {"operation": c.operation, "domain": c.domain}
    if source is not None:
        values["classification_source"] = source
    if c.capabilities:
        values["capabilities"] = list(c.capabilities)
    stmt = (
        update(MCPToolRecord)
        .where(
            MCPToolRecord.id == tool_id,
            MCPToolRecord.classification_reviewed.is_(False),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    result = tcast(CursorResult[Any], session.execute(stmt))
    return result.rowcount == 1


@dataclass(frozen=True)
class ClassifyRun:
    classified: int
    skipped_reviewed: int


def auto_classify(
    session: Session, classifier: ToolClassifier, tool_ids: list[str] | None = None
) -> ClassifyRun:
    stmt = select(
        MCPToolRecord.id, MCPToolRecord.name, MCPToolRecord.description, MCPToolRecord.input_schema
    ).where(MCPToolRecord.classification_reviewed.is_(False))
    if tool_ids is not None:
        stmt = stmt.where(MCPToolRecord.id.in_(tool_ids))
    classified = skipped = 0
    for tid, name, desc, schema in session.execute(stmt).all():
        c = classifier.classify(name, desc or "", schema or {})
        if apply_auto_classification(session, tid, c, source=classifier.name):
            classified += 1
        else:
            skipped += 1  # reviewed between our read and the write
    return ClassifyRun(classified=classified, skipped_reviewed=skipped)


# ---------------------------------------------------------- enable/disable
def set_enabled(session: Session, tool_id: str, enabled: bool, *, actor: str) -> MCPToolRecord:
    tool = _load(session, tool_id)
    previous = tool.enabled
    tool.enabled = enabled
    session.flush()
    audit(
        "tool.enable" if enabled else "tool.disable",
        actor=actor,
        tool_id=tool.id,
        tool=tool.name,
        server_id=tool.server_id,
        previous=previous,
    )
    return tool
