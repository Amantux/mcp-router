"""Shared helpers moved out of tests/test_analytics_api.py (W0-2); not a test module."""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api import deps_auth
from mcprouter.api.deps_auth import configure_security
from mcprouter.api.routes_analytics import get_now, install_analytics
from mcprouter.limits import make_limiters
from mcprouter.settings import Settings
from tests.conftest import TEST_DB_URL
from tests.support.analytics import NOW

ADMIN = "admin_" + "z" * 40


H_ADMIN = {"Authorization": f"Bearer {ADMIN}"}


def _app(factory: sessionmaker[Session]) -> FastAPI:
    deps_auth._reset_dev_warning_for_tests()
    app = FastAPI()
    app.state.settings = Settings(database_url=TEST_DB_URL)
    app.state.limiters = make_limiters(app.state.settings)
    app.state.engine = factory.kw["bind"]
    app.state.session_factory = factory
    configure_security(app, {"MCPR_ADMIN_TOKEN": ADMIN})
    install_analytics(app)
    app.dependency_overrides[get_now] = lambda: NOW
    return app
