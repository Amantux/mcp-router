"""Skill embedding: embed only skills whose canonical text (or backend) changed."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.inference.hash_backend import HashEmbeddingBackend
from mcprouter.inference.pipeline import canonical_skill_text, embed_pending_skills
from mcprouter.models import SkillRecord, SkillSourceRecord

from .conftest import requires_db

pytestmark = requires_db


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


def test_canonical_skill_text_uses_first_kb_of_body() -> None:
    body = "x" * 5000
    text = canonical_skill_text("pdf-fill", " Fill PDFs ", body)
    assert text.startswith("pdf-fill: Fill PDFs\n")
    assert text.endswith("x" * 1024) and len(text) == len("pdf-fill: Fill PDFs\n") + 1024
    # body beyond 1KB is not part of the identity
    assert canonical_skill_text("a", "b", body) == canonical_skill_text("a", "b", body + "zz")


def _seed(s: Session) -> tuple[str, str]:
    src = SkillSourceRecord(name="src-embed", kind="directory", location="/tmp/x")
    s.add(src)
    s.flush()
    a = SkillRecord(
        source_id=src.id,
        name="pdf-fill",
        description="Fill PDF forms",
        body="Use pypdf",
        relative_path="pdf-fill",
        content_hash="0" * 64,
        manifest_hash="0" * 64,
    )
    b = SkillRecord(
        source_id=src.id,
        name="git-log",
        description="Summarise git history",
        body="",
        relative_path="git-log",
        content_hash="1" * 64,
        manifest_hash="1" * 64,
    )
    s.add_all([a, b])
    s.commit()
    return a.id, b.id


def test_embeds_then_skips_then_reembeds_only_changed(db: sessionmaker[Session]) -> None:
    with db() as s:
        a_id, b_id = _seed(s)
    be = Counting()
    with db() as s:
        rep = embed_pending_skills(s, be)
        s.commit()
    assert (rep.embedded, rep.skipped, be.texts) == (2, 0, 2)

    be2 = Counting()
    with db() as s:
        rep = embed_pending_skills(s, be2)
        s.commit()
    assert (rep.embedded, rep.skipped, be2.texts) == (0, 2, 0)  # no backend call at all

    with db() as s:
        a = s.get(SkillRecord, a_id)
        assert a is not None
        a.body = "Use pypdf, carefully"
        s.commit()
    be3 = Counting()
    with db() as s:
        rep = embed_pending_skills(s, be3)
        s.commit()
    assert (rep.embedded, be3.texts) == (1, 1)
    assert "carefully" in be3.batches[0][0]

    be4 = Counting(name="other-v1")  # backend switch re-embeds everything
    with db() as s:
        assert embed_pending_skills(s, be4).embedded == 2
        s.commit()
    with db() as s:
        b = s.get(SkillRecord, b_id)
        assert b is not None and b.embedding_backend == "other-v1"


def test_concurrent_skill_change_is_not_clobbered(db: sessionmaker[Session]) -> None:
    with db() as s:
        a_id, _ = _seed(s)
        a = s.get(SkillRecord, a_id)
        assert a is not None
        a.updated_at = datetime(2020, 1, 1, tzinfo=UTC)
        s.commit()

    class RacingSync(Counting):
        def embed(self, texts: list[str]) -> list[list[float]]:
            with db() as other:
                t = other.get(SkillRecord, a_id)
                assert t is not None
                t.description = "Fill and sign PDF forms"
                other.commit()
            return super().embed(texts)

    with db() as s:
        rep = embed_pending_skills(s, RacingSync())
        s.commit()
    assert rep.conflicts == 1 and rep.embedded == 1
    with db() as s:
        a = s.get(SkillRecord, a_id)
        assert a is not None
        assert a.embedding is None
        assert a.updated_at > datetime(2020, 1, 2, tzinfo=UTC)


@pytest.mark.parametrize("n", [0])
def test_empty_catalog(db: sessionmaker[Session], n: int) -> None:
    with db() as s:
        rep = embed_pending_skills(s, Counting())
    assert rep.considered == n and rep.batches == 0
