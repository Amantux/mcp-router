"""Hybrid candidate retrieval (FR-03): pgvector cosine + Postgres full-text,
fused by reciprocal-rank fusion (RRF).

Design decisions (load-bearing):

* **Vector leg** compares only rows whose `embedding_backend` equals the query
  embedder's `name` — vectors from different backends share dimensions but not
  a space, so comparing them would be silent garbage (scoping.md §5).
* **Keyword leg** runs over `name + tags + description` with a raw
  `to_tsvector` expression (models.py is frozen, so no generated column).
  Each query word is normalised by `plainto_tsquery` individually and the
  results are OR-ed: `plainto_tsquery` alone ANDs every term, which makes
  natural-language queries ("find the open bugs in my repo") match almost
  nothing. OR + `ts_rank_cd` keeps ranking meaningful. The keyword leg is also
  what keeps tools WITHOUT embeddings reachable.
* **Performance**: building the document is the keyword leg's dominant cost
  (~20ms per 1,000 rows on the dev box, measured). `ensure_keyword_index`
  creates a GIN index over the byte-identical expression so `@@` is an index
  scan, and the MATERIALIZED CTE computes the document once per hit. Each leg
  is ONE round trip and returns full candidate rows (no follow-up lookup).
* **RRF** (k=60) fuses ranks, not raw scores — cosine distances and ts_rank
  live on incomparable scales. The fused score is normalised by the best
  possible value (rank 1 in both legs) into (0, 1].
* `matched_on` is honest: "vector" only when the vector leg returned the row,
  "keyword:<query word>" only for query words whose own tsquery matches the
  tool's document.
* Filters: tool enabled + available, server enabled and not offline, optional
  server_ids. `enabled_only=False` drops the enabled/available/offline filters
  (admin routing simulation); it never drops `server_ids`.

Only values are bound as parameters; the SQL text is built from module
constants only — no identifier or fragment is ever composed from input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import ColumnElement, Engine, and_, select, text
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.interfaces import EmbeddingBackend, ToolCandidate
from mcprouter.models import MCPServerRecord, MCPToolRecord

RRF_K = 60
_MAX_QUERY_WORDS = 32
_WORD = re.compile(r"[a-z0-9]+")

# Weighted document: name (A) > tags (B) > description (C). Underscores in
# snake_case tool names become spaces so "search_issues" yields both words.
_DOC_TEMPLATE = (
    "setweight(to_tsvector('english', replace({p}name, '_', ' ')), 'A') || "
    "setweight(to_tsvector('english', coalesce({p}tags::text, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce({p}description, '')), 'C')"
)
_DOC_SQL = _DOC_TEMPLATE.format(p="t.")

_KEYWORD_SQL = text(
    f"""
    WITH words AS MATERIALIZED (
      SELECT w, plainto_tsquery('english', w) AS q
      FROM unnest(CAST(:words AS text[])) AS w
    ),
    tsq AS MATERIALIZED (
      SELECT CAST(string_agg('(' || q::text || ')', ' | ') AS tsquery) AS q
      FROM words WHERE q::text <> ''
    ),
    hits AS MATERIALIZED (
      SELECT t.id, t.server_id, t.name, s.name AS server_name, t.description,
             t.domain, t.operation, {_DOC_SQL} AS doc
      FROM mcp_tools t JOIN mcp_servers s ON s.id = t.server_id
      WHERE ({_DOC_SQL}) @@ (SELECT q FROM tsq)
        AND (NOT :enabled_only OR (t.enabled AND t.available AND s.enabled
                                   AND s.status <> 'offline'))
        AND (CAST(:server_ids AS text[]) IS NULL
             OR t.server_id = ANY(CAST(:server_ids AS text[])))
    )
    SELECT h.id, h.server_id, h.name, h.server_name, h.description, h.domain,
           h.operation,
           ARRAY(SELECT words.w FROM words
                 WHERE words.q::text <> '' AND h.doc @@ words.q) AS matched
    FROM hits h
    ORDER BY ts_rank_cd(h.doc, (SELECT q FROM tsq)) DESC, h.id
    LIMIT :lim
    """
)

KEYWORD_INDEX = "ix_tools_routing_fts"


def ensure_keyword_index(engine: Engine) -> None:
    """Idempotently create the GIN expression index the keyword leg relies on.

    models.py is frozen for this branch, so the index is created here; at
    integration it belongs in an Alembic revision (expression must stay
    byte-identical to _DOC_TEMPLATE). Constant DDL — no input is composed.
    """
    ddl = (
        f"CREATE INDEX IF NOT EXISTS {KEYWORD_INDEX} ON mcp_tools "
        f"USING GIN (({_DOC_TEMPLATE.format(p='')}))"
    )
    with engine.begin() as conn:
        conn.execute(text(ddl))


@dataclass(frozen=True)
class _Row:
    tool_id: str
    server_id: str
    tool_name: str
    server_name: str
    description: str
    domain: str | None
    operation: str


class HybridRetriever:
    """Implements `interfaces.Retriever`."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        embedder: EmbeddingBackend,
        *,
        leg_multiplier: int = 3,
    ) -> None:
        self._factory = session_factory
        self._embedder = embedder
        self._leg_multiplier = leg_multiplier

    def retrieve(
        self,
        query: str,
        *,
        limit: int,
        server_ids: list[str] | None = None,
        enabled_only: bool = True,
    ) -> list[ToolCandidate]:
        if limit <= 0 or server_ids == []:
            return []
        leg_limit = max(limit * self._leg_multiplier, limit)
        qvec = self._embedder.embed([query])[0]
        with self._factory() as s:
            vector_rows = self._vector_leg(s, qvec, leg_limit, server_ids, enabled_only)
            keyword_rows = self._keyword_leg(s, query, leg_limit, server_ids, enabled_only)

        rows: dict[str, _Row] = {}
        fused: dict[str, float] = {}
        for rank, row in enumerate(vector_rows, start=1):
            rows[row.tool_id] = row
            fused[row.tool_id] = fused.get(row.tool_id, 0.0) + 1.0 / (RRF_K + rank)
        kw_terms: dict[str, list[str]] = {}
        for rank, (row, words) in enumerate(keyword_rows, start=1):
            rows[row.tool_id] = row
            kw_terms[row.tool_id] = words
            fused[row.tool_id] = fused.get(row.tool_id, 0.0) + 1.0 / (RRF_K + rank)
        best = 2.0 / (RRF_K + 1)
        # Deterministic order: score desc, then id.
        top = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

        vector_set = {r.tool_id for r in vector_rows}
        out: list[ToolCandidate] = []
        for tid, score in top:
            row = rows[tid]
            matched: list[str] = []
            if tid in vector_set:
                matched.append("vector")
            matched.extend(f"keyword:{w}" for w in kw_terms.get(tid, []))
            out.append(
                ToolCandidate(
                    tool_id=row.tool_id,
                    server_id=row.server_id,
                    tool_name=row.tool_name,
                    server_name=row.server_name,
                    description=row.description,
                    domain=row.domain,
                    operation=row.operation,
                    retrieval_score=score / best,
                    matched_on=matched,
                )
            )
        return out

    # ------------------------------------------------------------- legs
    @staticmethod
    def _filters(server_ids: list[str] | None, enabled_only: bool) -> list[ColumnElement[bool]]:
        f: list[ColumnElement[bool]] = []
        if enabled_only:
            f += [
                MCPToolRecord.enabled.is_(True),
                MCPToolRecord.available.is_(True),
                MCPServerRecord.enabled.is_(True),
                MCPServerRecord.status != "offline",
            ]
        if server_ids is not None:
            f.append(MCPToolRecord.server_id.in_(server_ids))
        return f

    def _vector_leg(
        self,
        s: Session,
        qvec: list[float],
        lim: int,
        server_ids: list[str] | None,
        enabled_only: bool,
    ) -> list[_Row]:
        dist = MCPToolRecord.embedding.cosine_distance(qvec)
        stmt = (
            select(
                MCPToolRecord.id,
                MCPToolRecord.server_id,
                MCPToolRecord.name,
                MCPServerRecord.name,
                MCPToolRecord.description,
                MCPToolRecord.domain,
                MCPToolRecord.operation,
            )
            .join(MCPServerRecord, MCPServerRecord.id == MCPToolRecord.server_id)
            .where(
                and_(
                    MCPToolRecord.embedding.is_not(None),
                    MCPToolRecord.embedding_backend == self._embedder.name,
                    *self._filters(server_ids, enabled_only),
                )
            )
            .order_by(dist, MCPToolRecord.id)
            .limit(lim)
        )
        return [_Row(*r) for r in s.execute(stmt)]

    def _keyword_leg(
        self,
        s: Session,
        query: str,
        lim: int,
        server_ids: list[str] | None,
        enabled_only: bool,
    ) -> list[tuple[_Row, list[str]]]:
        """Returns [(row, [matched query words])] in rank order."""
        words = list(dict.fromkeys(_WORD.findall(query.lower())))[:_MAX_QUERY_WORDS]
        if not words:
            return []
        res = s.execute(
            _KEYWORD_SQL,
            {
                "words": words,
                "enabled_only": enabled_only,
                "server_ids": server_ids,
                "lim": lim,
            },
        )
        return [(_Row(*r[:7]), list(r[7] or [])) for r in res]
