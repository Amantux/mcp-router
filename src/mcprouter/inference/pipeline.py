"""Tool-embedding pipeline (SPEC §7: cache tool embeddings, recompute only on
metadata change).

A tool is (re-)embedded iff ANY of:
* it has no vector (`embedding IS NULL`);
* its stored `embedding_text_hash` != sha256(canonical text) — name,
  description or tags changed;
* its `embedding_backend` != the backend in use — vectors from different
  backends are never comparable (scoping #5), so a backend switch re-embeds.

Everything else is skipped without calling the backend at all.

Canonical text: "name: description [tag1, tag2]" with tags stripped,
de-duplicated and sorted, so re-ordering tags is not a change.

Transactions: the CALLER owns commit/rollback (one commit per request/job).
Re-embedding does not bump `updated_at` — that column means "tool metadata
changed", and a new vector is not a metadata change (it is SET to itself, so
the ORM onupdate hook does not fire).

Concurrency: optimistic. Each row is written only `WHERE id = :id AND
updated_at = <value read>`. If a catalog sync changed the tool in between
(the ORM bumps `updated_at`), the write is skipped and counted in
`conflicts`: the stale vector is never stored and the newer timestamp is
never reverted; the tool's text hash no longer matches, so the next run
embeds the new text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import cast

from sqlalchemy import Table, bindparam, func, select, update
from sqlalchemy.orm import Session

from mcprouter.inference.engine import batched
from mcprouter.inference.errors import EmbeddingDimensionError
from mcprouter.interfaces import EmbeddingBackend
from mcprouter.models import EMBEDDING_DIM, MCPToolRecord, SkillRecord


def canonical_tool_text(name: str, description: str, tags: list[str]) -> str:
    tag_part = ", ".join(sorted({t.strip() for t in tags if t.strip()}))
    return f"{name}: {description.strip()} [{tag_part}]"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EmbedReport:
    backend: str
    considered: int
    embedded: int
    skipped: int
    batches: int
    conflicts: int = 0  # rows changed concurrently; left for the next run


def embed_pending_tools(
    session: Session, backend: EmbeddingBackend, *, batch_size: int = 32
) -> EmbedReport:
    T = MCPToolRecord
    rows = session.execute(
        select(
            T.id,
            T.name,
            T.description,
            T.tags,
            T.embedding_backend,
            T.embedding_text_hash,
            T.updated_at,
            T.embedding.is_(None).label("missing"),
        ).order_by(T.id)
    ).all()

    pending: list[tuple[str, str, str, object]] = []  # (id, text, hash, updated_at)
    for r in rows:
        text = canonical_tool_text(r.name, r.description or "", list(r.tags or []))
        h = text_hash(text)
        if r.missing or r.embedding_backend != backend.name or r.embedding_text_hash != h:
            pending.append((r.id, text, h, r.updated_at))

    tbl = cast(Table, T.__table__)
    stmt = (
        update(tbl)
        .where(tbl.c.id == bindparam("b_id"), tbl.c.updated_at == bindparam("b_seen"))
        .values(
            embedding=bindparam("b_vec", type_=tbl.c.embedding.type),
            embedding_backend=bindparam("b_backend"),
            embedding_text_hash=bindparam("b_hash"),
            updated_at=tbl.c.updated_at,  # explicit self-assignment: no onupdate bump
        )
    )
    n_batches = 0
    conflicts = 0
    for chunk in batched(pending, batch_size):
        vectors = backend.embed([text for _, text, _, _ in chunk])
        if len(vectors) != len(chunk) or any(len(v) != EMBEDDING_DIM for v in vectors):
            raise EmbeddingDimensionError(
                f"backend {backend.name!r} returned vectors that are not {EMBEDDING_DIM}-dim"
            )
        for (tool_id, _, h, seen), vec in zip(chunk, vectors, strict=True):
            # Per row (not executemany) so the conflict count is exact; refresh
            # runs are ~1k rows, so the round trips are negligible.
            result = session.execute(
                stmt,
                {
                    "b_id": tool_id,
                    "b_seen": seen,
                    "b_vec": vec,
                    "b_backend": backend.name,
                    "b_hash": h,
                },
            )
            if getattr(result, "rowcount", 1) == 0:
                conflicts += 1
        n_batches += 1
    session.flush()
    return EmbedReport(
        backend=backend.name,
        considered=len(rows),
        embedded=len(pending) - conflicts,
        skipped=len(rows) - len(pending),
        batches=n_batches,
        conflicts=conflicts,
    )


SKILL_BODY_EMBED_CHARS = 1024


def canonical_skill_text(name: str, description: str, body: str) -> str:
    """Skill identity for embedding: "name: description" + the first 1024 characters of body.

    Body beyond that is deliberately excluded: the opening section carries the
    "when to use" signal, and long bodies would otherwise dominate the vector.
    """
    return f"{name}: {description.strip()}\n{body[:SKILL_BODY_EMBED_CHARS]}"


def embed_pending_skills(
    session: Session, backend: EmbeddingBackend, *, batch_size: int = 32
) -> EmbedReport:
    """Skill mirror of `embed_pending_tools`: same recompute rules (missing vector,
    text-hash change, backend change), same optimistic `updated_at` guard and the
    same caller-owns-commit contract."""
    S = SkillRecord
    rows = session.execute(
        select(
            S.id,
            S.name,
            S.description,
            # Only the embedded prefix is loaded; bodies are unbounded Text.
            func.substr(S.body, 1, SKILL_BODY_EMBED_CHARS).label("body"),
            S.embedding_backend,
            S.embedding_text_hash,
            S.updated_at,
            S.embedding.is_(None).label("missing"),
        ).order_by(S.id)
    ).all()
    pending: list[tuple[str, str, str, object]] = []
    for r in rows:
        text = canonical_skill_text(r.name, r.description or "", r.body or "")
        h = text_hash(text)
        if r.missing or r.embedding_backend != backend.name or r.embedding_text_hash != h:
            pending.append((r.id, text, h, r.updated_at))
    tbl = cast(Table, S.__table__)
    stmt = (
        update(tbl)
        .where(tbl.c.id == bindparam("b_id"), tbl.c.updated_at == bindparam("b_seen"))
        .values(
            embedding=bindparam("b_vec", type_=tbl.c.embedding.type),
            embedding_backend=bindparam("b_backend"),
            embedding_text_hash=bindparam("b_hash"),
            updated_at=tbl.c.updated_at,  # no onupdate bump: a vector is not a metadata change
        )
    )
    n_batches = 0
    conflicts = 0
    for chunk in batched(pending, batch_size):
        vectors = backend.embed([text for _, text, _, _ in chunk])
        if len(vectors) != len(chunk) or any(len(v) != EMBEDDING_DIM for v in vectors):
            raise EmbeddingDimensionError(
                f"backend {backend.name!r} returned vectors that are not {EMBEDDING_DIM}-dim"
            )
        for (skill_id, _, h, seen), vec in zip(chunk, vectors, strict=True):
            result = session.execute(
                stmt,
                {
                    "b_id": skill_id,
                    "b_seen": seen,
                    "b_vec": vec,
                    "b_backend": backend.name,
                    "b_hash": h,
                },
            )
            if getattr(result, "rowcount", 1) == 0:
                conflicts += 1
        n_batches += 1
    session.flush()
    return EmbedReport(
        backend=backend.name,
        considered=len(rows),
        embedded=len(pending) - conflicts,
        skipped=len(rows) - len(pending),
        batches=n_batches,
        conflicts=conflicts,
    )
