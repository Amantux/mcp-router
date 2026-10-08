"""FastAPI glue shared by routes_tools and routes_dedup: DB session and the
management-API admin gate."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from mcprouter.registry.errors import RegistryError

DEV_ACTOR = "local-dev"


def get_session(request: Request) -> Iterator[Session]:
    """Mutating routes commit explicitly before returning; anything left
    uncommitted is rolled back by close()."""
    s: Session = request.app.state.session_factory()
    try:
        yield s
    finally:
        s.close()


def require_admin(request: Request) -> str:
    """Management API gate. Returns the acting principal (for audit lines).

    * No agent keys configured -> auth is disabled instance-wide (scoping §6;
      create_app already logs the loud warning) -> dev actor.
    * Agent keys configured -> FAIL CLOSED until integration wires a real admin
      authenticator (override this dependency via
      `app.dependency_overrides[require_admin]`). Never silently open.
    """
    if not request.app.state.settings.agent_keys:
        return DEV_ACTOR
    raise HTTPException(
        status_code=403,
        detail="Admin authentication is not configured; the management API is locked.",
    )


@contextmanager
def curated_errors() -> Iterator[None]:
    """Map typed registry errors to their curated message. Anything else
    propagates to FastAPI's generic 500 (no exception text leaks)."""
    try:
        yield
    except RegistryError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
