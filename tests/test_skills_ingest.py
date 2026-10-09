"""Ingest reconcile: versions, change kinds, removal/restore, flags, status."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select

from mcprouter import generation
from mcprouter.models import SkillRecord, SkillSourceRecord, SkillVersionRecord
from mcprouter.settings import Settings
from mcprouter.skills.ingest import sync_source


def _write(root: Path, name: str, desc: str = "d", body: str = "body", dirname: str = "") -> Path:
    d = root / (dirname or name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n")
    return d


@pytest.fixture()
def sdb(db: Any) -> Iterator[Any]:
    yield db
    with db() as s:
        s.execute(delete(SkillSourceRecord))
        s.commit()


def _run(sdb: Any, root: Path, settings: Settings, sid: str) -> dict[str, Any]:
    with sdb() as s:
        src = s.get(SkillSourceRecord, sid)
        rep = sync_source(s, src, root, settings)
        s.commit()
        return rep


def _new_source(sdb: Any, root: Path) -> str:
    sid = str(uuid.uuid4())
    with sdb() as s:
        s.add(
            SkillSourceRecord(id=sid, name=f"src-{sid[:8]}", kind="directory", location=str(root))
        )
        s.commit()
    return sid


def _kinds(sdb: Any, name: str) -> list[str]:
    with sdb() as s:
        rec = s.scalars(select(SkillRecord).where(SkillRecord.name == name)).one()
        rows = s.scalars(
            select(SkillVersionRecord)
            .where(SkillVersionRecord.skill_id == rec.id)
            .order_by(SkillVersionRecord.version)
        )
        return [r.change_kind for r in rows]


def test_lifecycle(sdb: Any, settings: Settings, tmp_path: Path) -> None:
    sid = _new_source(sdb, tmp_path)
    _write(tmp_path, "alpha")
    _write(tmp_path, "beta", body="x" * 10)
    _write(tmp_path, "gamma", dirname="not-gamma")
    g0 = generation.catalog_generation()
    rep = _run(sdb, tmp_path, settings, sid)
    assert rep["added"] == 2 and rep["skipped"][0]["path"] == "not-gamma"
    assert generation.catalog_generation() > g0
    with sdb() as s:
        assert s.get(SkillSourceRecord, sid).status == "degraded"
        beta = s.scalars(select(SkillRecord).where(SkillRecord.name == "beta")).one()
        assert beta.body_tokens_est == 3  # ceil(11/4)
    g1 = generation.catalog_generation()
    assert _run(sdb, tmp_path, settings, sid) == {
        "added": 0,
        "changed": 0,
        "removed": 0,
        "skipped": rep["skipped"],
    }
    assert generation.catalog_generation() == g1  # no-op sync does not bump
    _write(tmp_path, "alpha", body="new body")
    _write(tmp_path, "beta", desc="new desc", body="x" * 10)
    (tmp_path / "beta" / "extra.txt").write_text("r")
    _run(sdb, tmp_path, settings, sid)
    assert _kinds(sdb, "alpha") == ["added", "content"]
    assert _kinds(sdb, "beta") == ["added", "metadata"]
    (tmp_path / "beta" / "extra.txt").write_text("changed")
    _run(sdb, tmp_path, settings, sid)
    assert _kinds(sdb, "beta")[-1] == "resources"
    (tmp_path / "alpha" / "SKILL.md").unlink()
    assert _run(sdb, tmp_path, settings, sid)["removed"] == 1
    with sdb() as s:
        a = s.scalars(select(SkillRecord).where(SkillRecord.name == "alpha")).one()
        assert a.available is False
    _write(tmp_path, "alpha", body="new body")
    _run(sdb, tmp_path, settings, sid)
    assert _kinds(sdb, "alpha")[-2:] == ["removed", "restored"]


def test_flags_and_offline(sdb: Any, settings: Settings, tmp_path: Path) -> None:
    sid = _new_source(sdb, tmp_path)
    _write(tmp_path, "leaky", body="token ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
    _run(sdb, tmp_path, settings, sid)
    with sdb() as s:
        rec = s.scalars(select(SkillRecord).where(SkillRecord.name == "leaky")).one()
        assert "secret_like" in rec.ingest_flags
        assert "ghp_" in rec.body  # content is never mutated
    _run(sdb, tmp_path / "missing", settings, sid)
    with sdb() as s:
        assert s.get(SkillSourceRecord, sid).status == "offline"


def test_unknown_keys_and_skill_md_cap(sdb: Any, settings: Settings, tmp_path: Path) -> None:
    sid = _new_source(sdb, tmp_path)
    _write(tmp_path, "vendor", desc="d\nx-vendor: 1")
    cap = settings.skill_body_max_bytes + 16 * 1024
    _write(tmp_path, "huge", body="b" * (cap + 10))
    rep = _run(sdb, tmp_path, settings, sid)
    assert {"path": "huge/SKILL.md", "reason": "skill_md_too_large"} in rep["skipped"]
    with sdb() as s:
        rec = s.scalars(select(SkillRecord).where(SkillRecord.name == "vendor")).one()
        assert "unknown_frontmatter_keys" in rec.ingest_flags
        assert rec.skill_metadata["_unknown_keys"] == "x-vendor"
        assert s.scalars(select(SkillRecord).where(SkillRecord.name == "huge")).first() is None
