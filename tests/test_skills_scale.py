"""Skill testbed: generator ground truth vs ingest, and 1,000-skill ingest timing."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from testbed.skills.generate import generate, make_git_repo

from mcprouter.models import SkillRecord, SkillSourceRecord
from mcprouter.settings import Settings
from mcprouter.skills.ingest import sync_source


@pytest.fixture()
def sdb(db: Any) -> Iterator[Any]:
    yield db
    with db() as s:
        s.execute(delete(SkillSourceRecord))
        s.commit()


def _sync(sdb: Any, root: Path, settings: Settings) -> tuple[dict[str, Any], float]:
    sid = str(uuid.uuid4())
    with sdb() as s:
        s.add(SkillSourceRecord(id=sid, name=f"s-{sid[:8]}", kind="directory", location=str(root)))
        s.commit()
        t0 = time.perf_counter()
        rep = sync_source(s, s.get(SkillSourceRecord, sid), root, settings)
        s.commit()
        return rep, time.perf_counter() - t0


def test_generator_is_deterministic(tmp_path: Path) -> None:
    a = generate(tmp_path / "a", 40, seed=7)
    b = generate(tmp_path / "b", 40, seed=7)
    assert a == b and len(a) == 40
    assert (tmp_path / "a" / "pdf-extract-text" / "SKILL.md").read_bytes() == (
        tmp_path / "b" / "pdf-extract-text" / "SKILL.md"
    ).read_bytes()
    groups = [v["duplicate_group"] for v in a.values() if v["duplicate_group"]]
    assert groups and all(groups.count(g) == 2 for g in groups)
    assert {v["domain"] for v in a.values()} == {
        "development", "communication", "files", "databases", "productivity"
    }  # fmt: skip


def test_ground_truth_matches_ingest(sdb: Any, settings: Settings, tmp_path: Path) -> None:
    truth = generate(tmp_path, 60, seed=1, include_invalid=True)
    invalid = json.loads((tmp_path / "invalid.json").read_text())
    rep, _ = _sync(sdb, tmp_path, settings)
    assert rep["added"] == len(truth)
    reasons = {e["path"]: e["reason"] for e in rep["skipped"]}
    for path, want in invalid.items():
        assert want in reasons[path], (path, reasons)
    with sdb() as s:
        rows = {r.name: r for r in s.scalars(select(SkillRecord))}
    assert set(rows) == set(truth)
    for name, gt in truth.items():
        assert rows[name].has_scripts == gt["has_scripts"], name
        for flag in gt["expect_flags"]:
            assert flag in rows[name].ingest_flags


def test_as_git_uses_injected_runner(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: Sequence[str]) -> None:
        calls.append(list(argv))

    make_git_repo(tmp_path, runner=runner)
    assert [c[3] for c in calls] == ["init", "add", "commit"]
    assert all(c[:3] == ["git", "-C", str(tmp_path)] for c in calls)


def test_ingest_1000_skills_under_60s(sdb: Any, settings: Settings, tmp_path: Path) -> None:
    generate(tmp_path, 1000, seed=0)
    rep, elapsed = _sync(sdb, tmp_path, settings)
    print(f"\n[scale] ingested 1000 skills in {elapsed:.2f}s")
    assert rep["added"] == 1000 and rep["skipped"] == []
    with sdb() as s:
        assert s.scalar(select(func.count()).select_from(SkillRecord)) == 1000
    assert elapsed < 60
