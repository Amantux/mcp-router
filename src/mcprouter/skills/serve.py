"""Safe serving of skill bodies and resource files (wave-4 S3).

Skill content is UNTRUSTED author content handed to agents: it is served
verbatim, never executed and never interpolated into the router's own prompts.

Every resource read is confined to the skill directory under its source root
(safe_join semantics, symlink escapes refused via realpath), restricted to the
paths ingest recorded in ``resource_manifest``, and size-capped.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from mcprouter.models import SkillRecord, SkillSourceRecord

log = logging.getLogger(__name__)


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


# Markup a browser would execute: never served as text (mime-XSS posture);
# always an application/octet-stream blob. Scripts are included: a .js served
# as text/javascript is executable if a client ever renders it. .ts/.tsx stay
# text/plain: no browser executes TypeScript, and text/plain is never sniffed
# into script by a nosniff-respecting client.
_ACTIVE_CONTENT_EXT = frozenset(
    {".html", ".htm", ".xhtml", ".svg", ".svgz", ".xml", ".js", ".mjs", ".cjs", ".jsx"}
)
_BLOB_MIME = "application/octet-stream"

_TEXT_MIME = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".csv": "text/csv",
    ".ts": "text/plain",
    ".tsx": "text/plain",
}


def resource_mime(path: str, is_text: bool) -> str:
    """THE mime decision, shared by resources/read (serve) and resources/list
    (gateway). Active content and non-text are always an octet-stream blob."""
    ext = PurePosixPath(path).suffix.lower()
    if not is_text or ext in _ACTIVE_CONTENT_EXT:
        return _BLOB_MIME
    return _TEXT_MIME.get(ext, "text/plain")


def manifest_entries(skill: SkillRecord) -> dict[str, dict[str, object]]:
    """Normalized path -> manifest entry. A malformed entry is logged and
    skipped: one bad entry must not make the whole skill unservable."""
    out: dict[str, dict[str, object]] = {}
    for e in skill.resource_manifest or []:
        try:
            if not isinstance(e, dict) or not isinstance(e.get("path"), str):
                raise SkillServeError("invalid_path", "malformed entry")
            out[normalize_relpath(e["path"])] = e
        except SkillServeError:
            log.warning("skill %s: skipping malformed manifest entry", skill.id)
    return out


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
        entries = manifest_entries(skill)
        if rel not in entries:
            raise SkillServeError("not_in_manifest", "Resource is not part of this skill.")
        sdir = self.skill_dir(skill.relative_path)
        real = os.path.realpath(os.path.join(sdir, rel))
        if not _within(real, sdir) or real == sdir:
            raise SkillServeError("invalid_path", "Resource path escapes the skill directory.")
        # O_NOFOLLOW + fstat on the opened fd: a final component swapped to a
        # symlink (or non-regular file) after realpath() is refused, not followed.
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)  # a FIFO must not hang a worker thread
        )
        try:
            fd = os.open(real, flags)
        except FileNotFoundError:
            raise SkillServeError("not_found", "Resource not found.") from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:  # swapped to a symlink after the escape check
                raise SkillServeError(
                    "invalid_path", "Resource path escapes the skill directory."
                ) from None
            raise SkillServeError("unreadable", "Resource could not be read.") from None
        try:
            st = os.fstat(fd)  # before fdopen: it refuses directories itself
            if not stat.S_ISREG(st.st_mode):
                os.close(fd)
                raise SkillServeError("not_found", "Resource not found.")
            if st.st_size > self._max:
                os.close(fd)
                raise SkillServeError("too_large", "Resource exceeds the size cap.")
            with os.fdopen(fd, "rb") as fh:
                data = fh.read(self._max + 1)
        except OSError:
            raise SkillServeError("unreadable", "Resource could not be read.") from None
        if len(data) > self._max:  # grew between fstat and read
            raise SkillServeError("too_large", "Resource exceeds the size cap.")
        # sha256 is REQUIRED: the content check is what closes the dir-swap race
        # (a swapped directory serves different bytes). No hash => refuse as stale.
        want = entries[rel].get("sha256")
        if not isinstance(want, str) or not want:
            raise SkillServeError("stale", "Resource changed since the skill was indexed.")
        if hashlib.sha256(data).hexdigest() != want.lower():
            raise SkillServeError("stale", "Resource changed since the skill was indexed.")
        if resource_mime(rel, True) != _BLOB_MIME and b"\x00" not in data:
            try:
                text = data.decode("utf-8")
                return ResourceContent(rel, resource_mime(rel, True), text=text)
            except UnicodeDecodeError:
                pass
        return ResourceContent(rel, _BLOB_MIME, blob=data)


def read_body(skill: SkillRecord, max_bytes: int) -> str:
    """Body from the DB (ingest already capped it; re-cap defensively)."""
    body = skill.body or ""
    raw = body.encode("utf-8")
    if len(raw) > max_bytes:
        return raw[:max_bytes].decode("utf-8", errors="ignore")
    return body
