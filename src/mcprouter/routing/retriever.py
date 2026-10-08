"""Hybrid candidate retrieval (FR-03): pgvector cosine + Postgres full-text,
fused by reciprocal-rank fusion (RRF).

Design decisions (load-bearing):

* **Vector leg** compares only rows whose `embedding_backend` equals the query
  embedder's `name` — vectors from different backends share dimensions but not
  a space, so comparing them would be silent garbage (scoping.md §5).
* **Keyword leg** runs over `name + description + tags` with a raw
  `to_tsvector` expression (models.py is frozen, so no generated column or
  GIN index). Each query word is normalised by `plainto_tsquery` individually
  and the lexemes are OR-ed: `plainto_tsquery` alone ANDs every term, which
  makes natural-language queries ("find the open bugs in my repo") match almost
  nothing. OR + `ts_rank_cd` keeps the ranking meaningful. The keyword leg is
  also what keeps tools WITHOUT embeddings reachable.
* **RRF** (k=60) fuses ranks, not raw scores — cosine distances and ts_rank
  live on incomparable scales. The fused score is normalised by the best
  possible value (rank 1 in both legs) into (0, 1].
* `matched_on` is honest: "vector" only when the vector leg returned the row,
  "keyword:<query word>" only for query words whose lexeme is in the tool's
  document.
* Filters: tool enabled + available, server enabled and not offline, optional
  server_ids. `enabled_only=False` drops the enabled/available/offline filters
  (admin routing simulation); it never drops `server_ids`.

Only values are bound as parameters; no identifier is ever composed from
input.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, select, text
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.interfaces import EmbeddingBackend, ToolCandidate
from mcprouter.models import MCPServerRecord, MCPToolRecord

RRF_K = 60
_MAX_QUERY_WORDS = 32
_WORD = re.compile(r"[a-z0-9]+")
_LEXEME = re.compile(r"'((?:[^']|'')*)'")

# Weighted document: name (A) > tags (B) > description (C). Underscores in
# snake_case tool names become spaces so "search_issues" yields both words.
_DOC_SQL = (
    "setweight(to_tsvector('english', replace(t.name, '_', ' ')), 'A') || "
    "setweight(to_tsvector('english', coalesce(t.tags::text, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce(t.description, '')), 'C')"
)

_KEYWORD_SQL = text(
    f"""
    SELECT t.id AS id,
           ts_rank_cd({_DOC_SQL}, CAST(:tsq AS tsquery)) AS rank,
           tsvector_to_array({_DOC_SQL}) AS lexemes
    FROM mcp_tools t JOIN mcp_servers s ON s.id = t.server_id
    WHERE ({_DOC_SQL}) @@ CAST(:tsq AS tsquery)
      AND (NOT :enabled_only OR (t.enabled AND t.available AND s.enabled
                                 AND s.status <> 'offline'))
      AND (CAST(:server_ids AS text[]) IS NULL OR t.server_id = ANY(CAST(:server_ids AS text[])))
    ORDER BY rank DESC, t.id
    LIMIT :lim
    """
)
# _DOC_SQL is a module constant (no input); every value above is bound.

_LEXEMES_SQL = text(
    "SELECT w AS word, plainto_tsquery('english', w)::text AS q FROM unnest(CAST(:words AS text[])) AS w"
)


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
            vector_ids = self._vector_leg(s, qvec, leg_limit, server_ids, enabled_only)
            keyword_hits = self._keyword_leg(s, query, leg_limit, server_ids, enabled_only)

            fused: dict[str, float] = {}
            for rank, tid in enumerate(vector_ids, start=1):
                fused[tid] = fused.get(tid, 0.0) + 1.0 / (RRF_K + rank)
            for rank, (tid, _) in enumerate(keyword_hits, start=1):
                fused[tid] = fused.get(tid, 0.0) + 1.0 / (RRF_K + rank)
            best = 2.0 / (RRF_K + 1)
            # Deterministic order: score desc, then id.
            top = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
            rows = self._load_rows(s, [tid for tid, _ in top])

        vector_set = set(vector_ids)
        kw_terms = dict(keyword_hits)
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
    ) -> list[str]:
        dist = MCPToolRecord.embedding.cosine_distance(qvec)
        stmt = (
            select(MCPToolRecord.id)
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
        return list(s.scalars(stmt))

    def _keyword_leg(
        self,
        s: Session,
        query: str,
        lim: int,
        server_ids: list[str] | None,
        enabled_only: bool,
    ) -> list[tuple[str, list[str]]]:
        """Returns [(tool_id, [matched query words])] in rank order."""
        words = list(dict.fromkeys(_WORD.findall(query.lower())))[:_MAX_QUERY_WORDS]
        if not words:
            return []
        # word -> its normalised lexemes (stopwords map to none).
        word_lexemes: dict[str, set[str]] = {}
        for word, q in s.execute(_LEXEMES_SQL, {"words": words}):
            lex = {m.replace("''", "'") for m in _LEXEME.findall(q or "")}
            if lex:
                word_lexemes[word] = lex
        if not word_lexemes:
            return []
        # Each part is plainto_tsquery output (already valid tsquery syntax);
        # OR them so any matching term is evidence.
        tsq = " | ".join(
            "(" + " & ".join(_quote(lx) for lx in sorted(lexemes)) + ")"
            for lexemes in word_lexemes.values()
        )
        res = s.execute(
            _KEYWORD_SQL,
            {
                "tsq": tsq,
                "enabled_only": enabled_only,
                "server_ids": server_ids,
                "lim": lim,
            },
        )
        hits: list[tuple[str, list[str]]] = []
        for tid, _rank, lexemes in res:
            doc = set(lexemes or [])
            hits.append((tid, [w for w, lx in word_lexemes.items() if lx & doc]))
        return hits

    @staticmethod
    def _load_rows(s: Session, ids: Sequence[str]) -> dict[str, _Row]:
        if not ids:
            return {}
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
            .where(MCPToolRecord.id.in_(ids))
        )
        return {r[0]: _Row(*r) for r in s.execute(stmt)}


def _quote(lexeme: str) -> str:
    """Quote a lexeme as a tsquery literal (single quotes doubled)."""
    return "'" + lexeme.replace("'", "''") + "'"
