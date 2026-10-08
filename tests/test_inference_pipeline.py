"""Embedding pipeline: embed only tools whose canonical text (or backend) changed."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.inference.errors import EmbeddingDimensionError
from mcprouter.inference.hash_backend import HashEmbeddingBackend
from mcprouter.inference.pipeline import canonical_tool_text, embed_pending_tools
from mcprouter.models import MCPServerRecord, MCPToolRecord

from .conftest import requires_db


class Counting:
    def __init__(self, name: str = "hash-v1") -> None:
        self.name = name
        self.batches: list[list[str]] = []
        self._inner = HashEmbeddingBackend()

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return self._inner.embed(texts)

    @property
    def texts(self) -> int:
        return sum(len(b) for b in self.batches)


def test_canonical_text_shape_and_tag_order_independence() -> None:
    assert canonical_tool_text("read_file", " Read a file ", ["fs", "io"]) == (
        "read_file: Read a file [fs, io]"
    )
    assert canonical_tool_text("t", "d", ["b", "a", "a", " "]) == canonical_tool_text(
        "t", "d", ["a", "b"]
    )
    assert canonical_tool_text("t", "d", []) == "t: d []"


def _seed(s: Session) -> tuple[str, str]:
    srv = MCPServerRecord(name="fs", transport="stdio")
    s.add(srv)
    s.flush()
    a = MCPToolRecord(
        server_id=srv.id, name="read_file", description="Read a file", schema_hash="x", tags=["fs"]
    )
    b = MCPToolRecord(
        server_id=srv.id, name="send_message", description="Post a message", schema_hash="y"
    )
    s.add_all([a, b])
    s.flush()
    return a.id, b.id


@requires_db
def test_embeds_new_tools_then_skips_unchanged(db: sessionmaker[Session]) -> None:
    with db() as s:
        a_id, b_id = _seed(s)
        s.commit()

    be = Counting()
    with db() as s:
        rep = embed_pending_tools(s, be)
        s.commit()
    assert (rep.considered, rep.embedded, rep.skipped) == (2, 2, 0)
    assert be.texts == 2

    with db() as s:
        a = s.get(MCPToolRecord, a_id)
        assert a is not None and a.embedding is not None
        text = canonical_tool_text("read_file", "Read a file", ["fs"])
        assert a.embedding_backend == "hash-v1"
        assert a.embedding_text_hash == hashlib.sha256(text.encode()).hexdigest()
        assert list(a.embedding) == pytest.approx(HashEmbeddingBackend().embed([text])[0], abs=1e-6)

    # The load-bearing property: nothing changed -> the backend is NOT called again.
    with db() as s:
        rep = embed_pending_tools(s, be)
        s.commit()
    assert (rep.embedded, rep.skipped) == (0, 2)
    assert be.texts == 2 and len(be.batches) == 1


@requires_db
def test_reembeds_only_the_changed_tool(db: sessionmaker[Session]) -> None:
    with db() as s:
        a_id, b_id = _seed(s)
        embed_pending_tools(s, Counting())
        s.commit()

    with db() as s:
        b = s.get(MCPToolRecord, b_id)
        assert b is not None
        b.description = "Post a chat message to a channel"
        a = s.get(MCPToolRecord, a_id)
        assert a is not None
        a.tags = ["fs"]  # same tags, new list object: not a text change
        s.commit()

    be = Counting()
    with db() as s:
        rep = embed_pending_tools(s, be)
        s.commit()
    assert rep.embedded == 1
    assert be.batches == [
        [canonical_tool_text("send_message", "Post a chat message to a channel", [])]
    ]


@requires_db
def test_reembeds_on_backend_change_or_missing_vector(db: sessionmaker[Session]) -> None:
    with db() as s:
        a_id, _ = _seed(s)
        embed_pending_tools(s, Counting())
        s.commit()

    # Vectors from different backends are never comparable (scoping #5).
    other = Counting(name="bge-small-en-v1.5")
    with db() as s:
        assert embed_pending_tools(s, other).embedded == 2
        s.commit()

    with db() as s:
        a = s.get(MCPToolRecord, a_id)
        assert a is not None
        a.embedding = None  # hash still matches, vector gone
        s.commit()
    again = Counting(name="bge-small-en-v1.5")
    with db() as s:
        assert embed_pending_tools(s, again).embedded == 1
        s.commit()


@requires_db
def test_reembedding_does_not_touch_updated_at(db: sessionmaker[Session]) -> None:
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    with db() as s:
        a_id, _ = _seed(s)
        a = s.get(MCPToolRecord, a_id)
        assert a is not None
        a.updated_at = stamp
        s.commit()
    with db() as s:
        embed_pending_tools(s, Counting())
        s.commit()
    with db() as s:
        got = s.execute(
            select(MCPToolRecord.updated_at).where(MCPToolRecord.id == a_id)
        ).scalar_one()
        assert got == stamp


@requires_db
def test_batches_and_rejects_wrong_width(db: sessionmaker[Session]) -> None:
    with db() as s:
        _seed(s)
        be = Counting()
        rep = embed_pending_tools(s, be, batch_size=1)
        assert rep.batches == 2 and [len(b) for b in be.batches] == [1, 1]
        s.rollback()

    class Wide:
        name = "wide"

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 768 for _ in texts]

    with db() as s:
        _seed(s)
        with pytest.raises(EmbeddingDimensionError):
            embed_pending_tools(s, Wide())
        s.rollback()
