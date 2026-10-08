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

Transactions: this flushes; the CALLER owns commit/rollback (one commit per
request/job). Re-embedding does not bump `updated_at` — that column means
"tool metadata changed", and a new vector is not a metadata change.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from mcprouter.inference.engine import batched
from mcprouter.inference.errors import EmbeddingDimensionError
from mcprouter.interfaces import EmbeddingBackend
from mcprouter.models import EMBEDDING_DIM, MCPToolRecord


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

    n_batches = 0
    for chunk in batched(pending, batch_size):
        vectors = backend.embed([text for _, text, _, _ in chunk])
        if len(vectors) != len(chunk) or any(len(v) != EMBEDDING_DIM for v in vectors):
            raise EmbeddingDimensionError(
                f"backend {backend.name!r} returned vectors that are not {EMBEDDING_DIM}-dim"
            )
        session.execute(
            update(T),
            [
                {
                    "id": tool_id,
                    "embedding": vec,
                    "embedding_backend": backend.name,
                    "embedding_text_hash": h,
                    "updated_at": updated_at,  # explicit: suppress the onupdate bump
                }
                for (tool_id, _, h, updated_at), vec in zip(chunk, vectors, strict=True)
            ],
        )
        n_batches += 1
    session.flush()
    return EmbedReport(
        backend=backend.name,
        considered=len(rows),
        embedded=len(pending),
        skipped=len(rows) - len(pending),
        batches=n_batches,
    )
