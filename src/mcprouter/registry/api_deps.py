"""FastAPI glue shared by routes_tools and routes_dedup: DB session and the
management-API admin gate (delegating to the gateway's admin dependency)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from mcprouter.api.deps_auth import require_admin as gateway_require_admin
from mcprouter.registry.errors import RegistryError

DEV_ACTOR = "local-dev"
ADMIN_ACTOR = "admin"


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

    Collapsed at integration onto the ONE admin dependency,
    `mcprouter.api.deps_auth.require_admin` (MCPR_ADMIN_TOKEN; fails closed
    outside dev mode). This wrapper only names the actor for audit lines:
    `admin` when an admin token is configured, `local-dev` in dev mode.
    """
    gateway_require_admin(request)  # raises 401/403/503; never returns on failure
    security = request.app.state.security
    return ADMIN_ACTOR if security.admin_token_hash is not None else DEV_ACTOR


@contextmanager
def curated_errors() -> Iterator[None]:
    """Map typed registry errors to their curated message. Anything else
    propagates to FastAPI's generic 500 (no exception text leaks)."""
    try:
        yield
    except RegistryError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from None
