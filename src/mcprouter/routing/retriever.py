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
from typing import Any

from sqlalchemy import ColumnElement, Engine, and_, select, text
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.interfaces import EmbeddingBackend, ToolCandidate
from mcprouter.models import MCPServerRecord, MCPToolRecord, SkillRecord, SkillSourceRecord
from mcprouter.registry.schema import index_exists
from mcprouter.routing.cache import QueryEmbeddingCache

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
    ORDER BY ts_rank_cd(h.doc, (SELECT q FROM tsq)) DESC, h.server_name, h.name
    LIMIT :lim
    """
)

KEYWORD_INDEX = "ix_tools_routing_fts"

# Skills: name (A, hyphens -> spaces) > tags (B) > description (C) > the first
# 2KB of the body (D). Un-embedded skills stay reachable through this leg.
_SKILL_DOC_TEMPLATE = (
    "setweight(to_tsvector('english', replace({p}name, '-', ' ')), 'A') || "
    "setweight(to_tsvector('english', coalesce({p}tags::text, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce({p}description, '')), 'C') || "
    "setweight(to_tsvector('english', left(coalesce({p}body, ''), 2048)), 'D')"
)
_SKILL_DOC_SQL = _SKILL_DOC_TEMPLATE.format(p="k.")

_SKILL_KEYWORD_SQL = text(
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
      SELECT k.id, k.source_id, k.name, src.name AS source_name, k.description,
             k.domain, k.operation, k.body_tokens_est, {_SKILL_DOC_SQL} AS doc
      FROM skills k JOIN skill_sources src ON src.id = k.source_id
      WHERE ({_SKILL_DOC_SQL}) @@ (SELECT q FROM tsq)
        AND (NOT :enabled_only OR (k.enabled AND k.available AND src.enabled
                                   AND src.status <> 'offline'))
    )
    SELECT h.id, h.source_id, h.name, h.source_name, h.description, h.domain,
           h.operation, h.body_tokens_est,
           ARRAY(SELECT words.w FROM words
                 WHERE words.q::text <> '' AND h.doc @@ words.q) AS matched
    FROM hits h
    ORDER BY ts_rank_cd(h.doc, (SELECT q FROM tsq)) DESC, h.source_name, h.name
    LIMIT :lim
    """
)

SKILL_KEYWORD_INDEX = "ix_skills_routing_fts"


def ensure_skill_keyword_index(engine: Engine) -> None:
    """Skills twin of ensure_keyword_index — call it at app init next to it.
    Constant DDL; expression must stay byte-identical to _SKILL_DOC_TEMPLATE."""
    ddl = (
        f"CREATE INDEX IF NOT EXISTS {SKILL_KEYWORD_INDEX} ON skills "
        f"USING GIN (({_SKILL_DOC_TEMPLATE.format(p='')}))"
    )
    with engine.begin() as conn:
        if not index_exists(conn, SKILL_KEYWORD_INDEX):  # in migration 0001: zero DDL
            conn.execute(text(ddl))


def skill_eligibility_filters() -> list[ColumnElement[bool]]:
    """Skill routing eligibility: skill enabled + available, source enabled
    and not offline. Shared by both skill legs and the route-cache re-check."""
    return [
        SkillRecord.enabled.is_(True),
        SkillRecord.available.is_(True),
        SkillSourceRecord.enabled.is_(True),
        SkillSourceRecord.status != "offline",
    ]


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
        if not index_exists(conn, KEYWORD_INDEX):  # in migration 0001: zero DDL
            conn.execute(text(ddl))


def eligibility_filters() -> list[ColumnElement[bool]]:
    """Routing eligibility (discovery's rule): tool enabled + available, server
    enabled and not offline. Shared by both legs and the route-cache re-check."""
    return [
        MCPToolRecord.enabled.is_(True),
        MCPToolRecord.available.is_(True),
        MCPServerRecord.enabled.is_(True),
        MCPServerRecord.status != "offline",
    ]


