"""serve.py hardening: O_NOFOLLOW swap TOCTOU, active-content mime, stale hash."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcprouter.skills.serve import SkillFiles, SkillServeError


def _skill(manifest: list[dict[str, Any]]) -> Any:
    return SimpleNamespace(relative_path="sk", resource_manifest=manifest)


def _setup(tmp_path: Path, name: str, data: bytes) -> tuple[SkillFiles, Path]:
    sd = tmp_path / "root" / "sk"
    sd.mkdir(parents=True)
    (sd / name).write_bytes(data)
    return SkillFiles(tmp_path / "root", 4096), sd


def test_symlink_swapped_in_after_manifest_build_is_refused(tmp_path: Path) -> None:
    files, sd = _setup(tmp_path, "a.md", b"ok")
    sk = _skill([{"path": "a.md", "size": 2}])
    assert files.read_resource(sk, "a.md").text == "ok"
    # Swap a.md to a symlink AFTER the realpath() escape check: simulated by
    # making realpath() the identity (as if it ran before the swap).
    (sd / "b.md").write_text("other")
    os.unlink(sd / "a.md")
    os.symlink(sd / "b.md", sd / "a.md")
    real_realpath = os.path.realpath
    try:
        os.path.realpath = lambda p, *a, **k: str(p)  # type: ignore[assignment]
        with pytest.raises(SkillServeError) as ei:
            files.read_resource(sk, "a.md")
    finally:
        os.path.realpath = real_realpath  # type: ignore[assignment]
    assert ei.value.code == "invalid_path"


def test_fifo_or_dir_is_not_served(tmp_path: Path) -> None:
    files, sd = _setup(tmp_path, "a.md", b"ok")
    (sd / "d").mkdir()
    with pytest.raises(SkillServeError) as ei:
        files.read_resource(_skill([{"path": "d"}]), "d")
    assert ei.value.code == "not_found"  # rejected by fstat, not by a failed read


@pytest.mark.parametrize("name", ["x.html", "x.htm", "x.svg", "x.xhtml"])
def test_active_content_served_as_blob(tmp_path: Path, name: str) -> None:
    payload = b"<svg onload=alert(1)></svg>"
    files, _ = _setup(tmp_path, name, payload)
    rc = files.read_resource(_skill([{"path": name}]), name)
    assert rc.text is None and rc.blob == payload
    assert rc.mime_type == "application/octet-stream"


def test_content_swap_detected_as_stale(tmp_path: Path) -> None:
    files, sd = _setup(tmp_path, "a.md", b"original")
    sk = _skill([{"path": "a.md", "sha256": hashlib.sha256(b"original").hexdigest()}])
    assert files.read_resource(sk, "a.md").text == "original"
    (sd / "a.md").write_bytes(b"tampered")
    with pytest.raises(SkillServeError) as ei:
        files.read_resource(sk, "a.md")
    assert ei.value.code == "stale"


def test_fifo_does_not_hang(tmp_path: Path) -> None:
    files, sd = _setup(tmp_path, "a.md", b"ok")
    os.mkfifo(sd / "f.md")
    with pytest.raises(SkillServeError) as ei:
        files.read_resource(_skill([{"path": "f.md"}]), "f.md")
    assert ei.value.code == "not_found"
