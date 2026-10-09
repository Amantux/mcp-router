"""Skill-source + skills read API: admin gate, sync, list/detail, delete refusal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete

from mcprouter.api import deps_auth
from mcprouter.api.deps_auth import configure_security
from mcprouter.api.routes_skill_sources import router as src_router
from mcprouter.api.routes_skills import router as skills_router
from mcprouter.models import PolicyRule, SkillSourceRecord
from mcprouter.settings import Settings

ADMIN = "admin_" + "e" * 40
H = {"Authorization": f"Bearer {ADMIN}"}


def test_api_flow(db: Any, settings: Settings, tmp_path: Path) -> None:
    deps_auth._reset_dev_warning_for_tests()
    app = FastAPI()
    app.state.settings, app.state.session_factory = settings, db
    app.state.engine = db.kw["bind"]
    configure_security(app, {"MCPR_ADMIN_TOKEN": ADMIN})
    app.include_router(src_router)
    app.include_router(skills_router)
    d = tmp_path / "alpha"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: alpha\ndescription: Alpha skill\n---\nhello\n")
    c = TestClient(app)
    try:
        assert c.get("/api/v1/skill-sources").status_code in (401, 403)
        assert c.get("/api/v1/skills").status_code in (401, 403)
        body = {"name": "local", "kind": "directory", "location": str(tmp_path)}
        sid = c.post("/api/v1/skill-sources", json=body, headers=H).json()["id"]
        bad = {"name": "g", "kind": "git", "location": "ssh://x/y"}
        assert c.post("/api/v1/skill-sources", json=bad, headers=H).status_code == 422
        rep = c.post(f"/api/v1/skill-sources/{sid}/sync", headers=H).json()
        assert rep["added"] == 1
        page = c.get("/api/v1/skills", params={"q": "alp"}, headers=H).json()
        assert page["total"] == 1
        kid = page["items"][0]["id"]
        assert c.get(f"/api/v1/skills/{kid}", headers=H).json()["frontmatter"]["name"] == "alpha"
        assert c.get(f"/api/v1/skills/{kid}/body", headers=H).json()["body"] == "hello\n"
        assert c.get(f"/api/v1/skills/{kid}/versions", headers=H).json()[0]["changeKind"] == "added"
        with db() as s:
            s.add(PolicyRule(agent_id="a", server_id=sid, resource_kind="skill"))
            s.commit()
        assert c.delete(f"/api/v1/skill-sources/{sid}", headers=H).status_code == 409
        with db() as s:
            s.execute(delete(PolicyRule).where(PolicyRule.server_id == sid))
            s.commit()
        assert c.delete(f"/api/v1/skill-sources/{sid}", headers=H).status_code == 204
    finally:
        with db() as s:
            s.execute(delete(SkillSourceRecord))
            s.commit()
