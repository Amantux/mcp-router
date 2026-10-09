"""Admin API: /api/v1/skills.

# ---- READ SIDE (S1: list / detail / body / versions) ----
# ---- S3 adds activate / bundle below a separate section marker ----
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.deps_auth import require_admin
from mcprouter.models import SkillRecord, SkillVersionRecord

router = APIRouter(prefix="/api/v1/skills", tags=["skills"], dependencies=[Depends(require_admin)])


def _factory(request: Request) -> sessionmaker[Session]:
    f: sessionmaker[Session] = request.app.state.session_factory
    return f


def _summary(r: SkillRecord) -> dict[str, Any]:
    return {
        "id": r.id,
        "sourceId": r.source_id,
        "name": r.name,
        "description": r.description,
        "domain": r.domain,
        "operation": r.operation,
        "version": r.version,
        "enabled": r.enabled,
        "available": r.available,
        "classificationReviewed": r.classification_reviewed,
        "hasScripts": r.has_scripts,
        "bodyTokensEst": r.body_tokens_est,
        "ingestFlags": r.ingest_flags,
    }


def _get(s: Session, skill_id: str) -> SkillRecord:
    rec = s.get(SkillRecord, skill_id)
    if rec is None:
        raise HTTPException(404, "skill not found")
    return rec


@router.get("")
def list_skills(
    request: Request,
    q: str | None = Query(default=None, max_length=200),
    domain: str | None = None,
    operation: str | None = None,
    source: str | None = None,
    enabled: bool | None = None,
    available: bool | None = None,
    reviewed: bool | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    stmt = select(SkillRecord)
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        stmt = stmt.where(or_(SkillRecord.name.ilike(like), SkillRecord.description.ilike(like)))
    for col, val in (
        (SkillRecord.domain, domain),
        (SkillRecord.operation, operation),
        (SkillRecord.source_id, source),
        (SkillRecord.enabled, enabled),
        (SkillRecord.available, available),
        (SkillRecord.classification_reviewed, reviewed),
    ):
        if val is not None:
            stmt = stmt.where(col == val)
    with _factory(request)() as s:
        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = s.scalars(
            stmt.order_by(SkillRecord.name, SkillRecord.id).limit(limit).offset(offset)
        )
        return {
            "items": [_summary(r) for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


@router.get("/{skill_id}")
def get_skill(skill_id: str, request: Request) -> dict[str, Any]:
    with _factory(request)() as s:
        r = _get(s, skill_id)
        versions = s.scalars(
            select(SkillVersionRecord)
            .where(SkillVersionRecord.skill_id == r.id)
            .order_by(SkillVersionRecord.version.desc())
            .limit(10)
        )
        return {
            **_summary(r),
            "frontmatter": {
                "name": r.name,
                "description": r.description,
                "license": r.license,
                "compatibility": r.compatibility,
                "metadata": r.skill_metadata,
                "allowedTools": r.allowed_tools,
            },
            "relativePath": r.relative_path,
            "resourceManifest": r.resource_manifest,
            "contentHash": r.content_hash,
            "manifestHash": r.manifest_hash,
            "recentVersions": [
                {
                    "version": v.version,
                    "changeKind": v.change_kind,
                    "recordedAt": v.recorded_at.isoformat(),
                }
                for v in versions
            ],
        }


@router.get("/{skill_id}/body")
def get_body(skill_id: str, request: Request) -> dict[str, Any]:
    with _factory(request)() as s:
        r = _get(s, skill_id)
        return {"id": r.id, "body": r.body, "bodyTokensEst": r.body_tokens_est}


@router.get("/{skill_id}/versions")
def get_versions(skill_id: str, request: Request) -> list[dict[str, Any]]:
    with _factory(request)() as s:
        _get(s, skill_id)
        rows = s.scalars(
            select(SkillVersionRecord)
            .where(SkillVersionRecord.skill_id == skill_id)
            .order_by(SkillVersionRecord.version)
        )
        return [
            {
                "version": v.version,
                "changeKind": v.change_kind,
                "contentHash": v.content_hash,
                "manifestHash": v.manifest_hash,
                "recordedAt": v.recorded_at.isoformat(),
            }
            for v in rows
        ]


# ---- S3: activate / bundle (write side) goes below this line ----
