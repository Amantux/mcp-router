"""Skills REST, admin read side (S1): `router` under /api/v1/skills.

Split out of routes_skills.py (wave-6 P-207); routes_skills re-exports it.
Include `routes_skills_agent.agent_router` BEFORE this router, or
`/skills/bundle` is captured by `/{skill_id}` (tests/test_rest_gaps_routing).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from mcprouter.api.deps import session_factory
from mcprouter.api.deps_auth import require_admin
from mcprouter.generation import bump_catalog
from mcprouter.models import SkillRecord, SkillVersionRecord
from mcprouter.registry.audit import audit
from mcprouter.registry.catalog import _CLASSIFICATION_COLUMNS
from mcprouter.registry.wire import ClassificationPatchIn

router = APIRouter(prefix="/api/v1/skills", tags=["skills"], dependencies=[Depends(require_admin)])


_factory = session_factory  # P-206: one spelling, in api/deps.py


def _summary(r: SkillRecord) -> dict[str, Any]:
    return {
        "id": r.id,
        "sourceId": r.source_id,
        "name": r.name,
        "description": r.description,
        "domain": r.domain,
        "operation": r.operation,
        "categories": list(r.capabilities or []),
        "tags": list(r.tags or []),
        "requiredScopes": list(r.required_scopes or []),
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
    source_id: str | None = Query(default=None, alias="sourceId"),  # D13: the UI's name
    has_scripts: bool | None = Query(default=None, alias="hasScripts"),  # D13
    enabled: bool | None = None,
    available: bool | None = None,
    reviewed: bool | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if source is not None and source_id is not None and source != source_id:
        raise HTTPException(422, "source and sourceId disagree; pass one of them")
    source = source if source is not None else source_id
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
        (SkillRecord.has_scripts, has_scripts),
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


@router.patch("/{skill_id}/classification")
def patch_classification(
    skill_id: str,
    body: ClassificationPatchIn,
    request: Request,
    admin: Annotated[str, Depends(require_admin)],
) -> dict[str, Any]:
    """Human override (mirrors the tools PATCH): wins over auto-classification
    forever, because the skill classifier skips `classification_reviewed` rows."""
    fields = body.to_fields()
    with _factory(request)() as s:
        r = _get(s, skill_id)
        for wire_name, value in fields.items():
            setattr(r, _CLASSIFICATION_COLUMNS[wire_name], value)
        r.classification_reviewed = True
        r.classification_source = "human"
        s.commit()
        audit(
            "skill.classification.override",
            actor=admin,
            skill_id=r.id,
            skill=r.name,
            fields=",".join(sorted(fields)) or "(approve)",
        )
        bump_catalog()  # route cache
        return _summary(r)


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
