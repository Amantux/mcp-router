"""Skill-source CRUD + sync orchestration (sync SQLAlchemy; caller commits)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mcprouter.models import PolicyRule, SkillSourceRecord
from mcprouter.settings import Settings
from mcprouter.skills import gitsource
from mcprouter.skills.ingest import sync_source

_log = logging.getLogger(__name__)

# Constant client-facing messages per failure class: the exception text is
# never echoed (a runner/stderr could carry URLs, tokens or paths).
_GIT_SYNC_MESSAGES: dict[type[gitsource.GitSourceError], str] = {
    gitsource.GitCloneFailedError: "git clone failed",
    gitsource.GitTimeoutError: "git clone timed out",
    gitsource.GitTooLargeError: "repository too large",
    gitsource.GitInvalidRefError: "invalid git ref",
    gitsource.GitNotRepositoryError: "not a git repository",
}
_GIT_SYNC_FALLBACK = "git sync failed"


def git_error_message(exc: gitsource.GitSourceError) -> str:
    for cls, msg in _GIT_SYNC_MESSAGES.items():
        if isinstance(exc, cls):
            return msg
    return _GIT_SYNC_FALLBACK


class SourceError(ValueError):
    """Curated client-facing error (never echoes paths/URLs)."""


def validate_location(kind: str, location: str, git_ref: str | None) -> None:
    if kind == "directory":
        if not os.path.isabs(location):
            raise SourceError("directory sources need an absolute path")
    elif kind == "git":
        if not location.lower().startswith("https://"):
            raise SourceError("git sources must use https")
        try:
            parts = urlsplit(location)
            userinfo = parts.username is not None or parts.password is not None
        except ValueError:
            raise SourceError("git source URL is malformed") from None
        if userinfo or "@" in parts.netloc:
            # Stored and echoed on every read: never accept embedded credentials.
            raise SourceError("git source URLs must not embed credentials (user:token@)")
        try:
            gitsource.validate_ref(git_ref or "main")
        except gitsource.GitSourceError as exc:
            raise SourceError(git_error_message(exc)) from exc
    else:
        raise SourceError("kind must be 'directory' or 'git'")


def referencing_rules(session: Session, source_id: str) -> int:
    q = select(func.count()).where(
        PolicyRule.resource_kind == "skill", PolicyRule.server_id == source_id
    )
    return int(session.scalar(q) or 0)


def run_sync(session: Session, src: SkillSourceRecord, settings: Settings) -> dict[str, Any]:
    root: Path
    if src.kind == "git":
        try:
            root, commit = gitsource.fetch(
                src.id, src.location, src.git_ref, settings.skills_cache_dir
            )
        except gitsource.GitSourceError as exc:
            src.status = "offline"
            _log.warning("skill source git sync failed: %s", type(exc).__name__)
            msg = git_error_message(exc)
            return {"added": 0, "changed": 0, "removed": 0, "skipped": [], "error": msg}
        src.last_commit = commit
    else:
        root = Path(src.location)
    return sync_source(session, src, root, settings)
