"""Admin API: /api/v1/skill-sources (CRUD + sync)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.deps_auth import require_admin
from mcprouter.lifecycle import skill_content_snapshot
from mcprouter.models import SkillSourceRecord
from mcprouter.skills.sources import SourceError, referencing_rules, run_sync, validate_location

router = APIRouter(
    prefix="/api/v1/skill-sources", tags=["skills"], dependencies=[Depends(require_admin)]
)


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


# No "/" (names become path/URI segments), whitespace or control chars.
_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$"


class SourceIn(_Camel):
    name: str = Field(min_length=1, max_length=120, pattern=_NAME_PATTERN)
    kind: str
    location: str = Field(min_length=1, max_length=2000)
    git_ref: str | None = Field(default=None, max_length=200)
    enabled: bool = True
    sync_interval_s: int = Field(default=3600, ge=60)


class SourcePatch(_Camel):
    name: str | None = Field(default=None, min_length=1, max_length=120, pattern=_NAME_PATTERN)
    location: str | None = Field(default=None, min_length=1, max_length=2000)
    git_ref: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    sync_interval_s: int | None = Field(default=None, ge=60)


def _factory(request: Request) -> sessionmaker[Session]:
    f: sessionmaker[Session] = request.app.state.session_factory
    return f


def _out(s: SkillSourceRecord) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "kind": s.kind,
        "location": s.location,
        "gitRef": s.git_ref,
        "enabled": s.enabled,
        "status": s.status,
        "lastSyncedAt": s.last_synced_at.isoformat() if s.last_synced_at else None,
        "lastCommit": s.last_commit,
        "syncIntervalS": s.sync_interval_s,
    }


def _get(session: Session, sid: str) -> SkillSourceRecord:
    src = session.get(SkillSourceRecord, sid)
    if src is None:
        raise HTTPException(404, "skill source not found")
    return src


@router.get("")
def list_sources(request: Request) -> list[dict[str, Any]]:
    with _factory(request)() as s:
        return [
            _out(r) for r in s.scalars(select(SkillSourceRecord).order_by(SkillSourceRecord.name))
        ]


@router.post("", status_code=201)
def create_source(body: SourceIn, request: Request) -> dict[str, Any]:
    try:
        validate_location(body.kind, body.location, body.git_ref)
    except SourceError as exc:
        raise HTTPException(422, str(exc)) from exc
    with _factory(request)() as s:
        if s.scalar(select(SkillSourceRecord.id).where(SkillSourceRecord.name == body.name)):
            raise HTTPException(409, "a skill source with that name exists")
        rec = SkillSourceRecord(**body.model_dump())
        s.add(rec)
        s.commit()
        return _out(rec)


@router.get("/{sid}")
def get_source(sid: str, request: Request) -> dict[str, Any]:
    with _factory(request)() as s:
        return _out(_get(s, sid))


@router.patch("/{sid}")
def patch_source(sid: str, body: SourcePatch, request: Request) -> dict[str, Any]:
    with _factory(request)() as s:
        rec = _get(s, sid)
        upd = body.model_dump(exclude_unset=True)
        try:
            validate_location(
                rec.kind, upd.get("location", rec.location), upd.get("git_ref", rec.git_ref)
            )
        except SourceError as exc:
            raise HTTPException(422, str(exc)) from exc
        for k, v in upd.items():
            setattr(rec, k, v)
        s.commit()
        return _out(rec)


@router.delete("/{sid}", status_code=204)
def delete_source(sid: str, request: Request) -> Response:
    with _factory(request)() as s:
        rec = _get(s, sid)
        if referencing_rules(s, sid):
            raise HTTPException(409, "skill source is referenced by policy rules")
        s.delete(rec)
        s.commit()
    return Response(status_code=204)


@router.post("/{sid}/sync")
def sync(sid: str, request: Request) -> dict[str, Any]:
    with _factory(request)() as s:
        rec = _get(s, sid)
        before = skill_content_snapshot(s, rec.id)
        report = run_sync(s, rec, request.app.state.settings)
        s.commit()
    hook = getattr(request.app.state, "skill_post_sync", None)
    if hook is not None:  # classify + embed (wired in api/app.py)
        hook(sid, before)
    return report
