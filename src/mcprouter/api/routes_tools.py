"""Catalog management API (FR-02/FR-08). Thin: parsing + auth + commit;
logic lives in mcprouter.registry.

Integration: `app.include_router(routes_tools.router)` and call
`registry.schema.init_registry(engine)` after `init_db(engine)`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from mcprouter.api.acting import admin_actor
from mcprouter.api.deps import get_session
from mcprouter.api.errors import curated_errors
from mcprouter.generation import bump_catalog
from mcprouter.registry.catalog import (
    MAX_LIMIT,
    ClassificationUpdate,
    ToolFilter,
    get_tool_detail,
    search_tools,
    server_name_of,
    set_enabled,
    update_classification,
)
from mcprouter.registry.wire import (
    ClassificationPatchIn,
    Operation,
    ToolDetailOut,
    ToolOut,
    ToolPageOut,
    tool_out,
    version_out,
)

router = APIRouter(prefix="/api/v1/tools", tags=["tools"])

SessionDep = Annotated[Session, Depends(get_session)]
AdminDep = Annotated[str, Depends(admin_actor)]


@router.get("", response_model=ToolPageOut)
def list_tools(
    session: SessionDep,
    _admin: AdminDep,
    q: Annotated[str | None, Query(max_length=500)] = None,
    domain: str | None = None,
    operation: Operation | None = None,
    tags: Annotated[list[str] | None, Query()] = None,
    server_id: Annotated[str | None, Query(alias="serverId")] = None,
    enabled: bool | None = None,
    available: bool | None = None,
    classification_reviewed: Annotated[bool | None, Query(alias="classificationReviewed")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ToolPageOut:
    with curated_errors():
        page = search_tools(
            session,
            ToolFilter(
                q=q,
                domain=domain,
                operation=operation,
                tags=tags,
                server_id=server_id,
                enabled=enabled,
                available=available,
                classification_reviewed=classification_reviewed,
                limit=limit,
                offset=offset,
            ),
        )
    return ToolPageOut(
        items=[tool_out(h.tool, server_name=h.server_name, rank=h.rank) for h in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{tool_id}", response_model=ToolDetailOut)
def get_tool(tool_id: str, session: SessionDep, _admin: AdminDep) -> ToolDetailOut:
    with curated_errors():
        d = get_tool_detail(session, tool_id)
    base = tool_out(d.tool, server_name=d.server_name)
    return ToolDetailOut(**base.model_dump(), versions=[version_out(v) for v in d.versions])


@router.patch("/{tool_id}/classification", response_model=ToolOut)
def patch_classification(
    tool_id: str, body: ClassificationPatchIn, session: SessionDep, admin: AdminDep
) -> ToolOut:
    with curated_errors():
        tool = update_classification(
            session, tool_id, ClassificationUpdate(fields=body.to_fields()), actor=admin
        )
        session.commit()
        bump_catalog()  # route cache (wave 2)
        return tool_out(tool, server_name=server_name_of(session, tool.server_id))


def _toggle(session: Session, tool_id: str, enabled: bool, admin: str) -> ToolOut:
    with curated_errors():
        tool = set_enabled(session, tool_id, enabled, actor=admin)
        session.commit()
        bump_catalog()  # route cache (wave 2)
        return tool_out(tool, server_name=server_name_of(session, tool.server_id))


@router.post("/{tool_id}/enable", response_model=ToolOut)
def enable_tool(tool_id: str, session: SessionDep, admin: AdminDep) -> ToolOut:
    return _toggle(session, tool_id, True, admin)


@router.post("/{tool_id}/disable", response_model=ToolOut)
def disable_tool(tool_id: str, session: SessionDep, admin: AdminDep) -> ToolOut:
    return _toggle(session, tool_id, False, admin)
