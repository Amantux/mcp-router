"""Walk a skill source root for */SKILL.md and build resource manifests.

Symlinks (files or dirs) whose realpath leaves the source root are refused;
in-root symlinks are followed for files only (directories are never followed,
so link cycles cannot loop). `.git` and `node_modules` are skipped.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKIP_DIRS = frozenset({".git", "node_modules"})
_KINDS = {"scripts": "script", "references": "reference", "assets": "asset"}


@dataclass
class FoundSkill:
    dir: Path  # real path of the skill directory
    relative_path: str  # posix, relative to the source root
    skill_md: bytes
    manifest: list[dict[str, Any]]
    has_scripts: bool


@dataclass
class WalkResult:
    skills: list[FoundSkill] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


def _inside(root: Path, p: Path) -> bool:
    try:
        Path(os.path.realpath(p)).relative_to(root)
    except ValueError:
        return False
    return True


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(
    root: Path, skill_dir: Path, *, resource_max_bytes: int, skipped: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], bool]:
    manifest: list[dict[str, Any]] = []
    has_scripts = False
    for dirpath, dirnames, filenames in os.walk(skill_dir, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        base = Path(dirpath)
        # Owner decision (wave 4): a child directory with its own SKILL.md is a
        # separate skill; its files never appear in the parent's manifest.
        dirnames[:] = [d for d in dirnames if not (base / d / "SKILL.md").exists()]
        for d in list(dirnames):
            if (base / d).is_symlink():
                dirnames.remove(d)
                skipped.append({"path": _rel(root, base / d), "reason": "symlinked directory"})
        for fn in sorted(filenames):
            p = base / fn
            rel_skill = p.relative_to(skill_dir).as_posix()
            if rel_skill == "SKILL.md":
                continue
            if not _inside(root, p):
                skipped.append({"path": _rel(root, p), "reason": "symlink escapes source root"})
                continue
            st = p.stat()
            if not stat.S_ISREG(st.st_mode):
                continue
            kind = _KINDS.get(rel_skill.split("/", 1)[0], "other") if "/" in rel_skill else "other"
            entry: dict[str, Any] = {"path": rel_skill, "size": st.st_size, "kind": kind}
            if st.st_size > resource_max_bytes:
                entry["oversize"] = True
                entry["sha256"] = None
            else:
                entry["sha256"] = _sha256(p)
            if kind == "script" or st.st_mode & 0o111:
                has_scripts = True
            manifest.append(entry)
    return manifest, has_scripts


def _rel(root: Path, p: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.name


def _read_capped(p: Path, cap: int | None) -> bytes | None:
    """Read at most cap+1 bytes; None if larger than cap. O_NOFOLLOW + fstat
    closes the swap-to-symlink race between the walk's checks and this read."""
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise OSError("not a regular file")
        data = f.read() if cap is None else f.read(cap + 1)
    if cap is not None and len(data) > cap:
        return None
    return data


def walk_source(
    root_in: Path | str, *, resource_max_bytes: int, skill_md_max_bytes: int | None = None
) -> WalkResult:
    root = Path(os.path.realpath(root_in))
    res = WalkResult()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not (base / d).is_symlink()
        )
        if "SKILL.md" not in filenames or base == root:
            continue
        md = base / "SKILL.md"
        if not _inside(root, md) or not md.is_file():
            res.skipped.append({"path": _rel(root, md), "reason": "symlink escapes source root"})
            continue
        try:
            skill_md = _read_capped(md, skill_md_max_bytes)
        except OSError:
            res.skipped.append({"path": _rel(root, md), "reason": "SKILL.md not readable"})
            continue
        if skill_md is None:
            res.skipped.append({"path": _rel(root, md), "reason": "skill_md_too_large"})
            continue
        manifest, has_scripts = build_manifest(
            root, base, resource_max_bytes=resource_max_bytes, skipped=res.skipped
        )
        res.skills.append(
            FoundSkill(
                dir=base,
                relative_path=base.relative_to(root).as_posix(),
                skill_md=skill_md,
                manifest=manifest,
                has_scripts=has_scripts,
            )
        )
    return res