@dataclass(frozen=True)
class _Row:
    tool_id: str
    server_id: str
    tool_name: str
    server_name: str
    description: str
    domain: str | None
    operation: str
    kind: str = "tool"
    body_tokens_est: int = 0

    @property
    def key(self) -> str:
        """Fusion key: tools bare, skills "skill:<id>" (no cross-kind collision)."""
        return self.tool_id if self.kind == "tool" else f"skill:{self.tool_id}"


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
        self._query_vectors = QueryEmbeddingCache()  # keyed (text, backend name)

    def retrieve(
        self,
        query: str,
        *,
        limit: int,
        server_ids: list[str] | None = None,
        enabled_only: bool = True,
        kinds: tuple[str, ...] = ("tool",),
    ) -> list[ToolCandidate]:
        """`server_ids` scopes MCP tools only; skills are scoped by policy
        downstream (PolicyScope.permits dispatches on kind)."""
        want_tools = "tool" in kinds and server_ids != []
        want_skills = "skill" in kinds
        if limit <= 0 or not (want_tools or want_skills):
            return []
        leg_limit = max(limit * self._leg_multiplier, limit)
        qvec = self._query_vectors.get_or_compute(
            query, self._embedder.name, lambda q: self._embedder.embed([q])[0]
        )
        # One RRF over up to four ranked lists (tool/skill x vector/keyword):
        # native scores are per-table, so each kind's legs rank independently.
        vec_lists: list[list[_Row]] = []
        kw_lists: list[list[tuple[_Row, list[str]]]] = []
        with self._factory() as s:
            if want_tools:
                vec_lists.append(self._vector_leg(s, qvec, leg_limit, server_ids, enabled_only))
                kw_lists.append(self._keyword_leg(s, query, leg_limit, server_ids, enabled_only))
            if want_skills:
                vec_lists.append(self._skill_vector_leg(s, qvec, leg_limit, enabled_only))
                kw_lists.append(self._skill_keyword_leg(s, query, leg_limit, enabled_only))
        vector_rows = [r for lst in vec_lists for r in lst]

        rows: dict[str, _Row] = {}
        fused: dict[str, float] = {}
        for vlist in vec_lists:
            for rank, row in enumerate(vlist, start=1):
                rows[row.key] = row
                fused[row.key] = fused.get(row.key, 0.0) + 1.0 / (RRF_K + rank)
        kw_terms: dict[str, list[str]] = {}
        for klist in kw_lists:
            for rank, (row, words) in enumerate(klist, start=1):
                rows[row.key] = row
                kw_terms[row.key] = words
                fused[row.key] = fused.get(row.key, 0.0) + 1.0 / (RRF_K + rank)
        best = 2.0 / (RRF_K + 1)
        # Deterministic, catalog-stable order: score desc, then server/tool
        # NAME (ids are random per catalog, so an id tie-break is not reproducible).
        top = sorted(
            fused.items(),
            key=lambda kv: (-kv[1], rows[kv[0]].server_name, rows[kv[0]].tool_name),
        )[:limit]

        vector_set = {r.key for r in vector_rows}
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
                    kind=row.kind,
                    body_tokens_est=row.body_tokens_est,
                )
            )
        return out

    # ------------------------------------------------------------- legs
    @staticmethod
    def _filters(server_ids: list[str] | None, enabled_only: bool) -> list[ColumnElement[bool]]:
        f: list[ColumnElement[bool]] = []
        if enabled_only:
            f += eligibility_filters()
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
            .order_by(dist, MCPServerRecord.name, MCPToolRecord.name)
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

    def _skill_vector_leg(
        self, s: Session, qvec: list[float], lim: int, enabled_only: bool
    ) -> list[_Row]:
        dist = SkillRecord.embedding.cosine_distance(qvec)
        filters = skill_eligibility_filters() if enabled_only else []
        stmt = (
            select(
                SkillRecord.id,
                SkillRecord.source_id,
                SkillRecord.name,
                SkillSourceRecord.name,
                SkillRecord.description,
                SkillRecord.domain,
                SkillRecord.operation,
                SkillRecord.body_tokens_est,
            )
            .join(SkillSourceRecord, SkillSourceRecord.id == SkillRecord.source_id)
            .where(
                and_(
                    SkillRecord.embedding.is_not(None),
                    SkillRecord.embedding_backend == self._embedder.name,
                    *filters,
                )
            )
            .order_by(dist, SkillSourceRecord.name, SkillRecord.name)
            .limit(lim)
        )
        return [_skill_row(r) for r in s.execute(stmt)]

    def _skill_keyword_leg(
        self, s: Session, query: str, lim: int, enabled_only: bool
    ) -> list[tuple[_Row, list[str]]]:
        words = list(dict.fromkeys(_WORD.findall(query.lower())))[:_MAX_QUERY_WORDS]
        if not words:
            return []
        res = s.execute(
            _SKILL_KEYWORD_SQL, {"words": words, "enabled_only": enabled_only, "lim": lim}
        )
        return [(_skill_row(r), list(r[8] or [])) for r in res]


def _skill_row(r: Any) -> _Row:
    """(id, source_id, name, source_name, description, domain, operation, tokens, ...)"""
    return _Row(
        tool_id=r[0], server_id=r[1], tool_name=r[2], server_name=r[3],
        description=r[4], domain=r[5], operation=r[6],
        kind="skill", body_tokens_est=int(r[7] or 0),
    )  # fmt: skip
