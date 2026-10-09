"""POST /api/v1/route/{requestId}/feedback -- see analytics/feedback.py.

Admin bearer -> source=human on any live decision; otherwise the caller's
agent principal -> source=agent on its OWN live decision (else 404).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from mcprouter.analytics.feedback import (
    FeedbackInvalid,
    FeedbackItem,
    FeedbackNotFound,
    FeedbackRateLimited,
    record_feedback,
)
from mcprouter.api.deps_auth import get_principal, is_admin_bearer, security_of

router = APIRouter(prefix="/api/v1/route", tags=["feedback"])


class _In(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class FeedbackItemIn(_In):
    kind: Literal["tool", "skill"] | None = None
    id: str | None = Field(default=None, max_length=400)
    name: str | None = Field(default=None, max_length=400)
    helpful: bool
    note: str | None = Field(default=None, max_length=2000)  # truncated to 500 after scrub


class FeedbackIn(_In):
    items: list[FeedbackItemIn] = Field(min_length=1, max_length=50)


class FeedbackOut(_In):
    recorded: int
    source: Literal["agent", "human"]


@router.post("/{request_id}/feedback", response_model=FeedbackOut)
def post_feedback(request_id: str, body: FeedbackIn, request: Request) -> FeedbackOut:
    config, factory = security_of(request)
    auth = request.headers.get("authorization")
    if is_admin_bearer(config, auth):
        source: Literal["agent", "human"] = "human"
        agent_id, principal = None, "admin"
    else:
        p = get_principal(request)
        source, agent_id, principal = "agent", p.agent_id, f"agent:{p.agent_id}"
    items = [FeedbackItem(**i.model_dump()) for i in body.items]
    try:
        with factory() as s:
            n = record_feedback(
                s,
                request_id=request_id,
                items=items,
                source=source,
                agent_id=agent_id,
                principal=principal,
            )
    except FeedbackNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except FeedbackRateLimited as exc:
        raise HTTPException(429, str(exc)) from None
    except FeedbackInvalid as exc:
        raise HTTPException(422, str(exc)) from None
    return FeedbackOut(recorded=n, source=source)
