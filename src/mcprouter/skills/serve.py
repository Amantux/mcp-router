"""Safe serving of skill bodies and resource files (wave-4 S3).

Skill content is UNTRUSTED author content handed to agents: it is served
verbatim, never executed and never interpolated into the router's own prompts.

Every resource read is confined to the skill directory under its source root
(safe_join semantics, symlink escapes refused via realpath), restricted to the
paths ingest recorded in ``resource_manifest``, and size-capped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from mcprouter.models import SkillRecord, SkillSourceRecord


class SkillServeError(Exception):
    """Typed refusal with a curated, client-safe message (never a raw OS error)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResourceContent:
    path: str  # manifest-relative POSIX path
    mime_type: str
    text: str | None = None  # set for UTF-8 text
    blob: bytes | None = None  # set for binary

    @property
    def is_text(self) -> bool:
        return self.text is not None


_TEXT_MIME = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".csv": "text/csv",
    ".html": "text/html",
    ".js": "text/javascript",
    ".ts": "text/plain",
}


def normalize_relpath(path: str) -> str:
    """Return a clean relative POSIX path or raise. Refuses absolute paths,
    drive letters, backslashes, empty/`.`/`..` segments and NUL bytes."""
    if not path or "\x00" in path or "\\" in path:
        raise SkillServeError("invalid_path", "Invalid resource path.")
    if path.startswith("/") or (len(path) > 1 and path[1] == ":"):
        raise SkillServeError("invalid_path", "Resource paths must be relative.")
    parts = PurePosixPath(path).parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise SkillServeError("invalid_path", "Resource path may not contain '.' or '..'.")
    return "/".join(parts)


def source_root(source: SkillSourceRecord, cache_dir: str) -> Path:
    """Directory sources are served from their location; git sources from the
    clone under the cache dir keyed by source id (S1 ingest seam: the clone
    path MUST follow this convention — recorded in the integration notes)."""
    if source.kind == "directory":
        return Path(source.location)
    return Path(cache_dir) / source.id


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


class SkillFiles:
    def __init__(self, source_root: Path | str, max_bytes: int) -> None:
        self._root = Path(source_root)
        self._max = max_bytes

    def skill_dir(self, skill_relative_path: str) -> str:
        rel = "." if skill_relative_path in ("", ".") else normalize_relpath(skill_relative_path)
        root_real = os.path.realpath(self._root)
        d = os.path.realpath(os.path.join(root_real, rel))
        if not _within(d, root_real):
            raise SkillServeError("invalid_path", "Skill directory escapes its source.")
        return d

    def read_resource(self, skill: SkillRecord, resource_path: str) -> ResourceContent:
        rel = normalize_relpath(resource_path)
        manifest = {normalize_relpath(str(e["path"])) for e in skill.resource_manifest or []}
        if rel not in manifest:
            raise SkillServeError("not_in_manifest", "Resource is not part of this skill.")
        sdir = self.skill_dir(skill.relative_path)
        real = os.path.realpath(os.path.join(sdir, rel))
        if not _within(real, sdir) or real == sdir:
            raise SkillServeError("invalid_path", "Resource path escapes the skill directory.")
        try:
            st = os.stat(real)
            if not os.path.isfile(real):
                raise SkillServeError("not_found", "Resource not found.")
            if st.st_size > self._max:
                raise SkillServeError("too_large", "Resource exceeds the size cap.")
            with open(real, "rb") as fh:
                data = fh.read(self._max + 1)
        except FileNotFoundError:
            raise SkillServeError("not_found", "Resource not found.") from None
        except OSError:
            raise SkillServeError("unreadable", "Resource could not be read.") from None
        if len(data) > self._max:  # grew between stat and read
            raise SkillServeError("too_large", "Resource exceeds the size cap.")
        ext = PurePosixPath(rel).suffix.lower()
        if b"\x00" not in data:
            try:
                text = data.decode("utf-8")
                return ResourceContent(rel, _TEXT_MIME.get(ext, "text/plain"), text=text)
            except UnicodeDecodeError:
                pass
        return ResourceContent(rel, "application/octet-stream", blob=data)


def read_body(skill: SkillRecord, max_bytes: int) -> str:
    """Body from the DB (ingest already capped it; re-cap defensively)."""
    body = skill.body or ""
    raw = body.encode("utf-8")
    if len(raw) > max_bytes:
        return raw[:max_bytes].decode("utf-8", errors="ignore")
    return body
