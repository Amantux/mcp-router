"""Shared FastAPI dependencies for the REST routers (wave-6 P-206, D10).

ONE spelling each for what used to be copied per router: the app's session
factory, a request-scoped session, and the execution manager. Routers import
these instead of reaching into ``request.app.state`` (or into each other).
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.execution.manager import ExecutionManager


def session_factory(request: Request) -> sessionmaker[Session]:
    """The app's sessionmaker (``app.state.session_factory``)."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    return factory


def get_session(request: Request) -> Iterator[Session]:
    """Request-scoped session. Mutating routes commit explicitly before
    returning; anything left uncommitted is rolled back by close()."""
    s = session_factory(request)()
    try:
        yield s
    finally:
        s.close()


def get_manager(request: Request) -> ExecutionManager:
    """The ONE ExecutionManager (503 when the app never installed it)."""
    mgr = getattr(request.app.state, "execution_manager", None)
    if not isinstance(mgr, ExecutionManager):
        raise HTTPException(status_code=503, detail="execution manager not configured")
    return mgr
