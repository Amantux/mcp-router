"""Reconcile a walked skill source into SkillRecord / SkillVersionRecord rows.

Mirrors discovery/sync.apply_listing: rows are keyed (source_id, name); a
change bumps `version` and appends a SkillVersionRecord whose change_kind is
added|content|resources|metadata|removed|restored. Removed skills keep their
row with available=False. Content is never mutated (only the body cap applies,
flagged body_truncated). Status: healthy (clean), degraded (some entries
skipped), offline (root unreadable).
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mcprouter import generation
from mcprouter.execution.redaction import redact
from mcprouter.models import SkillRecord, SkillSourceRecord, SkillVersionRecord
from mcprouter.settings import Settings
from mcprouter.skills.hashing import content_hash, manifest_hash
from mcprouter.skills.validate import (
    FRONTMATTER_MAX_BYTES,
    ParsedSkill,
    SkillValidationError,
    parse_skill_md,
)
from mcprouter.skills.walker import FoundSkill, walk_source


def _flags(parsed: ParsedSkill, found: FoundSkill) -> list[str]:
    flags = list(parsed.flags)
    if redact(parsed.body) != parsed.body or redact(parsed.description) != parsed.description:
        flags.append("secret_like")
    if any(e.get("oversize") for e in found.manifest):
        flags.append("resource_oversize")
    return flags


def _snapshot(p: ParsedSkill, manifest: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": p.name,
        "description": p.description,
        "license": p.license,
        "compatibility": p.compatibility,
        "metadata": p.metadata,
        "allowedTools": p.allowed_tools,
        "body": p.body,
        "manifest": manifest,
    }


def _meta_tuple(r: SkillRecord) -> tuple[Any, ...]:
    return (r.description, r.license, r.compatibility, r.skill_metadata, r.allowed_tools)


def _version(rec: SkillRecord, kind: str, snap: dict[str, Any]) -> SkillVersionRecord:
    return SkillVersionRecord(
        id=str(uuid.uuid4()),
        skill_id=rec.id,
        version=rec.version,
        content_hash=rec.content_hash,
        manifest_hash=rec.manifest_hash,
        snapshot=snap,
        change_kind=kind,
        recorded_at=datetime.now(UTC),
    )


def sync_source(
    session: Session, source: SkillSourceRecord, root: Path | str, settings: Settings
) -> dict[str, Any]:
    """Walk `root` and reconcile `source`'s skills. Caller commits."""
    report: dict[str, Any] = {"added": 0, "changed": 0, "removed": 0, "skipped": []}
    now = datetime.now(UTC)
    if not Path(root).is_dir():
        source.status = "offline"
        report["skipped"].append({"path": ".", "reason": "source root is not readable"})
        return report
    walked = walk_source(
        root,
        resource_max_bytes=settings.skill_resource_max_bytes,
        # Owner decision: read at most body cap + frontmatter cap + 1 byte.
        skill_md_max_bytes=settings.skill_body_max_bytes + FRONTMATTER_MAX_BYTES,
    )
    report["skipped"].extend(walked.skipped)
    existing = {
        r.name: r
        for r in session.scalars(select(SkillRecord).where(SkillRecord.source_id == source.id))
    }
    seen: set[str] = set()
    for found in walked.skills:
        try:
            text = found.skill_md.decode("utf-8")
            parsed = parse_skill_md(
                text, dir_name=found.dir.name, body_max_bytes=settings.skill_body_max_bytes
            )
        except UnicodeDecodeError:
            report["skipped"].append({"path": found.relative_path, "reason": "not UTF-8"})
            continue
        except SkillValidationError as exc:
            report["skipped"].append({"path": found.relative_path, "reason": str(exc)})
            continue
        if parsed.name in seen:
            report["skipped"].append({"path": found.relative_path, "reason": "duplicate name"})
            continue
        seen.add(parsed.name)
        chash, mhash = content_hash(found.skill_md), manifest_hash(found.manifest)
        fields: dict[str, Any] = {
            "description": parsed.description,
            "body": parsed.body,
            "relative_path": found.relative_path,
            "license": parsed.license,
            "compatibility": parsed.compatibility,
            "skill_metadata": parsed.metadata,
            "allowed_tools": parsed.allowed_tools,
            "resource_manifest": found.manifest,
            "has_scripts": found.has_scripts,
            "content_hash": chash,
            "manifest_hash": mhash,
            "body_tokens_est": math.ceil(len(parsed.body) / 4),
            "ingest_flags": _flags(parsed, found),
        }
        snap = _snapshot(parsed, found.manifest)
        rec = existing.get(parsed.name)
        if rec is None:
            rec = SkillRecord(
                id=str(uuid.uuid4()), source_id=source.id, name=parsed.name, version=1, **fields
            )
            session.add(rec)
            session.flush()  # no ORM relationship to versions: parent row must exist first
            session.add(_version(rec, "added", snap))
            report["added"] += 1
            continue
        old_body, old_meta = rec.body, _meta_tuple(rec)
        if not rec.available:
            kind: str | None = "restored"
        elif rec.content_hash != chash:
            kind = "content"
        elif rec.manifest_hash != mhash:
            kind = "resources"
        else:
            kind = None
        for k, v in fields.items():
            setattr(rec, k, v)
        if kind == "content" and old_body == rec.body and old_meta != _meta_tuple(rec):
            kind = "metadata"
        if kind is not None:
            rec.available = True
            rec.version += 1
            session.add(_version(rec, kind, snap))
            report["changed"] += 1
    for name, rec in existing.items():
        if name not in seen and rec.available:
            rec.available = False
            rec.version += 1
            session.add(_version(rec, "removed", {"name": name}))
            report["removed"] += 1
    source.last_synced_at = now
    source.status = "degraded" if report["skipped"] else "healthy"
    session.flush()
    if report["added"] or report["changed"] or report["removed"]:
        generation.bump_catalog()
    return report
