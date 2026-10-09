"""Skill-source CRUD + sync orchestration (sync SQLAlchemy; caller commits)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mcprouter.models import PolicyRule, SkillSourceRecord
from mcprouter.settings import Settings
from mcprouter.skills import gitsource
from mcprouter.skills.ingest import sync_source


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
            gitsource.validate_ref(git_ref or "main")
        except gitsource.GitSourceError as exc:
            raise SourceError(str(exc)) from exc
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
            return {"added": 0, "changed": 0, "removed": 0, "skipped": [], "error": str(exc)}
        src.last_commit = commit
    else:
        root = Path(src.location)
    return sync_source(session, src, root, settings)
