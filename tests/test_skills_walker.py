"""Walker: discovery, symlink escapes, manifest, size caps, hashes."""

from __future__ import annotations

import os
from pathlib import Path

from mcprouter.skills.hashing import manifest_hash
from mcprouter.skills.walker import walk_source

MD = "---\nname: {n}\ndescription: d\n---\nbody\n"


def _skill(root: Path, rel: str) -> Path:
    d = root / rel
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(MD.format(n=d.name))
    return d


def test_walk_manifest_and_kinds(tmp_path: Path) -> None:
    d = _skill(tmp_path, "deep/nested/alpha")
    (d / "scripts").mkdir()
    (d / "scripts" / "run.sh").write_text("echo hi")
    (d / "references").mkdir()
    (d / "references" / "a.md").write_text("ref")
    (d / "big.bin").write_bytes(b"x" * 50)
    _skill(tmp_path, ".git/hidden")
    _skill(tmp_path, "node_modules/pkg")
    res = walk_source(tmp_path, resource_max_bytes=10)
    assert [s.relative_path for s in res.skills] == ["deep/nested/alpha"]
    man = {e["path"]: e for e in res.skills[0].manifest}
    assert man["scripts/run.sh"]["kind"] == "script"
    assert man["references/a.md"]["kind"] == "reference"
    assert man["big.bin"]["oversize"] is True and man["big.bin"]["sha256"] is None
    assert res.skills[0].has_scripts


def test_exec_bit_counts_as_script(tmp_path: Path) -> None:
    d = _skill(tmp_path, "beta")
    (d / "tool").write_text("x")
    os.chmod(d / "tool", 0o755)
    assert walk_source(tmp_path, resource_max_bytes=100).skills[0].has_scripts


def test_symlink_escape_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP SECRET")
    root = tmp_path / "root"
    d = _skill(root, "gamma")
    (d / "leak.txt").symlink_to(outside / "secret.txt")
    (d / "inroot.txt").write_text("ok")
    (d / "alias.txt").symlink_to(d / "inroot.txt")
    (root / "linkdir").symlink_to(outside, target_is_directory=True)
    res = walk_source(root, resource_max_bytes=100)
    paths = [e["path"] for e in res.skills[0].manifest]
    assert "leak.txt" not in paths and "alias.txt" in paths
    assert any(s["reason"] == "symlink escapes source root" for s in res.skipped)


def test_symlinked_skill_md_outside_refused(tmp_path: Path) -> None:
    outside = tmp_path / "o.md"
    outside.write_text(MD.format(n="delta"))
    (tmp_path / "root" / "delta").mkdir(parents=True)
    (tmp_path / "root" / "delta" / "SKILL.md").symlink_to(outside)
    res = walk_source(tmp_path / "root", resource_max_bytes=100)
    assert res.skills == [] and res.skipped


def test_manifest_hash_order_independent() -> None:
    a = [{"path": "a", "sha256": "1", "size": 1}, {"path": "b", "sha256": "2", "size": 1}]
    assert manifest_hash(a) == manifest_hash(list(reversed(a)))
    assert manifest_hash(a) != manifest_hash(a[:1])


def test_skill_md_over_cap_skipped_without_full_read(tmp_path: Path) -> None:
    d = _skill(tmp_path, "big")
    (d / "SKILL.md").write_text(MD.format(n="big") + "x" * 500)
    size = (d / "SKILL.md").stat().st_size
    over = walk_source(tmp_path, resource_max_bytes=100, skill_md_max_bytes=size - 1)
    assert over.skills == []
    assert over.skipped == [{"path": "big/SKILL.md", "reason": "skill_md_too_large"}]
    exact = walk_source(tmp_path, resource_max_bytes=100, skill_md_max_bytes=size)
    assert len(exact.skills) == 1 and len(exact.skills[0].skill_md) == size


def test_parent_manifest_excludes_nested_child_skill(tmp_path: Path) -> None:
    parent = _skill(tmp_path, "parent")
    (parent / "notes.md").write_text("n")
    child = _skill(tmp_path, "parent/child")
    (child / "scripts").mkdir()
    (child / "scripts" / "run.sh").write_text("echo")
    res = walk_source(tmp_path, resource_max_bytes=100)
    by = {s.relative_path: s for s in res.skills}
    assert [e["path"] for e in by["parent"].manifest] == ["notes.md"]
    assert not by["parent"].has_scripts
    assert [e["path"] for e in by["parent/child"].manifest] == ["scripts/run.sh"]


def test_dangling_symlink_is_skipped_not_fatal(tmp_path: Path) -> None:
    d = _skill(tmp_path, "dang")
    (d / "gone").symlink_to(d / "missing")
    res = walk_source(tmp_path, resource_max_bytes=100)
    assert len(res.skills) == 1
    assert {"path": "dang/gone", "reason": "resource not readable"} in res.skipped
