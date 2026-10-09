"""P-207: `/skills/bundle` is served by the agent router, never captured by
the admin `/skills/{skill_id}` route (include order in create_app)."""

from __future__ import annotations

from dataclasses import replace

from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.app import create_app
from mcprouter.api.routes_skills_agent import UNKNOWN_SKILL, skills_bundle
from mcprouter.settings import Settings

from .conftest import requires_db

pytestmark = requires_db

ADMIN = "admin_" + "r" * 40
KEY = "agent-r-" + "z" * 32


def test_agent_key_on_skills_bundle_reaches_the_agent_handler(
    db: sessionmaker[Session], settings: Settings
) -> None:
    app = create_app(
        replace(settings, agent_keys=f"bundler:{KEY}"), env={"MCPR_ADMIN_TOKEN": ADMIN}
    )
    first = next(
        ctx
        for ctx in iter_route_contexts(app.routes)
        if "GET" in (ctx.methods or ())
        and ctx.path_regex is not None
        and ctx.path_regex.match("/api/v1/skills/bundle")
    )
    assert first.endpoint is skills_bundle

    r = TestClient(app).get("/api/v1/skills/bundle", headers={"Authorization": f"Bearer {KEY}"})
    # Nothing routed yet -> the AGENT handler's uniform 404; the admin
    # /skills/{skill_id} handler would 401 an agent key instead.
    assert (r.status_code, r.json()["detail"]) == (404, UNKNOWN_SKILL), r.text
