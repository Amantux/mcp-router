"""Duplicate-review API (FR-05/FR-08). Suggestions only: accepting a
suggestion never disables or deletes a tool.

Integration: `app.include_router(routes_dedup.router)` (needs
`registry.schema.init_registry(engine)` for the unique pair index).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from mcprouter.dedup.detect import DEFAULT_THRESHOLD, run_dedup
from mcprouter.dedup.review import (
    accept_suggestion,
    dismiss_suggestion,
    list_suggestions,
    to_out,
)
from mcprouter.registry.api_deps import curated_errors, get_session, require_admin
from mcprouter.registry.audit import audit
from mcprouter.registry.catalog import MAX_LIMIT
from mcprouter.registry.wire import (
    DedupRunIn,
    DedupRunOut,
    DismissIn,
    SuggestionOut,
    SuggestionPageOut,
)

router = APIRouter(prefix="/api/v1/dedup", tags=["dedup"])

SessionDep = Annotated[Session, Depends(get_session)]
AdminDep = Annotated[str, Depends(require_admin)]


@router.get("/suggestions", response_model=SuggestionPageOut)
def get_suggestions(
    session: SessionDep,
    _admin: AdminDep,
    status: Literal["open", "accepted", "dismissed", "all"] = "open",
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SuggestionPageOut:
    with curated_errors():
        page = list_suggestions(
            session, status=None if status == "all" else status, limit=limit, offset=offset
        )
    return SuggestionPageOut(
        items=page.items, total=page.total, limit=page.limit, offset=page.offset
    )


@router.post("/suggestions", response_model=DedupRunOut)
def run_suggestions(
    session: SessionDep, admin: AdminDep, body: DedupRunIn | None = None
) -> DedupRunOut:
    threshold = body.threshold if body and body.threshold is not None else DEFAULT_THRESHOLD
    with curated_errors():
        result = run_dedup(session, threshold=threshold)
        session.commit()
    audit("dedup.run", actor=admin, threshold=threshold, created=result.created)
    return DedupRunOut(
        pairs_considered=result.pairs_considered,
        created=result.created,
        refreshed=result.refreshed,
        skipped_decided=result.skipped_decided,
    )


@router.post("/suggestions/{suggestion_id}/accept", response_model=SuggestionOut)
def accept(suggestion_id: str, session: SessionDep, admin: AdminDep) -> SuggestionOut:
    with curated_errors():
        sug = accept_suggestion(session, suggestion_id, actor=admin)
        session.commit()
        return to_out(session, sug)


@router.post("/suggestions/{suggestion_id}/dismiss", response_model=SuggestionOut)
def dismiss(
    suggestion_id: str, body: DismissIn, session: SessionDep, admin: AdminDep
) -> SuggestionOut:
    with curated_errors():
        sug = dismiss_suggestion(
            session, suggestion_id, justification=body.justification, actor=admin
        )
        session.commit()
        return to_out(session, sug)
