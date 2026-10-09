"""Wave 4 S2b: reviewed-skill escalation when the skill's content changed."""

from __future__ import annotations

from mcprouter.models import SkillRecord, SkillSourceRecord
from mcprouter.registry.classify import Classification, apply_skill_classification


def _seed(db, op: str, reviewed: bool = True, flags: list[str] | None = None) -> str:  # noqa: ANN001
    with db() as s:
        src = SkillSourceRecord(name="src", kind="directory", location="/x")
        s.add(src)
        s.flush()
        sk = SkillRecord(
            source_id=src.id, name="pdf-guide", description="d", relative_path="pdf-guide",
            content_hash="0" * 64, manifest_hash="0" * 64, operation=op,
            classification_reviewed=reviewed, classification_source="human",
            ingest_flags=flags or [],
        )  # fmt: skip
        s.add(sk)
        s.commit()
        return sk.id


def _apply(db, sid: str, op: str, changed: bool) -> tuple[bool, str, bool, list[str]]:  # noqa: ANN001
    with db() as s:
        ok = apply_skill_classification(
            s, sid, Classification(operation=op, domain=None), content_changed=changed
        )
        s.commit()
    with db() as s:
        sk = s.get(SkillRecord, sid)
        assert sk is not None
        return ok, sk.operation, sk.classification_reviewed, list(sk.ingest_flags or [])


def test_unchanged_content_reviewed_guard_absolute(db) -> None:  # noqa: ANN001
    assert _apply(db, _seed(db, "read"), "execute", False) == (False, "read", True, [])


def test_changed_content_escalates_reviewed_and_flags_stale(db) -> None:  # noqa: ANN001
    sid = _seed(db, "read", flags=["secret_shaped"])
    assert _apply(db, sid, "execute", True) == (
        True, "execute", False, ["secret_shaped", "review_stale"],
    )  # fmt: skip


def test_changed_content_never_moves_reviewed_toward_read(db) -> None:  # noqa: ANN001
    assert _apply(db, _seed(db, "execute"), "read", True) == (False, "execute", True, [])


def test_stale_flag_not_duplicated(db) -> None:  # noqa: ANN001
    sid = _seed(db, "write", flags=["review_stale"])
    assert _apply(db, sid, "unknown", True)[3] == ["review_stale"]


def test_changed_content_unreviewed_gets_no_stale_flag(db) -> None:  # noqa: ANN001
    assert _apply(db, _seed(db, "read", reviewed=False), "write", True) == (
        True, "write", False, [],
    )  # fmt: skip


def test_json_null_flags_become_stale_array(db) -> None:  # noqa: ANN001
    """A JSON 'null' (not SQL NULL) in ingest_flags must not yield [null, ...]."""
    from sqlalchemy import text

    sid = _seed(db, "read")
    with db() as s:
        s.execute(text("UPDATE skills SET ingest_flags = 'null'::json WHERE id = :i"), {"i": sid})
        s.commit()
    assert _apply(db, sid, "execute", True)[3] == ["review_stale"]
