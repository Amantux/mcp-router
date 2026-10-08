"""App factory. Routers register here at integration; each workstream ships
its router module and ONE include line lands in this file."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from prometheus_client import make_asgi_app

from mcprouter.db import init_db, make_engine, make_session_factory
from mcprouter.settings import Settings

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="MCP Router", version="0.1.0", docs_url="/docs")
    engine = make_engine(settings)
    init_db(engine)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)

    if not settings.agent_keys:
        log.warning(
            "MCPR_AGENT_KEYS is unset — gateway auth is DISABLED. Dev only; "
            "never expose this instance beyond localhost."
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        from sqlalchemy import text

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    app.mount("/metrics", make_asgi_app())
    return app
