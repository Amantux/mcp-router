"""Oversize resources are listed but never served (wave-4 integrator item A2)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcprouter.skills.serve import SkillFiles, SkillServeError
from mcprouter.skills.walker import build_manifest


def _skill(manifest: list[dict[str, Any]]) -> Any:
    return SimpleNamespace(id="s1", relative_path="pdf", resource_manifest=manifest)


def test_oversize_listed_but_refused_as_stale(tmp_path: Path) -> None:
    sdir = tmp_path / "pdf"
    sdir.mkdir()
    (sdir / "SKILL.md").write_text("---\nname: pdf\n---\n")
    (sdir / "big.md").write_text("x" * 50)
    manifest, _ = build_manifest(tmp_path, sdir, resource_max_bytes=10, skipped=[])
    big = next(e for e in manifest if e["path"] == "big.md")
    assert big["oversize"] is True and big["sha256"] is None
    files = SkillFiles(tmp_path, 10_000)  # serve cap larger than the ingest cap
    with pytest.raises(SkillServeError) as ei:
        files.read_resource(_skill(manifest), "big.md")
    assert ei.value.code == "stale"


def test_oversize_flag_refused_even_with_a_hash(tmp_path: Path) -> None:
    sdir = tmp_path / "pdf"
    sdir.mkdir()
    (sdir / "big.md").write_text("x" * 50)
    digest = hashlib.sha256(b"x" * 50).hexdigest()
    entry = {"path": "big.md", "size": 50, "kind": "other", "oversize": True, "sha256": digest}
    with pytest.raises(SkillServeError) as ei:
        SkillFiles(tmp_path, 10_000).read_resource(_skill([entry]), "big.md")
    assert ei.value.code == "stale"
