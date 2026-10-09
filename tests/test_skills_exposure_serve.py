"""Wave-4 S3: safe file serving + bundle (no DB). Every refusal is mutation-checked."""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from mcprouter.models import SkillRecord
from mcprouter.skills.bundle import MARKER, BundleError, build_bundle
from mcprouter.skills.serve import SkillFiles, SkillServeError, normalize_relpath, read_body

_SHA = {
    p: hashlib.sha256(b).hexdigest()
    for p, b in {
        "references/guide.md": b"# Guide",
        "references/bin.dat": b"\x00\x01\xff",
        "big.txt": b"x" * 200,
    }.items()
}


def _skill(rel: str, manifest: list[str], name: str = "pdf-tools") -> SkillRecord:
    return SkillRecord(
        id="s1",
        source_id="src",
        name=name,
        description="Work with PDFs",
        body="Do the thing.",
        relative_path=rel,
        resource_manifest=[
            {"path": p, "size": 1, "kind": "reference", "sha256": _SHA.get(p, "")} for p in manifest
        ],
        license=None,
        compatibility=None,
        skill_metadata={},
        allowed_tools=[],
    )


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    sd = root / "skills" / "pdf-tools"
    (sd / "references").mkdir(parents=True)
    (sd / "SKILL.md").write_text("---\nname: pdf-tools\n---\nbody")
    (sd / "references" / "guide.md").write_text("# Guide")
    (sd / "references" / "bin.dat").write_bytes(b"\x00\x01\xff")
    (sd / "big.txt").write_text("x" * 200)
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    os.symlink(tmp_path / "secret.txt", sd / "references" / "escape.md")
    return root


def _files(root: Path, cap: int = 100) -> SkillFiles:
    return SkillFiles(root, cap)


def test_reads_text_and_binary(tree: Path) -> None:
    sk = _skill("skills/pdf-tools", ["references/guide.md", "references/bin.dat"])
    t = _files(tree).read_resource(sk, "references/guide.md")
    assert t.text == "# Guide" and t.mime_type == "text/markdown"
    b = _files(tree).read_resource(sk, "references/bin.dat")
    assert b.blob == b"\x00\x01\xff" and b.text is None


@pytest.mark.parametrize(
    "path",
    [
        "../SKILL.md",
        "references/../../../secret.txt",
        "/etc/passwd",
        "a\\b",
        "",
        "references/./../../x",
    ],
)
def test_refuses_traversal_and_absolute(tree: Path, path: str) -> None:
    sk = _skill("skills/pdf-tools", [path])  # even if a manifest lists it
    with pytest.raises(SkillServeError) as ei:
        _files(tree).read_resource(sk, path)
    assert ei.value.code == "invalid_path"


def test_refuses_symlink_escape(tree: Path) -> None:
    sk = _skill("skills/pdf-tools", ["references/escape.md"])
    with pytest.raises(SkillServeError) as ei:
        _files(tree).read_resource(sk, "references/escape.md")
    assert ei.value.code == "invalid_path"


def test_refuses_skill_dir_escape(tree: Path) -> None:
    sk = _skill("..", ["secret.txt"])
    with pytest.raises(SkillServeError):
        _files(tree).read_resource(sk, "secret.txt")


def test_refuses_non_manifest_path(tree: Path) -> None:
    sk = _skill("skills/pdf-tools", ["references/guide.md"])
    with pytest.raises(SkillServeError) as ei:
        _files(tree).read_resource(sk, "SKILL.md")
    assert ei.value.code == "not_in_manifest"


def test_refuses_oversize(tree: Path) -> None:
    sk = _skill("skills/pdf-tools", ["big.txt"])
    with pytest.raises(SkillServeError) as ei:
        _files(tree, cap=100).read_resource(sk, "big.txt")
    assert ei.value.code == "too_large"
    assert _files(tree, cap=200).read_resource(sk, "big.txt").text == "x" * 200


def test_error_messages_do_not_leak_paths(tree: Path) -> None:
    sk = _skill("skills/pdf-tools", ["references/missing.md"])
    with pytest.raises(SkillServeError) as ei:
        _files(tree).read_resource(sk, "references/missing.md")
    assert ei.value.code == "not_found" and str(tree) not in ei.value.message


def test_read_body_caps() -> None:
    sk = _skill("x", [])
    sk.body = "é" * 10
    assert read_body(sk, 5) == "éé"
    assert read_body(sk, 1000) == "é" * 10


def test_normalize_relpath() -> None:
    assert normalize_relpath("a//b/c") == "a/b/c"
    for bad in ("C:/x", "a/../b", "a/\x00"):
        with pytest.raises(SkillServeError):
            normalize_relpath(bad)


# ------------------------------------------------------------------ bundle
def test_bundle_layout_and_marker(tree: Path) -> None:
    sk = _skill("skills/pdf-tools", ["references/guide.md", "references/escape.md"])
    data, skipped = build_bundle([(sk, _files(tree))])
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())
    assert names == {"pdf-tools/SKILL.md", "pdf-tools/references/guide.md", MARKER}
    assert skipped == ["pdf-tools/references/escape.md"]  # symlink escape never bundled
    assert "TOP SECRET" not in data.decode("latin-1")
    md = zf.read("pdf-tools/SKILL.md").decode()
    assert md.startswith('---\nname: "pdf-tools"\n') and md.endswith("Do the thing.")
    assert json.loads(zf.read(MARKER)) == {"skills": ["pdf-tools"]}
    for n in names:
        assert not n.startswith("/") and ".." not in n.split("/")


def test_bundle_refuses_zip_slip_name(tree: Path) -> None:
    for bad in ("..", "a/b", "/abs"):
        with pytest.raises(BundleError):
            build_bundle([(_skill("skills/pdf-tools", [], name=bad), _files(tree))])


def test_bundle_caps(tree: Path) -> None:
    sk = _skill("skills/pdf-tools", ["big.txt"])
    with pytest.raises(BundleError) as ei:
        build_bundle([(sk, _files(tree, cap=1000))], max_total_bytes=150)
    assert ei.value.code == "too_large"
    with pytest.raises(BundleError) as ei:
        build_bundle([(sk, _files(tree))] * 2, max_skills=1)
    assert ei.value.code == "too_many"
